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

# Only a commit already on master is published. A tag or a manual dispatch can
# name any commit, including one on a branch nobody reviewed, and a published
# tag goes on to plugins.qgis.org. A dry run skips this so a branch can be
# inspected.
if [ "$DRY_RUN" -eq 0 ]; then
  git fetch --quiet origin master || true
  if ! git merge-base --is-ancestor "$REF" origin/master; then
    echo "error: $REF is not on origin/master; only merged commits are published" >&2
    exit 1
  fi
fi

echo "==> splitting $PREFIX out of $REF"
SPLIT="$(git subtree split --prefix="$PREFIX" "$REF")"
COUNT="$(git rev-list --count "$SPLIT")"
echo "    $SPLIT ($COUNT commits)"

# WHAT IS PUSHED, WHEN THE PUBLIC REPO DID NOT START FROM THE SPLIT
# ----------------------------------------------------------------
# The public repo was opened on 2026-08-04 with two hand commits, before this
# script existed, so its main never descended from the split. Every publish from
# 2026-09-18 on was refused with "fetch first", and main there is protected
# against force pushes, so --force is refused as well.
#
# Instead the split is laid on top of the public head with one merge commit
# whose tree IS the split's tree: the public repo still shows exactly what is
# under $PREFIX, the real commits stay reachable as the merge's second parent,
# and the push is an ordinary fast-forward. Later publishes do the same on top
# of the last merge.
#
# This is not a licence to absorb anything. A commit reachable from the public
# head and not from the split must be one of the two known starting commits or
# a merge this script wrote (it carries a Mirrored-Split trailer). Anything else
# is a commit somebody made in the public repo, and the script still stops so a
# person can look at it.
LEGACY_PUBLIC_COMMITS="e4fdd04756f40ae476e7da835495481b5276f003 ab3395e234c243cb30cf4a7f21dd96f706ae2d73"
PUBLISH="$SPLIT"
PUBLIC=""
if [ "$FORCE" -eq 0 ] && git fetch --quiet "$REMOTE" "+refs/heads/$BRANCH:refs/mirror/public" 2>/dev/null; then
  PUBLIC="$(git rev-parse refs/mirror/public)"
fi
if [ -n "$PUBLIC" ] && ! git merge-base --is-ancestor "$PUBLIC" "$SPLIT"; then
  if git merge-base --is-ancestor "$SPLIT" "$PUBLIC"; then
    echo "==> $BRANCH there already contains $SPLIT; nothing new to publish"
    PUBLISH="$PUBLIC"
  else
    unknown=""
    for c in $(git rev-list "$PUBLIC" "^$SPLIT"); do
      case " $LEGACY_PUBLIC_COMMITS " in *" $c "*) continue ;; esac
      if git log -1 --format=%B "$c" | grep -q '^Mirrored-Split: '; then continue; fi
      unknown="$unknown $c"
    done
    if [ -n "$unknown" ]; then
      echo "error: $BRANCH there has commits this monorepo did not make:" >&2
      for c in $unknown; do git log -1 --format='    %h %an: %s' "$c" >&2; done
      echo "Bring them into $PREFIX here. To discard them instead, lift the force-push" >&2
      echo "rule on $BRANCH there and re-run with --force." >&2
      exit 1
    fi
    # Author and date come from the split head, so publishing the same split
    # onto the same public head always writes the same commit.
    GIT_AUTHOR_NAME="$(git log -1 --format=%an "$SPLIT")"
    GIT_AUTHOR_EMAIL="$(git log -1 --format=%ae "$SPLIT")"
    GIT_AUTHOR_DATE="$(git log -1 --format=%ad --date=raw "$SPLIT")"
    export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_AUTHOR_DATE
    export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL" GIT_COMMITTER_DATE="$GIT_AUTHOR_DATE"
    PUBLISH="$(printf 'Publish %s from the monorepo\n\nMirrored-Split: %s\n' "${SPLIT:0:12}" "$SPLIT" \
      | git commit-tree "$SPLIT^{tree}" -p "$PUBLIC" -p "$SPLIT")"
    echo "==> laid the split on top of $BRANCH there as $PUBLISH"
  fi
fi

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
  echo "    $PUBLISH -> $REMOTE $BRANCH$([ "$FORCE" -eq 1 ] && echo ' (forced)')"
  [ -n "$TAG" ] && echo "    tag $TAG -> $REMOTE"
  echo
  echo "published tree at the split head:"
  git ls-tree --name-only "$SPLIT" | sed 's/^/    /'
  exit 0
fi

echo "==> pushing to $REMOTE $BRANCH"
push_args=("$REMOTE" "$PUBLISH:refs/heads/$BRANCH")
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
  git push "$REMOTE" "$PUBLISH:refs/tags/$TAG"
fi

echo
echo "published $COUNT commits to $REMOTE $BRANCH"
