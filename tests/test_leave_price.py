"""Getting a result into QGIS: priced before it is spent.

A placed sheet costs one placement the first time it leaves Mapdex, and the
QGIS import of its GeoTIFF is it leaving. The load-bearing tests are about NOT
knowing: an unreadable quote must never read as free, because "free" is the
one answer that lets an import start without anybody pressing anything.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.leave_price import (  # noqa: E402
    consent_text,
    is_free,
    is_leave_refusal,
    partial_import_message,
    price_line,
    quote_files,
    sum_quotes,
)


def quote(charge, unpaid=1, sheets=1, available=1100):
    return {"sheets": sheets, "unpaid_sheets": unpaid, "charge_cents": charge, "available_cents": available}


def test_an_unknown_quote_is_never_free():
    assert not is_free(None)
    assert sum_quotes([quote(0), None]) is None
    line = price_line(None)
    assert "could not be read" in line
    assert "free" not in line.lower().split(".")[0]


def test_a_result_with_no_placed_raster_is_known_free_without_asking():
    asked = []
    answer = quote_files([], lambda file_id: asked.append(file_id) or quote(200))
    assert is_free(answer)
    assert asked == []


def test_each_raster_is_quoted_once_and_summed():
    asked = []

    def server(file_id):
        asked.append(file_id)
        return quote(200)

    answer = quote_files(["file_a", "file_b", "file_a", ""], server)
    assert asked == ["file_a", "file_b"]
    assert answer["charge_cents"] == 400 and answer["unpaid_sheets"] == 2
    assert not is_free(answer)
    assert "$4" in price_line(answer) and "2 placed sheets" in price_line(answer)


def test_a_quote_that_raises_is_unknown_not_free():
    def server(file_id):
        raise RuntimeError("down")

    assert quote_files(["file_a"], server) is None


def test_the_price_line_says_free_when_it_is_and_the_balance_when_it_is_short():
    assert price_line(quote(0, unpaid=0)) == "Free: these placed sheets have already been paid for."
    assert price_line(quote(0, unpaid=0, sheets=0)) == "Free."
    one = price_line(quote(200, available=1100))
    assert one.startswith("$2") and "first time" in one and "balance" not in one
    short = price_line(quote(200, available=100))
    assert "Your balance is $1." in short
    assert "$2" in consent_text(quote(200)) and consent_text(quote(200)).endswith("Import it now?")


class Refused(Exception):
    code = "INSUFFICIENT_CREDITS"


class Other(Exception):
    code = "NOT_FOUND"


def test_only_a_short_balance_is_a_leave_refusal():
    assert is_leave_refusal(Refused())
    assert not is_leave_refusal(Other())
    assert not is_leave_refusal(RuntimeError("x"))


def test_a_partial_import_names_what_arrived_and_what_did_not():
    assert partial_import_message(2, []) == ""
    message = partial_import_message(2, ["Sheet B", "Sheet C"])
    assert message.startswith("Added 2 result layer(s).")
    assert "Sheet B, Sheet C" in message
    assert partial_import_message(0, ["Sheet B"]).startswith("Not imported")
