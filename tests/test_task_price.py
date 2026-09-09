"""The batch estimate: multiplied from the server's figure, never computed here.

The load-bearing tests are the ones about NOT knowing. A plugin ships
independently of the server it talks to, so an older or newer `/v1/plans` must
degrade to silence; rendering an unknown price as $0.00 is the one failure mode
that costs a customer money without anything looking broken.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.task_price import (  # noqa: E402
    afford,
    balance_refusal,
    charge_for_kind,
    estimate,
    format_cents,
    kinds_from,
    price_line,
    total_cents,
    total_label,
    read_balance,
    read_sheet_prices,
)

# Shaped exactly like the server's `batch_kinds`, from
# apps/api/internal/modules/billing/infrastructure/http_handler.go.
KINDS = [
    {"kind": "georeference", "charge": {"placements": 1, "traces": 0}, "cents": 200},
    {"kind": "digitize_parcels", "charge": {"placements": 0, "traces": 1}, "cents": 500},
    {"kind": "validate_deliver", "charge": {"placements": 0, "traces": 0}, "cents": 0},
]


# -- money ------------------------------------------------------------------

def test_money_is_rendered_the_way_the_server_publishes_it():
    assert format_cents(200) == "$2"
    assert format_cents(0) == "$0"
    assert format_cents(1650) == "$16.50"
    assert format_cents(5) == "$0.05"


def test_no_float_ever_touches_the_total():
    # 3 sheets at $0.05 is $0.15, and 0.05*3 in binary floating point is not.
    figures = estimate([{"kind": "k", "charge": {}, "cents": 5}], "k", 3)
    assert figures["total_cents"] == 15
    assert format_cents(figures["total_cents"]) == "$0.15"


# -- multiplying the server's own figure ------------------------------------

def test_the_estimate_multiplies_the_published_per_item_charge():
    figures = estimate(KINDS, "georeference", 8)
    assert figures == {
        "sheets": 8,
        "per_item_cents": 200,
        "total_cents": 1600,
        "placements": 8,
        "traces": 0,
    }


def test_the_line_states_the_rate_and_the_button_states_the_total():
    # Two numbers, two places, each answering a different question: the line
    # says what a sheet costs, the button says what pressing it will spend.
    # The total is deliberately NOT repeated in the line - one figure printed
    # twice a centimetre apart reads as two figures.
    line = price_line(KINDS, "georeference", 8)
    assert "$2 per sheet" in line
    assert "$16" not in line
    assert total_label(KINDS, "georeference", 8) == "$16"


def test_the_rate_does_not_change_with_the_count():
    assert price_line(KINDS, "georeference", 1) == price_line(KINDS, "georeference", 40)


def test_the_line_states_what_the_charge_is_conditional_on():
    # The difference between an estimate and a quote: an abstention, a review or
    # a failure on our side costs nothing.
    assert "only when its result is produced" in price_line(KINDS, "georeference", 3)


def test_tracing_says_it_is_in_beta_when_the_server_says_so():
    assert "beta" in price_line(KINDS, "digitize_parcels", 2, trace_beta=True)
    # ...and never on a workflow that traces nothing.
    assert "beta" not in price_line(KINDS, "georeference", 2, trace_beta=True)
    # ...nor when the server has not said the discount is on.
    assert "beta" not in price_line(KINDS, "digitize_parcels", 2, trace_beta=False)


# -- free is not the same as unpriced ---------------------------------------

def test_a_workflow_that_bills_nothing_says_so():
    # The server publishes validate_deliver with a zero charge deliberately,
    # so a surface can tell "free" from "we do not know".
    assert price_line(KINDS, "validate_deliver", 40) == "No charge for this workflow."
    assert estimate(KINDS, "validate_deliver", 40)["total_cents"] == 0


def test_an_unpriced_kind_produces_no_line_at_all():
    assert estimate(KINDS, "some_future_kind", 4) is None
    assert price_line(KINDS, "some_future_kind", 4) == ""


def test_an_entry_with_no_cents_field_is_unknown_not_zero():
    kinds = [{"kind": "georeference", "charge": {"placements": 1}}]
    assert estimate(kinds, "georeference", 3) is None
    assert price_line(kinds, "georeference", 3) == ""


def test_a_non_integer_cents_field_is_refused():
    # A float here would be a price computed somewhere it should not have been.
    kinds = [{"kind": "georeference", "charge": {}, "cents": 2.0}]
    assert estimate(kinds, "georeference", 3) is None


def test_no_sheets_means_no_estimate():
    assert estimate(KINDS, "georeference", 0) is None
    assert price_line(KINDS, "georeference", 0) == ""


# -- degrading against a server this build does not recognise ----------------

def test_a_payload_this_build_cannot_read_yields_silence_not_zero():
    for payload in (None, {}, [], "plans", {"sheet_prices": "no"}, {"batch_kinds": {}}):
        state = read_sheet_prices(payload)
        assert kinds_from(state) == []
        assert price_line(kinds_from(state), "georeference", 8) == ""


def test_the_pricing_block_is_read_out_of_a_real_plans_response():
    state = read_sheet_prices({
        "plans": [],
        "sheet_prices": {"placement_cents": 200, "trace_cents": 500, "trace_beta": True},
        "batch_kinds": KINDS,
    })
    assert state["trace_beta"] is True
    assert kinds_from(state) == KINDS
    assert "$2 per sheet" in price_line(kinds_from(state), "georeference", 8)
    assert total_label(kinds_from(state), "georeference", 8) == "$16"


def test_a_malformed_entry_does_not_break_the_lookup():
    kinds = ["not a dict", None, {"kind": "georeference", "charge": {}, "cents": 200}]
    assert charge_for_kind(kinds, "georeference")["cents"] == 200


# ---------------------------------------------------------------------------
# What is left to spend
#
# Every sheet reserves its estimate as its own run starts, so a list the
# balance cannot cover does not fail cleanly: it runs until it stops, halfway
# through an archive the customer has already partly paid for. These tests are
# about the two ways that check can be wrong, and only one of them is survivable.


CREDITS = {"credits_balance": 2000, "credits_reserved": 400,
           "credits_available": 1600, "credits_enforced": True}


def test_the_balance_read_is_what_the_reservation_gate_enforces():
    # `credits_available` is the balance minus the holds other runs already
    # have out, so a panel reading the raw balance can say $20 while the run
    # is refused. The available figure is the one to compare against.
    assert read_balance(CREDITS)["available_cents"] == 1600


def test_the_raw_balance_stands_in_when_available_is_absent():
    older = {"credits_balance": 900, "credits_enforced": True}
    assert read_balance(older)["available_cents"] == 900


def test_a_response_this_build_cannot_read_is_unknown_not_empty():
    # The direction matters and is the whole point. Guessing "empty" refuses a
    # customer who has the money; saying nothing leaves the server's own
    # refusal as the authority, which is where it belongs.
    for payload in (None, {}, "no", {"credits_available": 500},
                    {"credits_enforced": True}, {"credits_available": "500",
                                                 "credits_enforced": True}):
        assert read_balance(payload) is None


def test_a_true_is_not_a_balance():
    # bool is an int in Python, and a payload carrying `true` where a number
    # belongs would otherwise read as one cent.
    assert read_balance({"credits_available": True, "credits_enforced": True}) is None


def test_an_unknown_balance_blocks_nothing():
    verdict = afford(None, 1600)
    assert verdict["known"] is False and verdict["enough"] is True


def test_a_deployment_that_does_not_enforce_credits_blocks_nothing():
    free = {"available_cents": 0, "enforced": False}
    assert afford(free, 1600)["enough"] is True
    assert balance_refusal(free, 1600, 8) == ""


def test_a_balance_that_covers_the_whole_list_passes():
    assert afford(read_balance(CREDITS), 1600)["enough"] is True
    assert balance_refusal(read_balance(CREDITS), 1600, 8) == ""


def test_a_balance_one_cent_short_refuses():
    # The boundary is the whole list, not the first sheet: every sheet holds
    # its estimate at once.
    verdict = afford(read_balance(CREDITS), 1601)
    assert verdict["enough"] is False and verdict["short_cents"] == 1


def test_the_refusal_states_what_is_there_what_is_needed_and_what_is_missing():
    notice = balance_refusal(read_balance(CREDITS), 4000, 20)
    assert "$16" in notice      # left
    assert "$40" in notice      # needed
    assert "$24" in notice      # short
    assert "20 sheets" in notice
    # And why the whole list has to be covered rather than the next sheet.
    assert "before it runs" in notice


def test_one_sheet_is_singular_in_the_refusal():
    assert "1 sheet." in balance_refusal(read_balance(CREDITS), 4000, 1)


def test_an_unpriced_workflow_is_never_refused_for_money():
    # Nothing to compare against, so nothing to refuse. The alternative is
    # blocking a batch whose cost we could not read, which is the same defect
    # as printing $0 for an unknown price.
    assert total_cents(KINDS, "some_future_kind", 8) == 0
    assert balance_refusal(read_balance(CREDITS), 0, 8) == ""


def test_a_free_workflow_is_never_refused_for_money():
    assert total_cents(KINDS, "validate_deliver", 300) == 0
    assert afford(read_balance({"credits_available": 0, "credits_enforced": True}),
                  total_cents(KINDS, "validate_deliver", 300))["enough"] is True
