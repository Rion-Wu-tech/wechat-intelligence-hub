#!/usr/bin/env python3
"""Optional, explicit onboarding. Not part of the read-only reader contract."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time

import rion_wechat_reader as reader

ACCESS_REVISION = "2026-10-26.1"

# Fixed messages only: exceptions may contain private paths or provider material.
MATERIAL_ERRORS = {
    "invalid_json": "文件不是可读取的JSON；支持UTF-8/BOM、UTF-16/32，请检查导出格式。",
    "invalid_access_bundle": "材料结构不受支持；提供路径映射、schema-2 keys或Reader salt_keys对象。",
    "invalid_key": "需要64位raw key或96位raw key+salt；不能用API密钥、图片密钥或口令替代。",
    "invalid_key_parameters": "SQLCipher参数无效；核对导出工具的参数，勿猜测或关闭校验。",
    "key_salt_mismatch": "96位材料末尾的salt与映射条目不一致；核对账号和导出来源。",
    "conflicting_key_fields": "同一条目的key、enc_key、raw_key不一致；核对来源后重新导出。",
    "passphrase_requires_derivation": "收到的是口令，尚需由对应provider按每个库的salt派生；不要直接当raw key。",
    "empty_access_bundle": "材料文件没有数据库密钥；安装成功或空JSON不代表已经获取成功。",
    "unsafe_key_permissions": "材料权限不满足私有要求；Windows检查真实ACL，macOS检查文件权限。",
    "unsafe_access_bundle_path": "材料中的数据库路径超出所选目录；检查迁移路径，不自动扩大账号范围。",
    "access_bundle_not_compatible": "未打开任何加密库；核对账号、目录、salt和派生类型，此结果不能单独证明版本不支持。",
    "sqlcipher_driver_required": "当前Python缺少SQLCipher；先修复该运行环境，无需重新获取key。",
    "database_root_missing": "所选数据库目录不存在；检查路径迁移。",
    "database_root_required": "路径型材料需要指定本人的数据库目录。",
    "access_bundle_missing": "所选材料文件不存在；只提供本机路径，无需粘贴key。",
}


def material_diagnostic(exc: reader.ReaderError) -> dict:
    code = exc.code if exc.code in MATERIAL_ERRORS else "access_validation_failed"
    return {"code": code, "message": MATERIAL_ERRORS.get(code, "材料校验失败，检查本机输入，不上传原始文件。")}


def support_next_step(report: dict) -> str:
    """Return a stable, non-sensitive action code for support and onboarding."""
    if report.get("recovery_review_required") is True:
        return "review_wechat_recovery"
    state = report.get("state")
    if state == "ready" and report.get("live_database_read_ok") is True:
        return "use_existing_reader"
    material_error = report.get("material_error", {}).get("code")
    if material_error == "sqlcipher_driver_required":
        return "repair_runtime_dependency"
    if material_error == "unsafe_key_permissions":
        return "review_local_permissions"
    if material_error in {"database_root_missing", "database_root_required"}:
        return "select_account_or_database"
    if state == "ready_to_configure":
        return "verify_then_configure"
    if state == "dependency_required":
        return "repair_runtime_dependency"
    if state in {"account_selection_required", "needs_database_location", "database_missing", "database_layout_ambiguous"}:
        return "select_account_or_database"
    if state in {"filesystem_access_required", "unsafe_key_permissions"}:
        return "review_local_permissions"
    if state in {"partial", "scan_incomplete"}:
        return "review_coverage"
    if state in {"verification_failed", "invalid_access_material", "access_material_requires_review"}:
        return "review_existing_material"
    if state in {"needs_access", "acquisition_platform_not_supported", "provider_required", "existing_provider_material_available"}:
        return "review_access_options"
    return "manual_review"


def support_summary(report: dict) -> dict:
    """Public support payload: independently select fields, never copy raw data."""
    states = {"ready", "ready_to_configure", "needs_access", "dependency_required", "partial",
              "verification_failed", "unsafe_key_permissions", "needs_database_location",
              "database_layout_unsupported", "scan_incomplete", "account_selection_required",
              "access_material_requires_review", "acquisition_platform_not_supported",
              "existing_configuration_requires_review", "invalid_access_material", "database_missing",
              "filesystem_access_required", "database_layout_ambiguous", "configuration_check_failed",
              "provider_required", "existing_provider_material_available"}
    current_state = report.get("state")
    if current_state == "ready" and report.get("live_database_read_ok") is not True:
        current_state = "unknown"
    result = {"schema_version": 1, "access_helper_revision": ACCESS_REVISION,
              "state": current_state if current_state in states else "unknown",
              "live_database_read_ok": report.get("live_database_read_ok") is True,
              "recovery_review_required": report.get("recovery_review_required") is True,
              "next_step_code": support_next_step(report),
              "network_called": False, "configuration_changed": False}
    environment = report.get("environment", {})
    result["environment"] = {}
    for name in ("os_version", "wechat_version", "wechat_build", "python_version"):
        value = environment.get(name)
        if isinstance(value, str) and re.fullmatch(r"[0-9.]{1,40}", value):
            result["environment"][name] = value
    for name, allowed in (("system", {"Darwin", "Windows", "Linux"}),
                          ("architecture", {"arm64", "aarch64", "x86_64", "AMD64", "x86", "i386", "i686"})):
        value = environment.get(name)
        if value in allowed:
            result["environment"][name] = value
    result["counts"] = {name: value for name, value in report.get("counts", {}).items()
                        if name in {"configured_message_databases", "missing_databases", "core_databases_without_key",
                                    "account_candidates", "session", "contact", "messages", "scanned_databases", "unresolved_databases"}
                        and type(value) is int and 0 <= value <= 1000000}
    preview = report.get("material_preview")
    if isinstance(preview, dict):
        code = preview.get("material_error", {}).get("code")
        result["material_preview"] = {
            "state": preview.get("state") if preview.get("state") in states else "unknown",
            "error_code": code if code in MATERIAL_ERRORS else "none_or_unknown",
            "next_step_code": support_next_step(preview)}
    known_tools = {tool for tool, _, _ in KNOWN_MATERIAL_LOCATIONS}
    result["known_material_tools"] = sorted(item["tool"] for item in known_material_candidates()
                                            if item["tool"] in known_tools)
    result["last_attempt_diagnostics"] = safe_diagnostics(report.get("last_attempt", {}).get("diagnostics", {}))
    attempt_state = report.get("last_attempt", {}).get("state")
    result["last_attempt_state"] = attempt_state if attempt_state in WORKER_STATES else "none_or_unknown"
    return result


def diagnose(args: argparse.Namespace) -> dict:
    report = access_status(args.config, args.database_root, max(1, args.max_files))
    if args.source:
        # Use an empty temporary config to check explicitly supplied material
        # independently of a working installation, without replacing that setup.
        with reader.private_temporary_directory(prefix="rion-access-diagnose-") as directory:
            preview = argparse.Namespace(**vars(args))
            preview.config = Path(directory).resolve() / "config.json"
            preview.apply = False
            preview.provider = None
            preview.sha256 = ""
            preview.confirm_reviewed_provider = False
            preview.confirm_side_effects = False
            try:
                if not private_path(args.source).is_file():
                    raise reader.ReaderError("材料文件不存在", "access_bundle_missing")
                report["material_preview"] = onboard(preview)
            except reader.ReaderError as exc:
                report["material_preview"] = {"state": "access_material_requires_review", "material_error": material_diagnostic(exc)}
    report["network_called"] = False
    report["configuration_changed"] = False
    if getattr(args, "support_summary", False):
        return support_summary(report)
    if args.jev_request:
        current = report.get("material_preview", report)
        allowed_states = {"ready", "ready_to_configure", "needs_access", "dependency_required", "partial",
                          "access_material_requires_review", "invalid_access_material", "unsafe_key_permissions",
                          "account_selection_required", "verification_failed", "acquisition_platform_not_supported",
                          "needs_database_location", "database_layout_unsupported", "scan_incomplete"}
        state = current.get("state")
        error = current.get("material_error", {}).get("code")
        signals = safe_diagnostics(report.get("last_attempt", {}).get("diagnostics", {})).get("signals", [])
        report["jev_request"] = {
            "model": "jev-latest",
            "state": {"access_state": state if state in allowed_states else "unknown",
                      "material_error": error if error in MATERIAL_ERRORS else "none",
                      "historical_signals": signals, "recovery_review_required": bool(report.get("recovery_review_required")),
                      "system": report.get("environment", {}).get("system") if report.get("environment", {}).get("system") in {"Darwin", "Windows", "Linux"} else "unknown"},
            "questions": {"next_stage": {"type": "choice",
                "instructions": "Select the next diagnostic stage. Recovery flag requires recovery review first. Current access_state outranks historical signals. ready means reuse; ready_to_configure requires explicit configuration confirmation. This is advice only; never infer permission to acquire keys or compatibility from a model answer.",
                "criteria": {"reuse": "Current database access verified", "configure": "Material verified; configuration awaits confirmation",
                             "recovery": "Client recovery requires review; do not acquire again", "dependencies": "SQLCipher or runtime missing",
                             "permissions": "File ACL or OS access", "material": "JSON, raw key format or salt",
                             "account": "Database location or selected account", "provider": "No material or acquisition unavailable",
                             "manual_review": "Insufficient or conflicting evidence"}}},
        }
    return report

DEBUGGER_PROBE = """
import lldb
import sys
required = {
    'SBLaunchInfo': ('SetUserID', 'SetGroupID', 'SetEnvironmentEntries', 'GetLaunchFlags', 'SetLaunchFlags', 'SetListener'),
    'SBProcessInfo': ('IsValid', 'UserIDIsValid', 'EffectiveUserIDIsValid', 'GroupIDIsValid', 'EffectiveGroupIDIsValid',
                      'GetUserID', 'GetEffectiveUserID', 'GetGroupID', 'GetEffectiveGroupID'),
    'SBProcess': ('GetProcessInfo', 'GetState', 'GetStopID', 'Continue', 'Kill'),
    'SBListener': ('WaitForEvent',),
    'SBDebugger': ('Create', 'SetAsync', 'CreateTarget', 'Destroy'),
}
missing = any(not callable(getattr(getattr(lldb, cls, None), method, None))
              for cls, methods in required.items() for method in methods)
