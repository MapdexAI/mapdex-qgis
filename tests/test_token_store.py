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
