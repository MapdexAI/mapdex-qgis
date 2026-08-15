"""Model provider abstraction: hosted by default, BYOK when a key is present.

Two runtimes, one interface:

``hosted``
    No key configured. The turn goes through Mapdex ``/v1/compose`` exactly as
    before, gated by the account's plan. This is the default and the flow is
    unchanged.

``byok``
    The user configured their own provider key. The request goes **directly** to
    that provider from QGIS, so it never transits Mapdex and costs us nothing to
    serve. BYOK always wins when a key exists, whatever the plan says - the user
    paid for that key and expects it to be used.

Everything a provider needs is behind :class:`ModelProvider`, so adding Mistral
or a corporate gateway is one subclass plus a registry entry - no change to the
agent, the capability registry, or any GIS code. GIS actions are never coupled
to a vendor.

Secrets discipline: a key is passed to the transport and nowhere else. It is
never placed in a prompt, a capability payload, a log line, an exception
message, or a companion context. :func:`redact` is applied to every error this
module raises.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

DEFAULT_TIMEOUT = 60
MAX_OUTPUT_TOKENS = 2048
# Anything that looks like a credential is masked before it can be surfaced.
SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|sk-ant-[A-Za-z0-9_\-]{8,}|AIza[A-Za-z0-9_\-]{20,}|Bearer\s+[A-Za-z0-9._\-]{8,})"
)

RUNTIME_HOSTED = "hosted"
RUNTIME_BYOK = "byok"


class ProviderError(Exception):
    """A provider call failed. The message is always redacted."""


def redact(text: Any) -> str:
    """Mask anything credential-shaped in text destined for a user or a log."""
    return SECRET_PATTERN.sub("[redacted]", str(text))


def resolve_runtime(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Decide whether this install talks to a provider directly or via Mapdex.

    The rule the product asks for, in one place:

    * a configured provider key wins outright -> ``byok``, direct to the vendor;
    * otherwise, if the plan allows assistant use -> ``hosted`` via Mapdex;
    * otherwise the user is told what to do rather than silently failing.

    ``plan_allows_hosted`` is server-reported. When it is unknown we assume the
    hosted path is available and let the API answer authoritatively - the plugin
    must not invent an entitlement decision locally.
    """
    provider = str(settings.get("provider") or "").strip().lower()
    has_key = bool(str(settings.get("api_key") or "").strip())
    # A local runtime such as Ollama is BYOK without a key: nothing leaves the
    # machine and there is no vendor to authenticate to.
    local_endpoint = bool(str(settings.get("base_url") or "").strip()) and provider in LOCAL_PROVIDERS
    plan_allows = settings.get("plan_allows_hosted")
    if provider and (has_key or local_endpoint):
        return {
            "runtime": RUNTIME_BYOK,
            "provider": provider,
            "reason": "user_key" if has_key else "local_endpoint",
            "sends_context_to_mapdex": False,
        }
    if plan_allows is False:
        return {
            "runtime": "unavailable",
            "provider": "",
            "reason": "plan_does_not_include_assistant",
            "sends_context_to_mapdex": False,
        }
    return {
        "runtime": RUNTIME_HOSTED,
        "provider": "mapdex",
        "reason": "plan_hosted",
        "sends_context_to_mapdex": True,
    }


class ModelProvider:
    """One model vendor. Implementations translate to and from the wire format.

    ``complete`` returns raw assistant text. The agent is responsible for
    parsing it into a typed capability request and validating it; a provider is
    never trusted to return something executable.
    """

    name = ""
    default_model = ""

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        transport: Callable[..., bytes] | None = None,
    ):
        self._api_key = str(api_key or "")
        self.model = str(model or self.default_model)
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = int(timeout or DEFAULT_TIMEOUT)
        # Injectable so tests never open a socket.
        self._transport = transport or _http_post

    def complete(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """Non-secret description for the settings UI and for evidence."""
        return {"provider": self.name, "model": self.model, "endpoint": self.base_url or "default"}

    def _post(self, url: str, headers: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            raw = self._transport(url, headers, payload, self.timeout)
        except urllib.error.HTTPError as exc:  # pragma: no cover - network shape
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                detail = ""
            raise ProviderError(redact("{} returned HTTP {}. {}".format(self.name, exc.code, detail)))
        except urllib.error.URLError as exc:  # pragma: no cover - network shape
            raise ProviderError(redact("Could not reach {}: {}".format(self.name, exc.reason)))
        except Exception as exc:
            raise ProviderError(redact("{} request failed: {}".format(self.name, exc)))
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, AttributeError) as exc:
            raise ProviderError(redact("{} returned a malformed response: {}".format(self.name, exc)))


def _http_post(url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: int) -> bytes:
    # Second gate at the transport itself, so a provider subclass that builds a
    # URL some other way still cannot reach file:// or another local scheme.
    if not _transport_is_safe(url):
        raise ProviderError("refusing to open a non-HTTPS provider endpoint")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed vendor hosts
        return response.read()


class OpenAIProvider(ModelProvider):
    name = "openai"
    default_model = "gpt-4o-mini"
    endpoint = "https://api.openai.com/v1"

    def complete(self, system, messages):
        base = self.base_url or self.endpoint
        payload = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "system", "content": system}] + [dict(item) for item in messages],
        }
        data = self._post(
            base + "/chat/completions",
            {"Authorization": "Bearer " + self._api_key},
            payload,
        )
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("openai returned no choices")
        return str((choices[0].get("message") or {}).get("content") or "")