missing = missing or any(not hasattr(lldb, name) for name in
                        ('SBEvent', 'SBError', 'eLaunchFlagStopAtEntry', 'eStateStopped',
                         'eStateExited', 'eStateCrashed', 'eStateDetached'))
sys.exit(42 if missing else 0)
"""


class AccessError(Exception):
    def __init__(self, code: str, diagnostics: dict | None = None):
        super().__init__(code)
        self.diagnostics = diagnostics or {}


def private_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise AccessError("symlink_path_rejected")
    return path


def provider_digest(path: Path) -> str:
    path = private_path(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AccessError("provider_unavailable")
    if path.stat().st_mode & 0o022:
        raise AccessError("provider_writable_by_others")
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_root(root: Path) -> Path:
    root = private_path(root)
    if not root.is_dir():
        raise AccessError("database_root_missing")
    accounts = [p for p in root.glob("*/db_storage") if p.is_dir()]
    if (root / "db_storage").is_dir():
        accounts.append(root / "db_storage")
    if len(accounts) > 1:
        raise AccessError("account_selection_required")
    return root


def provider_roots(root: Path) -> tuple[Path, Path]:
    """Reader accepts storage roots; wxkey's PBKDF probe appends db_storage."""
    root = check_root(root)
    if root.name == "db_storage":
        return root.parent, root
    if (root / "db_storage").is_dir():
        return root, private_path(root / "db_storage")
    accounts = [private_path(p) for p in root.glob("*/db_storage") if p.is_dir()]
    if len(accounts) == 1:
        return accounts[0].parent, accounts[0]
    raise AccessError("provider_account_root_required")


