#!/usr/bin/env bash
# Publish the QGIS plugin to its public repository, with its history.
#
# This replaces the old scripts/sync_public_repo.sh, which rsync'd a chosen
# subset of files into a clone and committed them as one "chore: mirror <sha>".
# Two things were wrong with that. The public repo could never have usable blame
# or bisect -- every release looked like a single squashed drop from a machine.
# And because the next run committed whatever the monorepo said, any commit a
# contributor landed in the public repo was silently overwritten.
#
# `git subtree split` instead replays the real commits that touched the plugin,
# preserving message, author and date. Publishing becomes a fast-forward of the
# public branch rather than a fresh snapshot on top of it.
#
#   ./scripts/publish_public_repo.sh --dry-run
#   ./scripts/publish_public_repo.sh --remote git@github.com:MapdexAI/mapdex-qgis.git
#   ./scripts/publish_public_repo.sh --tag v0.10.1
#
# WHY THE PUBLISHED SET IS THE WHOLE DIRECTORY
# --------------------------------------------
# A split publishes everything under the prefix; it cannot rename or drop files
# on the way out. (`.gitattributes` export-ignore does not help -- that applies
# to `git archive`, not to subtree split.) So instead of filtering, the prefix
# is laid out exactly as the public repo should look: the public repo's own
# workflows live at mapdex/apps/qgis-plugin/.github/, which is inert here
# because GitHub only discovers .github/ at a repository root. What you see
# under the prefix is what gets published, which is also what makes
# scripts/standalone_check.sh an exact test rather than an approximation.
#
# WHY FAST-FORWARD RATHER THAN --rejoin OR --force
# -----------------------------------------------
# `--rejoin` writes a merge commit back into the monorepo on every publish, so
# CI would have to push to master to mirror a plugin change. That is a much
# larger permission and a much noisier history than this is worth.
#
# Without --rejoin the split is recomputed each time, and it is deterministic:
# the same monorepo history over the same prefix synthesizes the same commits,
# so each publish fast-forwards the last. A rejected push therefore means
# something real -- the public repo has commits the monorepo does not know
# about -- and stopping to look at that is the correct behaviour. Forcing is
# available, but only when a human asks for it by name.
set -euo pipefail

PREFIX="mapdex/apps/qgis-plugin"
REMOTE="${MAPDEX_QGIS_REMOTE:-git@github.com:MapdexAI/mapdex-qgis.git}"
BRANCH="main"
REF="HEAD"
TAG=""
FORCE=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --remote) REMOTE="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --ref)    REF="$2";    shift 2 ;;
    --tag)    TAG="$2";    shift 2 ;;
    --force)  FORCE=1;     shift ;;
    --dry-run) DRY_RUN=1;  shift ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

if [ ! -d "$PREFIX" ]; then
  echo "error: $PREFIX does not exist; run this from the Mapdex monorepo" >&2
  exit 2
fi

# A shallow clone silently produces a truncated history, which would then be
# force-pushed over a complete one. Refuse rather than publish a stump.
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "error: this is a shallow clone, so the split would lose history." >&2
  echo "Fetch with depth 0 (actions/checkout: fetch-depth: 0) and retry." >&2
  exit 2
fi

echo "==> splitting $PREFIX out of $REF"
SPLIT="$(git subtree split --prefix="$PREFIX" "$REF")"
COUNT="$(git rev-list --count "$SPLIT")"
echo "    $SPLIT ($COUNT commits)"

# The tag must name the version the tree actually declares, or the public
# release workflow packages one version under another's name.
if [ -n "$TAG" ]; then
  declared="$(git show "$SPLIT:metadata.txt" | grep -m1 '^version=' | cut -d= -f2 | tr -d '[:space:]')"
  if [ "$TAG" != "v$declared" ]; then
    echo "error: tag $TAG does not match metadata.txt version $declared" >&2
    echo "Bump metadata.txt, or tag qgis-v$declared." >&2
    exit 1
  fi
  echo "==> tag $TAG matches metadata.txt ($declared)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "dry run: would push"
  echo "    $SPLIT -> $REMOTE $BRANCH$([ "$FORCE" -eq 1 ] && echo ' (forced)')"
  [ -n "$TAG" ] && echo "    tag $TAG -> $REMOTE"
  echo
  echo "published tree at the split head:"
  git ls-tree --name-only "$SPLIT" | sed 's/^/    /'
  exit 0
fi

echo "==> pushing to $REMOTE $BRANCH"
push_args=("$REMOTE" "$SPLIT:refs/heads/$BRANCH")
[ "$FORCE" -eq 1 ] && push_args=(--force "${push_args[@]}")

if ! git push "${push_args[@]}"; then
  echo >&2
  echo "error: the public repository refused the push." >&2
  echo >&2
  echo "This is not normally a transient failure. It means $BRANCH there has" >&2
  echo "commits this split does not contain -- someone pushed to the public" >&2
  echo "repo directly, or the monorepo's history was rewritten." >&2
  echo >&2
  echo "Look at what diverged before doing anything else. If the public" >&2
  echo "commits should be discarded, re-run with --force." >&2
  exit 1
fi

if [ -n "$TAG" ]; then
  echo "==> pushing tag $TAG"
  git push "$REMOTE" "$SPLIT:refs/tags/$TAG"
fi

echo
echo "published $COUNT commits to $REMOTE $BRANCH"
