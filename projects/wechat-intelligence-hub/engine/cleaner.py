"""WeChat file slimming, safe trash movement, and archive engine."""

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
    from engine.common import Colors, format_bytes, render_progress, _audit_logger
    from engine.scanner import AccountProfile, ScanCategory
    from engine.whitelist import WhiteListManager
except ImportError:
    from .common import Colors, format_bytes, render_progress, _audit_logger
    from .scanner import AccountProfile, ScanCategory
    from .whitelist import WhiteListManager
def move_to_trash(file_path: Path) -> bool:
    """安全将文件移入 macOS 废纸篓 (可通过访达随时放回原处，支持转义与降级兜底)."""
    try:
        resolved = str(file_path.resolve())
        safe_path = resolved.replace('\\', '\\\\').replace('"', '\\"')
        cmd = ['osascript', '-e', f'tell application "Finder" to delete POSIX file "{safe_path}"']
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return True
    except Exception:
        pass

    try:
        trash_dir = Path.home() / ".Trash"
        if trash_dir.is_dir():
            target = trash_dir / file_path.name
            if target.exists():
                target = trash_dir / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"
            shutil.move(str(file_path), str(target))
            return True
    except Exception:
        pass
    return False


@dataclass
class SlimResult:
    """瘦身执行统计结果 (支持解构赋值 (freed_count, freed_bytes) 保持向下兼容)."""
    freed_count: int
    freed_bytes: int
    protected_count: int = 0
    protected_bytes: int = 0

    def __iter__(self):
        return iter((self.freed_count, self.freed_bytes))


def execute_slimming(
    acc: AccountProfile,
    categories: Dict[str, ScanCategory],
    days: int,
    min_size_bytes: int,
    selected_types: List[str],
    dry_run: bool = False,
    archive_to: Optional[Path] = None,
    whitelist_mgr: Optional[WhiteListManager] = None,
) -> SlimResult:
    """执行瘦身与清理操作 (集成核心人脉防删白名单检查).
    
    返回: SlimResult (可解构为 (清理文件数, 释放字节数))
    """
    cutoff_time = datetime.now() - timedelta(days=days) if days > 0 else datetime.now() + timedelta(days=99999)
    cutoff_ts = cutoff_time.timestamp()

    freed_bytes = 0
    freed_count = 0
    protected_bytes = 0
    protected_count = 0

    if archive_to:
        archive_to = archive_to.resolve()
        if not dry_run:
            archive_to.mkdir(parents=True, exist_ok=True)

    total_target_files = sum(len(c.files) for k, c in categories.items() if k in selected_types and not c.is_protected)
    cur_idx = 0

    for type_key in selected_types:
        cat = categories.get(type_key)
        if not cat or cat.is_protected:
            continue

        for fp, size, mtime in cat.files:
            cur_idx += 1
            if not dry_run and total_target_files > 50 and cur_idx % 20 == 0:
                render_progress(cur_idx, total_target_files, prefix="正在瘦身处理")

            # 绝对安全护栏 1：绝不处理数据库文件
            if fp.suffix in ['.db', '.db-wal', '.db-shm', '.sqlite', '.wcdb'] or 'db_storage' in fp.parts:
                continue

            # 过滤条件 1: 文件大小阈值
            if size < min_size_bytes:
                continue

            # 过滤条件 2: 时间跨度 (mtime 必须早于截断时间)
            if days > 0 and mtime > cutoff_ts:
                continue

            # 绝对安全护栏 2 (Phase 2): 核心人脉防删白名单检查
            if whitelist_mgr:
                is_prot, _ = whitelist_mgr.is_protected(fp, mtime)
                if is_prot:
                    protected_count += 1
                    protected_bytes += size
                    continue

            # 命中待处理文件
            freed_count += 1
            freed_bytes += size

            if dry_run:
                continue

            try:
                if archive_to:
                    # 归档模式：计算相对路径并安全移动到外置目录
                    try:
                        rel_path = fp.relative_to(acc.root_path)
                    except ValueError:
                        rel_path = Path(cat.name) / fp.name
                    dest_path = archive_to / rel_path
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(fp), str(dest_path))
                else:
                    # 默认安全清理：移至 macOS 废纸篓
                    move_to_trash(fp)
            except (OSError, PermissionError, shutil.Error) as e:
                _audit_logger.error(f"Failed to process file {fp}: {e}")
                continue

    if not dry_run and total_target_files > 50:
        render_progress(total_target_files, total_target_files, prefix="正在瘦身处理")

    return SlimResult(freed_count, freed_bytes, protected_count, protected_bytes)