# Only fixed markers/counters survive. Provider output may contain keys and paths.
PROVIDER_MARKERS = (
    ("using wechat-cli shadow copy", "shadow_prepare"),
    ("Extracting keys and writing config", "passive_scan"),
    ("PBKDF fallback: launching", "pbkdf_launch"),
    ("key config written", "material_written"),
    ("prepare shadow WeChat:", "shadow_prepare_failed"),
    ("lldb python path unavailable", "debugger_unavailable"),
    ("No module named 'lldb'", "debugger_unavailable"),
    ("launch failed", "target_launch_failed"),
    ("target_launch_failed", "target_launch_failed"),
    ("shadow_policy_blocked", "shadow_policy_blocked"),
    ("official_wechat_stop_failed", "official_wechat_stop_failed"),
    ("official_wechat_scan_failed", "official_wechat_scan_failed"),
    ("official_wechat_process_unverified", "official_wechat_process_unverified"),
    ("other_wechat_install_running", "other_wechat_install_running"),
    ("pbkdf_process_scan_failed", "pbkdf_process_scan_failed"),
    ("pbkdf_process_unverified", "pbkdf_process_unverified"),
    ("pbkdf_foreign_wechat_process", "pbkdf_foreign_wechat_process"),
    ("pbkdf_process_stop_failed", "pbkdf_process_stop_failed"),
    ("pbkdf_shadow_path_invalid", "pbkdf_shadow_path_invalid"),
    ("pbkdf_shadow_signature_unverified", "pbkdf_shadow_signature_unverified"),
    ("target_identity_mismatch", "target_identity_mismatch"),
    ("target_identity_unverified", "target_identity_unverified"),
    ("target_entry_timeout", "target_entry_timeout"),
    ("target_continue_failed", "target_continue_failed"),
    ("original_reopen_failed", "original_reopen_failed"),
    ("original_reopen_requested", "original_reopen_requested"),
    ("No PBKDF calls were observed", "no_derivation_observed"),
    ("none of its salts matched this DB root", "account_salt_mismatch"),
    ("none mapped to local DB salts", "account_salt_mismatch"),
    ("no derived key verified page-1 HMAC", "key_verification_failed"),
    ("partial key coverage", "partial_key_coverage"),
)
PROVIDER_SIGNALS = {code for _, code in PROVIDER_MARKERS}
PREFLIGHT_ERRORS = {
    "database_read_permission_denied", "database_files_not_found", "database_header_unreadable",
    "database_not_regular_file", "database_preflight_limit_exceeded", "database_preflight_failed",
    "symlink_path_rejected", "debugger_unavailable", "provider_dependency_check_failed",
    "provider_account_root_required",
    "debugger_api_incompatible", "worker_preflight_failed",
    "other_user_wechat_running", "other_wechat_install_running",
}
WORKER_STATES = PREFLIGHT_ERRORS | {"provider_finished", "provider_failed", "provider_start_failed", "provider_timeout_cleanup_required"}
SIGNAL_ACTIONS = {
    "account_salt_mismatch": "核对正在登录的账号与所选db_storage是否一致；不要把别的账号材料混入本账号。",
    "no_derivation_observed": "未观察到目标派生调用；先核对登录状态、目标进程身份与当前版本兼容性，不能据此断言key不存在。",
    "key_verification_failed": "已捕获调用但验证未通过；核对当前版本派生参数，不能把捕获结果直接写成有效key。",
    "target_launch_failed": "目标启动失败；检查副本启动策略和实际UID，HOME指向普通用户不代表进程UID已经降权。",
    "target_identity_mismatch": "实际目标UID/GID与账号所有者不一致；停止获取，检查provider启动身份，不能继续用root目标重试。",
    "target_identity_unverified": "无法确认目标身份或账号目录归属；先核对所选账号和LLDB兼容性，不绕过身份检查。",
    "target_entry_timeout": "目标未在启动入口按时暂停；检查副本启动和LLDB事件机制，先恢复微信再决定是否重试。",
    "target_continue_failed": "调试目标无法继续运行；检查恢复状态与当前系统的调试兼容性。",
    "original_reopen_failed": "工具报告原微信重开失败；请手动打开官方微信并确认登录，不自动清除恢复锁。",
    "original_reopen_requested": "工具仅报告已请求重开原微信，不代表已经登录或恢复读取；请检查后再处理恢复锁。",
    "shadow_prepare_failed": "副本准备失败；检查官方微信是否已恢复，不自动关闭SIP或重签原应用。",
    "shadow_policy_blocked": "系统启动策略拒绝临时副本；本次不应退出官方微信，也不应关闭系统保护或循环重试。",
    "official_wechat_stop_failed": "原微信未在限定时间内退出，或仍有同名进程；先核对官方客户端与残留进程，不强制杀进程、不重复获取。",
    "official_wechat_scan_failed": "副本准备前无法可靠列出微信进程；本轮已停止，先核对进程查询权限，不靠重复取key解决。",
    "official_wechat_process_unverified": "副本准备前无法核实同名进程属于官方微信；本轮已停止，先人工核对该实例。",
    "other_wechat_install_running": "发现另一份微信安装实例；本轮不退出它，先确认各实例的归属和登录状态。",
    "pbkdf_process_scan_failed": "兜底获取无法可靠列出微信进程；停止操作，确认本机进程查询权限后再评估。",
    "pbkdf_process_unverified": "兜底获取无法核实某个微信进程的位置；未向进程发送退出信号，先确认是否有其他实例。",
    "pbkdf_foreign_wechat_process": "检测到不属于官方客户端或本次副本的微信进程；未向进程发送退出信号，先人工核对实例。",
    "pbkdf_process_stop_failed": "已核实的微信进程未正常退出或退出信号失败；不要强制杀进程或自动重试，先检查官方微信是否恢复。",
    "pbkdf_shadow_path_invalid": "旧临时副本的位置、目录类型或可执行文件不可靠；本轮不复用它，先由人工检查受管目录，不修改原微信。",
    "pbkdf_shadow_signature_unverified": "旧临时副本的签名未通过检查；本轮不启动它，先核对来源，不关闭系统保护。",
    "debugger_unavailable": "核对Command Line Tools和实际Apple Python的LLDB导入能力。",
    "partial_key_coverage": "可能只得到部分材料；显式connect只读验证覆盖范围，不直接重新获取或宣称全量可读。",
}


def diagnostic_actions(diagnostics: dict) -> list[str]:
    return [SIGNAL_ACTIONS[s] for s in safe_diagnostics(diagnostics).get("signals", []) if s in SIGNAL_ACTIONS]


def provider_signals(chunk: bytes, diagnostics: dict) -> None:
    text = chunk.decode("utf-8", errors="replace")
    observed = set(diagnostics.get("signals", []))
    for marker, code in PROVIDER_MARKERS:
        if marker in text:
            observed.add(code)
    diagnostics["signals"] = sorted(observed)


