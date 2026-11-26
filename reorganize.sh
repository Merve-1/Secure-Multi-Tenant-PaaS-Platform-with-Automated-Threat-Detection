#!/usr/bin/env bash
# One-time repository reorganisation.
#
# Run it from the repository root (the folder that contains .git):
#   bash reorganize.sh            # dry run: only prints what it WOULD do
#   bash reorganize.sh --apply    # does it
#
# It moves files with "git mv" so history is kept, skips anything that is
# missing or whose target already exists, and never touches AWS.
set -uo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

if [ ! -d .git ]; then
  echo "Run this from the repository root (the folder that contains .git)."
  exit 1
fi
if [ "$APPLY" = 1 ] && [ -n "$(git status --porcelain)" ]; then
  echo "Commit or stash your changes first (git status is not clean)."
  exit 1
fi

move() {  # move SOURCE DESTINATION
  [ -e "$1" ] || return 0
  if [ -e "$2" ]; then echo "skip    $2 (already exists)"; return 0; fi
  echo "move    $1  ->  $2"
  if [ "$APPLY" = 1 ]; then
    mkdir -p "$(dirname "$2")"
    git mv "$1" "$2" 2>/dev/null || mv "$1" "$2"
  fi
}

remove() {  # remove PATH (generated files only)
  [ -e "$1" ] || return 0
  echo "delete  $1"
  if [ "$APPLY" = 1 ]; then rm -rf "$1"; fi
}

ignore() {  # ignore PATTERN (add to .gitignore if missing)
  if [ -f .gitignore ] && grep -qxF "$1" .gitignore; then return 0; fi
  echo "ignore  $1"
  if [ "$APPLY" = 1 ]; then echo "$1" >> .gitignore; fi
}

echo "== 1. Move the SAM project up to the repository root =="
move sam-fastapi/template.yaml   template.yaml
move sam-fastapi/samconfig.toml  samconfig.toml
move sam-fastapi/application     application
move sam-fastapi/frontend        frontend
move sam-fastapi/scripts         scripts
move sam-fastapi/tests           tests
move sam-fastapi/.gitignore      .gitignore
move sam-fastapi/README.md       docs/sam-readme.md

echo
echo "== 2. Remove generated files and boilerplate =="
remove sam-fastapi/.venv
remove sam-fastapi/.aws-sam
remove sam-fastapi/events
if [ -e sam-fastapi/__init__.py ] && [ ! -s sam-fastapi/__init__.py ]; then
  remove sam-fastapi/__init__.py
fi

echo
echo "== 3. End-to-end tests live in tests/e2e =="
move scripts/test-crud.sh  tests/e2e/test-crud.sh
move scripts/test-orgs.sh  tests/e2e/test-orgs.sh

echo
echo "== 4. Documentation =="
move "step1&step2-notes.md"  docs/build-log/step-01-02-notes.md
move "step3&step4-notes.md"  docs/build-log/step-03-04-notes.md
if [ -d explainationImages ]; then
  for f in explainationImages/*; do
    move "$f" "docs/images/$(basename "$f")"
  done
fi
if [ "$APPLY" = 1 ]; then
  mkdir -p docs/architecture docs/security docs/cost
  for d in docs/architecture docs/security docs/cost; do
    [ -e "$d/.gitkeep" ] || touch "$d/.gitkeep"
  done
  # Image links in the notes now point one folder over.
  for n in docs/build-log/*.md; do
    [ -f "$n" ] && sed -i 's#explainationImages/#../images/#g' "$n"
  done
  rmdir explainationImages sam-fastapi 2>/dev/null || true
fi

echo
echo "== 5. .gitignore =="
for p in ".venv/" ".aws-sam/" "tokens.json" "*.bak" ".env" "__pycache__/" \
         ".pytest_cache/" "frontend/config.json"; do
  ignore "$p"
done

echo
if [ "$APPLY" = 1 ]; then
  echo "Done. Next steps:"
  echo "  python3.12 -m venv .venv && source .venv/bin/activate"
  echo "  pip install -r application/requirements.txt"
  echo "  sam validate --lint && sam build"
  echo "  git status        # review, then commit"
else
  echo "Dry run only. Nothing was changed. Run: bash reorganize.sh --apply"
fi