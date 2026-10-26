import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "reply_drafts.py"
spec = importlib.util.spec_from_file_location("reply_drafts", MODULE)
drafts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drafts)


class FakeAdapter:
    def __init__(self):
        self.fingerprint = "context-a"
        self.status = "ready"
        self.calls = []
        self.timeout = False

    def probe(self, label):
        return {"status": self.status, "fingerprint": self.fingerprint, "reason": "blocked"}

    def stage(self, label, fingerprint, text):
        self.calls.append((label, fingerprint, text))
        if self.timeout:
            raise TimeoutError("PRIVATE MESSAGE")
        return {"status": "staged" if self.status == "ready" and fingerprint == self.fingerprint else "blocked"}


class ReplyDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.adapter = FakeAdapter()
        self.store = drafts.DraftStore(Path(self.tmp.name).resolve() / "private", self.adapter, lambda: self.now)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def prepare(self):
        return self.store.prepare("account-id", "chat-id", "Example", "Hello\nSecond line")

    def approved(self):
        draft = self.prepare()
        approval = self.store.approve(draft["id"], draft["sha"], True, True)
        return draft, approval

    def test_prepare_never_writes_to_ui(self):
        self.prepare()
        self.assertEqual(self.adapter.calls, [])

    def test_requires_confirmation_of_target_and_attachments(self):
        draft = self.prepare()
        for target, attachments in ((False, True), (True, False), (False, False)):
            with self.assertRaises(drafts.DraftError):
                self.store.approve(draft["id"], draft["sha"], target, attachments)
        self.assertEqual(self.adapter.calls, [])

    def test_stage_once_never_claims_sent(self):
        draft, approval = self.approved()
        result = self.store.stage(draft["id"], approval["approval"])
        self.assertEqual(result["state"], "staged")
        self.assertFalse(result["sent"])
        with self.assertRaises(drafts.DraftError):
            self.store.stage(draft["id"], approval["approval"])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_approval_missing_or_wrong_is_rejected(self):
        draft = self.prepare()
        with self.assertRaises(drafts.DraftError):
            self.store.stage(draft["id"], "not-approved")
        self.assertEqual(self.adapter.calls, [])

    def test_text_digest_change_blocks_approval(self):
        draft = self.prepare()
        with self.assertRaises(drafts.DraftError):
            self.store.approve(draft["id"], "wrong", True, True)

    def test_changed_chat_blocks_approval(self):
        draft = self.prepare()
        self.adapter.fingerprint = "other-chat"
        with self.assertRaises(drafts.DraftError):
            self.store.approve(draft["id"], draft["sha"], True, True)

    def test_changed_chat_after_approval_blocks_staging(self):
        draft, approval = self.approved()
        self.adapter.fingerprint = "other-chat"
        self.assertEqual(self.store.stage(draft["id"], approval["approval"])["state"], "blocked")

    def test_expiration_does_not_touch_ui(self):
        draft, approval = self.approved()
        self.now += 121
        with self.assertRaises(drafts.DraftError):
            self.store.stage(draft["id"], approval["approval"])
        self.assertEqual(self.adapter.calls, [])

    def test_expiration_during_approval_probe_cannot_extend_authorization(self):
        draft = self.prepare()
        def slow_probe(label):
            self.now += 601
            return {"status": "ready", "fingerprint": "context-a"}
        self.adapter.probe = slow_probe
        with self.assertRaises(drafts.DraftError):
            self.store.approve(draft["id"], draft["sha"], True, True)
        self.assertEqual(self.store.show(draft["id"])["state"], "draft")

    def test_uncertain_write_never_retries_or_leaks(self):
        draft, approval = self.approved()
        self.adapter.timeout = True
        result = self.store.stage(draft["id"], approval["approval"])
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("PRIVATE", str(result))
        with self.assertRaises(drafts.DraftError):
            self.store.stage(draft["id"], approval["approval"])

    def test_interrupted_staging_cannot_be_replayed(self):
        draft, approval = self.approved()
        with self.store.db:
            self.store.db.execute("UPDATE drafts SET state='staging' WHERE id=?", (draft["id"],))
        with self.assertRaises(drafts.DraftError):
            self.store.stage(draft["id"], approval["approval"])

    def test_nonempty_or_unavailable_composer_blocks_prepare(self):
        self.adapter.status = "blocked"
        with self.assertRaises(drafts.DraftError):
            self.prepare()

    def test_modified_approved_text_is_not_staged(self):
        draft, approval = self.approved()
        with self.store.db:
            self.store.db.execute("UPDATE drafts SET text='changed' WHERE id=?", (draft["id"],))
        self.assertEqual(self.store.stage(draft["id"], approval["approval"])["state"], "blocked")
        self.assertEqual(self.adapter.calls, [])

    def test_symlink_store_is_rejected(self):
        link = Path(self.tmp.name) / "alias"
        link.symlink_to(Path(self.tmp.name) / "private", target_is_directory=True)
        with self.assertRaises(drafts.DraftError):
            drafts.DraftStore(link)

    def test_show_does_not_expose_approval_token(self):
        draft, approval = self.approved()
        shown = self.store.show(draft["id"])
        self.assertEqual(shown["state"], "approved")
        self.assertNotIn(approval["approval"], str(shown))

    def test_cancel_never_touches_ui_or_allows_staging(self):
        draft, approval = self.approved()
        self.store.cancel(draft["id"])
        with self.assertRaises(drafts.DraftError):
            self.store.stage(draft["id"], approval["approval"])
        self.assertEqual(self.adapter.calls, [])

    def test_native_source_has_no_send_or_keyboard_apis(self):
        source = MODULE.with_name("macos_reply_draft.swift").read_text()
        for forbidden in ("AXUIElementPerformAction", "CGEvent(", "NSPasteboard", "keyCode:"):
            self.assertNotIn(forbidden, source)

    def visual_approved(self):
        draft = self.store.prepare("account-id", "chat-id", "Example", "Test draft",
                                   visual_observation="cua:test-1")
        approval = self.store.approve(draft["id"], draft["sha"], True, True, visual_review=True)
        return draft, approval

    def test_visual_path_requires_explicit_review(self):
        draft = self.store.prepare("account-id", "chat-id", "Example", "Test draft",
                                   visual_observation="cua:test-1")
        with self.assertRaises(drafts.DraftError):
            self.store.approve(draft["id"], draft["sha"], True, True)

    def test_visual_cannot_call_native_stage(self):
        draft, approval = self.visual_approved()
        with self.assertRaises(drafts.DraftError):
            self.store.stage(draft["id"], approval["approval"])
        self.assertEqual(self.adapter.calls, [])

    def test_native_cannot_use_visual_handoff(self):
        draft, approval = self.approved()
        with self.assertRaises(drafts.DraftError):
            self.store.handoff(draft["id"], approval["approval"], "Example", True)

    def test_visual_handoff_is_one_time_and_has_no_ui_side_effect(self):
        draft, approval = self.visual_approved()
        handoff = self.store.handoff(draft["id"], approval["approval"], "Example", True)
        self.assertEqual(handoff["state"], "staging")
        self.assertEqual(self.adapter.calls, [])
        with self.assertRaises(drafts.DraftError):
            self.store.handoff(draft["id"], approval["approval"], "Example", True)

    def test_visual_wrong_target_or_nonempty_composer_rejected(self):
        draft, approval = self.visual_approved()
        for label, empty in (("Wrong", True), ("Example", False)):
            with self.assertRaises(drafts.DraftError):
                self.store.handoff(draft["id"], approval["approval"], label, empty)

    def test_visual_success_is_distinct_from_native_verification(self):
        draft, approval = self.visual_approved()
        handoff = self.store.handoff(draft["id"], approval["approval"], "Example", True)
        result = self.store.record_visual_result(draft["id"], handoff["receipt"], "staged", draft["sha"], "Example")
        self.assertEqual(result["state"], "staged_visual")
        self.assertFalse(result["sent"])
        with self.assertRaises(drafts.DraftError):
            self.store.record_visual_result(draft["id"], handoff["receipt"], "staged", draft["sha"], "Example")

    def test_visual_readback_mismatch_is_unknown(self):
        draft, approval = self.visual_approved()
        handoff = self.store.handoff(draft["id"], approval["approval"], "Example", True)
        result = self.store.record_visual_result(draft["id"], handoff["receipt"], "staged", "wrong", "Example")
        self.assertEqual(result["state"], "unknown")

    def test_visual_delayed_receipt_cannot_claim_success(self):
        draft, approval = self.visual_approved()
        handoff = self.store.handoff(draft["id"], approval["approval"], "Example", True)
        self.now += 121
        result = self.store.record_visual_result(draft["id"], handoff["receipt"], "staged", draft["sha"], "Example")
        self.assertEqual(result["state"], "unknown")

    def test_visual_wrong_receipt_rejected(self):
        draft, approval = self.visual_approved()
        self.store.handoff(draft["id"], approval["approval"], "Example", True)
        with self.assertRaises(drafts.DraftError):
            self.store.record_visual_result(draft["id"], "bad", "staged", draft["sha"], "Example")

    def test_native_block_reason_is_retained(self):
        draft, approval = self.approved()
        self.adapter.stage = lambda *a: {"status": "blocked", "reason": "official_wechat_required"}
        result = self.store.stage(draft["id"], approval["approval"])
        self.assertEqual(result["reason"], "official_wechat_required")
        self.assertEqual(self.store.show(draft["id"])["reason"], "official_wechat_required")

    def test_native_block_reason_is_allowlisted(self):
        draft, approval = self.approved()
        self.adapter.stage = lambda *a: {"status": "blocked", "reason": "PRIVATE"}
        result = self.store.stage(draft["id"], approval["approval"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertNotIn("PRIVATE", str(self.store.show(draft["id"])))


if __name__ == "__main__":
    unittest.main()
