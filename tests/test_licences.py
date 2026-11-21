"""Third-party terms ship complete, and the package refuses to ship without them."""
import pathlib
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package  # noqa: E402

ASSETS = ROOT / "mapdex_qgis" / "assets"
LUCIDE_LICENCES = (ASSETS / "lucide" / "LICENSE.txt", ASSETS / "LUCIDE-LICENSE.txt")

# The Feather-derived icons this plugin bundles, as named in the published
# lucide-static 1.17.0 LICENSE. Their terms are MIT, not ISC.
FEATHER_ICONS_BUNDLED = (
    "arrow-left", "arrow-up", "check", "chevron-down", "chevron-right", "crosshair",
    "external-link", "log-out", "square", "upload", "x", "zoom-in",
)


def test_the_lucide_licence_carries_both_its_parts():
    for path in LUCIDE_LICENCES:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("ISC License"), path
        assert "Lucide Icons and Contributors" in text, path
        assert "The MIT License (MIT) (for the icons listed above)" in text, path
        assert "Copyright (c) 2013-present Cole Bemis" in text, path
        assert "shall be included in all\ncopies or substantial portions of the Software." in text, path
    assert LUCIDE_LICENCES[0].read_bytes() == LUCIDE_LICENCES[1].read_bytes()


def test_every_bundled_feather_icon_is_named_in_the_licence():
    text = LUCIDE_LICENCES[0].read_text(encoding="utf-8")
    listed = text.split("derived from the Feather project:")[1].split("The MIT License")[0]
    names = {name.strip() for name in listed.replace("\n", " ").split(",")}
    for icon in FEATHER_ICONS_BUNDLED:
        assert (ASSETS / "lucide" / "{}.svg".format(icon)).is_file(), icon
        assert icon in names, icon


def _zip(tmp_path, skip=(), replace=None):
    path = tmp_path / "plugin.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in package.REQUIRED_ENTRIES:
            archive.writestr(name, "x")
        sources = {
            "mapdex/assets/lucide/LICENSE.txt": LUCIDE_LICENCES[0],
            "mapdex/assets/LUCIDE-LICENSE.txt": LUCIDE_LICENCES[1],
            "mapdex/assets/fonts/OFL.txt": ASSETS / "fonts" / "OFL.txt",
        }
        for name, source in sources.items():
            if name in skip:
                continue
            data = (replace or {}).get(name)
            archive.writestr(name, data if data is not None else source.read_bytes())
    return path


def test_a_complete_package_passes(tmp_path):
    package._verify(_zip(tmp_path))


@pytest.mark.parametrize("name", sorted(package.REQUIRED_LICENCES))
def test_a_package_missing_a_licence_is_refused(tmp_path, name):
    with pytest.raises(SystemExit, match="without their licence"):
        package._verify(_zip(tmp_path, skip=(name,)))


def test_a_lucide_licence_cut_to_its_isc_half_is_refused(tmp_path):
    text = LUCIDE_LICENCES[0].read_text(encoding="utf-8")
    half = text.split("\n---\n")[0].encode("utf-8")
    with pytest.raises(SystemExit, match="Cole Bemis"):
        package._verify(_zip(tmp_path, replace={"mapdex/assets/lucide/LICENSE.txt": half}))
