"""Build an installable QGIS plugin zip from the Companion sources.

    python scripts/package.py                      # development build
    python scripts/package.py --channel production # release build

A production build is pinned to the hosted Mapdex: `build_profile.CHANNEL` is
rewritten in the packaged copy, which hides the API/web address fields so a
published install cannot be pointed elsewhere by accident. The working tree is
never modified.
"""
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PLUGIN_DIR_NAME = "mapdex"

# The plugin-scoped excerpt of the Python bindings (see qgis_subset.go), not
# the full module. Present only in the monorepo; in the public repository the
# vendored copy in mapdex_qgis/ is the one that ships.
generated = REPO / "packages" / "contracts" / "generated_python" / "contracts_qgis.py"

# What the QGIS plugin repository expects beside the code: metadata, the icon
# named in metadata.txt, the documentation and the licence.
DOCUMENTS = ("metadata.txt", "README.md", "LICENSE")
ASSETS = ("icon.png",)
CHANNELS = ("development", "production")

# The vendored `nivo` package is source, so `rglob("*.py")` below already
# carries its modules. What it does NOT carry is the package's data: the shared
# system prompt, which `nivo.prompt` opens at import time, its MIT licence, and
# the `py.typed` marker. A zip missing the prompt imports cleanly here and
# raises FileNotFoundError inside QGIS on a user's machine, so the non-Python
# files are collected explicitly rather than left to a glob that cannot see
# them.
VENDOR_DIR = "_vendor"
VENDOR_DATA_SUFFIXES = (".md", ".txt", ".json")
VENDOR_DATA_NAMES = ("py.typed", "NIVO_VERSION", "NIVO-LICENSE")


def channel_from(argv) -> str:
    if "--channel" in argv:
        value = argv[argv.index("--channel") + 1]
    else:
        value = "development"
    if value not in CHANNELS:
        raise SystemExit("unknown channel {!r}; use one of {}".format(value, ", ".join(CHANNELS)))
    return value


def stamped_build_profile(source: Path, channel: str) -> bytes:
    """Return build_profile.py with CHANNEL set for this package."""
    text = source.read_text(encoding="utf-8")
    stamped, count = re.subn(
        r'^CHANNEL = ".*"$', 'CHANNEL = "{}"'.format(channel), text, count=1, flags=re.M
    )
    if count != 1:
        raise SystemExit("could not stamp the build channel into build_profile.py")
    return stamped.encode("utf-8")


# Files whose absence from the zip is invisible until QGIS loads the plugin on
# somebody else's machine. Each one is opened at import time by code that ships
# in the same archive, so a missing entry is an ImportError in a release rather
# than a degraded feature.
REQUIRED_ENTRIES = (
    f"{PLUGIN_DIR_NAME}/__init__.py",
    f"{PLUGIN_DIR_NAME}/mapdex_capabilities.py",
    f"{PLUGIN_DIR_NAME}/generated_contracts.py",
    f"{PLUGIN_DIR_NAME}/_vendor/__init__.py",
    f"{PLUGIN_DIR_NAME}/_vendor/nivo/__init__.py",
    f"{PLUGIN_DIR_NAME}/_vendor/nivo/capabilities.py",
    f"{PLUGIN_DIR_NAME}/_vendor/nivo/prompts/nivo.system.md",
    # MIT: shipping the source without the terms is a licence violation, not a
    # packaging nicety.
    f"{PLUGIN_DIR_NAME}/_vendor/NIVO-LICENSE",
    f"{PLUGIN_DIR_NAME}/metadata.txt",
)


def _verify(zip_path: Path) -> None:
    """Refuse to hand over an archive that cannot load."""
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    missing = [name for name in REQUIRED_ENTRIES if name not in names]
    if missing:
        raise SystemExit("the package is missing files it needs to import: {}".format(", ".join(missing)))


def main(argv) -> int:
    channel = channel_from(argv)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / "mapdex-qgis.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in DOCUMENTS:
            source = ROOT / name
            if source.is_file():
                archive.write(source, f"{PLUGIN_DIR_NAME}/{name}")
        for name in ASSETS:
            source = ROOT / "mapdex_qgis" / name
            if source.is_file():
                archive.write(source, f"{PLUGIN_DIR_NAME}/{name}")
        for path in sorted((ROOT / "mapdex_qgis" / "assets").rglob("*")):
            if path.is_file():
                archive.write(
                    path,
                    f"{PLUGIN_DIR_NAME}/assets/{path.relative_to(ROOT / 'mapdex_qgis' / 'assets').as_posix()}",
                )
        for path in sorted((ROOT / "mapdex_qgis").rglob("*.py")):
            if path.name.endswith("_test.py") or path.name == "conftest.py":
                continue
            if path.name == "generated_contracts.py" and generated.is_file():
                archive.write(generated, f"{PLUGIN_DIR_NAME}/generated_contracts.py")
                continue
            if path.name == "build_profile.py":
                archive.writestr(
                    f"{PLUGIN_DIR_NAME}/build_profile.py",
                    stamped_build_profile(path, channel),
                )
                continue
            archive.write(
                path, f"{PLUGIN_DIR_NAME}/{path.relative_to(ROOT / 'mapdex_qgis').as_posix()}"
            )
        for path in sorted((ROOT / "mapdex_qgis" / VENDOR_DIR).rglob("*")):
            if not path.is_file() or path.suffix == ".py":
                continue
            if "__pycache__" in path.parts:
                continue
            if path.suffix not in VENDOR_DATA_SUFFIXES and path.name not in VENDOR_DATA_NAMES:
                continue
            archive.write(
                path, f"{PLUGIN_DIR_NAME}/{path.relative_to(ROOT / 'mapdex_qgis').as_posix()}"
            )

    _verify(zip_path)
    print("{} ({} build)".format(zip_path, channel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
