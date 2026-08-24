"""The batch item list: ordering, wording, and the one action per row."""

from mapdex_qgis.job_items import (
    item_action,
    item_status,
    item_title,
    items_summary,
    order_items,
)


def _item(index, state, **extra):
    row = {"index": index, "state": state, "file_id": "file_%d" % index}
    row.update(extra)
    return row


def test_attention_comes_first():
    # docs/UX.md 10.1: failed, then needs_review, then active, then done. A list
    # in submission order buries the twenty sheets that need a person among the
    # two hundred and eighty that do not.
    items = [
        _item(0, "succeeded"),
        _item(1, "pending"),
        _item(2, "failed"),
        _item(3, "needs_review"),
        _item(4, "running"),
    ]
    assert [row["state"] for row in order_items(items)] == [
        "failed",
        "needs_review",
        "running",
        "pending",
        "succeeded",
    ]


def test_order_is_stable_within_a_group():
    # A reviewer working down the failures expects sheet 4 before sheet 9, and a
    # list that reshuffles on every poll is one nobody can keep their place in.
    items = [_item(9, "failed"), _item(4, "failed"), _item(6, "failed")]
    assert [row["index"] for row in order_items(items)] == [4, 6, 9]


def test_an_unknown_state_sorts_last_and_is_shown_as_itself():
    items = [_item(0, "teleported"), _item(1, "failed")]
    ordered = order_items(items)
    assert ordered[0]["state"] == "failed"
    # Mapping it onto a known label would report something this build cannot
    # know. It is shown, not guessed at.
    assert item_status(ordered[1])["label"] == "Teleported"


def test_the_title_is_the_name_the_person_chose():
    names = {"file_3": "/home/somebody/CA_Cannell Peak_100592.tiff"}
    assert item_title(_item(3, "failed"), names) == "CA_Cannell Peak_100592.tiff"


def test_a_paged_item_says_which_page():
    names = {"file_1": "atlas.pdf"}
    assert item_title(_item(1, "pending", page=7), names) == "atlas.pdf · page 7"


def test_page_zero_means_unpaged_and_is_not_shown_as_page_one():
    names = {"file_1": "sheet.tif"}
    assert item_title(_item(1, "pending", page=0), names) == "sheet.tif"


def test_an_unnamed_item_is_numbered_from_one():
    # Zero-based indexes are the wire's business. A person counts from one.
    assert item_title(_item(0, "pending"), {}) == "Item 1"


def test_a_failure_carries_its_own_reason():
    row = _item(2, "failed", error={"code": "GEOREFERENCE_REQUIRED", "message": "No placement."})
    status = item_status(row)
    assert status["label"] == "Failed"
    assert status["detail"] == "No placement."


def test_state_is_never_carried_by_colour_alone():
    # DESIGN.md 4.2. Every state has a mark and a word, so nothing depends on
    # a theme rendering a colour the way this build expected.
    for state in ("failed", "needs_review", "running", "pending", "succeeded", "cancelled"):
        status = item_status(_item(0, state))
        assert status["mark"], state
        assert status["label"], state


def test_one_action_per_row_and_it_matches_the_state():
    assert item_action(_item(0, "failed"))["key"] == "open"
    assert item_action(_item(0, "needs_review"))["key"] == "review"
    assert item_action(_item(0, "succeeded"))["key"] == "import"
    # Nothing to offer while it is still running: a button that cannot do
    # anything yet is one a person clicks and then distrusts.
    assert item_action(_item(0, "running"))["key"] == ""
    assert item_action(_item(0, "pending"))["key"] == ""


def test_the_summary_counts_the_list_it_sits_above():
    items = [
        _item(0, "failed"),
        _item(1, "needs_review"),
        _item(2, "succeeded"),
        _item(3, "succeeded"),
    ]
    summary = items_summary(items)
    assert "4 items" in summary
    assert "1 failed" in summary
    assert "1 needs review" in summary
    assert "2 done" in summary


def test_an_empty_batch_says_nothing_rather_than_zero():
    assert items_summary([]) == ""