def safe_diagnostics(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get("phase") in {"worker_preflight", "database_preflight", "debugger_preflight", "provider_start", "provider_exit", "provider_timeout"}:
        result["phase"] = value["phase"]
    for name in ("provider_started", "timed_out"):
        if type(value.get(name)) is bool:
            result[name] = value[name]
    for name in ("exit_code", "readable_database_count"):
        if type(value.get(name)) is int and -1000000 <= value[name] <= 1000000:
            result[name] = value[name]
    signals = value.get("signals", [])
    if isinstance(signals, list):
        result["signals"] = sorted({s for s in signals if isinstance(s, str) and s in PROVIDER_SIGNALS})
    return result


def read_worker_result(path: Path) -> dict:
    path = private_path(path)
    if path.stat().st_size > 16384:
        raise AccessError("worker_result_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("state"), str) or value["state"] not in WORKER_STATES:
        raise AccessError("worker_result_invalid")
    return {"state": value["state"], "diagnostics": safe_diagnostics(value.get("diagnostics"))}


def access_status(config: Path, root: Path | None, max_files: int) -> dict:
    """Support output is an allowlist, not a redacted copy of private logs."""
    plan = reader.access_plan(private_path(config), private_path(root) if root else None, max_files=max_files)
    environment = dict(plan["environment"])
    environment["access_helper_revision"] = ACCESS_REVISION
    environment["os_version"] = platform.mac_ver()[0] if platform.system() == "Darwin" else platform.release()
    app_info = Path("/Applications/WeChat.app/Contents/Info.plist")
    if platform.system() == "Darwin" and app_info.is_file():
        try:
            with app_info.open("rb") as handle:
                info = plistlib.load(handle)
            for field, name in (("CFBundleShortVersionString", "wechat_version"), ("CFBundleVersion", "wechat_build")):
                value = str(info.get(field, ""))
                if re.fullmatch(r"[0-9.]{1,40}", value):
                    environment[name] = value
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    parent = private_path(Path.home() / ".config/rion-wechat-reader/access-runs")
    locked = os.path.lexists(parent / "recovery-required.lock")
    result = {"schema_version": 1, "state": plan["state"], "environment": environment,
              "live_database_read_ok": plan["live_database_read_ok"], "counts": plan["counts"],
              "recovery_review_required": locked, "provider_executed": False,
              "next_actions": plan["next_actions"], "scope": plan["scope"]}
    candidates = list(parent.glob("run-*/worker-result.json")) if parent.is_dir() else []
    if candidates:
        latest = max(candidates, key=lambda p: p.lstat().st_mtime_ns)
        try:
            result["last_attempt"] = read_worker_result(latest)
            result["last_attempt"]["suggested_checks"] = diagnostic_actions(result["last_attempt"]["diagnostics"])
        except (AccessError, OSError, ValueError):
            result["last_attempt"] = {"state": "worker_result_invalid"}
    if locked:
        result["recovery_check"] = recovery_process_check()
        result["next_actions"] = ["先查看last_attempt定位阶段，核对官方微信和残留进程；不要重复获取或自动删恢复锁。已有生成材料可在确认来源后显式connect验证。"]
    return result


def recovery_process_check() -> dict:
    """Read executable names only; never expose process arguments, paths or PIDs."""
    result = {"state": "unavailable", "login_verified": False,
              "all_acquisition_processes_verified_stopped": False}
    if platform.system() != "Darwin":
        return result
    try:
        observed = subprocess.run(["/bin/ps", "-axo", "uid=,comm="], capture_output=True,
                                  text=True, timeout=5, check=True).stdout
        if len(observed) > 2 * 1024 * 1024:
            return result
        counts = {"official_wechat": 0, "shadow_wechat": 0, "other_wechat": 0,
                  "known_provider": 0}
        official = "/Applications/WeChat.app/Contents/MacOS/WeChat"
        shadow = str(Path.home() / "Library/Application Support/wx-mcp/WeChat-shadow.app/Contents/MacOS/WeChat")
        for line in observed.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                return result
            uid, executable = int(fields[0]), fields[1]
            if Path(executable).name == "wxkey":
                counts["known_provider"] += 1
            if Path(executable).name != "WeChat":
                continue
            if executable == official and uid == os.getuid():
                counts["official_wechat"] += 1
            elif executable == shadow:
                counts["shadow_wechat"] += 1
            else:
                counts["other_wechat"] += 1
        result.update(state="observed", counts=counts)
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def finish_recovery(confirm_wechat_ready: bool, confirm_no_acquisition: bool) -> dict:
    if platform.system() != "Darwin":
        raise AccessError("recovery_platform_not_supported")
    if not confirm_wechat_ready or not confirm_no_acquisition:
        raise AccessError("recovery_confirmation_required")
    if os.geteuid() == 0:
        raise AccessError("launch_from_normal_user_session")
    lock = private_path(Path.home() / ".config/rion-wechat-reader/access-runs/recovery-required.lock")
    if not lock.exists():
        return {"state": "no_recovery_lock", "lock_removed": False, "provider_executed": False}
    original = lock.stat()
    if not stat.S_ISREG(original.st_mode) or original.st_uid != os.getuid() or original.st_mode & 0o077:
        raise AccessError("recovery_lock_requires_manual_review")
    check = recovery_process_check()
    if check["state"] != "observed":
        raise AccessError("recovery_process_check_unavailable")
    counts = check["counts"]
    if any(counts[name] for name in ("shadow_wechat", "other_wechat", "known_provider")):
        raise AccessError("recovery_processes_still_present")
    if not counts["official_wechat"]:
        raise AccessError("official_wechat_not_running")
    current = lock.stat()
    if (current.st_dev, current.st_ino, current.st_mtime_ns) != (original.st_dev, original.st_ino, original.st_mtime_ns):
        raise AccessError("recovery_lock_changed")
    lock.unlink()
    return {"state": "recovery_review_completed", "lock_removed": True, "provider_executed": False,
            "wechat_ready_confirmed_by_user": True, "no_acquisition_confirmed_by_user": True,
            "note": "仅移除本次恢复锁，不获取key、不删除配置、不重启微信。"}


def preflight_debugger() -> None:
    """Check the same Apple Python/LLDB pair used by the reviewed provider."""
    try:
        result = subprocess.run(["/usr/bin/lldb", "-P"], capture_output=True, text=True, timeout=15, check=False)
        module_path = result.stdout.strip()
        if result.returncode or not module_path or not Path(module_path).is_dir():
            raise AccessError("debugger_unavailable")
        probe = subprocess.run(["/usr/bin/python3", "-c", DEBUGGER_PROBE], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                               env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": module_path}, check=False)
        if probe.returncode == 42:
            raise AccessError("debugger_api_incompatible")
        if probe.returncode:
            raise AccessError("debugger_unavailable")
    except (OSError, subprocess.SubprocessError) as exc:
        raise AccessError("provider_dependency_check_failed") from exc


def inspect(provider: Path, expected: str, root: Path) -> dict:
    digest = provider_digest(provider)
    check_root(root)
    return {
        "state": "review_required",
        "experimental": True,
        "system": platform.system(),
        "provider_sha256": digest,
        "digest_matches": bool(expected) and digest == expected.lower(),
        "provider_executed": False,
        "password_route": "macOS administrator authorization; no password passed to this tool",
        "source_review_required": "Verify this exact build skips Keychain when root and honors WXKEY_NO_ELEVATE.",
        "effects_requiring_confirmation": ["process_debug_access", "quit_and_restart_wechat", "resign_shadow_copy"],
        "not_guaranteed": ["new_machine_compatibility", "automatic_gui_cleanup_after_timeout", "zero_account_risk"],
    }


def preflight_database_access(root: Path, max_files: int = 500) -> int:
    """Run in the actual worker identity, before a provider can quit WeChat."""
    count = 0
    def denied(error):
        raise AccessError("database_read_permission_denied") from error
    try:
        for directory, dirs, files in os.walk(root, onerror=denied, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise AccessError("symlink_path_rejected")
            for name in files:
                if not name.lower().endswith(".db"):
                    continue
                path = private_path(Path(directory) / name)
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise AccessError("database_not_regular_file")
                    if len(os.read(fd, 16)) < 16:
                        raise AccessError("database_header_unreadable")
                finally:
                    os.close(fd)
                count += 1
                if count > max_files:
                    raise AccessError("database_preflight_limit_exceeded")
    except PermissionError as exc:
        raise AccessError("database_read_permission_denied") from exc
    except OSError as exc:
        raise AccessError("database_preflight_failed") from exc
    if not count:
        raise AccessError("database_files_not_found")
    return count


def bounded_provider(argv: list[str], env: dict[str, str], timeout: int, *, diagnostics: dict | None = None) -> str:
    """Drain bounded chunks without storing logs; kill only our process group."""
    diagnostics = diagnostics if diagnostics is not None else {}
    process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    deadline = time.monotonic() + timeout
    tail = b""
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        provider_signals(tail + chunk, diagnostics)
                        tail = (tail + chunk)[-256:]
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        diagnostics.update(phase="provider_exit", exit_code=code, timed_out=False)
        return "provider_finished" if code == 0 else "provider_failed"
    except subprocess.TimeoutExpired:
        if diagnostics is not None:
            diagnostics.update(phase="provider_timeout", timed_out=True)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return "provider_timeout_cleanup_required"
    finally:
        process.stdout.close()


def worker(args: argparse.Namespace) -> int:
    # This path is launched only through macOS's authorization prompt. No
    # password is requested, read from Keychain, or supplied to sudo by us.
    import pwd
    if os.geteuid() != 0 or args.uid <= 0:
        raise AccessError("administrator_worker_required")
    owner = pwd.getpwuid(args.uid)
    home = private_path(Path(owner.pw_dir))
    run_dir = private_path(args.run_dir)
    expected_parent = home / ".config" / "rion-wechat-reader" / "access-runs"
    if run_dir.parent != expected_parent or run_dir.stat().st_uid != args.uid or stat.S_IMODE(run_dir.stat().st_mode) != 0o700:
        raise AccessError("invalid_worker_directory")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home), "USER": owner.pw_name,
        "WXKEY_ORIG_HOME": str(home), "WXKEY_ORIG_USER": owner.pw_name,
        "WXKEY_NO_ELEVATE": "1", "WXKEY_ELEVATED": "1",
        "WXKEY_SETUP_TIMEOUT": "180s", "WXKEY_PBKDF_PROBE_TIMEOUT": "180s",
    }
    diagnostics = {"phase": "worker_preflight", "provider_started": False}
    try:
        if provider_digest(args.provider) != args.sha256.lower():
            raise AccessError("provider_digest_mismatch")
        source = private_path(home / ".config" / "wxcli" / "config.json")
        if source.exists():
            raise AccessError("provider_config_exists_use_connect")
        account_root, root = provider_roots(args.database_root)
        # The reviewed provider can quit WeChat globally. Refuse shared-user runs.
        processes = subprocess.run(["/bin/ps", "-axo", "uid=,comm="], capture_output=True, text=True, check=True).stdout
        official_wechat = "/Applications/WeChat.app/Contents/MacOS/WeChat"
        for line in processes.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or Path(fields[1]).name != "WeChat":
                continue
            if int(fields[0]) != args.uid:
                raise AccessError("other_user_wechat_running")
            if fields[1] != official_wechat:
                raise AccessError("other_wechat_install_running")
        diagnostics["phase"] = "database_preflight"
        diagnostics["readable_database_count"] = preflight_database_access(root, getattr(args, "max_files", 500))
        diagnostics["phase"] = "debugger_preflight"
        preflight_debugger()
    except AccessError as exc:
        state = str(exc) if str(exc) in PREFLIGHT_ERRORS else "worker_preflight_failed"
    except (OSError, ValueError, subprocess.SubprocessError):
        state = "worker_preflight_failed"
    else:
        diagnostics["phase"] = "provider_start"
        try:
            diagnostics["provider_started"] = True
            state = bounded_provider([str(args.provider), "bootstrap", "--root", str(account_root)], env, args.timeout,
                                     diagnostics=diagnostics)
        except (OSError, subprocess.SubprocessError):
            diagnostics["provider_started"] = False
            state = "provider_start_failed"
    # Exclusive creation avoids replacing any existing recovery result.
    fd = os.open(str(run_dir / "worker-result.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"state": state, "diagnostics": diagnostics}, handle)
    os.chown(run_dir / "worker-result.json", args.uid, owner.pw_gid)
    return 0 if state == "provider_finished" else 1


def connect(source: Path | None, root: Path, config: Path, max_files: int = 500) -> dict:
    """Verify in a fresh private generation, then publish one config atomically."""
    if os.environ.get("RION_WECHAT_READER_KEYS"):
        raise AccessError("key_environment_override_requires_removal")
    source = private_path(source) if source is not None else None
    root, config = check_root(root), private_path(config)
    if config.exists():
        raise AccessError("reader_config_exists_not_overwritten")
    reader.prepare_private_output(config, chmod_parent=False)
    if not reader.safe_mode(config.parent):
        raise AccessError("config_directory_not_private")
    run_dir = Path(tempfile.mkdtemp(prefix="verified-access-", dir=config.parent))
    keys = run_dir / "keys.json"
    staged_config = run_dir / "config.json"
    published = False
    try:
        if platform.system() == "Windows" and not reader.windows_private_acl(run_dir, initialize=True):
            raise reader.ReaderError(reader.permission_help(), "unsafe_key_permissions")
        verified = 0
        if source is not None:
            imported = reader.import_access_bundle(source, root, keys, force=False, verify=True, max_files=max_files)
            verified = imported["verification"]["matched_database_count"]
        else:
            reader.secure_write_json(keys, {"keys": {}}, force=False)
        plan = reader.access_plan(staged_config, root, keys, max_files)
        if plan["state"] != "ready_to_configure":
            return {"state": plan["state"], "configured": False, "counts": plan["counts"]}
        setup = reader.setup_cli(staged_config, root, keys, "", max_files, False)
        if setup["doctor"]["summary"] != "ready":
            raise AccessError("reader_verification_failed")
        # A hard link is an atomic no-replace publication on the same volume.
        os.link(staged_config, config)
        published = True
        staged_config.unlink()
        return {"state": "ready", "configured": True,
                "verified_database_count": verified,
                "live_database_read_ok": True,
                "scope": "local_databases_only", "next_step": "抽检所需私聊、群聊、标签和时间范围。"}
    finally:
        if not published:
            for path in (staged_config, keys):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            with contextlib.suppress(OSError):
                run_dir.rmdir()


def run(args: argparse.Namespace) -> dict:
    if platform.system() != "Darwin":
        raise AccessError("acquisition_platform_not_supported")
    if os.geteuid() == 0:
        raise AccessError("launch_from_normal_user_session")
    if not args.confirm_reviewed_provider or not args.confirm_side_effects:
        raise AccessError("explicit_confirmation_required")
    if os.environ.get("RION_WECHAT_READER_KEYS"):
        raise AccessError("key_environment_override_requires_removal")
    if not args.sha256 or provider_digest(args.provider) != args.sha256.lower():
        raise AccessError("provider_digest_mismatch")
    root, config = check_root(args.database_root), private_path(args.config)
    source = private_path(Path.home() / ".config" / "wxcli" / "config.json")
    if config.exists():
        raise AccessError("reader_config_exists_not_overwritten")
    if source.exists():
        raise AccessError("provider_config_exists_use_connect")
    if reader.sqlcipher_driver()[0] is None:
        raise AccessError("sqlcipher_driver_required")
    provider_roots(root)
    # mkdir(parents=True) does not apply mode to intermediate directories.
    # Prepare both private roots before authorization or acquisition begins.
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(config.parent.stat().st_mode) & 0o077:
        raise AccessError("config_directory_not_private")
    state_root = private_path(Path.home() / ".config" / "rion-wechat-reader")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(state_root.stat().st_mode) & 0o077:
        raise AccessError("run_directory_not_private")
    parent = private_path(state_root / "access-runs")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise AccessError("run_directory_not_private")
    lock = parent / "recovery-required.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AccessError("previous_run_requires_review")
    os.close(fd)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    argv = [sys.executable, str(Path(__file__).resolve()), "_worker", "--provider", str(args.provider.absolute()),
            "--sha256", args.sha256, "--database-root", str(root), "--run-dir", str(run_dir),
            "--uid", str(os.getuid()), "--timeout", str(args.timeout), "--max-files", str(args.max_files)]
    command = "exec " + shlex.join(argv) + " >/dev/null 2>&1"
    script = "do shell script " + json.dumps(command, ensure_ascii=False) + " with administrator privileges"
    try:
        authorization = subprocess.run(["/usr/bin/osascript", "-e", script], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=args.timeout + 180, check=False,
                                       env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        result_file = run_dir / "worker-result.json"
        if not result_file.is_file():
            raise AccessError("authorization_or_worker_failed_review_required")
        worker_result = read_worker_result(result_file)
        state = worker_result["state"]
        if authorization.returncode != 0 or state != "provider_finished":
            raise AccessError(state, worker_result["diagnostics"])
        if not source.is_file() or source.stat().st_uid != os.getuid():
            raise AccessError("provider_material_owner_invalid")
        result = connect(source, root, config, args.max_files)
        # Database verification does not prove that the GUI recovered or that
        # out-of-group acquisition processes have exited.
        result["recovery_review_required"] = True
        if result["state"] == "ready":
            result["next_step"] = "数据库已可读；确认官方微信正常登录、无残留获取进程后，再显式finish-recovery完成恢复检查。不要重新获取。"
        return result
    except subprocess.TimeoutExpired:
        # An authorized child may still be cleaning up. Never silently retry.
        raise AccessError("authorization_timeout_review_required")


# Fixed output locations of known key tools, relative to HOME. Only existence is
# checked; contents are read later and only when the user selects one via --source.
KNOWN_MATERIAL_LOCATIONS = (
    ("wxkey", ".config/wxcli/config.json", True),
    ("wechat-cli", ".wechat-cli/all_keys.json", True),
    ("wcdb-key-tool", ".wcdb-key-tool/wechat-passphrase.json", False),
)


def known_material_candidates() -> list[dict]:
    home = Path.home()
    return [{"tool": tool, "location": f"~/{relative}", "importable": importable}
            for tool, relative, importable in KNOWN_MATERIAL_LOCATIONS
            if os.path.lexists(home / relative)]


def onboard(args: argparse.Namespace) -> dict:
    """Idempotent workflow: inspect, reuse, connect, or request acquisition consent."""
    config = private_path(args.config)
    source = private_path(args.source) if args.source else None
    root = private_path(args.database_root) if args.database_root else None
    limit = max(1, args.max_files)
    plan = reader.access_plan(config, root, source, limit)
    outcome = {
        "workflow": "local_wechat_onboarding", "state": plan["state"],
        "live_database_read_ok": plan["live_database_read_ok"],
        "scope": plan["scope"], "counts": plan["counts"],
        "performed": dict(plan["performed"]), "next_actions": plan["next_actions"],
    }
    if plan["state"] == "ready":
        outcome["reused_existing_configuration"] = True
        outcome["recovery_review_required"] = os.path.lexists(Path.home() / ".config/rion-wechat-reader/access-runs/recovery-required.lock")
        if outcome["recovery_review_required"]:
            outcome["next_actions"] = ["现有数据库可读，继续复用；上次接入的微信恢复检查尚未完成，先status，不要重新获取。"]
        return outcome
    if config.exists():
        outcome.update(state="existing_configuration_requires_review", diagnostic_state=plan["state"],
                       next_actions=["先修复或显式选择另一份配置；不会覆盖已有配置，也不会因读失败自动重新取key。"])
        return outcome
    # Relative-path bundles need normalization before discovery can match them.
    # Preview uses a disposable private directory, never publishes configuration.
    if source is not None and plan["state"] in {"verification_failed", "partial", "needs_access"}:
        material = reader.load_json(source)
        bundled = material.get("database_root") or material.get("db_root")
        material_root = root or (Path(str(bundled)).expanduser() if bundled else reader.default_wechat_root())
        if material_root is not None:
            material_root = check_root(material_root)
            with reader.private_temporary_directory(prefix="rion-access-preview-") as directory:
                normalized = Path(directory) / "keys.json"
                try:
                    reader.import_access_bundle(source, material_root, normalized, force=False, verify=True, max_files=limit)
                    plan = reader.access_plan(Path(directory) / "config.json", material_root, normalized, limit)
                except reader.ReaderError as exc:
                    outcome.update(state="access_material_requires_review",
                                   material_error=material_diagnostic(exc),
                                   next_actions=[material_diagnostic(exc)["message"]])
                    return outcome
            root = material_root
            outcome.update(state=plan["state"], counts=plan["counts"], scope=plan["scope"], next_actions=plan["next_actions"])
    if plan["state"] not in {"ready_to_configure", "needs_access"}:
        return outcome
    if os.environ.get("RION_WECHAT_READER_KEYS"):
        raise AccessError("key_environment_override_requires_removal")

    # Only read the caller's material or the Reader's own default, not another
    # application's private config. access_plan already checked its permissions.
    selected_keys = source or private_path(Path("~/.config/rion-wechat-reader/keys.json"))
    material = reader.load_json(selected_keys)
    if root is None:
        bundled = material.get("database_root") or material.get("db_root")
        root = Path(str(bundled)).expanduser() if bundled else reader.default_wechat_root()
    if root is None:
        outcome.update(state="needs_database_location", next_actions=["确认本人账号和数据库目录后继续。"])
        return outcome
    root = check_root(root)

    if plan["state"] == "ready_to_configure":
        if not args.apply:
            outcome["next_actions"] = ["已验证输入；加--apply继续导入和配置，不需要获取新key。"]
            return outcome
        result = connect(selected_keys if selected_keys.is_file() else None, root, config, limit)
        outcome.update(result)
        outcome["performed"]["configuration_write"] = result.get("configured", False)
        return outcome

    if source is not None:
        outcome.update(state="access_material_requires_review", next_actions=["指定材料尚不能读取数据库，先检查该文件和账号，不自动改走获取路线。"])
        return outcome
    found = known_material_candidates()
    if found:
        importable = [item for item in found if item["importable"]]
        actions = ["检测到本机已知工具的输出文件，仅检查了是否存在，未读取内容或确认可用。确认属于所选账号后，以onboard --source显式指定该文件先验证；不要重新获取或覆盖它。"]
        if not importable:
            actions = ["检测到的材料是口令格式，需要按各数据库salt派生，当前助手尚不能直接导入；不要因此重新获取，先保留该文件。"]
        outcome.update(state="existing_provider_material_available",
                       material_candidates=found, next_actions=actions)
        return outcome
    if platform.system() != "Darwin":
        outcome.update(state="acquisition_platform_not_supported", next_actions=["此助手尚未实现本系统获取；可以验证用户已有的授权材料。"])
        if platform.system() == "Windows":
            outcome["windows_route"] = {
                "status": "community_report_not_integrated",
                "reported_dll_version": "4.1.13.12",
                "reference": "skills/wechat-cli/references/windows-access.md",
                "acquisition_performed": False,
            }
            outcome["next_actions"] = [
                "Windows获取仍未合入；先看windows-access指引，区分依赖、ACL、转换和校验失败。",
                "已有本人材料先验证复用。社区4.1.13.12成功案例不代表本机兼容，不删平台判断、不自动运行群友脚本。",
            ]
        return outcome
    if reader.sqlcipher_driver()[0] is None:
        outcome.update(state="dependency_required", next_actions=["先为当前CLI运行环境安装SQLCipher，再继续。"])
        return outcome
    if args.provider is None:
        outcome.update(state="provider_required", next_actions=["由Codex按接入指引核验并准备固定版本的本地获取工具，无需用户手工找key。"])
        return outcome
    inspection = inspect(args.provider, args.sha256, root)
    outcome["provider_review"] = inspection
    if not inspection["digest_matches"]:
        outcome.update(state="provider_review_required", next_actions=["核验源码和具体构建，再传入已审核的SHA256；不要自动接受现场计算出的哈希。"])
        return outcome
    if not args.apply or not args.confirm_reviewed_provider or not args.confirm_side_effects:
        outcome.update(state="authorization_required", next_actions=["向用户确认进程访问、微信退出/重启和副本重签名；确认后才继续，密码只在系统授权窗口输入。"])
        return outcome
    args.database_root = root
    args.config = config
    result = run(args)
    outcome.update(result)
    outcome["performed"].update(key_acquisition=True, provider_execution=True,
                                configuration_write=result.get("configured", False))
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optional local access onboarding; acquisition is experimental and opt-in.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run", "_worker"):
        p = sub.add_parser(name)
        p.add_argument("--provider", required=True, type=Path)
        p.add_argument("--sha256", default="")
        p.add_argument("--database-root", required=True, type=Path)
        if name != "plan":
            p.add_argument("--timeout", type=int, choices=range(30, 901), default=600, metavar="30..900")
        if name == "run":
            p.add_argument("--confirm-reviewed-provider", action="store_true")
            p.add_argument("--confirm-side-effects", action="store_true")
            p.add_argument("--config", type=Path, default=reader.DEFAULT_CONFIG)
            p.add_argument("--max-files", type=int, default=500)
        if name == "_worker":
            p.add_argument("--uid", type=int, required=True)
            p.add_argument("--run-dir", type=Path, required=True)
            p.add_argument("--max-files", type=int, default=500)
    p = sub.add_parser("connect")
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--database-root", required=True, type=Path)
    p.add_argument("--config", type=Path, default=reader.DEFAULT_CONFIG)
    p.add_argument("--max-files", type=int, default=500)
    p = sub.add_parser("finish-recovery", help="Remove only a reviewed recovery lock; requires post-run user confirmation")
    p.add_argument("--confirm-wechat-ready", action="store_true")
    p.add_argument("--confirm-no-acquisition", action="store_true")
    p = sub.add_parser("onboard", help="Guided first-access workflow; defaults to inspection only")
    p.add_argument("--config", type=Path, default=Path(os.environ.get("RION_WECHAT_READER_CONFIG", str(reader.DEFAULT_CONFIG))))
    p.add_argument("--database-root", type=Path)
    p.add_argument("--source", type=Path)
    p.add_argument("--provider", type=Path)
    p.add_argument("--sha256", default="")
    p.add_argument("--apply", action="store_true", help="Allow verified configuration; acquisition still needs both confirmations")
    p.add_argument("--confirm-reviewed-provider", action="store_true")
    p.add_argument("--confirm-side-effects", action="store_true")
    p.add_argument("--max-files", type=int, default=500)
    p.add_argument("--timeout", type=int, choices=range(30, 901), default=600, metavar="30..900")
    for name in ("status", "diagnose"):
        p = sub.add_parser(name, help="Read-only access diagnostics; no raw logs, keys or account paths")
        p.add_argument("--config", type=Path, default=Path(os.environ.get("RION_WECHAT_READER_CONFIG", str(reader.DEFAULT_CONFIG))))
        p.add_argument("--database-root", type=Path)
        p.add_argument("--max-files", type=int, default=500)
        if name == "diagnose":
            p.add_argument("--source", type=Path, help="Preview supplied material without replacing active configuration")
            output_mode = p.add_mutually_exclusive_group()
            output_mode.add_argument("--jev-request", action="store_true", help="Include an allowlisted Jev request; does not call the network")
            output_mode.add_argument("--support-summary", action="store_true", help="Output only allowlisted support fields for review before sharing")
    args = parser.parse_args(argv)
    try:
        if args.command == "_worker":
            return worker(args)
        if args.command == "plan":
            result = inspect(args.provider, args.sha256, args.database_root)
        elif args.command == "onboard":
            result = onboard(args)
        elif args.command == "connect":
            result = connect(args.source, args.database_root, args.config, max(1, args.max_files))
        elif args.command == "status":
            result = access_status(args.config, args.database_root, max(1, args.max_files))
        elif args.command == "diagnose":
            result = diagnose(args)
        elif args.command == "finish-recovery":
            result = finish_recovery(args.confirm_wechat_ready, args.confirm_no_acquisition)
        else:
            result = run(args)
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
        return 0 if args.command in {"plan", "status", "diagnose", "finish-recovery"} or result["state"] == "ready" else 1
    except (AccessError, reader.ReaderError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        # Never forward provider or SQLCipher exception strings to the agent.
        exc = sys.exc_info()[1]
        code = str(exc) if isinstance(exc, AccessError) else (material_diagnostic(exc)["code"] if isinstance(exc, reader.ReaderError) else "access_validation_failed")
        guidance = {
            "database_read_permission_denied": "实际提权进程无法读取数据库。先核对系统授权对象与文件权限；本阶段未启动provider，不要用重复获取代替权限修复。",
            "database_files_not_found": "所选目录没有可读的.db文件。确认是本人账号目录，未启动provider。",
            "database_header_unreadable": "数据库文件头不完整，先检查同步和文件状态；未启动provider。",
            "provider_failed": "外部工具失败，不能据此判断是权限、启动策略还是版本问题。查看本地worker-result.json的阶段和退出码；确认微信恢复后再决定，保留恢复锁，不上传原始日志。",
            "previous_run_requires_review": "上次接入尚未完成恢复检查。先确认官方微信可正常打开、个人配置未变，再人工审查恢复锁；不要自动删锁或重复取key。",
            "provider_account_root_required": "获取工具需要包含db_storage的单一账号目录。可传账号目录、db_storage或只有一个账号的数据父目录，由助手统一转换。",
            "debugger_unavailable": "实际worker的Apple Python无法加载LLDB；先修复Command Line Tools/调试器依赖，本阶段未启动provider。",
            "debugger_api_incompatible": "Apple Python可导入LLDB，但缺少启动身份或事件等待接口；先修复Command Line Tools兼容性，本阶段未启动provider。",
            "recovery_confirmation_required": "这是接入后的独立确认：先确认官方微信正常登录，且获取进程和授权窗口均已结束；不能沿用取key前的授权自动清锁。",
            "recovery_processes_still_present": "仍观察到副本、其他微信实例或已知获取工具；保留恢复锁，先人工检查，不自动结束进程。",
            "official_wechat_not_running": "未观察到当前用户的官方微信进程；先打开官方微信并确认登录，恢复锁保持不变。",
            "recovery_process_check_unavailable": "无法可靠检查当前进程，保留恢复锁；不要把检查失败当作没有残留。",
            "provider_dependency_check_failed": "获取前的调试器依赖检查失败或超时；先核对本机工具链，本阶段未启动provider。",
            "sqlcipher_driver_required": "当前助手的Python没有SQLCipher；先使用安装脚本准备的Reader运行环境，不要重新获取key。",
        }
        print(json.dumps({"ok": False, "error": {"code": code,
            "message": guidance.get(code, MATERIAL_ERRORS.get(code, "操作未完成；未覆盖已有reader配置。不要循环重试或上传访问材料。")),
            "diagnostics": safe_diagnostics(exc.diagnostics) if isinstance(exc, AccessError) else {},
            "suggested_checks": diagnostic_actions(exc.diagnostics) if isinstance(exc, AccessError) else []}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
