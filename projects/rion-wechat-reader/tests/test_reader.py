import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from sqlcipher3 import dbapi2 as SQLCIPHER
except ImportError:
    try:
        from pysqlcipher3 import dbapi2 as SQLCIPHER
    except ImportError:
        SQLCIPHER = None

try:
    import zstandard as ZSTANDARD
except ImportError:
    ZSTANDARD = None


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "rion_wechat_reader.py"


class ReaderContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        data_root = root / "data with spaces"
        data_root.mkdir()
        self.data_root = data_root
        self.session = data_root / "session.db"
        self.contact = data_root / "contact.db"
        self.message = data_root / "message_0.db"
        self.favorite = data_root / "favorite.db"
        self.sns = data_root / "sns.db"
        self.hardlink = data_root / "hardlink.db"
        self.resource_root = data_root / "resources"
        self.config = root / "config.json"
        self.keys = root / "keys.json"
        self._build_fixtures()
        self.keys.write_text('{"keys": {}}', encoding="utf-8")
        self.keys.chmod(0o600)
        self.config.write_text(
            json.dumps(
                {
                    "session_db": str(self.session),
                    "contact_db": str(self.contact),
                    "message_dbs": [str(self.message)],
                    "favorite_db": str(self.favorite),
                    "sns_db": str(self.sns),
                    "hardlink_db": str(self.hardlink),
                    "resource_roots": [str(self.resource_root)],
                    "keys_file": str(self.keys),
                    "self_username": "wxid_me",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _build_fixtures(self):
        with sqlite3.connect(self.contact) as conn:
            conn.executescript(
                """
                CREATE TABLE contact(
                  id INTEGER PRIMARY KEY, username TEXT, nick_name TEXT, remark TEXT, alias TEXT,
                  description TEXT, local_type INTEGER
                );
                INSERT INTO contact VALUES
                  (1, 'wxid_alice', 'Alice', '合作方 Alice', 'alice', '', 1),
                  (2, 'team@chatroom', 'AI 项目群', '', '', '', 2),
                  (3, 'wxid_me', 'Me', '我', '', '', 1);
                CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT, owner TEXT, ext_buffer BLOB);
                INSERT INTO chat_room VALUES(2, 'team@chatroom', 'wxid_me', NULL);
                CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER, UNIQUE(room_id, member_id));
                INSERT INTO chatroom_member VALUES(2, 1);
                INSERT INTO chatroom_member VALUES(2, 3);
                CREATE TABLE chat_room_info_detail(
                  room_id_ INTEGER PRIMARY KEY, username_ TEXT, announcement_ TEXT,
                  announcement_editor_ TEXT, announcement_publish_time_ INTEGER
                );
                INSERT INTO chat_room_info_detail VALUES(2, 'team@chatroom', '今晚八点项目复盘', 'wxid_me', 180);
                """
            )
        with sqlite3.connect(self.session) as conn:
            conn.executescript(
                """
                CREATE TABLE SessionTable(
                  username TEXT, type INTEGER, unread_count INTEGER, summary TEXT,
                  last_timestamp INTEGER, sort_timestamp INTEGER,
                  last_msg_type INTEGER, last_msg_sub_type INTEGER,
                  last_msg_sender TEXT, last_sender_display_name TEXT
                );
                INSERT INTO SessionTable VALUES
                  ('wxid_alice', 1, 2, '方案什么时候发？', 200, 200, 1, 0, 'wxid_alice', 'Alice'),
                  ('team@chatroom', 2, 0, '今晚复盘', 100, 100, 1, 0, 'wxid_alice', 'Alice');
                """
            )
        table = "Msg_" + hashlib.md5(b"wxid_alice").hexdigest()
        group_table = "Msg_" + hashlib.md5(b"team@chatroom").hexdigest()
        with sqlite3.connect(self.message) as conn:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
            conn.execute("INSERT INTO Name2Id(rowid, user_name, is_session) VALUES(1, 'wxid_alice', 1)")
            conn.execute("INSERT INTO Name2Id(rowid, user_name, is_session) VALUES(2, 'wxid_me', 1)")
            conn.execute(
                f'''CREATE TABLE "{table}"(
                  local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
                  sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
                  status INTEGER, message_content TEXT, compress_content TEXT
                )'''
            )
            conn.execute(f'INSERT INTO "{table}" VALUES(1, 101, 1, 1, 1, 100, 0, "你好", "")')
            conn.execute(f'INSERT INTO "{table}" VALUES(2, 102, 1, 2, 2, 200, 0, "明天发方案", "")')
            conn.execute(f'''UPDATE "{table}" SET compress_content=X'FF' WHERE local_id=1''')
            conn.execute(
                f'''CREATE TABLE "{group_table}"(
                  local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
                  sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
                  status INTEGER, message_content TEXT, compress_content TEXT
                )'''
            )
            conn.execute(f'''INSERT INTO "{group_table}" VALUES(10, 110, 3, 10, 1, 300, 0, '<msg><img md5="abc123" length="42"/></msg>', '')''')
            conn.execute(f'''INSERT INTO "{group_table}" VALUES(11, 111, 49, 11, 1, 310, 0, '<msg><appmsg><type>2000</type><title>转账</title></appmsg><wcpayinfo><paysubtype>1</paysubtype></wcpayinfo></msg>', '')''')
            conn.execute(f'''INSERT INTO "{group_table}" VALUES(12, 112, 49, 12, 1, 320, 0, '<msg><appmsg><type>2001</type></appmsg><wcpayinfo><nativeurl>wxpay://hongbao/example</nativeurl></wcpayinfo></msg>', '')''')
            conn.execute(f'''INSERT INTO "{group_table}" VALUES(13, 113, 49, 13, 1, 330, 0, '<msg><appmsg><type>19</type><title>聊天记录</title></appmsg><recorditem>fixture</recorditem></msg>', '')''')
        with sqlite3.connect(self.favorite) as conn:
            conn.execute(
                "CREATE TABLE fav_db_item(local_id INTEGER, server_id INTEGER, type INTEGER, "
                "update_time INTEGER, content TEXT, fromusr TEXT, realchatname TEXT)"
            )
            conn.execute(
                "INSERT INTO fav_db_item VALUES(1, 9001, 1, 240, '收藏的 AI 工作流', 'wxid_alice', '')"
            )
        with sqlite3.connect(self.sns) as conn:
            conn.execute("CREATE TABLE SnsTimeLine(tid INTEGER, user_name TEXT, content TEXT, pack_info_buf BLOB)")
            conn.execute(
                "INSERT INTO SnsTimeLine VALUES(1001, 'wxid_alice', "
                "'<TimelineObject><createTime>250</createTime><contentDesc><![CDATA[AI 产品发布]]></contentDesc></TimelineObject>', X'FF')"
            )
            conn.execute(
                "CREATE TABLE SnsMessage_tmp3(local_id INTEGER, create_time INTEGER, type INTEGER, "
                "feed_id INTEGER, is_unread INTEGER, from_username TEXT, from_nickname TEXT, "
                "to_username TEXT, to_nickname TEXT, content TEXT)"
            )
            conn.execute(
                "INSERT INTO SnsMessage_tmp3 VALUES(1, 260, 1, 1001, 1, 'wxid_alice', "
                "'Alice', 'wxid_me', 'Me', '写得很好')"
            )
        with sqlite3.connect(self.hardlink) as conn:
            conn.execute("CREATE TABLE dir2id(username TEXT)")
            conn.execute("INSERT INTO dir2id(rowid, username) VALUES(1, 'd1')")
            conn.execute("INSERT INTO dir2id(rowid, username) VALUES(2, 'd2')")
            for table in ("image_hardlink_info_v4", "video_hardlink_info_v4", "file_hardlink_info_v4"):
                conn.execute(
                    f'CREATE TABLE "{table}"(md5_hash INTEGER, md5 TEXT, type INTEGER, file_name TEXT, '
                    "file_size INTEGER, modify_time INTEGER, dir1 INTEGER, dir2 INTEGER, "
                    "_rowid_ INTEGER, extra_buffer BLOB)"
                )
            conn.execute(
                "INSERT INTO image_hardlink_info_v4 VALUES(1, 'abc123', 3, 'photo.dat', 42, 300, 1, 2, 1, NULL)"
            )
        media_dir = self.resource_root / "attach" / "d1" / "d2" / "Img"
        media_dir.mkdir(parents=True)
        (media_dir / "photo.dat").write_bytes(b"fixture-image")

    def run_cli(self, *args):
        result = subprocess.run(
            [sys.executable, str(CLI), "--config", str(self.config), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def run_with_config(self, config, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(CLI), "--config", str(config), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def encrypt_fixture(self, source, target, key):
        self.assertIsNotNone(SQLCIPHER)
        conn = SQLCIPHER.connect(str(target))
        try:
            conn.execute(f"PRAGMA key=\"x'{key}'\"")
            quoted = str(source).replace("'", "''")
            conn.execute(f"ATTACH DATABASE '{quoted}' AS plaintext KEY ''")
            conn.execute("SELECT sqlcipher_export('main', 'plaintext')")
            conn.execute("DETACH DATABASE plaintext")
        finally:
            conn.close()

    def test_status_reports_live_ready(self):
        payload = self.run_cli("status")
        self.assertTrue(payload["data"]["status"]["live_database_read_ok"])
        self.assertEqual(payload["data"]["status"]["readiness"], "ready")
        self.assertTrue(payload["data"]["status"]["schema"]["messages"]["compatible"])

    def test_version_uses_single_public_cli_name(self):
        payload = self.run_cli("version")
        self.assertEqual(payload["data"]["name"], "rion-wechat-cli")
        self.assertEqual(payload["data"]["version"], "0.9.2-preview.2")

    def test_import_access_converts_explicit_bundle_without_exposing_keys(self):
        source = Path(self.temp.name) / "legacy-access.json"
        destination = Path(self.temp.name) / "imported-keys.json"
        key = "44" * 32
        source.write_text(
            json.dumps(
                {
                    "_metadata": {"format": "legacy-compatible"},
                    "message_0.db": {"enc_key": key},
                }
            ),
            encoding="utf-8",
        )
        source.chmod(0o600)
        payload = self.run_cli(
            "import-access",
            "--source",
            str(source),
            "--database-root",
            str(self.data_root),
            "--keys-file",
            str(destination),
            "--no-verify",
        )
        self.assertEqual(payload["data"]["imported_key_count"], 1)
        self.assertEqual(payload["data"]["metadata_entry_count"], 1)
        self.assertNotIn(key, json.dumps(payload))
        imported = json.loads(destination.read_text(encoding="utf-8"))["keys"]
        self.assertEqual(imported[str(self.message.resolve())]["key"], key)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_import_access_rejects_broad_source_permissions(self):
        source = Path(self.temp.name) / "unsafe-access.json"
        source.write_text(json.dumps({"message_0.db": {"enc_key": "44" * 32}}), encoding="utf-8")
        source.chmod(0o644)
        payload = self.run_with_config(
            self.config,
            "import-access",
            "--source",
            str(source),
            "--database-root",
            str(self.data_root),
            "--keys-file",
            str(Path(self.temp.name) / "unused.json"),
            "--no-verify",
            expected=1,
        )
        self.assertEqual(payload["error"]["code"], "unsafe_key_permissions")

    def test_path_access_requires_explicit_database_root(self):
        source = Path(self.temp.name) / "path-access.json"
        source.write_text(json.dumps({"message_0.db": {"enc_key": "44" * 32}}), encoding="utf-8")
        source.chmod(0o600)
        payload = self.run_with_config(
            self.config,
            "import-access",
            "--source",
            str(source),
            "--keys-file",
            str(Path(self.temp.name) / "unused.json"),
            "--no-verify",
            expected=1,
        )
        self.assertEqual(payload["error"]["code"], "database_root_required")

    def test_self_test_verifies_read_only_runtime(self):
        payload = self.run_cli("self-test")
        self.assertTrue(payload["data"]["passed"])
        self.assertTrue(all(payload["data"]["checks"].values()))
        self.assertIn("available", payload["data"]["sqlcipher"])

    def test_tool_discovery_exposes_only_implemented_commands(self):
        payload = self.run_cli("tools", "--profile", "all")
        commands = {item["command"] for item in payload["data"]["tools"]}
        self.assertTrue(
            {"sessions", "timeline", "search-context", "tail", "unread", "stats", "media", "transfers"}
            <= commands
        )
        history = next(item for item in payload["data"]["tools"] if item["command"] == "history")
        self.assertIn("after_message_server_id_str", history["inputSchema"]["properties"])

    def test_tool_schema_resolves_compatibility_alias(self):
        payload = self.run_cli("tool-schema", "search_with_context")
        self.assertEqual(payload["data"]["command"]["name"], "search-context")

    def test_doctor_reports_ready(self):
        payload = self.run_cli("doctor")
        self.assertEqual(payload["data"]["summary"], "ready")
        self.assertEqual(payload["data"]["diagnostics"][0]["code"], "ready")

    def test_discover_classifies_plaintext_databases(self):
        payload = self.run_cli("discover", "--root", str(self.data_root))
        candidates = payload["data"]["candidates"]
        self.assertEqual(candidates["session"], [str(self.session.resolve())])
        self.assertEqual(candidates["contact"], [str(self.contact.resolve())])
        self.assertEqual(candidates["messages"], [str(self.message.resolve())])
        self.assertEqual(candidates["favorite"], [str(self.favorite.resolve())])
        self.assertEqual(candidates["sns"], [str(self.sns.resolve())])
        self.assertEqual(candidates["hardlink"], [str(self.hardlink.resolve())])

    def test_discover_counts_encrypted_or_unreadable_databases(self):
        encrypted = self.data_root / "encrypted.db"
        encrypted.write_bytes(b"not-a-plaintext-sqlite-database")
        payload = self.run_cli("discover", "--root", str(self.data_root))
        self.assertEqual(payload["data"]["unreadable_or_encrypted_count"], 1)

    def test_discover_skips_attachment_trees(self):
        attachment = self.data_root / "attachments" / "deep"
        attachment.mkdir(parents=True)
        (attachment / "noise.db").write_bytes(b"not-sqlite")
        payload = self.run_cli("discover", "--root", str(self.data_root))
        self.assertEqual(payload["data"]["unreadable_or_encrypted_count"], 0)

    def test_setup_is_the_single_first_run_command(self):
        root = Path(self.temp.name) / "one-command-config"
        config = root / "config.json"
        keys = root / "keys.json"
        payload = self.run_with_config(
            config,
            "setup",
            "--database-root",
            str(self.data_root),
            "--keys-file",
            str(keys),
            "--self-username",
            "wxid_me",
        )
        self.assertEqual(payload["data"]["doctor"]["summary"], "ready")
        self.assertTrue(config.exists())
        configured = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(configured["favorite_db"], str(self.favorite.resolve()))
        self.assertEqual(configured["sns_db"], str(self.sns.resolve()))
        self.assertEqual(configured["hardlink_db"], str(self.hardlink.resolve()))

    def test_access_plan_reuses_ready_config_without_writing(self):
        before = {p: p.read_bytes() for p in (self.config, self.keys, self.session, self.contact, self.message)}
        plan = self.run_cli("access-plan")["data"]
        self.assertEqual(plan["state"], "ready")
        self.assertTrue(plan["live_database_read_ok"])
        self.assertFalse(any(plan["performed"].values()))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        output = json.dumps(plan)
        for private in (str(self.data_root), "wxid_alice", "方案什么时候发"):
            self.assertNotIn(private, output)

    def test_access_plan_does_not_write_first_run_config(self):
        config = Path(self.temp.name) / "not-created.json"
        plan = self.run_with_config(config, "access-plan", "--database-root", str(self.data_root), "--keys-file", str(self.keys))["data"]
        self.assertEqual(plan["state"], "ready_to_configure")
        self.assertFalse(config.exists())
        self.assertFalse(plan["live_database_read_ok"])

    def test_access_plan_explicit_root_does_not_reuse_unrelated_ready_config(self):
        root = Path(self.temp.name) / "another-account"
        root.mkdir()
        (root / "encrypted.db").write_bytes(b"unknown-database")
        keys = root / "missing-keys.json"
        plan = self.run_cli("access-plan", "--database-root", str(root), "--keys-file", str(keys))["data"]
        self.assertEqual(plan["state"], "needs_access")
        self.assertFalse(keys.exists())
        self.assertFalse(plan["performed"]["provider_execution"])

    def test_access_plan_rejects_broad_key_permissions(self):
        self.keys.chmod(0o644)
        plan = self.run_cli("access-plan")["data"]
        self.assertEqual(plan["state"], "unsafe_key_permissions")
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o644)

    def test_access_plan_empty_key_map_is_missing_material(self):
        root = Path(self.temp.name) / "empty-key-map"
        root.mkdir()
        (root / "encrypted.db").write_bytes(b"unknown-database")
        plan = self.run_cli("access-plan", "--database-root", str(root), "--keys-file", str(self.keys))["data"]
        self.assertEqual(plan["state"], "needs_access")

    def test_access_plan_invalid_material_is_redacted(self):
        secret = "45" * 32
        self.keys.write_text('{"secret":"' + secret, encoding="utf-8")
        plan = self.run_cli("access-plan")["data"]
        self.assertEqual(plan["state"], "invalid_access_material")
        self.assertNotIn(secret, json.dumps(plan))
        self.assertNotIn(str(self.keys), json.dumps(plan))

    def test_access_plan_malformed_config_is_redacted(self):
        self.config.write_text("not-json-private-content", encoding="utf-8")
        plan = self.run_cli("access-plan")["data"]
        self.assertEqual(plan["state"], "configuration_check_failed")
        self.assertNotIn("not-json-private-content", json.dumps(plan))

    def test_access_plan_requires_account_selection(self):
        root = Path(self.temp.name) / "accounts"
        for name in ("account-a", "account-b"):
            (root / name / "db_storage").mkdir(parents=True)
        plan = self.run_cli("access-plan", "--database-root", str(root))["data"]
        self.assertEqual(plan["state"], "account_selection_required")
        self.assertEqual(plan["counts"]["account_candidates"], 2)
        self.assertNotIn("account-a", json.dumps(plan))

    def test_access_plan_scan_limit_is_not_ready(self):
        plan = self.run_cli("access-plan", "--database-root", str(self.data_root), "--max-files", "1")["data"]
        self.assertEqual(plan["state"], "scan_incomplete")

    def test_access_plan_unreadable_file_is_not_missing_key(self):
        (self.data_root / "broken.db").symlink_to(self.data_root / "does-not-exist")
        plan = self.run_cli("access-plan", "--database-root", str(self.data_root))["data"]
        self.assertEqual(plan["state"], "filesystem_access_required")

    def test_access_plan_partial_coverage_is_not_ready(self):
        (self.data_root / "unresolved.db").write_bytes(b"unknown-database")
        plan = self.run_cli("access-plan", "--database-root", str(self.data_root))["data"]
        self.assertEqual(plan["state"], "partial")
        self.assertFalse(plan["live_database_read_ok"])

    def test_access_plan_missing_database_does_not_request_key(self):
        self.message.unlink()
        plan = self.run_cli("access-plan")["data"]
        self.assertEqual(plan["state"], "database_missing")

    def test_access_plan_missing_core_key(self):
        self.message.write_bytes(b"unknown-database")
        plan = self.run_cli("access-plan")["data"]
        self.assertEqual(plan["state"], "needs_access")
        self.assertEqual(plan["counts"]["core_databases_without_key"], 1)

    @unittest.skipIf(SQLCIPHER is None, "SQLCipher driver unavailable")
    def test_access_plan_verifies_encrypted_candidates_without_saving_config(self):
        root = Path(self.temp.name) / "encrypted-plan"
        root.mkdir()
        key = "36" * 32
        for source in (self.session, self.contact, self.message):
            self.encrypt_fixture(source, root / source.name, key)
        keys = root / "keys.json"
        keys.write_text(json.dumps({"keys": {"*": key}}), encoding="utf-8")
        keys.chmod(0o600)
        config = root / "config.json"
        plan = self.run_with_config(config, "access-plan", "--database-root", str(root), "--keys-file", str(keys))["data"]
        self.assertEqual(plan["state"], "ready_to_configure")
        self.assertNotIn(key, json.dumps(plan))
        self.assertFalse(config.exists())
        keys.write_text(json.dumps({"keys": {"*": "37" * 32}}), encoding="utf-8")
        failed = self.run_with_config(config, "access-plan", "--database-root", str(root), "--keys-file", str(keys))["data"]
        self.assertEqual(failed["state"], "verification_failed")

    def test_setup_reports_the_exact_encrypted_database_blocker(self):
        encrypted_root = Path(self.temp.name) / "encrypted-only"
        encrypted_root.mkdir()
        (encrypted_root / "message.db").write_bytes(b"encrypted-placeholder")
        config = encrypted_root / "config.json"
        payload = self.run_with_config(
            config,
            "setup",
            "--database-root",
            str(encrypted_root),
            "--keys-file",
            str(encrypted_root / "keys.json"),
            expected=1,
        )
        self.assertEqual(payload["error"]["code"], "database_access_material_required")

    def test_init_writes_private_config_and_keys(self):
        root = Path(self.temp.name) / "new-config"
        config = root / "config.json"
        keys = root / "keys.json"
        payload = self.run_with_config(
            config,
            "init",
            "--session-db",
            str(self.session),
            "--contact-db",
            str(self.contact),
            "--message-db",
            str(self.message),
            "--keys-file",
            str(keys),
            "--self-username",
            "wxid_me",
            "--favorite-db",
            str(self.favorite),
            "--sns-db",
            str(self.sns),
            "--hardlink-db",
            str(self.hardlink),
            "--resource-root",
            str(self.resource_root),
        )
        self.assertTrue(payload["data"]["config_written"])
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(keys.stat().st_mode & 0o777, 0o600)
        configured = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(configured["favorite_db"], str(self.favorite.resolve()))
        self.assertEqual(configured["sns_db"], str(self.sns.resolve()))
        self.assertEqual(configured["hardlink_db"], str(self.hardlink.resolve()))
        self.assertEqual(configured["resource_roots"], [str(self.resource_root.resolve())])
        doctor_payload = self.run_with_config(config, "doctor")
        self.assertEqual(doctor_payload["data"]["summary"], "ready")

    def test_init_refuses_to_overwrite_existing_config(self):
        payload = self.run_with_config(
            self.config,
            "init",
            "--session-db",
            str(self.session),
            "--contact-db",
            str(self.contact),
            "--message-db",
            str(self.message),
            "--keys-file",
            str(self.keys),
            expected=1,
        )
        self.assertEqual(payload["error"]["code"], "reader_error")

    def test_sessions_are_filtered_and_named(self):
        payload = self.run_cli("sessions", "--type-filter", "private", "--limit", "10")
        rows = payload["data"]["sessions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "合作方 Alice")
        self.assertEqual(rows[0]["unread_count"], 2)

    def test_sessions_accept_realistic_schema_without_optional_sort_and_type_columns(self):
        session = Path(self.temp.name) / "session-minimal.db"
        with sqlite3.connect(session) as conn:
            conn.execute(
                "CREATE TABLE SessionTable(username TEXT, unread_count INTEGER, summary TEXT, "
                "last_timestamp INTEGER, last_msg_type INTEGER, last_msg_sender TEXT, "
                "last_sender_display_name TEXT)"
            )
            conn.execute(
                "INSERT INTO SessionTable VALUES('wxid_alice', 3, '真实会话摘要', 600, 1, 'wxid_alice', 'Alice')"
            )
        config = Path(self.temp.name) / "session-minimal-config.json"
        config.write_text(
            json.dumps(
                {
                    "session_db": str(session),
                    "contact_db": str(self.contact),
                    "message_dbs": [str(self.message)],
                    "keys_file": str(self.keys),
                }
            ),
            encoding="utf-8",
        )
        rows = self.run_with_config(config, "sessions")["data"]["sessions"]
        self.assertEqual(rows[0]["summary"], "真实会话摘要")
        self.assertEqual(rows[0]["display_name"], "合作方 Alice")
        self.assertIsNone(rows[0]["sort_timestamp"])

    def test_contacts_without_database_returns_error_not_empty_success(self):
        missing_config = Path(self.temp.name) / "missing-config.json"
        payload = self.run_with_config(missing_config, "contacts", "--limit", "1", expected=1)
        self.assertEqual(payload["error"]["code"], "database_not_configured")

    def test_invalid_authorized_key_spec_fails_before_driver_loading(self):
        root = Path(self.temp.name) / "invalid-key"
        root.mkdir()
        keys = root / "keys.json"
        keys.write_text(json.dumps({"keys": {"session.db": {"key": "abcd"}}}), encoding="utf-8")
        keys.chmod(0o600)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "session_db": str(self.session),
                    "contact_db": str(self.contact),
                    "message_dbs": [str(self.message)],
                    "keys_file": str(keys),
                }
            ),
            encoding="utf-8",
        )
        payload = self.run_with_config(config, "sessions", expected=1)
        self.assertEqual(payload["error"]["code"], "invalid_key")

    def test_resolve_chat_by_remark(self):
        payload = self.run_cli("resolve-chat", "合作方 Alice")
        self.assertEqual(payload["data"]["candidates"][0]["username"], "wxid_alice")

    def test_resolve_chat_group_by_name_when_group_is_a_contact(self):
        payload = self.run_cli("resolve-chat", "AI 项目群", "--type-filter", "group")
        self.assertEqual(payload["data"]["candidates"][0]["username"], "team@chatroom")

    def _drop_group_contact_rows(self):
        # Windows WeChat 4.x keeps groups out of the contact table (issue #82).
        with sqlite3.connect(self.contact) as conn:
            conn.execute("DELETE FROM contact WHERE username LIKE '%@chatroom'")

    def test_resolve_chat_group_id_when_group_is_not_a_contact(self):
        self._drop_group_contact_rows()
        payload = self.run_cli("resolve-chat", "team@chatroom", "--type-filter", "group")
        candidate = payload["data"]["candidates"][0]
        self.assertEqual(candidate["username"], "team@chatroom")
        self.assertEqual(candidate["chat_type"], "group")

    def test_resolve_chat_lists_session_only_groups(self):
        self._drop_group_contact_rows()
        payload = self.run_cli("resolve-chat", "chatroom", "--type-filter", "group")
        self.assertEqual([row["username"] for row in payload["data"]["candidates"]], ["team@chatroom"])

    def test_resolve_chat_passes_unknown_group_id_through(self):
        payload = self.run_cli("resolve-chat", "999@chatroom")
        self.assertEqual(payload["data"]["candidates"][0]["username"], "999@chatroom")

    def test_timeline_contract_and_order(self):
        payload = self.run_cli("timeline", "合作方 Alice", "--limit", "10", "--display-order", "asc")
        rows = payload["data"]["messages"]
        self.assertEqual([row["text"] for row in rows], ["你好", "明天发方案"])
        self.assertFalse(rows[0]["from_me"])
        self.assertTrue(rows[1]["from_me"])

    def test_timeline_since_filter(self):
        payload = self.run_cli("timeline", "wxid_alice", "--since", "150", "--limit", "10")
        rows = payload["data"]["messages"]
        self.assertEqual([row["local_id"] for row in rows], [2])

    def test_composite_message_types_and_group_sender_prefix_are_normalized(self):
        table = "Msg_" + hashlib.md5(b"team@chatroom").hexdigest()
        with sqlite3.connect(self.message) as conn:
            conn.execute(
                f'''INSERT INTO "{table}" VALUES(14, 114, ?, 14, 1, 340, 0,
                '<msg><appmsg><type>57</type></appmsg></msg>', '')''',
                ((57 << 32) | 49,),
            )
            conn.execute(
                f'''INSERT INTO "{table}" VALUES(15, 115, ?, 15, 1, 350, 0,
                '<msg><appmsg><type>33</type></appmsg></msg>', '')''',
                ((33 << 32) | 49,),
            )
            conn.execute(
                f'''INSERT INTO "{table}" VALUES(16, 116, 48, 16, 1, 360, 0,
                'location-payload', '')'''
            )
            conn.execute(
                f'''INSERT INTO "{table}" VALUES(17, 117, 1, 17, 1, 370, 0,
                'wxid_alice:\n群聊正文', '')'''
            )
            conn.execute(
                f'''INSERT INTO "{table}" VALUES(18, 118, ?, 18, 1, 380, 0,
                'solitaire-payload', '')''',
                ((53 << 32) | 49,),
            )
            for local_id, subtype, payload in (
                (19, 8, "file-payload"),
                (20, 51, "channel-video-payload"),
                (21, 62, "pat-payload"),
            ):
                conn.execute(
                    f'''INSERT INTO "{table}" VALUES(?, ?, ?, ?, 1, ?, 0, ?, '')''',
                    (local_id, 100 + local_id, (subtype << 32) | 49, local_id, 380 + local_id, payload),
                )
            conn.execute(f'''INSERT INTO "{table}" VALUES(22, 122, 50, 22, 1, 402, 0, 'voip-payload', '')''')
            conn.execute(f'''INSERT INTO "{table}" VALUES(23, 123, 67, 23, 1, 403, 0, 'unknown-payload', '')''')
        rows = self.run_cli("timeline", "team@chatroom", "--limit", "20")["data"]["messages"]
        by_id = {row["local_id"]: row for row in rows}
        self.assertEqual(by_id[14]["kind_name"], "quote")
        self.assertEqual(by_id[15]["kind_name"], "miniprogram")
        self.assertEqual(by_id[16]["kind_name"], "location")
        self.assertEqual(by_id[17]["text"], "群聊正文")
        self.assertEqual(by_id[18]["kind_name"], "solitaire")
        self.assertEqual(by_id[19]["kind_name"], "file")
        self.assertEqual(by_id[20]["kind_name"], "channel_video")
        self.assertEqual(by_id[21]["kind_name"], "pat")
        self.assertEqual(by_id[22]["kind_name"], "voip")
        self.assertEqual(by_id[23]["kind_name"], "unknown")

    def test_wcdb_projection_accepts_missing_optional_message_columns(self):
        message = Path(self.temp.name) / "message-wcdb-minimal.db"
        table = "Msg_" + hashlib.md5(b"wxid_alice").hexdigest()
        with sqlite3.connect(message) as conn:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
            conn.execute("INSERT INTO Name2Id(rowid, user_name, is_session) VALUES(1, 'wxid_alice', 1)")
            conn.execute(
                f'''CREATE TABLE "{table}"(
                  local_id INTEGER PRIMARY KEY, local_type INTEGER, real_sender_id INTEGER,
                  create_time INTEGER, message_content TEXT, WCDB_CT_message_content INTEGER
                )'''
            )
            conn.execute(f'''INSERT INTO "{table}" VALUES(1, 1, 1, 400, '真实 WCDB 字段', 0)''')
        config = Path(self.temp.name) / "wcdb-minimal-config.json"
        config.write_text(
            json.dumps(
                {
                    "session_db": str(self.session),
                    "contact_db": str(self.contact),
                    "message_dbs": [str(message)],
                    "keys_file": str(self.keys),
                    "self_username": "wxid_me",
                }
            ),
            encoding="utf-8",
        )
        timeline = self.run_with_config(config, "timeline", "wxid_alice")["data"]["messages"]
        self.assertEqual(timeline[0]["text"], "真实 WCDB 字段")
        self.assertIsNone(timeline[0]["server_id"])

        unavailable = self.run_with_config(
            config,
            "context",
            "wxid_alice",
            "--server-id",
            "123",
            expected=1,
        )
        self.assertEqual(unavailable["error"]["code"], "server_id_not_available")

    @unittest.skipUnless(ZSTANDARD is not None, "zstandard driver not installed")
    def test_wcdb_zstandard_content_is_decoded_for_timeline_and_search(self):
        message = Path(self.temp.name) / "message-wcdb-zstandard.db"
        table = "Msg_" + hashlib.md5(b"wxid_alice").hexdigest()
        compressed = ZSTANDARD.ZstdCompressor().compress("压缩合作方案".encode("utf-8"))
        with sqlite3.connect(message) as conn:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
            conn.execute("INSERT INTO Name2Id(rowid, user_name, is_session) VALUES(1, 'wxid_alice', 1)")
            conn.execute(
                f'''CREATE TABLE "{table}"(
                  local_id INTEGER PRIMARY KEY, local_type INTEGER, real_sender_id INTEGER,
                  create_time INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER
                )'''
            )
            conn.execute(f'''INSERT INTO "{table}" VALUES(1, 1, 1, 500, ?, 4)''', (compressed,))
        config = Path(self.temp.name) / "wcdb-zstandard-config.json"
        config.write_text(
            json.dumps(
                {
                    "session_db": str(self.session),
                    "contact_db": str(self.contact),
                    "message_dbs": [str(message)],
                    "keys_file": str(self.keys),
                }
            ),
            encoding="utf-8",
        )
        timeline = self.run_with_config(config, "timeline", "wxid_alice")["data"]["messages"]
        self.assertEqual(timeline[0]["text"], "压缩合作方案")
        search = self.run_with_config(config, "search", "合作", "--search-mode", "substring")["data"]["messages"]
        self.assertEqual([row["local_id"] for row in search], [1])

        regex = self.run_with_config(config, "search", "压缩.*方案", "--search-mode", "regex")["data"]["messages"]
        self.assertEqual([row["local_id"] for row in regex], [1])

    def test_message_compatibility_aliases_and_filters(self):
        payload = self.run_cli(
            "messages",
            "--talker",
            "wxid_alice",
            "--after-local-id",
            "1",
            "--from-me",
            "--fields",
            "local_id,text",
            "--display-order",
            "desc",
        )
        self.assertEqual(payload["data"]["messages"], [{"local_id": 2, "text": "明天发方案"}])

        context = self.run_cli(
            "message-context",
            "--talker",
            "wxid_alice",
            "--server-id-str",
            "102",
            "--before-messages",
            "1",
            "--after-messages",
            "0",
        )
        self.assertEqual([row["local_id"] for row in context["data"]["messages"]], [1, 2])

    def test_context_marks_anchor(self):
        payload = self.run_cli("context", "wxid_alice", "--local-id", "2", "--before-count", "1")
        rows = payload["data"]["messages"]
        self.assertEqual([row["local_id"] for row in rows], [1, 2])
        self.assertEqual(rows[-1]["context_role"], "anchor")

    def test_search_across_sessions(self):
        payload = self.run_cli("search", "方案", "--limit", "10")
        rows = payload["data"]["messages"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["local_id"], 2)

    def test_search_with_context_returns_match_and_neighbors(self):
        payload = self.run_cli("search-context", "方案", "--before-count", "1", "--after-count", "1")
        rows = payload["data"]["results"]
        self.assertEqual(rows[0]["match"]["local_id"], 2)
        self.assertEqual([row["local_id"] for row in rows[0]["context"]], [1, 2])

    def test_search_filters_before_pagination_and_has_more_uses_lookahead(self):
        first = self.run_cli("search", ".", "--search-mode", "regex", "--type", "text",
                             "--limit", "1")["data"]
        self.assertEqual([r["local_id"] for r in first["messages"]], [2])
        self.assertTrue(first["query"]["has_more"])
        second = self.run_cli("search", ".", "--search-mode", "regex", "--type", "text",
                              "--limit", "1", "--offset", "1")["data"]
        self.assertEqual([r["local_id"] for r in second["messages"]], [1])
        self.assertFalse(second["query"]["has_more"])
        incoming = self.run_cli("search", ".", "--search-mode", "regex", "--type", "text",
                                "--no-from-me", "--limit", "1")["data"]
        self.assertEqual([r["local_id"] for r in incoming["messages"]], [1])
        self.assertFalse(incoming["query"]["has_more"])

    def test_search_snippet_and_no_text_do_not_leak_raw_body(self):
        for flag in ("--snippet-only", "--no-include-text"):
            result = self.run_cli("search", "你好", flag, "--max-text-chars", "1")["data"]["messages"][0]
            self.assertNotIn("message_content", result)
            self.assertNotIn("compress_content", result)
            if flag == "--snippet-only":
                self.assertEqual(result["text"], "你")
            else:
                self.assertNotIn("text", result)
                self.assertNotIn("content", result)
        filtered = self.run_cli(
            "search-with-context",
            "明天发方案",
            "--talker",
            "wxid_alice",
            "--search-mode",
            "exact",
            "--from-me",
        )
        self.assertEqual(filtered["data"]["results"][0]["match"]["local_id"], 2)

    def test_tail_returns_only_messages_after_cursor(self):
        payload = self.run_cli("tail", "wxid_alice", "--since-local-id", "1", "--limit", "10")
        self.assertEqual([row["local_id"] for row in payload["data"]["events"]], [2])
        self.assertEqual(payload["data"]["cursor"], 2)
        alias_payload = self.run_cli("read_events", "--talker", "wxid_alice", "--cursor", "1")
        self.assertEqual([row["local_id"] for row in alias_payload["data"]["events"]], [2])

    def test_tail_jsonl_and_bounded_follow(self):
        jsonl_row = self.run_with_config(
            self.config,
            "tail",
            "wxid_alice",
            "--since-local-id",
            "1",
            "--jsonl",
        )
        self.assertEqual(jsonl_row["local_id"], 2)
        follow_row = self.run_with_config(
            self.config,
            "tail",
            "wxid_alice",
            "--since-local-id",
            "1",
            "--follow",
            "--max-polls",
            "1",
            "--poll-interval",
            "0.1",
        )
        self.assertEqual(follow_row["local_id"], 2)

    def test_unread_and_stats(self):
        unread_payload = self.run_cli("unread", "--limit", "10")
        self.assertEqual([row["username"] for row in unread_payload["data"]["sessions"]], ["wxid_alice"])
        stats_payload = self.run_cli("stats")
        self.assertEqual(stats_payload["data"]["session_count"], 2)
        self.assertEqual(stats_payload["data"]["unread_session_count"], 1)
        self.assertEqual(stats_payload["data"]["contact_count"], 3)

    def test_group_members_and_schema(self):
        members_payload = self.run_cli("members", "AI 项目群", "--limit", "10")
        self.assertEqual(members_payload["data"]["total"], 2)
        self.assertEqual(
            [row["username"] for row in members_payload["data"]["members"]],
            ["wxid_alice", "wxid_me"],
        )
        schema_payload = self.run_cli("schema", "--subdir", "contact", "--file", "contact.db")
        table_names = {row["name"] for row in schema_payload["data"]["databases"][0]["tables"]}
        self.assertTrue({"contact", "chat_room", "chatroom_member"} <= table_names)

    def test_agent_cache_status_and_export(self):
        agent_payload = self.run_cli("agent", "--mode", "coverage", "--include-status")
        self.assertTrue(agent_payload["data"]["coverage"]["complete_local_history"])
        cache_payload = self.run_cli("cache-status")
        self.assertEqual(cache_payload["data"]["mode"], "snapshot_only")
        self.assertFalse(cache_payload["data"]["persistent_cache"])
        refresh_payload = self.run_cli("cache-refresh")
        rebuild_payload = self.run_cli("cache-rebuild")
        self.assertEqual(refresh_payload["data"]["reason"], "no_persistent_content_cache")
        self.assertEqual(rebuild_payload["data"]["reason"], "no_persistent_content_cache")

        output = Path(self.temp.name) / "exports" / "chat.jsonl"
        export_payload = self.run_cli(
            "export",
            "wxid_alice",
            "--path",
            str(output),
            "--format",
            "jsonl",
        )
        self.assertEqual(export_payload["data"]["message_count"], 2)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        exported = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["local_id"] for row in exported], [1, 2])
        self.assertEqual(exported[0]["compress_content"]["encoding"], "base64")
        blocked = self.run_with_config(
            self.config,
            "export",
            "wxid_alice",
            "--path",
            str(output),
            expected=1,
        )
        self.assertEqual(blocked["error"]["code"], "output_exists")

    def test_announcements_and_restricted_sql(self):
        announcement_payload = self.run_cli("announcements", "team@chatroom", "--limit", "10")
        announcements = announcement_payload["data"]["announcements"]
        self.assertEqual(len(announcements), 1)
        self.assertEqual(announcements[0]["announcement"], "今晚八点项目复盘")

        sql_payload = self.run_cli(
            "sql",
            "SELECT username, unread_count FROM SessionTable ORDER BY sort_timestamp DESC",
            "--subdir",
            "session",
            "--limit",
            "1",
        )
        self.assertEqual(sql_payload["data"]["returned"], 1)
        self.assertEqual(sql_payload["data"]["rows"][0]["username"], "wxid_alice")
        blocked = self.run_with_config(
            self.config,
            "sql",
            "DELETE FROM SessionTable",
            "--subdir",
            "session",
            expected=1,
        )
        self.assertEqual(blocked["error"]["code"], "unsafe_query")
        blob_payload = self.run_cli(
            "sql",
            "SELECT X'FF' AS binary_value",
            "--subdir",
            "session",
        )
        self.assertEqual(blob_payload["data"]["rows"][0]["binary_value"]["encoding"], "base64")

    def test_media_and_special_message_commands(self):
        media_payload = self.run_cli("media", "team@chatroom", "--type", "image")
        resources = media_payload["data"]["resources"]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["kind_name"], "image")
        self.assertEqual(resources[0]["metadata"]["md5"], "abc123")
        self.assertEqual(media_payload["data"]["coverage"], "hardlink_index_and_existing_local_paths")
        self.assertEqual(
            media_payload["data"]["resources"][0]["local_paths"],
            [str((self.resource_root / "attach" / "d1" / "d2" / "Img" / "photo.dat").resolve())],
        )
        alias_media = self.run_cli(
            "media-resources",
            "--talker",
            "team@chatroom",
            "--message-server-id-str",
            "110",
            "--kind-name",
            "image",
        )
        self.assertEqual([row["local_id"] for row in alias_media["data"]["resources"]], [10])

        transfers = self.run_cli("transfers", "--limit", "10")["data"]["messages"]
        red_packets = self.run_cli("red-packets", "--chat", "team@chatroom")["data"]["messages"]
        forwards = self.run_cli("forward-history", "--limit", "10")["data"]["messages"]
        self.assertEqual([row["local_id"] for row in transfers], [11])
        self.assertEqual([row["local_id"] for row in red_packets], [12])
        self.assertEqual([row["local_id"] for row in forwards], [13])

    def test_favorites_and_sns_commands(self):
        favorite_rows = self.run_cli("favorites", "--after", "200")["data"]["favorites"]
        self.assertEqual([row["local_id"] for row in favorite_rows], [1])

        feed_rows = self.run_cli("sns-feed", "--keyword", "产品")["data"]["items"]
        self.assertEqual([row["tid"] for row in feed_rows], [1001])
        self.assertEqual(feed_rows[0]["text"], "AI 产品发布")
        search_rows = self.run_cli("sns-search", "发布")["data"]["items"]
        self.assertEqual([row["tid"] for row in search_rows], [1001])

        notifications = self.run_cli("sns-notifications")["data"]["notifications"]
        self.assertEqual([row["local_id"] for row in notifications], [1])

    def test_optional_databases_are_visible_to_schema_and_status(self):
        schema_payload = self.run_cli("schema", "--subdir", "sns")
        self.assertEqual(schema_payload["data"]["databases"][0]["subdir"], "sns")
        capabilities = self.run_cli("status")["data"]["status"]["capabilities"]
        self.assertTrue(capabilities["favorites"])
        self.assertTrue(capabilities["sns_notifications"])

    def test_favorites_accepts_public_minimal_schema_without_optional_columns(self):
        favorite = Path(self.temp.name) / "favorite-minimal.db"
        with sqlite3.connect(favorite) as conn:
            conn.execute(
                "CREATE TABLE fav_db_item(local_id INTEGER, type INTEGER, update_time INTEGER, content TEXT)"
            )
            conn.execute("INSERT INTO fav_db_item VALUES(2, 1, 270, '最小收藏字段')")
        config = Path(self.temp.name) / "favorite-minimal-config.json"
        config.write_text(
            json.dumps(
                {
                    "session_db": str(self.session),
                    "contact_db": str(self.contact),
                    "message_dbs": [str(self.message)],
                    "favorite_db": str(favorite),
                    "keys_file": str(self.keys),
                }
            ),
            encoding="utf-8",
        )
        rows = self.run_with_config(config, "favorites")["data"]["favorites"]
        self.assertEqual(rows[0]["content"], "最小收藏字段")
        self.assertIsNone(rows[0]["server_id"])

    @unittest.skipUnless(SQLCIPHER is not None, "SQLCipher driver not installed")
    def test_encrypted_wechat_fixtures_end_to_end(self):
        root = Path(self.temp.name) / "encrypted-e2e"
        root.mkdir()
        key = "22" * 32
        source_paths = {
            "session_db": self.session,
            "contact_db": self.contact,
            "favorite_db": self.favorite,
            "sns_db": self.sns,
            "hardlink_db": self.hardlink,
        }
        encrypted_paths = {}
        for name, source in source_paths.items():
            target = root / source.name
            self.encrypt_fixture(source, target, key)
            encrypted_paths[name] = target
        encrypted_message = root / self.message.name
        self.encrypt_fixture(self.message, encrypted_message, key)
        all_encrypted = [*encrypted_paths.values(), encrypted_message]

        config = root / "config.json"
        keys = root / "keys.json"
        rejected_keys = root / "rejected-keys.json"
        wrong_access = root / "wrong-access.json"
        wrong_access.write_text(
            json.dumps({encrypted_message.name: {"enc_key": "33" * 32}}),
            encoding="utf-8",
        )
        wrong_access.chmod(0o600)
        rejected = self.run_with_config(
            config,
            "import-access",
            "--source",
            str(wrong_access),
            "--database-root",
            str(root),
            "--keys-file",
            str(rejected_keys),
            expected=1,
        )
        self.assertEqual(rejected["error"]["code"], "access_bundle_not_compatible")
        self.assertFalse(rejected_keys.exists())

        schema2_access = root / "schema2-access.json"
        schema2_keys = root / "schema2-keys.json"
        schema2_access.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "wxid": "fixture-only",
                    "db_root": str(root),
                    "keys": {path.read_bytes()[:16].hex(): key for path in all_encrypted},
                }
            ),
            encoding="utf-8",
        )
        schema2_access.chmod(0o600)
        direct_discovery = self.run_with_config(
            config,
            "discover",
            "--root",
            str(root),
            "--keys-file",
            str(schema2_access),
        )["data"]
        self.assertEqual(direct_discovery["authorized_encrypted_count"], len(all_encrypted))
        schema2_imported = self.run_with_config(
            config,
            "import-access",
            "--source",
            str(schema2_access),
            "--keys-file",
            str(schema2_keys),
        )["data"]
        self.assertEqual(schema2_imported["format"], "schema2_salt_map")
        self.assertEqual(schema2_imported["imported_key_count"], len(all_encrypted))
        self.assertEqual(schema2_imported["verification"]["matched_database_count"], len(all_encrypted))
        self.assertNotIn(key, json.dumps(schema2_imported))

        legacy_access = root / "legacy-access.json"
        legacy_access.write_text(
            json.dumps(
                {
                    "_metadata": {"format": "legacy-compatible"},
                    **{
                        path.name: {"enc_key": key, "cipher_compatibility": 4}
                        for path in all_encrypted
                    },
                }
            ),
            encoding="utf-8",
        )
        legacy_access.chmod(0o600)
        imported = self.run_with_config(
            config,
            "import-access",
            "--source",
            str(legacy_access),
            "--database-root",
            str(root),
            "--keys-file",
            str(keys),
        )["data"]
        self.assertEqual(imported["format"], "path_key_map")
        self.assertEqual(imported["imported_key_count"], len(source_paths) + 1)
        self.assertEqual(imported["verification"]["matched_database_count"], len(source_paths) + 1)
        config.write_text(
            json.dumps(
                {
                    **{name: str(path) for name, path in encrypted_paths.items()},
                    "message_dbs": [str(encrypted_message)],
                    "resource_roots": [str(self.resource_root)],
                    "keys_file": str(schema2_keys),
                    "self_username": "wxid_me",
                }
            ),
            encoding="utf-8",
        )

        discovered = self.run_with_config(
            config,
            "discover",
            "--root",
            str(root),
            "--keys-file",
            str(schema2_keys),
        )["data"]
        self.assertEqual(discovered["authorized_encrypted_count"], len(source_paths) + 1)
        self.assertEqual(discovered["unresolved_database_count"], 0)

        auto_config = root / "auto-config.json"
        auto_setup = self.run_with_config(
            auto_config,
            "setup",
            "--keys-file",
            str(schema2_keys),
            "--resource-root",
            str(self.resource_root),
        )
        self.assertEqual(auto_setup["data"]["doctor"]["summary"], "ready")

        status = self.run_with_config(config, "status")["data"]["status"]
        self.assertTrue(status["live_database_read_ok"])
        self.assertTrue(status["sqlcipher_driver_ready"])
        timeline = self.run_with_config(config, "timeline", "wxid_alice")["data"]["messages"]
        self.assertEqual([row["local_id"] for row in timeline], [1, 2])
        self.assertEqual(len(self.run_with_config(config, "favorites")["data"]["favorites"]), 1)
        self.assertEqual(len(self.run_with_config(config, "sns-feed")["data"]["items"]), 1)
        media = self.run_with_config(config, "media", "team@chatroom", "--type", "image")["data"]
        self.assertEqual(len(media["resources"][0]["local_paths"]), 1)


if __name__ == "__main__":
    unittest.main()
