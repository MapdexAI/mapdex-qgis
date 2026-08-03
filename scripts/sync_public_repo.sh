#!/usr/bin/env bash
# Mirror the QGIS plugin into its public open-source repository.
#
# The monorepo stays the source of truth. The public repo must match the zip
# uploaded to plugins.qgis.org byte for byte, so this copies the published set
# (and deletes what no longer belongs), vendors the generated contracts module
# that only exists inside the monorepo, and stops. Review and push by hand.
#
#   ./scripts/sync_public_repo.sh ~/src/mapdex-qgis
#
set -euo pipefail

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "usage: $0 <path-to-public-repo-clone>" >&2
  exit 2
fi
if [[ ! -d "$TARGET/.git" ]]; then
  echo "error: $TARGET is not a git clone" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GENERATED="$(cd "$ROOT/../.." && pwd)/packages/contracts/generated_python/contracts.py"

rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  "$ROOT/mapdex_qgis/" "$TARGET/mapdex_qgis/"
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  "$ROOT/tests/" "$TARGET/tests/"

mkdir -p "$TARGET/scripts" "$TARGET/.github/workflows"
cp "$ROOT/scripts/package.py" "$ROOT/scripts/publish.py" "$ROOT/scripts/release_checks.py" "$TARGET/scripts/"
cp "$ROOT/ci/github-actions-ci.yml" "$TARGET/.github/workflows/ci.yml"
cp "$ROOT/ci/github-actions-release.yml" "$TARGET/.github/workflows/release.yml"
cp "$ROOT/metadata.txt" "$ROOT/README.md" "$ROOT/LICENSE" "$TARGET/"

# generated_contracts.py is produced by `make gen` in the monorepo. The public
# repo carries the vendored copy so its own CI can build the same zip.
if [[ -f "$GENERATED" ]]; then
  cp "$GENERATED" "$TARGET/mapdex_qgis/generated_contracts.py"
fi

cat > "$TARGET/.gitignore" <<'IGNORE'
__pycache__/
*.pyc
.pytest_cache/
dist/
IGNORE

echo "Synced into $TARGET"
echo
git -C "$TARGET" status --short
echo
echo "Review the diff, then commit and push from $TARGET."
