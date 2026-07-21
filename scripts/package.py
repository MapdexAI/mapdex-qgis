"""Build an installable QGIS plugin zip from the Companion sources."""
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PLUGIN_DIR_NAME = "mapdex"

generated = REPO / "packages" / "contracts" / "generated_python" / "contracts.py"
target = ROOT / "mapdex_qgis" / "generated_contracts.py"
if generated.is_file():
    shutil.copy2(generated, target)

dist = ROOT / "dist"
dist.mkdir(exist_ok=True)
zip_path = dist / "mapdex-qgis.zip"

with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.write(ROOT / "metadata.txt", f"{PLUGIN_DIR_NAME}/metadata.txt")
    for path in sorted((ROOT / "mapdex_qgis").rglob("*.py")):
        if path.name.endswith("_test.py") or path.name == "conftest.py":
            continue
        archive.write(path, f"{PLUGIN_DIR_NAME}/{path.relative_to(ROOT / 'mapdex_qgis').as_posix()}")

print(zip_path)
