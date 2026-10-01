"""Which Mapdex this build talks to.

`scripts/package.py --channel production` rewrites CHANNEL when it builds a
release package, so a published plugin points at the hosted service and does
not offer endpoint fields at all. Development builds keep them: pointing QGIS
at a local stack is how the plugin gets tested.

Support can still unlock the fields on a released build by setting the QGIS
setting `mapdex/allow_custom_endpoint` to true; that is a deliberate,
non-discoverable action for a self-hosted customer, not a UI affordance.

What the unlock never does is point a released build at THIS computer. A
self-hosted customer's server has an address; a loopback address is a
developer's local stack. A released build that honoured one sent a user's
requests to 127.0.0.1 because a development session had left the unlock and a
local address behind in their QGIS profile, and nothing on screen said so.
"""
from __future__ import annotations

from urllib.parse import urlsplit

CHANNEL = "development"

PRODUCTION_API = "https://api.mapdex.ai"
PRODUCTION_WEB = "https://app.mapdex.ai"
DEVELOPMENT_API = "http://127.0.0.1:8080"
DEVELOPMENT_WEB = "http://127.0.0.1:3000"

ALLOW_CUSTOM_ENDPOINT_SETTING = "mapdex/allow_custom_endpoint"

# This computer. Owned here, rather than in api_client where it started, because
# this module imports nothing and api_client already imports it: the build
# profile and the transport check have to agree on what "local" means, and a
# second copy of the set is how they would come to disagree.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

_TRUE = {"1", "true", "yes", "on"}


def is_production() -> bool:
    return CHANNEL.strip().lower() == "production"


def is_loopback(url) -> bool:
    """Does this address name the computer QGIS is running on?"""
    return (urlsplit(str(url or "").strip()).hostname or "").lower() in LOOPBACK_HOSTS


def endpoint_choices():
    """The addresses the Settings fields offer, as (api_choices, web_choices).

    A released build offers only the hosted service. It used to list the local
    development stack FIRST in both fields, so the one install that shows these
    fields - a support-unlocked self-hosted customer - was invited to pick an
    address that cannot be theirs.
    """
    if is_production():
        return (PRODUCTION_API,), (PRODUCTION_WEB,)
    return (DEVELOPMENT_API, PRODUCTION_API), (DEVELOPMENT_WEB, PRODUCTION_WEB)


def endpoint_refusal(url) -> str:
    """Why this build will not use `url`, or "" when it may."""
    if is_production() and is_loopback(url):
        return (
            "This is a released build of Mapdex for QGIS, so it connects to Mapdex, "
            "not to a server on this computer. Enter your Mapdex server's address, "
            "or use a development build to work against a local stack."
        )
    return ""


def endpoints_unlocked(setting_value=None) -> bool:
    """May this build choose its own API/web address?"""
    if not is_production():
        return True
    return str(setting_value or "").strip().lower() in _TRUE


def resolve_endpoints(stored_api: str, stored_web: str, unlocked: bool):
    """Return the (api, web, drop_token) to start with.

    A released build ignores whatever endpoint an earlier development session
    stored. The token that came with that endpoint is dropped as well: a
    session belongs to the deployment that issued it.
    """
    if unlocked:
        if is_production():
            # A deliberately unlocked production build remains hosted by
            # default; support opted into editing, not localhost. That sentence
            # was the intent here and the code honoured a stored loopback
            # address anyway, so a development session's leftovers sent a
            # released build to 127.0.0.1. A loopback value is now treated as
            # absent, and the session it came with is dropped: it was issued
            # by that local stack.
            api = "" if is_loopback(stored_api) else stored_api
            web = "" if is_loopback(stored_web) else stored_web
            dropped = bool(stored_api) and not api
            return (api or PRODUCTION_API, web or PRODUCTION_WEB, dropped)
        # A source/default package is a development build. It should work with
        # the local stack without making the tester replace two hosted URLs on
        # every clean QGIS profile.
        return (stored_api or DEVELOPMENT_API, stored_web or DEVELOPMENT_WEB, False)
    stale = bool(stored_api) and stored_api.strip().rstrip("/") != PRODUCTION_API
    return (PRODUCTION_API, PRODUCTION_WEB, stale)
