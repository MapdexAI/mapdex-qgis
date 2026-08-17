"""BYOK credential storage backed by the QGIS Authentication Manager.

A provider key is a real secret. It is stored only in the encrypted QGIS
authentication database, exactly like the plugin's device token, and never in
``QSettings`` - QSettings is a plaintext INI/registry file that syncs, backs up
and gets pasted into bug reports.

If the authentication database is locked or unavailable the key is held in
memory for the session only. That is a deliberate downgrade: the alternative is
writing a plaintext fallback, which would quietly turn a secure install into an
insecure one without the user ever being told.

The store hands out the key only to the provider transport. Nothing here is
reachable from the capability registry, the companion context, or the agent
prompt, so a key cannot leak into a model request by construction.
"""
from __future__ import annotations

from typing import Any

from .guard import log_debug

# Non-secret preferences live in QSettings; the secret itself never does.
SETTINGS_PREFIX = "mapdex/nivo"
AUTH_CONFIG_NAME = "Mapdex Nivo provider key"
# Only these fields may be persisted as plain preferences.
PUBLIC_SETTING_KEYS = frozenset({"provider", "model", "base_url", "timeout", "auth_config_id", "mode"})
SECRET_KEYS = frozenset({"api_key", "token", "password", "secret", "authcfg"})


class CredentialError(Exception):
    """Storing or reading a credential failed. Never carries the secret."""


class ProviderCredentialStore:
    """Secret storage for one provider key.

    ``auth_manager`` is the QGIS ``QgsAuthManager``; it is injected so the logic
    is testable without a QGIS runtime and so a future platform keychain can be
    substituted without touching callers.
    """

    def __init__(self, auth_manager: Any = None, settings: Any = None):
        self._auth = auth_manager
        self._settings = settings
        self._session_key = ""
        self._session_only = False

    # -- availability ------------------------------------------------------

    def is_secure_storage_available(self) -> bool:
        """True when the encrypted auth database can actually be written."""
        if self._auth is None:
            return False
        try:
            if hasattr(self._auth, "masterPasswordIsSet") and not self._auth.masterPasswordIsSet():
                return False
            if hasattr(self._auth, "isDisabled") and self._auth.isDisabled():
                return False
        except Exception:
            return False
        return True

    @property
    def session_only(self) -> bool:
        """True when the key is held in memory because storage was unavailable."""
        return self._session_only

    # -- write -------------------------------------------------------------

    def store(self, provider: str, api_key: str) -> dict[str, Any]:
        """Persist a key, returning the non-secret reference to remember.

        The return value is safe to write to QSettings and safe to log: it holds
        the auth config id, never the key.
        """
        provider = str(provider or "").strip().lower()
        api_key = str(api_key or "").strip()
        if not provider:
            raise CredentialError("a provider is required")
        if not api_key:
            raise CredentialError("an API key is required")
        if not self.is_secure_storage_available():
            self._session_key = api_key
            self._session_only = True
            return {"provider": provider, "auth_config_id": "", "storage": "session"}
        try:
            config = self._new_config(provider, api_key)
            if not self._auth.storeAuthenticationConfig(config):
                raise CredentialError("the QGIS authentication database rejected the entry")
            identifier = config.id()
        except CredentialError:
            raise
        except Exception as exc:
            # The exception text is from QGIS, but a defensive scrub keeps a
            # future QGIS build from echoing the value back into a dialog.
            raise CredentialError("could not store the key securely: {}".format(_scrub(exc, api_key)))
        self._session_key = ""
        self._session_only = False
        return {"provider": provider, "auth_config_id": identifier, "storage": "auth_manager"}

    def _new_config(self, provider: str, api_key: str) -> Any:
        from qgis.core import QgsAuthMethodConfig  # noqa: PLC0415 - QGIS-only import

        config = QgsAuthMethodConfig()
        config.setName("{} ({})".format(AUTH_CONFIG_NAME, provider))
        config.setMethod("Basic")
        # The key is the password field; the username is a non-secret label so
        # the entry is recognisable in the QGIS authentication UI.
        config.setConfig("username", "mapdex-nivo")
        config.setConfig("password", api_key)
        return config

    # -- read --------------------------------------------------------------

    def load(self, auth_config_id: str = "") -> str:
        """Return the key for the provider transport, or an empty string.

        Callers must pass the result straight to the transport. It must not be
        placed in a prompt, an action payload, a log, or an error.
        """
        if self._session_key:
            return self._session_key
        identifier = str(auth_config_id or "").strip()
        if not identifier or not self.is_secure_storage_available():
            return ""
        try:
            from qgis.core import QgsAuthMethodConfig  # noqa: PLC0415 - QGIS-only import

            config = QgsAuthMethodConfig()
            if not self._auth.loadAuthenticationConfig(identifier, config, True):
                return ""
            return str(config.config("password") or "")
        except Exception:
            return ""

    def clear(self, auth_config_id: str = "") -> None:
        self._session_key = ""
        self._session_only = False
        identifier = str(auth_config_id or "").strip()
        if identifier and self.is_secure_storage_available():
            try:
                self._auth.removeAuthenticationConfig(identifier)
            except Exception as exc:  # noqa: BLE001 - the auth database may raise anything
                # The in-memory key is already gone, so the user is not left
                # holding a live session either way; the stored entry may not be.
                # Only the config id is involved in this call, never the secret,
                # so the reason is safe to record and worth recording: a key that
                # silently refuses to be deleted is exactly the failure a user
                # needs to be able to find.
                log_debug("removing the stored provider key", exc)


def _scrub(value: Any, secret: str) -> str:
    text = str(value)
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text


def public_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Strip every secret-shaped field before anything is persisted or shown.

    Applied on the way into QSettings and on the way into any diagnostic. A key
    that is never in the dictionary cannot be written to disk by a future caller
    that forgets the rule.
    """
    clean: dict[str, Any] = {}
    for key, value in (values or {}).items():
        lowered = str(key).lower()
        if lowered in SECRET_KEYS or any(marker in lowered for marker in ("key", "secret", "password", "token")):
            if lowered != "auth_config_id":
                continue
        if lowered in PUBLIC_SETTING_KEYS:
            clean[lowered] = value
    return clean


def describe_privacy(runtime: dict[str, Any]) -> str:
    """One honest sentence about where this turn's context is going.

    The privacy boundary has to be legible in the UI, not buried in a document:
    a user running a local model deserves to know their project metadata stays
    on the machine, and a user on the hosted path deserves to know it does not.
    """
    mode = str(runtime.get("runtime") or "")
    provider = str(runtime.get("provider") or "")
    if mode == "byok":
        if provider in LOCAL_PROVIDER_NAMES:
            return "Local model: your map context stays on this machine and is not sent to Mapdex."
        return "Your key, sent directly to {}: this request does not pass through Mapdex servers.".format(provider)
    if mode == "hosted":
        return "Mapdex-hosted assistant: bounded map context is sent to Mapdex and billed to your plan."
    return "No assistant runtime is configured. Add a provider key or upgrade your plan."


LOCAL_PROVIDER_NAMES = frozenset({"ollama", "openai_compatible"})
