"""Behavior and privacy checks using synthetic private-data canaries."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from main import dispatch
from paragraph_search.diagnostics import DEMO_NAME, DEMO_TEXT, diagnostic_summary, run_checks, sample_result
from paragraph_search.plugin import ParagraphPlugin


BASE = Path(__file__).resolve().parents[1]


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.notes = self.base / "PRIVATE-PATH-CANARY"
        self.notes.mkdir()
        (self.notes / "private.md").write_text("PRIVATE-TEXT-CANARY", encoding="utf-8")
        self.document = {"roots": [{"alias": "PRIVATE-ALIAS-CANARY", "path": str(self.notes)}]}
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps(self.document), encoding="utf-8")
        (self.base / "plugin.json").write_text('{"Version":"0.2.6"}', encoding="utf-8")
        sample = self.base / "samples/diagnostics" / DEMO_NAME
        sample.parent.mkdir(parents=True)
        sample.write_text(DEMO_TEXT, encoding="utf-8")
        self.cache = self.base / "cache"
        self.cache.mkdir()
        (self.cache / "index.sqlite3").write_bytes(b"PRIVATE-INDEX-CANARY")
        env = patch.dict(os.environ, {"PARAGRAPH_SEARCH_CONFIG": str(self.config), "PARAGRAPH_SEARCH_HOME": str(self.cache)})
        env.start()
        self.addCleanup(env.stop)

    def check(self, report, identifier):
        return next(item for item in report["checks"] if item["id"] == identifier)

    def assert_sanitized(self, document):
        output = document if isinstance(document, str) else json.dumps(document)
        for canary in ("PRIVATE-PATH", "PRIVATE-ALIAS", "PRIVATE-TEXT", "PRIVATE-INDEX", "PRIVATE-ERROR", str(self.base)):
            self.assertNotIn(canary, output)

    def test_checks_and_demo_preserve_config_index_and_environment(self):
        before_config = self.config.read_bytes()
        before_index = (self.cache / "index.sqlite3").read_bytes()
        before_env = dict(os.environ)
        # Scanning actual notes would expose the canary. Only the fixed temp
        # fixtures may enter ParagraphIndex, even though a personal config exists.
        from paragraph_search.engine import ParagraphIndex
        original = ParagraphIndex.sync
        def sync(index, *args, **kwargs):
            self.assertTrue(all(root.path != self.notes for root in index.roots))
            return original(index, *args, **kwargs)
        with patch.object(ParagraphIndex, "sync", sync):
            report = run_checks(self.base)
            data = sample_result(self.base)
        self.assertEqual(report["automatic_status"], "pass")
        self.assertEqual(data["source"], "人工样例.txt · 第 3 行")
        self.assert_sanitized(report)
        self.assert_sanitized(diagnostic_summary(report))
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((self.cache / "index.sqlite3").read_bytes(), before_index)
        self.assertEqual(dict(os.environ), before_env)
        self.assertEqual(sorted(path.name for path in self.cache.iterdir()), ["index.sqlite3"])
        self.assertTrue(all(value == "pending" for value in json.loads(diagnostic_summary(report))["manual"].values()))

    def test_invalid_config_does_not_block_cache_or_sample_checks(self):
        self.config.write_text("{ PRIVATE-ERROR-CANARY", encoding="utf-8")
        report = run_checks(self.base)
        self.assertEqual(report["automatic_status"], "fail")
        self.assertEqual(self.check(report, "config")["code"], "CONFIG_INVALID")
        self.assertEqual(self.check(report, "roots")["status"], "pending")
        self.assertEqual(self.check(report, "sample")["status"], "pass")
        self.assertEqual(self.check(report, "cache")["status"], "pass")
        self.assert_sanitized(report)

    def test_unavailable_root_is_numbered_without_alias_or_path(self):
        self.document["roots"][0]["path"] = str(self.notes / "missing")
        self.config.write_text(json.dumps(self.document), encoding="utf-8")
        report = run_checks(self.base)
        self.assertEqual(self.check(report, "root_1")["code"], "ROOT_UNAVAILABLE")
        self.assert_sanitized(report)

    def test_unwritable_cache_and_raw_os_error_are_sanitized(self):
        with patch("paragraph_search.diagnostics.sqlite3.connect", side_effect=PermissionError("PRIVATE-ERROR-CANARY")):
            report = run_checks(self.base)
        self.assertEqual(self.check(report, "cache")["error_type"], "PermissionError")
        self.assertEqual(self.check(report, "cache")["code"], "CACHE_UNWRITABLE")
        self.assert_sanitized(report)
        self.assert_sanitized(diagnostic_summary(report))

    def test_selected_unavailable_editor_requires_fix(self):
        self.document["editor"] = "vscode"
        self.config.write_text(json.dumps(self.document), encoding="utf-8")
        with patch("paragraph_search.diagnostics.find_vscode", return_value=None):
            report = run_checks(self.base)
        self.assertEqual(self.check(report, "editor")["code"], "EDITOR_UNAVAILABLE")
        self.assertEqual(report["automatic_status"], "fail")

    def test_stale_or_untrusted_manifest_version_is_not_claimed_valid(self):
        report = run_checks(self.base, active_version="0.2.5")
        self.assertEqual(self.check(report, "manifest")["status"], "fail")
        (self.base / "plugin.json").write_text('{"Version":"PRIVATE-ERROR-CANARY"}', encoding="utf-8")
        report = run_checks(self.base)
        self.assertEqual(report["version"], "unknown")
        self.assert_sanitized(report)

    def test_changed_demo_is_rejected_without_exposing_content(self):
        (self.base / "samples/diagnostics" / DEMO_NAME).write_text("PRIVATE-TEXT-CANARY", encoding="utf-8")
        report = run_checks(self.base)
        self.assertEqual(self.check(report, "sample")["code"], "SAMPLE_FAILED")
        self.assert_sanitized(report)

    def test_cli_doctor_handles_invalid_config_and_keeps_check_contract(self):
        self.config.write_text("{ broken", encoding="utf-8")
        result = subprocess.run([sys.executable, str(BASE / "main.py"), "--doctor"], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout)["automatic_status"], "fail")
        self.assert_sanitized(result.stdout.decode())
        self.config.write_text(json.dumps(self.document), encoding="utf-8")
        result = subprocess.run([sys.executable, str(BASE / "main.py"), "--check"], capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["roots"][0]["path"], str(self.notes))

    def test_flow_entry_still_works_when_config_is_broken(self):
        self.config.write_text("{ broken", encoding="utf-8")
        plugin = ParagraphPlugin(BASE)
        entries = plugin.query("")
        self.assertTrue(any(item["JsonRPCAction"]["method"] == "open_diagnostics" for item in entries))
        with patch("paragraph_search.plugin.subprocess.Popen") as launch:
            self.assertIsNone(dispatch({"method": "open_diagnostics", "parameters": []}, plugin))
        self.assertEqual(launch.call_args.args[0][-1], "--diagnostics")
        with self.assertRaises(ValueError):
            dispatch({"method": "open_diagnostics", "parameters": ["arbitrary"]}, plugin)
