"""Secure persistence for the Mapdex bearer token.

Only the QGIS Authentication Manager configuration id is kept in QSettings.
The token itself is stored as the password of an encrypted QGIS auth config.
If the user's authentication database is unavailable, the plugin deliberately
keeps the token in memory for the current QGIS session instead of downgrading
to plaintext storage.
"""
from __future__ import annotations

from typing import Optional


AUTH_CONFIG_SETTING = "mapdex/auth_config_id"
LEGACY_TOKEN_SETTING = "mapdex/token"  # nosec B105 - settings key, removed on sight


class SecureTokenStore:
    def __init__(self, settings, auth_manager, config_factory):
        self._settings = settings
        self._auth_manager = auth_manager
        self._config_factory = config_factory

    def load(self) -> str:
        config_id = str(self._settings.value(AUTH_CONFIG_SETTING, "") or "")
        if not config_id:
            return ""
        config = self._config_factory()
        if not self._auth_manager.loadAuthenticationConfig(config_id, config, True):
            return ""
        return str(config.config("password", "") or "")

    def save(self, token: str) -> bool:
        if not token:
            return False
        config_id = str(self._settings.value(AUTH_CONFIG_SETTING, "") or "")
        config = self._config_factory()
        if config_id:
            self._auth_manager.loadAuthenticationConfig(config_id, config, True)
            config.setId(config_id)
        config.setName("Mapdex for QGIS")
        config.setMethod("Basic")
        config.setConfig("username", "mapdex-device")
        config.setConfig("password", token)
        stored = (
            self._auth_manager.updateAuthenticationConfig(config)
            if config_id
            else self._auth_manager.storeAuthenticationConfig(config)
        )
        if not stored:
            return False
        saved_id = str(config.id() or config_id)
        if not saved_id:
            return False
        self._settings.setValue(AUTH_CONFIG_SETTING, saved_id)
        self._settings.remove(LEGACY_TOKEN_SETTING)
        return True

    def clear(self) -> None:
        config_id = str(self._settings.value(AUTH_CONFIG_SETTING, "") or "")
        if config_id:
            self._auth_manager.removeAuthenticationConfig(config_id)
        self._settings.remove(AUTH_CONFIG_SETTING)
        self._settings.remove(LEGACY_TOKEN_SETTING)


def qgis_token_store(settings) -> Optional[SecureTokenStore]:
    """Create the real QGIS-backed store, or return None outside QGIS."""
    try:
        from qgis.core import QgsApplication, QgsAuthMethodConfig

        return SecureTokenStore(settings, QgsApplication.authManager(), QgsAuthMethodConfig)
    except (ImportError, AttributeError):
        return None
