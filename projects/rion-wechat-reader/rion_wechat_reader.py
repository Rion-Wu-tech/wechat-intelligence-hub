#!/usr/bin/env python3
"""Clean-room, read-only WeChat reader contract.

The reader never acquires database keys and never mutates WeChat. It can query
user-authorized SQLite/WCDB inputs and expose macOS notification previews as an
explicitly incomplete fallback.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import contextlib
import datetime as dt
import hashlib
import html
import json
import os
import platform
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.9.2-preview.2"
DEFAULT_CONFIG = Path("~/.config/rion-wechat-reader/config.json").expanduser()
DEFAULT_NOTIFICATIONS_DB = Path(
    "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
).expanduser()
WECHAT_BUNDLE_ID = "com.tencent.xinwechat"
DEFAULT_WECHAT_ROOTS = [
    Path("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files").expanduser(),
]

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "access-plan": {"aliases": [], "properties": ["database_root", "keys_file", "max_files"]},
    "sessions": {"aliases": [], "properties": ["keyword", "limit", "type_filter"]},
    "contacts": {"aliases": [], "properties": ["friends_only", "groups_only", "keyword", "limit"]},
    "resolve-chat": {"aliases": ["resolve_chat"], "properties": ["chat", "keyword", "limit", "query", "type_filter"]},
    "timeline": {"aliases": ["chat-timeline", "chat_timeline"], "properties": ["after", "after_local_id", "after_message", "after_message_id", "after_message_local_id", "after_message_server_id", "after_message_server_id_str", "after_server_id", "after_server_id_str", "base_kind", "before", "before_local_id", "before_message", "before_message_id", "before_message_local_id", "before_message_server_id", "before_message_server_id_str", "before_server_id", "before_server_id_str", "chat", "debug", "display_order", "from_me", "include_debug", "include_images", "include_media_paths", "keyword", "kind_name", "limit", "offset", "order", "sender", "since", "since_local_id", "since_message", "since_time", "talker", "type"]},
    "history": {"aliases": ["messages"], "properties": ["after", "after_local_id", "after_message", "after_message_id", "after_message_local_id", "after_message_server_id", "after_message_server_id_str", "after_server_id", "after_server_id_str", "base_kind", "before", "before_local_id", "before_message", "before_message_id", "before_message_local_id", "before_message_server_id", "before_message_server_id_str", "before_server_id", "before_server_id_str", "chat", "debug", "display_order", "fields", "from_me", "include_debug", "include_media_paths", "keyword", "kind_name", "limit", "offset", "order", "sender", "since", "since_local_id", "since_message", "since_time", "talker", "type", "view"]},
    "context": {"aliases": ["message-context", "message_context"], "properties": ["after_count", "after_messages", "around_local_id", "around_server_id", "around_server_id_str", "before_count", "before_messages", "chat", "debug", "display_order", "include_anchor", "include_debug", "include_media_paths", "limit", "local_id", "message_local_id", "message_server_id", "message_server_id_str", "server_id", "server_id_str", "talker"]},
    "search": {"aliases": [], "properties": ["after", "base_kind", "before", "chat", "from_me", "include_text", "keyword", "kind_name", "limit", "max_text_chars", "offset", "search_mode", "sender", "snippet_only", "talker", "type"]},
    "search-context": {"aliases": ["search_context", "search-with-context", "search_with_context"], "properties": ["after", "after_count", "after_messages", "base_kind", "before", "before_count", "before_messages", "chat", "context_limit", "debug", "from_me", "include_debug", "include_media_paths", "include_text", "keyword", "kind_name", "limit", "max_text_chars", "search_mode", "sender", "snippet_only", "talker", "type"]},
    "tail": {"aliases": ["watch", "observe", "events", "read-events", "read_events"], "properties": ["after", "chat", "cursor", "debug", "follow", "from_me", "include_debug", "include_media_paths", "jsonl", "kind_name", "limit", "mode", "poll_interval", "scan_limit", "sender", "since", "since_local_id", "since_time", "talker", "type"]},
    "unread": {"aliases": [], "properties": ["filter", "limit", "type_filter"]},
    "stats": {"aliases": [], "properties": []},
    "members": {"aliases": ["group-members", "group_members"], "properties": ["chat", "chatroom_id", "limit", "offset", "stats"]},
    "schema": {"aliases": [], "properties": ["file", "subdir"]},
    "agent": {"aliases": ["read-os", "read_os", "os"], "properties": ["debug", "include_debug", "include_status", "mode"]},
    "cache-status": {"aliases": ["cache_status"], "properties": []},
    "cache-refresh": {"aliases": ["cache_refresh"], "properties": ["background", "force"]},
    "cache-rebuild": {"aliases": ["cache_rebuild"], "properties": []},
    "export": {"aliases": ["export-messages", "export_messages"], "properties": ["after", "base_kind", "before", "chat", "format", "from_me", "keyword", "kind_name", "limit", "offset", "path", "sender", "talker", "type", "view"]},
    "announcements": {"aliases": ["chatroom-announcements", "chatroom_announcements"], "properties": ["after", "before", "chatroom_id", "limit"]},
    "sql": {"aliases": [], "properties": ["file", "limit", "query", "subdir"]},
    "media": {"aliases": ["media-resources", "media_resources", "attachments"], "properties": ["after", "base_kind", "before", "chat", "debug", "from_me", "include_debug", "include_local_paths", "kind_name", "limit", "local_id", "message_server_id", "message_server_id_str", "offset", "resource_family", "resource_type_raw", "sender", "server_id", "server_id_str", "talker", "type"]},
    "transfers": {"aliases": [], "properties": ["after", "before", "limit"]},
    "red-packets": {"aliases": ["red_packets"], "properties": ["after", "before", "chat", "limit", "sender", "talker"]},
    "forward-history": {"aliases": ["forward_history"], "properties": ["after", "before", "limit"]},
    "favorites": {"aliases": [], "properties": ["after", "before", "limit"]},
    "sns-feed": {"aliases": ["sns", "sns_feed"], "properties": ["after", "before", "keyword", "limit", "offset", "user"]},
    "sns-search": {"aliases": ["sns_search"], "properties": ["after", "before", "keyword", "limit", "offset", "user"]},
    "sns-notifications": {"aliases": ["sns_notifications"], "properties": ["after", "before", "include_read", "limit"]},
    "notifications": {"aliases": [], "properties": ["after", "keyword", "limit"]},
}
COMMAND_ALIASES = {
    "resolve_chat": "resolve-chat",
    "chat-timeline": "timeline",
    "chat_timeline": "timeline",
    "messages": "history",
    "message-context": "context",
    "message_context": "context",
    "search_context": "search-context",
    "search-with-context": "search-context",
    "search_with_context": "search-context",
    "watch": "tail",
    "observe": "tail",
    "events": "tail",
    "read-events": "tail",
    "read_events": "tail",
    "group-members": "members",
    "group_members": "members",
    "read-os": "agent",
    "read_os": "agent",
    "os": "agent",
    "cache_status": "cache-status",
    "cache_refresh": "cache-refresh",
    "cache_rebuild": "cache-rebuild",
    "export-messages": "export",
    "export_messages": "export",
    "chatroom-announcements": "announcements",
    "chatroom_announcements": "announcements",
    "media-resources": "media",
    "media_resources": "media",
    "attachments": "media",
    "red_packets": "red-packets",
    "forward_history": "forward-history",
    "sns": "sns-feed",
    "sns_feed": "sns-feed",
    "sns_search": "sns-search",
    "sns_notifications": "sns-notifications",
    "import-keys": "import-access",
    "import_keys": "import-access",
}


class ReaderError(RuntimeError):
    def __init__(self, message: str, code: str = "reader_error"):
        super().__init__(message)
        self.code = code


def sqlcipher_driver() -> tuple[Any | None, str]:
    try:
        from sqlcipher3 import dbapi2 as sqlcipher_dbapi  # type: ignore

        return sqlcipher_dbapi, "sqlcipher3"
    except ImportError:
        try:
            from pysqlcipher3 import dbapi2 as sqlcipher_dbapi  # type: ignore

            return sqlcipher_dbapi, "pysqlcipher3"
        except ImportError:
            return None, ""


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def emit(command: str, data: Any, pretty: bool = False) -> None:
    payload = {"ok": True, "tool": command.replace("-", "_"), "command": command, "data": data}
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2 if pretty else None))


def fail(command: str, message: str, code: str = "reader_error", pretty: bool = False) -> int:
    payload = {
        "ok": False,
        "tool": command.replace("-", "_"),
        "command": command,
        "error": {"code": code, "message": message},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))
    return 1


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        raise ReaderError("无法读取配置 JSON；支持 UTF-8/BOM、UTF-16 和 UTF-32", "invalid_json") from exc
    if not isinstance(value, dict):
        raise ReaderError(f"配置必须是 JSON 对象：{path}")
    return value


def parse_time(value: str | None) -> int | None:
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(text, fmt).timestamp())
        except ValueError:
            pass
    try:
        return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError as exc:
        raise ReaderError(f"无法识别时间：{value}") from exc


def time_iso(value: int | float | None) -> str:
    if not value:
        return ""
    return dt.datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")


def windows_private_acl(path: Path, *, initialize: bool = False) -> bool:
    """Check real ACLs; initialize only a newly created private resource."""
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not shell or not path.exists() or path.is_symlink():
        return False
    script = r"""
$ErrorActionPreference = 'Stop'
$item = Get-Item -LiteralPath $env:RION_READER_ACL_PATH -Force
if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 2 }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
if ($env:RION_READER_ACL_INITIALIZE -eq '1') {
    if ($item.PSIsContainer) {
        $new = [Security.AccessControl.DirectorySecurity]::new()
        $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    } else {
        $new = [Security.AccessControl.FileSecurity]::new()
        $inherit = [Security.AccessControl.InheritanceFlags]::None
    }
    $new.SetOwner($sid)
    $new.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $identity, 'FullControl', $inherit, 'None', 'Allow')
        $new.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $env:RION_READER_ACL_PATH -AclObject $new
}
$acl = Get-Acl -LiteralPath $env:RION_READER_ACL_PATH
$valid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $sid.Value
$currentAllowed = $false
foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
    if ($rule.AccessControlType -eq 'Allow') {
        if ($rule.IdentityReference.Value -notin @($sid.Value, 'S-1-5-18')) { $valid = $false }
        if ($rule.IdentityReference.Value -eq $sid.Value) { $currentAllowed = $true }
    }
}
if ($valid -and $currentAllowed) { 'SAFE' } else { 'UNSAFE' }
"""
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "RION_READER_ACL_PATH": str(path.absolute()),
                 "RION_READER_ACL_INITIALIZE": "1" if initialize else "0"},
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and result.stdout.strip() == b"SAFE"
    except (OSError, subprocess.SubprocessError):
        return False


def permission_help() -> str:
    if platform.system() == "Windows":
        return "请检查 Windows ACL，仅允许当前用户和 SYSTEM；无法检查时核对 PowerShell，勿跳过权限验证"
    return "请先执行 chmod 600"


def safe_mode(path: Path) -> bool:
    if not path.exists():
        return True
    if platform.system() == "Windows":
        return windows_private_acl(path)
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def prepare_private_output(path: Path, *, chmod_parent: bool = True) -> None:
    created = not path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if platform.system() == "Windows":
        if not windows_private_acl(path.parent, initialize=created):
            raise ReaderError(permission_help(), "unsafe_key_permissions")
    elif chmod_parent:
        os.chmod(path.parent, 0o700)


def protect_new_file(path: Path) -> None:
    if platform.system() == "Windows":
        if not windows_private_acl(path, initialize=True):
            raise ReaderError(permission_help(), "unsafe_key_permissions")
    else:
        os.chmod(path, 0o600)


@contextlib.contextmanager
def private_temporary_directory(*, prefix: str):
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        path = Path(directory)
        if platform.system() == "Windows" and not windows_private_acl(path, initialize=True):
            raise ReaderError(permission_help(), "unsafe_key_permissions")
        yield directory


def is_hex(value: Any, lengths: set[int]) -> bool:
    text = str(value or "").strip()
    return len(text) in lengths and all(character in "0123456789abcdefABCDEF" for character in text)


def database_salt(path: Path) -> str:
    """Return the encrypted database's 16-byte SQLCipher salt without exposing content."""
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return ""
    if len(header) != 16 or header == b"SQLite format 3\x00":
        return ""
    return header.hex()


class Snapshot:
    def __init__(self, source: Path):
        self.source = source
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.path = source

    def __enter__(self) -> Path:
        self.temp = tempfile.TemporaryDirectory(prefix="rion-wechat-reader-")
        target_dir = Path(self.temp.name)
        self.path = target_dir / self.source.name
        try:
            shutil.copy2(self.source, self.path)
            for suffix in ("-wal", "-shm"):
                companion = Path(str(self.source) + suffix)
                if companion.exists():
                    shutil.copy2(companion, Path(str(self.path) + suffix))
        except OSError as exc:
            self.temp.cleanup()
            self.temp = None
            raise ReaderError(f"无法创建只读快照：{self.source.name}: {exc}") from exc
        return self.path

    def __exit__(self, *_: Any) -> None:
        if self.temp:
            self.temp.cleanup()


