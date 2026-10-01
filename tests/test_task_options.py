"""Batch options: the three fields the server reads and this plugin never sent.

The load-bearing test here is the one that pins each key against the server's
own JSON name. `expand_pdf_pages` sent as `expandPdfPages` is not an error
anywhere - Go's decoder ignores what it does not recognise, the batch is created,
and a 300-page archive silently arrives as one sheet.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.task_options import (  # noqa: E402
    CONCURRENCY_SERVER_DEFAULT,
    MAX_CONCURRENCY,
    TaskOptions,
    applicable,
    concurrency_choices,
    normalise_concurrency,
    summary,
)


def test_the_defaults_are_the_behaviour_this_plugin_already_had():
    # Opening the options panel and closing it must change nothing, and an
    # existing user's next task must behave as their last one did.
    options = TaskOptions()
    assert options.is_default
    assert options.payload() == {}


def test_each_key_matches_the_server_field_name():
    # Pinned against createBatchRequest in
    # apps/api/internal/modules/batches/infrastructure/http_handler.go. A
    # misspelled key is not an error anywhere: the batch is created and the
    # option is silently dropped.
    payload = TaskOptions(expand_pdf_pages=True, skip_completed=True, max_concurrency=4).payload()
    assert payload == {
        "expand_pdf_pages": True,
        "skip_completed": True,
        "max_concurrency": 4,
    }


def test_the_server_default_concurrency_is_omitted_rather_than_sent_as_zero():
    # Sending 0 states "use your default" as though it were a choice, and pins
    # this build against a default the server may move.
    assert "max_concurrency" not in TaskOptions(expand_pdf_pages=True).payload()


def test_concurrency_is_clamped_to_what_the_server_will_honour():
    # A picker showing 12 over a batch running 8 has told the user something
    # untrue.
    assert TaskOptions(max_concurrency=99).max_concurrency == MAX_CONCURRENCY
    assert normalise_concurrency(MAX_CONCURRENCY + 1) == MAX_CONCURRENCY


def test_nonsense_concurrency_falls_back_to_the_server_default():
    for value in (None, "", "many", -4, 0, object()):
        assert normalise_concurrency(value) == CONCURRENCY_SERVER_DEFAULT


def test_the_choices_offer_the_server_default_first_and_stop_at_the_cap():
    choices = concurrency_choices()
    assert choices[0][1] == CONCURRENCY_SERVER_DEFAULT
    assert [value for _label, value in choices[1:]] == list(range(1, MAX_CONCURRENCY + 1))


def test_every_offered_value_survives_normalisation_unchanged():
    # A control may not offer a number the server would silently change.
    for _label, value in concurrency_choices():
        assert normalise_concurrency(value) == value


def test_the_chip_names_what_changed_rather_than_counting_it():
    assert summary(TaskOptions()) == "Default"
    assert summary(None) == "Default"
    assert summary(TaskOptions(expand_pdf_pages=True)) == "Every PDF page"
    assert summary(TaskOptions(skip_completed=True, max_concurrency=2)) == "Skip completed · 2 at a time"


def test_options_compare_by_value():
    assert TaskOptions(skip_completed=True) == TaskOptions(skip_completed=True)
    assert TaskOptions(skip_completed=True) != TaskOptions()


# ---------------------------------------------------------------------------
# An option is offered when it can act
#
# Reported from a real session: the list held no PDF, the chip read "Every PDF
# page", and the checkbox above it was ticked. Nothing was broken - the server
# would simply have ignored the field - which is exactly why it survived: a
# promise about work that cannot happen looks identical to one that will.


def test_the_page_option_is_dropped_when_no_pdf_is_in_the_list():
    asked = TaskOptions(expand_pdf_pages=True, skip_completed=True)
    applied = applicable(asked, has_pdf=False)
    assert applied.expand_pdf_pages is False
    # ...and only that one. The other decisions are about the batch, not about
    # what kind of file is in it.
    assert applied.skip_completed is True


def test_the_page_option_survives_when_a_pdf_is_in_the_list():
    asked = TaskOptions(expand_pdf_pages=True)
    assert applicable(asked, has_pdf=True) == asked


def test_what_is_dropped_never_reaches_the_wire():
    payload = applicable(TaskOptions(expand_pdf_pages=True), has_pdf=False).payload()
    assert "expand_pdf_pages" not in payload


def test_what_is_dropped_never_reaches_the_chip():
    # The panel and the payload read the same value, once. Two readings of one
    # setting is how a screen comes to disagree with what it sends.
    assert summary(applicable(TaskOptions(expand_pdf_pages=True), has_pdf=False)) == "Default"
    assert "Every PDF page" in summary(applicable(TaskOptions(expand_pdf_pages=True), has_pdf=True))


def test_nothing_is_invented_for_an_absent_options_value():
    assert applicable(None, has_pdf=True) == TaskOptions()
