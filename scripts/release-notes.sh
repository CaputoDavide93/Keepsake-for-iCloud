#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: scripts/release-notes.sh vX.Y.Z" >&2
  exit 1
fi

tag="$1"
if [[ ! "$tag" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
  echo "Release tag must have the form vX.Y.Z." >&2
  exit 1
fi

version="${BASH_REMATCH[1]}"
changelog="${KEEPSAKE_CHANGELOG:-CHANGELOG.md}"
if [[ ! -f "$changelog" ]]; then
  echo "Changelog file not found: $changelog." >&2
  exit 1
fi

if notes="$(awk -v heading="## [$version]" '
  $0 == heading || index($0, heading " - ") == 1 {
    found = 1
    next
  }
  found && /^## / {
    exit
  }
  found {
    lines[++count] = $0
  }
  END {
    if (!found) {
      exit 2
    }
    first = 1
    while (first <= count && lines[first] ~ /^[[:space:]]*$/) {
      first++
    }
    last = count
    while (last >= first && lines[last] ~ /^[[:space:]]*$/) {
      last--
    }
    if (first > last) {
      exit 3
    }
    for (line = first; line <= last; line++) {
      print lines[line]
    }
  }
' "$changelog")"; then
  printf '%s\n' "$notes"
else
  status=$?
  if [[ $status -eq 2 ]]; then
    echo "Release notes for $tag are missing from $changelog." >&2
  elif [[ $status -eq 3 ]]; then
    echo "Release notes for $tag are empty in $changelog." >&2
  else
    echo "Release notes for $tag could not be read from $changelog." >&2
  fi
  exit 1
fi
