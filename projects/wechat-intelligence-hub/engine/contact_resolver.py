"""WeChat Contact Nickname Resolver.

Safely and opportunistically inspects WeChat contact/session SQLite databases
in STRICT READ-ONLY mode to resolve wxids to human-readable nicknames and remarks.
100% database safety guaranteed: never writes, always uses read-only mode or temporary snapshots.
High performance: uses direct mode=ro URI and single-pass table caching.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Dict, List, Optional, Set


@dataclass
class ContactInfo:
    wxid: str
    nickname: str = ""
    remark: str = ""

    @property
    def display_name(self) -> str:
        """Returns the best display name: remark if available, else nickname, else wxid."""
        if self.remark:
            return self.remark
        if self.nickname:
            return self.nickname
        return self.wxid

    def to_formatted_str(self) -> str:
        parts = []
        if self.remark:
            parts.append(f"备注: {self.remark}")
        if self.nickname and self.nickname != self.remark:
            parts.append(f"昵称: {self.nickname}")
        extra = f" ({', '.join(parts)})" if parts else ""
        return f"{self.display_name}{extra}"


class ContactResolver:
    """Safe, high-performance read-only contact name resolver for WeChat on macOS."""

    CANDIDATE_DB_NAMES = [
        "contact.db",
        "contact_sub.db",
        "WCDB_Contact.sqlite",
        "session.db",
        "Session.db",
        "message.db",
    ]

    def __init__(self, root_or_db_path: Optional[Path] = None):
        self._cache: Dict[str, ContactInfo] = {}
        self._db_paths: List[Path] = []
        self._dbs_scanned: Set[Path] = set()
        if root_or_db_path:
            self._discover_dbs(root_or_db_path)

    def _discover_dbs(self, path: Path) -> None:
        if path.is_file():
            self._db_paths.append(path)
            return

        # 微信 4.0 真实结构: db_storage/ 下是分类子目录，库文件在子目录里：
        #   db_storage/contact/contact.db
        #   db_storage/session/session.db
        #   db_storage/message/message_N.db
        # 因此必须递归下钻，只在 db_storage 根目录下找 contact.db 是找不到的。
        db_storage = path / "db_storage"
        target_dirs = [db_storage, path] if db_storage.is_dir() else [path]

        for target_dir in target_dirs:
            for candidate in self._iter_candidate_dbs(target_dir):
                if candidate not in self._db_paths:
                    self._db_paths.append(candidate)

    def _iter_candidate_dbs(self, root: Path):
        """递归枚举候选联系人/会话库（深度受限，避免全盘遍历）。"""
        max_depth = 3
        for dirpath, dirnames, filenames in os.walk(root):
            depth = str(dirpath).count(os.sep) - str(root).count(os.sep)
            if depth >= max_depth:
                dirnames[:] = []
            # 跳过明显的非会话库目录，减少无谓 IO
            dirnames[:] = [d for d in dirnames if d not in ("MMKV", "head_image", "emoticon", "sns")]
            for name in filenames:
                if not name.endswith(".db"):
                    continue
                if name.endswith(("-wal", "-shm")) or ".db-" in name:
                    continue
                low = name.lower()
                # 只保留可能含联系人/会话映射的库
                if any(key in low for key in ("contact", "session", "message", "friend", "group")):
                    yield Path(dirpath) / name

    def resolve(self, wxid: str) -> Optional[ContactInfo]:
        """Resolves a single wxid to ContactInfo."""
        if not wxid:
            return None
        if wxid in self._cache:
            return self._cache[wxid]

        # Scan unscanned candidate databases
        for db_path in self._db_paths:
            if db_path not in self._dbs_scanned:
                self._scan_db(db_path)
                if wxid in self._cache:
                    return self._cache[wxid]

        return None

    def batch_resolve(self, wxids: List[str]) -> Dict[str, ContactInfo]:
        """Resolves a list of wxids."""
        result: Dict[str, ContactInfo] = {}
        for wxid in wxids:
            info = self.resolve(wxid)
            if info:
                result[wxid] = info
        return result

    def get_display_name(self, wxid: str, fallback_name: str = "") -> str:
        """Helper to get best display name with optional fallback."""
        info = self.resolve(wxid)
        if info and info.display_name:
            return info.display_name
        return fallback_name or wxid

    def _scan_db(self, db_path: Path) -> None:
        """Extracts contact mappings from a database in one safe pass."""
        self._dbs_scanned.add(db_path)

        conn = None
        temp_dir = None
        try:
            # 1. First attempt direct connection in strict read-only mode (fastest, zero disk copy)
            try:
                uri_path = f"file:{db_path.resolve()}?mode=ro"
                conn = sqlite3.connect(uri_path, uri=True, timeout=1.0)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                # 2. Fallback: if database is locked by another process or requires snapshot
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                temp_dir = tempfile.mkdtemp(prefix="wechat_slim_ro_")
                temp_db = Path(temp_dir) / db_path.name
                shutil.copy2(db_path, temp_db)

                wal = db_path.with_name(db_path.name + "-wal")
                shm = db_path.with_name(db_path.name + "-shm")
                if wal.exists():
                    try:
                        shutil.copy2(wal, Path(temp_dir) / wal.name)
                    except Exception:
                        pass
                if shm.exists():
                    try:
                        shutil.copy2(shm, Path(temp_dir) / shm.name)
                    except Exception:
                        pass

                uri_path = f"file:{temp_db}?mode=ro"
                conn = sqlite3.connect(uri_path, uri=True, timeout=1.0)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]

            for table in tables:
                try:
                    cursor.execute(f"PRAGMA table_info({table})")
                    cols = [c[1] for c in cursor.fetchall()]
                    cols_lower = [c.lower() for c in cols]

                    # Find wxid column
                    wxid_col = None
                    for candidate in ["m_nsusrname", "username", "wxid", "user_name"]:
                        if candidate in cols_lower:
                            wxid_col = cols[cols_lower.index(candidate)]
                            break
                    if not wxid_col:
                        continue

                    # Find remark column
                    remark_col = None
                    for candidate in ["m_nsremark", "remark", "conremark"]:
                        if candidate in cols_lower:
                            remark_col = cols[cols_lower.index(candidate)]
                            break

                    # Find nickname column
                    nick_col = None
                    for candidate in ["m_nsnickname", "nickname", "connickname"]:
                        if candidate in cols_lower:
                            nick_col = cols[cols_lower.index(candidate)]
                            break

                    query_cols = [wxid_col]
                    if remark_col:
                        query_cols.append(remark_col)
                    if nick_col:
                        query_cols.append(nick_col)

                    cursor.execute(f"SELECT {', '.join(query_cols)} FROM {table}")
                    for row in cursor.fetchall():
                        w = str(row[0] or "").strip()
                        if not w:
                            continue
                        r = str(row[1] or "").strip() if remark_col and len(row) > 1 else ""
                        n = str(row[-1] or "").strip() if nick_col and len(row) > (2 if remark_col else 1) else ""

                        # If already seen with better data, keep the better one
                        if w in self._cache:
                            existing = self._cache[w]
                            if not existing.remark and r:
                                existing.remark = r
                            if not existing.nickname and n:
                                existing.nickname = n
                        else:
                            self._cache[w] = ContactInfo(wxid=w, remark=r, nickname=n)

                except (sqlite3.Error, OSError):
                    continue

        except (sqlite3.DatabaseError, OSError, PermissionError):
            # Encrypted database (WCDB/SQLCipher) or unreadable: fail safely
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