class DatabaseSet:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_json(config_path)
        self.keys_path = Path(
            os.environ.get(
                "RION_WECHAT_READER_KEYS",
                str(self.config.get("keys_file") or "~/.config/rion-wechat-reader/keys.json"),
            )
        ).expanduser()
        self.keys = load_json(self.keys_path)
        self.session_db = self._path("session_db")
        self.contact_db = self._path("contact_db")
        self.message_dbs = [Path(str(p)).expanduser() for p in self.config.get("message_dbs", [])]
        self.favorite_db = self._path("favorite_db")
        self.sns_db = self._path("sns_db")
        self.hardlink_db = self._path("hardlink_db")
        self.resource_roots = [Path(str(p)).expanduser() for p in self.config.get("resource_roots", [])]
        self.self_username = str(self.config.get("self_username") or "")

    def _path(self, name: str) -> Path | None:
        value = self.config.get(name)
        return Path(str(value)).expanduser() if value else None

    def configured(self) -> bool:
        return bool(self.session_db and self.contact_db and self.message_dbs)

    def key_for(self, path: Path) -> str:
        return str(self.key_spec_for(path).get("key") or "")

    def key_spec_for(self, path: Path) -> dict[str, Any]:
        values = self.keys.get("keys", self.keys)
        if not isinstance(values, dict):
            values = {}
        for candidate in (str(path), str(path.resolve()), path.name, "*", "default"):
            value = values.get(candidate)
            if isinstance(value, str) and value.strip():
                return {"key": value.strip()}
            if isinstance(value, dict) and (value.get("key") or value.get("enc_key")):
                return {**value, "key": value.get("key") or value.get("enc_key")}

        salt_values = self.keys.get("salt_keys")
        try:
            schema_version = int(self.keys.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        if not isinstance(salt_values, dict) and schema_version >= 2:
            salt_values = values
        if isinstance(salt_values, dict):
            salt = database_salt(path)
            value = salt_values.get(salt) if salt else None
            if isinstance(value, str):
                spec: dict[str, Any] = {"key": value.strip()}
            elif isinstance(value, dict) and (value.get("key") or value.get("enc_key")):
                spec = {**value, "key": value.get("key") or value.get("enc_key")}
            else:
                spec = {}
            key = str(spec.get("key") or "").strip()
            if salt and len(key) == 64:
                spec["key"] = key + salt
            if spec:
                return spec
        return {}

    @staticmethod
    def validated_key_spec(spec: dict[str, Any], filename: str) -> dict[str, Any]:
        def numeric(name: str) -> int:
            try:
                return int(spec[name])
            except (TypeError, ValueError) as exc:
                raise ReaderError(f"{name} 必须是整数", "invalid_key_parameters") from exc

        key = str(spec.get("key") or "").strip()
        literal = re.fullmatch(r"[xX]'([0-9a-fA-F]{64}|[0-9a-fA-F]{96})'", key)
        if literal:
            key = literal.group(1)
        if not is_hex(key, {64, 96}):
            raise ReaderError(f"{filename} 的 raw key 必须是 64 或 96 位十六进制", "invalid_key")
        result: dict[str, Any] = {"key": key}
        if "cipher_compatibility" in spec:
            value = numeric("cipher_compatibility")
            if value not in {1, 2, 3, 4}:
                raise ReaderError("cipher_compatibility 只接受 1、2、3、4", "invalid_key_parameters")
            result["cipher_compatibility"] = value
        if "cipher_page_size" in spec:
            value = numeric("cipher_page_size")
            if value < 512 or value > 65536 or value & (value - 1):
                raise ReaderError("cipher_page_size 必须是 512–65536 的 2 次幂", "invalid_key_parameters")
            result["cipher_page_size"] = value
        if "kdf_iter" in spec:
            value = numeric("kdf_iter")
            if value <= 0:
                raise ReaderError("kdf_iter 必须是正整数", "invalid_key_parameters")
            result["kdf_iter"] = value
        if "cipher_plaintext_header_size" in spec:
            value = numeric("cipher_plaintext_header_size")
            if value < 0 or value % 16:
                raise ReaderError("cipher_plaintext_header_size 必须是非负的 16 倍数", "invalid_key_parameters")
            result["cipher_plaintext_header_size"] = value
        if "cipher_use_hmac" in spec:
            result["cipher_use_hmac"] = 1 if bool(spec["cipher_use_hmac"]) else 0
        allowed_algorithms = {
            "cipher_hmac_algorithm": {"HMAC_SHA1", "HMAC_SHA256", "HMAC_SHA512"},
            "cipher_kdf_algorithm": {"PBKDF2_HMAC_SHA1", "PBKDF2_HMAC_SHA256", "PBKDF2_HMAC_SHA512"},
        }
        for name, allowed in allowed_algorithms.items():
            if name in spec:
                value = str(spec[name]).upper()
                if value not in allowed:
                    raise ReaderError(f"{name} 不受支持", "invalid_key_parameters")
                result[name] = value
        return result

    @contextlib.contextmanager
    def connect(self, source: Path):
        if not source.exists():
            raise ReaderError(f"数据库不存在：{source}")
        raw_spec = self.key_spec_for(source)
        key_spec = self.validated_key_spec(raw_spec, source.name) if raw_spec else {}
        key = str(key_spec.get("key") or "")
        module: Any = sqlite3
        if key:
            module, _driver_name = sqlcipher_driver()
            if module is None:
                raise ReaderError("检测到加密数据库密钥，但未安装兼容的 sqlcipher3/pysqlcipher3 DB-API 驱动")
        with Snapshot(source) as snapshot:
            conn = None
            try:
                uri = snapshot.resolve().as_uri() + "?mode=ro"
                conn = module.connect(uri, uri=True)
                conn.row_factory = getattr(module, "Row", sqlite3.Row)
                if key:
                    conn.execute(f"PRAGMA key=\"x'{key}'\"")
                    for pragma in (
                        "cipher_compatibility",
                        "cipher_page_size",
                        "kdf_iter",
                        "cipher_use_hmac",
                        "cipher_plaintext_header_size",
                        "cipher_hmac_algorithm",
                        "cipher_kdf_algorithm",
                    ):
                        if pragma in key_spec:
                            conn.execute(f"PRAGMA {pragma}={key_spec[pragma]}")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                yield conn
            except ReaderError:
                raise
            except Exception as exc:
                raise ReaderError(f"数据库无法只读打开：{source.name}: {exc}") from exc
            finally:
                if conn is not None:
                    conn.close()

    def schema_report(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "session": {"configured": bool(self.session_db), "compatible": False},
            "contact": {"configured": bool(self.contact_db), "compatible": False},
            "messages": {
                "configured": bool(self.message_dbs),
                "compatible": False,
                "database_count": len(self.message_dbs),
            },
        }
        specifications = [
            ("session", self.session_db, "SessionTable", {"username", "last_timestamp"}),
            ("contact", self.contact_db, "contact", {"username", "nick_name", "remark"}),
        ]
        for kind, path, table, required in specifications:
            if not path or not path.exists():
                continue
            try:
                with self.connect(path) as conn:
                    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    columns = (
                        {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
                        if table in tables
                        else set()
                    )
                report[kind].update(
                    {
                        "compatible": table in tables and required <= columns,
                        "table_present": table in tables,
                        "missing_columns": sorted(required - columns),
                    }
                )
            except ReaderError as exc:
                report[kind]["error"] = str(exc)

        compatible_message_dbs = 0
        message_table_count = 0
        wcdb_compression_table_count = 0
        message_errors: list[str] = []
        required_message_columns = {"local_id", "local_type", "real_sender_id", "create_time"}
        for path in self.message_dbs:
            if not path.exists():
                continue
            try:
                with self.connect(path) as conn:
                    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    msg_tables = sorted(name for name in tables if name.startswith("Msg_"))
                    valid_tables = 0
                    for table in msg_tables:
                        columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
                        if "WCDB_CT_message_content" in columns:
                            wcdb_compression_table_count += 1
                        if required_message_columns <= columns and {"message_content", "compress_content"} & columns:
                            valid_tables += 1
                    if "Name2Id" in tables and valid_tables:
                        compatible_message_dbs += 1
                        message_table_count += valid_tables
            except ReaderError as exc:
                message_errors.append(str(exc))
        report["messages"].update(
            {
                "compatible": bool(self.message_dbs) and compatible_message_dbs == len(self.message_dbs),
                "compatible_database_count": compatible_message_dbs,
                "message_table_count": message_table_count,
                "wcdb_compression_table_count": wcdb_compression_table_count,
            }
        )
        if message_errors:
            report["messages"]["errors"] = message_errors
        return report

    def contact_index(self) -> dict[str, dict[str, Any]]:
        if not self.contact_db:
            raise ReaderError("尚未配置 contact_db", "database_not_configured")
        with self.connect(self.contact_db) as conn:
            columns = {str(row[1]) for row in conn.execute('PRAGMA table_info("contact")')}
            required = {"username", "nick_name", "remark"}
            if not required <= columns:
                raise ReaderError("联系人表缺少必需字段", "incompatible_contact_schema")
            fields = [
                f'"{name}"' if name in columns else f'NULL AS "{name}"'
                for name in (
                    "username",
                    "nick_name",
                    "remark",
                    "alias",
                    "description",
                    "local_type",
                    "verify_flag",
                    "delete_flag",
                )
            ]
            rows = conn.execute(
                "SELECT " + ", ".join(fields) + " FROM contact"
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["display_name"] = item.get("remark") or item.get("nick_name") or item.get("username")
            item["is_verified"] = bool(item.get("verify_flag"))
            result[str(item["username"])] = item
        return result


def chat_type(username: str, contact: dict[str, Any] | None = None) -> str:
    if username.endswith("@chatroom"):
        return "group"
    if "@openim" in username:
        return "corp_im"
    if username.startswith("gh_") or int((contact or {}).get("verify_flag") or 0):
        return "official_account"
    if contact is None:
        return "folded"
    return "private"


def message_kind(local_type: int, content: str) -> str:
    base_type = local_type & 0xFFFFFFFF
    subtype = local_type >> 32
    normalized = content.casefold()
    basic = {
        1: "text",
        3: "image",
        34: "voice",
        43: "video",
        47: "sticker",
        48: "location",
        10000: "system",
    }
    if base_type in basic:
        return basic[base_type]
    if base_type == 50:
        return "voip"
    if base_type == 67:
        return "unknown"
    if base_type == 49:
        if subtype == 5:
            return "link"
        if subtype == 6:
            return "file"
        if subtype == 8:
            return "file"
        if subtype == 19:
            return "forward_chat"
        if subtype == 33:
            return "miniprogram"
        if subtype == 51:
            return "channel_video"
        if subtype == 53:
            return "solitaire"
        if subtype == 57:
            return "quote"
        if subtype == 62:
            return "pat"
        if subtype == 2000:
            return "transfer"
        if subtype == 2001:
            return "red_packet"
        if "<recorditem" in normalized or re.search(r"<type>\s*19\s*</type>", normalized):
            return "forward_chat"
        if "hongbao" in normalized or "redpacket" in normalized or "redenvelope" in normalized:
            return "red_packet"
        if "<paysubtype>" in normalized or re.search(r"<type>\s*2000\s*</type>", normalized):
            return "transfer"
        if re.search(r"<type>\s*6\s*</type>", normalized):
            return "file"
        if re.search(r"<type>\s*5\s*</type>", normalized):
            return "link"
        if re.search(r"<type>\s*33\s*</type>", normalized):
            return "miniprogram"
        if re.search(r"<type>\s*57\s*</type>", normalized):
            return "quote"
        return "app"
    return f"type_{base_type}"


def visible_message_content(content: str, talker: str, sender_username: str) -> str:
    """Remove the sender prefix stored inside group-message payloads."""
    if talker.endswith("@chatroom") and sender_username:
        prefix = sender_username + ":\n"
        if content.startswith(prefix):
            return content[len(prefix) :]
    return content


def message_metadata(content: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for output_name, attribute_names in {
        "md5": ("md5", "filemd5", "rawfilemd5"),
        "filename": ("filename", "file_name"),
        "title": ("title",),
        "size": ("length", "filesize", "totallen"),
    }.items():
        for attribute_name in attribute_names:
            match = re.search(
                rf"(?:<{attribute_name}>|\b{attribute_name}=\")([^<\"]+)",
                content,
                flags=re.IGNORECASE,
            )
            if match:
                metadata[output_name] = match.group(1)[:500]
                break
    return metadata


def filter_message_rows(
    rows: list[dict[str, Any]],
    sender: str | None = None,
    from_me: bool | None = None,
    kind_name: str | None = None,
    base_kind: int | None = None,
) -> list[dict[str, Any]]:
    sender_needle = (sender or "").casefold().replace(" ", "")
    kind_needle = (kind_name or "").casefold().replace("-", "_")
    result: list[dict[str, Any]] = []
    for row in rows:
        if sender_needle:
            values = (str(row.get("sender") or ""), str(row.get("sender_wxid") or ""))
            if sender_needle not in "".join(values).casefold().replace(" ", ""):
                continue
        if from_me is not None and bool(row.get("from_me")) != from_me:
            continue
        if kind_needle and str(row.get("kind_name") or "").casefold().replace("-", "_") != kind_needle:
            continue
        if base_kind is not None and int(row.get("local_type") or 0) != base_kind:
            continue
        result.append(row)
    return result


def shape_search_rows(
    rows: list[dict[str, Any]],
    include_text: bool,
    snippet_only: bool,
    max_text_chars: int,
) -> list[dict[str, Any]]:
    shaped: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if snippet_only or not include_text:
            item.pop("message_content", None)
            item.pop("compress_content", None)
        if not include_text:
            item.pop("text", None)
            item.pop("content", None)
        elif snippet_only:
            text = str(item.get("text") or "")[: max(0, max_text_chars)]
            item["text"] = text
            item["content"] = text
        shaped.append(item)
    return shaped


def apply_search_mode(rows: list[dict[str, Any]], keyword: str, mode: str) -> list[dict[str, Any]]:
    needle = keyword.casefold()
    if mode == "exact":
        return [row for row in rows if str(row.get("text") or "").casefold() == needle]
    if mode == "prefix":
        return [row for row in rows if str(row.get("text") or "").casefold().startswith(needle)]
    if mode == "regex":
        try:
            pattern = re.compile(keyword, re.IGNORECASE)
        except re.error as exc:
            raise ReaderError(f"无效正则表达式：{exc}", "invalid_argument") from exc
        return [row for row in rows if pattern.search(str(row.get("text") or ""))]
    return rows


def first_int(*values: Any) -> int | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ReaderError(f"无法识别消息 ID：{value}", "invalid_argument") from exc
    return None


def filter_message_cursors(
    rows: list[dict[str, Any]],
    after_local_id: int | None = None,
    before_local_id: int | None = None,
    after_server_id: int | None = None,
    before_server_id: int | None = None,
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        local_id = int(row.get("local_id") or 0)
        server_id = int(row.get("server_id") or 0)
        if after_local_id is not None and local_id <= after_local_id:
            continue
        if before_local_id is not None and local_id >= before_local_id:
            continue
        if after_server_id is not None and server_id <= after_server_id:
            continue
        if before_server_id is not None and server_id >= before_server_id:
            continue
        result.append(row)
    return result


def local_id_for_server(db: DatabaseSet, chat: str, server_id: int) -> int:
    username = resolve_username(db, chat)
    table = message_table(username)
    for source in db.message_dbs:
        with db.connect(source) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if "server_id" not in columns:
                raise ReaderError(
                    "当前微信消息表没有 server_id；请改用 local_id 定位上下文",
                    "server_id_not_available",
                )
            row = conn.execute(f'SELECT local_id FROM "{table}" WHERE server_id=? LIMIT 1', (server_id,)).fetchone()
            if row:
                return int(row[0])
    raise ReaderError(f"找不到消息 server_id={server_id}", "message_not_found")


def sessions(db: DatabaseSet, limit: int, type_filter: str | None, keyword: str | None) -> list[dict[str, Any]]:
    if not db.session_db:
        raise ReaderError("尚未配置 session_db", "database_not_configured")
    contacts = db.contact_index()
    with db.connect(db.session_db) as conn:
        columns = {str(row[1]) for row in conn.execute('PRAGMA table_info("SessionTable")')}
        required = {"username", "last_timestamp"}
        if not required <= columns:
            raise ReaderError("会话表缺少必需字段", "incompatible_session_schema")
        fields = [
            f'"{name}"' if name in columns else f'NULL AS "{name}"'
            for name in (
                "username",
                "type",
                "unread_count",
                "summary",
                "last_timestamp",
                "sort_timestamp",
                "last_msg_type",
                "last_msg_sub_type",
                "last_msg_sender",
                "last_sender_display_name",
            )
        ]
        order_column = "sort_timestamp" if "sort_timestamp" in columns else "last_timestamp"
        rows = conn.execute(
            "SELECT " + ", ".join(fields) + f" FROM SessionTable ORDER BY {order_column} DESC LIMIT ?",
            (max(limit * 4, limit),),
        ).fetchall()
    allowed = set((type_filter or "").split(",")) - {"", "all"}
    needle = (keyword or "").casefold().replace(" ", "")
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("summary"), bytes):
            item["summary"] = decode_message_content(
                {"message_content": item["summary"], "WCDB_CT_message_content": 4}
            )
        username = str(item.get("username") or "")
        contact = contacts.get(username, {})
        item["display_name"] = contact.get("display_name") or username
        item["chat_type"] = chat_type(username, contact or None)
        if allowed and item["chat_type"] not in allowed:
            continue
        haystack = f"{username}{item['display_name']}{item.get('summary') or ''}".casefold().replace(" ", "")
        if needle and needle not in haystack:
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result


def contacts(db: DatabaseSet, limit: int, keyword: str | None) -> list[dict[str, Any]]:
    rows = list(db.contact_index().values())
    needle = (keyword or "").casefold().replace(" ", "")
    result = []
    for item in rows:
        username = str(item.get("username") or "")
        item = {
            **item,
            "chat_type": chat_type(username, item),
            "type": "group" if username.endswith("@chatroom") else "friend",
        }
        haystack = "".join(str(item.get(key) or "") for key in ("username", "display_name", "nick_name", "remark", "alias"))
        if needle and needle not in haystack.casefold().replace(" ", ""):
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result


def is_chat_id(value: str) -> bool:
    return value.startswith("wxid_") or value.endswith("@chatroom") or value.startswith("gh_")


def bare_chat(username: str) -> dict[str, Any]:
    return {
        "username": username,
        "display_name": username,
        "nick_name": None,
        "remark": None,
        "alias": None,
        "chat_type": chat_type(username, {} if username.endswith("@chatroom") else None),
        "type": "group" if username.endswith("@chatroom") else "friend",
    }


def session_usernames(db: DatabaseSet) -> list[str]:
    if not db.session_db:
        return []
    try:
        with db.connect(db.session_db) as conn:
            return [str(row[0]) for row in conn.execute("SELECT username FROM SessionTable") if row[0]]
    except ReaderError:  # connect() wraps driver errors; sessions are an optional supplement here
        return []


def chat_directory(db: DatabaseSet) -> list[dict[str, Any]]:
    """Contacts plus sessions the contact table does not list.

    Windows WeChat 4.x keeps groups in chat_room/SessionTable rather than contact,
    so matching contacts alone cannot find them (issue #82).
    """
    rows = contacts(db, 100000, None)
    known = {str(row.get("username") or "") for row in rows}
    for username in session_usernames(db):
        if username not in known:
            known.add(username)
            rows.append(bare_chat(username))
    return rows


def resolve_chat(db: DatabaseSet, query: str, type_filter: str | None, limit: int) -> list[dict[str, Any]]:
    needle = query.casefold().replace(" ", "")
    rows = chat_directory(db)
    query = query.strip()
    if is_chat_id(query) and all(str(row.get("username") or "") != query for row in rows):
        rows.append(bare_chat(query))
    allowed = set((type_filter or "").split(",")) - {"", "all"}
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in rows:
        if allowed and item["chat_type"] not in allowed:
            continue
        values = [str(item.get(k) or "") for k in ("username", "display_name", "nick_name", "remark", "alias")]
        normalized = [v.casefold().replace(" ", "") for v in values]
        score = 3 if needle in normalized else 2 if any(v.startswith(needle) for v in normalized) else 1 if any(needle in v for v in normalized) else 0
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("display_name") or "")))
    return [item for _, item in ranked[:limit]]


def resolve_username(db: DatabaseSet, chat: str) -> str:
    if is_chat_id(chat):
        return chat
    rows = resolve_chat(db, chat, None, 2)
    if not rows:
        raise ReaderError(f"找不到聊天对象：{chat}")
    if len(rows) > 1 and rows[0].get("display_name") == rows[1].get("display_name"):
        raise ReaderError(f"聊天对象有重名，请使用 username：{chat}")
    return str(rows[0]["username"])


def message_table(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def message_projection(conn: Any, table: str) -> str:
    columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
    required = {"local_id", "local_type", "real_sender_id", "create_time"}
    missing = required - columns
    if missing:
        raise ReaderError("消息表缺少必需字段：" + ", ".join(sorted(missing)), "incompatible_message_schema")
    if not ({"message_content", "compress_content"} & columns):
        raise ReaderError("消息表缺少内容字段", "incompatible_message_schema")
    selected = []
    for name in (
        "local_id",
        "server_id",
        "local_type",
        "sort_seq",
        "real_sender_id",
        "create_time",
        "status",
        "message_content",
        "compress_content",
        "WCDB_CT_message_content",
    ):
        selected.append(f'"{name}"' if name in columns else f'NULL AS "{name}"')
    return ", ".join(selected)


def decode_message_content(item: dict[str, Any]) -> str:
    value = item.get("message_content")
    if value in (None, "", b""):
        value = item.get("compress_content")
    if value is None:
        return ""
    if not isinstance(value, bytes):
        return str(value)
    try:
        compression_type = int(item.get("WCDB_CT_message_content") or 0)
    except (TypeError, ValueError):
        compression_type = 0
    looks_zstandard = value.startswith(b"\x28\xb5\x2f\xfd")
    if compression_type == 4 or looks_zstandard:
        try:
            import zstandard  # type: ignore
        except ImportError as exc:
            raise ReaderError(
                "读取 WCDB 压缩消息需要 zstandard 运行依赖",
                "zstandard_driver_required",
            ) from exc
        try:
            value = zstandard.ZstdDecompressor().decompress(value)
        except zstandard.ZstdError as exc:
            if compression_type == 4:
                raise ReaderError("WCDB 消息内容解压失败", "message_decompression_failed") from exc
    return value.decode("utf-8", errors="replace")


def text_matches(text: str, keyword: str, mode: str) -> bool:
    normalized = text.casefold()
    needle = keyword.casefold()
    if mode == "exact":
        return normalized == needle
    if mode == "prefix":
        return normalized.startswith(needle)
    if mode == "regex":
        try:
            return bool(re.search(keyword, text, re.IGNORECASE))
        except re.error as exc:
            raise ReaderError(f"无效正则表达式：{exc}", "invalid_argument") from exc
    return needle in normalized


def timeline(db: DatabaseSet, chat: str, limit: int, offset: int, since: str | None, before: str | None) -> list[dict[str, Any]]:
    username = resolve_username(db, chat)
    table = message_table(username)
    contact_map = db.contact_index()
    start = parse_time(since)
    end = parse_time(before)
    rows: list[dict[str, Any]] = []
    for source in db.message_dbs:
        with db.connect(source) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            sender_names = {
                int(row["rowid"]): str(row["user_name"])
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
            }
            clauses: list[str] = []
            params: list[Any] = []
            if start is not None:
                clauses.append("create_time >= ?")
                params.append(start)
            if end is not None:
                clauses.append("create_time <= ?")
                params.append(end)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            fields = message_projection(conn, table)
            query = (
                f'SELECT {fields} FROM "{table}"{where} '
                "ORDER BY create_time DESC, local_id DESC LIMIT ? OFFSET ?"
            )
            for row in conn.execute(query, (*params, limit + offset, 0)).fetchall():
                item = dict(row)
                sender_username = sender_names.get(int(item.get("real_sender_id") or 0), "")
                sender = contact_map.get(sender_username, {}).get("display_name") or sender_username
                content = visible_message_content(decode_message_content(item), username, sender_username)
                item.update(
                    {
                        "chat": contact_map.get(username, {}).get("display_name") or username,
                        "talker": username,
                        "sender": sender,
                        "sender_wxid": sender_username,
                        "from_me": bool(db.self_username and sender_username == db.self_username),
                        "text": str(content),
                        "content": str(content),
                        "time": time_iso(item.get("create_time")),
                        "kind_name": message_kind(int(item.get("local_type") or 0), str(content)),
                    }
                )
                rows.append(item)
    rows.sort(key=lambda item: (int(item.get("create_time") or 0), int(item.get("local_id") or 0)), reverse=True)
    return rows[offset : offset + limit]


def message_context(
    db: DatabaseSet, chat: str, local_id: int, before_count: int, after_count: int
) -> list[dict[str, Any]]:
    username = resolve_username(db, chat)
    table = message_table(username)
    contact_map = db.contact_index()
    for source in db.message_dbs:
        with db.connect(source) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            fields = message_projection(conn, table)
            anchor = conn.execute(f'SELECT {fields} FROM "{table}" WHERE local_id=?', (local_id,)).fetchone()
            if not anchor:
                continue
            anchor_time = int(anchor["create_time"] or 0)
            before_rows = conn.execute(
                f'SELECT {fields} FROM "{table}" '
                "WHERE create_time < ? OR (create_time = ? AND local_id < ?) "
                "ORDER BY create_time DESC, local_id DESC LIMIT ?",
                (anchor_time, anchor_time, local_id, max(0, before_count)),
            ).fetchall()
            after_rows = conn.execute(
                f'SELECT {fields} FROM "{table}" '
                "WHERE create_time > ? OR (create_time = ? AND local_id > ?) "
                "ORDER BY create_time ASC, local_id ASC LIMIT ?",
                (anchor_time, anchor_time, local_id, max(0, after_count)),
            ).fetchall()
            sender_names = {
                int(row["rowid"]): str(row["user_name"])
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
            }
            ordered = list(reversed(before_rows)) + [anchor] + list(after_rows)
            selected: list[dict[str, Any]] = []
            for row in ordered:
                item = dict(row)
                sender_username = sender_names.get(int(item.get("real_sender_id") or 0), "")
                content = visible_message_content(decode_message_content(item), username, sender_username)
                item.update(
                    {
                        "chat": contact_map.get(username, {}).get("display_name") or username,
                        "talker": username,
                        "sender": contact_map.get(sender_username, {}).get("display_name") or sender_username,
                        "sender_wxid": sender_username,
                        "from_me": bool(db.self_username and sender_username == db.self_username),
                        "text": str(content),
                        "content": str(content),
                        "time": time_iso(item.get("create_time")),
                        "kind_name": message_kind(int(item.get("local_type") or 0), str(content)),
                        "context_role": "anchor" if int(item.get("local_id") or 0) == local_id else "context",
                    }
                )
                selected.append(item)
            return selected
    raise ReaderError(f"找不到消息 local_id={local_id}")


def search_messages(
    db: DatabaseSet,
    keyword: str,
    chat: str | None,
    limit: int,
    offset: int,
    after: str | None,
    before: str | None,
    search_mode: str = "contains",
    *,
    sender_filter: str | None = None,
    from_me: bool | None = None,
    kind_name: str | None = None,
    base_kind: int | None = None,
) -> list[dict[str, Any]]:
    start = parse_time(after)
    end = parse_time(before)
    contact_map = db.contact_index()
    known_sessions = sessions(db, 100000, None, None)
    username_by_table = {message_table(str(row["username"])): str(row["username"]) for row in known_sessions}
    selected_username = resolve_username(db, chat) if chat else ""
    result: list[dict[str, Any]] = []
    for source in db.message_dbs:
        with db.connect(source) as conn:
            sender_names = {
                int(row["rowid"]): str(row["user_name"])
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
            }
            tables = [
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'").fetchall()
            ]
            for table in tables:
                username = username_by_table.get(table, "")
                if not username or selected_username and username != selected_username:
                    continue
                clauses: list[str] = []
                params: list[Any] = []
                if start is not None:
                    clauses.append("create_time >= ?")
                    params.append(start)
                if end is not None:
                    clauses.append("create_time <= ?")
                    params.append(end)
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                fields = message_projection(conn, table)
                query = f'SELECT {fields} FROM "{table}"{where} ORDER BY create_time DESC, local_id DESC'
                matched_in_table = 0
                for row in conn.execute(query, params):
                    item = dict(row)
                    sender_username = sender_names.get(int(item.get("real_sender_id") or 0), "")
                    sender = contact_map.get(sender_username, {}).get("display_name") or sender_username
                    content = visible_message_content(decode_message_content(item), username, sender_username)
                    if not text_matches(content, keyword, search_mode):
                        continue
                    item.update(
                        {
                            "chat": contact_map.get(username, {}).get("display_name") or username,
                            "talker": username,
                            "sender": sender,
                            "sender_wxid": sender_username,
                            "from_me": bool(db.self_username and sender_username == db.self_username),
                            "text": str(content),
                            "content": str(content),
                            "time": time_iso(item.get("create_time")),
                            "kind_name": message_kind(int(item.get("local_type") or 0), str(content)),
                        }
                    )
                    if not filter_message_rows([item], sender_filter, from_me, kind_name, base_kind):
                        continue
                    result.append(item)
                    matched_in_table += 1
                    if matched_in_table >= limit + offset:
                        break
    result.sort(key=lambda item: (int(item.get("create_time") or 0), int(item.get("local_id") or 0)), reverse=True)
    return result[offset : offset + limit]


def search_with_context(
    db: DatabaseSet,
    keyword: str,
    chat: str | None,
    limit: int,
    after: str | None,
    before: str | None,
    before_count: int,
    after_count: int,
    search_mode: str = "contains",
) -> list[dict[str, Any]]:
    hits = search_messages(db, keyword, chat, limit, 0, after, before, search_mode)
    results: list[dict[str, Any]] = []
    for hit in hits:
        talker = str(hit.get("talker") or "")
        local_id = int(hit.get("local_id") or 0)
        try:
            context_rows = message_context(db, talker, local_id, before_count, after_count)
        except ReaderError:
            context_rows = [hit]
        results.append({"match": hit, "context": context_rows})
    return results


def special_messages(
    db: DatabaseSet,
    kinds: set[str],
    limit: int,
    after: str | None,
    before: str | None,
    chat: str | None = None,
    local_id: int | None = None,
) -> list[dict[str, Any]]:
    start = parse_time(after)
    end = parse_time(before)
    contact_map = db.contact_index()
    known_sessions = sessions(db, 100000, None, None)
    username_by_table = {message_table(str(row["username"])): str(row["username"]) for row in known_sessions}
    selected_username = resolve_username(db, chat) if chat else ""
    result: list[dict[str, Any]] = []
    for source in db.message_dbs:
        with db.connect(source) as conn:
            sender_names = {
                int(row["rowid"]): str(row["user_name"])
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
            }
            tables = [
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
            ]
            for table in tables:
                username = username_by_table.get(table, "")
                if not username or selected_username and username != selected_username:
                    continue
                clauses: list[str] = []
                params: list[Any] = []
                if start is not None:
                    clauses.append("create_time >= ?")
                    params.append(start)
                if end is not None:
                    clauses.append("create_time <= ?")
                    params.append(end)
                if local_id is not None:
                    clauses.append("local_id = ?")
                    params.append(local_id)
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                fields = message_projection(conn, table)
                query = f'SELECT {fields} FROM "{table}"{where} ORDER BY create_time DESC, local_id DESC LIMIT ?'
                for row in conn.execute(query, (*params, max(limit * 10, 500))).fetchall():
                    item = dict(row)
                    sender_username = sender_names.get(int(item.get("real_sender_id") or 0), "")
                    content = visible_message_content(decode_message_content(item), username, sender_username)
                    kind = message_kind(int(item.get("local_type") or 0), str(content))
                    if kind not in kinds:
                        continue
                    item.update(
                        {
                            "chat": contact_map.get(username, {}).get("display_name") or username,
                            "talker": username,
                            "sender": contact_map.get(sender_username, {}).get("display_name") or sender_username,
                            "sender_wxid": sender_username,
                            "from_me": bool(db.self_username and sender_username == db.self_username),
                            "text": str(content),
                            "content": str(content),
                            "time": time_iso(item.get("create_time")),
                            "kind_name": kind,
                            "metadata": message_metadata(str(content)),
                        }
                    )
                    result.append(item)
    result.sort(key=lambda item: (int(item.get("create_time") or 0), int(item.get("local_id") or 0)), reverse=True)
    return result[:limit]


def hardlink_resources(
    db: DatabaseSet,
    metadata: dict[str, Any],
    resource_family: str | None,
    resource_type_raw: int | None,
    include_local_paths: bool,
) -> list[dict[str, Any]]:
    if not db.hardlink_db:
        return []
    table_families = {
        "image_hardlink_info_v4": "image",
        "video_hardlink_info_v4": "video",
        "file_hardlink_info_v4": "file",
    }
    selected_families = {resource_family} if resource_family else set(table_families.values())
    md5 = str(metadata.get("md5") or "")
    filename = str(metadata.get("filename") or "")
    if not md5 and not filename:
        return []
    resources: list[dict[str, Any]] = []
    with db.connect(db.hardlink_db) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        directory_names: dict[int, str] = {}
        if "dir2id" in tables:
            directory_names = {
                int(row["rowid"]): str(row["username"] or "")
                for row in conn.execute("SELECT rowid, username FROM dir2id").fetchall()
            }
        for table, family in table_families.items():
            if table not in tables or family not in selected_families:
                continue
            clauses: list[str] = []
            params: list[Any] = []
            if md5:
                clauses.append("md5=?")
                params.append(md5)
            if filename:
                clauses.append("file_name=?")
                params.append(filename)
            type_clause = ""
            if resource_type_raw is not None:
                type_clause = " AND type=?"
                params.append(resource_type_raw)
            rows = conn.execute(
                f'SELECT md5, type, file_name, file_size, modify_time, dir1, dir2 FROM "{table}" '
                + "WHERE ("
                + " OR ".join(clauses)
                + ")"
                + type_clause
                + " ORDER BY modify_time DESC LIMIT 20",
                tuple(params),
            ).fetchall()
            for row in rows:
                item = dict(row)
                file_name = str(item.get("file_name") or "")
                dir1 = directory_names.get(int(item.get("dir1") or 0), "")
                dir2 = directory_names.get(int(item.get("dir2") or 0), "")
                local_paths: list[str] = []
                if include_local_paths and file_name:
                    for root in db.resource_roots:
                        candidates = [
                            root / dir1 / dir2 / file_name,
                            root / family / dir1 / dir2 / file_name,
                            root / "HardLink" / dir1 / dir2 / file_name,
                            root / "FileStorage" / "HardLink" / dir1 / dir2 / file_name,
                        ]
                        # Current macOS WeChat stores ordinary attachments below
                        # msg/attach, msg/video, and msg/file.  The hardlink index
                        # still describes the hashed directory components, so keep
                        # the older layouts above and add the live layouts here.
                        if family == "image" and dir1 and dir2:
                            candidates.append(root / "attach" / dir1 / dir2 / "Img" / file_name)
                        elif family == "video" and dir1:
                            candidates.append(root / "video" / dir1 / file_name)
                        elif family == "file" and dir1:
                            candidates.append(root / "file" / dir1 / file_name)
                        local_paths.extend(str(path.resolve()) for path in candidates if path.is_file())
                item.update(
                    {
                        "resource_family": family,
                        "resource_type_raw": int(item.get("type") or 0),
                        "time": time_iso(item.get("modify_time")),
                        "local_paths": sorted(set(local_paths)),
                    }
                )
                resources.append(item)
    return resources


def tail_messages(
    db: DatabaseSet,
    chat: str,
    since_local_id: int,
    limit: int,
    scan_limit: int,
    after: str | None,
) -> list[dict[str, Any]]:
    rows = timeline(db, chat, max(limit, scan_limit), 0, after, None)
    rows = [row for row in rows if int(row.get("local_id") or 0) > since_local_id]
    rows.sort(key=lambda row: (int(row.get("create_time") or 0), int(row.get("local_id") or 0)))
    return rows[-limit:]


def stream_tail(
    db: DatabaseSet,
    chat: str,
    since_local_id: int,
    limit: int,
    scan_limit: int,
    after: str | None,
    follow: bool,
    poll_interval: float,
    max_polls: int,
    sender: str | None = None,
    from_me: bool | None = None,
    kind_name: str | None = None,
) -> None:
    cursor = since_local_id
    polls = 0
    while True:
        raw_rows = tail_messages(db, chat, cursor, limit, scan_limit, after)
        next_cursor = max((int(row.get("local_id") or 0) for row in raw_rows), default=cursor)
        rows = filter_message_rows(raw_rows, sender, from_me, kind_name)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        cursor = next_cursor
        polls += 1
        if not follow or max_polls > 0 and polls >= max_polls:
            return
        time.sleep(max(0.1, poll_interval))


def unread_sessions(db: DatabaseSet, limit: int, type_filter: str | None) -> list[dict[str, Any]]:
    rows = sessions(db, 100000, type_filter, None)
    return [row for row in rows if int(row.get("unread_count") or 0) > 0][:limit]


def database_stats(db: DatabaseSet) -> dict[str, Any]:
    session_rows = sessions(db, 100000, None, None)
    contact_rows = contacts(db, 100000, None)
    return {
        "session_count": len(session_rows),
        "unread_session_count": sum(int(row.get("unread_count") or 0) > 0 for row in session_rows),
        "contact_count": len(contact_rows),
        "message_database_count": len(db.message_dbs),
        "message_table_count": db.schema_report()["messages"]["message_table_count"],
    }


def group_members(db: DatabaseSet, chat: str, limit: int, offset: int) -> dict[str, Any]:
    username = resolve_username(db, chat)
    if not username.endswith("@chatroom"):
        raise ReaderError("members 只接受群聊", "invalid_argument")
    if not db.contact_db:
        raise ReaderError("尚未配置 contact_db", "database_not_configured")
    with db.connect(db.contact_db) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"contact", "chat_room", "chatroom_member"}
        if not required <= tables:
            raise ReaderError("联系人数据库缺少群成员表", "schema_incompatible")
        total_row = conn.execute(
            "SELECT count(*) FROM chat_room room "
            "JOIN chatroom_member member_map ON member_map.room_id=room.id "
            "WHERE room.username=?",
            (username,),
        ).fetchone()
        rows = conn.execute(
            "SELECT member.username, member.nick_name, member.remark, member.alias, member.local_type "
            "FROM chat_room room "
            "JOIN chatroom_member member_map ON member_map.room_id=room.id "
            "JOIN contact member ON member.id=member_map.member_id "
            "WHERE room.username=? ORDER BY member.id LIMIT ? OFFSET ?",
            (username, max(0, limit), max(0, offset)),
        ).fetchall()
    members = []
    for row in rows:
        item = dict(row)
        item["display_name"] = item.get("remark") or item.get("nick_name") or item.get("username")
        members.append(item)
    total = int(total_row[0] if total_row else 0)
    return {"chatroom_id": username, "total": total, "members": members}


def configured_databases(db: DatabaseSet, subdir: str | None, filename: str | None) -> list[tuple[str, Path]]:
    available: list[tuple[str, Path]] = []
    if db.session_db:
        available.append(("session", db.session_db))
    if db.contact_db:
        available.append(("contact", db.contact_db))
    available.extend(("message", path) for path in db.message_dbs)
    if db.favorite_db:
        available.append(("favorite", db.favorite_db))
    if db.sns_db:
        available.append(("sns", db.sns_db))
    if db.hardlink_db:
        available.append(("hardlink", db.hardlink_db))
    return [
        (kind, path)
        for kind, path in available
        if (not subdir or kind == subdir) and (not filename or path.name == filename)
    ]


def database_schema(db: DatabaseSet, subdir: str | None, filename: str | None) -> dict[str, Any]:
    selected = configured_databases(db, subdir, filename)
    if not selected:
        raise ReaderError("找不到匹配的已配置数据库", "database_not_configured")
    databases = []
    for kind, path in selected:
        with db.connect(path) as conn:
            tables = [
                {"name": str(row[0]), "sql": str(row[1] or "")}
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
        databases.append({"subdir": kind, "file": path.name, "tables": tables})
    return {"databases": databases}


def chatroom_announcements(
    db: DatabaseSet,
    chatroom: str | None,
    limit: int,
    after: str | None,
    before: str | None,
) -> list[dict[str, Any]]:
    if not db.contact_db:
        raise ReaderError("尚未配置 contact_db", "database_not_configured")
    username = resolve_username(db, chatroom) if chatroom else ""
    if username and not username.endswith("@chatroom"):
        raise ReaderError("announcements 只接受群聊", "invalid_argument")
    start = parse_time(after)
    end = parse_time(before)
    clauses = []
    params: list[Any] = []
    if username:
        clauses.append("room.username=?")
        params.append(username)
    if start is not None:
        clauses.append("detail.announcement_publish_time_>=?")
        params.append(start)
    if end is not None:
        clauses.append("detail.announcement_publish_time_<=?")
        params.append(end)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.connect(db.contact_db) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"chat_room", "chat_room_info_detail"} <= tables:
            raise ReaderError("联系人数据库缺少群公告表", "schema_incompatible")
        rows = conn.execute(
            "SELECT room.username AS chatroom_id, detail.announcement_ AS announcement, "
            "detail.announcement_editor_ AS editor, detail.announcement_publish_time_ AS publish_time "
            "FROM chat_room room JOIN chat_room_info_detail detail ON detail.room_id_=room.id"
            + where
            + " ORDER BY detail.announcement_publish_time_ DESC LIMIT ?",
            (*params, max(0, limit)),
        ).fetchall()
    return [
        {
            **dict(row),
            "time": time_iso(row["publish_time"]),
        }
        for row in rows
        if row["announcement"]
    ]


def require_optional_database(path: Path | None, name: str) -> Path:
    if not path:
        raise ReaderError(f"尚未配置 {name}_db", "database_not_configured")
    return path


def favorites(
    db: DatabaseSet,
    limit: int,
    after: str | None,
    before: str | None,
) -> list[dict[str, Any]]:
    path = require_optional_database(db.favorite_db, "favorite")
    start = parse_time(after)
    end = parse_time(before)
    clauses: list[str] = []
    params: list[Any] = []
    if start is not None:
        clauses.append("update_time>=?")
        params.append(start)
    if end is not None:
        clauses.append("update_time<=?")
        params.append(end)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.connect(path) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "fav_db_item" not in tables:
            raise ReaderError("收藏数据库缺少 fav_db_item 表", "schema_incompatible")
        columns = {str(row[1]) for row in conn.execute('PRAGMA table_info("fav_db_item")')}
        required = {"local_id", "type", "update_time", "content"}
        missing = required - columns
        if missing:
            raise ReaderError(
                "收藏表缺少必需字段：" + ", ".join(sorted(missing)),
                "schema_incompatible",
            )
        fields = [
            f'"{name}"' if name in columns else f'NULL AS "{name}"'
            for name in ("local_id", "server_id", "type", "update_time", "content", "fromusr", "realchatname")
        ]
        rows = conn.execute(
            "SELECT " + ", ".join(fields) + " "
            "FROM fav_db_item" + where + " ORDER BY update_time DESC, local_id DESC LIMIT ?",
            (*params, max(0, limit)),
        ).fetchall()
    return [
        {**dict(row), "time": time_iso(row["update_time"]), "source": "favorite"}
        for row in rows
    ]


def sns_xml_value(content: str, name: str) -> str:
    match = re.search(
        rf"<{re.escape(name)}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{re.escape(name)}>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(match.group(1).strip()) if match else ""


def sns_feed(
    db: DatabaseSet,
    limit: int,
    offset: int,
    after: str | None,
    before: str | None,
    keyword: str | None,
    user: str | None,
) -> list[dict[str, Any]]:
    path = require_optional_database(db.sns_db, "sns")
    start = parse_time(after)
    end = parse_time(before)
    needle = (keyword or "").casefold()
    user_needle = (user or "").casefold()
    with db.connect(path) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "SnsTimeLine" not in tables:
            raise ReaderError("朋友圈数据库缺少 SnsTimeLine 表", "schema_incompatible")
        rows = conn.execute(
            # pack_info_buf is declared as TEXT in some live databases but may
            # contain protobuf bytes that are not valid UTF-8.  It is not needed
            # for the feed contract, so do not ask the driver to decode it.
            "SELECT tid, user_name, content FROM SnsTimeLine ORDER BY tid DESC LIMIT ?",
            (max(500, (offset + limit) * 10),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.get("content") or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = str(raw)
        create_time_text = sns_xml_value(raw, "createTime")
        create_time = int(create_time_text) if create_time_text.isdigit() else int(item.get("tid") or 0)
        username = str(item.get("user_name") or sns_xml_value(raw, "userName"))
        text = sns_xml_value(raw, "contentDesc")
        if start is not None and create_time < start:
            continue
        if end is not None and create_time > end:
            continue
        if user_needle and user_needle not in username.casefold():
            continue
        if needle and needle not in f"{text} {raw}".casefold():
            continue
        result.append(
            {
                "tid": item.get("tid"),
                "user_name": username,
                "text": text,
                "content": raw,
                "create_time": create_time,
                "time": time_iso(create_time),
                "source": "sns_timeline",
            }
        )
    result.sort(key=lambda item: (int(item.get("create_time") or 0), int(item.get("tid") or 0)), reverse=True)
    return result[offset : offset + limit]


def sns_notifications(
    db: DatabaseSet,
    limit: int,
    after: str | None,
    before: str | None,
    include_read: bool,
) -> list[dict[str, Any]]:
    path = require_optional_database(db.sns_db, "sns")
    start = parse_time(after)
    end = parse_time(before)
    clauses: list[str] = []
    params: list[Any] = []
    if start is not None:
        clauses.append("create_time>=?")
        params.append(start)
    if end is not None:
        clauses.append("create_time<=?")
        params.append(end)
    if not include_read:
        clauses.append("is_unread=1")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.connect(path) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "SnsMessage_tmp3" not in tables:
            raise ReaderError("朋友圈数据库缺少 SnsMessage_tmp3 表", "schema_incompatible")
        rows = conn.execute(
            "SELECT local_id, create_time, type, feed_id, is_unread, from_username, from_nickname, "
            "to_username, to_nickname, content FROM SnsMessage_tmp3"
            + where
            + " ORDER BY create_time DESC, local_id DESC LIMIT ?",
            (*params, max(0, limit)),
        ).fetchall()
    return [
        {**dict(row), "time": time_iso(row["create_time"]), "source": "sns_notification"}
        for row in rows
    ]


def readonly_sql(
    db: DatabaseSet,
    query: str,
    subdir: str | None,
    filename: str | None,
    limit: int,
) -> dict[str, Any]:
    text_query = query.strip()
    lowered = " ".join(text_query.casefold().split())
    forbidden = (" insert ", " update ", " delete ", " drop ", " alter ", " create ", " attach ", " detach ", " vacuum ", " reindex ", " replace ")
    padded = f" {lowered} "
    if ";" in text_query.rstrip(";") or not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ReaderError("sql 只允许单条 SELECT 或 WITH 查询", "unsafe_query")
    if any(token in padded for token in forbidden):
        raise ReaderError("sql 查询包含不允许的写入或结构操作", "unsafe_query")
    selected = configured_databases(db, subdir, filename)
    if len(selected) != 1:
        raise ReaderError("sql 必须通过 --subdir/--file 唯一指定一个已配置数据库", "database_layout_ambiguous")
    kind, path = selected[0]
    with db.connect(path) as conn:
        cursor = conn.execute(text_query)
        columns = [str(item[0]) for item in (cursor.description or [])]
        rows = [dict(row) for row in cursor.fetchmany(max(0, min(limit, 1000)))]
    return {"subdir": kind, "file": path.name, "columns": columns, "rows": rows, "returned": len(rows)}


def agent_overview(db: DatabaseSet, mode: str, include_status: bool) -> dict[str, Any]:
    current_status = status(db)
    capabilities = current_status["status"]["capabilities"]
    payload: dict[str, Any] = {
        "mode": mode,
        "identity": current_status["identity"],
        "coverage": {
            "complete_local_history": bool(current_status["status"]["live_database_read_ok"]),
            "incoming_notification_preview": bool(current_status["status"]["notification_preview_ok"]),
        },
        "capabilities": capabilities,
    }
    if mode in {"overview", "workflows"}:
        payload["workflows"] = [
            "resolve-chat -> timeline/context",
            "search -> search-context",
            "tail --jsonl --follow",
            "unread -> reply prioritization",
            "export -> local user-selected artifact",
        ]
    if include_status or mode == "status":
        payload["status"] = current_status["status"]
    return payload


def cache_status(db: DatabaseSet) -> dict[str, Any]:
    return {
        "mode": "snapshot_only",
        "persistent_cache": False,
        "refresh_required": False,
        "database_configured": db.configured(),
        "note": "rion-wechat-cli 每次读取数据库快照，不维护联系人或消息内容缓存。",
    }


def cache_maintenance(db: DatabaseSet, command: str) -> dict[str, Any]:
    current = status(db)
    return {
        "command": command,
        "performed": False,
        "reason": "no_persistent_content_cache",
        "snapshot_reads": True,
        "live_database_read_ok": current["status"]["live_database_read_ok"],
        "note": "新 CLI 直接读取临时只读快照；无需刷新或重建联系人/消息内容缓存。",
    }


def secure_write_text(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise ReaderError(f"导出目标已存在，未覆盖：{path}", "output_exists")
    prepare_private_output(path, chmod_parent=False)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            protect_new_file(Path(temp_name))
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def export_chat(
    db: DatabaseSet,
    chat: str,
    path: Path,
    output_format: str,
    limit: int,
    offset: int,
    after: str | None,
    before: str | None,
    keyword: str | None,
    sender: str | None,
    from_me: bool | None,
    kind_name: str | None,
    base_kind: int | None,
    force: bool,
) -> dict[str, Any]:
    rows = timeline(db, chat, limit, offset, after, before)
    rows.sort(key=lambda row: (int(row.get("create_time") or 0), int(row.get("local_id") or 0)))
    if keyword:
        needle = keyword.casefold()
        rows = [row for row in rows if needle in str(row.get("text") or "").casefold()]
    rows = filter_message_rows(rows, sender, from_me, kind_name, base_kind)
    if output_format == "jsonl":
        content = "".join(json.dumps(json_safe(row), ensure_ascii=False) + "\n" for row in rows)
    elif output_format == "markdown":
        lines = [f"# {rows[0].get('chat') if rows else chat}", ""]
        lines.extend(f"- **{row.get('time', '')}｜{row.get('sender', '')}**：{row.get('text', '')}" for row in rows)
        content = "\n".join(lines).rstrip() + "\n"
    else:
        items = "".join(
            "<li><time>" + html.escape(str(row.get("time") or "")) + "</time> "
            "<strong>" + html.escape(str(row.get("sender") or "")) + "</strong>："
            + html.escape(str(row.get("text") or "")) + "</li>"
            for row in rows
        )
        title = html.escape(str(rows[0].get("chat") if rows else chat))
        content = f"<!doctype html><meta charset=\"utf-8\"><title>{title}</title><h1>{title}</h1><ol>{items}</ol>\n"
    secure_write_text(path, content, force)
    return {"path": str(path), "format": output_format, "message_count": len(rows), "permissions": "0600"}


def tools_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": name.replace("-", "_"),
            "command": name,
            "aliases": spec["aliases"],
            "read_only": True,
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {property_name: {} for property_name in spec["properties"]},
            },
        }
        for name, spec in TOOL_SPECS.items()
    ]


def tool_schema(name: str) -> dict[str, Any]:
    normalized = name.replace("_", "-")
    canonical = next(
        (
            command
            for command, spec in TOOL_SPECS.items()
            if normalized == command or name in spec["aliases"] or normalized in spec["aliases"]
        ),
        "",
    )
    if not canonical:
        raise ReaderError(f"未知工具：{name}", "unknown_tool")
    spec = TOOL_SPECS[canonical]
    return {
        "command": {"name": canonical, "command": canonical, "aliases": spec["aliases"]},
        "tool": {
            "name": canonical.replace("-", "_"),
            "read_only": True,
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {property_name: {} for property_name in spec["properties"]},
            },
        },
    }


def notification_records(limit: int, after: str | None, keyword: str | None) -> list[dict[str, Any]]:
    path = DEFAULT_NOTIFICATIONS_DB
    if not path.exists():
        raise ReaderError("找不到 macOS Notification Center 数据库")
    start = parse_time(after)
    with Snapshot(path) as snapshot:
        conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            query = (
                "SELECT record.data, record.delivered_date FROM record JOIN app USING(app_id) "
                "WHERE app.identifier=? ORDER BY record.delivered_date DESC LIMIT ?"
            )
            raw_rows = conn.execute(query, (WECHAT_BUNDLE_ID, max(limit * 4, limit))).fetchall()
        finally:
            conn.close()
    needle = (keyword or "").casefold()
    result: list[dict[str, Any]] = []
    for blob, delivered_date in raw_rows:
        unix_time = int(float(delivered_date) + 978307200) if delivered_date else 0
        if start is not None and unix_time < start:
            continue
        try:
            payload = plistlib.loads(blob)
        except Exception:
            continue
        title = str(payload.get("titl") or payload.get("nam") or "")
        body = str(payload.get("body") or "")
        if needle and needle not in f"{title} {body}".casefold():
            continue
        result.append(
            {
                "sender": title,
                "chat": title,
                "text": body,
                "content": body,
                "create_time": unix_time,
                "time": time_iso(unix_time),
                "source": "macos_notification_preview",
                "coverage": "incoming_preview_only",
            }
        )
        if len(result) >= limit:
            break
    return result


def classify_database_tables(tables: set[str]) -> list[str]:
    kinds = []
    if "SessionTable" in tables:
        kinds.append("session")
    if "contact" in tables:
        kinds.append("contact")
    if "Name2Id" in tables and any(name.startswith("Msg_") for name in tables):
        kinds.append("messages")
    if "fav_db_item" in tables:
        kinds.append("favorite")
    if "SnsTimeLine" in tables:
        kinds.append("sns")
    if "image_hardlink_info_v4" in tables or "file_hardlink_info_v4" in tables:
        kinds.append("hardlink")
    return kinds


def inspect_plain_database(path: Path) -> list[str]:
    """Classify an explicitly selected plaintext SQLite database."""
    try:
        with Snapshot(path) as snapshot:
            conn = sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
    except (ReaderError, sqlite3.DatabaseError, OSError):
        return []
    return classify_database_tables(tables)


def inspect_authorized_database(db: DatabaseSet, path: Path) -> list[str]:
    try:
        with db.connect(path) as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except ReaderError:
        return []
    return classify_database_tables(tables)


def sqlite_header(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def discover_databases(root: Path, max_files: int, keys_file: Path | None = None) -> dict[str, Any]:
    if not root.is_dir():
        raise ReaderError(f"扫描目录不存在：{root}")
    candidates: dict[str, list[str]] = {"session": [], "contact": [], "messages": [], "favorite": [], "sns": [], "hardlink": []}
    scanned = 0
    truncated = False
    unreadable_or_encrypted = 0
    locked_paths: list[Path] = []
    scan_errors = 0
    storage_roots = [path for path in root.glob("*/db_storage") if path.is_dir()]
    if (root / "db_storage").is_dir():
        storage_roots.append(root / "db_storage")
    search_roots = storage_roots or [root]
    ignored_directories = {"attach", "attachments", "cache", "file", "files", "image", "images", "tmp", "video", "videos"}

    def record_scan_error(_error: OSError) -> None:
        nonlocal scan_errors
        scan_errors += 1

    stop = False
    for search_root in search_roots:
        try:
            walker = os.walk(search_root, onerror=record_scan_error)
            for dirpath, dirnames, filenames in walker:
                dirnames[:] = sorted(name for name in dirnames if name.casefold() not in ignored_directories)
                for filename in sorted(filenames):
                    if not filename.casefold().endswith(".db"):
                        continue
                    if scanned >= max_files:
                        truncated = True
                        stop = True
                        break
                    path = Path(dirpath) / filename
                    scanned += 1
                    try:
                        with path.open("rb") as handle:
                            plaintext = handle.read(16) == b"SQLite format 3\x00"
                    except OSError:
                        scan_errors += 1
                        continue
                    if not plaintext:
                        unreadable_or_encrypted += 1
                        locked_paths.append(path)
                        continue
                    for kind in inspect_plain_database(path):
                        candidates[kind].append(str(path.resolve()))
                if stop:
                    break
        except OSError:
            scan_errors += 1
        if stop:
            break
    authorized_encrypted_count = 0
    unrecognized_readable_count = 0
    if keys_file and keys_file.is_file() and locked_paths:
        if not safe_mode(keys_file):
            raise ReaderError(f"密钥文件权限过宽，{permission_help()}：{keys_file}", "unsafe_key_permissions")
        with tempfile.TemporaryDirectory(prefix="rion-wechat-discovery-") as temp_dir:
            config = Path(temp_dir) / "config.json"
            config.write_text(json.dumps({"keys_file": str(keys_file.resolve())}), encoding="utf-8")
            authorized_db = DatabaseSet(config)
            for path in locked_paths:
                try:
                    with authorized_db.connect(path) as conn:
                        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                except ReaderError:
                    continue
                authorized_encrypted_count += 1
                kinds = classify_database_tables(tables)
                if not kinds:
                    unrecognized_readable_count += 1
                for kind in kinds:
                    candidates[kind].append(str(path.resolve()))
    return {
        "root": str(root.resolve()),
        "scanned_file_count": scanned,
        "unreadable_or_encrypted_count": unreadable_or_encrypted,
        "authorized_encrypted_count": authorized_encrypted_count,
        "unrecognized_readable_count": unrecognized_readable_count,
        "unresolved_database_count": unreadable_or_encrypted - authorized_encrypted_count,
        "scan_error_count": scan_errors,
        "truncated": truncated,
        "candidates": candidates,
        "note": "先识别明文 SQLite；提供权限为 600 的 keys.json 时，会用授权密钥只读识别加密数据库。",
    }


def default_wechat_root() -> Path | None:
    return next((path for path in DEFAULT_WECHAT_ROOTS if path.is_dir()), None)


def default_resource_roots(database_root: Path) -> list[Path]:
    roots: list[Path] = []
    for base in (database_root, database_root.parent):
        for candidate in (
            base / "msg",
            base / "resource",
            base / "FileStorage",
            base / "files",
        ):
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
    if roots and database_root.is_dir():
        roots.insert(0, database_root)
    return roots


def infer_self_username(
    db: DatabaseSet,
    *,
    session_limit: int = 500,
    per_table_limit: int = 100,
) -> str:
    """Infer the local account from the sender present across the most chats.

    The local account normally appears as a sender in many unrelated chats,
    while any other sender is concentrated in one or a few chats. Ambiguous or
    very small datasets deliberately return an empty value.
    """
    session_rows = sessions(db, session_limit, None, None)
    wanted_tables = {
        message_table(str(row.get("username") or ""))
        for row in session_rows
        if row.get("username") and row.get("chat_type") != "folded"
    }
    chat_presence: Counter[str] = Counter()
    message_counts: Counter[str] = Counter()
    for source in db.message_dbs:
        with db.connect(source) as conn:
            available_tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                )
            }
            selected_tables = available_tables & wanted_tables
            if not selected_tables:
                continue
            sender_names = {
                int(row["rowid"]): str(row["user_name"])
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id")
            }
            for table in selected_tables:
                seen: set[str] = set()
                for row in conn.execute(
                    f'SELECT real_sender_id FROM "{table}" '
                    "ORDER BY create_time DESC, local_id DESC LIMIT ?",
                    (per_table_limit,),
                ):
                    username = sender_names.get(int(row["real_sender_id"] or 0), "")
                    if not username:
                        continue
                    seen.add(username)
                    message_counts[username] += 1
                chat_presence.update(seen)
    ranked = sorted(
        chat_presence,
        key=lambda username: (chat_presence[username], message_counts[username]),
        reverse=True,
    )
    if not ranked or chat_presence[ranked[0]] < 5:
        return ""
    if len(ranked) > 1 and chat_presence[ranked[0]] < chat_presence[ranked[1]] * 2:
        return ""
    return ranked[0]


