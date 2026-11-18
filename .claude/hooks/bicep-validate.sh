#!/usr/bin/env bash
# PostToolUse hook: compile + lint any .bicep file Claude just edited/wrote.
# Claude Code passes the hook payload as JSON on stdin; exit 2 feeds stderr back to Claude.
set -uo pipefail

file=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))')

[[ "$file" == *.bicep ]] || exit 0

if ! out=$({ bicep build "$file" --stdout >/dev/null && bicep lint "$file"; } 2>&1); then
  printf 'Bicep validation failed for %s:\n%s\n' "$file" "$out" >&2
  exit 2
fi
exit 0
