from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
generated = REPO / "packages" / "contracts" / "generated_python" / "contracts.py"
target = ROOT / "mapdex_qgis" / "generated_contracts.py"
shutil.copy2(generated, target)
dist = ROOT / "dist"; dist.mkdir(exist_ok=True)
with zipfile.ZipFile(dist / "mapdex-qgis.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    archive.write(ROOT / "metadata.txt", "mapdex/metadata.txt")
    for path in (ROOT / "mapdex_qgis").rglob("*.py"):
        archive.write(path, "mapdex/" + str(path.relative_to(ROOT / "mapdex_qgis")))
print(dist / "mapdex-qgis.zip")
