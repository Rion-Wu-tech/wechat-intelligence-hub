"""Multi-chat duplicate file detection and APFS hardlink deduplication engine."""

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
    from engine.scanner import ScanCategory
    from engine.cleaner import move_to_trash
except ImportError:
    from .common import Colors, format_bytes, render_progress, _audit_logger
    from .scanner import ScanCategory
    from .cleaner import move_to_trash
@dataclass
class DuplicateGroup:
    """一组内容完全相同的重复文件."""
    file_hash: str
    file_size: int
    files: List[Path]
    saving_bytes: int = 0
    wasted_count: int = 0


def compute_fast_hash(fp: Path, size: int) -> str:
    """快速稀疏哈希: 仅采样头、中、尾生成指纹，大幅加速大文件初筛."""
    chunk = 16384
    hasher = hashlib.md5()
    try:
        with open(fp, 'rb') as f:
            if size <= chunk * 3:
                hasher.update(f.read())
            else:
                hasher.update(f.read(chunk))
                f.seek(size // 2 - chunk // 2)
                hasher.update(f.read(chunk))
                f.seek(size - chunk)
                hasher.update(f.read(chunk))
    except (OSError, PermissionError):
        return ''
    return hasher.hexdigest()


def compute_full_hash(fp: Path, chunk_size: int = 524288) -> str:
    """全量 MD5 计算完整文件校验和 (512KB 缓冲区大幅减少 read 系统调用)."""
    hasher = hashlib.md5()
    try:
        with open(fp, 'rb') as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
    except (OSError, PermissionError):
        return ''
    return hasher.hexdigest()


def find_duplicates(
    categories: Dict[str, ScanCategory],
    selected_types: List[str],
    min_size_bytes: int = 1024,
) -> List[DuplicateGroup]:
    """三级流水线快速查找重复文件 (大小桶分流 -> 稀疏哈希 -> 全量哈希，集成 Inode 缓存与分步剪枝)."""
    # 1. 收集文件并按文件精确大小归类 (大小不同的文件绝不可能是重复文件)
    size_buckets: Dict[int, List[Path]] = defaultdict(list)
    for t in selected_types:
        cat = categories.get(t)
        if not cat or cat.is_protected:
            continue
        for fp, size, _ in cat.files:
            if fp.suffix in ['.db', '.db-wal', '.db-shm', '.sqlite', '.wcdb'] or 'db_storage' in fp.parts:
                continue
            if size > 0 and size >= min_size_bytes:
                size_buckets[size].append(fp)

    # 阶段 1 内存就地剪枝：移除唯一大小文件
    candidate_sizes = [sz for sz, fps in size_buckets.items() if len(fps) > 1]
    if not candidate_sizes:
        return []

    # Inode 缓存：记录 (st_dev, st_ino) -> (fast_hash, full_hash) 消除同 Inode 硬链接的重复磁盘 I/O
    inode_fast_cache: Dict[Tuple[int, int], str] = {}
    inode_full_cache: Dict[Tuple[int, int], str] = {}
    file_stat_cache: Dict[Path, Tuple[Tuple[int, int], float]] = {}

    def get_file_info(p: Path) -> Optional[Tuple[Tuple[int, int], float]]:
        if p in file_stat_cache:
            return file_stat_cache[p]
        try:
            st = p.stat()
            info = ((st.st_dev, st.st_ino), st.st_mtime)
            file_stat_cache[p] = info
            return info
        except OSError:
            return None

    # 2. 仅对存在相同大小的文件进行快速哈希初筛
    fast_hash_buckets: Dict[Tuple[int, str], List[Path]] = defaultdict(list)
    for sz in candidate_sizes:
        fps = size_buckets[sz]
        for fp in fps:
            info = get_file_info(fp)
            if not info:
                continue
            inode_key, _ = info
            if inode_key in inode_fast_cache:
                fh = inode_fast_cache[inode_key]
            else:
                try:
                    fh = compute_fast_hash(fp, sz)
                    if fh:
                        inode_fast_cache[inode_key] = fh
                except (OSError, PermissionError):
                    continue
            if fh:
                fast_hash_buckets[(sz, fh)].append(fp)

    del size_buckets  # 及时释放第一阶段大字典内存

    # 阶段 2 内存就地剪枝：移除快速哈希无碰撞的单例文件
    candidate_fast_keys = [k for k, fps in fast_hash_buckets.items() if len(fps) > 1]
    if not candidate_fast_keys:
        return []

    # 3. 仅对稀疏哈希碰撞的文件进行全量 MD5 确认 (结合 Inode 缓存免除硬链接文件的重复磁盘 I/O)
    full_hash_groups: Dict[str, Tuple[int, List[Path]]] = defaultdict(lambda: (0, []))
    for key in candidate_fast_keys:
        size = key[0]
        fps = fast_hash_buckets[key]
        for fp in fps:
            info = get_file_info(fp)
            if not info:
                continue
            inode_key, _ = info
            if inode_key in inode_full_cache:
                full_h = inode_full_cache[inode_key]
            else:
                try:
                    full_h = compute_full_hash(fp)
                    if full_h:
                        inode_full_cache[inode_key] = full_h
                except (OSError, PermissionError):
                    continue
            if full_h:
                prev_size, prev_list = full_hash_groups[full_h]
                full_hash_groups[full_h] = (size, prev_list + [fp])

    del fast_hash_buckets  # 及时释放第二阶段字典内存

    duplicate_groups: List[DuplicateGroup] = []
    for fhash, (size, fps) in full_hash_groups.items():
        if len(fps) > 1:
            # 检查是否有文件已经互为硬链接 (复用已获取的 inode_key)
            inodes_seen: Set[Tuple[int, int]] = set()
            for fp in fps:
                info = get_file_info(fp)
                if info:
                    inodes_seen.add(info[0])

            if len(inodes_seen) > 1:
                wasted_count = len(inodes_seen) - 1
                saving = wasted_count * size
            else:
                wasted_count = 0
                saving = 0

            # 按修改时间排序，保留最早或最基础的文件为主副本
            fps.sort(key=lambda p: (get_file_info(p)[1] if get_file_info(p) else 0))
            duplicate_groups.append(DuplicateGroup(
                file_hash=fhash,
                file_size=size,
                files=fps,
                saving_bytes=saving,
                wasted_count=wasted_count,
            ))

    # 按可释放空间由大到小排序
    duplicate_groups.sort(key=lambda g: g.saving_bytes, reverse=True)
    return duplicate_groups


def execute_dedup(
    groups: List[DuplicateGroup],
    action: str = 'hardlink',
    dry_run: bool = False,
) -> Tuple[int, int]:
    """执行重复文件去重.
    
    action='hardlink': (推荐) 将重复文件原子替换为系统硬链接，原路径原文件名完全保留，
                       微信内各群聊仍可正常读取，但在 macOS APFS 物理磁盘仅占一份空间！
    action='trash':    将冗余副本直接移至 macOS 废纸篓。
    """
    processed_count = 0
    freed_bytes = 0
    total_copies = sum(len(grp.files) - 1 for grp in groups if grp.wasted_count > 0 and len(grp.files) >= 2)

    for grp in groups:
        if grp.wasted_count == 0 or len(grp.files) < 2:
            continue
        primary = grp.files[0]
        try:
            prim_st = primary.stat()
            prim_ino_key = (prim_st.st_dev, prim_st.st_ino)
        except OSError:
            continue

        for dup in grp.files[1:]:
            try:
                dup_st = dup.stat()
                if (dup_st.st_dev, dup_st.st_ino) == prim_ino_key:
                    continue
            except OSError:
                continue

            processed_count += 1
            freed_bytes += grp.file_size

            if not dry_run and total_copies > 10 and processed_count % 5 == 0:
                render_progress(processed_count, total_copies, prefix="正在去重处理")

            if dry_run:
                continue

            if action == 'hardlink':
                try:
                    # 使用临时硬链接原子替换，确保过程安全
                    tmp_link = dup.with_name(f".tmp_link_{os.getpid()}_{dup.name}")
                    os.link(primary, tmp_link)
                    os.replace(tmp_link, dup)
                except Exception:
                    continue
            elif action == 'trash':
                move_to_trash(dup)

    if not dry_run and total_copies > 10:
        render_progress(total_copies, total_copies, prefix="正在去重处理")

    return processed_count, freed_bytes


