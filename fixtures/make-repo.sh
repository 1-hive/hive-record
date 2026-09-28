#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the deterministic fixture repository that fixture pins reference (SPEC §21).
#
#   fixtures/make-repo.sh [ROOT]      (default ROOT: /tmp/hive-record-fixture)
#
# Creates ROOT/fixture.git (bare: the "published" remote, refs/heads/main),
# ROOT/fixture (a clone: hivepin's local_path) and ROOT/registry.json.
# The main commit holds policy/ (copied from this repository) and content/*.md.
# A second commit on refs/heads/wip is fetchable but not published.
# Commit ids are stable for a given policy/ tree, so golden fixtures assume the
# default ROOT.
set -eu
ROOT=${1:-/tmp/hive-record-fixture}
HERE=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d "${ROOT%/*}/.fixture.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

export GIT_AUTHOR_NAME=fixture GIT_AUTHOR_EMAIL=fixture@example.invalid
export GIT_COMMITTER_NAME=fixture GIT_COMMITTER_EMAIL=fixture@example.invalid
export GIT_AUTHOR_DATE='2026-01-01T00:00:00Z' GIT_COMMITTER_DATE='2026-01-01T00:00:00Z'
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
G="git -c init.defaultBranch=main -c commit.gpgsign=false -c core.autocrlf=false -c core.filemode=true"

W="$TMP/fixture"
$G init -q "$W"
mkdir -p "$W/policy" "$W/content"
(cd "$HERE" && find policy -type f ! -path '*/__pycache__/*' | LC_ALL=C sort) | while read -r f; do
  mkdir -p "$W/$(dirname "$f")"
  cp "$HERE/$f" "$W/$f"
  chmod 644 "$W/$f"
done
for name in order-1 order-2 order-3 order-4 order-5 order-6 \
            result-1 result-2 result-3 result-4 result-5 result-6 \
            review-1 review-2 review-3 review-4 review-5 review-6 \
            question-1 question-2 answer-1 answer-2 report-1 report-2 \
            goal-1 goal-2 summary-1 context-1 diagnosis-1 rationale-1 role-prompt-1; do
  printf '# %s\n\nFixture content for %s.\n' "$name" "$name" > "$W/content/$name.md"
  chmod 644 "$W/content/$name.md"
done
$G -C "$W" add -A
$G -C "$W" commit -q -m "fixture: policy and content"
$G -C "$W" checkout -q -b wip
printf '# unpublished\n' > "$W/content/unpublished.md"
chmod 644 "$W/content/unpublished.md"
$G -C "$W" add -A
$G -C "$W" commit -q -m "fixture: unpublished"
$G -C "$W" checkout -q main
$G clone -q --bare "$W" "$TMP/fixture.git"
$G -C "$TMP/fixture.git" fetch -q "$W" wip:wip

cat > "$TMP/registry.json" <<JSON
{"repositories":{"fixture":{"allowed_ref_patterns":["refs/heads/main"],"fetch_urls":["file://$ROOT/fixture.git"],"local_path":"$ROOT/fixture"}},"version":1}
JSON
# Point the clone's origin at the final location, then swap in atomically.
$G -C "$W" remote add origin "file://$ROOT/fixture.git"
rm -rf "$ROOT.old"
[ -e "$ROOT" ] && mv "$ROOT" "$ROOT.old"
mv "$TMP" "$ROOT"
rm -rf "$ROOT.old"
trap - EXIT
$G -C "$ROOT/fixture" rev-parse main
