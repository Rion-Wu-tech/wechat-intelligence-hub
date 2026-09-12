"""WeChat storage scanning engine and account discovery."""

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
    from engine.common import format_bytes, render_progress
except ImportError:
    from .common import format_bytes, render_progress
@dataclass
class AccountProfile:
    """微信账号存储路径描述."""
    account_id: str
    version_type: str  # 'v4' or 'v3'
    root_path: Path
    db_path: Optional[Path] = None
    msg_video_path: Optional[Path] = None
    msg_file_path: Optional[Path] = None
    msg_attach_path: Optional[Path] = None
    cache_path: Optional[Path] = None
    temp_path: Optional[Path] = None


@dataclass
class ScanCategory:
    """某一类文件的空间统计."""
    name: str
    description: str
    path: Path
    is_protected: bool = False
    file_count: int = 0
    total_bytes: int = 0
    files: List[Tuple[Path, int, float]] = field(default_factory=list)  # (path, size, mtime)


def discover_accounts(custom_path: Optional[Path] = None) -> List[AccountProfile]:
    """自动发现或指定当前 Mac 上的微信存储账号路径."""
    if custom_path:
        cp = Path(custom_path).resolve()
        if cp.is_dir():
            if (cp / 'db_storage').exists() or (cp / 'msg').exists() or (cp / 'cache').exists():
                return [
                    AccountProfile(
                        account_id=cp.name,
                        version_type='custom (自定义目录)',
                        root_path=cp,
                        db_path=cp / 'db_storage' if (cp / 'db_storage').exists() else None,
                        msg_video_path=cp / 'msg/video' if (cp / 'msg/video').exists() else None,
                        msg_file_path=cp / 'msg/file' if (cp / 'msg/file').exists() else None,
                        msg_attach_path=cp / 'msg/attach' if (cp / 'msg/attach').exists() else None,
                        cache_path=cp / 'cache' if (cp / 'cache').exists() else None,
                        temp_path=cp / 'temp' if (cp / 'temp').exists() else None,
                    )
                ]
            accs = []
            for sub in cp.iterdir():
                if sub.is_dir() and not sub.name.startswith('.'):
                    accs.append(AccountProfile(
                        account_id=sub.name,
                        version_type='custom (自定义目录)',
                        root_path=sub,
                        db_path=sub / 'db_storage' if (sub / 'db_storage').exists() else None,
                        msg_video_path=sub / 'msg/video' if (sub / 'msg/video').exists() else None,
                        msg_file_path=sub / 'msg/file' if (sub / 'msg/file').exists() else None,
                        msg_attach_path=sub / 'msg/attach' if (sub / 'msg/attach').exists() else None,
                        cache_path=sub / 'cache' if (sub / 'cache').exists() else None,
                        temp_path=sub / 'temp' if (sub / 'temp').exists() else None,
                    ))
            if accs:
                return accs
        return []

    accounts: List[AccountProfile] = []
    home = Path.home()

    # 1. 微信 4.0+ 路径: ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/
    v4_base = home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
    if v4_base.is_dir():
        for item in v4_base.iterdir():
            if item.is_dir() and item.name not in ['all_users', 'Backup'] and not item.name.startswith('.'):
                acc = AccountProfile(
                    account_id=item.name,
                    version_type='v4 (微信 4.0+)',
                    root_path=item,
                    db_path=item / 'db_storage' if (item / 'db_storage').exists() else None,
                    msg_video_path=item / 'msg/video' if (item / 'msg/video').exists() else None,
                    msg_file_path=item / 'msg/file' if (item / 'msg/file').exists() else None,
                    msg_attach_path=item / 'msg/attach' if (item / 'msg/attach').exists() else None,
                    cache_path=item / 'cache' if (item / 'cache').exists() else None,
                    temp_path=item / 'temp' if (item / 'temp').exists() else None,
                )
                accounts.append(acc)

    # 2. 微信 3.x 传统路径: ~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/
    v3_base = home / 'Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat'
    if v3_base.is_dir():
        for ver in v3_base.iterdir():
            if ver.is_dir() and not ver.name.startswith('.'):
                for acc_dir in ver.iterdir():
                    if acc_dir.is_dir() and len(acc_dir.name) == 32 and not acc_dir.name.startswith('.'):
                        acc = AccountProfile(
                            account_id=acc_dir.name[:8] + '...',
                            version_type=f'v3 ({ver.name})',
                            root_path=acc_dir,
                            msg_attach_path=acc_dir / 'Message/MessageTemp' if (acc_dir / 'Message/MessageTemp').exists() else None,
                            cache_path=acc_dir / 'Caches' if (acc_dir / 'Caches').exists() else None,
                        )
                        accounts.append(acc)

    return accounts


def scan_directory(
    category_name: str,
    desc: str,
    dir_path: Optional[Path],
    is_protected: bool = False,
    collect_files: bool = True,
) -> ScanCategory:
    """递归统计指定目录下的文件数量与总大小 (基于 os.scandir 复用 DirEntry 元数据，受保护目录零内存开销)."""
    cat = ScanCategory(name=category_name, description=desc, path=dir_path or Path('/dev/null'), is_protected=is_protected)
    if not dir_path or not dir_path.exists():
        return cat

    stack = [str(dir_path)]
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            cat.file_count += 1
                            cat.total_bytes += st.st_size
                            if collect_files and not is_protected:
                                cat.files.append((Path(entry.path), st.st_size, st.st_mtime))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue

    return cat


def scan_account(acc: AccountProfile, collect_files: bool = True) -> Dict[str, ScanCategory]:
    """对单个账号执行全量存储透视扫描."""
    results: Dict[str, ScanCategory] = {}

    # 1. 核心数据库 (必须保护，零内存缓冲)
    results['db'] = scan_directory('db_storage', '核心聊天数据库与文字索引 [🔒 绝对保护，禁止删除]', acc.db_path, is_protected=True, collect_files=collect_files)

    # 2. 视频缓存
    results['video'] = scan_directory('video', '接收与缓存的视频文件 (msg/video)', acc.msg_video_path, collect_files=collect_files)

    # 3. 接收文件
    results['file'] = scan_directory('file', '接收的文档与办公文件 (msg/file)', acc.msg_file_path, collect_files=collect_files)

    # 4. 聊天图片与多媒体附件
    results['attach'] = scan_directory('attach', '聊天图片、表情与多媒体附件 (msg/attach)', acc.msg_attach_path, collect_files=collect_files)

    # 5. 缓存与临时文件
    cache_files: List[Tuple[Path, int, float]] = []
    total_cache_size = 0
    total_cache_count = 0
    for p in [acc.cache_path, acc.temp_path]:
        if p and p.exists():
            c = scan_directory('cache_raw', '', p, collect_files=collect_files)
            if collect_files:
                cache_files.extend(c.files)
            total_cache_size += c.total_bytes
            total_cache_count += c.file_count

    results['cache'] = ScanCategory(
        name='cache',
        description='运行临时缓存与缩略图 (cache/temp) [可安全清理]',
        path=acc.cache_path or acc.root_path,
        file_count=total_cache_count,
        total_bytes=total_cache_size,
        files=cache_files,
    )

    return results
