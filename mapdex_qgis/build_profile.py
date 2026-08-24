"""Which Mapdex this build talks to.

`scripts/package.py --channel production` rewrites CHANNEL when it builds a
release package, so a published plugin points at the hosted service and does
not offer endpoint fields at all. Development builds keep them: pointing QGIS
at a local stack is how the plugin gets tested.

Support can still unlock the fields on a released build by setting the QGIS
setting `mapdex/allow_custom_endpoint` to true; that is a deliberate,
non-discoverable action for a self-hosted customer, not a UI affordance.
"""
from __future__ import annotations

CHANNEL = "development"

PRODUCTION_API = "https://api.mapdex.ai"
PRODUCTION_WEB = "https://app.mapdex.ai"
DEVELOPMENT_API = "http://127.0.0.1:8080"
DEVELOPMENT_WEB = "http://127.0.0.1:3000"

ALLOW_CUSTOM_ENDPOINT_SETTING = "mapdex/allow_custom_endpoint"

_TRUE = {"1", "true", "yes", "on"}


def is_production() -> bool:
    return CHANNEL.strip().lower() == "production"


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
        # A source/default package is a development build. It should work with
        # the local stack without making the tester replace two hosted URLs on
        # every clean QGIS profile. A deliberately unlocked production build
        # remains hosted by default; support opted into editing, not localhost.
        default_api = PRODUCTION_API if is_production() else DEVELOPMENT_API
        default_web = PRODUCTION_WEB if is_production() else DEVELOPMENT_WEB
        return (stored_api or default_api, stored_web or default_web, False)
    stale = bool(stored_api) and stored_api.strip().rstrip("/") != PRODUCTION_API
    return (PRODUCTION_API, PRODUCTION_WEB, stale)
