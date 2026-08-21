"""This build says it can start a plan, and the claim is read from the code.

The probe is defensive by design, so a mistake makes it report False rather than
raise - which is what happened while writing it: the import named a class that
does not exist and the advertisement quietly went to False. A silent
under-claim costs a real capability, so it needs a test that fails loudly.
"""

from mapdex_qgis import nivo


def test_this_build_advertises_that_it_can_start_a_plan():
    assert nivo.CAN_RUN_PLANS is True, (
        "the plan control exists in this build and the probe reports otherwise; "
        "check the lazy import in nivo._can_run_plans")


def test_the_advertisement_reaches_the_companion_context():
    context = nivo.companion_context({"crs": "EPSG:4326"})
    assert context["can_run_plans"] is True


def test_the_probe_reads_both_halves_of_the_round_trip():
    # Stating the cost and asking is one half; submitting the plan unchanged
    # with its hash is the other. A build with only one of them cannot start a
    # plan, so the probe must not be satisfied by either alone.
    from mapdex_qgis import plan_offer
    from mapdex_qgis.api_client import MapdexAPI

    assert callable(plan_offer.plan_offer)
    assert callable(MapdexAPI.create_run)


def test_running_a_plan_is_not_advertised_as_a_supported_action_kind():
    # supported_action_kinds is what the SERVER asks the desktop to perform.
    # Running a plan is the desktop acting on a proposal, the other direction,
    # and putting it in that list would corrupt the registry intersection the
    # server does on arrival.
    context = nivo.companion_context({"crs": "EPSG:4326"})
    for kind in context.get("supported_action_kinds", []):
        assert "plan" not in kind.split(":")[-1].split("@")[0].split("_"), (
            f"{kind} looks like a plan-running capability in the action list")
