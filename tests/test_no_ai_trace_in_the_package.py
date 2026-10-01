"""AI Trace is unfinished, so it does not ship.

The assisted raster-to-vector tracer lives in `mapdex/research/qgis-ai-trace/`
and is deliberately outside this directory, because everything under this prefix
ships twice: `scripts/package.py` builds the installable zip from it, and
`scripts/publish_public_repo.sh` mirrors the WHOLE directory, with history, to
the public repository. A `.gitattributes` cannot narrow that - the script says so
itself - so the only way to keep unfinished work unpublished is to keep it out of
the prefix.

It had already leaked once. Three of the engine's modules were sitting in
`apps/qgis-plugin/mapdex/`, byte-identical to the copies in the research
directory and importing three more that were never beside them, so that subset
could not have run in any case. Nothing in the plugin imported it, nothing in the
zip carried it, and it would still have been published as source.

So this is a check rather than a note, and it asks the two separate questions a
note cannot:

* does the built package contain such a module (it is built here, never skipped
  for want of one - a packaging test that skips when there is no zip passes
  loudest on the machine that never builds); and
* does any shipped source import one, which is what would put it in the package
  in the first place.

The measurement bar it has to clear before it ships is in that directory's
README: a least-cost path always returns a path, so over blank paper it returns
the straight chord, and a trace with no measurement behind it cannot tell the
difference.
"""
import pathlib
import re
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The engine's own module names. `trace_view.py` is NOT one of them: it renders a
# compose turn's execution trace in the panel, it is maintained, and it ships.
AI_TRACE_MODULES = (
    "trace_benchmark",
    "trace_contracts",
    "trace_corpus",
    "trace_engine",
    "trace_junctions",
    "trace_livewire",
    "trace_maptool",
    "trace_metrics",
    "trace_neural",
    "trace_refiner",
    "trace_runtime",
)


def _build() -> pathlib.Path:
    """Build the production package and return it."""
    subprocess.run(
        [sys.executable, "scripts/package.py", "--channel", "production"],
        cwd=ROOT, check=True, capture_output=True,
    )
    built = ROOT / "dist" / "mapdex-qgis.zip"
    assert built.is_file(), "scripts/package.py produced no archive"
    return built


def test_the_built_package_carries_no_ai_trace_module():
    names = zipfile.ZipFile(_build()).namelist()
    leaked = [
        name for name in names
        if pathlib.PurePosixPath(name).stem in AI_TRACE_MODULES
    ]
    assert not leaked, (
        "AI Trace is unfinished and these are in the installable plugin: "
        + ", ".join(sorted(leaked))
    )
    # The check is only worth anything if the archive it read is the real one.
    assert any(name.endswith("/plugin.py") for name in names)


def test_no_shipped_source_imports_the_tracer():
    pattern = re.compile(
        r"\b(?:from|import)\s+\.?(?:%s)\b" % "|".join(AI_TRACE_MODULES)
    )
    offenders = []
    for path in sorted((ROOT / "mapdex_qgis").rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, (
        "these ship and import the unfinished tracer: " + ", ".join(offenders)
    )


def test_the_published_prefix_holds_no_tracer_source():
    """The mirror publishes this whole directory, zip or no zip."""
    stray = [
        path.relative_to(ROOT).as_posix()
        for path in sorted(ROOT.rglob("*.py"))
        if path.stem in AI_TRACE_MODULES
        and "dist" not in path.parts
        and "__pycache__" not in path.parts
    ]
    assert not stray, (
        "publish_public_repo.sh mirrors this directory whole; these would be "
        "published as source: " + ", ".join(stray)
    )