def access_plan(
    config_path: Path,
    database_root: Path | None = None,
    keys_file: Path | None = None,
    max_files: int = 500,
) -> dict[str, Any]:
    """Inspect access prerequisites without acquiring keys or changing configuration.

    Return only counts and fixed guidance: upstream errors and account paths may
    contain private material and must not become a shareable support report.
    """
    result: dict[str, Any] = {
        "schema_version": 1,
        "environment": {"system": platform.system(), "architecture": platform.machine(), "reader_version": VERSION},
        "state": "needs_database_location",
        "live_database_read_ok": False,
        "scope": "not_verified",
        "performed": {"key_acquisition": False, "configuration_write": False, "provider_execution": False},
        "counts": {},
        "retry_policy": "同样的错误不循环重试；条件改变后再检查。",
        "provider": {
            "mode": "external_optional_not_executed",
            "compatibility": "not_tested_by_this_check",
            "confirmation_required": ["process_access", "wechat_restart_or_resign", "administrator_credential_storage"],
        },
    }

    def finish(state: str, message: str, action: str) -> dict[str, Any]:
        result.update(state=state, message=message, next_actions=[action])
        return result

    try:
        config = load_json(config_path)
        selected_keys = keys_file or Path(os.environ.get(
            "RION_WECHAT_READER_KEYS",
            str(config.get("keys_file") or "~/.config/rion-wechat-reader/keys.json"),
        )).expanduser()
        if selected_keys.exists() and not safe_mode(selected_keys):
            return finish("unsafe_key_permissions", "访问材料权限未通过检查，尚未读取其内容。", permission_help())
        try:
            material = load_json(selected_keys)
        except ReaderError:
            return finish("invalid_access_material", "访问材料无法读取或不是JSON对象。", "检查用户明确提供的文件与格式；不要打印文件内容或填入示例key。")
        entries = material.get("salt_keys", material.get("keys", {
            k: v for k, v in material.items() if k not in {"database_root", "db_root", "schema_version", "wxid", "image_key"}
        }))
        has_material = isinstance(entries, dict) and bool(entries)
        # Explicit inputs describe a different candidate, not the active setup.
        if config_path.is_file() and database_root is None and keys_file is None:
            db = DatabaseSet(config_path)
            state = status(db)["status"]
            result["counts"]["configured_message_databases"] = len(db.message_dbs)
            result["counts"]["missing_databases"] = state["missing_database_count"]
            if state["live_database_read_ok"]:
                result.update(live_database_read_ok=True, scope="configured_local_databases_only")
                return finish("ready", "已有配置可读，无需重新获取key。", "直接使用读取命令；抽检所需私聊、群聊、标签和时间范围，不代表手机完整历史已同步。")
            if state["missing_database_count"]:
                return finish("database_missing", "配置指向的部分数据库已不存在。", "核对当前账号、迁移和数据库位置；不要先重新获取key。")
            paths = [p for p in [db.session_db, db.contact_db, *db.message_dbs] if p]
            missing_keys = sum(1 for p in paths if not sqlite_header(p) and not db.key_for(p))
            result["counts"]["core_databases_without_key"] = missing_keys
            if missing_keys:
                return finish("needs_access", "部分核心数据库缺少匹配的访问材料。", "已有材料可显式导入；没有材料则阅读接入指引，先审计并确认外部provider的影响，不循环setup。")
            if not state["sqlcipher_driver_ready"] or (state["zstandard_required"] and not state["zstandard_driver_ready"]):
                return finish("dependency_required", "数据库读取依赖尚未就绪。", "安装当前运行环境缺少的SQLCipher或zstandard依赖，再运行doctor；无需重新获取key。")
            if db.configured():
                return finish("verification_failed", "现有材料或数据库结构未通过读取验证。", "分别核对材料、加密参数与数据库结构；此结果不能证明一定是key错误。")

        root = database_root
        if root is None:
            bundled_root = material.get("database_root") or material.get("db_root")
            root = Path(str(bundled_root)).expanduser() if bundled_root else default_wechat_root()
        if root is None or not root.is_dir():
            return finish("needs_database_location", "尚未找到所选账号的数据库目录。", "确认本人微信已登录并有本地记录；显式指定--database-root，本命令不会全盘搜索key。")
        account_roots = [p for p in root.glob("*/db_storage") if p.is_dir()]
        if (root / "db_storage").is_dir():
            account_roots.append(root / "db_storage")
        if len(account_roots) > 1:
            result["counts"]["account_candidates"] = len(account_roots)
            return finish("account_selection_required", "发现多个账号目录，未自动选择或读取数据库。", "由用户确认目标账号，再指定该账号的db_storage目录。")
        discovery = discover_databases(root, max(1, max_files), selected_keys if selected_keys.is_file() else None)
        counts = {kind: len(discovery["candidates"][kind]) for kind in ("session", "contact", "messages")}
        result["counts"].update(counts, scanned_databases=discovery["scanned_file_count"], unresolved_databases=discovery["unresolved_database_count"])
        if discovery["scan_error_count"]:
            return finish("filesystem_access_required", "扫描存在文件访问错误，结果不完整。", "核对所选目录的文件访问权限；完全磁盘访问不等于进程调试权限。")
        if discovery["truncated"]:
            return finish("scan_incomplete", "达到数据库扫描上限，不能据此判定完整覆盖。", "缩小到单一账号目录，或显式提高--max-files后再检查。")
        if counts["session"] > 1 or counts["contact"] > 1:
            return finish("database_layout_ambiguous", "核心数据库候选不唯一。", "明确目标目录或通过init指定数据库，不能自动合并账号。")
        core_found = counts["session"] == 1 and counts["contact"] == 1 and counts["messages"] > 0
        if discovery["unresolved_database_count"]:
            if core_found:
                result["scope"] = "partial_candidates_only"
                return finish("partial", "已识别核心数据库，但仍有未能识别或打开的数据库。", "核对缺失项与目标时间范围；可配置已验证核心库，但不可声称全量覆盖。")
            if not has_material:
                return finish("needs_access", "存在不能直接读取的数据库，尚无访问材料。", "显式导入本人已有材料，或先确认外部获取方案；不可读文件也可能损坏，不等于都已证明加密。")
            if sqlcipher_driver()[0] is None:
                return finish("dependency_required", "有访问材料，但缺少SQLCipher运行依赖。", "先安装SQLCipher，再验证材料；不要重复获取key。")
            return finish("verification_failed", "已有材料未能识别所需核心数据库。", "检查账号、salt映射、加密参数与版本；文件存在不等于key已验证。")
        if core_found:
            return finish("ready_to_configure", "核心数据库候选可读，尚未写入配置。", "用相同database-root和keys-file运行setup，然后doctor及真实数据抽检。")
        return finish("database_layout_unsupported", "未识别到所需的核心数据库结构。", "核对目录与微信版本；不要将结构不支持当成缺key。")
    except (ReaderError, OSError, ValueError, TypeError, AttributeError):
        return finish("configuration_check_failed", "配置或本地文件检查失败，详细原文未输出以免泄露隐私。", "在本机检查配置字段和文件访问，修复后重试；不要提交原始配置到Issue。")


