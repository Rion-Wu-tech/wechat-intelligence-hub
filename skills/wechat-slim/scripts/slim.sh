#!/usr/bin/env bash
set -euo pipefail

repo_candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/wechat_slim.py"
if [ -f "$repo_candidate" ]; then
  exec python3 "$repo_candidate" "$@"
fi

if command -v wechat_slim.py >/dev/null 2>&1; then
  exec wechat_slim.py "$@"
fi

echo "wechat_slim.py not found." >&2
exit 127
