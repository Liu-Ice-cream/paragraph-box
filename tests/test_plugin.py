"""Protocol integration tests using synthetic files and no native UI actions."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from main import dispatch, main  # noqa: E402
from paragraph_search.plugin import ParagraphPlugin, excerpt, location_data, find_vscode  # noqa: E402
from paragraph_search.settings import load_settings  # noqa: E402


class PluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.notes = self.base / "写作素材"
        self.notes.mkdir()
        self.note = self.notes / "雨夜.md"
        self.note.write_text("# 车站\n\n雨夜里，她等待列车。\n旧钟响了。\n\n清晨已经到来。\n", encoding="utf-8")
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"roots": [{"alias": "写作", "path": str(self.notes)}]}, ensure_ascii=False), encoding="utf-8")
        self.env = patch.dict(os.environ, {"PARAGRAPH_SEARCH_CONFIG": str(self.config), "PARAGRAPH_SEARCH_HOME": str(self.base / "cache")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)
        self.plugin = ParagraphPlugin(BASE)

    def rpc(self, method: str, parameters: list) -> dict:
        request = json.dumps({"method": method, "parameters": parameters}, ensure_ascii=True)
        run = subprocess.run([sys.executable, str(BASE / "main.py"), request], capture_output=True, check=True)
        self.assertEqual(run.stderr, b"")
        return json.loads(run.stdout)

    def test_fresh_process_query_and_context_menu_protocol(self) -> None:
        first = self.rpc("query", ["雨夜"])
        second = self.rpc("query", ["@写作 雨夜 列车"])
        for field in ("alias", "path", "heading", "text", "line_start", "line_end"):
            self.assertEqual(first["result"][0]["ContextData"][field], second["result"][0]["ContextData"][field])
        self.assertEqual(len(first["result"]), 1)
        hit = first["result"][0]
        self.assertEqual(hit["Title"], "雨夜里，她等待列车。 旧钟响了。")
        self.assertIn("雨夜.md", hit["SubTitle"])
        self.assertIn("车站", hit["SubTitle"])
        self.assertEqual(hit["ContextData"]["line_start"], 3)
        self.assertEqual(hit["ContextData"]["line_end"], 4)
        self.assertEqual(hit["JsonRPCAction"], {"method": "open_file", "parameters": [str(self.note)]})
        menu = self.rpc("context_menu", [hit["ContextData"]])["result"]
        copied = menu[2]["JsonRPCAction"]
        self.assertEqual(copied["method"], "Flow.Launcher.CopyToClipboard")
        self.assertEqual(copied["parameters"][1:], [False, True])
        self.assertIn("雨夜里，她等待列车。\n旧钟响了。", copied["parameters"][0])
        self.assertIn(str(self.note), copied["parameters"][0])
        self.assertIn("第 3–4 行", copied["parameters"][0])

    def test_alias_whitespace_and_unknown_alias(self) -> None:
        self.assertEqual(len(self.plugin.query("@写作\t雨夜")), 1)
        self.assertIn("未知目录别名", self.plugin.query("@陌生 雨夜")[0]["Title"])
        self.assertIn("请输入关键词", self.plugin.query("@写作")[0]["Title"])

    def test_alias_selector_uses_the_same_unicode_normalization_as_index(self) -> None:
        self.config.write_text(json.dumps({"roots": [{"alias": "StraßeＡＩ", "path": str(self.notes)}]}), encoding="utf-8")
        hits = self.rpc("query", ["@STRASSEai 雨夜"])["result"]
        self.assertEqual(hits[0]["ContextData"]["alias"], "StraßeＡＩ")
        self.assertEqual(hits[0]["ContextData"]["match_kind"], "body")
        self.assertEqual(self.rpc("query", ["＠ＳＴＲＡＳＳＥａｉ 雨夜"])["result"][0]["ContextData"]["alias"], "StraßeＡＩ")

    def test_empty_query_gives_config_entry(self) -> None:
        results = self.rpc("query", [""])["result"]
        self.assertTrue(any(result["Title"] == "配置检索目录" for result in results))

    def test_no_match_stays_valid_protocol(self) -> None:
        results = self.rpc("query", ["不存在的关键词"])["result"]
        self.assertEqual(results[0]["Title"], "没有找到匹配段落")

    def test_bad_config_has_readable_error_and_no_stderr(self) -> None:
        self.config.write_text("{ invalid json", encoding="utf-8")
        results = self.rpc("query", ["雨夜"])["result"]
        self.assertEqual(results[0]["Title"], "段落搜索暂时无法运行")
        self.assertTrue((self.base / "cache" / "plugin.log").is_file())

    def test_configuration_validates_aliases_types_and_relative_paths(self) -> None:
        self.config.write_text(json.dumps({"roots": [{"alias": "写作", "path": "写作素材"}]}), encoding="utf-8")
        self.assertEqual(load_settings(BASE).roots[0].path, self.notes)
        cases = [
            {"roots": [{"alias": "A", "path": "."}, {"alias": "a", "path": "."}]},
            {"roots": [{"alias": "bad alias", "path": "."}]},
            {"roots": [{"alias": "x", "path": "."}], "max_results": True},
            {"roots": []},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.config.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_settings(BASE)

    def test_open_file_dispatch_uses_windows_default_app_and_empty_output(self) -> None:
        with patch("paragraph_search.plugin.os.startfile") as open_file:
            self.assertIsNone(dispatch({"method": "open_file", "parameters": [str(self.note)]}, self.plugin))
            open_file.assert_called_once_with(str(self.note))

    def test_editor_defaults_and_vscode_location_keeps_unicode_spaces_as_one_argument(self):
        self.assertEqual(load_settings(BASE).editor, "default")
        document = json.loads(self.config.read_text(encoding='utf-8'))
        document['editor'] = 'vscode'
        self.config.write_text(json.dumps(document), encoding='utf-8')
        note = self.notes / '中文 空格 & 资料.md'
        note.write_text('# 精确定位\n\n唯一定位内容。', encoding='utf-8')
        hit = self.plugin.query('唯一定位')[0]
        self.assertEqual(hit['JsonRPCAction']['method'], 'open_location')
        self.assertEqual(hit['JsonRPCAction']['parameters'], [location_data(hit['ContextData'])])
        executable = Path(r'C:\Program Files\Microsoft VS Code\Code.exe')
        with patch('paragraph_search.plugin.find_vscode', return_value=executable), patch('paragraph_search.plugin.subprocess.Popen') as launch:
            self.assertIsNone(dispatch(hit['JsonRPCAction'], self.plugin))
        self.assertEqual(launch.call_args.args[0], [str(executable), '--reuse-window', '--goto', f'{note}:3:1'])
        self.assertNotIn('shell', launch.call_args.kwargs)
        self.assertEqual(launch.call_args.kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(launch.call_args.kwargs['stderr'], subprocess.DEVNULL)
        menu = self.plugin.context_menu(hit['ContextData'])
        self.assertEqual(menu[2]['Title'], '复制带出处的摘录')
        self.assertEqual(menu[-1]['JsonRPCAction']['method'], 'open_location')

    def test_location_rejects_changed_source_and_missing_editor_without_launching(self):
        document = json.loads(self.config.read_text(encoding='utf-8'))
        document['editor'] = 'vscode'
        self.config.write_text(json.dumps(document), encoding='utf-8')
        payload = self.plugin.query('雨夜')[0]['JsonRPCAction']['parameters'][0]
        with patch('paragraph_search.plugin.find_vscode', return_value=None), patch('paragraph_search.plugin.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, '未找到 VS Code'):
                self.plugin.open_location(payload)
            launch.assert_not_called()
        self.note.write_text('# 已变化\n\n雨夜另一个段落。', encoding='utf-8')
        with patch('paragraph_search.plugin.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, '重新检索'):
                self.plugin.open_location(payload)
            launch.assert_not_called()

    def test_default_location_action_checks_source_and_opens_default_app(self):
        data = self.plugin.query('雨夜')[0]['ContextData']
        with patch('paragraph_search.plugin.os.startfile') as opened:
            self.assertEqual(self.plugin.open_location(location_data(data)), {'editor':'default','located':False})
        opened.assert_called_once_with(str(self.note))

    def test_vscode_detection_resolves_cli_shim_to_executable_without_running_it(self):
        installed = self.base / 'VS Code'
        (installed / 'bin').mkdir(parents=True)
        executable = installed / 'Code.exe'
        executable.write_bytes(b'test fixture')
        with patch.dict(os.environ, {'LOCALAPPDATA':'', 'ProgramFiles':'', 'ProgramFiles(x86)':''}), patch('paragraph_search.plugin.shutil.which', side_effect=[None, str(installed/'bin/code.cmd')]):
            self.assertEqual(find_vscode(), executable)

    def test_settings_action_does_not_keep_flow_protocol_pipes_open(self) -> None:
        with patch("paragraph_search.plugin.subprocess.Popen") as launch:
            self.assertIsNone(dispatch({"method": "open_config", "parameters": []}, self.plugin))
        self.assertEqual(launch.call_args.args[0], [sys.executable, str(BASE / "settings_ui.py")])
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(launch.call_args.kwargs["close_fds"])

    def test_open_file_rejects_paths_outside_roots(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("unrelated", encoding="utf-8")
        with patch("paragraph_search.plugin.os.startfile") as open_file:
            with self.assertRaises(ValueError):
                self.plugin.open_file(str(outside))
            open_file.assert_not_called()

    def test_unassociated_markdown_opens_in_notepad(self) -> None:
        missing_association = OSError("No application is associated with this file")
        missing_association.winerror = 1155
        with patch("paragraph_search.plugin.os.startfile", side_effect=missing_association), patch("paragraph_search.plugin.subprocess.Popen") as notepad:
            self.plugin.open_file(str(self.note))
        self.assertEqual(notepad.call_args.args[0], ["notepad.exe", str(self.note)])
        self.assertEqual(notepad.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(notepad.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_action_failure_emits_flow_notification_instead_of_stderr(self) -> None:
        request = json.dumps({"method": "open_file", "parameters": [str(self.note)]})
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["main.py", request]), patch("paragraph_search.plugin.os.startfile", side_effect=OSError("no associated app")), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(), 0)
        self.assertEqual(stderr.getvalue(), "")
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["method"], "Flow.Launcher.ShowMsgError")
        self.assertEqual(len(result["parameters"]), 2)
        self.assertIn("no associated app", result["parameters"][1])

    def test_missing_application_hresult_uses_notepad_but_access_denied_does_not(self) -> None:
        for code in (-2147221003, 0x800401F5):
            missing = OSError("application not found")
            missing.winerror = code
            with patch("paragraph_search.plugin.os.startfile", side_effect=missing), patch("paragraph_search.plugin.subprocess.Popen") as fallback:
                self.plugin.open_file(str(self.note))
            self.assertEqual(fallback.call_args.args[0], ["notepad.exe", str(self.note)])
        denied = PermissionError("access denied")
        denied.winerror = 5
        with patch("paragraph_search.plugin.os.startfile", side_effect=denied), patch("paragraph_search.plugin.subprocess.Popen") as fallback:
            with self.assertRaises(PermissionError):
                self.plugin.open_file(str(self.note))
            fallback.assert_not_called()

    def test_missing_or_nonparagraph_context_menu_is_empty(self) -> None:
        self.assertEqual(self.rpc("context_menu", [None]), {"result": []})
        self.assertEqual(self.rpc("context_menu", [{"anything": 1}]), {"result": []})

    def test_existing_excerpt_remains_copyable_when_config_becomes_invalid(self):
        data = self.plugin.query('雨夜')[0]['ContextData']
        self.config.write_text('{ invalid json', encoding='utf-8')
        menu = self.plugin.context_menu(data)
        self.assertEqual(len(menu), 4)
        self.assertEqual(menu[2]['JsonRPCAction']['method'], 'Flow.Launcher.CopyToClipboard')
        self.assertIn(data['text'], menu[2]['JsonRPCAction']['parameters'][0])

    def test_snippet_shows_deep_match_and_normalized_text(self) -> None:
        text = "前面的文字" * 40 + "ＡＩ关键词" + "后面的文字" * 40
        clipped = excerpt(text, "ai关键词")
        self.assertIn("ＡＩ关键词", clipped)
        self.assertTrue(clipped.startswith("…"))
        self.assertLessEqual(len(clipped), 112)

    def test_dispatch_rejects_arbitrary_methods(self) -> None:
        for name in ("__init__", "__class__", "query_extra"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                dispatch({"method": name, "parameters": []}, self.plugin)


if __name__ == "__main__":
    unittest.main()
