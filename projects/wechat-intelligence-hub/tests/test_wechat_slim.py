import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from wechat_slim import (
    AccountProfile,
    ScanCategory,
    execute_slimming,
    format_bytes,
    parse_size_str,
    scan_directory,
)


class TestWeChatSlim(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), '500.0 B')
        self.assertEqual(format_bytes(1024), '1.0 KB')
        self.assertEqual(format_bytes(1024 * 1024 * 5), '5.0 MB')
        self.assertEqual(format_bytes(1024 * 1024 * 1024 * 2.5), '2.5 GB')

    def test_parse_size_str(self):
        self.assertEqual(parse_size_str('10MB'), 10 * 1024 * 1024)
        self.assertEqual(parse_size_str('500KB'), 500 * 1024)
        self.assertEqual(parse_size_str('1GB'), 1024 * 1024 * 1024)
        self.assertEqual(parse_size_str('0B'), 0)
        self.assertEqual(parse_size_str(''), 0)

    def test_execute_slimming_safeguards_db(self):
        test_dir = Path(tempfile.mkdtemp())
        try:
            video_dir = test_dir / 'msg/video'
            video_dir.mkdir(parents=True)
            db_dir = test_dir / 'db_storage'
            db_dir.mkdir(parents=True)

            # Create mock video and db
            v_file = video_dir / 'large_video.mp4'
            v_file.write_bytes(b'x' * 1024 * 100)  # 100KB

            db_file = db_dir / 'message.db'
            db_file.write_bytes(b'sqlite_database_content')

            acc = AccountProfile(
                account_id='test_wxid',
                version_type='test',
                root_path=test_dir,
                db_path=db_dir,
                msg_video_path=video_dir,
            )

            cat_video = scan_directory('video', 'videos', video_dir)
            cat_db = scan_directory('db', 'db', db_dir, is_protected=True)

            categories = {'video': cat_video, 'db': cat_db}

            # Dry run test
            count, freed = execute_slimming(
                acc, categories, days=0, min_size_bytes=1000, selected_types=['video', 'db'], dry_run=True
            )
            self.assertEqual(count, 1)
            self.assertEqual(freed, 1024 * 100)
            self.assertTrue(v_file.exists())
            self.assertTrue(db_file.exists())

            # Archive test
            archive_dir = test_dir / 'external_ssd'
            count, freed = execute_slimming(
                acc, categories, days=0, min_size_bytes=1000, selected_types=['video', 'db'], dry_run=False, archive_to=archive_dir
            )
            self.assertEqual(count, 1)
            self.assertFalse(v_file.exists())
            self.assertTrue(db_file.exists(), 'Database file MUST NEVER be touched')
            self.assertTrue((archive_dir / 'msg/video/large_video.mp4').exists())

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


    def test_cli_integration_custom_path(self):
        """端到端集成测试: 测试 scan 与 clean --archive-to 命令行调用."""
        import subprocess

        test_dir = Path(tempfile.mkdtemp())
        archive_dir = Path(tempfile.mkdtemp())
        try:
            # 创建真实微信目录结构
            (test_dir / 'db_storage').mkdir(parents=True)
            (test_dir / 'msg/video').mkdir(parents=True)
            (test_dir / 'msg/file').mkdir(parents=True)
            (test_dir / 'cache').mkdir(parents=True)

            # 写入模拟数据
            (test_dir / 'db_storage/contact.db').write_bytes(b'sqlite_header_protected')
            (test_dir / 'msg/video/demo_presentation.mp4').write_bytes(b'0' * (1024 * 1024))  # 1MB
            (test_dir / 'msg/file/quarterly_report.pdf').write_bytes(b'1' * (512 * 1024))      # 512KB
            (test_dir / 'cache/thumb_001.tmp').write_bytes(b'2' * 2048)

            script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')

            # 1. 测试 scan 命令
            scan_res = subprocess.run(
                [sys.executable, script_path, 'scan', '--path', str(test_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(scan_res.returncode, 0)
            self.assertIn('WeChat Slim - 微信智能存储透视器', scan_res.stdout)
            self.assertIn('db_storage', scan_res.stdout)
            self.assertIn('[🔒 数据库绝对保护]', scan_res.stdout)
            self.assertIn('video', scan_res.stdout)

            # 2. 测试 clean --dry-run
            dry_res = subprocess.run(
                [sys.executable, script_path, 'clean', '--path', str(test_dir), '--types', 'video,file', '--days', '0', '--dry-run'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry_res.returncode, 0)
            self.assertIn('演练模式 Dry-Run', dry_res.stdout)
            self.assertIn('共计 2 个文件', dry_res.stdout)
            # 确认文件仍在原处
            self.assertTrue((test_dir / 'msg/video/demo_presentation.mp4').exists())
            self.assertTrue((test_dir / 'msg/file/quarterly_report.pdf').exists())

            # 3. 测试 clean --archive-to (实际执行外置归档)
            clean_res = subprocess.run(
                [sys.executable, script_path, 'clean', '--path', str(test_dir), '--types', 'video,file', '--days', '0', '--archive-to', str(archive_dir), '-f'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(clean_res.returncode, 0)
            self.assertIn('处理完成', clean_res.stdout)

            # 验证原目录大文件已被移走
            self.assertFalse((test_dir / 'msg/video/demo_presentation.mp4').exists())
            self.assertFalse((test_dir / 'msg/file/quarterly_report.pdf').exists())

            # 验证归档目录已完整保存文件与目录结构
            self.assertTrue((archive_dir / 'msg/video/demo_presentation.mp4').exists())
            self.assertTrue((archive_dir / 'msg/file/quarterly_report.pdf').exists())

            self.assertTrue((test_dir / 'db_storage/contact.db').exists())
            self.assertEqual((test_dir / 'db_storage/contact.db').read_bytes(), b'sqlite_header_protected')
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)
            shutil.rmtree(archive_dir, ignore_errors=True)

    def test_dedup_hardlink_and_trash(self):
        """测试多群重复文件查重与 APFS 硬链接替换去重."""
        from wechat_slim import find_duplicates, execute_dedup, compute_fast_hash, compute_full_hash

        test_dir = Path(tempfile.mkdtemp())
        try:
            file_dir = test_dir / 'msg/file'
            file_dir.mkdir(parents=True)

            # 创建 3 个内容完全相同的文件 (模拟转发到 3 个不同的群)
            content = b'IMPORTANT_MEETING_PRESENTATION_CONTENT' * 1000 # 38KB
            f1 = file_dir / 'chat1_meeting.pdf'
            f2 = file_dir / 'chat2_meeting.pdf'
            f3 = file_dir / 'chat3_meeting.pdf'
            unique_file = file_dir / 'other_file.pdf'

            f1.write_bytes(content)
            f2.write_bytes(content)
            f3.write_bytes(content)
            unique_file.write_bytes(b'different_content')

            cat_file = scan_directory('file', 'files', file_dir)
            categories = {'file': cat_file}

            # 1. 查重
            groups = find_duplicates(categories, ['file'], min_size_bytes=100)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].wasted_count, 2)
            self.assertEqual(groups[0].saving_bytes, len(content) * 2)
            self.assertEqual(len(groups[0].files), 3)

            # 2. 执行硬链接去重
            count, freed = execute_dedup(groups, action='hardlink', dry_run=False)
            self.assertEqual(count, 2)
            self.assertEqual(freed, len(content) * 2)

            # 3. 验证 3 个文件依然全部完好存在 (微信聊天窗口永不断链)
            self.assertTrue(f1.exists())
            self.assertTrue(f2.exists())
            self.assertTrue(f3.exists())
            self.assertEqual(f1.read_bytes(), content)
            self.assertEqual(f2.read_bytes(), content)

            # 4. 验证在文件系统层，3 个文件已指向同一个 inode (只占 1 份物理磁盘)
            st1 = f1.stat()
            st2 = f2.stat()
            st3 = f3.stat()
            self.assertEqual(st1.st_ino, st2.st_ino)
            self.assertEqual(st1.st_ino, st3.st_ino)

            # 5. 再次查重，应该感知到已硬链接，不会重复计算浪费
            cat_file2 = scan_directory('file', 'files', file_dir)
            groups2 = find_duplicates({'file': cat_file2}, ['file'], min_size_bytes=100)
            self.assertEqual(len(groups2), 1)
            self.assertEqual(groups2[0].wasted_count, 0)
            self.assertEqual(groups2[0].saving_bytes, 0)

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_cli_dedup_command(self):
        """测试 dedup 命令行调用 (dry-run 与 force 执行)."""
        import subprocess

        test_dir = Path(tempfile.mkdtemp())
        try:
            video_dir = test_dir / 'msg/video'
            video_dir.mkdir(parents=True)
            v1 = video_dir / 'shared_video_a.mp4'
            v2 = video_dir / 'shared_video_b.mp4'
            data = b'VIDEO_DATA_FOR_TESTING' * 2000 # ~44KB
            v1.write_bytes(data)
            v2.write_bytes(data)

            script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')

            # 1. 测试 dedup --dry-run
            res_dry = subprocess.run(
                [sys.executable, script_path, 'dedup', '--path', str(test_dir), '--types', 'video', '--min-size', '10KB', '--dry-run'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_dry.returncode, 0)
            self.assertIn('发现 1 组重复文件', res_dry.stdout)
            self.assertIn('演练模式', res_dry.stdout)
            self.assertNotEqual(v1.stat().st_ino, v2.stat().st_ino)

            # 2. 测试 dedup -f 执行硬链接去重
            res_run = subprocess.run(
                [sys.executable, script_path, 'dedup', '--path', str(test_dir), '--types', 'video', '--min-size', '10KB', '--action', 'hardlink', '-f'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_run.returncode, 0)
            self.assertIn('去重成功', res_run.stdout)
            self.assertEqual(v1.stat().st_ino, v2.stat().st_ino)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_web_server_endpoints(self):
        """测试 WebUI 接口响应 (GET /, GET /api/stats)."""
        import threading
        import urllib.request
        from http.server import HTTPServer
        from wechat_slim import WeChatSlimWebHandler

        test_dir = Path(tempfile.mkdtemp())
        try:
            (test_dir / 'db_storage').mkdir(parents=True)
            (test_dir / 'db_storage/test.db').write_bytes(b'db')
            WeChatSlimWebHandler.custom_path = test_dir

            server = HTTPServer(('127.0.0.1', 0), WeChatSlimWebHandler)
            port = server.server_port
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()

            # 1. 测试首页 HTML
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/') as resp:
                self.assertEqual(resp.status, 200)
                html = resp.read().decode('utf-8')
                self.assertIn('WeChat Slim', html)
                self.assertIn('<!DOCTYPE html>', html)

            # 2. 测试 /api/stats JSON 接口
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/stats') as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode('utf-8'))
                self.assertIn('account', data)
                self.assertIn('categories', data)
                self.assertIn('db', data['categories'])

            # 3. 测试 /api/whitelist 查询接口
            wl_file = test_dir / "wl.json"
            state_file = test_dir / "state.json"
            WeChatSlimWebHandler.whitelist_config = wl_file
            WeChatSlimWebHandler.state_path = state_file

            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/whitelist') as resp:
                self.assertEqual(resp.status, 200)
                wl_data = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(wl_data['rules'], [])

            # 4. 测试 POST /api/whitelist/add 添加白名单规则
            req_add = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/whitelist/add',
                data=json.dumps({'name': '老婆', 'wxid': 'wxid_wife', 'protect': 'absolute'}).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req_add) as resp:
                self.assertEqual(resp.status, 200)
                res_add = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(res_add['rule']['name'], '老婆')

            # 5. 验证白名单已存在
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/whitelist') as resp:
                self.assertEqual(resp.status, 200)
                wl_data = json.loads(resp.read().decode('utf-8'))
                self.assertEqual(len(wl_data['rules']), 1)

            # 6. 测试 GET /api/history 接口
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/history') as resp:
                self.assertEqual(resp.status, 200)
                hist_data = json.loads(resp.read().decode('utf-8'))
                self.assertIn('total_runs', hist_data)
                self.assertIn('recent_logs', hist_data)

            # 7. 测试 POST /api/whitelist/remove 移除白名单规则
            req_rm = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/whitelist/remove',
                data=json.dumps({'target': 'wxid_wife'}).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req_rm) as resp:
                self.assertEqual(resp.status, 200)
                res_rm = json.loads(resp.read().decode('utf-8'))
                self.assertTrue(res_rm['ok'])
        finally:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_cli_tag_command_and_whitelist_clean_protection(self):
        """测试 tag 命令行管理与 clean 阶段白名单绝对防删机制."""
        import subprocess

        test_dir = Path(tempfile.mkdtemp())
        wl_config = test_dir / "custom_whitelist.json"
        archive_dir = test_dir / "archive"
        try:
            script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')

            # 1. 测试 tag --add
            res_add = subprocess.run(
                [sys.executable, script_path, 'tag', '--add', '老婆', '--wxid', 'wxid_wife', '--protect', 'absolute', '--keywords', '结婚,宝宝', '--whitelist-config', str(wl_config)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_add.returncode, 0)
            self.assertIn('成功添加白名单保护规则', res_add.stdout)

            # 2. 测试 tag --list
            res_list = subprocess.run(
                [sys.executable, script_path, 'tag', '--list', '--whitelist-config', str(wl_config)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_list.returncode, 0)
            self.assertIn('老婆', res_list.stdout)
            self.assertIn('wxid_wife', res_list.stdout)

            # 3. 创建测试文件: 一个命中白名单 (结婚照片.pdf)，一个普通可删 (广告.pdf)
            file_dir = test_dir / 'msg/file'
            file_dir.mkdir(parents=True)
            protected_file = file_dir / '结婚典礼纪念.pdf'
            disposable_file = file_dir / '垃圾推销广告.pdf'
            protected_file.write_bytes(b'MEMORIES_OF_FAMILY' * 100)
            disposable_file.write_bytes(b'JUNK_ADVERTISEMENT' * 100)

            # 4. 测试 clean --dry-run 查看白名单防护日志
            res_dry = subprocess.run(
                [sys.executable, script_path, 'clean', '--path', str(test_dir), '--types', 'file', '--days', '0', '--dry-run', '--whitelist-config', str(wl_config)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_dry.returncode, 0)
            self.assertIn('白名单保护: 已自动跳过并锁定保护 1 个核心联系人文件', res_dry.stdout)

            # 5. 测试 clean --archive-to 实际执行
            res_clean = subprocess.run(
                [sys.executable, script_path, 'clean', '--path', str(test_dir), '--types', 'file', '--days', '0', '--archive-to', str(archive_dir), '-f', '--whitelist-config', str(wl_config)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_clean.returncode, 0)
            self.assertIn('白名单防删: 严格保护了 1 个核心联系人文件未被触碰', res_clean.stdout)

            # 核心断言: 受保护文件必须完好留在原处！
            self.assertTrue(protected_file.exists())
            self.assertEqual(protected_file.read_bytes(), b'MEMORIES_OF_FAMILY' * 100)

            # 普通文件已被安全归档转移
            self.assertFalse(disposable_file.exists())
            self.assertTrue((archive_dir / 'msg/file/垃圾推销广告.pdf').exists())

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_cli_stats_and_state_tracking(self):
        """测试 stats 命令与运行时状态追踪/记录."""
        import subprocess

        test_dir = Path(tempfile.mkdtemp())
        state_file = test_dir / 'state.json'
        archive_dir = test_dir / 'archive'
        script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')

        try:
            # 1. 初始执行 stats 命令
            res_stats_init = subprocess.run(
                [sys.executable, script_path, 'stats', '--state-path', str(state_file)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_stats_init.returncode, 0)
            self.assertIn('历史累计瘦身统计与审计大盘', res_stats_init.stdout)
            self.assertIn('累计运行次数 : 0 次', res_stats_init.stdout)

            # 2. 准备测试数据并运行 clean
            msg_dir = test_dir / 'msg/video'
            msg_dir.mkdir(parents=True)
            v = msg_dir / 'sample.mp4'
            v.write_bytes(b'A' * 10240) # 10KB

            res_clean = subprocess.run(
                [
                    sys.executable, script_path, 'clean',
                    '--path', str(test_dir),
                    '--types', 'video',
                    '--days', '0',
                    '--archive-to', str(archive_dir),
                    '-f',
                    '--state-path', str(state_file),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_clean.returncode, 0)

            # 3. 再次执行 stats 命令，验证累计数据与历史操作展示
            res_stats_after = subprocess.run(
                [sys.executable, script_path, 'stats', '--state-path', str(state_file)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_stats_after.returncode, 0)
            self.assertIn('累计运行次数 : 1 次', res_stats_after.stdout)
            self.assertIn('累计瘦身清理 : 1 次', res_stats_after.stdout)
            self.assertIn('10.0 KB', res_stats_after.stdout)
            self.assertIn('外置归档', res_stats_after.stdout)

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


    def test_scan_empty_and_nonexistent_directory(self):
        """测试扫描空目录及不存在目录的容错表现."""
        import subprocess

        script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')
        non_existent = Path(tempfile.gettempdir()) / "non_existent_wechat_dir_xyz_123"

        # 不存在的目录
        res_non = subprocess.run(
            [sys.executable, script_path, 'scan', '--path', str(non_existent)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_non.returncode, 0)
        self.assertIn('未在指定或默认微信容器中发现微信数据目录', res_non.stdout)

        # 空微信账号目录 (有账号目录但文件大小为 0)
        empty_dir = Path(tempfile.mkdtemp())
        try:
            (empty_dir / "user_mock").mkdir()
            res_empty = subprocess.run(
                [sys.executable, script_path, 'scan', '--path', str(empty_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_empty.returncode, 0)
            self.assertIn('0.0 B', res_empty.stdout)
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_corrupted_whitelist_and_recovery(self):
        """测试白名单配置损坏时的容错机制."""
        from engine.whitelist import WhiteListManager

        test_dir = Path(tempfile.mkdtemp())
        try:
            bad_config = test_dir / "corrupted_whitelist.yaml"
            bad_config.write_text("::: INVALID YAML & JSON {[[", encoding="utf-8")

            # 实例化损坏的配置文件，验证不会导致系统崩溃并能以安全状态初始化
            manager = WhiteListManager(config_path=bad_config)
            self.assertEqual(manager.list_rules(), [])

            # 重新写入新规则能够自我修复
            manager.add(name="领导", wxid="wxid_boss", protect="absolute")
            self.assertTrue(bad_config.exists())
            rules = manager.list_rules()
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0].name, "领导")
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_dedup_zero_size_and_singletons(self):
        """测试查重引擎对空文件与单例文件的过滤."""
        from wechat_slim import find_duplicates

        test_dir = Path(tempfile.mkdtemp())
        try:
            f_dir = test_dir / "msg/file"
            f_dir.mkdir(parents=True)
            (f_dir / "empty1.txt").write_bytes(b"")
            (f_dir / "empty2.txt").write_bytes(b"")
            (f_dir / "unique.txt").write_bytes(b"HELLO_WORLD_UNIQUE")

            cat = scan_directory("file", "files", f_dir)
            # 查重应该自动过滤空文件与非重复文件
            groups = find_duplicates({"file": cat}, ["file"], min_size_bytes=0)
            self.assertEqual(len(groups), 0)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_cli_tag_remove_command(self):
        """测试 tag --remove 子命令."""
        import subprocess

        test_dir = Path(tempfile.mkdtemp())
        wl_config = test_dir / "whitelist.json"
        script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')

        try:
            # 1. 添加
            subprocess.run(
                [sys.executable, script_path, 'tag', '--add', '重要客户', '--wxid', 'wxid_vip', '--whitelist-config', str(wl_config)],
                check=True,
                capture_output=True,
            )
            # 2. 移除
            res_rm = subprocess.run(
                [sys.executable, script_path, 'tag', '--remove', 'wxid_vip', '--whitelist-config', str(wl_config)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_rm.returncode, 0)
            self.assertIn('成功移除', res_rm.stdout)

            # 3. 列表验证已空
            res_list = subprocess.run(
                [sys.executable, script_path, 'tag', '--list', '--whitelist-config', str(wl_config)],
                capture_output=True,
                text=True,
            )
            self.assertIn('当前暂无白名单规则', res_list.stdout)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()

