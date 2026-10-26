import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rion_wechat_reader as reader
import rion_wechat_access as access


class MaterialCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.json"
        self.target = self.root / "result.json"
        self.key = "ab" * 32
        self.salt = "cd" * 16

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value, encoding="utf-8"):
        self.source.write_text(json.dumps(value), encoding=encoding)
        self.source.chmod(0o600)

    def run_import(self):
        return reader.import_access_bundle(self.source, self.root, self.target,
                                           force=False, verify=False, max_files=50)

    def test_windows_json_encodings_preserve_mapping(self):
        for encoding in ("utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                self.write({"keys": {"消息.db": self.key}}, encoding)
                self.assertIn("keys", reader.load_json(self.source))
                result = self.run_import()
                self.assertEqual(result["imported_key_count"], 1)
                self.assertNotIn(self.key, json.dumps(result))
                self.target.unlink()

    def test_top_level_metadata_not_misread_as_key(self):
        self.write({"database_root": str(self.root), "schema_version": 1,
                    "message.db": {"raw_key": "x'" + self.key + "'"}})
        result = self.run_import()
        self.assertEqual(result["metadata_entry_count"], 2)
        self.assertEqual(result["imported_key_count"], 1)

    def test_schema2_accepts_raw_key_with_matching_salt(self):
        self.write({"schema_version": 2, "keys": {self.salt: {"enc_key": "X'" + self.key + self.salt + "'"}}})
        self.run_import()
        data = reader.load_json(self.target)
        self.assertEqual(data["salt_keys"][self.salt]["key"], self.key + self.salt)

    def test_equivalent_aliases_are_not_conflicts(self):
        self.write({"message.db": {"key": self.key.upper(), "raw_key": "x'" + self.key + "'"}})
        self.run_import()
        self.assertEqual(reader.load_json(self.target)["keys"][str(self.root / "message.db")]["key"], self.key)

    def test_mixed_maps_preserve_both_sources(self):
        self.write({"keys": {"message.db": self.key}, "salt_keys": {self.salt: self.key}})
        result = self.run_import()
        self.assertEqual(result["format"], "mixed_key_map")
        self.assertEqual(result["imported_key_count"], 2)
        self.assertIn(str(self.root / "message.db"), reader.load_json(self.target)["keys"])
        self.assertIn(self.salt, reader.load_json(self.target)["salt_keys"])

    def test_canonical_duplicate_conflicts_and_bad_wrappers_rejected(self):
        for value in (
            {"salt_keys": {self.salt: self.key, self.salt.upper(): "ef" * 32}},
            {"keys": {"message.db": self.key, "./message.db": "ef" * 32}},
            {"keys": [], "salt_keys": {self.salt: self.key}},
        ):
            with self.subTest(value_type=list(value)):
                self.write(value)
                with self.assertRaises(reader.ReaderError):
                    self.run_import()
                self.assertFalse(self.target.exists())

    def test_support_routes_material_failure_separately_from_working_reader(self):
        preview = {"state": "access_material_requires_review",
                   "material_error": {"code": "sqlcipher_driver_required"}}
        report = {"state": "ready", "live_database_read_ok": True, "material_preview": preview}
        summary = access.support_summary(report)
        self.assertEqual(summary["next_step_code"], "use_existing_reader")
        self.assertEqual(summary["material_preview"]["next_step_code"], "repair_runtime_dependency")
        self.assertEqual(access.support_next_step({"state": "database_layout_ambiguous"}), "select_account_or_database")

    @unittest.skipUnless(reader.sqlcipher_driver()[0], "SQLCipher required")
    def test_mixed_maps_open_two_encrypted_databases(self):
        driver = reader.sqlcipher_driver()[0]
        databases = [self.root / name for name in ("first.db", "second.db")]
        for database in databases:
            conn = driver.connect(str(database))
            conn.execute("PRAGMA key = \"x'" + self.key + "'\"")
            conn.execute("CREATE TABLE fixture(value TEXT)")
            conn.commit()
            conn.close()
        salt = reader.database_salt(databases[1])
        self.write({"keys": {"first.db": self.key}, "salt_keys": {salt: self.key}})
        result = reader.import_access_bundle(self.source, self.root, self.target,
                                            force=False, verify=True, max_files=50)
        self.assertEqual(result["verification"]["matched_database_count"], 2)

    def test_mismatch_conflict_and_passphrase_never_write(self):
        cases = [
            ({"schema_version": 2, "keys": {self.salt: self.key + "ef" * 16}}, "key_salt_mismatch"),
            ({"message.db": {"key": self.key, "raw_key": "ef" * 32}}, "conflicting_key_fields"),
            ({"message.db": {"passphrase": self.key}}, "passphrase_requires_derivation"),
            ({"../other-account.db": self.key}, "unsafe_access_bundle_path"),
            ({"message.db": "x'" + self.key + "'; DROP TABLE x;"}, "invalid_key"),
        ]
        for value, code in cases:
            with self.subTest(code=code):
                self.write(value)
                with self.assertRaises(reader.ReaderError) as raised:
                    self.run_import()
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(self.target.exists())
                self.assertNotIn(self.key, str(raised.exception))

    def test_invalid_encoding_error_contains_no_material(self):
        self.source.write_bytes(b"\xffPRIVATE-KEY-DATA")
        self.source.chmod(0o600)
        with self.assertRaises(reader.ReaderError) as raised:
            reader.load_json(self.source)
        self.assertEqual(raised.exception.code, "invalid_json")
        self.assertNotIn("PRIVATE", str(raised.exception))
        with self.assertRaises(reader.ReaderError) as imported:
            self.run_import()
        self.assertEqual(imported.exception.code, "invalid_json")
        self.assertNotIn("PRIVATE", str(imported.exception))
        self.assertFalse(self.target.exists())

    def test_material_diagnostic_does_not_echo_exception(self):
        result = access.material_diagnostic(reader.ReaderError("PRIVATE KEY " + self.key, "key_salt_mismatch"))
        self.assertEqual(result["code"], "key_salt_mismatch")
        self.assertNotIn(self.key, json.dumps(result))

    def test_jev_packet_is_allowlisted_and_offline(self):
        status = {"state": "ready", "live_database_read_ok": True,
                  "environment": {"system": "Windows", "path": "PRIVATE", "key": self.key},
                  "last_attempt": {"diagnostics": {"signals": ["account_salt_mismatch", "PRIVATE"], "raw": self.key}},
                  "next_actions": ["PRIVATE"], "counts": {"private": self.key}}
        args = SimpleNamespace(config=self.root / "config", database_root=None,
                               max_files=50, source=None, jev_request=True)
        with mock.patch.object(access, "access_status", return_value=status), \
             mock.patch.object(access, "run", side_effect=AssertionError("no acquisition")):
            result = access.diagnose(args)
        packet = json.dumps(result["jev_request"])
        self.assertNotIn("PRIVATE", packet)
        self.assertNotIn(self.key, packet)
        self.assertFalse(result["network_called"])
        self.assertEqual(result["jev_request"]["state"]["access_state"], "ready")

    def test_supplied_material_uses_disposable_config(self):
        self.write({"keys": {}})
        active = self.root / "config.json"
        active.write_text("unchanged")
        args = SimpleNamespace(config=active, database_root=self.root, source=self.source,
                               max_files=50, jev_request=False)
        def preview(candidate):
            self.assertNotEqual(candidate.config, active)
            self.assertFalse(candidate.apply)
            self.assertIsNone(candidate.provider)
            return {"state": "ready_to_configure"}
        with mock.patch.object(access, "access_status", return_value={"state": "ready"}), \
             mock.patch.object(access, "onboard", side_effect=preview):
            result = access.diagnose(args)
        self.assertEqual(active.read_text(), "unchanged")
        self.assertFalse(result["configuration_changed"])

    def test_support_summary_rejects_untrusted_fields(self):
        report = {"state": "ready", "live_database_read_ok": True,
                  "environment": {"system": "Windows", "architecture": "AMD64",
                                  "wechat_version": "4.1.13.12", "os_version": "PRIVATE-PATH"},
                  "counts": {"messages": 3, "contact": "PRIVATE", "keys": self.key},
                  "material_preview": {"state": "access_material_requires_review",
                                       "material_error": {"code": "invalid_json", "message": "PRIVATE"}},
                  "raw_log": self.key, "next_actions": ["PRIVATE"],
                  "last_attempt": {"diagnostics": {"raw": "PRIVATE"}}}
        result = access.support_summary(report)
        self.assertEqual(result["counts"], {"messages": 3})
        self.assertEqual(result["environment"]["architecture"], "AMD64")
        self.assertEqual(result["material_preview"]["error_code"], "invalid_json")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn(self.key, json.dumps(result))

    def test_diagnose_summary_omits_full_report(self):
        args = SimpleNamespace(config=self.root / "config", database_root=None,
                               max_files=50, source=None, support_summary=True, jev_request=False)
        with mock.patch.object(access, "access_status", return_value={"state": "ready", "live_database_read_ok": True, "private": self.key}):
            result = access.diagnose(args)
        self.assertNotIn("private", result)
        self.assertEqual(result["state"], "ready")

    @unittest.skipUnless(reader.sqlcipher_driver()[0], "SQLCipher required")
    def test_encoded_literal_material_opens_encrypted_fixture(self):
        driver = reader.sqlcipher_driver()[0]
        database = self.root / "message.db"
        conn = driver.connect(str(database))
        conn.execute("PRAGMA key = \"x'" + self.key + "'\"")
        conn.execute("CREATE TABLE fixture(value TEXT)")
        conn.execute("INSERT INTO fixture VALUES ('synthetic')")
        conn.commit()
        conn.close()
        self.write({"message.db": {"raw_key": "x'" + self.key + "'"}}, "utf-16")
        result = reader.import_access_bundle(self.source, self.root, self.target,
                                             force=False, verify=True, max_files=50)
        self.assertEqual(result["verification"]["matched_database_count"], 1)
        self.assertNotIn(self.key, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