def setup_cli(
    config_path: Path,
    database_root: Path | None,
    keys_file: Path,
    self_username: str,
    max_files: int,
    force: bool,
    favorite_db: Path | None = None,
    sns_db: Path | None = None,
    hardlink_db: Path | None = None,
    resource_roots: list[Path] | None = None,
) -> dict[str, Any]:
    bundled_root: Path | None = None
    if database_root is None and keys_file.is_file() and safe_mode(keys_file):
        try:
            keys_value = json.loads(keys_file.read_bytes())
        except (OSError, ValueError, UnicodeError):
            keys_value = {}
        if isinstance(keys_value, dict) and keys_value.get("database_root"):
            bundled_root = Path(str(keys_value["database_root"])).expanduser()
    root = database_root or bundled_root or default_wechat_root()
    if not root:
        raise ReaderError(
            "未找到默认微信数据目录；请使用 --database-root 指定你有权访问的数据库目录。",
            "wechat_data_not_found",
        )
    discovery = discover_databases(root, max_files, keys_file if keys_file.is_file() else None)
    candidates = discovery["candidates"]
    counts = {kind: len(candidates[kind]) for kind in ("session", "contact", "messages")}
    if counts["session"] != 1 or counts["contact"] != 1 or counts["messages"] < 1:
        encrypted_count = int(discovery["unresolved_database_count"])
        if encrypted_count:
            message = (
                f"发现 {encrypted_count} 个无法由 SQLite 直接打开的数据库文件。"
                "当前 CLI 缺少这些数据库的授权访问材料，不能完成完整历史读取。"
                "请运行 access-plan 检查接入前提；setup不会获取key，反复运行不能补齐材料。"
            )
            code = "database_access_material_required"
        else:
            message = (
                "无法自动确定唯一的 session/contact/message 数据库；"
                f"候选数量为 session={counts['session']}、contact={counts['contact']}、messages={counts['messages']}。"
            )
            code = "database_layout_ambiguous"
        raise ReaderError(message, code)
    inferred_roots = resource_roots or default_resource_roots(root)
    initialized = initialize_config(
        config_path,
        Path(candidates["session"][0]),
        Path(candidates["contact"][0]),
        [Path(path) for path in candidates["messages"]],
        keys_file,
        self_username,
        force,
        favorite_db or (Path(candidates["favorite"][0]) if len(candidates["favorite"]) == 1 else None),
        sns_db or (Path(candidates["sns"][0]) if len(candidates["sns"]) == 1 else None),
        hardlink_db or (Path(candidates["hardlink"][0]) if len(candidates["hardlink"]) == 1 else None),
        inferred_roots,
    )
    db = DatabaseSet(config_path)
    inferred_self = False
    if not self_username:
        inferred_username = infer_self_username(db)
        if inferred_username:
            config_value = json.loads(config_path.read_bytes())
            config_value["self_username"] = inferred_username
            secure_write_json(config_path, config_value, force=True)
            db = DatabaseSet(config_path)
            inferred_self = True
    initialized["self_username_inferred"] = inferred_self
    initialized["resource_root_count"] = len(inferred_roots)
    return {"setup": initialized, "doctor": doctor(db), "discovery": {"scanned_file_count": discovery["scanned_file_count"]}}


