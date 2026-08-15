"""Saved-connection discovery: enough to be useful, nothing that is a secret."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.connections import (  # noqa: E402
    MAX_CONNECTIONS,
    connection_reference,
    describe_connections,
    discover_connections,
)
from mapdex_qgis.nivo import companion_context  # noqa: E402

# A realistic saved-connection blob. None of these values may ever be sent.
# Deliberately chosen so no secret is a substring of the display names used
# below - otherwise the assertions pass or fail on coincidence, not on leakage.
SECRETS = {
    "host": "db.internal.example",
    "port": "54329",
    "database": "gis_prod_db",
    "username": "gis_admin",
    "password": "hunter2",
    "authcfg": "abc123",
    "service": "svc_prod",
    "sslmode": "require",
}


def test_only_an_id_and_a_name_leave_the_machine():
    reference = connection_reference("postgres", "Production cadastre")
    assert set(reference) == {"id", "name", "provider"}
    assert reference["name"] == "Production cadastre"
    assert reference["id"] == "postgres:Production cadastre"


def test_no_credential_field_can_be_carried_by_the_projection():
    reference = connection_reference("postgres", "Production cadastre")
    serialized = repr(reference)
    for secret in SECRETS.values():
        assert secret not in serialized


def test_discovery_lists_saved_connections_by_name():
    found = discover_connections(lambda provider: ["Cadastre", "Utilities"])
    assert [item["name"] for item in found] == ["Cadastre", "Utilities"]


def test_a_broken_provider_lookup_does_not_lose_the_others():
    def names_for(provider):
        if provider == "postgres":
            raise RuntimeError("registry unavailable")
        return ["Other"]

    # postgres fails; the call must still return rather than raising into the UI.
    assert discover_connections(names_for) == []
    assert discover_connections(names_for, providers=("postgres", "mssql")) == [
        {"id": "mssql:Other", "name": "Other", "provider": "mssql"}
    ]


def test_blank_and_duplicate_names_are_dropped():
    found = discover_connections(lambda provider: ["", "  ", "A", "A"])
    assert [item["name"] for item in found] == ["A"]


def test_the_list_is_bounded():
    found = discover_connections(lambda provider: ["c{}".format(index) for index in range(200)])
    assert len(found) == MAX_CONNECTIONS


def test_a_long_user_authored_name_is_truncated():
    reference = connection_reference("postgres", "x" * 500)
    assert len(reference["name"]) == 120


def test_companion_context_carries_only_id_and_name():
    payload = companion_context({
        "crs": "EPSG:3857",
        "connections": [dict(SECRETS, id="postgres:Cadastre", name="Cadastre")],
    })
    connection = payload["connections"][0]
    assert set(connection) == {"id", "name"}
    for secret in SECRETS.values():
        assert secret not in repr(payload)


def test_the_answer_names_real_connections_and_is_honest_when_there_are_none():
    assert "Cadastre" in describe_connections([{"id": "postgres:Cadastre", "name": "Cadastre"}])
    empty = describe_connections([])
    assert "can't see any saved database connections" in empty
    assert "Browser" in empty
