#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${WECHAT_HUB_HOME:-}"
PYTHON_BIN="${PYTHON_BIN:-}"
LOCAL_HOME_FILE="${CODEX_HOME:-$HOME/.codex}/config/wechat-hub-home"

hub_entry() {
  if [ -f "$1/wechat_intelligence_hub.py" ]; then
    echo wechat_intelligence_hub.py
  elif [ -f "$1/wechat_deal_radar.py" ]; then
    echo wechat_deal_radar.py
  fi
}

if [ -z "$PROJECT_DIR" ] && [ -f "$LOCAL_HOME_FILE" ]; then
  IFS= read -r PROJECT_DIR < "$LOCAL_HOME_FILE" || true
  if [ -n "$PROJECT_DIR" ] && [ -z "$(hub_entry "$PROJECT_DIR")" ]; then
    echo "提示：$LOCAL_HOME_FILE 指向的 $PROJECT_DIR 已失效，改为自动查找；找到后请更新该文件。" >&2
    PROJECT_DIR=""
  fi
fi

if [ -z "$PROJECT_DIR" ]; then
  for candidate in \
    "$PWD" \
    "$SCRIPT_DIR/../../../../projects/wechat-intelligence-hub" \
    "$SCRIPT_DIR/../../../share/wechat-intelligence-hub/projects/wechat-intelligence-hub" \
    "$HOME/wechat-intelligence-hub" \
    "$HOME/Documents/wechat-intelligence-hub"
  do
    if [ -n "$(hub_entry "$candidate")" ]; then
      PROJECT_DIR="$candidate"
      break
    fi
  done
fi

if [ -z "$PROJECT_DIR" ] && [ -d "$HOME/Documents" ]; then
  project_file="$(find "$HOME/Documents" -maxdepth 5 -type f -name wechat_intelligence_hub.py -path '*wechat-intelligence-hub*' -print -quit 2>/dev/null || true)"
  if [ -n "$project_file" ]; then
    PROJECT_DIR="$(dirname "$project_file")"
  fi
fi

HUB_ENTRY="$(hub_entry "${PROJECT_DIR:-/nonexistent}")"
if [ -z "$PROJECT_DIR" ] || [ -z "$HUB_ENTRY" ]; then
  echo "找不到 WeChat Intelligence Hub 项目。请设置 WECHAT_HUB_HOME=/path/to/wechat-intelligence-hub" >&2
  exit 2
fi

cd "$PROJECT_DIR"
if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
  if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
  fi
fi
if [ "${1:-}" = "--runtime-info" ]; then
  exec "$PYTHON_BIN" -c 'import json, pathlib, sys, importlib; hub = importlib.import_module(sys.argv[1]); print(json.dumps({"project_dir": str(pathlib.Path(hub.__file__).resolve().parent), "entry": sys.argv[1] + ".py", "reader_path": getattr(hub, "DEFAULT_WECHAT_READER", None)}, ensure_ascii=False, indent=2))' "${HUB_ENTRY%.py}"
fi
if [ "${1:-}" = "reply-draft" ]; then
  shift
  exec "$PYTHON_BIN" "$PROJECT_DIR/reply_drafts.py" "$@"
fi
exec "$PYTHON_BIN" "$HUB_ENTRY" "$@"
