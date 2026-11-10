#!/bin/sh
# Arm the local hooks: the scrub on every commit, and a guard against trailers
# that credit a tool or session in commit messages.
set -eu
root=$(git rev-parse --show-toplevel)
hooks="$root/.git/hooks"
cat > "$hooks/pre-commit" <<'HOOK'
#!/bin/sh
exec python3 tools/scrub.py --staged
HOOK
cat > "$hooks/commit-msg" <<'HOOK'
#!/bin/sh
if grep -qiE '^(co-authored-by|generated (with|by))|session[ _-]?id' "$1"; then
  echo "commit-msg: no co-author trailers or generated-by lines in this repository" >&2
  exit 1
fi
HOOK
chmod +x "$hooks/pre-commit" "$hooks/commit-msg"
echo "hooks armed in $hooks"
