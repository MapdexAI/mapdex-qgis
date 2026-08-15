"""The runtime version must come from metadata.txt, not a hand-copied string.

Before this, the companion context reported a version literal that had drifted
from the packaged plugin, so server-side telemetry attributed new behaviour to
an old build.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.build_version import PLUGIN_VERSION  # noqa: E402


def _metadata_version() -> str:
    for line in (ROOT / "metadata.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("metadata.txt has no version")


def test_the_runtime_version_matches_metadata():
    assert PLUGIN_VERSION == _metadata_version()
    assert PLUGIN_VERSION != "0.0.0"


def test_no_module_hardcodes_a_version_literal():
    # A second copy of the version is how the two drifted apart in the first
    # place, so the source tree must not reintroduce one.
    version = _metadata_version()
    for path in (ROOT / "mapdex_qgis").glob("*.py"):
        if path.name == "build_version.py":
            continue
        assert '"{}"'.format(version) not in path.read_text(encoding="utf-8"), path.name
