"""What getting a result into QGIS costs, said before it is spent.

A placed sheet costs one placement the first time it leaves Mapdex, and
importing its georeferenced GeoTIFF into QGIS is it leaving
(packages/contracts/placement_leave.go). The server prices that, through
`GET /v1/credits/placement-quote`, and this module only words what it answered:
the panel states the price before the import, asks before an import that will
charge, and imports on its own only when the answer is known to be free.

Nothing here knows what a placement costs. A quote that could not be read is
None, and None is never rendered as free: "we do not know" and "this costs
nothing" are different answers, and only the second may start an import that
nobody pressed.

No Qt import, so the wording and the decisions are testable without a host.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional, Sequence

from .task_price import format_cents


def sum_quotes(quotes: Sequence[Optional[dict]]) -> Optional[dict]:
    """Several quotes as one, or None when any of them is unknown.

    The sheets and charges add; the balance is the one balance they would all
    be taken from. A partial sum understates the price, so an unknown quote
    makes the whole answer unknown rather than smaller.
    """
    if any(not isinstance(quote, dict) for quote in quotes):
        return None
    sheets = unpaid = cents = 0
    available = None
    for quote in quotes:
        sheets += _int(quote.get("sheets"))
        unpaid += _int(quote.get("unpaid_sheets"))
        cents += _int(quote.get("charge_cents"))
        if isinstance(quote.get("available_cents"), int):
            available = quote["available_cents"]
    return {
        "sheets": sheets,
        "unpaid_sheets": unpaid,
        "charge_cents": cents,
        "available_cents": available,
    }


def quote_files(file_ids: Iterable[str], quote: Callable[[str], Optional[dict]]) -> Optional[dict]:
    """Ask the server what letting each file out costs, summed.

    No file means nothing leaves that can be charged: a vector result, a draft,
    an artifact - a zero quote, which is a known answer, not an unknown one.
    """
    ids = [file_id for file_id in dict.fromkeys(file_ids) if file_id]
    if not ids:
        return {"sheets": 0, "unpaid_sheets": 0, "charge_cents": 0, "available_cents": None}
    answers = []
    for file_id in ids:
        try:
            answers.append(quote(file_id))
        except Exception:  # noqa: BLE001 - an unreadable quote is unknown, never free
            answers.append(None)
    return sum_quotes(answers)


def is_free(quote: Optional[dict]) -> bool:
    """True only when the server said this import charges nothing."""
    return isinstance(quote, dict) and _int(quote.get("charge_cents")) <= 0


def price_line(quote: Optional[dict]) -> str:
    """The sentence the panel shows before the import."""
    if quote is None:
        return (
            "The price could not be read just now. A placed sheet is charged once, "
            "the first time it is imported or downloaded."
        )
    cents = _int(quote.get("charge_cents"))
    if cents <= 0:
        if _int(quote.get("sheets")) > 0:
            return "Free: these placed sheets have already been paid for."
        return "Free."
    sheets = max(_int(quote.get("unpaid_sheets")), 1)
    if sheets == 1:
        line = "{price}, charged once: the first time this placed sheet leaves Mapdex.".format(
            price=format_cents(cents)
        )
    else:
        line = "{price} for {n} placed sheets, charged once each, the first time they leave Mapdex.".format(
            price=format_cents(cents), n=sheets
        )
    available = quote.get("available_cents")
    if isinstance(available, int) and available < cents:
        line += " Your balance is {balance}.".format(balance=format_cents(available))
    return line


def import_label(quote: Optional[dict]) -> str:
    """The label of a control that imports a priced result: the price is IN it.

    Pressing it is the consent, so it may not leave the price to a line the
    person may not have read.
    """
    if quote is None:
        return "Import into QGIS (price not checked)"
    cents = _int(quote.get("charge_cents"))
    if cents <= 0:
        return "Import into QGIS (free)"
    return "Import into QGIS ({price})".format(price=format_cents(cents))


def ready_message(quote: Optional[dict], plan_run: bool) -> str:
    """What the panel says when a finished result was NOT imported on its own.

    A batch has the Get result button. A plan run has no batch and so no such
    button: telling its owner to press it pointed at a control that was not
    on screen, and the result could never be imported. A plan run's result is
    offered by a control on its own turn instead, and the text says so.
    """
    line = price_line(quote)
    if plan_run:
        return (
            "The result is ready in Mapdex. Importing it into QGIS: {line} "
            "Use the Import into QGIS button below to bring it in."
        ).format(line=line)
    return (
        "Results are ready in Mapdex. Importing them into QGIS: {line} "
        "Press Get result from Mapdex to import."
    ).format(line=line)


def consent_text(quote: Optional[dict]) -> str:
    """The question asked before an import that will, or may, charge."""
    return "Getting this result into QGIS:\n\n{line}\n\nImport it now?".format(line=price_line(quote))


def is_leave_refusal(exception) -> bool:
    """A placed sheet's first download refused because the balance is short."""
    return str(getattr(exception, "code", "") or "") == "INSUFFICIENT_CREDITS"


def refusal_numbers(details: Optional[dict]) -> str:
    """The two numbers a short balance is refused with, as a sentence.

    The server sends `required` and `available` in cents on the 402. A refusal
    that drops them asks the person to pay without saying how much, and how
    much they have. Empty when the server did not send both.
    """
    details = details if isinstance(details, dict) else {}
    required, available = details.get("required"), details.get("available")
    if not isinstance(required, int) or isinstance(required, bool):
        return ""
    if not isinstance(available, int) or isinstance(available, bool):
        return ""
    return "It costs {required} and the workspace has {available}.".format(
        required=format_cents(required), available=format_cents(available)
    )


def refusal_text(exception) -> str:
    """The sentence for a placed sheet's first import refused for a short balance."""
    numbers = refusal_numbers(getattr(exception, "details", None))
    if not numbers:
        return str(exception)
    return (
        "A placed sheet is charged once, the first time it leaves Mapdex, and the "
        "balance does not cover it. {numbers}"
    ).format(numbers=numbers)


# What the balance dialog says above and below the refusal when an IMPORT was
# refused. The batch wording ("did not start", "nothing was uploaded") is about
# a batch and is false here.
IMPORT_REFUSED_HEADLINE = "This placed sheet was not imported."
IMPORT_REFUSED_REASSURANCE = (
    "Nothing was charged for it. Results already imported stay in the project."
)


def partial_import_message(added: int, refused: Sequence[str], details: Optional[dict] = None) -> str:
    """What happened when some results came in and some were refused.

    The layers that arrived are named as arrived, and the ones the balance did
    not cover are named, with what they cost and what the balance holds, so
    nobody has to work out which sheet is missing or how much it needs.
    """
    if not refused:
        return ""
    names = ", ".join(refused)
    head = (
        "Added {n} result layer(s). ".format(n=added) if added else ""
    )
    numbers = refusal_numbers(details)
    return (
        "{head}Not imported, because the balance does not cover the first download of "
        "these placed sheets: {names}.{numbers}"
    ).format(head=head, names=names, numbers=(" " + numbers) if numbers else "")


def _int(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0
