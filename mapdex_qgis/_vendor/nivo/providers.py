# SPDX-License-Identifier: MIT
"""Model provider abstraction: hosted by default, BYOK when a key is present.

Two runtimes, one interface:

``hosted``
    No key configured. The turn goes through the host application's own
    endpoint, gated by whatever entitlement that host reports. This package
    does not make that call; it only says which runtime applies.

``byok``
    The user configured their own provider key. The request goes **directly**
    to that provider from the host, so it never transits the host's servers.
    BYOK always wins when a key exists, whatever the plan says - the user paid
    for that key and expects it to be used.

    A BYOK session is bound to :func:`nivo.capabilities.offline_capability_ids`,
    so a mode that deliberately cannot reach the host's servers is never
    offered capabilities that execute there. A host that advertises this
    privacy property owes its users a test that the branch is really taken;
    copy is not evidence.

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
# A chat completion is a few kilobytes. A body far past this is a hostile or
# broken endpoint, and reading it whole would take the host application's memory.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Anything that looks like a credential is masked before it can be surfaced.
# Case-insensitive, and the bearer alphabet includes base64's `+/=`, because a
# pattern that only matches the vendors' own spelling misses a gateway's token.
# Keys with no recognisable shape are masked by value in ModelProvider._scrub.
SECRET_PATTERN = re.compile(
    r"(?i)(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{8,}|gsk_[A-Za-z0-9]{8,}|xai-[A-Za-z0-9_\-]{8,}"
    r"|hf_[A-Za-z0-9]{8,}|AIza[A-Za-z0-9_\-]{20,}|bearer\s+[A-Za-z0-9._~+/=\-]{8,}"
    r"|(?:api[_-]?key|access[_-]?token|key|token)=[^&\s]{8,})"
)

RUNTIME_HOSTED = "hosted"
RUNTIME_BYOK = "byok"

# What the hosted runtime calls itself when no vendor key is configured. A host
# application overrides it per call through ``settings["hosted_provider_name"]``
# so this package never has to know a product name.
HOSTED_PROVIDER_NAME = "hosted"


class ProviderError(Exception):
    """A provider call failed. The message is always redacted."""


def redact(text: Any) -> str:
    """Mask anything credential-shaped in text destined for a user or a log."""
    return SECRET_PATTERN.sub("[redacted]", str(text))


def resolve_runtime(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Decide whether this install talks to a provider directly or via the host.

    The rule, in one place:

    * a configured provider key wins outright -> ``byok``, direct to the vendor;
    * a selected provider with no usable credential -> ``unavailable``;
    * otherwise, if no provider was selected and the plan allows assistant use
      -> ``hosted`` via the host;
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
            "sends_context_to_host": False,
        }
    if provider:
        return {
            "runtime": "unavailable",
            "provider": provider,
            "reason": "provider_credential_missing",
            "sends_context_to_host": False,
        }
    if plan_allows is False:
        return {
            "runtime": "unavailable",
            "provider": "",
            "reason": "plan_does_not_include_assistant",
            "sends_context_to_host": False,
        }
    return {
        "runtime": RUNTIME_HOSTED,
        "provider": str(settings.get("hosted_provider_name") or HOSTED_PROVIDER_NAME),
        "reason": "plan_hosted",
        "sends_context_to_host": True,
    }


# Providers that genuinely run on the user's own computer. An OpenAI-compatible
# endpoint is deliberately absent: it is usually somebody else's server.
ON_MACHINE_PROVIDERS = frozenset({"ollama"})


def describe_privacy(runtime: Mapping[str, Any], service_name: str = "the hosted service") -> str:
    """One honest sentence about where this turn's context is going.

    The privacy boundary has to be legible in the UI, not buried in a document:
    a user running a local model deserves to know their project metadata stays
    on the machine, and a user on the hosted path deserves to know it does not.

    ``service_name`` is supplied by the host application. This package cannot
    name a service it does not know, and a wrong name here is a false privacy
    claim rather than a cosmetic defect.
    """
    mode = str(runtime.get("runtime") or "")
    provider = str(runtime.get("provider") or "")
    if mode == RUNTIME_BYOK:
        if provider in ON_MACHINE_PROVIDERS:
            return (
                "Local model: your map context stays on this machine and is not sent "
                "to {}.".format(service_name)
            )
        if provider == "openai_compatible":
            return (
                "Your own endpoint: this request goes there and does not pass through "
                "{} servers.".format(service_name)
            )
        return (
            "Your key, sent directly to {}: this request does not pass through {} "
            "servers.".format(provider, service_name)
        )
    if mode == RUNTIME_HOSTED:
        return (
            "{}-hosted assistant: bounded map context is sent to {} and billed to your "
            "plan.".format(service_name, service_name)
        )
    return "No assistant runtime is configured. Add a provider key or upgrade your plan."


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

    def _scrub(self, text: Any) -> str:
        """Redact credential shapes, and this provider's own key by value.

        A pattern can only recognise keys that look like a known vendor's. The
        configured key is the one value certain to be a secret, whatever it looks
        like, so it is removed literally as well.
        """
        cleaned = redact(text)
        if len(self._api_key) >= 8:
            cleaned = cleaned.replace(self._api_key, "[redacted]")
        return cleaned

    def _post(self, url: str, headers: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
        # `from None` on every re-raise: the original exception keeps the
        # unredacted text in __context__, and a traceback formatter prints it.
        try:
            raw = self._transport(url, headers, payload, self.timeout)
        except urllib.error.HTTPError as exc:  # pragma: no cover - network shape
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                detail = ""
            raise ProviderError(self._scrub("{} returned HTTP {}. {}".format(self.name, exc.code, detail))) from None
        except urllib.error.URLError as exc:  # pragma: no cover - network shape
            raise ProviderError(self._scrub("Could not reach {}: {}".format(self.name, exc.reason))) from None
        except Exception as exc:
            raise ProviderError(self._scrub("{} request failed: {}".format(self.name, exc))) from None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, AttributeError) as exc:
            raise ProviderError(self._scrub("{} returned a malformed response: {}".format(self.name, exc))) from None


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
    # The scheme audit this warns about is the `_transport_is_safe` check above:
    # https anywhere, http only on loopback, and every other scheme - file:,
    # ftp:, custom handlers - refused before a Request is built. The host is not
    # fixed, because a user may point the plugin at their own OpenAI-compatible
    # gateway or a local Ollama; the scheme is what is constrained.
    #
    # Redirects are refused. urllib's default handler copies every request
    # header, the key included, onto the redirected request and allows an http
    # target, and the scheme check above runs only on the first URL. So an
    # endpoint answering `302 Location: http://anywhere/` received the key in
    # cleartext at a host nobody configured. A model POST has no legitimate
    # redirect to follow.
    opener = urllib.request.build_opener(_RefuseRedirects)
    with opener.open(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError("the provider response is larger than this client accepts")
    return raw


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Turn any redirect into an error instead of following it with the key."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib signature
        return None


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


def default_model_for(provider: str) -> str:
    """The model this provider uses when the user named none.

    Exposed so the settings field can show the real default instead of the word
    "default", and so a blank model is resolved in one place rather than being
    sent to a vendor as an empty string - which is not a request any of them
    accept, and produces a vendor error the user cannot act on.

    An OpenAI-compatible gateway deliberately has none: it is an arbitrary
    endpoint serving arbitrary model names, so there is nothing to guess and
    guessing would fail on the first call anyway.
    """
    factory = PROVIDERS.get(str(provider or "").strip().lower())
    return getattr(factory, "default_model", "") if factory is not None else ""


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
    if name in KEYED_PROVIDERS:
        # A vendor's key goes to that vendor's own endpoint and nowhere else. A
        # base URL left over from an earlier gateway setting, or written into the
        # host's settings by someone else, used to receive an OpenAI, Anthropic
        # or Gemini key while the interface said it was sent to the vendor. A
        # gateway is configured as `openai_compatible`, which names its URL.
        base_url = ""
    model = str(settings.get("model") or "").strip() or factory.default_model
    if not model:
        # Only reachable for a gateway with no default. Refused here, naming the
        # fix, rather than posting {"model": ""} and surfacing whatever the
        # gateway says about it.
        raise ProviderError("{} needs a model name; there is no default for a custom endpoint".format(name))
    return factory(
        api_key=api_key,
        model=model,
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
