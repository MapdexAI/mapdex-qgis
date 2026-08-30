#!/usr/bin/env bash
# Prove the published plugin tree stands on its own.
#
# The plugin is published to its own repository and uploaded to plugins.qgis.org
# as a zip, so it runs with no monorepo anywhere: no packages/contracts, no
# packages/ai-prompts, no Makefile. A published tree whose own CI cannot run is
# exactly the failure this repo already produced -- five red runs out of seven
# in the public repository, because the mirror shipped a tree missing the files
# its CI installs from.
#
# So: copy out the published file set, delete every trace of the monorepo, and
# run the plugin's own suite against it. The only monorepo coupling allowed is
# the two drift tests that deliberately skip when the monorepo is absent; any
# third skip, or any failure, means the plugin grew a dependency it cannot take
# with it.
#
#   ./scripts/standalone_check.sh
#
# The published file set is exactly the tracked files under this directory,
# because publishing is `git subtree split --prefix=mapdex/apps/qgis-plugin`
# and nothing rewrites the tree on the way out.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(git -C "$ROOT" rev-parse --show-toplevel)"
PREFIX="$(realpath --relative-to="$REPO" "$ROOT")"

# The two tests that are allowed to skip, and only for the standalone reason.
# Both compare a vendored copy against a source that lives outside the published
# tree: the generated contracts excerpt, and the vendored nivo-gis package.
# (test_vendored_prompt.py left when the prompt moved into that package - the
# plugin no longer carries a separate copy of it to drift.)
EXPECTED_SKIP_FILES="tests/test_vendored_contracts.py tests/test_vendored_nivo.py"

WORK="$(mtmp=$(mktemp -d -t mapdex-qgis-standalone-XXXXXX); echo "$mtmp")"
trap 'rm -rf "$WORK"' EXIT

TREE="$WORK/mapdex-qgis"
mkdir -p "$TREE"

echo "==> materializing the published file set from $PREFIX"
# What is about to be published: tracked files plus new ones, minus anything
# gitignored, minus anything deleted in the working tree. On a clean CI
# checkout that is exactly the tracked set, which is exactly what
# `git subtree split` carries.
git -C "$REPO" ls-files -z --cached --others --exclude-standard -- "$PREFIX" \
  | while IFS= read -r -d '' path; do
      # A tracked file deleted in the working tree will not be published.
      [ -f "$REPO/$path" ] || continue
      rel="${path#"$PREFIX"/}"
      mkdir -p "$TREE/$(dirname "$rel")"
      cp "$REPO/$path" "$TREE/$rel"
    done

file_count=$(find "$TREE" -type f | wc -l)
echo "    $file_count files"

# The tree must not be able to reach the monorepo even by accident. It is under
# /tmp, so there is no packages/ above it either.
if find "$TREE" -type d -name packages -print -quit | grep -q .; then
  echo "error: the published set contains a packages/ directory" >&2
  exit 1
fi

echo "==> the monorepo is absent"
for probe in packages/contracts/generated_python/contracts_qgis.py \
             packages/ai-prompts/prompts/nivo.system.md; do
  if [ -e "$TREE/../../$probe" ] || [ -e "$TREE/../$probe" ]; then
    echo "error: $probe is reachable from the standalone tree" >&2
    exit 1
  fi
done
echo "    no packages/contracts, no packages/ai-prompts"

echo "==> building the plugin package with no monorepo present"
cd "$TREE"
# Built before the suite, not after: several release checks assert things about
# dist/mapdex-qgis.zip and skip when it is missing. Packaging first is what
# makes them run, so the standalone check covers them too -- and it proves
# package.py falls back to the vendored contracts when the generated excerpt in
# packages/ is not there to substitute.
python3 scripts/package.py >/dev/null
test -f dist/mapdex-qgis.zip || { echo "error: no zip produced" >&2; exit 1; }
python3 - <<'PY'
import zipfile
names = zipfile.ZipFile("dist/mapdex-qgis.zip").namelist()
assert "mapdex/generated_contracts.py" in names, "the vendored contracts did not ship"
assert "mapdex/metadata.txt" in names, "metadata.txt did not ship"
print("    zip contains {} entries, including the vendored contracts".format(len(names)))
PY

echo "==> running the plugin's own suite"
# PYTEST_DISABLE_PLUGIN_AUTOLOAD, because this suite needs no third-party
# pytest plugin and autoload makes the run depend on whatever else happens to
# be installed in the interpreter's environment. On the self-hosted runner that
# environment is a SHARED ~/.local that other jobs pip-install into, and it
# broke this check with a traceback naming neither the plugin nor its tests:
# an `anyio` left there autoloaded its pytest plugin and died on a missing
# `typing_extensions`. The published tree passing or failing must depend on the
# published tree.
set +e
output="$(PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests -q -rs 2>&1)"
status=$?
set -e
echo "$output" | tail -20

if [ "$status" -ne 0 ]; then
  echo >&2
  echo "error: the published tree does not pass its own tests." >&2
  echo "The mirror must not publish it -- fix the coupling first." >&2
  exit 1
fi

echo "==> checking that nothing skipped for an unplanned reason"
skips="$(echo "$output" | grep '^SKIPPED' || true)"
if [ -z "$skips" ]; then
  echo "error: expected the two standalone drift skips, got none." >&2
  echo "If those tests were removed, update EXPECTED_SKIP_FILES here." >&2
  exit 1
fi

bad="$(echo "$skips" | grep -v 'standalone checkout:' || true)"
if [ -n "$bad" ]; then
  echo >&2
  echo "error: a test skipped for a reason other than the standalone checkout:" >&2
  echo "$bad" >&2
  exit 1
fi

# Which files skipped, deduplicated, in a stable order.
got="$(echo "$skips" | sed 's/^SKIPPED \[[0-9]*\] //; s/:.*//' | sort -u | tr '\n' ' ')"
want="$(echo "$EXPECTED_SKIP_FILES" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
if [ "$got" != "$want" ]; then
  echo >&2
  echo "error: the set of standalone-skipping tests changed." >&2
  echo "  expected: $want" >&2
  echo "  got:      $got" >&2
  echo "A new one means the plugin gained a monorepo dependency it cannot" >&2
  echo "publish. Remove the dependency, or record it here deliberately." >&2
  exit 1
fi
echo "    only $want, as intended"

echo
echo "the published tree stands on its own."
