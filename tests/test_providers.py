"""BYOK runtime resolution, provider adapters and credential-leak tests."""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.credentials import (  # noqa: E402
    CredentialError,
    ProviderCredentialStore,
    describe_privacy,
    public_settings,
)
from mapdex_qgis.providers import (  # noqa: E402
    AnthropicProvider,
    GeminiProvider,
    ModelProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderError,
    available_providers,
    build_provider,
    default_model_for,
    redact,
    register_provider,
    resolve_runtime,
)

SECRET = "sk-test-abcdef1234567890"


class Recorder:
    """Captures the outgoing request instead of opening a socket."""

    def __init__(self, response):
        self.response = response
        self.url = ""
        self.headers = {}
        self.payload = {}

    def __call__(self, url, headers, payload, timeout):
        self.url = url
        self.headers = dict(headers)
        self.payload = payload
        return json.dumps(self.response).encode("utf-8")


# --------------------------------------------------------------------------
# Runtime resolution: hosted by default, BYOK whenever a key exists
# --------------------------------------------------------------------------

def test_without_a_key_the_turn_stays_on_the_hosted_mapdex_path():
    runtime = resolve_runtime({"plan_allows_hosted": True})
    assert runtime["runtime"] == "hosted"
    assert runtime["provider"] == "mapdex"
    assert runtime["sends_context_to_mapdex"] is True


def test_a_configured_key_switches_to_byok_and_bypasses_mapdex():
    runtime = resolve_runtime({"provider": "openai", "api_key": SECRET, "plan_allows_hosted": True})
    assert runtime["runtime"] == "byok"
    assert runtime["provider"] == "openai"
    # The point of BYOK: no request through our servers, so no server cost.
    assert runtime["sends_context_to_mapdex"] is False


def test_byok_wins_even_when_the_plan_would_not_allow_hosted_use():
    runtime = resolve_runtime({"provider": "anthropic", "api_key": SECRET, "plan_allows_hosted": False})
    assert runtime["runtime"] == "byok"


def test_a_local_endpoint_is_byok_without_needing_a_key():
    runtime = resolve_runtime({"provider": "ollama", "base_url": "http://127.0.0.1:11434"})
    assert runtime["runtime"] == "byok"
    assert runtime["reason"] == "local_endpoint"


def test_no_key_and_no_entitlement_is_reported_rather_than_failing_silently():
    runtime = resolve_runtime({"plan_allows_hosted": False})
    assert runtime["runtime"] == "unavailable"
    assert "plan" in runtime["reason"]


def test_unknown_entitlement_defaults_to_hosted_so_the_server_decides():
    assert resolve_runtime({})["runtime"] == "hosted"


# --------------------------------------------------------------------------
# Provider adapters
# --------------------------------------------------------------------------

def test_openai_adapter_sends_a_bearer_key_and_reads_the_message():
    recorder = Recorder({"choices": [{"message": {"content": "hello"}}]})
    provider = OpenAIProvider(api_key=SECRET, transport=recorder)
    assert provider.complete("sys", [{"role": "user", "content": "hi"}]) == "hello"
    assert recorder.headers["Authorization"] == "Bearer " + SECRET
    assert recorder.payload["messages"][0]["role"] == "system"


def test_anthropic_adapter_uses_a_top_level_system_field_and_version_header():
    recorder = Recorder({"content": [{"text": "hi there"}]})
    provider = AnthropicProvider(api_key=SECRET, transport=recorder)
    assert provider.complete("sys", [{"role": "user", "content": "hi"}]) == "hi there"
    assert recorder.payload["system"] == "sys"
    assert recorder.headers["anthropic-version"] == "2023-06-01"
    # A system role inside messages is invalid for this vendor.
    assert all(item["role"] != "system" for item in recorder.payload["messages"])


