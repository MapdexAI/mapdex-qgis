"""The sheets a task will run over: a LIST, and everything decided about it.

The Task page used to ask "which source?" with a combo whose three entries were
`Select source…`, `Active QGIS layer` and `Choose files…`. That shape carries one
answer, so it could express a batch only by accident - the file dialog happened
to be multi-select and several files happened to start a real server-side batch,
which is exactly why nobody found it (docs/MEMORY.md, 2026-08-24: "a capability
nothing on screen mentions is one that does not exist for the person using it").
A picker that names one thing cannot say "these twelve sheets".

So the model here is a list of sources, each one named, each one removable, and
the two ways in - files from disk and layers already open in QGIS - add to the
same list rather than replacing each other. A batch is what a list of more than
one IS; nothing has to be switched on.

**Layers are added by ticking them, never by being open.** docs/UX.md keeps
desktop submission from expanding into "an unexpected export/upload of arbitrary
project layers", and that rule is about IMPLICIT expansion: an action that
quietly grows to cover work the user did not choose. An explicit picker where
each layer is selected by name is the same shape as the multi-file dialog that
rule already permits. What stays forbidden is a control that submits the project.

No Qt import. The ordering, the wording, the compatibility rule and the dedupe
are decided somewhere the headless suite can reach, because Qt is absent from
this plugin's test environment - the same reason `job_items.py` exists.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Sequence, Tuple

from .source_info import RASTER_EXTENSIONS, VECTOR_EXTENSIONS, human_size

# The two ways a sheet reaches a task. Kept as constants because three modules
# now compare against them and three copies of a string is how they drift.
KIND_FILE = "file"
KIND_LAYER = "layer"

# What a workflow reads. `validate_deliver` is the one that wants geometry; the
# other three read a scanned sheet. Derived from the workflow id rather than
# from a per-workflow table, so a new BatchKind is raster by default and a
# reviewer has to say otherwise rather than forget to.
VECTOR_WORKFLOWS = ("validate_deliver",)

DATA_RASTER = "raster"
DATA_VECTOR = "vector"
DATA_UNKNOWN = "unknown"


class Source:
    """One sheet in the list.

    `key` is what identity means for this kind: an absolute path for a file, a
    QGIS layer id for a layer. Dedupe is on (kind, key), so the same file added
    twice is one sheet and two layers pointing at one file are two - which is
    correct, because a person who ticked both meant both.

    `path` is separate from `key` on purpose. A raster layer's bytes are a file
    on disk and get uploaded directly; a vector layer has no single file we may
    upload and is exported at submit time, so its path is empty here and filled
    in later. Reading `key` as a path is how a layer id would end up passed to
    `open()`.
    """

    __slots__ = ("kind", "key", "label", "data_kind", "size", "detail", "path")

    def __init__(self, kind, key, label, data_kind=DATA_UNKNOWN, size=0, detail="", path=""):
        self.kind = kind
        self.key = key
        self.label = label
        self.data_kind = data_kind
        self.size = int(size or 0)
        self.detail = detail
        self.path = path

    @property
    def identity(self) -> Tuple[str, str]:
        return (self.kind, self.key)

    def __eq__(self, other):
        if not isinstance(other, Source):
            return NotImplemented
        return (
            self.identity == other.identity
            and self.label == other.label
            and self.data_kind == other.data_kind
            and self.size == other.size
            and self.detail == other.detail
            and self.path == other.path
        )

    def __hash__(self):
        return hash(self.identity)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Source({!r}, {!r}, {!r})".format(self.kind, self.key, self.label)


def data_kind_for_path(path: str) -> str:
    """Raster, vector or unknown, from the extension alone.

    The extension tables live in `source_info` and are not restated here: they
    are already the one owner of that answer, and a second copy is a second
    thing to update when a format is added.
    """
    extension = os.path.splitext(str(path or ""))[1].lower()
    if extension in RASTER_EXTENSIONS:
        return DATA_RASTER
    if extension in VECTOR_EXTENSIONS:
        return DATA_VECTOR
    return DATA_UNKNOWN


def file_source(path: str) -> Source:
    """A sheet chosen from disk."""
    absolute = os.path.abspath(str(path or ""))
    size = 0
    if os.path.isfile(absolute):
        size = os.path.getsize(absolute)
    return Source(
        kind=KIND_FILE,
        key=absolute,
        label=os.path.basename(absolute) or absolute,
        data_kind=data_kind_for_path(absolute),
        size=size,
        detail=human_size(size) if size else "",
        path=absolute,
    )


def layer_source(layer_id: str, name: str, data_kind: str, crs: str = "", path: str = "") -> Source:
    """A sheet that is already open in QGIS.

    `crs` becomes the row's second line because it is the fact a person checks
    before sending a layer somewhere - and an absent one is stated as absent
    rather than dropped, since "no CRS" is a finding and a blank row is not.
    """
    resolved = str(path or "")
    size = os.path.getsize(resolved) if resolved and os.path.isfile(resolved) else 0
    detail = str(crs or "No CRS")
    if size:
        detail = "{} · {}".format(detail, human_size(size))
    return Source(
        kind=KIND_LAYER,
        key=str(layer_id or ""),
        label=str(name or layer_id or ""),
        data_kind=str(data_kind or DATA_UNKNOWN),
        size=size,
        detail=detail,
        path=resolved,
    )


def add_sources(existing: Sequence[Source], incoming: Iterable[Source]) -> Tuple[List[Source], int, int]:
    """Append what is new, in the order it arrived, and report what repeated.

    Returns (sources, added, duplicates). The duplicate count is returned rather
    than swallowed: adding four files to a list that already holds three of them
    and seeing the count go up by one reads as the picker having lost them.
    """
    result = list(existing)
    seen = {source.identity for source in result}
    added = 0
    duplicates = 0
    for source in incoming:
        if source is None or not source.key:
            continue
        if source.identity in seen:
            duplicates += 1
            continue
        seen.add(source.identity)
        result.append(source)
        added += 1
    return result, added, duplicates


def remove_source(sources: Sequence[Source], kind: str, key: str) -> List[Source]:
    """Drop one sheet. Removing something absent is not an error."""
    return [source for source in sources if source.identity != (kind, key)]


def wanted_data_kind(workflow: str) -> str:
    """What this workflow reads."""
    return DATA_VECTOR if str(workflow or "") in VECTOR_WORKFLOWS else DATA_RASTER


def incompatible(sources: Sequence[Source], workflow: str) -> List[Source]:
    """The sheets this workflow cannot read, named individually.

    Named, rather than counted, because a list of twelve with one wrong file in
    it is a list the person can fix in one click once they know WHICH.
    """
    wanted = wanted_data_kind(workflow)
    return [source for source in sources if source.data_kind != wanted]


def missing_files(sources: Sequence[Source]) -> List[Source]:
    """File sheets whose bytes are no longer on disk.

    A layer is not checked here: a vector layer is exported at submit time and
    has no path yet, and reporting it as missing would refuse a source that is
    open on screen.
    """
    return [
        source
        for source in sources
        if source.kind == KIND_FILE and not os.path.isfile(source.path or source.key)
    ]


def has_pdf(sources: Sequence[Source]) -> bool:
    """Whether any sheet in this list is a PDF.

    The "treat every PDF page as its own sheet" option is meaningless over a
    list of TIFFs, and offering it there is not merely clutter: it is ticked,
    it names a file type nothing in the list has, and it reads as a promise
    about work that cannot happen. An option is offered when it can act.

    Read from the PATH and from nothing else. A layer's `key` is a QGIS id and
    its `label` is whatever the user called it, so either can end in ".pdf"
    while naming no document at all - and a layer genuinely loaded FROM a PDF
    carries that file as its path, which is the case the option exists for. The
    name is all we have (the list holds a path, never a decoded document); a
    `.pdf` that is not one is the server's answer to give, not this panel's.
    """
    for source in sources:
        path = str(source.path or (source.key if source.kind == KIND_FILE else ""))
        if os.path.splitext(path)[1].lower() == ".pdf":
            return True
    return False


def total_size(sources: Sequence[Source]) -> int:
    return sum(source.size for source in sources)


def is_batch(sources: Sequence[Source]) -> bool:
    """More than one sheet IS a batch. There is no switch."""
    return len(sources) > 1


def summary(sources: Sequence[Source], workflow: str) -> dict:
    """The one line under the source list, and whether Start may run.

    Mirrors `source_info.inspect_paths` in shape - {"valid", "summary", "error"}
    - because the Task page already renders that shape and one shape means the
    two paths cannot disagree about what a refusal looks like.
    """
    if not sources:
        return {"valid": False, "summary": "No sources selected", "error": "Add at least one source."}

    gone = missing_files(sources)
    if gone:
        return {
            "valid": False,
            "summary": _measured(sources),
            "error": "{} is no longer on disk. Remove it or choose it again.".format(gone[0].label)
            if len(gone) == 1
            else "{} of these files are no longer on disk.".format(len(gone)),
        }

    offenders = incompatible(sources, workflow)
    if offenders:
        expected = "vector" if wanted_data_kind(workflow) == DATA_VECTOR else "raster or PDF"
        return {
            "valid": False,
            "summary": _measured(sources),
            "error": "This workflow needs a {} source. {} is not one.".format(expected, offenders[0].label)
            if len(offenders) == 1
            else "This workflow needs {} sources. {} of these are not.".format(expected, len(offenders)),
        }

    return {"valid": True, "summary": _measured(sources), "error": ""}


def _measured(sources: Sequence[Source]) -> str:
    """What is in the list, counted by where it came from.

    Files and layers are counted apart because they behave differently on the
    way out - a file is uploaded as it is, a layer may be exported first - and a
    person who sees "5 sources" cannot tell which of those is about to happen.
    """
    files = [source for source in sources if source.kind == KIND_FILE]
    layers = [source for source in sources if source.kind == KIND_LAYER]
    parts = []
    if files:
        parts.append("{} file{}".format(len(files), "" if len(files) == 1 else "s"))
    if layers:
        parts.append("{} layer{}".format(len(layers), "" if len(layers) == 1 else "s"))
    measured = " · ".join(parts) if parts else "No sources selected"
    size = total_size(sources)
    if size:
        measured = "{} · {}".format(measured, human_size(size))
    return measured


def start_label(sources: Sequence[Source], price: str = "") -> str:
    """What the Start control says: what it starts, and what that costs.

    A batch says so on the button. "Start task" over twelve sheets understates
    what pressing it does, and the cost of a batch is twelve times the cost of
    the task the label describes.

    The money belongs HERE rather than only in the line beside it: pressing this
    is the moment it is spent, and somebody who presses without reading the
    surrounding sentence has still been told. The figure is what the batch
    HOLDS while it runs (its traces; placing is free) and the line beside the
    button says what a placed sheet costs later, the first time it is
    downloaded, which is why the total is not repeated there.

    This module computes no price. It renders the string it is handed; the
    server owns the figure and `task_price` multiplies it. An empty `price`
    means the workflow is unpriced or free, and the button then says nothing
    about money rather than showing a zero.
    """
    if is_batch(sources):
        label = "Start batch · {} sheets".format(len(sources))
    else:
        label = "Start task"
    return "{} · {}".format(label, price) if price else label


def ready_notice(sources: Sequence[Source], workflow_title: str) -> str:
    """The status sentence once a list is ready to send."""
    if not sources:
        return ""
    if is_batch(sources):
        return "Ready to run {} over {} sheets as one batch.".format(
            workflow_title or "this workflow", len(sources)
        )
    return "Ready to run {} on {}.".format(workflow_title or "this workflow", sources[0].label)
