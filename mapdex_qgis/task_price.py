"""What a batch will cost, read from the server and never computed here.

A batch of three hundred sheets is the largest single spend in this product and
it was dispatched with no figure anywhere on screen. The server already
anticipated that: `GET /v1/plans` publishes a `batch_kinds` list carrying the
per-item charge of every batch kind, derived from that kind's own tool chain,
and its own comment gives the reason - each child run reserves separately, so
without an estimate the first N succeed and item 47 stops in the middle of an
archive the customer has already partly paid for.

**Nothing here knows what a sheet costs.** `packages/contracts/pricing.go` is
the single source of the customer-facing price and says so in its first line;
a second copy of $2 and $5 in a desktop plugin is exactly the two-price-lists
problem that file was written to end. This module multiplies a number the
server sent by a count the user chose, and when the server has not told us it
says nothing at all - "we do not know what this costs" and "this costs nothing"
are different answers, and only one of them may be rendered as a price.

No Qt import, so the arithmetic and the wording are testable without a host.
"""

from __future__ import annotations

from typing import Optional, Sequence


def format_cents(cents: int) -> str:
    """Render money the way the server publishes it.

    Whole dollars where the cents are zero, two decimals otherwise, and integer
    arithmetic throughout - the mirror of `contracts.FormatCents`, which is the
    one rule worth restating here because a float that reaches money is a defect
    that survives every test that does not look for it.
    """
    value = int(cents)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value % 100 == 0:
        return "{}${}".format(sign, value // 100)
    return "{}${}.{:02d}".format(sign, value // 100, value % 100)


def charge_for_kind(batch_kinds, kind: str) -> Optional[dict]:
    """The server's entry for one batch kind, or None if it did not send one.

    None is a real answer and is not turned into zero. A kind the server did
    not price is one this build must not put a number against.
    """
    for entry in batch_kinds or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind") or "") == str(kind or ""):
            return entry
    return None


def estimate(batch_kinds, kind: str, sheets: int) -> Optional[dict]:
    """What `sheets` of this kind cost at the published rate.

    Returns None when the price is unknown - no entry, or an entry carrying no
    cents field. `{"cents": 0}` is NOT unknown: validate_deliver bills nothing
    and the server publishes it with a zero charge deliberately, precisely so a
    surface can tell "free" from "unpriced".
    """
    if sheets <= 0:
        return None
    entry = charge_for_kind(batch_kinds, kind)
    if entry is None:
        return None
    per_item = entry.get("cents")
    if not isinstance(per_item, int):
        return None
    charge = entry.get("charge") or {}
    return {
        "sheets": int(sheets),
        "per_item_cents": per_item,
        "total_cents": per_item * int(sheets),
        "placements": int(charge.get("placements") or 0) * int(sheets),
        "traces": int(charge.get("traces") or 0) * int(sheets),
    }


def price_line(batch_kinds, kind: str, sheets: int, trace_beta: bool = False) -> str:
    """The sentence under the Start control, or "" when we cannot price it.

    Three things it deliberately does NOT say. It does not print "$0.00" for an
    unknown price. It does not call itself a bill: a subscriber's included
    allowance is applied later by the meter, so this is the published
    pay-as-you-go rate and is worded as an estimate. And it repeats what the
    charge is conditional on - a sheet is charged only when the work it asked
    for is produced, so an abstention, a review or a failure on our side costs
    nothing - because that is the difference between this number and a quote.
    """
    figures = estimate(batch_kinds, kind, sheets)
    if figures is None:
        return ""
    if figures["total_cents"] == 0:
        return "No charge for this workflow."
    unit = format_cents(figures["per_item_cents"])
    line = "{unit} per sheet.".format(unit=unit)
    if trace_beta and figures["traces"] > 0:
        line += " Tracing is in beta."
    # Why the button's figure is a ceiling rather than a bill. The total is on
    # the button and is deliberately NOT repeated here: one number in two
    # adjacent places reads as two numbers.
    return line + (
        " A sheet is charged only when its result is produced, so a sheet that"
        " fails or abstains costs nothing."
    )


def total_label(batch_kinds, kind: str, sheets: int) -> str:
    """The money for the Start control, or "" when there is none to state.

    Empty for an unpriced workflow AND for a free one: a button reading
    "· $0" invites the reader to wonder what the zero is about, while the line
    beside it says "No charge for this workflow" in words.
    """
    figures = estimate(batch_kinds, kind, sheets)
    if figures is None or figures["total_cents"] == 0:
        return ""
    return format_cents(figures["total_cents"])


# -- What is left to spend -----------------------------------------------
#
# A batch is not charged once. Every sheet becomes its own run and every run
# RESERVES its estimate before it starts, which is the server's own reason for
# publishing a per-item charge at all: without a total checked up front, the
# first N sheets of a three hundred sheet archive succeed and sheet 47 stops
# with INSUFFICIENT_CREDITS, halfway through work the customer has already
# partly paid for and with nothing on screen having warned them.
#
# So the check belongs here, before the upload, over the WHOLE list. The
# balance is read from the server (`GET /v1/credits`) in the same unit the
# price is published in - the reservation holds `estimated_price_cents` against
# `credits_balance`, so both sides of this comparison are cents - and this
# module still computes no price and invents no balance.


def read_balance(credits_payload) -> Optional[dict]:
    """The workspace's spendable balance, or None when we cannot read one.

    None is deliberate and is not zero. A build talking to a server whose
    `/v1/credits` shape it does not recognise knows nothing about the balance,
    and the honest response to that is to say nothing and let the server refuse
    at reservation, where the refusal is authoritative. Guessing "empty" would
    refuse a customer who has the money, which is the worse of the two errors
    by a distance.

    `credits_available` is preferred over `credits_balance` because it is what
    the reservation gate actually enforces: it is the balance minus the holds
    other runs already have out, so a chip reading the raw balance can say $20
    while a run is refused.
    """
    if not isinstance(credits_payload, dict):
        return None
    enforced = credits_payload.get("credits_enforced")
    if not isinstance(enforced, bool):
        return None
    available = credits_payload.get("credits_available")
    if not isinstance(available, int):
        available = credits_payload.get("credits_balance")
    if not isinstance(available, int) or isinstance(available, bool):
        return None
    return {"available_cents": int(available), "enforced": bool(enforced)}


def afford(balance: Optional[dict], total_cents: int) -> dict:
    """Can this workspace pay for this batch?

    Three answers, not two. `known` is False when the balance could not be read
    or the deployment does not enforce credits at all; in both cases nothing on
    screen may claim a shortfall and nothing may be blocked, because we would
    be blocking on a number we do not have.
    """
    total = int(total_cents or 0)
    if balance is None or not balance.get("enforced") or total <= 0:
        return {"known": False, "enough": True, "short_cents": 0, "available_cents": 0}
    available = int(balance.get("available_cents") or 0)
    return {
        "known": True,
        "enough": available >= total,
        "short_cents": max(total - available, 0),
        "available_cents": available,
    }


def balance_refusal(balance: Optional[dict], total_cents: int, sheets: int) -> str:
    """Why this batch cannot start, in the numbers the customer can act on.

    Empty when it can. The three figures are all stated - what is there, what
    this needs, what is missing - because a refusal reading only "not enough
    credit" leaves somebody to work out how much to buy, and the amount is the
    whole decision.

    It says the balance must cover the WHOLE list rather than the first sheet,
    because that is what the server does: each sheet holds its estimate for the
    length of its run, so a list of twelve needs twelve sheets' worth held at
    once even though a sheet that fails or abstains is never charged.
    """
    verdict = afford(balance, total_cents)
    if not verdict["known"] or verdict["enough"]:
        return ""
    return (
        "{available} left, {needed} needed for {count} sheet{plural}. Add {short} "
        "to start. Every sheet is held before it runs, so the whole list has to "
        "be covered."
    ).format(
        available=format_cents(verdict["available_cents"]),
        needed=format_cents(int(total_cents or 0)),
        count=int(sheets or 0),
        plural="" if int(sheets or 0) == 1 else "s",
        short=format_cents(verdict["short_cents"]),
    )


def total_cents(batch_kinds, kind: str, sheets: int) -> int:
    """The figure the balance is checked against, or 0 when there is none."""
    figures = estimate(batch_kinds, kind, sheets)
    return 0 if figures is None else int(figures["total_cents"])


def read_sheet_prices(plans_payload) -> dict:
    """Pull the pricing block out of a `GET /v1/plans` response.

    Tolerant by construction: a payload this build does not recognise yields an
    empty dict, and every caller treats that as "not priced" rather than as
    zero. The plugin ships independently of the server it talks to, so an older
    or newer response must degrade to silence, never to a wrong number.
    """
    if not isinstance(plans_payload, dict):
        return {}
    prices = plans_payload.get("sheet_prices")
    prices = prices if isinstance(prices, dict) else {}
    kinds = plans_payload.get("batch_kinds")
    kinds = kinds if isinstance(kinds, list) else []
    return {
        "batch_kinds": kinds,
        "trace_beta": bool(prices.get("trace_beta")),
    }


def kinds_from(state: Optional[dict]) -> Sequence[dict]:
    if not isinstance(state, dict):
        return []
    kinds = state.get("batch_kinds")
    return kinds if isinstance(kinds, list) else []
