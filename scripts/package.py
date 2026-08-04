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

generated = REPO / "packages" / "contracts" / "generated_python" / "contracts.py"

# What the QGIS plugin repository expects beside the code: metadata, the icon
# named in metadata.txt, the documentation and the licence.
DOCUMENTS = ("metadata.txt", "README.md", "LICENSE")
ASSETS = ("icon.png",)
CHANNELS = ("development", "production")


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

    print("{} ({} build)".format(zip_path, channel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
