"""Common utilities, formatting, logging, and ANSI colors."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import urllib.parse
import webbrowser

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
    HAS_RICH = sys.stdout.isatty() and not bool(os.environ.get("NO_COLOR"))
    _console = Console() if HAS_RICH else None
except ImportError:
    HAS_RICH = False
    _console = None



class Colors:
    """零依赖 ANSI 彩色终端输出."""
    USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    RESET = "\033[0m" if USE_COLOR else ""
    BOLD = "\033[1m" if USE_COLOR else ""
    GREEN = "\033[32m" if USE_COLOR else ""
    BLUE = "\033[34m" if USE_COLOR else ""
    YELLOW = "\033[33m" if USE_COLOR else ""
    RED = "\033[31m" if USE_COLOR else ""
    CYAN = "\033[36m" if USE_COLOR else ""
    GRAY = "\033[90m" if USE_COLOR else ""
    MAGENTA = "\033[35m" if USE_COLOR else ""


def setup_logger(log_file: Optional[Path] = None) -> logging.Logger:
    """初始化审计日志系统，记录到 ~/.wechat_slim/audit.log."""
    logger = logging.getLogger("wechat_slim")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        if not log_file:
            log_dir = Path.home() / ".wechat_slim"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "audit.log"
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.INFO)
            formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        except Exception:
            pass
    return logger


_audit_logger = setup_logger()


def render_progress(current: int, total: int, prefix: str = "", bar_len: int = 25) -> None:
    """平滑终端字符动态进度条."""
    if not sys.stdout.isatty() or total <= 0:
        return
    pct = min(1.0, current / total)
    filled = int(bar_len * pct)
    bar = "█" * filled + "░" * (bar_len - filled)
    sys.stdout.write(f"\r  {prefix} [{bar}] {pct*100:5.1f}% ({current:,}/{total:,})")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def format_bytes(size: float) -> str:
    """格式化字节大小为人类可读字符串."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0 or unit == 'TB':
            return f'{size:.1f} {unit}'
        size /= 1024.0
    return f'{size:.1f} B'


def parse_size_str(size_str: str) -> int:
    """解析大小字符串 (如 10MB, 500KB, 1GB) 为字节数."""
    s = size_str.strip().upper()
    if not s:
        return 0
    multipliers = {
        'B': 1,
        'K': 1024,
        'KB': 1024,
        'M': 1024 * 1024,
        'MB': 1024 * 1024,
        'G': 1024 * 1024 * 1024,
        'GB': 1024 * 1024 * 1024,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            num = s[: -len(suffix)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                break
    try:
        return int(s)
    except ValueError:
        raise ValueError(f'无法识别的大小格式: {size_str} (例如: 10MB, 500KB)')


audit_logger = _audit_logger
AuditLogger = _audit_logger
