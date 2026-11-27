#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Iterable


ROOT_FILES = {
    ".gitignore",
    "README.md",
    "SECURITY.md",
    "intelligence_views.py",
    "macos_reply_draft.swift",
    "maintenance.py",
    "opportunity_store.py",
    "report_bundle_flagship.py",
    "report_bundle_html.py",
    "report_security.py",
    "reply_drafts.py",
    "requirements-html.txt",
    "message_time.py",
    "message_identity.py",
    "wechat_intelligence_hub.py",
    "wechat_deal_radar.py",
}
PUBLIC_DIRS = {"assets", "docs", "samples", "scripts", "tests"}
PUBLIC_EXACT = {
    "config/profile.example.json",
    "contacts/README.md",
    "contacts/重点客户名单.example.txt",
    "contacts/排除名单.example.txt",
    "contacts/同行创作者名单.example.txt",
    "contacts/资源群名单.example.txt",
}
EXCLUDED_NAMES = {
    "config/profile.local.json",
    "scripts/微信群聊日报.applescript",
}
PRIVATE_PATTERNS = {
    "wechat_id": re.compile(r"\bwxid_[A-Za-z0-9_-]{8,}\b"),
    "chatroom_id": re.compile(r"\b[A-Za-z0-9_-]{8,}@chatroom\b"),
    "local_home": re.compile(r"/Users/(?!example|username|yourname)[^/\s]+/"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "generic_secret": re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    ),
}


def public_files(root: Path) -> list[Path]:
    selected: set[Path] = set()
    for name in ROOT_FILES:
        path = root / name
        if path.is_file():
            selected.add(path)
    for relative in PUBLIC_EXACT:
        path = root / relative
        if path.is_file():
            selected.add(path)
    for directory in PUBLIC_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in EXCLUDED_NAMES or "__pycache__" in path.parts:
                continue
            if path.suffix in {".pyc", ".db", ".sqlite", ".sqlite3", ".log"}:
                continue
            selected.add(path)
    return sorted(selected)


def scan_private_markers(paths: Iterable[Path]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in paths:
        if path.name == "build_release.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path.name == "test_compatibility.py":
            text = text.replace("wxid_private-secret", "<synthetic-private-id>")
        for label, pattern in PRIVATE_PATTERNS.items():
            if pattern.search(text):
                findings.append({"file": str(path), "marker": label})
    return findings


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(root: Path, out: Path) -> dict[str, object]:
    files = public_files(root)
    source_findings = scan_private_markers(files)
    if source_findings:
        details = "\n".join(f"- {item['marker']}: {item['file']}" for item in source_findings)
        raise RuntimeError(f"公开白名单中发现疑似私有标记：\n{details}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    manifest: list[dict[str, object]] = []
    for source in files:
        relative = source.relative_to(root)
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest.append(
            {
                "path": relative.as_posix(),
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
            }
        )
    report = {
        "status": "release_candidate_ready",
        "source_root": str(root),
        "output": str(out),
        "files": manifest,
    }
    public_report = {
        "status": report["status"],
        "package": "wechat-intelligence-hub",
        "files": manifest,
    }
    (out / "release-manifest.json").write_text(
        json.dumps(public_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="从公开白名单构建不含个人数据的发布候选")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--out", default="dist/wechat-intelligence-hub")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    out = Path(args.out).expanduser()
    if not out.is_absolute():
        out = (root / out).resolve()
    if out == root or root in out.parents and out.name in {"output", "contacts", "config"}:
        raise SystemExit("发布目录不能覆盖项目、output、contacts 或 config。")
    try:
        report = build_release(root, out)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"发布候选：{report['output']}")
    print(f"公开文件：{len(report['files'])} 个")
    print("尚未上传或公开；请继续执行 docs/release-checklist.md。")


if __name__ == "__main__":
    main()
