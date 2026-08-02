"""Build an installable QGIS plugin zip from the Companion sources."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PLUGIN_DIR_NAME = "mapdex"

generated = REPO / "packages" / "contracts" / "generated_python" / "contracts.py"

# What the QGIS plugin repository expects beside the code: metadata, the icon
# named in metadata.txt, the documentation and the licence.
DOCUMENTS = ("metadata.txt", "README.md", "LICENSE")
ASSETS = ("icon.png",)

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
        archive.write(path, f"{PLUGIN_DIR_NAME}/{path.relative_to(ROOT / 'mapdex_qgis').as_posix()}")

print(zip_path)
