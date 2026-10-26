import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rion_wechat_access as access


class AccessOnboardingTests(unittest.TestCase):
    def test_support_next_step_prioritizes_recovery_and_current_readability(self):
        cases = (
            ({"state": "ready", "live_database_read_ok": True}, "use_existing_reader"),
            ({"state": "ready", "live_database_read_ok": True, "recovery_review_required": True}, "review_wechat_recovery"),
            ({"state": "ready_to_configure"}, "verify_then_configure"),
            ({"state": "dependency_required"}, "repair_runtime_dependency"),
            ({"state": "needs_access"}, "review_access_options"),
            ({"state": "verification_failed"}, "review_existing_material"),
            ({"state": "needs_access", "last_attempt": {"state": "provider_start_failed"}}, "review_access_options"),
            ({"state": "needs_access", "last_attempt": {"state": "worker_preflight_failed"}}, "review_access_options"),
            ({"state": "needs_access", "last_attempt": {"state": "provider_failed", "diagnostics": {"signals": ["shadow_policy_blocked"]}}}, "review_access_options"),
            ({"state": "ready", "live_database_read_ok": True, "last_attempt": {"state": "provider_start_failed"}}, "use_existing_reader"),
            ({"state": "dependency_required", "last_attempt": {"state": "provider_start_failed"}}, "repair_runtime_dependency"),
            ({"state": "unexpected"}, "manual_review"),
        )
        for report, expected in cases:
            with self.subTest(report=report):
                self.assertEqual(access.support_next_step(report), expected)
                self.assertEqual(access.support_summary(report)["next_step_code"], expected)

    def test_support_summary_only_exposes_allowlisted_attempt_state(self):
        report = {"state": "needs_access", "last_attempt": {
            "state": "PRIVATE-KEY-DO-NOT-LEAK", "diagnostics": {"phase": "provider_start", "secret": "PRIVATE-KEY-DO-NOT-LEAK"}}}
        summary = access.support_summary(report)
        self.assertEqual(summary["last_attempt_state"], "none_or_unknown")
        self.assertNotIn("PRIVATE-KEY-DO-NOT-LEAK", json.dumps(summary))

    def test_support_summary_ready_requires_live_database_read(self):
        summary = access.support_summary({"state": "ready", "live_database_read_ok": False})
        self.assertEqual(summary["state"], "unknown")
        self.assertEqual(summary["next_step_code"], "manual_review")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.environment = mock.patch.dict(os.environ, {"HOME": str(self.root)})
        self.environment.start()
        self.provider = self.root / "reviewed-helper"
        self.provider.write_bytes(b"test-only-not-a-real-provider")
        self.provider.chmod(0o700)
        self.digest = hashlib.sha256(self.provider.read_bytes()).hexdigest()
        self.db = self.root / "db_storage"
        self.db.mkdir()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def args(self):
        from argparse import Namespace
        return Namespace(provider=self.provider, sha256=self.digest, database_root=self.db,
                         confirm_reviewed_provider=True, confirm_side_effects=True,
                         config=self.root / "reader" / "config.json", timeout=30, max_files=500)

    def test_plan_does_not_execute_or_expose_paths(self):
        with mock.patch.object(access.subprocess, "run", side_effect=AssertionError("must not execute")):
            result = access.inspect(self.provider, self.digest, self.db)
        self.assertEqual(result["state"], "review_required")
        self.assertTrue(result["digest_matches"])
        self.assertFalse(result["provider_executed"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_provider_symlink_and_broad_permissions_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.provider)
        with self.assertRaisesRegex(access.AccessError, "symlink"):
            access.provider_digest(link)
        self.provider.chmod(0o777)
        with self.assertRaisesRegex(access.AccessError, "writable"):
            access.provider_digest(self.provider)

    def test_multiple_accounts_rejected(self):
        for account in ("one", "two"):
            (self.db / account / "db_storage").mkdir(parents=True)
        with self.assertRaisesRegex(access.AccessError, "account_selection"):
            access.check_root(self.db)

    def test_no_confirmation_never_executes(self):
        args = self.args()
        args.confirm_side_effects = False
        with mock.patch.object(access.platform, "system", return_value="Darwin"), \
             mock.patch.object(access.os, "geteuid", return_value=501), \
             mock.patch.object(access.subprocess, "run", side_effect=AssertionError("must not execute")):
            with self.assertRaisesRegex(access.AccessError, "explicit_confirmation"):
                access.run(args)

    def test_changed_provider_never_executes(self):
        args = self.args()
        self.provider.write_bytes(b"changed-provider")
        with mock.patch.object(access.platform, "system", return_value="Darwin"), \
             mock.patch.object(access.os, "geteuid", return_value=501):
            with self.assertRaisesRegex(access.AccessError, "digest_mismatch"):
                access.run(args)

    def test_non_mac_acquisition_rejected(self):
        with mock.patch.object(access.platform, "system", return_value="Windows"):
            with self.assertRaisesRegex(access.AccessError, "platform_not_supported"):
                access.run(self.args())

    def test_existing_config_is_never_overwritten(self):
        args = self.args()
        args.config.parent.mkdir(mode=0o700)
        args.config.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(access.AccessError, "not_overwritten"):
            access.connect(self.provider, self.db, args.config)
        self.assertEqual(args.config.read_text(), "existing")

    def test_provider_output_is_suppressed(self):
        diagnostics = {}
        state = access.bounded_provider([sys.executable, "-c", "print('PRIVATE MATERIAL'); raise SystemExit(3)"], {}, 5, diagnostics=diagnostics)
        self.assertEqual(state, "provider_failed")
        self.assertEqual(diagnostics['exit_code'], 3)
        self.assertNotIn('PRIVATE MATERIAL', json.dumps(diagnostics))

    def test_shadow_policy_signal_is_fixed_and_redacted(self):
        diagnostics = {}
        access.provider_signals(b"private/path: shadow_policy_blocked key=SECRET", diagnostics)
        self.assertEqual(access.safe_diagnostics(diagnostics)["signals"], ["shadow_policy_blocked"])
        self.assertNotIn("SECRET", json.dumps(access.safe_diagnostics(diagnostics)))

    def test_slow_wechat_exit_signal_is_fixed_and_redacted(self):
        diagnostics = {}
        access.provider_signals(b"PRIVATE /tmp/path official_wechat_stop_failed", diagnostics)
        self.assertEqual(access.safe_diagnostics(diagnostics)["signals"], ["official_wechat_stop_failed"])
        self.assertNotIn("PRIVATE", json.dumps(access.safe_diagnostics(diagnostics)))

    def test_pbkdf_process_signals_are_actionable_without_paths(self):
        diagnostics = {}
        access.provider_signals(b"/private/account pbkdf_foreign_wechat_process key=SECRET", diagnostics)
        self.assertEqual(access.safe_diagnostics(diagnostics)["signals"], ["pbkdf_foreign_wechat_process"])
        self.assertTrue(any("未向进程发送退出信号" in action for action in access.diagnostic_actions(diagnostics)))
        self.assertNotIn("SECRET", json.dumps(access.safe_diagnostics(diagnostics)))

    def test_pbkdf_shadow_signal_does_not_expose_signature_output(self):
        diagnostics = {}
        access.provider_signals(b"private codesign detail pbkdf_shadow_signature_unverified", diagnostics)
        self.assertEqual(access.safe_diagnostics(diagnostics)["signals"], ["pbkdf_shadow_signature_unverified"])
        self.assertNotIn("codesign detail", json.dumps(access.safe_diagnostics(diagnostics)))

    def test_shadow_preflight_signals_are_fixed_and_redacted(self):
        diagnostics = {}
        access.provider_signals(b"private/path other_wechat_install_running key=SECRET", diagnostics)
        self.assertEqual(access.safe_diagnostics(diagnostics)["signals"], ["other_wechat_install_running"])
        self.assertTrue(any("不退出它" in action for action in access.diagnostic_actions(diagnostics)))
        self.assertNotIn("SECRET", json.dumps(access.safe_diagnostics(diagnostics)))

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_provider_timeout_is_bounded(self):
        started = time.monotonic()
        state = access.bounded_provider([sys.executable, "-c", "import time; time.sleep(30)"], {}, 1)
        self.assertEqual(state, "provider_timeout_cleanup_required")
        self.assertLess(time.monotonic() - started, 6)

    def test_connect_failure_does_not_leave_keys_or_config(self):
        config = self.root / "private" / "config.json"
        with mock.patch.object(access.reader, "import_access_bundle", side_effect=access.reader.ReaderError("private-key-error")):
            with self.assertRaises(access.reader.ReaderError):
                access.connect(self.provider, self.db, config)
        self.assertFalse(config.exists())
        self.assertEqual(list(config.parent.iterdir()), [])

    def test_key_environment_cannot_override_staged_verification(self):
        with mock.patch.dict(os.environ, {"RION_WECHAT_READER_KEYS": "/unrelated/material"}):
            with self.assertRaisesRegex(access.AccessError, "environment_override"):
                access.connect(self.provider, self.db, self.root / "new/config.json")

    @unittest.skipUnless(os.name == "posix", "POSIX worker")
    def test_worker_does_not_inherit_password_or_original_resign_flags(self):
        import pwd
        from types import SimpleNamespace
        run_dir = self.root / ".config/rion-wechat-reader/access-runs/run-test"
        run_dir.mkdir(mode=0o700, parents=True)
        args = self.args()
        args.run_dir = run_dir
        args.uid = os.getuid()
        (self.db / 'sample.db').write_bytes(b'fictional-header')
        with mock.patch.object(access.os, "geteuid", return_value=0), \
             mock.patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.root), pw_name="testuser", pw_gid=os.getgid())), \
             mock.patch.object(access.subprocess, "run", return_value=SimpleNamespace(stdout="")), \
             mock.patch.object(access.os, "chown"), \
             mock.patch.object(access, "preflight_debugger"), \
             mock.patch.object(access, "bounded_provider", return_value="provider_finished") as invoke, \
             mock.patch.dict(os.environ, {"WXKEY_BOOTSTRAP_ORIGINAL_WECHAT": "1", "SECRET_PASSWORD": "must-not-pass"}):
            self.assertEqual(access.worker(args), 0)
        env = invoke.call_args.args[1]
        self.assertEqual(env["WXKEY_NO_ELEVATE"], "1")
        self.assertNotIn("WXKEY_BOOTSTRAP_ORIGINAL_WECHAT", env)
        self.assertNotIn("SECRET_PASSWORD", env)
        result = json.loads((run_dir / "worker-result.json").read_text())
        self.assertEqual(result['state'], 'provider_finished')
        self.assertTrue(result['diagnostics']['provider_started'])
        self.assertEqual(result['diagnostics']['readable_database_count'], 1)
        self.assertEqual(invoke.call_args.args[0][-1], str(self.root))

    def test_preflight_reads_header_without_exposing_paths_or_content(self):
        (self.db / 'sample.db').write_bytes(b'fictional-header')
        self.assertEqual(access.preflight_database_access(self.db), 1)
        with mock.patch.object(access.os, 'open', side_effect=PermissionError('sensitive path')):
            with self.assertRaisesRegex(access.AccessError, '^database_read_permission_denied$'):
                access.preflight_database_access(self.db)

    def test_preflight_rejects_empty_directory_and_symlink(self):
        with self.assertRaisesRegex(access.AccessError, 'database_files_not_found'):
            access.preflight_database_access(self.db)
        (self.db / 'link.db').symlink_to(self.provider)
        with self.assertRaisesRegex(access.AccessError, 'symlink_path_rejected'):
            access.preflight_database_access(self.db)

    @unittest.skipUnless(os.name == 'posix', 'POSIX worker')
    def test_worker_permission_failure_does_not_start_provider(self):
        import pwd
        from types import SimpleNamespace
        run_dir = self.root / '.config/rion-wechat-reader/access-runs/run-test'
        run_dir.mkdir(mode=0o700, parents=True)
        args = self.args()
        args.run_dir, args.uid = run_dir, os.getuid()
        with mock.patch.object(access.os, 'geteuid', return_value=0), \
             mock.patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.root), pw_name='testuser', pw_gid=os.getgid())), \
             mock.patch.object(access.subprocess, 'run', return_value=SimpleNamespace(stdout='')), \
             mock.patch.object(access.os, 'chown'), \
             mock.patch.object(access, 'preflight_database_access', side_effect=access.AccessError('database_read_permission_denied')), \
             mock.patch.object(access, 'bounded_provider') as invoke:
            self.assertEqual(access.worker(args), 1)
        invoke.assert_not_called()
        result = json.loads((run_dir / 'worker-result.json').read_text())
        self.assertFalse(result['diagnostics']['provider_started'])
        self.assertNotIn(str(self.root), json.dumps(result))

    @unittest.skipUnless(os.name == 'posix', 'POSIX worker')
    def test_worker_rejects_other_wechat_processes_before_provider(self):
        import pwd
        from types import SimpleNamespace
        other_user = f'{os.getuid() + 1} /Applications/WeChat.app/Contents/MacOS/WeChat'
        other_install = f'{os.getuid()} /tmp/Other.app/Contents/MacOS/WeChat'
        for label, process, expected in (
            ('user', other_user, 'other_user_wechat_running'),
            ('install', other_install, 'other_wechat_install_running'),
        ):
            with self.subTest(label=label):
                run_dir = self.root / '.config/rion-wechat-reader/access-runs' / ('run-' + label)
                run_dir.mkdir(mode=0o700, parents=True)
                args = self.args()
                args.run_dir, args.uid = run_dir, os.getuid()
                with mock.patch.object(access.os, 'geteuid', return_value=0), \
                     mock.patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.root), pw_name='testuser', pw_gid=os.getgid())), \
                     mock.patch.object(access.subprocess, 'run', return_value=SimpleNamespace(stdout=process)), \
                     mock.patch.object(access.os, 'chown'), \
                     mock.patch.object(access, 'bounded_provider') as invoke:
                    self.assertEqual(access.worker(args), 1)
                invoke.assert_not_called()
                result = json.loads((run_dir / 'worker-result.json').read_text())
                self.assertEqual(result['state'], expected)
                self.assertFalse(result['diagnostics']['provider_started'])
                self.assertNotIn('/tmp/Other.app', json.dumps(result))

    @unittest.skipUnless(os.name == 'posix', 'POSIX worker')
    def test_worker_preflight_and_spawn_failures_leave_fixed_results(self):
        import pwd
        from types import SimpleNamespace
        for failure, expected in (("preflight", "worker_preflight_failed"),
                                  ("spawn", "provider_start_failed")):
            with self.subTest(failure=failure):
                run_dir = self.root / '.config/rion-wechat-reader/access-runs' / ('run-' + failure)
                run_dir.mkdir(mode=0o700, parents=True)
                args = self.args()
                args.run_dir, args.uid = run_dir, os.getuid()
                (self.db / 'sample.db').write_bytes(b'fictional-header')
                with mock.patch.object(access.os, 'geteuid', return_value=0), \
                     mock.patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.root), pw_name='testuser', pw_gid=os.getgid())), \
                     mock.patch.object(access.os, 'chown'), \
                     mock.patch.object(access.subprocess, 'run', side_effect=OSError('PRIVATE PATH') if failure == 'preflight' else None), \
                     mock.patch.object(access, 'preflight_debugger'), \
                     mock.patch.object(access, 'bounded_provider', side_effect=OSError('PRIVATE PATH') if failure == 'spawn' else None):
                    self.assertEqual(access.worker(args), 1)
                result = json.loads((run_dir / 'worker-result.json').read_text())
                self.assertEqual(result['state'], expected)
                self.assertNotIn('PRIVATE PATH', json.dumps(result))
                self.assertFalse(result['diagnostics']['provider_started'])

    def test_encrypted_connect_publishes_only_verified_generation(self):
        import test_reader as fixtures
        if fixtures.SQLCIPHER is None:
            self.skipTest("SQLCipher unavailable")
        fixture = fixtures.ReaderContractTest()
        fixture.setUp()
        try:
            key = "74" * 32
            for source in (fixture.session, fixture.contact, fixture.message):
                fixture.encrypt_fixture(source, self.db / source.name, key)
            source = self.root / "authorized.json"
            source.write_text(json.dumps({p.name: key for p in self.db.glob("*.db")}))
            source.chmod(0o600)
            before = source.read_bytes()
            config = self.root / "reader/config.json"
            result = access.connect(source, self.db, config)
            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["verified_database_count"], 3)
            self.assertEqual(source.read_bytes(), before)
            self.assertNotIn(key, json.dumps(result))
            db = access.reader.DatabaseSet(config)
            self.assertTrue(access.reader.status(db)["status"]["live_database_read_ok"])
            self.assertEqual(db.keys_path.stat().st_mode & 0o777, 0o600)
        finally:
            fixture.tearDown()

    def test_partial_connect_does_not_publish(self):
        config = self.root / "private" / "config.json"
        with mock.patch.object(access.reader, "import_access_bundle", return_value={"verification": {"matched_database_count": 1}}), \
             mock.patch.object(access.reader, "access_plan", return_value={"state": "partial", "counts": {"unresolved": 1}}):
            result = access.connect(self.provider, self.db, config)
        self.assertEqual(result["state"], "partial")
        self.assertFalse(config.exists())

    def test_run_uses_native_authorization_and_preserves_failure_lock(self):
        args = self.args()
        with mock.patch.object(access.platform, "system", return_value="Darwin"), \
             mock.patch.object(access.os, "geteuid", return_value=501), \
             mock.patch.object(access.Path, "home", return_value=self.root), \
             mock.patch.object(access.reader, "sqlcipher_driver", return_value=(object(), "test")), \
             mock.patch.object(access.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as auth:
            with self.assertRaisesRegex(access.AccessError, "authorization_or_worker_failed"):
                access.run(args)
        argv = auth.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertIn("with administrator privileges", argv[2])
        self.assertNotIn("sudo -S", argv[2])
        self.assertTrue((self.root / ".config/rion-wechat-reader/access-runs/recovery-required.lock").exists())

    def test_cli_error_does_not_include_source_contents(self):
        self.provider.write_text("sensitive-invalid-json", encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "rion_wechat_access.py"), "connect",
                                 "--source", str(self.provider), "--database-root", str(self.db),
                                 "--config", str(self.root / "private/config.json")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("sensitive-invalid-json", result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)

    def workflow_args(self):
        args = self.args()
        args.source = None
        args.provider = None
        args.apply = False
        args.confirm_reviewed_provider = False
        args.confirm_side_effects = False
        return args

    def plan(self, state):
        return {"state": state, "live_database_read_ok": state == "ready", "scope": "test",
                "counts": {}, "performed": {"key_acquisition": False, "provider_execution": False,
                "configuration_write": False}, "next_actions": ["test"]}

    def test_onboard_ready_reuses_without_running_provider(self):
        with mock.patch.object(access.reader, "access_plan", return_value=self.plan("ready")), \
             mock.patch.object(access, "run", side_effect=AssertionError("must not execute")):
            result = access.onboard(self.workflow_args())
        self.assertTrue(result["reused_existing_configuration"])
        self.assertFalse(result["performed"]["key_acquisition"])

    def test_onboard_partial_or_missing_dependency_never_acquires(self):
        for state in ("partial", "dependency_required", "account_selection_required", "verification_failed"):
            with self.subTest(state=state), \
                 mock.patch.object(access.reader, "access_plan", return_value=self.plan(state)), \
                 mock.patch.object(access, "run", side_effect=AssertionError("must not execute")):
                result = access.onboard(self.workflow_args())
                self.assertEqual(result["state"], state)

    def test_onboard_does_not_replace_existing_configuration(self):
        args = self.workflow_args()
        args.config.parent.mkdir(mode=0o700)
        args.config.write_text("unchanged")
        args.apply = True
        with mock.patch.object(access.reader, "access_plan", return_value=self.plan("needs_access")):
            result = access.onboard(args)
        self.assertEqual(result["state"], "existing_configuration_requires_review")
        self.assertEqual(args.config.read_text(), "unchanged")

    def test_onboard_provider_and_consent_gates(self):
        args = self.workflow_args()
        with mock.patch.object(access.reader, "access_plan", return_value=self.plan("needs_access")), \
             mock.patch.object(access.platform, "system", return_value="Darwin"), \
             mock.patch.object(access.reader, "sqlcipher_driver", return_value=(object(), "test")), \
             mock.patch.object(access, "run", return_value={"state": "ready", "configured": True, "live_database_read_ok": True}) as acquire:
            self.assertEqual(access.onboard(args)["state"], "provider_required")
            args.provider = self.provider
            args.sha256 = ""
            self.assertEqual(access.onboard(args)["state"], "provider_review_required")
            args.sha256 = self.digest
            args.apply = True
            self.assertEqual(access.onboard(args)["state"], "authorization_required")
            args.confirm_reviewed_provider = True
            self.assertEqual(access.onboard(args)["state"], "authorization_required")
            acquire.assert_not_called()
            args.confirm_side_effects = True
            result = access.onboard(args)
        acquire.assert_called_once()
        self.assertTrue(result["performed"]["provider_execution"])

    def test_onboard_bad_explicit_material_does_not_fall_back_to_acquisition(self):
        args = self.workflow_args()
        args.source = self.root / "empty.json"
        args.source.write_text("{}")
        args.source.chmod(0o600)
        with mock.patch.object(access.reader, "access_plan", return_value=self.plan("needs_access")), \
             mock.patch.object(access, "run", side_effect=AssertionError("must not execute")):
            result = access.onboard(args)
        self.assertEqual(result["state"], "access_material_requires_review")

    def test_onboard_plaintext_requires_apply_then_publishes_verified_config(self):
        import test_reader as fixtures
        fixture = fixtures.ReaderContractTest()
        fixture.setUp()
        try:
            args = self.workflow_args()
            args.database_root = fixture.data_root.resolve()
            preview = access.onboard(args)
            self.assertEqual(preview["state"], "ready_to_configure")
            self.assertFalse(args.config.exists())
            args.apply = True
            result = access.onboard(args)
            self.assertEqual(result["state"], "ready")
            self.assertTrue(result["live_database_read_ok"])
            self.assertFalse(result["performed"]["provider_execution"])
            args.database_root = None
            again = access.onboard(args)
            self.assertTrue(again["reused_existing_configuration"])
        finally:
            fixture.tearDown()

    def test_onboard_fresh_home_acquisition_then_real_encrypted_connect(self):
        import test_reader as fixtures
        if fixtures.SQLCIPHER is None:
            self.skipTest("SQLCipher unavailable")
        fixture = fixtures.ReaderContractTest()
        fixture.setUp()
        old_umask = os.umask(0o022)
        try:
            key = "35" * 32
            for source in (fixture.session, fixture.contact, fixture.message):
                fixture.encrypt_fixture(source, self.db / source.name, key)
            args = self.workflow_args()
            args.config = self.root / ".config/rion-wechat-reader/config.json"
            args.provider = self.provider
            args.apply = args.confirm_reviewed_provider = args.confirm_side_effects = True

            def authorization(*unused_args, **unused_kwargs):
                material = self.root / ".config/wxcli/config.json"
                material.parent.mkdir(mode=0o700, parents=True)
                material.write_text(json.dumps({p.name: key for p in self.db.glob("*.db")}))
                material.chmod(0o600)
                run_dir = next((args.config.parent / "access-runs").glob("run-*"))
                (run_dir / "worker-result.json").write_text('{"state":"provider_finished"}')
                return subprocess.CompletedProcess([], 0)

            with mock.patch.object(access.Path, "home", return_value=self.root), \
                 mock.patch.object(access.platform, "system", return_value="Darwin"), \
                 mock.patch.object(access.os, "geteuid", return_value=501), \
                 mock.patch.object(access.subprocess, "run", side_effect=authorization):
                result = access.onboard(args)
            self.assertEqual(result["state"], "ready")
            self.assertTrue(result["live_database_read_ok"])
            self.assertEqual(args.config.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(args.config.stat().st_mode & 0o777, 0o600)
            self.assertTrue((args.config.parent / "access-runs/recovery-required.lock").exists())
            self.assertTrue(result["recovery_review_required"])
            self.assertNotIn(key, json.dumps(result))
            self.assertTrue(access.reader.status(access.reader.DatabaseSet(args.config))["status"]["live_database_read_ok"])
        finally:
            os.umask(old_umask)
            fixture.tearDown()

    def test_run_rejects_public_config_directory_before_authorization(self):
        args = self.args()
        args.config.parent.mkdir(mode=0o755)
        args.config.parent.chmod(0o755)
        with mock.patch.object(access.Path, "home", return_value=self.root), \
             mock.patch.object(access.platform, "system", return_value="Darwin"), \
             mock.patch.object(access.os, "geteuid", return_value=501), \
             mock.patch.object(access.reader, "sqlcipher_driver", return_value=(object(), "test")), \
             mock.patch.object(access.subprocess, "run", side_effect=AssertionError("must not authorize")):
            with self.assertRaisesRegex(access.AccessError, "config_directory_not_private"):
                access.run(args)

    def test_provider_root_accepts_storage_account_and_single_account_parent(self):
        for supplied in (self.root, self.db):
            self.assertEqual(access.provider_roots(supplied), (self.root, self.db))
        parent = self.root / "accounts"
        storage = parent / "fictional-user" / "db_storage"
        storage.mkdir(parents=True)
        self.assertEqual(access.provider_roots(parent), (storage.parent, storage))
        (parent / "another-user" / "db_storage").mkdir(parents=True)
        with self.assertRaisesRegex(access.AccessError, "account_selection_required"):
            access.provider_roots(parent)

    def test_provider_root_rejects_flat_export_but_reader_can_use_it(self):
        export = self.root / "export"
        export.mkdir()
        self.assertEqual(access.check_root(export), export)
        with self.assertRaisesRegex(access.AccessError, "provider_account_root_required"):
            access.provider_roots(export)

    def test_provider_root_rejects_symlink_account(self):
        parent = self.root / "parent"
        parent.mkdir()
        (parent / "linked-account").symlink_to(self.root)
        with self.assertRaisesRegex(access.AccessError, "symlink_path_rejected"):
            access.provider_roots(parent)

    def test_provider_output_is_bounded_and_only_allowlisted_markers_survive(self):
        diagnostics = {}
        state = access.bounded_provider([sys.executable, "-c",
            "import sys; print('secret-key-' * 200000); print('PBKDF fallback: launching /private/path'); "
            "print('PBKDF ran, but none of its salts matched this DB root'); sys.exit(2)"], {}, 5, diagnostics=diagnostics)
        self.assertEqual(state, "provider_failed")
        self.assertEqual(diagnostics["signals"], ["account_salt_mismatch", "pbkdf_launch"])
        self.assertNotIn("secret-key", json.dumps(diagnostics))
        self.assertNotIn("/private/path", json.dumps(diagnostics))

    def test_lifecycle_signals_do_not_claim_restored_or_expose_raw_output(self):
        diagnostics = {}
        access.provider_signals(b'target_identity_mismatch PRIVATE original_reopen_requested /private/home', diagnostics)
        self.assertEqual(diagnostics['signals'], ['original_reopen_requested', 'target_identity_mismatch'])
        actions = access.diagnostic_actions(diagnostics)
        self.assertTrue(any('不代表已经登录' in action for action in actions))
        self.assertNotIn('PRIVATE', json.dumps(diagnostics))
        self.assertNotIn('restored', diagnostics)

    def test_safe_diagnostics_drops_arbitrary_strings_and_unknown_fields(self):
        value = access.safe_diagnostics({"phase": "private/path", "signals": ["pbkdf_launch", "SECRET", {}, []],
            "exit_code": "secret", "key": "SECRET", "provider_started": True})
        self.assertEqual(value, {"signals": ["pbkdf_launch"], "provider_started": True})

    def test_debugger_preflight_checks_exact_python_without_acquisition(self):
        from types import SimpleNamespace
        with mock.patch.object(access.subprocess, "run", side_effect=[
            SimpleNamespace(stdout=str(self.root), returncode=0), SimpleNamespace(returncode=1)]) as invoke:
            with self.assertRaisesRegex(access.AccessError, "debugger_unavailable"):
                access.preflight_debugger()
        self.assertEqual(invoke.call_args.args[0], ["/usr/bin/python3", "-c", access.DEBUGGER_PROBE])
        self.assertNotIn("HOME", invoke.call_args.kwargs["env"])

    def test_debugger_failure_happens_before_provider(self):
        import pwd
        from types import SimpleNamespace
        args = self.args()
        args.uid = os.getuid()
        args.run_dir = self.root / ".config/rion-wechat-reader/access-runs/run-debugger"
        args.run_dir.mkdir(mode=0o700, parents=True)
        (self.db / "sample.db").write_bytes(b"fictional-header")
        with mock.patch.object(access.os, "geteuid", return_value=0), \
             mock.patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.root), pw_name="test", pw_gid=os.getgid())), \
             mock.patch.object(access.os, "chown"), \
             mock.patch.object(access.subprocess, "run", return_value=SimpleNamespace(stdout="")), \
             mock.patch.object(access, "preflight_debugger", side_effect=access.AccessError("debugger_unavailable")), \
             mock.patch.object(access, "bounded_provider") as provider:
            self.assertEqual(access.worker(args), 1)
        provider.assert_not_called()
        result = access.read_worker_result(args.run_dir / "worker-result.json")
        self.assertEqual(result["state"], "debugger_unavailable")
        self.assertFalse(result["diagnostics"]["provider_started"])

    def test_status_is_read_only_and_drops_private_worker_fields(self):
        directory = self.root / ".config/rion-wechat-reader/access-runs/run-test"
        directory.mkdir(parents=True)
        lock = directory.parent / "recovery-required.lock"
        lock.touch()
        (directory / "worker-result.json").write_text(json.dumps({"state": "provider_failed", "key": "SECRET",
            "diagnostics": {"phase": "provider_exit", "exit_code": 1, "path": str(self.root), "signals": ["target_launch_failed", "SECRET"]}}))
        plan = self.plan("needs_access")
        plan["environment"] = {}
        with mock.patch.object(access.reader, "access_plan", return_value=plan), \
             mock.patch.object(access.platform, "system", return_value="Test"), \
             mock.patch.object(access.subprocess, "run", side_effect=AssertionError("must not execute")):
            result = access.access_status(self.root / "config.json", None, 500)
        self.assertTrue(result["recovery_review_required"])
        self.assertTrue(lock.exists())
        self.assertEqual(result["last_attempt"]["diagnostics"]["signals"], ["target_launch_failed"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_worker_result_rejects_oversize_and_unrecognized_state(self):
        path = self.root / "worker-result.json"
        path.write_text(json.dumps({"state": "PRIVATE"}))
        with self.assertRaisesRegex(access.AccessError, "worker_result_invalid"):
            access.read_worker_result(path)
        path.write_bytes(b"x" * 16385)
        with self.assertRaisesRegex(access.AccessError, "worker_result_invalid"):
            access.read_worker_result(path)

    def test_onboard_converts_relative_bundle_without_reacquisition(self):
        import test_reader as fixtures
        if fixtures.SQLCIPHER is None:
            self.skipTest("SQLCipher unavailable")
        fixture = fixtures.ReaderContractTest()
        fixture.setUp()
        try:
            key = "53" * 32
            entries = {}
            for source in (fixture.session, fixture.contact, fixture.message):
                destination = self.db / "nested" / source.name
                destination.parent.mkdir(exist_ok=True)
                fixture.encrypt_fixture(source, destination, key)
                entries[str(destination.relative_to(self.db))] = key
            args = self.workflow_args()
            args.source = self.root / "authorized.json"
            args.source.write_text(json.dumps({"keys": entries}))
            args.source.chmod(0o600)
            with mock.patch.object(access, "run", side_effect=AssertionError("must not reacquire")):
                preview = access.onboard(args)
                self.assertEqual(preview["state"], "ready_to_configure")
                self.assertFalse(args.config.exists())
                args.apply = True
                result = access.onboard(args)
            self.assertEqual(result["state"], "ready")
            self.assertFalse(result["performed"]["key_acquisition"])
        finally:
            fixture.tearDown()

    def test_partial_relative_bundle_is_normalized_before_requesting_more_keys(self):
        import test_reader as fixtures
        if fixtures.SQLCIPHER is None:
            self.skipTest("SQLCipher unavailable")
        fixture = fixtures.ReaderContractTest()
        fixture.setUp()
        try:
            key = "53" * 32
            entries = {}
            for source in (fixture.session, fixture.contact, fixture.message):
                destination = self.db / "nested" / source.name
                destination.parent.mkdir(exist_ok=True)
                fixture.encrypt_fixture(source, destination, key)
                entries[str(destination)] = key
            extra = self.db / "nested" / "extra_message.db"
            fixture.encrypt_fixture(fixture.message, extra, key)
            entries[str(extra.relative_to(self.db))] = key
            args = self.workflow_args()
            args.source = self.root / "authorized.json"
            args.source.write_text(json.dumps({"keys": entries}))
            args.source.chmod(0o600)
            initial = access.reader.access_plan(args.config, self.db, args.source, 500)
            self.assertEqual(initial["state"], "partial")
            with mock.patch.object(access, "run", side_effect=AssertionError("must not reacquire")):
                preview = access.onboard(args)
            self.assertEqual(preview["state"], "ready_to_configure")
            self.assertFalse(args.config.exists())
        finally:
            fixture.tearDown()

    def test_normalized_salt_material_can_be_reused_and_salt_mismatch_rejected(self):
        import test_reader as fixtures
        if fixtures.SQLCIPHER is None:
            self.skipTest("SQLCipher unavailable")
        fixture = fixtures.ReaderContractTest()
        fixture.setUp()
        try:
            key = "34" * 32
            salts = {}
            for source in (fixture.session, fixture.contact, fixture.message):
                destination = self.db / source.name
                fixture.encrypt_fixture(source, destination, key)
                salt = access.reader.database_salt(destination)
                salts[salt] = {"key": key + salt}
            source = self.root / "normalized.json"
            source.write_text(json.dumps({"salt_keys": salts, "database_root": str(self.db)}))
            source.chmod(0o600)
            result = access.connect(source, self.db, self.root / "config/config.json")
            self.assertEqual(result["state"], "ready")
            first = next(iter(salts))
            salts[first]["key"] = key + "00" * 16
            source.write_text(json.dumps({"salt_keys": salts}))
            with self.assertRaisesRegex(access.reader.ReaderError, "salt mismatch"):
                access.connect(source, self.db, self.root / "new-config/config.json")
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