class OpenAICompatibleProvider(OpenAIProvider):
    """Any server speaking the OpenAI chat-completions shape.

    Covers self-hosted vLLM, LM Studio, OpenRouter, Azure-style gateways and
    corporate proxies. A base URL is required precisely because there is no
    default host to fall back to.
    """

    name = "openai_compatible"
    default_model = ""

    def complete(self, system, messages):
        if not self.base_url:
            raise ProviderError("an OpenAI-compatible provider needs a base URL")
        return super().complete(system, messages)


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    default_model = "claude-sonnet-4-5"
    endpoint = "https://api.anthropic.com/v1"

    def complete(self, system, messages):
        base = self.base_url or self.endpoint
        # Anthropic carries the system prompt as a top-level field, not a
        # message role, and requires an explicit API version header.
        payload = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": [
                {"role": item.get("role", "user"), "content": item.get("content", "")}
                for item in messages
                if item.get("role") in {"user", "assistant"}
            ],
        }
        data = self._post(
            base + "/messages",
            {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"},
            payload,
        )
        blocks = data.get("content") or []
        text = "".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict))
        if not text:
            raise ProviderError("anthropic returned no text content")
        return text


class GeminiProvider(ModelProvider):
    name = "gemini"
    default_model = "gemini-2.0-flash"
    endpoint = "https://generativelanguage.googleapis.com/v1beta"

    def complete(self, system, messages):
        base = self.base_url or self.endpoint
        contents = []
        for item in messages:
            role = "model" if item.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": str(item.get("content") or "")}]})
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
        }
        # The key travels as a header rather than a query parameter so it cannot
        # be captured by proxy or server request logging of the URL.
        data = self._post(
            "{}/models/{}:generateContent".format(base, self.model),
            {"x-goog-api-key": self._api_key},
            payload,
        )
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError("gemini returned no candidates")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))


class OllamaProvider(ModelProvider):
    """A local model runtime. Nothing leaves the machine, so no key is needed."""

    name = "ollama"
    default_model = "llama3.1"
    endpoint = "http://127.0.0.1:11434"

    def complete(self, system, messages):
        base = self.base_url or self.endpoint
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}] + [dict(item) for item in messages],
        }
        data = self._post(base + "/api/chat", {}, payload)
        message = data.get("message") or {}
        text = str(message.get("content") or "")
        if not text:
            raise ProviderError("ollama returned no message content")
        return text


PROVIDERS: dict[str, type[ModelProvider]] = {
    OpenAIProvider.name: OpenAIProvider,
    AnthropicProvider.name: AnthropicProvider,
    GeminiProvider.name: GeminiProvider,
    OpenAICompatibleProvider.name: OpenAICompatibleProvider,
    OllamaProvider.name: OllamaProvider,
}

# Providers that run on the user's own machine or network: usable without a key.
LOCAL_PROVIDERS = frozenset({OllamaProvider.name, OpenAICompatibleProvider.name})

# Providers that must never be contacted without TLS, because a key is sent.
KEYED_PROVIDERS = frozenset({OpenAIProvider.name, AnthropicProvider.name, GeminiProvider.name})


def register_provider(provider: type[ModelProvider]) -> None:
    """Extension point for contributors adding a vendor. One line, no forks."""
    if not provider.name:
        raise ValueError("a provider needs a name")
    PROVIDERS[provider.name] = provider


def available_providers() -> list[str]:
    return sorted(PROVIDERS)


def build_provider(settings: Mapping[str, Any], transport: Callable[..., bytes] | None = None) -> ModelProvider:
    """Instantiate the configured provider, refusing unsafe transports.

    A key must not be sent over plaintext HTTP. Localhost is exempt because the
    traffic never leaves the machine, which is the whole point of the local
    runtime option.
    """
    name = str(settings.get("provider") or "").strip().lower()
    factory = PROVIDERS.get(name)
    if factory is None:
        raise ProviderError("unknown provider: {}".format(name or "(none)"))
    base_url = str(settings.get("base_url") or "").strip()
    api_key = str(settings.get("api_key") or "")
    if base_url and not _transport_is_safe(base_url):
        # Checked for every provider, not only keyed ones: a keyless local
        # provider pointed at file:// or gopher:// would otherwise turn the
        # model transport into an arbitrary URL opener.
        raise ProviderError(
            "a provider endpoint must be https, or http on localhost; refusing {}".format(
                base_url.split("://")[0][:12] or "that scheme"
            )
        )
    if name in KEYED_PROVIDERS and not api_key:
        raise ProviderError("{} needs an API key".format(name))
    return factory(
        api_key=api_key,
        model=str(settings.get("model") or ""),
        base_url=base_url,
        timeout=int(settings.get("timeout") or DEFAULT_TIMEOUT),
        transport=transport,
    )


def _transport_is_safe(url: str) -> bool:
    lowered = url.lower()
    if lowered.startswith("https://"):
        return True
    if not lowered.startswith("http://"):
        return False
    host = lowered[len("http://"):].split("/")[0].split(":")[0]
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"}
