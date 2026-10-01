"""The three decisions a batch carries, and the sentence that summarises them.

`POST /v1/batches` has accepted `expand_pdf_pages`, `skip_completed` and
`max_concurrency` for as long as batches have existed, and this plugin has never
sent any of them. So a QGIS user submitting a 300-page PDF archive got one sheet
per FILE with no way to say otherwise, and re-running an archive after a partial
failure re-ran the sheets that had already succeeded and paid for them again.

The options row exists because these are real. An Options control that opens a
panel with nothing in it is the "entry that opens nothing" docs/UX.md refuses;
what makes this one honest is that every field below is a field the server reads.

Defaults are the CURRENT behaviour in all three cases, so opening this panel and
closing it changes nothing, and an existing user's next task behaves as their
last one did.

No Qt import: the summary wording and the payload shape are decided where the
headless suite can reach them.
"""

from __future__ import annotations

# The server's own bounds (`batches/domain/batch.go`): 0 means "server decides",
# which it resolves to 3, and anything above 8 is clamped to 8. Restated here so
# the picker cannot offer a number the server will silently change - a control
# that shows 12 over a batch running 8 has told the user something untrue.
SERVER_DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 8
CONCURRENCY_SERVER_DEFAULT = 0


class TaskOptions:
    """What to do with the sheets, beyond which workflow runs over them."""

    __slots__ = ("expand_pdf_pages", "skip_completed", "max_concurrency")

    def __init__(self, expand_pdf_pages=False, skip_completed=False,
                 max_concurrency=CONCURRENCY_SERVER_DEFAULT):
        self.expand_pdf_pages = bool(expand_pdf_pages)
        self.skip_completed = bool(skip_completed)
        self.max_concurrency = normalise_concurrency(max_concurrency)

    def __eq__(self, other):
        if not isinstance(other, TaskOptions):
            return NotImplemented
        return (
            self.expand_pdf_pages == other.expand_pdf_pages
            and self.skip_completed == other.skip_completed
            and self.max_concurrency == other.max_concurrency
        )

    def __repr__(self):  # pragma: no cover - debugging aid
        return "TaskOptions(expand_pdf_pages={!r}, skip_completed={!r}, max_concurrency={!r})".format(
            self.expand_pdf_pages, self.skip_completed, self.max_concurrency
        )

    @property
    def is_default(self) -> bool:
        return self == TaskOptions()

    def payload(self) -> dict:
        """Only what differs from the default reaches the wire.

        Sending `max_concurrency: 0` would be sending "please use your default"
        as though it were a choice; omitting it says the same thing and leaves
        the server free to move its default without this build disagreeing.
        """
        body = {}
        if self.expand_pdf_pages:
            body["expand_pdf_pages"] = True
        if self.skip_completed:
            body["skip_completed"] = True
        if self.max_concurrency:
            body["max_concurrency"] = self.max_concurrency
        return body


def applicable(options: TaskOptions, has_pdf: bool) -> TaskOptions:
    """These options as they actually apply to THIS list of sheets.

    `expand_pdf_pages` only means something when a PDF is in the list. Left on
    over a list of TIFFs it is a checkbox that reads as a promise, a chip that
    names a file type nothing here has, and a field on the wire the server can
    do nothing with. So the panel disables it and this is where the value is
    settled, once, for the payload AND for the chip - two readings of one
    setting is how a screen comes to disagree with what it sends.

    The user's own choice is not overwritten: this returns what applies, and
    the stored tick comes back the moment a PDF joins the list.
    """
    if options is None:
        return TaskOptions()
    if has_pdf or not options.expand_pdf_pages:
        return options
    return TaskOptions(
        expand_pdf_pages=False,
        skip_completed=options.skip_completed,
        max_concurrency=options.max_concurrency,
    )


def normalise_concurrency(value) -> int:
    """Clamp to what the server will honour, treating nonsense as its default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return CONCURRENCY_SERVER_DEFAULT
    if number <= 0:
        return CONCURRENCY_SERVER_DEFAULT
    return min(number, MAX_CONCURRENCY)


def concurrency_choices():
    """(label, value) for the picker, server default first."""
    choices = [("Server default ({})".format(SERVER_DEFAULT_CONCURRENCY), CONCURRENCY_SERVER_DEFAULT)]
    for value in range(1, MAX_CONCURRENCY + 1):
        choices.append(("{} at a time".format(value), value))
    return choices


def summary(options: TaskOptions) -> str:
    """The chip beside the Options row.

    Names what was CHANGED rather than counting it: "2 changed" makes the user
    open the panel to find out which two, which is the panel they just closed.
    """
    if options is None or options.is_default:
        return "Default"
    changed = []
    if options.expand_pdf_pages:
        changed.append("Every PDF page")
    if options.skip_completed:
        changed.append("Skip completed")
    if options.max_concurrency:
        changed.append("{} at a time".format(options.max_concurrency))
    return " · ".join(changed)