def self_test(require_sqlcipher: bool = False) -> dict[str, Any]:
    checks = {"sqlite": False, "snapshot": False, "query_only": False}
    driver, driver_name = sqlcipher_driver()
    sqlcipher = {"available": driver is not None, "driver": driver_name, "roundtrip": False}
    zstandard_check = {"available": False, "roundtrip": False}
    try:
        import zstandard  # type: ignore

        zstandard_check["available"] = True
        probe = "wcdb-zstandard-ok".encode("utf-8")
        compressed = zstandard.ZstdCompressor().compress(probe)
        zstandard_check["roundtrip"] = zstandard.ZstdDecompressor().decompress(compressed) == probe
    except ImportError:
        pass
    with tempfile.TemporaryDirectory(prefix="rion-wechat-cli-self-test-") as temp_dir:
        source = Path(temp_dir) / "self-test.db"
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE probe(value TEXT)")
            conn.execute("INSERT INTO probe VALUES('ok')")
        checks["sqlite"] = True
        with Snapshot(source) as snapshot:
            conn = sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                checks["snapshot"] = conn.execute("SELECT value FROM probe").fetchone()[0] == "ok"
                try:
                    conn.execute("INSERT INTO probe VALUES('blocked')")
                except sqlite3.DatabaseError:
                    checks["query_only"] = True
            finally:
                conn.close()
        if driver is not None:
            encrypted = Path(temp_dir) / "encrypted.db"
            key = "11" * 32
            conn = driver.connect(str(encrypted))
            try:
                conn.execute(f"PRAGMA key=\"x'{key}'\"")
                conn.execute("CREATE TABLE probe(value TEXT)")
                conn.execute("INSERT INTO probe VALUES('encrypted-ok')")
                conn.commit()
            finally:
                conn.close()
            config = Path(temp_dir) / "private" / "config.json"
            keys = config.with_name("keys.json")
            secure_write_json(keys, {"keys": {encrypted.name: key}})
            config.write_text(json.dumps({"keys_file": str(keys)}), encoding="utf-8")
            with DatabaseSet(config).connect(encrypted) as encrypted_conn:
                sqlcipher["roundtrip"] = encrypted_conn.execute("SELECT value FROM probe").fetchone()[0] == "encrypted-ok"
    passed = all(checks.values()) and (not require_sqlcipher or bool(sqlcipher["roundtrip"]))
    return {
        "passed": passed,
        "checks": checks,
        "sqlcipher": sqlcipher,
        "sqlcipher_required": require_sqlcipher,
        "zstandard": zstandard_check,
    }


