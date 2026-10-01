"""Discover saved database connections by name only.

Nivo can analyse a PostGIS table read-only, but it could never be asked to,
because the companion context reported an empty connection list - so the server
had no idea any database existed and answered "no layer is active" to a question
about connections.

What may leave the machine is deliberately minimal: a stable id and the label
the user themselves gave the connection. Host, port, database name, username,
password, service name, SSL settings and the authcfg id stay in QGIS. That is
not a policy applied later - the projection below simply has no field to carry
them, and a test asserts a realistic settings blob yields nothing but id/name.

The name is a user-authored string, so it is length-bounded and treated as
untrusted display data, never as an instruction or an executable target.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .guard import log_debug

MAX_CONNECTIONS = 25
MAX_NAME = 120

# QGIS stores saved connections under one settings group per provider.
PROVIDER_SETTINGS_GROUPS = {
    "postgres": "PostgreSQL/connections",
    "mssql": "MSSQL/connections",
    "oracle": "Oracle/connections",
    "hana": "HANA/connections",
    "spatialite": "SpatiaLite/connections",
}

# Providers Nivo can actually analyse today. Listing a connection it cannot
# query would be the same "advertise what you cannot do" mistake that made the
# action negotiation lie to the server.
ANALYSABLE_PROVIDERS = ("postgres",)


def connection_reference(provider: str, name: str) -> dict[str, str] | None:
    """Project one saved connection down to what may be sent."""
    provider = str(provider or "").strip().lower()
    label = str(name or "").strip()[:MAX_NAME]
    if not provider or not label:
        return None
    return {"id": "{}:{}".format(provider, label), "name": label, "provider": provider}


def discover_connections(
    names_for: Callable[[str], Iterable[str]],
    providers: Iterable[str] = ANALYSABLE_PROVIDERS,
) -> list[dict[str, str]]:
    """Enumerate saved connections for the analysable providers.

    ``names_for`` returns the saved connection names for one provider; it is
    injected so this is testable without QGIS and so a provider whose lookup
    raises cannot stop the rest from being reported.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for provider in providers:
        try:
            names = list(names_for(provider) or ())
        except Exception as exc:  # noqa: BLE001 - a provider lookup may raise anything
            # A broken provider registry must not cost the user every other
            # connection, and must never surface as a Nivo error. It is logged
            # so "my PostGIS connection is missing" is answerable.
            log_debug("reading saved {} connections".format(provider), exc)
            continue
        for name in names:
            reference = connection_reference(provider, name)
            if reference is None or reference["id"] in seen:
                continue
            seen.add(reference["id"])
            found.append(reference)
            if len(found) >= MAX_CONNECTIONS:
                return found
    return found


def qgis_connection_names(provider: str) -> list[str]:
    """Read saved connection names from the running QGIS install.

    Tries the provider metadata API first (QGIS 3.10+, the supported route),
    falling back to the settings group that older builds and some providers
    still use. Only names are read; no connection is opened and no credential
    is touched.
    """
    try:
        from qgis.core import QgsProviderRegistry  # noqa: PLC0415 - QGIS-only import

        metadata = QgsProviderRegistry.instance().providerMetadata(provider)
        if metadata is not None:
            return [str(name) for name in metadata.connections(False).keys()]
    except Exception as exc:  # noqa: BLE001 - provider metadata may raise anything
        # The supported route is unavailable on this build or this provider;
        # the settings-group fallback below still finds the names.
        log_debug("provider metadata for {} unavailable".format(provider), exc)
    group = PROVIDER_SETTINGS_GROUPS.get(provider)
    if not group:
        return []
    try:
        from qgis.core import QgsSettings  # noqa: PLC0415 - QGIS-only import

        settings = QgsSettings()
        settings.beginGroup(group)
        try:
            return [str(name) for name in settings.childGroups()]
        finally:
            settings.endGroup()
    except Exception:
        return []


def describe_connections(references: list[dict[str, Any]]) -> str:
    """A plain answer to "what connections do I have?" built from real names."""
    if not references:
        return (
            "I can't see any saved database connections in this QGIS profile. "
            "Add one under Browser > PostGIS, then ask me again and I can profile "
            "its tables read-only."
        )
    names = ", ".join(str(item.get("name")) for item in references[:MAX_CONNECTIONS])
    return "Saved connections I can read: {}.".format(names)
