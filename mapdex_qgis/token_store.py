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
# The name of a QSettings key, not a token. It is deleted wherever it is found.
LEGACY_TOKEN_SETTING = "mapdex/token"  # nosec B105


class SecureTokenStore:
    def __init__(self, settings, auth_manager, config_factory):
        self._settings = settings
        self._auth_manager = auth_manager
        self._config_factory = config_factory

    def is_available(self) -> bool:
        """Whether QGIS secure storage is already unlocked and usable.

        `storeAuthenticationConfig` opens QGIS's global master-password dialog
        when the database is locked. That dialog looks like Mapdex is asking
        for a credential and interrupts an otherwise completed browser sign-in.
        Mapdex must never trigger it implicitly; an unavailable store means a
        session-only connection, as documented by the plugin UI.
        """
        try:
            if (
                hasattr(self._auth_manager, "masterPasswordIsSet")
                and not self._auth_manager.masterPasswordIsSet()
            ):
                return False
            if (
                hasattr(self._auth_manager, "isDisabled")
                and self._auth_manager.isDisabled()
            ):
                return False
        except Exception:
            return False
        return True

    def load(self) -> str:
        if not self.is_available():
            return ""
        config_id = str(self._settings.value(AUTH_CONFIG_SETTING, "") or "")
        if not config_id:
            return ""
        config = self._config_factory()
        if not self._auth_manager.loadAuthenticationConfig(config_id, config, True):
            return ""
        return str(config.config("password", "") or "")

    def save(self, token: str) -> bool:
        if not token or not self.is_available():
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

    # ── The connector session ────────────────────────────────────────────────
    #
    # A device grant produced one bearer token and nothing else. A connector
    # produces three things that must survive together: the access token, the
    # refresh token that replaces it, and when the access token stops working.
    #
    # They live in the same encrypted config as the old single token, under
    # their own keys, because the alternative - a second config, or QSettings
    # for "just the expiry" - splits one credential across two stores that can
    # then disagree about whether a person is connected.

    def load_session(self) -> dict:
        """The stored connector session, or what an older install left behind.

        An install that connected with the device grant has a bearer token and
        no refresh token. It is returned as the access token with no expiry,
        which is exactly true of it: it keeps working until the server stops
        accepting it, and there is nothing to refresh it with. That person is
        not signed out by upgrading; they reconnect when it lapses.
        """
        if not self.is_available():
            return {}
        config_id = str(self._settings.value(AUTH_CONFIG_SETTING, "") or "")
        if not config_id:
            return {}
        config = self._config_factory()
        if not self._auth_manager.loadAuthenticationConfig(config_id, config, True):
            return {}
        access = str(config.config("password", "") or "")
        if not access:
            return {}
        try:
            expires_at = float(config.config("mapdex_expires_at", "") or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        return {
            "access_token": access,
            "refresh_token": str(config.config("mapdex_refresh_token", "") or ""),
            "expires_at": expires_at,
            # Which flow produced this. Read rather than inferred from the
            # presence of a refresh token: a connector session whose refresh
            # token was somehow lost is still a connector session, and treating
            # it as a device one would send the person down the wrong recovery.
            "kind": str(config.config("mapdex_kind", "") or "device"),
        }

    def save_session(self, access_token: str, refresh_token: str, expires_at: float) -> bool:
        """Store all three parts, or none of them.

        The server ROTATES refresh tokens: the one that comes back replaces the
        one that was sent, and re-presenting the old one revokes the whole
        chain as a theft signal. So a save that wrote the access token and lost
        the refresh token would not merely lose a convenience - the next
        refresh would present a revoked token and end the connection.
        """
        if not access_token or not self.is_available():
            return False
        config_id = str(self._settings.value(AUTH_CONFIG_SETTING, "") or "")
        config = self._config_factory()
        if config_id:
            self._auth_manager.loadAuthenticationConfig(config_id, config, True)
            config.setId(config_id)
        config.setName("Mapdex for QGIS")
        config.setMethod("Basic")
        config.setConfig("username", "mapdex-connector")
        config.setConfig("password", access_token)
        config.setConfig("mapdex_refresh_token", refresh_token)
        config.setConfig("mapdex_expires_at", str(expires_at))
        config.setConfig("mapdex_kind", "connector")
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
        if config_id and self.is_available():
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
