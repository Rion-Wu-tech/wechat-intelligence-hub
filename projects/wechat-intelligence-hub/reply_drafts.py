#!/usr/bin/env python3
"""Opt-in current-conversation draft staging, separate from the read-only reader."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import stat
import subprocess
import time
import uuid


class DraftError(Exception):
    pass


NATIVE_REASONS = {
    "accessibility_permission_required", "wechat_must_be_foreground", "official_wechat_required",
    "accessible_composer_unavailable", "focus_empty_message_composer", "composer_not_writable",
    "context_truncated", "conversation_context_unverifiable", "stale_or_invalid_draft",
    "context_changed", "native_adapter_failed", "platform_not_supported",
}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class NativeAdapter:
    def call(self, action, label, fingerprint=None, text=None):
        if platform.system() != "Darwin":
            return {"status": "blocked", "reason": "platform_not_supported"}
        helper = Path(__file__).with_name("macos_reply_draft.swift")
        request = {"action": action, "chat_label": label, "fingerprint": fingerprint, "text": text}
        result = subprocess.run(["/usr/bin/swift", str(helper)], input=json.dumps(request),
                                text=True, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise DraftError("native_adapter_failed")
        value = json.loads(result.stdout)
        if value.get("status") not in {"ready", "blocked", "staged", "unknown"}:
            raise DraftError("invalid_adapter_response")
        return value

    def probe(self, label):
        return self.call("probe", label)

    def stage(self, label, fingerprint, text):
        return self.call("stage", label, fingerprint, text)


class DraftStore:
    def __init__(self, directory, adapter=None, clock=time.time):
        if os.name != "posix":
            raise DraftError("platform_not_supported")
        directory = Path(directory).expanduser().absolute()
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise DraftError("symlink_store_rejected")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise DraftError("private_store_required")
        database = directory / "drafts.sqlite"
        fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise DraftError("private_database_required")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, account TEXT, chat TEXT, label TEXT, text TEXT, sha TEXT,
            fingerprint TEXT, state TEXT, expires REAL, approval TEXT)""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(drafts)")}
        for name, definition in (("backend", "TEXT NOT NULL DEFAULT 'native'"),
                                 ("reason", "TEXT"), ("receipt", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {definition}")
        self.db.commit()
        self.adapter = adapter or NativeAdapter()
        self.clock = clock

    def get(self, draft_id):
        row = self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise DraftError("draft_not_found")
        return dict(row)

    def prepare(self, account, chat, label, text, *, visual_observation=None):
        if not all(isinstance(x, str) and x.strip() for x in (account, chat, label, text)):
            raise DraftError("identity_and_text_required")
        if len(text.encode("utf-8")) > 32000 or any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise DraftError("invalid_draft_text")
        if visual_observation is not None:
            # The reference is an operator attestation, not machine-verified UI identity.
            if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,160}", visual_observation):
                raise DraftError("invalid_observation_reference")
            snapshot = {"fingerprint": visual_observation}
        else:
            snapshot = self.adapter.probe(label)
            if snapshot.get("status") != "ready" or not snapshot.get("fingerprint"):
                reason = snapshot.get("reason")
                raise DraftError(reason if reason in NATIVE_REASONS else "context_unverifiable")
        backend = "computer_use" if visual_observation is not None else "native"
        draft_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO drafts (id,account,chat,label,text,sha,fingerprint,state,expires,approval,backend) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (draft_id, account, chat, label, text, digest(text), snapshot["fingerprint"],
                             "draft", self.clock() + 600, None, backend))
        return {"id": draft_id, "state": "draft", "sha": digest(text), "text": text,
                "target": {"account": account, "chat": chat, "label": label},
                "identity_verification": "manual_current_chat", "backend": backend, "sent": False}

    def approve(self, draft_id, sha, confirm_target=False, confirm_no_attachments=False, *, visual_review=False):
        row = self.get(draft_id)
        if not confirm_target or not confirm_no_attachments:
            raise DraftError("explicit_target_and_empty_composer_confirmation_required")
        if row["state"] != "draft" or row["expires"] <= self.clock() or sha != row["sha"] or digest(row["text"]) != sha:
            raise DraftError("stale_or_modified_draft")
        if row["backend"] == "computer_use":
            if not visual_review:
                raise DraftError("fresh_visual_review_required")
        else:
            snapshot = self.adapter.probe(row["label"])
            if snapshot.get("status") != "ready" or snapshot.get("fingerprint") != row["fingerprint"]:
                raise DraftError("context_changed")
        token = uuid.uuid4().hex
        with self.db:
            changed = self.db.execute("UPDATE drafts SET state='approved',approval=?,expires=? WHERE id=? AND state='draft' AND expires>? AND sha=?",
                                      (token, self.clock() + 120, draft_id, self.clock(), sha)).rowcount
        if changed != 1:
            raise DraftError("approval_already_consumed")
        return {"id": draft_id, "state": "approved", "approval": token, "valid_seconds": 120}

    def stage(self, draft_id, token):
        # Consume approval before the external side effect; interrupted operations never retry.
        with self.db:
            changed = self.db.execute("""UPDATE drafts SET state='staging' WHERE id=? AND state='approved'
                AND approval=? AND expires>? AND backend='native'""", (draft_id, token, self.clock())).rowcount
        if changed != 1:
            raise DraftError("approval_missing_expired_or_consumed")
        row = self.get(draft_id)
        state = "unknown"
        reason = "write_outcome_unknown"
        try:
            if digest(row["text"]) != row["sha"]:
                state = "blocked"
                reason = "draft_modified"
            else:
                result = self.adapter.stage(row["label"], row["fingerprint"], row["text"])
                if result.get("status") in {"staged", "blocked", "unknown"}:
                    state = result["status"]
                    reason = None if state == "staged" else result.get("reason")
                    if state != "staged" and reason not in NATIVE_REASONS:
                        reason = "native_operation_blocked" if state == "blocked" else "write_outcome_unknown"
        except Exception:
            # No raw helper output: it could contain a private message.
            state = "unknown"
        with self.db:
            self.db.execute("UPDATE drafts SET state=?,reason=?,approval=NULL WHERE id=?", (state, reason, draft_id))
        return {"id": draft_id, "state": state, "reason": reason, "sent": False,
                "next": "review_in_wechat_and_send_manually" if state == "staged" else "inspect_manually_no_retry"}

    def handoff(self, draft_id, token, observed_label, confirm_empty=False):
        row = self.get(draft_id)
        if not confirm_empty or observed_label != row["label"] or digest(row["text"]) != row["sha"]:
            raise DraftError("fresh_target_and_empty_composer_review_required")
        receipt = uuid.uuid4().hex
        with self.db:
            changed = self.db.execute("""UPDATE drafts SET state='staging',approval=NULL,receipt=?,expires=?
                WHERE id=? AND state='approved' AND approval=? AND expires>? AND backend='computer_use'""",
                (receipt, self.clock() + 120, draft_id, token, self.clock())).rowcount
        if changed != 1:
            raise DraftError("approval_missing_expired_or_consumed")
        return {"id": draft_id, "state": "staging", "backend": "computer_use", "receipt": receipt,
                "text": row["text"], "sha": row["sha"], "account": row["account"], "chat": row["chat"],
                "label": row["label"], "sent": False,
                "next": "recheck_official_wechat_target_and_empty_composer_then_paste_once_without_enter"}

    def record_visual_result(self, draft_id, receipt, outcome, observed_sha=None, observed_label=None):
        row = self.get(draft_id)
        if row["backend"] != "computer_use" or row["state"] != "staging" or not receipt or receipt != row["receipt"]:
            raise DraftError("no_matching_visual_operation")
        if outcome not in {"staged", "blocked", "unknown"}:
            raise DraftError("invalid_visual_outcome")
        state = "staged_visual" if outcome == "staged" else outcome
        reason = None if outcome == "staged" else "visual_operation_" + outcome
        if row["expires"] <= self.clock() or (outcome == "staged" and
                (observed_sha != row["sha"] or observed_label != row["label"])):
            state = "unknown"
            reason = "visual_receipt_stale_or_mismatched"
        with self.db:
            changed = self.db.execute("UPDATE drafts SET state=?,reason=?,receipt=NULL WHERE id=? AND state='staging' AND receipt=?",
                                      (state, reason, draft_id, receipt)).rowcount
        if changed != 1:
            raise DraftError("visual_result_already_recorded")
        return {"id": draft_id, "state": state, "reason": reason, "sent": False, "verification": "operator_visual_review"}

    def show(self, draft_id):
        row = self.get(draft_id)
        return {field: row[field] for field in ("id", "account", "chat", "label", "text", "sha", "state", "expires", "backend", "reason")}

    def cancel(self, draft_id):
        with self.db:
            changed = self.db.execute("UPDATE drafts SET state='cancelled',approval=NULL WHERE id=? AND state IN ('draft','approved')",
                                      (draft_id,)).rowcount
        if changed != 1:
            raise DraftError("cannot_cancel_started_operation")
        return {"id": draft_id, "state": "cancelled", "sent": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path.home() / ".local/state/wechat-reply-drafts")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--chat-label", required=True)
    prepare = commands.add_parser("prepare")
    for name in ("account", "chat", "chat-label", "text-file"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--visual-observation", help="Opt-in CUA route: reference to the operator's fresh visual review; not an automated identity check")
    approve = commands.add_parser("approve")
    approve.add_argument("id")
    approve.add_argument("--sha", required=True)
    approve.add_argument("--confirm-current-account-and-chat", action="store_true")
    approve.add_argument("--confirm-no-attachments", action="store_true")
    approve.add_argument("--confirm-fresh-visual-review", action="store_true")
    stage = commands.add_parser("stage")
    stage.add_argument("id")
    stage.add_argument("--approval", required=True)
    handoff = commands.add_parser("visual-handoff")
    handoff.add_argument("id")
    handoff.add_argument("--approval", required=True)
    handoff.add_argument("--observed-label", required=True)
    handoff.add_argument("--confirm-empty-composer", action="store_true")
    result = commands.add_parser("visual-result")
    result.add_argument("id")
    result.add_argument("--receipt", required=True)
    result.add_argument("--outcome", choices=("staged", "blocked", "unknown"), required=True)
    result.add_argument("--observed-sha")
    result.add_argument("--observed-label")
    for name in ("show", "cancel"):
        commands.add_parser(name).add_argument("id")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "probe":
            result = NativeAdapter().probe(args.chat_label)
        else:
            store = DraftStore(args.store)
            try:
                if args.command == "prepare":
                    result = store.prepare(args.account, args.chat, args.chat_label, Path(args.text_file).read_text("utf-8"),
                                           visual_observation=args.visual_observation)
                elif args.command == "approve":
                    result = store.approve(args.id, args.sha, args.confirm_current_account_and_chat, args.confirm_no_attachments,
                                           visual_review=args.confirm_fresh_visual_review)
                elif args.command == "stage":
                    result = store.stage(args.id, args.approval)
                elif args.command == "show":
                    result = store.show(args.id)
                elif args.command == "visual-handoff":
                    result = store.handoff(args.id, args.approval, args.observed_label, args.confirm_empty_composer)
                elif args.command == "visual-result":
                    result = store.record_visual_result(args.id, args.receipt, args.outcome, args.observed_sha, args.observed_label)
                else:
                    result = store.cancel(args.id)
            finally:
                store.db.close()
        print(json.dumps(result, ensure_ascii=False))
        if result.get("state", result.get("status")) in {"blocked", "unknown"}:
            raise SystemExit(2)
    except DraftError as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc), "sent": False}))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"state": "blocked", "reason": "local_operation_failed", "sent": False}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