def test_gemini_adapter_sends_the_key_as_a_header_not_in_the_url():
    recorder = Recorder({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    provider = GeminiProvider(api_key=SECRET, transport=recorder)
    assert provider.complete("sys", [{"role": "user", "content": "hi"}]) == "ok"
    assert recorder.headers["x-goog-api-key"] == SECRET
    # A key in a query string leaks into proxy and server access logs.
    assert SECRET not in recorder.url


def test_ollama_adapter_needs_no_key_and_disables_streaming():
    recorder = Recorder({"message": {"content": "local answer"}})
    provider = OllamaProvider(transport=recorder)
    assert provider.complete("sys", [{"role": "user", "content": "hi"}]) == "local answer"
    assert recorder.payload["stream"] is False
    assert recorder.headers == {}


def test_a_malformed_provider_response_is_an_error_not_a_crash():
    def broken(url, headers, payload, timeout):
        return b"not json"

    with pytest.raises(ProviderError):
        OpenAIProvider(api_key=SECRET, transport=broken).complete("s", [])
    empty = Recorder({"choices": []})
    with pytest.raises(ProviderError):
        OpenAIProvider(api_key=SECRET, transport=empty).complete("s", [])


def test_a_transport_exception_becomes_a_redacted_provider_error():
    def explode(url, headers, payload, timeout):
        raise RuntimeError("failed with token Bearer " + SECRET)

    with pytest.raises(ProviderError) as info:
        OpenAIProvider(api_key=SECRET, transport=explode).complete("s", [])
    assert SECRET not in str(info.value)
    assert "[redacted]" in str(info.value)


# --------------------------------------------------------------------------
# Construction safety
# --------------------------------------------------------------------------

def test_a_key_is_never_sent_over_plain_http():
    with pytest.raises(ProviderError):
        build_provider({"provider": "openai", "api_key": SECRET, "base_url": "http://example.com/v1"})


def test_localhost_plain_http_is_allowed_because_nothing_leaves_the_machine():
    provider = build_provider({"provider": "ollama", "base_url": "http://127.0.0.1:11434"})
    assert isinstance(provider, OllamaProvider)


def test_a_keyed_provider_without_a_key_is_refused():
    with pytest.raises(ProviderError):
        build_provider({"provider": "anthropic"})


def test_unknown_providers_are_refused():
    with pytest.raises(ProviderError):
        build_provider({"provider": "definitely-not-a-vendor", "api_key": SECRET})


def test_openai_compatible_requires_an_explicit_base_url():
    settings = {"provider": "openai_compatible", "base_url": "https://gw.example/v1", "model": "gw-1"}
    provider = build_provider(settings)
    assert provider.base_url == "https://gw.example/v1"
    with pytest.raises(ProviderError):
        build_provider(dict(settings, base_url="")).complete("s", [])


def test_a_provider_with_no_default_model_is_refused_rather_than_sent_an_empty_one():
    # An arbitrary gateway serves arbitrary model names, so there is nothing to
    # guess. Refusing here names the fix; posting {"model": ""} produces a vendor
    # error about a field the user never knew they had to fill in.
    with pytest.raises(ProviderError):
        build_provider({"provider": "openai_compatible", "base_url": "https://gw.example/v1"})


def test_a_blank_model_falls_back_to_the_provider_default():
    assert build_provider({"provider": "openai", "api_key": SECRET}).model == "gpt-4o-mini"
    assert build_provider({"provider": "anthropic", "api_key": SECRET}).model == "claude-sonnet-4-5"
    assert build_provider({"provider": "ollama", "base_url": "http://127.0.0.1:11434"}).model == "llama3.1"
    assert default_model_for("gemini") == "gemini-2.0-flash"
    assert default_model_for("openai_compatible") == ""
    assert default_model_for("not-a-vendor") == ""


def test_contributors_can_register_a_provider_without_editing_the_agent():
    class Corporate(ModelProvider):
        name = "corporate_gateway"
        default_model = "internal-1"

        def complete(self, system, messages):
            return "fixed"

    register_provider(Corporate)
    assert "corporate_gateway" in available_providers()
    built = build_provider({"provider": "corporate_gateway", "base_url": "https://gw.internal/v1"})
    assert built.complete("s", []) == "fixed"


def test_describe_never_includes_the_key():
    described = OpenAIProvider(api_key=SECRET).describe()
    assert SECRET not in json.dumps(described)


def test_redaction_masks_every_common_key_shape():
    for secret in ("sk-abcdefghijklmnop", "sk-ant-abcdefghijkl", "AIza" + "b" * 24, "Bearer abcdefgh.ijk"):
        assert secret not in redact("failed using " + secret)


# --------------------------------------------------------------------------
# Credential storage
# --------------------------------------------------------------------------

class FakeAuthManager:
    def __init__(self, available=True):
        self.available = available
        self.stored = {}

    def masterPasswordIsSet(self):
        return self.available

    def isDisabled(self):
        return not self.available


def test_without_a_usable_auth_database_the_key_is_session_only_not_plaintext():
    store = ProviderCredentialStore(auth_manager=FakeAuthManager(available=False))
    result = store.store("openai", SECRET)
    assert result["storage"] == "session"
    assert result["auth_config_id"] == ""
    assert store.session_only is True
    # It is still usable for this session, just never written to disk.
    assert store.load() == SECRET


def test_no_auth_manager_at_all_is_treated_as_insecure_storage():
    store = ProviderCredentialStore(auth_manager=None)
    assert store.is_secure_storage_available() is False
    assert store.store("openai", SECRET)["storage"] == "session"


def test_storing_requires_both_a_provider_and_a_key():
    store = ProviderCredentialStore(auth_manager=FakeAuthManager(False))
    with pytest.raises(CredentialError):
        store.store("", SECRET)
    with pytest.raises(CredentialError):
        store.store("openai", "  ")


def test_clearing_forgets_the_session_key():
    store = ProviderCredentialStore(auth_manager=FakeAuthManager(False))
    store.store("openai", SECRET)
    store.clear()
    assert store.load() == ""
    assert store.session_only is False


def test_public_settings_drop_every_secret_shaped_field():
    clean = public_settings({
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": SECRET,
        "token": "t",
        "password": "p",
        "provider_secret": "s",
        "auth_config_id": "cfg1",
        "unrelated": "x",
    })
    assert clean == {"provider": "openai", "model": "gpt-4o-mini", "auth_config_id": "cfg1"}
    assert SECRET not in json.dumps(clean)


def test_privacy_statement_tells_the_user_where_context_goes():
    # Every state names Mapdex or names its absence. The BYOK sentences are
    # asserted against the live wiring in tests/test_privacy_claims.py rather
    # than pinned to a literal here: pinning the words in two places is how the
    # copy and the code came apart in the first place.
    assert "sent to Mapdex" in describe_privacy({"runtime": "byok", "provider": "ollama"})
    assert "sent to Mapdex" in describe_privacy({"runtime": "byok", "provider": "openai"})
    assert "sent to Mapdex" in describe_privacy({"runtime": "hosted", "provider": "mapdex"})
    assert "No assistant runtime" in describe_privacy({"runtime": "unavailable"})


def test_a_keyless_local_provider_cannot_point_at_a_non_http_scheme():
    # Without this the model transport becomes an arbitrary URL opener.
    for bad in ("file:///etc/passwd", "gopher://x/1", "ftp://host/x"):
        with pytest.raises(ProviderError):
            build_provider({"provider": "ollama", "base_url": bad})


def test_a_remote_openai_compatible_gateway_must_use_tls():
    with pytest.raises(ProviderError):
        build_provider({"provider": "openai_compatible", "base_url": "http://gw.example/v1", "model": "gw-1"})
    assert build_provider({"provider": "openai_compatible", "base_url": "https://gw.example/v1", "model": "gw-1"})
