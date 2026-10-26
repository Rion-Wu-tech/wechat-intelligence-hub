import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rion_wechat_access as access


@unittest.skipUnless(os.name == 'posix', 'macOS recovery requires POSIX process identities')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = mock.patch.object(access.Path, 'home', return_value=self.root)
        self.home.start()
        self.system = mock.patch.object(access.platform, 'system', return_value='Darwin')
        self.system.start()
        self.lock = self.root / '.config/rion-wechat-reader/access-runs/recovery-required.lock'
        self.lock.parent.mkdir(mode=0o700, parents=True)
        self.lock.touch(mode=0o600)
        self.ps = f'{os.getuid()} /Applications/WeChat.app/Contents/MacOS/WeChat\n'

    def tearDown(self):
        self.system.stop()
        self.home.stop()
        self.temp.cleanup()

    def observed(self, text=None):
        return mock.patch.object(access.subprocess, 'run', return_value=SimpleNamespace(stdout=self.ps if text is None else text))

    def test_confirmation_is_required_after_acquisition(self):
        for ready, stopped in ((False, False), (True, False), (False, True)):
            with self.subTest(ready=ready, stopped=stopped), self.observed():
                with self.assertRaisesRegex(access.AccessError, 'recovery_confirmation_required'):
                    access.finish_recovery(ready, stopped)
                self.assertTrue(self.lock.exists())

    def test_recovery_check_only_returns_counts_not_paths(self):
        shadow = self.root / 'Library/Application Support/wx-mcp/WeChat-shadow.app/Contents/MacOS/WeChat'
        with self.observed(self.ps + f'0 {shadow}\n0 /private/tool/wxkey\n501 /private/sensitive/python3\n') as invoked:
            result = access.recovery_process_check()
        self.assertEqual(result['counts'], {'official_wechat': 1, 'shadow_wechat': 1, 'other_wechat': 0, 'known_provider': 1})
        self.assertFalse(result['login_verified'])
        self.assertFalse(result['all_acquisition_processes_verified_stopped'])
        self.assertNotIn('/private', json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(invoked.call_args.args[0], ['/bin/ps', '-axo', 'uid=,comm='])
        self.assertEqual(invoked.call_args.kwargs['timeout'], 5)

    def test_renamed_unknown_process_not_claimed_absent(self):
        with self.observed(self.ps + '501 /somewhere/renamed-provider\n'):
            result = access.recovery_process_check()
        self.assertFalse(result['all_acquisition_processes_verified_stopped'])

    def test_live_provider_or_shadow_keeps_lock(self):
        for extra in ('0 /tool/wxkey\n', '0 /unknown/WeChat\n',
                      f'{os.getuid()} {self.root}/Library/Application Support/wx-mcp/WeChat-shadow.app/Contents/MacOS/WeChat\n'):
            with self.subTest(extra=extra), self.observed(self.ps + extra):
                with self.assertRaisesRegex(access.AccessError, 'recovery_processes_still_present'):
                    access.finish_recovery(True, True)
                self.assertTrue(self.lock.exists())

    def test_other_users_official_process_not_counted_as_ours(self):
        with self.observed('0 /Applications/WeChat.app/Contents/MacOS/WeChat\n'):
            result = access.recovery_process_check()
        self.assertEqual(result['counts']['official_wechat'], 0)
        self.assertEqual(result['counts']['other_wechat'], 1)

    def test_missing_official_wechat_keeps_lock(self):
        with self.observed('501 /usr/bin/python3\n'):
            with self.assertRaisesRegex(access.AccessError, 'official_wechat_not_running'):
                access.finish_recovery(True, True)
        self.assertTrue(self.lock.exists())

    def test_failed_or_malformed_process_check_keeps_lock(self):
        with self.observed('malformed output'):
            with self.assertRaisesRegex(access.AccessError, 'recovery_process_check_unavailable'):
                access.finish_recovery(True, True)
        with mock.patch.object(access.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ps', 5)):
            self.assertEqual(access.recovery_process_check()['state'], 'unavailable')
        self.assertTrue(self.lock.exists())

    def test_oversize_process_output_is_unknown(self):
        with self.observed('x' * (2 * 1024 * 1024 + 1)):
            self.assertEqual(access.recovery_process_check()['state'], 'unavailable')

    def test_finish_only_removes_lock_preserves_other_files(self):
        keep = self.lock.parent / 'do-not-delete.json'
        keep.write_text('unchanged')
        with self.observed(), mock.patch.object(access, 'bounded_provider', side_effect=AssertionError('no acquisition')):
            result = access.finish_recovery(True, True)
        self.assertEqual(result['state'], 'recovery_review_completed')
        self.assertFalse(self.lock.exists())
        self.assertEqual(keep.read_text(), 'unchanged')
        self.assertFalse(result['provider_executed'])

    def test_no_lock_is_noop(self):
        self.lock.unlink()
        with mock.patch.object(access.subprocess, 'run', side_effect=AssertionError('no check necessary')):
            self.assertEqual(access.finish_recovery(True, True)['state'], 'no_recovery_lock')

    def test_symlink_lock_is_not_removed(self):
        self.lock.unlink()
        target = self.root / 'target'
        target.write_text('keep')
        self.lock.symlink_to(target)
        with self.assertRaisesRegex(access.AccessError, 'symlink_path_rejected'):
            access.finish_recovery(True, True)
        self.assertEqual(target.read_text(), 'keep')

    def test_replaced_lock_is_not_removed(self):
        def changed():
            self.lock.unlink()
            self.lock.write_text('new lock')
            return {'state': 'observed', 'counts': {'official_wechat': 1, 'shadow_wechat': 0, 'other_wechat': 0, 'known_provider': 0}}
        with mock.patch.object(access, 'recovery_process_check', side_effect=changed):
            with self.assertRaisesRegex(access.AccessError, 'recovery_lock_changed'):
                access.finish_recovery(True, True)
        self.assertTrue(self.lock.exists())

    def test_nonprivate_lock_is_not_removed(self):
        self.lock.chmod(0o666)
        with self.assertRaisesRegex(access.AccessError, 'recovery_lock_requires_manual_review'):
            access.finish_recovery(True, True)
        self.assertTrue(self.lock.exists())

    def test_known_material_detected_without_read_or_acquisition(self):
        from test_access import AccessOnboardingTests
        setup = AccessOnboardingTests()
        setup.setUp()
        try:
            # This test owns both the synthetic HOME and the metadata-only file.
            with mock.patch.object(access.Path, 'home', return_value=setup.root):
                material = setup.root / '.config/wxcli/config.json'
                material.parent.mkdir(parents=True)
                material.write_text('PRIVATE MATERIAL MUST NOT BE READ')
                args = setup.workflow_args()
                args.apply = True
                with mock.patch.object(access.reader, 'access_plan', return_value=setup.plan('needs_access')), \
                     mock.patch.object(access.reader, 'load_json', return_value={}) as load, \
                     mock.patch.object(access, 'run', side_effect=AssertionError('no acquisition')):
                    result = access.onboard(args)
                self.assertEqual(result['state'], 'existing_provider_material_available')
                self.assertNotIn(material, [call.args[0] for call in load.call_args_list])
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertNotIn(str(setup.root), json.dumps(result))
        finally:
            setup.tearDown()

    def test_other_known_tool_outputs_detected_without_read(self):
        from test_access import AccessOnboardingTests
        setup = AccessOnboardingTests()
        setup.setUp()
        try:
            with mock.patch.object(access.Path, 'home', return_value=setup.root):
                for relative in ('.wechat-cli/all_keys.json', '.wcdb-key-tool/wechat-passphrase.json'):
                    material = setup.root / relative
                    material.parent.mkdir(parents=True)
                    material.write_text('PRIVATE MATERIAL MUST NOT BE READ')
                args = setup.workflow_args()
                with mock.patch.object(access.reader, 'access_plan', return_value=setup.plan('needs_access')), \
                     mock.patch.object(access.reader, 'load_json', return_value={}), \
                     mock.patch.object(access, 'run', side_effect=AssertionError('no acquisition')):
                    result = access.onboard(args)
                self.assertEqual(result['state'], 'existing_provider_material_available')
                self.assertEqual([item['tool'] for item in result['material_candidates']],
                                 ['wechat-cli', 'wcdb-key-tool'])
                self.assertEqual([item['importable'] for item in result['material_candidates']], [True, False])
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertNotIn(str(setup.root), json.dumps(result))
                summary = access.support_summary(result)
                self.assertEqual(summary['known_material_tools'], ['wcdb-key-tool', 'wechat-cli'])
        finally:
            setup.tearDown()

    def test_passphrase_only_material_does_not_suggest_import(self):
        from test_access import AccessOnboardingTests
        setup = AccessOnboardingTests()
        setup.setUp()
        try:
            with mock.patch.object(access.Path, 'home', return_value=setup.root):
                material = setup.root / '.wcdb-key-tool/wechat-passphrase.json'
                material.parent.mkdir(parents=True)
                material.write_text('{}')
                with mock.patch.object(access.reader, 'access_plan', return_value=setup.plan('needs_access')), \
                     mock.patch.object(access.reader, 'load_json', return_value={}), \
                     mock.patch.object(access, 'run', side_effect=AssertionError('no acquisition')):
                    result = access.onboard(setup.workflow_args())
                self.assertIn('口令格式', result['next_actions'][0])
        finally:
            setup.tearDown()

    def test_ready_configuration_still_signals_pending_recovery(self):
        plan = {'state': 'ready', 'live_database_read_ok': True, 'scope': 'test',
                'counts': {}, 'performed': {}, 'next_actions': []}
        args = SimpleNamespace(config=self.root / 'config.json', source=None, database_root=None, max_files=500)
        with mock.patch.object(access.reader, 'access_plan', return_value=plan):
            result = access.onboard(args)
        self.assertTrue(result['recovery_review_required'])
        self.assertTrue(result['reused_existing_configuration'])


class DebuggerPreflightTests(unittest.TestCase):
    def test_complete_interface_is_checked_without_calling_methods(self):
        def no_call(*args, **kwargs):
            raise AssertionError('method invocation is not a preflight')
        class Interface:
            def __getattr__(self, name):
                return no_call
        fake = SimpleNamespace(**{name: Interface() for name in
                                 ('SBLaunchInfo', 'SBProcessInfo', 'SBProcess', 'SBListener', 'SBDebugger')})
        for name in ('SBEvent', 'SBError', 'eLaunchFlagStopAtEntry', 'eStateStopped',
                     'eStateExited', 'eStateCrashed', 'eStateDetached'):
            setattr(fake, name, no_call)
        with mock.patch.dict(sys.modules, {'lldb': fake}):
            with self.assertRaises(SystemExit) as result:
                exec(access.DEBUGGER_PROBE, {})
        self.assertEqual(result.exception.code, 0)

    def test_missing_api_is_distinct_from_import_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            for code, error in ((42, 'debugger_api_incompatible'), (1, 'debugger_unavailable')):
                with self.subTest(code=code), mock.patch.object(access.subprocess, 'run', side_effect=[
                    SimpleNamespace(stdout=directory, returncode=0), SimpleNamespace(returncode=code)]):
                    with self.assertRaisesRegex(access.AccessError, error):
                        access.preflight_debugger()

    def test_probe_does_not_create_debugger_or_launch_process(self):
        # Introspection may resolve methods, but must not invoke any of them.
        def no_call(*args, **kwargs):
            raise AssertionError('must not launch')
        class API:
            def __getattr__(self, name):
                return no_call
        with mock.patch.dict(sys.modules, {'lldb': API()}):
            with self.assertRaises(SystemExit) as result:
                exec(access.DEBUGGER_PROBE, {})
        self.assertEqual(result.exception.code, 42)

    def test_probe_rejects_partial_interface(self):
        with mock.patch.dict(sys.modules, {'lldb': SimpleNamespace(SBLaunchInfo=object)}):
            with self.assertRaises(SystemExit) as result:
                exec(access.DEBUGGER_PROBE, {})
        self.assertEqual(result.exception.code, 42)


if __name__ == '__main__':
    unittest.main()