def secure_write_json(path: Path, value: dict[str, Any], force: bool = False) -> None:
    if path.exists() and not force:
        raise ReaderError(f"配置已存在，未覆盖：{path}；确认后使用 --force")
    prepare_private_output(path)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            protect_new_file(Path(temp_name))
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def path_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root.resolve()), str(candidate.resolve()))) == str(root.resolve())
    except (OSError, ValueError):
        return False


def import_access_bundle(
    source: Path,
    database_root: Path | None,
    keys_file: Path,
    *,
    force: bool,
    verify: bool,
    max_files: int,
) -> dict[str, Any]:
    """Import an explicitly supplied legacy-compatible access bundle without exposing key material."""
    if not source.is_file():
        raise ReaderError("授权材料文件不存在", "access_bundle_missing")
    if not safe_mode(source):
        raise ReaderError(f"授权材料文件权限过宽，{permission_help()}", "unsafe_key_permissions")
    try:
        loaded = json.loads(source.read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        raise ReaderError("授权材料不是有效 JSON", "invalid_json") from exc
    if not isinstance(loaded, dict):
        raise ReaderError("授权材料必须是路径到 key 信息的对象", "invalid_access_bundle")

    try:
        schema_version = int(loaded.get("schema_version") or 0) if isinstance(loaded, dict) else 0
    except (TypeError, ValueError):
        schema_version = 0
    schema2_salt_map = schema_version >= 2 and isinstance(loaded, dict) and isinstance(loaded.get("keys"), dict)
    wrapped = "keys" in loaded or "salt_keys" in loaded
    entry_groups = []
    if wrapped:
        for field in ("keys", "salt_keys"):
            if field not in loaded:
                continue
            if not isinstance(loaded[field], dict):
                raise ReaderError("授权材料映射必须是对象", "invalid_access_bundle")
            entry_groups.append((field == "salt_keys" or schema2_salt_map, loaded[field]))
    else:
        entry_groups.append((False, loaded))
    if database_root is None:
        bundled_root = (loaded.get("database_root") or loaded.get("db_root")) if isinstance(loaded, dict) else None
        if not bundled_root:
            raise ReaderError(
                "路径型授权材料需要显式提供 --database-root",
                "database_root_required",
            )
        database_root = Path(str(bundled_root)).expanduser()
    if not database_root.is_dir():
        raise ReaderError("数据库目录不存在", "database_root_missing")

    imported: dict[str, dict[str, Any]] = {}
    imported_salts: dict[str, dict[str, Any]] = {}
    metadata_fields = {"database_root", "db_root", "schema_version", "wxid", "image_key"}
    metadata_count = len(set(loaded) - {"keys", "salt_keys"}) if wrapped else 0
    missing_file_count = 0
    root = database_root.resolve()
    allowed_parameters = {
        "cipher_compatibility",
        "cipher_page_size",
        "kdf_iter",
        "cipher_use_hmac",
        "cipher_plaintext_header_size",
        "cipher_hmac_algorithm",
        "cipher_kdf_algorithm",
    }
    for salt_map, raw_name, raw_value in (
        (is_salt, name, value) for is_salt, entries in entry_groups for name, value in entries.items()
    ):
        name = str(raw_name)
        if name.startswith("_") or (not wrapped and name in metadata_fields):
            metadata_count += 1
            continue
        if isinstance(raw_value, str):
            spec: dict[str, Any] = {"key": raw_value}
        elif isinstance(raw_value, dict):
            if raw_value.get("passphrase") is not None or raw_value.get("material_type") == "passphrase":
                raise ReaderError("口令需要按数据库参数派生，不能作为raw key导入", "passphrase_requires_derivation")
            supplied = [DatabaseSet.validated_key_spec({"key": raw_value[field]}, "entry")["key"].lower()
                        for field in ("key", "enc_key", "raw_key") if raw_value.get(field)]
            if len(set(supplied)) > 1:
                raise ReaderError("同一条目存在冲突的密钥字段", "conflicting_key_fields")
            key = supplied[0] if supplied else None
            spec = {"key": key}
            spec.update({parameter: raw_value[parameter] for parameter in allowed_parameters if parameter in raw_value})
        else:
            raise ReaderError("授权材料包含不支持的 key 条目", "invalid_access_bundle")
        if salt_map:
            if not is_hex(name, {32}):
                raise ReaderError("schema-2 授权材料包含无效数据库 salt", "invalid_access_bundle")
            raw_key = DatabaseSet.validated_key_spec(spec, "salt entry")["key"]
            if is_hex(raw_key, {96}):
                if raw_key[-32:].casefold() != name.casefold():
                    raise ReaderError("normalized raw key salt mismatch", "key_salt_mismatch")
                raw_key = raw_key[:64]
            if not is_hex(raw_key, {64}):
                raise ReaderError("schema-2 enc_key 必须是 64 位十六进制", "invalid_key")
            validated = DatabaseSet.validated_key_spec(
                {**spec, "key": raw_key + name.casefold()},
                "schema-2 entry",
            )
            validated["key"] = validated["key"].lower()
            if name.casefold() in imported_salts and imported_salts[name.casefold()] != validated:
                raise ReaderError("同一salt存在冲突的授权材料", "conflicting_key_fields")
            imported_salts[name.casefold()] = validated
            continue
        normalized_name = name.replace("\\", "/")
        candidate = Path(normalized_name).expanduser()
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if not path_within(root, target):
            raise ReaderError("授权材料包含越界数据库路径", "unsafe_access_bundle_path")
        validated = DatabaseSet.validated_key_spec(spec, target.name)
        validated["key"] = validated["key"].lower()
        if str(target) in imported and imported[str(target)] != validated:
            raise ReaderError("同一数据库存在冲突的授权材料", "conflicting_key_fields")
        imported[str(target)] = validated
        if not target.is_file():
            missing_file_count += 1
    if not imported and not imported_salts:
        raise ReaderError("授权材料中没有可导入的数据库 key", "empty_access_bundle")

    if imported_salts:
        matched_salts: set[str] = set()
        inspected = 0
        for candidate in root.rglob("*.db"):
            if inspected >= max(1, max_files):
                break
            inspected += 1
            salt = database_salt(candidate)
            if salt in imported_salts:
                matched_salts.add(salt)
        missing_file_count += len(set(imported_salts) - matched_salts)
        staged_value: dict[str, Any] = {
            "salt_keys": imported_salts,
            "database_root": str(root),
        }
        import_format = "schema2_salt_map"
        if imported:
            staged_value["keys"] = imported
            import_format = "mixed_key_map"
    else:
        staged_value = {"keys": imported, "database_root": str(root)}
        import_format = "path_key_map"

    verification: dict[str, Any] = {
        "attempted": verify,
        "matched_database_count": 0,
        "unresolved_database_count": None,
    }
    if verify:
        driver, _driver_name = sqlcipher_driver()
        if driver is None:
            raise ReaderError("验证加密数据库需要 SQLCipher 运行环境", "sqlcipher_driver_required")
        with private_temporary_directory(prefix="rion-wechat-access-import-") as temp_dir:
            staged_keys = Path(temp_dir) / "keys.json"
            secure_write_json(staged_keys, staged_value)
            discovery = discover_databases(root, max(1, max_files), staged_keys)
        matched = int(discovery["authorized_encrypted_count"])
        unresolved = int(discovery["unresolved_database_count"])
        verification.update({"matched_database_count": matched, "unresolved_database_count": unresolved,
                             "unrecognized_readable_count": discovery["unrecognized_readable_count"],
                             "scan_truncated": discovery["truncated"], "scan_error_count": discovery["scan_error_count"],
                             "verification_level": "sqlite_schema_read"})
        if matched == 0:
            raise ReaderError(
                "授权材料未能打开任何发现的加密数据库；未写入目标 key 文件",
                "access_bundle_not_compatible",
            )

    secure_write_json(keys_file, staged_value, force=force)
    return {
        "format": import_format,
        "imported_key_count": len(imported) + len(imported_salts),
        "metadata_entry_count": metadata_count,
        "missing_database_count": missing_file_count,
        "keys_file_permissions_safe": safe_mode(keys_file),
        "verification": verification,
        "next_command": "rion-wechat-cli setup --keys-file <private-keys-file> --pretty",
    }


def initialize_config(
    config_path: Path,
    session_db: Path,
    contact_db: Path,
    message_dbs: list[Path],
    keys_file: Path,
    self_username: str,
    force: bool,
    favorite_db: Path | None = None,
    sns_db: Path | None = None,
    hardlink_db: Path | None = None,
    resource_roots: list[Path] | None = None,
) -> dict[str, Any]:
    if config_path.exists() and not force:
        raise ReaderError(f"配置已存在，未覆盖：{config_path}；确认后使用 --force")
    paths = [session_db, contact_db, *message_dbs, *([favorite_db] if favorite_db else []), *([sns_db] if sns_db else []), *([hardlink_db] if hardlink_db else [])]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ReaderError("以下数据库不存在：" + ", ".join(missing))
    if not message_dbs:
        raise ReaderError("至少需要一个 --message-db")
    if not keys_file.exists():
        secure_write_json(keys_file, {"keys": {}}, force=False)
    elif not safe_mode(keys_file):
        raise ReaderError(f"密钥文件权限过宽，{permission_help()}：{keys_file}")
    value = {
        "session_db": str(session_db.resolve()),
        "contact_db": str(contact_db.resolve()),
        "message_dbs": [str(path.resolve()) for path in message_dbs],
        "keys_file": str(keys_file.resolve()),
        "self_username": self_username,
    }
    if favorite_db:
        value["favorite_db"] = str(favorite_db.resolve())
    if sns_db:
        value["sns_db"] = str(sns_db.resolve())
    if hardlink_db:
        value["hardlink_db"] = str(hardlink_db.resolve())
    roots = resource_roots or []
    invalid_roots = [str(path) for path in roots if not path.is_dir()]
    if invalid_roots:
        raise ReaderError("以下资源目录不存在：" + ", ".join(invalid_roots))
    if roots:
        value["resource_roots"] = [str(path.resolve()) for path in roots]
    secure_write_json(config_path, value, force=force)
    return {
        "config_written": True,
        "config_path": str(config_path),
        "keys_file": str(keys_file),
        "keys_file_permissions_safe": safe_mode(keys_file),
        "next_command": f"rion-wechat-cli --config {config_path} --pretty doctor",
    }


def doctor(db: DatabaseSet) -> dict[str, Any]:
    current = status(db)
    state = current["status"]
    diagnostics: list[dict[str, str]] = []
    if not db.config_path.exists():
        diagnostics.append({"level": "error", "code": "config_missing", "message": "尚未创建数据库配置。"})
    if not state["keys_file_permissions_safe"]:
        diagnostics.append({"level": "error", "code": "unsafe_key_permissions", "message": "密钥文件权限必须收紧为 600。"})
    if state["missing_database_count"]:
        diagnostics.append({"level": "error", "code": "database_missing", "message": "配置中的部分数据库不存在。"})
    schema = state["schema"]
    for kind in ("session", "contact", "messages"):
        if schema[kind]["configured"] and not schema[kind]["compatible"]:
            diagnostics.append({"level": "error", "code": f"{kind}_schema_incompatible", "message": f"{kind} 数据库结构不兼容或无法打开。"})
    if state["zstandard_required"] and not state["zstandard_driver_ready"]:
        diagnostics.append({"level": "error", "code": "zstandard_driver_required", "message": "WCDB 压缩消息需要 zstandard 运行依赖。"})
    if not state["live_database_read_ok"] and state["notification_preview_ok"]:
        diagnostics.append({"level": "warning", "code": "notification_only", "message": "当前只能读取入站通知预览，不是完整聊天历史。"})
    if not diagnostics:
        diagnostics.append({"level": "ok", "code": "ready", "message": "数据库读取与核心查询能力可用。"})
    return {
        "summary": state["readiness"],
        "status": current,
        "diagnostics": diagnostics,
        "next_actions": [
            "运行 discover --root <你有权使用的数据库目录> 查找明文候选数据库。",
            "使用 init 明确写入数据库路径；密钥只保存在本机 keys.json。",
            "doctor 显示 ready 后再运行 sessions、timeline 和 search。",
        ] if not state["live_database_read_ok"] else [],
    }


def optional_database_ready(db: DatabaseSet, path: Path | None, required_table: str) -> bool:
    if not path or not path.exists():
        return False
    try:
        with db.connect(path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (required_table,),
            ).fetchone()
        return bool(row)
    except ReaderError:
        return False


def status(db: DatabaseSet) -> dict[str, Any]:
    configured = db.configured()
    paths = [p for p in [db.session_db, db.contact_db, *db.message_dbs, db.favorite_db, db.sns_db, db.hardlink_db] if p]
    missing = [str(p) for p in paths if not p.exists()]
    encrypted = [p for p in paths if db.key_for(p)]
    driver_ready = True
    _driver, driver_name = sqlcipher_driver()
    if encrypted:
        driver_ready = _driver is not None
    notification_ready = DEFAULT_NOTIFICATIONS_DB.exists()
    schema = db.schema_report()
    try:
        import zstandard as _zstandard  # type: ignore  # noqa: F401

        zstandard_ready = True
    except ImportError:
        zstandard_ready = False
    zstandard_required = bool(schema["messages"].get("wcdb_compression_table_count"))
    sessions_ready = bool(schema["session"]["compatible"] and schema["contact"]["compatible"])
    messages_ready = bool(schema["messages"]["compatible"])
    favorite_ready = optional_database_ready(db, db.favorite_db, "fav_db_item")
    sns_ready = optional_database_ready(db, db.sns_db, "SnsTimeLine")
    live_ready = False
    error = ""
    if configured and not missing and driver_ready and sessions_ready and messages_ready and (zstandard_ready or not zstandard_required):
        try:
            sessions(db, 1, None, None)
            live_ready = True
        except ReaderError as exc:
            error = str(exc)
    elif zstandard_required and not zstandard_ready:
        error = "WCDB 压缩消息需要 zstandard 运行依赖"
    readiness = "ready" if live_ready else "degraded" if notification_ready else "blocked"
    warnings: list[str] = []
    if zstandard_required and not zstandard_ready:
        warnings.append("zstandard_driver_missing")
    if not live_ready and notification_ready:
        warnings.append("incoming_preview_only")
    return {
        "identity": {
            "name": "rion-wechat-cli",
            "version": VERSION,
            "contract": "read only; no sending, UI control, key acquisition, or WeChat mutation",
        },
        "status": {
            "readiness": readiness,
            "live_read_ok": live_ready,
            "live_database_read_ok": live_ready,
            "notification_preview_ok": notification_ready,
            "notification_coverage": "incoming_preview_only" if notification_ready else "unavailable",
            "database_configured": configured,
            "missing_database_count": len(missing),
            "encrypted_database_count": len(encrypted),
            "sqlcipher_driver_ready": driver_ready,
            "sqlcipher_driver": driver_name,
            "zstandard_required": zstandard_required,
            "zstandard_driver_ready": zstandard_ready,
            "keys_file_permissions_safe": safe_mode(db.keys_path),
            "schema": schema,
            "capabilities": {
                "sessions": sessions_ready,
                "contacts": bool(schema["contact"]["compatible"]),
                "name_resolution": bool(schema["contact"]["compatible"]),
                "timeline": live_ready,
                "context": live_ready,
                "search": live_ready,
                "search_context": live_ready,
                "tail": live_ready,
                "unread": sessions_ready,
                "stats": live_ready,
                "media": live_ready,
                "media_local_paths": optional_database_ready(db, db.hardlink_db, "image_hardlink_info_v4") and bool(db.resource_roots),
                "transfers": live_ready,
                "red_packets": live_ready,
                "forward_history": live_ready,
                "favorites": favorite_ready,
                "sns": sns_ready,
                "sns_feed": sns_ready,
                "sns_search": sns_ready,
                "sns_notifications": optional_database_ready(db, db.sns_db, "SnsMessage_tmp3"),
                "group_members": bool(schema["contact"]["compatible"]),
                "schema": configured and not missing,
                "read_os": True,
                "cache_status": True,
                "cache_refresh": True,
                "cache_rebuild": True,
                "export_messages": live_ready,
                "chatroom_announcements": bool(schema["contact"]["compatible"]),
                "sql": configured and not missing,
                "notifications": notification_ready,
                "tool_discovery": True,
            },
            "warnings": warnings,
            "error": error,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rion-wechat-cli")
    parser.add_argument("--config", default=os.environ.get("RION_WECHAT_READER_CONFIG", str(DEFAULT_CONFIG)))
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--strict-read-only", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_message_identity_filters(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--sender")
        command_parser.add_argument("--from-me", action=argparse.BooleanOptionalAction, default=None)
        command_parser.add_argument("--type", dest="kind_name")
        command_parser.add_argument("--kind-name", dest="kind_name")
        command_parser.add_argument("--base-kind", type=int)

    def add_message_cursors(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--after-local-id", type=int)
        command_parser.add_argument("--before-local-id", type=int)
        command_parser.add_argument("--since-local-id", type=int)
        command_parser.add_argument("--after-message")
        command_parser.add_argument("--after-message-id")
        command_parser.add_argument("--after-message-local-id", type=int)
        command_parser.add_argument("--before-message")
        command_parser.add_argument("--before-message-id")
        command_parser.add_argument("--before-message-local-id", type=int)
        command_parser.add_argument("--since-message")
        command_parser.add_argument("--after-server-id", type=int)
        command_parser.add_argument("--after-server-id-str")
        command_parser.add_argument("--after-message-server-id", type=int)
        command_parser.add_argument("--after-message-server-id-str")
        command_parser.add_argument("--before-server-id", type=int)
        command_parser.add_argument("--before-server-id-str")
        command_parser.add_argument("--before-message-server-id", type=int)
        command_parser.add_argument("--before-message-server-id-str")
    sub.add_parser("version")
    p = sub.add_parser("tools")
    p.add_argument("--profile", default="all")
    p = sub.add_parser("tool-schema")
    p.add_argument("name")
    p.add_argument("--profile", default="all")
    p = sub.add_parser("self-test")
    p.add_argument("--require-sqlcipher", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    p = sub.add_parser("access-plan")
    p.add_argument("--database-root")
    p.add_argument("--keys-file")
    p.add_argument("--max-files", type=int, default=500)
    p = sub.add_parser("setup")
    p.add_argument("--database-root")
    p.add_argument("--keys-file", default="~/.config/rion-wechat-reader/keys.json")
    p.add_argument("--self-username", default="")
    p.add_argument("--favorite-db")
    p.add_argument("--sns-db")
    p.add_argument("--hardlink-db")
    p.add_argument("--resource-root", action="append", default=[])
    p.add_argument("--max-files", type=int, default=500)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser(
        "announcements",
        aliases=["chatroom-announcements", "chatroom_announcements"],
    )
    p.add_argument("chatroom_id", nargs="?")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after")
    p.add_argument("--before")
    p = sub.add_parser("sql")
    p.add_argument("query")
    p.add_argument("--subdir", choices=["session", "contact", "message", "favorite", "sns", "hardlink"])
    p.add_argument("--file")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("media", aliases=["media-resources", "media_resources", "attachments"])
    p.add_argument("chat", nargs="?")
    p.add_argument("--talker")
    p.add_argument("--local-id", type=int)
    p.add_argument("--type", dest="kind_name", choices=["image", "voice", "video", "file", "sticker"])
    p.add_argument("--kind-name", dest="kind_name", choices=["image", "voice", "video", "file", "sticker"])
    p.add_argument("--base-kind", type=int)
    p.add_argument("--sender")
    p.add_argument("--from-me", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--server-id", type=int)
    p.add_argument("--server-id-str")
    p.add_argument("--message-server-id", type=int)
    p.add_argument("--message-server-id-str")
    p.add_argument("--resource-family", choices=["image", "video", "file", "cover", "unknown"])
    p.add_argument("--resource-type-raw", type=int)
    p.add_argument("--include-local-paths", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--include-debug", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--after")
    p.add_argument("--before")
    p = sub.add_parser("transfers")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after")
    p.add_argument("--before")
    p = sub.add_parser("red-packets", aliases=["red_packets"])
    p.add_argument("--chat")
    p.add_argument("--talker")
    p.add_argument("--sender")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after")
    p.add_argument("--before")
    p = sub.add_parser("forward-history", aliases=["forward_history"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after")
    p.add_argument("--before")
    p = sub.add_parser("favorites")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after")
    p.add_argument("--before")
    for name, aliases in (("sns-feed", ["sns", "sns_feed"]), ("sns-search", ["sns_search"])):
        p = sub.add_parser(name, aliases=aliases)
        if name == "sns-search":
            p.add_argument("keyword")
        else:
            p.add_argument("--keyword")
        p.add_argument("--user")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--after")
        p.add_argument("--before")
    p = sub.add_parser("sns-notifications", aliases=["sns_notifications"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after")
    p.add_argument("--before")
    p.add_argument("--include-read", action="store_true")
    p = sub.add_parser("discover")
    p.add_argument("--root", required=True)
    p.add_argument("--max-files", type=int, default=500)
    p.add_argument("--keys-file")
    p = sub.add_parser("import-access", aliases=["import-keys", "import_keys"])
    p.add_argument("--source", required=True)
    p.add_argument("--database-root")
    p.add_argument("--keys-file", default="~/.config/rion-wechat-reader/keys.json")
    p.add_argument("--max-files", type=int, default=500)
    p.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("init")
    p.add_argument("--session-db", required=True)
    p.add_argument("--contact-db", required=True)
    p.add_argument("--message-db", action="append", required=True)
    p.add_argument("--keys-file", default="~/.config/rion-wechat-reader/keys.json")
    p.add_argument("--self-username", default="")
    p.add_argument("--favorite-db")
    p.add_argument("--sns-db")
    p.add_argument("--hardlink-db")
    p.add_argument("--resource-root", action="append", default=[])
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("sessions")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--type-filter")
    p.add_argument("--keyword")
    p = sub.add_parser("contacts")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--keyword")
    p.add_argument("--friends-only", action="store_true")
    p.add_argument("--groups-only", action="store_true")
    p = sub.add_parser("resolve-chat", aliases=["resolve_chat"])
    p.add_argument("query", nargs="?")
    p.add_argument("--chat")
    p.add_argument("--keyword")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--type-filter")
    for name, aliases in (("timeline", ["chat-timeline", "chat_timeline"]), ("history", ["messages"])):
        p = sub.add_parser(name, aliases=aliases)
        p.add_argument("chat", nargs="?")
        p.add_argument("--talker")
        p.add_argument("--limit", type=int, default=50)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--since")
        p.add_argument("--since-time")
        p.add_argument("--after")
        p.add_argument("--before")
        p.add_argument("--display-order", choices=("asc", "desc", "query"), default="asc")
        p.add_argument("--order", choices=("asc", "desc"))
        p.add_argument("--keyword")
        p.add_argument("--include-media-paths", default="false")
        p.add_argument("--debug", action="store_true")
        p.add_argument("--include-debug", action="store_true")
        add_message_identity_filters(p)
        add_message_cursors(p)
        if name == "timeline":
            p.add_argument("--include-images", action=argparse.BooleanOptionalAction, default=False)
        else:
            p.add_argument("--fields")
            p.add_argument("--view", choices=["agent", "raw"], default="agent")
    p = sub.add_parser("context", aliases=["message-context", "message_context"])
    p.add_argument("chat", nargs="?")
    p.add_argument("--talker")
    p.add_argument("--local-id", type=int)
    p.add_argument("--message-local-id", type=int)
    p.add_argument("--around-local-id", type=int)
    p.add_argument("--server-id", type=int)
    p.add_argument("--server-id-str")
    p.add_argument("--message-server-id", type=int)
    p.add_argument("--message-server-id-str")
    p.add_argument("--around-server-id", type=int)
    p.add_argument("--around-server-id-str")
    p.add_argument("--before-count", type=int, default=20)
    p.add_argument("--after-count", type=int, default=20)
    p.add_argument("--before-messages", type=int)
    p.add_argument("--after-messages", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--display-order", choices=["asc", "desc"], default="asc")
    p.add_argument("--include-anchor", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--include-media-paths", default="false")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--include-debug", action="store_true")
    p = sub.add_parser("search")
    p.add_argument("keyword")
    p.add_argument("--in", dest="chat")
    p.add_argument("--chat", dest="chat")
    p.add_argument("--talker", dest="chat")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--after")
    p.add_argument("--before")
    p.add_argument("--sender")
    p.add_argument("--from-me", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--type", dest="kind_name")
    p.add_argument("--kind-name", dest="kind_name")
    p.add_argument("--base-kind", type=int)
    p.add_argument("--include-text", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--snippet-only", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--max-text-chars", type=int, default=500)
    p.add_argument("--search-mode", choices=["substring", "exact", "prefix", "regex"], default="substring")
    p = sub.add_parser(
        "search-context",
        aliases=["search_context", "search-with-context", "search_with_context"],
    )
    p.add_argument("keyword")
    p.add_argument("--in", dest="chat")
    p.add_argument("--chat", dest="chat")
    p.add_argument("--talker", dest="chat")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--after")
    p.add_argument("--before")
    p.add_argument("--before-count", type=int, default=5)
    p.add_argument("--after-count", type=int, default=5)
    p.add_argument("--before-messages", type=int)
    p.add_argument("--after-messages", type=int)
    p.add_argument("--context-limit", type=int)
    p.add_argument("--sender")
    p.add_argument("--from-me", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--type", dest="kind_name")
    p.add_argument("--kind-name", dest="kind_name")
    p.add_argument("--base-kind", type=int)
    p.add_argument("--include-text", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--snippet-only", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--max-text-chars", type=int, default=500)
    p.add_argument("--search-mode", choices=["substring", "exact", "prefix", "regex"], default="substring")
    p.add_argument("--include-media-paths", default="false")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--include-debug", action="store_true")
    p = sub.add_parser("tail", aliases=["watch", "observe", "events", "read-events", "read_events"])
    p.add_argument("chat", nargs="?")
    p.add_argument("--talker")
    p.add_argument("--since-local-id", type=int, default=0)
    p.add_argument("--cursor", type=int)
    p.add_argument("--after")
    p.add_argument("--since")
    p.add_argument("--since-time")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--scan-limit", type=int, default=500)
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--jsonl", action="store_true")
    p.add_argument("--follow", action="store_true")
    p.add_argument("--mode", choices=["snapshot", "follow"])
    p.add_argument("--sender")
    p.add_argument("--from-me", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--type", dest="kind_name")
    p.add_argument("--kind-name", dest="kind_name")
    p.add_argument("--include-media-paths", default="false")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--include-debug", action="store_true")
    p.add_argument("--max-polls", type=int, default=0, help=argparse.SUPPRESS)
    p = sub.add_parser("unread")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--type-filter")
    p.add_argument("--filter")
    sub.add_parser("stats")
    p = sub.add_parser("members", aliases=["group-members", "group_members"])
    p.add_argument("chat", nargs="?")
    p.add_argument("--chatroom-id")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--stats", action="store_true")
    p = sub.add_parser("schema")
    p.add_argument("--subdir", choices=["session", "contact", "message", "favorite", "sns", "hardlink"])
    p.add_argument("--file")
    p = sub.add_parser("agent", aliases=["read-os", "read_os", "os"])
    p.add_argument("--mode", choices=["overview", "coverage", "workflows", "status"], default="overview")
    p.add_argument("--include-status", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--include-debug", action="store_true")
    sub.add_parser("cache-status", aliases=["cache_status"])
    p = sub.add_parser("cache-refresh", aliases=["cache_refresh"])
    p.add_argument("--background", action="store_true")
    p.add_argument("--force", action="store_true")
    sub.add_parser("cache-rebuild", aliases=["cache_rebuild"])
    p = sub.add_parser("export", aliases=["export-messages", "export_messages"])
    p.add_argument("chat", nargs="?")
    p.add_argument("--talker")
    p.add_argument("--path", required=True)
    p.add_argument("--format", choices=["jsonl", "markdown", "html"], default="jsonl")
    p.add_argument("--view", choices=["agent", "raw"], default="agent")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--after")
    p.add_argument("--before")
    p.add_argument("--keyword")
    p.add_argument("--sender")
    p.add_argument("--from-me", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--type", dest="kind_name")
    p.add_argument("--kind-name", dest="kind_name")
    p.add_argument("--base-kind", type=int)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("notifications")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--after")
    p.add_argument("--keyword")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(argv if argv is not None else sys.argv[1:])
    pretty_anywhere = "--pretty" in raw_args
    strict_anywhere = "--strict-read-only" in raw_args
    raw_args = [arg for arg in raw_args if arg not in {"--pretty", "--strict-read-only"}]
    args = parser.parse_args(raw_args)
    args.pretty = pretty_anywhere
    args.strict_read_only = strict_anywhere
    command = COMMAND_ALIASES.get(args.command, args.command)
    try:
        if command == "version":
            emit(command, {"name": "rion-wechat-cli", "version": VERSION}, args.pretty)
        elif command == "tools":
            emit(command, {"tools": tools_catalog()}, args.pretty)
        elif command == "tool-schema":
            emit(command, tool_schema(args.name), args.pretty)
        elif command == "self-test":
            result = self_test(args.require_sqlcipher)
            emit(command, result, args.pretty)
            if not result["passed"]:
                return 1
        elif command == "access-plan":
            emit(command, access_plan(
                Path(args.config).expanduser(),
                Path(args.database_root).expanduser() if args.database_root else None,
                Path(args.keys_file).expanduser() if args.keys_file else None,
                max(1, args.max_files),
            ), args.pretty)
        elif command == "setup":
            emit(
                command,
                setup_cli(
                    Path(args.config).expanduser(),
                    Path(args.database_root).expanduser() if args.database_root else None,
                    Path(args.keys_file).expanduser(),
                    args.self_username,
                    max(1, args.max_files),
                    args.force,
                    Path(args.favorite_db).expanduser() if args.favorite_db else None,
                    Path(args.sns_db).expanduser() if args.sns_db else None,
                    Path(args.hardlink_db).expanduser() if args.hardlink_db else None,
                    [Path(path).expanduser() for path in args.resource_root],
                ),
                args.pretty,
            )
        elif command == "discover":
            emit(
                command,
                discover_databases(
                    Path(args.root).expanduser(),
                    max(1, args.max_files),
                    Path(args.keys_file).expanduser() if args.keys_file else None,
                ),
                args.pretty,
            )
        elif command == "import-access":
            emit(
                command,
                import_access_bundle(
                    Path(args.source).expanduser(),
                    Path(args.database_root).expanduser() if args.database_root else None,
                    Path(args.keys_file).expanduser(),
                    force=args.force,
                    verify=args.verify,
                    max_files=max(1, args.max_files),
                ),
                args.pretty,
            )
        elif command == "init":
            result = initialize_config(
                Path(args.config).expanduser(),
                Path(args.session_db).expanduser(),
                Path(args.contact_db).expanduser(),
                [Path(path).expanduser() for path in args.message_db],
                Path(args.keys_file).expanduser(),
                args.self_username,
                args.force,
                Path(args.favorite_db).expanduser() if args.favorite_db else None,
                Path(args.sns_db).expanduser() if args.sns_db else None,
                Path(args.hardlink_db).expanduser() if args.hardlink_db else None,
                [Path(path).expanduser() for path in args.resource_root],
            )
            emit(command, result, args.pretty)
        else:
            db = DatabaseSet(Path(args.config).expanduser())
            if command == "status":
                emit(command, status(db), args.pretty)
            elif command == "doctor":
                emit(command, doctor(db), args.pretty)
            elif command == "sessions":
                emit(command, {"sessions": sessions(db, args.limit, args.type_filter, args.keyword)}, args.pretty)
            elif command == "contacts":
                rows = contacts(db, args.limit, args.keyword)
                if args.friends_only:
                    rows = [row for row in rows if row["chat_type"] == "private"]
                if args.groups_only:
                    rows = [row for row in rows if row["chat_type"] == "group"]
                emit(command, {"contacts": rows}, args.pretty)
            elif command == "resolve-chat":
                query = args.query or args.chat or args.keyword
                if not query:
                    raise ReaderError("resolve-chat 需要 query、--chat 或 --keyword", "missing_required_argument")
                emit(command, {"candidates": resolve_chat(db, query, args.type_filter, args.limit)}, args.pretty)
            elif command in {"timeline", "history"}:
                chat = args.chat or args.talker
                if not chat:
                    raise ReaderError(f"{command} 需要 chat 或 --talker", "missing_required_argument")
                scan_limit = max(args.limit + args.offset, 5000)
                rows = timeline(db, chat, scan_limit, 0, args.since or args.since_time or args.after, args.before)
                rows = filter_message_cursors(
                    rows,
                    first_int(args.after_local_id, args.after_message_local_id, args.after_message_id, args.after_message, args.since_local_id, args.since_message),
                    first_int(args.before_local_id, args.before_message_local_id, args.before_message_id, args.before_message),
                    first_int(args.after_server_id, args.after_server_id_str, args.after_message_server_id, args.after_message_server_id_str),
                    first_int(args.before_server_id, args.before_server_id_str, args.before_message_server_id, args.before_message_server_id_str),
                )
                rows = filter_message_rows(rows, args.sender, args.from_me, args.kind_name, args.base_kind)
                if args.keyword:
                    needle = args.keyword.casefold()
                    rows = [row for row in rows if needle in str(row.get("text") or "").casefold()]
                rows = rows[args.offset : args.offset + args.limit]
                display_order = args.order or args.display_order
                if display_order == "asc":
                    rows.reverse()
                if args.debug or args.include_debug:
                    for row in rows:
                        row["debug"] = {"media_paths_requested": str(args.include_media_paths).casefold() == "true"}
                if command == "history" and args.fields:
                    selected_fields = {field.strip() for field in args.fields.split(",") if field.strip()}
                    rows = [{key: value for key, value in row.items() if key in selected_fields} for row in rows]
                emit(command, {"query": {"has_more": len(rows) == args.limit, "next_offset": args.offset + len(rows)}, "messages": rows}, args.pretty)
            elif command == "context":
                chat = args.chat or args.talker
                if not chat:
                    raise ReaderError("context 需要 chat 或 --talker", "missing_required_argument")
                local_id = first_int(args.local_id, args.message_local_id, args.around_local_id)
                if local_id is None:
                    server_id = first_int(
                        args.server_id,
                        args.server_id_str,
                        args.message_server_id,
                        args.message_server_id_str,
                        args.around_server_id,
                        args.around_server_id_str,
                    )
                    if server_id is None:
                        raise ReaderError("context 需要 local_id 或 server_id", "missing_required_argument")
                    local_id = local_id_for_server(db, chat, server_id)
                before_count = args.before_messages if args.before_messages is not None else args.before_count
                after_count = args.after_messages if args.after_messages is not None else args.after_count
                if args.limit is not None:
                    before_count = min(before_count, max(0, args.limit // 2))
                    after_count = min(after_count, max(0, args.limit - before_count - 1))
                rows = message_context(db, chat, local_id, before_count, after_count)
                if not args.include_anchor:
                    rows = [row for row in rows if row.get("context_role") != "anchor"]
                if args.display_order == "desc":
                    rows.reverse()
                if args.debug or args.include_debug:
                    for row in rows:
                        row["debug"] = {"anchor_local_id": local_id}
                emit(command, {"messages": rows}, args.pretty)
            elif command == "search":
                rows = search_messages(
                    db,
                    args.keyword,
                    args.chat,
                    args.limit + 1,
                    args.offset,
                    args.after,
                    args.before,
                    args.search_mode,
                    sender_filter=args.sender,
                    from_me=args.from_me,
                    kind_name=args.kind_name,
                    base_kind=args.base_kind,
                )
                has_more = len(rows) > args.limit
                rows = rows[:args.limit]
                rows = shape_search_rows(rows, args.include_text, args.snippet_only, args.max_text_chars)
                emit(command, {"query": {"has_more": has_more, "next_offset": args.offset + len(rows)}, "messages": rows}, args.pretty)
            elif command == "search-context":
                before_count = args.before_messages if args.before_messages is not None else args.before_count
                after_count = args.after_messages if args.after_messages is not None else args.after_count
                if args.context_limit is not None:
                    before_count = min(before_count, max(0, args.context_limit // 2))
                    after_count = min(after_count, max(0, args.context_limit - before_count - 1))
                rows = search_with_context(
                    db,
                    args.keyword,
                    args.chat,
                    args.limit,
                    args.after,
                    args.before,
                    before_count,
                    after_count,
                    args.search_mode,
                )
                filtered_results = []
                for result in rows:
                    match_rows = filter_message_rows([result["match"]], args.sender, args.from_me, args.kind_name, args.base_kind)
                    match_rows = apply_search_mode(match_rows, args.keyword, args.search_mode)
                    if not match_rows:
                        continue
                    result["match"] = shape_search_rows(match_rows, args.include_text, args.snippet_only, args.max_text_chars)[0]
                    if args.debug or args.include_debug:
                        result["debug"] = {"media_paths_requested": str(args.include_media_paths).casefold() == "true"}
                    filtered_results.append(result)
                emit(command, {"results": filtered_results}, args.pretty)
            elif command == "tail":
                chat = args.chat or args.talker
                if not chat:
                    raise ReaderError("tail 当前需要指定聊天对象", "missing_required_argument")
                cursor = first_int(args.cursor, args.since_local_id) or 0
                after = args.after or args.since or args.since_time
                follow = args.follow or args.mode == "follow"
                if follow or args.jsonl:
                    stream_tail(
                        db,
                        chat,
                        cursor,
                        args.limit,
                        args.scan_limit,
                        after,
                        follow,
                        args.poll_interval,
                        args.max_polls,
                        args.sender,
                        args.from_me,
                        args.kind_name,
                    )
                else:
                    rows = tail_messages(db, chat, cursor, args.limit, args.scan_limit, after)
                    rows = filter_message_rows(rows, args.sender, args.from_me, args.kind_name)
                    if args.debug or args.include_debug:
                        for row in rows:
                            row["debug"] = {"media_paths_requested": str(args.include_media_paths).casefold() == "true"}
                    emit(command, {"events": rows, "cursor": rows[-1]["local_id"] if rows else cursor}, args.pretty)
            elif command == "unread":
                emit(command, {"sessions": unread_sessions(db, args.limit, args.type_filter or args.filter)}, args.pretty)
            elif command == "stats":
                emit(command, database_stats(db), args.pretty)
            elif command == "members":
                chat = args.chat or args.chatroom_id
                if not chat:
                    raise ReaderError("members 需要 chat 或 --chatroom-id", "missing_required_argument")
                emit(command, group_members(db, chat, args.limit, args.offset), args.pretty)
            elif command == "schema":
                emit(command, database_schema(db, args.subdir, args.file), args.pretty)
            elif command == "agent":
                overview = agent_overview(db, args.mode, args.include_status)
                if args.debug or args.include_debug:
                    overview["debug"] = {"config_exists": db.config_path.exists(), "strict_read_only": True}
                emit(command, overview, args.pretty)
            elif command == "cache-status":
                emit(command, cache_status(db), args.pretty)
            elif command in {"cache-refresh", "cache-rebuild"}:
                maintenance = cache_maintenance(db, command)
                if command == "cache-refresh":
                    maintenance["requested"] = {"background": args.background, "force": args.force}
                emit(command, maintenance, args.pretty)
            elif command == "export":
                export_chat_name = args.chat or args.talker
                if not export_chat_name:
                    raise ReaderError("export 需要 chat 或 --talker", "missing_required_argument")
                emit(
                    command,
                    export_chat(
                        db,
                        export_chat_name,
                        Path(args.path).expanduser(),
                        args.format,
                        args.limit,
                        args.offset,
                        args.after,
                        args.before,
                        args.keyword,
                        args.sender,
                        args.from_me,
                        args.kind_name,
                        args.base_kind,
                        args.force,
                    ),
                    args.pretty,
                )
            elif command == "announcements":
                emit(
                    command,
                    {"announcements": chatroom_announcements(db, args.chatroom_id, args.limit, args.after, args.before)},
                    args.pretty,
                )
            elif command == "sql":
                emit(command, readonly_sql(db, args.query, args.subdir, args.file, args.limit), args.pretty)
            elif command == "media":
                media_kinds = {args.kind_name} if args.kind_name else {"image", "voice", "video", "file", "sticker"}
                rows = special_messages(
                    db,
                    media_kinds,
                    args.limit + args.offset,
                    args.after,
                    args.before,
                    args.chat or args.talker,
                    args.local_id,
                )
                rows = filter_message_rows(rows, args.sender, args.from_me, args.kind_name, args.base_kind)
                server_id = args.server_id or args.message_server_id
                server_id_text = args.server_id_str or args.message_server_id_str
                if server_id is not None:
                    rows = [row for row in rows if int(row.get("server_id") or 0) == server_id]
                if server_id_text:
                    rows = [row for row in rows if str(row.get("server_id") or "") == server_id_text]
                for row in rows:
                    details = hardlink_resources(
                        db,
                        dict(row.get("metadata") or {}),
                        args.resource_family,
                        args.resource_type_raw,
                        args.include_local_paths,
                    )
                    if args.resource_family or args.resource_type_raw is not None:
                        if not details:
                            continue
                    row["resource_details"] = details
                    row["local_paths"] = sorted(
                        {path for detail in details for path in detail.get("local_paths", [])}
                    )
                    if args.debug or args.include_debug:
                        row["debug"] = {
                            "hardlink_db_configured": bool(db.hardlink_db),
                            "resource_root_count": len(db.resource_roots),
                        }
                if args.resource_family or args.resource_type_raw is not None:
                    rows = [row for row in rows if row.get("resource_details")]
                emit(
                    command,
                    {
                        "resources": rows[args.offset : args.offset + args.limit],
                        "coverage": (
                            "hardlink_index_and_existing_local_paths"
                            if db.hardlink_db and db.resource_roots
                            else "message_metadata_only; local resource paths require hardlink_db and resource_roots"
                        ),
                    },
                    args.pretty,
                )
            elif command == "transfers":
                emit(command, {"messages": special_messages(db, {"transfer"}, args.limit, args.after, args.before)}, args.pretty)
            elif command == "red-packets":
                rows = special_messages(
                    db,
                    {"red_packet"},
                    args.limit,
                    args.after,
                    args.before,
                    args.chat or args.talker,
                )
                rows = filter_message_rows(rows, args.sender)
                emit(
                    command,
                    {"messages": rows},
                    args.pretty,
                )
            elif command == "forward-history":
                emit(command, {"messages": special_messages(db, {"forward_chat"}, args.limit, args.after, args.before)}, args.pretty)
            elif command == "favorites":
                emit(command, {"favorites": favorites(db, args.limit, args.after, args.before)}, args.pretty)
            elif command in {"sns-feed", "sns-search"}:
                rows = sns_feed(
                    db,
                    args.limit,
                    args.offset,
                    args.after,
                    args.before,
                    args.keyword,
                    args.user,
                )
                emit(command, {"items": rows}, args.pretty)
            elif command == "sns-notifications":
                emit(
                    command,
                    {"notifications": sns_notifications(db, args.limit, args.after, args.before, args.include_read)},
                    args.pretty,
                )
            elif command == "notifications":
                emit(command, {"messages": notification_records(args.limit, args.after, args.keyword)}, args.pretty)
            else:
                raise ReaderError(f"尚未实现命令：{command}")
        return 0
    except ReaderError as exc:
        return fail(command, str(exc), code=exc.code, pretty=args.pretty)


if __name__ == "__main__":
    raise SystemExit(main())
