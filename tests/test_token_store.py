import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.token_store import AUTH_CONFIG_SETTING, LEGACY_TOKEN_SETTING, SecureTokenStore


class FakeSettings:
    def __init__(self):
        self.values = {LEGACY_TOKEN_SETTING: "plaintext-old-token"}

    def value(self, key, default=""):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def remove(self, key):
        self.values.pop(key, None)


class FakeConfig:
    def __init__(self):
        self._id = ""
        self.values = {}

    def setId(self, value):
        self._id = value

    def id(self):
        return self._id

    def setName(self, value):
        self.values["name"] = value

    def setMethod(self, value):
        self.values["method"] = value

    def setConfig(self, key, value):
        self.values[key] = value

    def config(self, key, default=""):
        return self.values.get(key, default)


class FakeAuthManager:
    def __init__(self):
        self.saved = {}

    def storeAuthenticationConfig(self, config):
        config.setId("auth_1")
        self.saved[config.id()] = dict(config.values)
        return True

    def updateAuthenticationConfig(self, config):
        self.saved[config.id()] = dict(config.values)
        return True

    def loadAuthenticationConfig(self, config_id, config, _full):
        if config_id not in self.saved:
            return False
        config.setId(config_id)
        config.values.update(self.saved[config_id])
        return True

    def removeAuthenticationConfig(self, config_id):
        self.saved.pop(config_id, None)
        return True

    def masterPasswordIsVerified(self):
        return True


class LockedAuthManager(FakeAuthManager):
    def __init__(self):
        super().__init__()
        self.store_attempted = False

    def masterPasswordIsVerified(self):
        return False

    def storeAuthenticationConfig(self, config):
        self.store_attempted = True
        return super().storeAuthenticationConfig(config)


def test_token_is_kept_in_auth_database_not_qsettings():
    settings = FakeSettings()
    manager = FakeAuthManager()
    store = SecureTokenStore(settings, manager, FakeConfig)

    assert store.save("secret-jwt")
    assert settings.values == {AUTH_CONFIG_SETTING: "auth_1"}
    assert manager.saved["auth_1"]["password"] == "secret-jwt"
    assert store.load() == "secret-jwt"


def test_clear_removes_auth_config_and_legacy_plaintext():
    settings = FakeSettings()
    manager = FakeAuthManager()
    store = SecureTokenStore(settings, manager, FakeConfig)
    store.save("secret-jwt")

    store.clear()

    assert settings.values == {}
    assert manager.saved == {}


def test_a_connector_session_keeps_all_three_parts_together():
    # The server rotates refresh tokens: the one that comes back replaces the
    # one sent, and re-presenting a rotated token revokes the whole chain as a
    # theft signal. A save that kept the access token and dropped the refresh
    # token would not lose a convenience, it would end the connection at the
    # next refresh.
    settings = FakeSettings()
    store = SecureTokenStore(settings, FakeAuthManager(), FakeConfig)

    assert store.save_session("access-1", "refresh-1", 1234.5)
    session = store.load_session()
    assert session["access_token"] == "access-1"
    assert session["refresh_token"] == "refresh-1"
    assert session["expires_at"] == 1234.5
    assert session["kind"] == "connector"


def test_a_rotated_session_replaces_the_previous_one():
    settings = FakeSettings()
    store = SecureTokenStore(settings, FakeAuthManager(), FakeConfig)
    store.save_session("access-1", "refresh-1", 100.0)
    store.save_session("access-2", "refresh-2", 200.0)

    session = store.load_session()
    assert session["access_token"] == "access-2"
    # The old refresh token must be gone. Keeping it would leave a revoked
    # credential on disk that a later read could present.
    assert session["refresh_token"] == "refresh-2"
    assert settings.values[AUTH_CONFIG_SETTING] == "auth_1"


def test_an_older_device_install_is_not_signed_out_by_the_upgrade():
    # Somebody who connected with the device grant has a bearer token and no
    # refresh token. It keeps working until the server stops accepting it, and
    # there is nothing to refresh it with, which is exactly what this reports.
    settings = FakeSettings()
    manager = FakeAuthManager()
    store = SecureTokenStore(settings, manager, FakeConfig)
    store.save("device-era-token")

    session = store.load_session()
    assert session["access_token"] == "device-era-token"
    assert session["refresh_token"] == ""
    assert session["expires_at"] == 0.0
    assert session["kind"] == "device"


def test_no_stored_session_reads_as_nothing_rather_than_an_empty_token():
    store = SecureTokenStore(FakeSettings(), FakeAuthManager(), FakeConfig)
    assert store.load_session() == {}


def test_a_locked_auth_database_never_prompts_during_connection():
    settings = FakeSettings()
    manager = LockedAuthManager()
    store = SecureTokenStore(settings, manager, FakeConfig)

    assert not store.save_session("access-1", "refresh-1", 1234.5)
    assert not manager.store_attempted
    assert AUTH_CONFIG_SETTING not in settings.values


def test_a_locked_auth_database_is_not_opened_while_loading_or_clearing():
    settings = FakeSettings()
    settings.values[AUTH_CONFIG_SETTING] = "auth_existing"
    manager = LockedAuthManager()
    manager.saved["auth_existing"] = {"password": "secret"}
    store = SecureTokenStore(settings, manager, FakeConfig)

    assert store.load_session() == {}
    store.clear()
    assert manager.saved["auth_existing"]["password"] == "secret"
    assert AUTH_CONFIG_SETTING not in settings.values


def test_a_corrupt_expiry_does_not_take_the_session_down():
    # An unreadable expiry means "refresh it", which costs one request. Raising
    # here would make a stored session unloadable and sign the person out.
    settings = FakeSettings()
    manager = FakeAuthManager()
    store = SecureTokenStore(settings, manager, FakeConfig)
    store.save_session("access-1", "refresh-1", 100.0)
    manager.saved["auth_1"]["mapdex_expires_at"] = "not a number"

    session = store.load_session()
    assert session["access_token"] == "access-1"
    assert session["expires_at"] == 0.0
