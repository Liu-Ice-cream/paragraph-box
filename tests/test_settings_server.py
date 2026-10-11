import json
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from paragraph_search.settings_server import SettingsServer


class SettingsServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / 'notes').mkdir()
        (self.base / 'notes/记录.md').write_text('# 项目\n\n雨夜写下的段落。\n', encoding='utf-8')
        self.config = {'roots': [{'alias': '写作', 'path': 'notes'}], 'max_results': 30, 'max_file_bytes': 2000000}
        (self.base / 'config.example.json').write_text(json.dumps(self.config), encoding='utf-8')
        (self.base / 'plugin.json').write_text(json.dumps({'Name': 'ParagraphBox', 'Version': '0.2.0'}), encoding='utf-8')
        (self.base / 'ui').mkdir()
        (self.base / 'ui/index.html').write_text('<!doctype html><title>ParagraphBox</title>', encoding='utf-8')
        self.env = patch.dict(os.environ, {'PARAGRAPH_SEARCH_HOME': str(self.base / '.cache')})
        self.env.start()
        os.environ.pop('PARAGRAPH_SEARCH_CONFIG', None)
        self.server = SettingsServer(self.base)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.env.stop()
        self.temp.cleanup()

    def request(self, route, body=None, headers=None, auth=True):
        hdrs = {'Authorization': 'Bearer ' + self.server.token} if auth else {}
        if body is not None:
            hdrs['Content-Type'] = 'application/json'
        hdrs.update(headers or {})
        req = urllib.request.Request(self.server.origin + route, data=None if body is None else json.dumps(body).encode(), headers=hdrs)
        try:
            response = self.opener.open(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            return response.status, json.loads(data) if 'application/json' in response.headers['Content-Type'] else data, response.headers

    def test_save_search_and_index_stats(self):
        self.assertTrue(self.request('/api/config')[1]['sample_mode'])
        self.config['max_results'] = 25
        status, data, _ = self.request('/api/config', self.config)
        self.assertEqual(status, 200)
        self.assertFalse(data['sample_mode'])
        self.assertEqual(json.loads((self.base / 'config.json').read_text(encoding='utf-8')), self.config)
        status, data, _ = self.request('/api/search', {'query': '@写作 雨夜'})
        self.assertEqual(status, 200)
        self.assertIn('雨夜', data['results'][0]['Title'])
        self.assertEqual(data['results'][0]['ContextData']['line_start'], 3)
        self.assertEqual(self.request('/api/config')[1]['stats'], {'files': 1, 'paragraphs': 1})
        self.assertEqual(data['search_status']['search']['matched'], 1)
        self.assertGreaterEqual(data['search_status']['duration_ms'], 0)

    def test_diagnostics_reachable_with_broken_config_and_safe_summary(self):
        from paragraph_search.diagnostics import DEMO_NAME, DEMO_TEXT
        sample = self.base / 'samples/diagnostics' / DEMO_NAME
        sample.parent.mkdir(parents=True)
        sample.write_text(DEMO_TEXT, encoding='utf-8')
        (self.base / 'config.json').write_text('{ PRIVATE-CONFIG-CANARY', encoding='utf-8')
        self.assertEqual(self.request('/api/config')[0], 400)
        status, data, _ = self.request('/api/diagnostics', {})
        self.assertEqual(status, 200)
        self.assertEqual(data['report']['automatic_status'], 'fail')
        summary = data['summary']
        self.assertNotIn(str(self.base), summary)
        self.assertNotIn(self.server.token, summary)
        self.assertNotIn('PRIVATE-CONFIG-CANARY', json.dumps(data))
        self.assertTrue(all(value == 'pending' for value in json.loads(summary)['manual'].values()))
        self.assertEqual(self.request('/api/demo', {})[1]['source'], '人工样例.txt · 第 3 行')
        self.assertFalse((self.base / '.cache/index.sqlite3').exists())

    def test_diagnostic_actions_require_auth_and_refuse_arbitrary_paths(self):
        from paragraph_search.diagnostics import DEMO_NAME, DEMO_TEXT
        sample = self.base / 'samples/diagnostics' / DEMO_NAME
        sample.parent.mkdir(parents=True)
        sample.write_text(DEMO_TEXT, encoding='utf-8')
        for route in ('/api/diagnostics', '/api/demo', '/api/demo-open'):
            self.assertEqual(self.request(route, {}, auth=False)[0], 401)
            self.assertEqual(self.request(route, {}, headers={'Origin':'https://example.com'})[0], 403)
            self.assertEqual(self.request(route, {'path':str(self.base / 'notes/记录.md')})[0], 400)
        with patch('paragraph_search.settings_server.open_text_file') as opened:
            status, data, _ = self.request('/api/demo-open', {})
        self.assertEqual(status, 200)
        self.assertEqual(data, {'requested':True})
        opened.assert_called_once_with(sample.resolve())
        with patch('paragraph_search.settings_server.open_text_file', side_effect=PermissionError('PRIVATE-PATH-CANARY')):
            status, data, _ = self.request('/api/demo-open', {})
        self.assertEqual(status, 400)
        self.assertNotIn('PRIVATE-PATH-CANARY', json.dumps(data))

    def context_request(self, hit):
        data = hit['ContextData']
        return {name:data[name] for name in ('path','alias','line_start','line_end')} | {
            'text_hash':hashlib.sha256((data['heading'] + '\0' + data['text']).encode()).hexdigest()}

    def test_editor_roundtrip_validation_and_index_signature_compatibility(self):
        self.request('/api/rebuild-index', {})
        _, before, _ = self.request('/api/config')
        self.assertNotIn('editor', before['config'])
        with patch('paragraph_search.settings_server.find_vscode', return_value=Path('Code.exe')):
            status, saved, _ = self.request('/api/config', dict(self.config, editor='vscode'))
        self.assertEqual(status, 200)
        self.assertEqual(saved['config']['editor'], 'vscode')
        self.assertTrue(saved['editors']['vscode'])
        self.assertTrue(saved['index_status']['config_current'])
        config_bytes = (self.base/'config.json').read_bytes()
        for editor in ('arbitrary.exe', None, [], {'command':'code'}, True):
            status, data, _ = self.request('/api/config', dict(self.config, editor=editor))
            self.assertEqual(status, 400)
            self.assertEqual(data['issues'][0]['field'], 'editor')
            self.assertEqual((self.base/'config.json').read_bytes(), config_bytes)

    def test_open_location_requires_auth_source_identity_and_current_rules(self):
        hit = self.request('/api/search', {'query':'雨夜'})[1]['results'][0]
        payload = self.context_request(hit)
        with patch('paragraph_search.plugin.os.startfile') as opened:
            status, data, _ = self.request('/api/open-location', payload)
            self.assertEqual(status, 200)
            self.assertEqual(data, {'editor':'default','located':False})
            opened.assert_called_once_with(hit['ContextData']['path'])
        with patch('paragraph_search.plugin.os.startfile') as opened:
            self.assertEqual(self.request('/api/open-location', payload, auth=False)[0], 401)
            self.assertEqual(self.request('/api/open-location', dict(payload, text_hash='0'*64))[0], 400)
            self.assertEqual(self.request('/api/open-location', dict(payload, path=str(self.base/'config.example.json')))[0], 400)
            self.request('/api/config', dict(self.config, roots=[{'alias':'写作','path':'notes','exclude_dirs':['记录.md']}]))
            self.assertEqual(self.request('/api/open-location', payload)[0], 400)
            opened.assert_not_called()

    def test_context_boundaries_and_current_directory_rules(self):
        note = self.base / 'notes/记录.md'
        note.write_text('# 资料\n\n前一段。\n\n雨夜命中段落。\n\n后一段。', encoding='utf-8')
        hit = self.request('/api/search', {'query':'雨夜'})[1]['results'][0]
        payload = self.context_request(hit)
        status, data, _ = self.request('/api/context', payload)
        self.assertEqual(status, 200)
        self.assertEqual(data['before']['text'], '前一段。')
        self.assertEqual(data['selected']['text'], hit['ContextData']['text'])
        self.assertEqual(data['after']['text'], '后一段。')
        self.assertEqual(self.request('/api/context', payload, auth=False)[0], 401)
        self.assertEqual(self.request('/api/context', dict(payload, path=str(self.base / 'config.example.json')))[0], 400)
        self.assertEqual(self.request('/api/context', dict(payload, alias='其他'))[0], 400)
        self.assertEqual(self.request('/api/context', dict(payload, text_hash='0'*64))[0], 400)
        self.assertEqual(self.request('/api/context', dict(payload, line_start=True))[0], 400)
        self.assertEqual(self.request('/api/context', dict(payload, line_end=2**64))[0], 400)
        saved = dict(self.config, roots=[{'alias':'写作','path':'notes','exclude_dirs':['记录.md']}])
        self.request('/api/config', saved)
        self.assertEqual(self.request('/api/context', payload)[0], 400)

    def test_context_rejects_old_content_and_heading_even_inside_recent_cache_window(self):
        note = self.base / 'notes/记录.md'
        hit = self.request('/api/search', {'query':'雨夜'})[1]['results'][0]
        payload = self.context_request(hit)
        note.write_text('# 更名\n\n雨夜写下的段落。\n', encoding='utf-8')
        status, data, _ = self.request('/api/context', payload)
        self.assertEqual(status, 400)
        self.assertIn('重新检索', data['error'])
        hit = self.request('/api/search', {'query':'雨夜'})[1]['results'][0]
        payload = self.context_request(hit)
        note.write_text('# 更名\n\n内容已经更新。\n', encoding='utf-8')
        self.assertEqual(self.request('/api/context', payload)[0], 400)

    def test_long_selected_paragraph_does_not_need_a_large_post_body(self):
        note = self.base / 'notes/记录.md'
        text = '雨夜' + '中文资料' * 20000
        note.write_text(text + '\n\n末段', encoding='utf-8')
        hit = self.request('/api/search', {'query':'雨夜'})[1]['results'][0]
        payload = self.context_request(hit)
        self.assertLess(len(json.dumps(payload).encode()), 1024)
        status, data, _ = self.request('/api/context', payload)
        self.assertEqual(status, 200)
        self.assertEqual(data['selected']['text'], text)
        self.assertIsNone(data['before'])
        self.assertEqual(data['after']['text'], '末段')

    def test_invalid_save_preserves_existing_file(self):
        self.request('/api/config', self.config)
        before = (self.base / 'config.json').read_bytes()
        invalid = dict(self.config, roots=[{'alias': '缺失', 'path': 'missing'}])
        self.assertEqual(self.request('/api/config', invalid)[0], 400)
        self.assertEqual(self.request('/api/config', dict(self.config, max_results=True))[0], 400)
        self.assertEqual((self.base / 'config.json').read_bytes(), before)

    def test_unicode_duplicate_alias_rejected(self):
        invalid = dict(self.config, roots=[{'alias': 'Ａ', 'path': 'notes'}, {'alias': 'a', 'path': 'notes'}])
        self.assertEqual(self.request('/api/config', invalid)[0], 400)
        self.assertFalse((self.base / 'config.json').exists())

    def test_field_errors_identify_all_invalid_rows_and_preserve_config(self):
        self.request('/api/config', self.config)
        before = (self.base / 'config.json').read_bytes()
        invalid = dict(self.config, roots=[{'alias': 'Ａ', 'path': 'notes'}, {'alias': 'a', 'path': 'missing'}])
        status, data, _ = self.request('/api/config', invalid)
        self.assertEqual(status, 400)
        fields = {(item['index'], item['field']) for item in data['issues']}
        self.assertEqual(fields, {(0, 'alias'), (1, 'alias'), (1, 'path')})
        self.assertEqual((self.base / 'config.json').read_bytes(), before)

    def test_rejected_posts_deliver_error_instead_of_resetting_connection(self):
        for _ in range(3):
            status, data, _ = self.request('/api/config', self.config, auth=False)
            self.assertEqual(status, 401)
            self.assertIn('会话', data['error'])

    def test_request_requires_session_and_local_origin(self):
        self.assertEqual(self.request('/api/config', self.config, auth=False)[0], 401)
        self.assertEqual(self.request('/api/config', self.config, headers={'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.request('/api/config', self.config, headers={'Host': 'example.com'})[0], 403)
        self.assertFalse((self.base / 'config.json').exists())

    def test_static_files_are_explicitly_allowed(self):
        for route in ['/config.example.json', '/../config.example.json', '/unknown']:
            self.assertEqual(self.request(route)[0], 404)
        status, body, headers = self.request('/', auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'ParagraphBox', body)
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_query_shape_and_length_checked(self):
        for query in [[], 'x' * 501, None]:
            self.assertEqual(self.request('/api/search', {'query': query})[0], 400)

    def test_exclusion_save_normalizes_paths_and_invalid_rules_preserve_config(self):
        valid = dict(self.config, roots=[{'alias': '写作', 'path': 'notes', 'exclude_dirs': ['备份\\旧版/', '备份/旧版']}])
        status, data, _ = self.request('/api/config', valid)
        self.assertEqual(status, 200)
        self.assertEqual(data['config']['roots'][0]['exclude_dirs'], ['备份/旧版'])
        before = (self.base / 'config.json').read_bytes()
        for invalid_rule in ['../private', 'D:/private', '/private', '.', 'backups/*', 'bad\x00name', 'a/../b']:
            invalid = dict(self.config, roots=[{'alias': '写作', 'path': 'notes', 'exclude_dirs': [invalid_rule]}])
            status, data, _ = self.request('/api/config', invalid)
            self.assertEqual(status, 400, invalid_rule)
            self.assertEqual(data['issues'][0]['field'], 'exclude_dirs')
            self.assertEqual((self.base / 'config.json').read_bytes(), before)

    def test_rebuild_status_detects_saved_rule_changes_and_clears_stale_results(self):
        (self.base / 'notes/备份').mkdir()
        (self.base / 'notes/备份/旧.txt').write_text('备份独有词', encoding='utf-8')
        status, data, _ = self.request('/api/rebuild-index', {})
        self.assertEqual(status, 200)
        self.assertTrue(data['index_status']['config_current'])
        self.assertTrue(data['index_status']['report']['rebuilt'])
        self.assertEqual(data['stats']['files'], 2)
        updated = dict(self.config, roots=[{'alias': '写作', 'path': 'notes', 'exclude_dirs': ['备份']}])
        _, data, _ = self.request('/api/config', updated)
        self.assertFalse(data['index_status']['config_current'])
        _, data, _ = self.request('/api/rebuild-index', {})
        self.assertEqual(data['stats']['files'], 1)
        self.assertEqual(data['index_status']['report']['excluded'], 1)
        self.assertTrue(data['index_status']['config_current'])
        _, data, _ = self.request('/api/search', {'query': '备份独有词'})
        self.assertFalse(any(item.get('ContextData') for item in data['results']))
        self.assertEqual(self.request('/api/rebuild-index', {}, auth=False)[0], 401)
        self.assertEqual(self.request('/api/rebuild-index', {'force': True})[0], 400)

    def test_preview_open_file_checks_current_roots_exclusions_and_existence(self):
        note = self.base / 'notes/记录.md'
        with patch('paragraph_search.plugin.os.startfile') as opening:
            self.assertEqual(self.request('/api/open-file', {'path': str(note)})[0], 200)
            opening.assert_called_once_with(str(note.resolve()))
            self.assertEqual(self.request('/api/open-file', {'path': str(self.base / 'config.example.json')})[0], 400)
            self.assertEqual(self.request('/api/open-file', {'path': str(note)}, auth=False)[0], 401)
            (self.base / 'notes/备份').mkdir()
            hidden = self.base / 'notes/备份/旧.txt'
            hidden.write_text('资料', encoding='utf-8')
            updated = dict(self.config, roots=[{'alias': '写作', 'path': 'notes', 'exclude_dirs': ['备份']}])
            self.request('/api/config', updated)
            self.assertEqual(self.request('/api/open-file', {'path': str(hidden)})[0], 400)
            note.unlink()
            self.assertEqual(self.request('/api/open-file', {'path': str(note)})[0], 400)
            self.assertEqual(opening.call_count, 1)

    def test_health_reports_running_service_version_after_files_are_upgraded(self):
        (self.base / 'plugin.json').write_text(json.dumps({'Name': 'ParagraphBox', 'Version': '0.2.2'}), encoding='utf-8')
        _, data, _ = self.request('/api/health')
        self.assertEqual(data['version'], '0.2.0')

    def test_progress_is_visible_during_real_read_and_requires_session(self):
        operation = "progress-rebuild-023"
        reading, release = threading.Event(), threading.Event()
        original = Path.open
        note = (self.base / 'notes/记录.md').resolve()
        def hold_read(path, *args, **kwargs):
            if path == note and args and args[0] == 'rb':
                reading.set()
                if not release.wait(timeout=3):
                    raise TimeoutError("test did not release read")
            return original(path, *args, **kwargs)
        responses = []
        worker = threading.Thread(target=lambda: responses.append(self.request('/api/rebuild-index', {}, headers={'X-ParagraphBox-Operation': operation})))
        with patch.object(Path, 'open', hold_read):
            worker.start()
            try:
                self.assertTrue(reading.wait(timeout=2))
                route = '/api/index-progress?operation=' + operation
                self.assertEqual(self.request(route, auth=False)[0], 401)
                status, data, _ = self.request(route)
                self.assertEqual(status, 200)
                self.assertEqual(data['progress']['status'], 'running')
                self.assertEqual(data['progress']['stage'], 'read')
                self.assertEqual(data['progress']['processed'], 1)
                self.assertEqual(data['progress']['updated'], 0)
                self.assertGreaterEqual(data['progress']['elapsed_ms'], 0)
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertEqual(responses[0][0], 200)
        finished = self.request(route)[1]['progress']
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual(finished['updated'], 1)
        self.assertEqual(finished['stage'], 'done')
        self.assertEqual(self.request('/api/rebuild-index', {}, headers={'X-ParagraphBox-Operation': operation})[0], 400)
        self.assertEqual(self.request('/api/index-progress?operation=bad')[0], 400)

    def test_failed_rebuild_keeps_previous_report_and_tracks_failure(self):
        import sqlite3
        self.request('/api/rebuild-index', {})
        before = self.request('/api/config')[1]['index_status']['report']
        with patch('paragraph_search.engine._paragraphs', side_effect=sqlite3.OperationalError('interrupted rebuild')):
            status, _, _ = self.request('/api/rebuild-index', {}, headers={'X-ParagraphBox-Operation': 'failed-rebuild-023'})
        self.assertEqual(status, 400)
        self.assertEqual(self.request('/api/config')[1]['index_status']['report'], before)
        self.assertEqual(self.request('/api/index-progress?operation=failed-rebuild-023')[1]['progress']['status'], 'failed')

    def test_separate_operations_do_not_overwrite_each_other_and_history_is_bounded(self):
        with self.server.track_index('first-operation-023') as first:
            first({'stage': 'scan', 'processed': 8, 'updated': 3, 'skipped': 0})
            with self.server.track_index('second-operation-023') as second:
                second({'stage': 'read', 'processed': 1, 'updated': 0, 'skipped': 0})
                self.assertEqual(self.server.index_progress('first-operation-023')['processed'], 8)
                self.assertEqual(self.server.index_progress('second-operation-023')['processed'], 1)
        for number in range(80):
            with self.server.track_index(f'bounded-operation-{number:03}'):
                pass
        self.assertLessEqual(len(self.server.operations), 64)
        self.assertEqual(self.request('/api/index-progress?operation=unknown-operation-023')[1], {'progress': None})


if __name__ == '__main__':
    unittest.main()
