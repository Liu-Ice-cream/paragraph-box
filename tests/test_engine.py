"""Behavioral tests for the paragraph search engine, using disposable data."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paragraph_search.engine import ParagraphIndex, Root  # noqa: E402


class ParagraphIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.notes = self.base / "notes"
        self.notes.mkdir()
        self.cache = self.base / "index.sqlite3"
        self.open_indexes: list[ParagraphIndex] = []

    def tearDown(self) -> None:
        for index in self.open_indexes:
            index.close()
        self.temp.cleanup()

    def make_index(
        self, roots: list[Root] | None = None, max_file_bytes: int = 2_000_000
    ) -> ParagraphIndex:
        index = ParagraphIndex(
            self.cache,
            roots if roots is not None else [Root("写作", self.notes)],
            max_file_bytes=max_file_bytes,
        )
        self.open_indexes.append(index)
        return index

    def close_index(self, index: ParagraphIndex) -> None:
        index.close()
        self.open_indexes.remove(index)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.notes / name
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_mtime = path.stat().st_mtime_ns if path.exists() else None
        path.write_text(text, encoding=encoding)
        if previous_mtime is not None:
            stat = path.stat()
            # Exercise the documented mtime+size check without relying on sleeps.
            os.utime(
                path,
                ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous_mtime + 2_000_000_000)),
            )
        return path

    def test_chinese_substrings_and_space_separated_tokens(self) -> None:
        path = self.write(
            "中文笔记.md",
            "机器学习帮助整理写作素材。\n\n机器翻译处理文本。\n\n学习计划安排在周末。\n",
        )
        index = self.make_index()
        index.sync()

        hits = index.search("机器 学习")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].path, path)
        self.assertIn("机器学习", hits[0].text)
        self.assertEqual(hits[0].alias, "写作")
        self.assertEqual(len(index.search("机器")), 2)
        self.assertEqual(index.search("机器 缺失词"), [])

    def test_recent_scan_is_shared_by_new_connections_and_expires_without_sliding(self):
        self.write("记录.txt", "原段落")
        with patch("paragraph_search.engine.time.time", return_value=100):
            index = self.make_index()
            first = index.sync(min_interval=2)
        self.close_index(index)
        self.write("记录.txt", "新的内容")
        index = self.make_index()
        with patch("paragraph_search.engine.time.time", return_value=101.9), patch.object(index, "_walk", side_effect=AssertionError("must reuse")):
            reused = index.sync(min_interval=2)
            self.assertTrue(reused["cache_reused"])
            self.assertEqual(reused["checked_at"], first["checked_at"])
            self.assertEqual(reused["finished_at"], first["finished_at"])
            self.assertEqual(index.search("新的内容"), [])
        with patch("paragraph_search.engine.time.time", return_value=102):
            refreshed = index.sync(min_interval=2)
            self.assertFalse(refreshed["cache_reused"])
            self.assertEqual(refreshed["updated"], 1)
            self.assertEqual(len(index.search("新的内容")), 1)

    def test_recent_scan_bypasses_config_changes_force_old_reports_and_clock_reversal(self):
        self.write("备份/旧.txt", "排除测试")
        with patch("paragraph_search.engine.time.time", return_value=100):
            index = self.make_index()
            index.sync(min_interval=2)
            other = self.make_index([Root("写作", self.notes, ("备份",))])
            self.assertEqual(other.sync(min_interval=2)["deleted"], 1)
            self.assertEqual(other.search("排除测试"), [])
            self.assertFalse(index.sync(min_interval=2)["cache_reused"])
            smaller = self.make_index(max_file_bytes=1)
            self.assertEqual(smaller.sync(min_interval=2)["files"], 0)
            self.assertEqual(index.sync(min_interval=2)["files"], 1)
            forced = index.sync(force=True, min_interval=2)
            self.assertEqual(forced["updated"], 1)
            self.assertFalse(forced["cache_reused"])
            old = dict(forced)
            old.pop("report_version")
            index._connection.execute("UPDATE index_metadata SET value=? WHERE key='last_sync'", (json.dumps(old),))
            self.assertFalse(index.sync(min_interval=2)["cache_reused"])
        with patch("paragraph_search.engine.time.time", return_value=99):
            self.assertFalse(index.sync(min_interval=2)["cache_reused"])

    def test_concurrent_requests_share_one_recent_scan(self):
        self.write("并发.txt", "共享索引")
        barrier = threading.Barrier(6)
        def query(_):
            with ParagraphIndex(self.cache, [Root("写作", self.notes)]) as index:
                barrier.wait(timeout=10)
                report = index.sync(min_interval=2)
                return report, index.search("共享索引")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(query, range(6)))
        self.assertEqual(sum(not report["cache_reused"] for report, _ in results), 1)
        self.assertTrue(all(len(hits) == 1 for _, hits in results))

    def test_malformed_cache_reports_trigger_a_scan(self):
        self.write("记录.txt", "中文检索")
        index = self.make_index()
        first = index.sync()
        for invalid in ([], [1], "old", None, dict(first, checked_at=float('nan')), dict(first, checked_at=True)):
            index._connection.execute("UPDATE index_metadata SET value=? WHERE key='last_sync'", (json.dumps(invalid),))
            self.assertFalse(index.sync(min_interval=2)["cache_reused"])

    def test_progress_and_timings_describe_actual_work_and_persist(self):
        self.write("资料.md", "# 标题\n\n中文段落")
        events = []
        index = self.make_index()
        report = index.sync(progress=events.append)
        self.assertEqual(events[0]["stage"], "waiting")
        self.assertEqual(events[-1]["stage"], "done")
        self.assertEqual(events[-1]["processed"], 1)
        self.assertEqual(events[-1]["updated"], 1)
        self.assertTrue({"scan", "read", "parse", "database", "commit"} <= {event["stage"] for event in events})
        self.assertTrue(all(value >= 0 for value in report["timings_ms"].values()))
        self.assertAlmostEqual(sum(report["timings_ms"].values()), report["duration_ms"], delta=1)
        stored = json.loads(index._connection.execute("SELECT value FROM index_metadata WHERE key='last_sync'").fetchone()[0])
        self.assertEqual(stored, report)

    def test_normalization_casefold_and_query_whitespace(self) -> None:
        self.write("normalized.txt", "ＡＩ Straße 中文资料\n\nAI only\n")
        index = self.make_index()
        index.sync()

        hits = index.search("  ai\tSTRASSE\n中文  ")
        self.assertEqual(len(hits), 1)
        self.assertIn("ＡＩ", hits[0].text)
        self.assertIn("Straße", hits[0].text)
        self.assertEqual(len(index.search("ａｉ")), 2)

    def test_large_match_set_keeps_score_caps_and_deterministic_ties(self):
        # Frequency is capped at ten. All of these top paragraphs tie, so file
        # path and original line order must still select the same first thirty.
        expected = self.write('a.md', '# 中文\n\n' + '\n\n'.join('中文' * count for count in range(10, 70)))
        self.write('b.md', '# 中文\n\n' + '\n\n'.join('中文' * count for count in range(1, 70)))
        index = self.make_index()
        index.sync()
        metrics = {}
        hits = index.search('中文', limit=30, metrics=metrics)
        self.assertEqual(len(hits), 30)
        self.assertTrue(all(hit.path == expected and hit.score == 245 for hit in hits))
        self.assertEqual([hit.line_start for hit in hits], list(range(3, 63, 2)))
        self.assertEqual(metrics['matched'], 129)
        self.assertEqual(metrics['returned'], 30)
        self.assertEqual(hits, index.search('中文', limit=30))
        self.assertAlmostEqual(sum(metrics[name] for name in ('query_ms','ranking_ms','result_ms')), metrics['duration_ms'], delta=0.1)
        self.assertEqual(len(index.search('中文', limit=500)), 129)

    def test_context_uses_adjacent_parsed_paragraphs_in_one_file(self):
        path = self.write('上下文.md', '# 开头\n\n第一段\n\n```\n不算段落\n```\n\n## 中间\n\n命中内容\n\n最后一段')
        self.write('其他.md', '不能混入其他文件')
        index = self.make_index()
        index.sync()
        selected = index.search('命中内容')[0]
        digest = hashlib.sha256((selected.heading + '\0' + selected.text).encode()).hexdigest()
        context = index.paragraph_context(path, '写作', selected.line_start, selected.line_end, digest)
        self.assertEqual(context['before'].text, '第一段')
        self.assertEqual(context['selected'].text, '命中内容')
        self.assertEqual(context['after'].text, '最后一段')
        self.assertEqual(context['before'].heading, '开头')
        self.assertEqual(context['selected'].heading, '开头 > 中间')
        first = context['before']
        first_hash = hashlib.sha256((first.heading + '\0' + first.text).encode()).hexdigest()
        self.assertIsNone(index.paragraph_context(path, '写作', first.line_start, first.line_end, first_hash)['before'])
        last = context['after']
        last_hash = hashlib.sha256((last.heading + '\0' + last.text).encode()).hexdigest()
        self.assertIsNone(index.paragraph_context(path, '写作', last.line_start, last.line_end, last_hash)['after'])
        for wrong in ('0' * 64, hashlib.sha256(selected.text.encode()).hexdigest()):
            with self.assertRaises(ValueError):
                index.paragraph_context(path, '写作', selected.line_start, selected.line_end, wrong)

    def test_alias_filter_and_result_limit(self) -> None:
        work = self.base / "work"
        work.mkdir()
        self.write("story.txt", "共享关键词 写作第一段\n\n共享关键词 写作第二段\n")
        work_path = work / "meeting.txt"
        work_path.write_text("共享关键词 工作记录\n", encoding="utf-8")
        index = self.make_index([Root("写作", self.notes), Root("工作", work)])
        index.sync()

        self.assertEqual(len(index.search("共享关键词")), 3)
        work_hits = index.search("共享关键词", alias="工作")
        self.assertEqual(len(work_hits), 1)
        self.assertEqual(work_hits[0].path, work_path)
        self.assertEqual(work_hits[0].alias, "工作")
        self.assertEqual(index.search("共享关键词", alias="未配置"), [])
        self.assertEqual(len(index.search("共享关键词", limit=1)), 1)

    def test_markdown_heading_hierarchy_line_ranges_and_exclusions(self) -> None:
        path = self.write(
            "chapters.md",
            "---\n"
            "title: 前言暗语\n"
            "---\n"
            "# 总章\n"
            "\n"
            "第一段中文内容\n"
            "同一段第二行\n"
            "\n"
            "## 子章\n"
            "\n"
            "第二段中文内容\n"
            "\n"
            "```text\n"
            "围栏暗语\n"
            "\n"
            "围栏第二段\n"
            "```\n"
            "\n"
            "新章\n"
            "====\n"
            "\n"
            "第三段中文内容\n",
        )
        index = self.make_index()
        report = index.sync()
        self.assertEqual(report["paragraphs"], 3)

        first = index.search("第一段")[0]
        self.assertEqual(first.path, path)
        self.assertEqual(first.heading, "总章")
        self.assertEqual(first.text, "第一段中文内容\n同一段第二行")
        self.assertEqual((first.line_start, first.line_end), (6, 7))
        second = index.search("第二段")[0]
        self.assertEqual(second.heading, "总章 > 子章")
        self.assertEqual((second.line_start, second.line_end), (11, 11))
        third = index.search("第三段")[0]
        self.assertEqual(third.heading, "新章")
        self.assertEqual((third.line_start, third.line_end), (22, 22))
        self.assertEqual(index.search("前言暗语"), [])
        self.assertEqual(index.search("围栏暗语"), [])
        self.assertEqual(index.search("围栏第二段"), [])

    def test_txt_preserves_markdown_like_content_as_plain_text(self) -> None:
        self.write("plain.txt", "# 普通文字\n```\n保留内容\n```\n\n末尾段落")
        index = self.make_index()
        report = index.sync()

        self.assertEqual(report["paragraphs"], 2)
        hit = index.search("保留内容")[0]
        self.assertEqual(hit.heading, "")
        self.assertEqual((hit.line_start, hit.line_end), (1, 4))
        self.assertIn("# 普通文字", hit.text)
        tail = index.search("末尾段落")[0]
        self.assertEqual((tail.line_start, tail.line_end), (6, 6))

    def test_whitespace_only_lines_split_paragraphs_and_crlf_lines_count(self) -> None:
        path = self.notes / "windows.txt"
        path.write_bytes("第一段\r\n续行\r\n  \t\r\n第二段\r\n".encode("utf-8"))
        index = self.make_index()
        index.sync()

        first = index.search("第一段")[0]
        self.assertEqual((first.line_start, first.line_end), (1, 2))
        self.assertEqual(first.text, "第一段\n续行")
        second = index.search("第二段")[0]
        self.assertEqual((second.line_start, second.line_end), (4, 4))

    def test_incremental_index_survives_reopening_and_tracks_changes(self) -> None:
        first_path = self.write("first.md", "旧版材料\n")
        index = self.make_index()
        first_report = index.sync()
        self.assertEqual(first_report["files"], 1)
        self.assertEqual(first_report["paragraphs"], 1)
        self.assertEqual(first_report["updated"], 1)
        self.assertIsInstance(first_report["warnings"], list)
        self.close_index(index)

        reopened = self.make_index()
        self.assertEqual(len(reopened.search("旧版材料")), 1)
        self.assertEqual(reopened.sync()["updated"], 0)

        # The replacement has the same byte length, so mtime must be considered.
        self.write("first.md", "新版材料\n")
        added_path = self.write("nested/new.txt", "新增材料\n")
        report = reopened.sync()
        self.assertEqual(report["files"], 2)
        self.assertEqual(report["updated"], 2)
        self.assertEqual(reopened.search("旧版材料"), [])
        self.assertEqual(reopened.search("新版材料")[0].path, first_path)
        self.assertEqual(reopened.search("新增材料")[0].path, added_path)

        first_path.unlink()
        added_path.unlink()
        report = reopened.sync()
        self.assertEqual(report["files"], 0)
        self.assertEqual(report["paragraphs"], 0)
        self.assertEqual(reopened.search("材料"), [])

    def test_size_change_is_detected_even_if_mtime_is_restored(self) -> None:
        path = self.write("same-time.txt", "原始文本\n")
        index = self.make_index()
        index.sync()
        before = path.stat()
        path.write_text("替换后的更长文本\n", encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        self.assertEqual(index.sync()["updated"], 1)
        self.assertEqual(index.search("原始文本"), [])
        self.assertEqual(len(index.search("替换后的更长文本")), 1)

    def test_supported_encodings_are_searchable(self) -> None:
        for encoding, name in [
            ("utf-8-sig", "utf8-bom.txt"),
            ("utf-16", "utf16-bom.md"),
            ("gb18030", "gb18030.txt"),
        ]:
            self.write(name, "中文编码测试 " + name + "\n", encoding=encoding)
        (self.notes / "utf16-be.txt").write_bytes(
            b"\xfe\xff" + "中文编码测试 大端序\n".encode("utf-16-be")
        )
        index = self.make_index()
        report = index.sync()

        self.assertEqual(report["paragraphs"], 4)
        self.assertEqual(len(index.search("中文编码测试")), 4)
        self.assertEqual(len(index.search("大端序")), 1)

    def test_oversized_files_are_skipped_and_stale_hits_removed(self) -> None:
        path = self.write("growing.txt", "初始内容\n")
        self.write("small.txt", "小文件内容\n")
        index = self.make_index(max_file_bytes=64)
        index.sync()
        self.assertEqual(len(index.search("初始内容")), 1)
        self.write("growing.txt", "超过限制" * 100)

        report = index.sync()
        self.assertGreaterEqual(report["skipped"], 1)
        self.assertEqual(index.search("初始内容"), [])
        self.assertEqual(index.search("超过限制"), [])
        self.assertEqual(len(index.search("小文件内容")), 1)
        self.assertTrue(path.exists())

    def test_removed_root_does_not_leave_cached_results(self) -> None:
        work = self.base / "work"
        work.mkdir()
        (work / "work.txt").write_text("工作独有词\n", encoding="utf-8")
        self.write("writing.txt", "写作独有词\n")
        index = self.make_index([Root("写作", self.notes), Root("工作", work)])
        index.sync()
        self.close_index(index)

        reduced = self.make_index([Root("写作", self.notes)])
        reduced.sync()
        self.assertEqual(reduced.search("工作独有词"), [])
        self.assertEqual(len(reduced.search("写作独有词")), 1)

    def test_concurrent_first_use_of_one_persisted_index(self) -> None:
        self.write("concurrent.txt", "并发检索材料\n")
        barrier = threading.Barrier(6)

        def query_from_independent_instance() -> tuple[int, str]:
            barrier.wait(timeout=10)
            index = ParagraphIndex(self.cache, [Root("写作", self.notes)])
            try:
                report = index.sync()
                hits = index.search("并发检索")
                return report["files"], hits[0].text
            finally:
                index.close()

        # Flow invokes independent processes; separate SQLite connections expose
        # the same first-use and writer contention without launching app windows.
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(query_from_independent_instance) for _ in range(6)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(results, [(1, "并发检索材料")] * 6)

    def test_external_symlinks_are_not_indexed(self) -> None:
        external = self.base / "external"
        external.mkdir()
        outside_file = external / "outside.txt"
        outside_file.write_text("外部私有暗语\n", encoding="utf-8")
        try:
            (self.notes / "linked-directory").symlink_to(external, target_is_directory=True)
            (self.notes / "linked-file.txt").symlink_to(outside_file)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Creating symlinks is unavailable on this system: {exc}")
        self.write("inside.txt", "内部正常内容\n")
        index = self.make_index()
        index.sync()

        self.assertEqual(index.search("外部私有暗语"), [])
        self.assertEqual(len(index.search("内部正常内容")), 1)

    def test_relative_exclusions_prune_cached_hits_and_do_not_match_siblings(self) -> None:
        self.write("备份/旧版/记录.txt", "旧版资料")
        self.write("备份2/记录.txt", "正常资料")
        self.write("子目录/备份/记录.txt", "嵌套资料")
        initial = self.make_index()
        initial.sync()
        self.close_index(initial)
        filtered = self.make_index([Root("写作", self.notes, ("备份",))])
        report = filtered.sync()
        self.assertEqual(report["files"], 2)
        self.assertEqual(report["deleted"], 1)
        self.assertEqual(report["excluded"], 1)
        self.assertEqual(filtered.search("旧版资料"), [])
        self.assertEqual(len(filtered.search("正常资料")), 1)
        self.assertEqual(len(filtered.search("嵌套资料")), 1)
        self.close_index(filtered)
        restored = self.make_index()
        self.assertEqual(restored.sync()["updated"], 1)
        self.assertEqual(len(restored.search("旧版资料")), 1)

    def test_new_exclusions_remove_cached_hits_when_root_becomes_unreadable(self) -> None:
        self.write("备份/旧.txt", "排除资料")
        self.write("保留.txt", "保留资料")
        index = self.make_index()
        index.sync()
        self.close_index(index)
        filtered = self.make_index([Root("写作", self.notes, ("备份",))])
        with patch("paragraph_search.engine.os.scandir", side_effect=PermissionError("unreadable")):
            report = filtered.sync()
        self.assertEqual(report["files"], 1)
        self.assertEqual(filtered.search("排除资料"), [])
        self.assertEqual(len(filtered.search("保留资料")), 1)

    def test_path_exclusion_does_not_expand_letters_like_alias_matching(self) -> None:
        self.write("Straße/旧.txt", "排除资料")
        self.write("Strasse/新.txt", "保留资料")
        index = self.make_index([Root("写作", self.notes, ("Straße",))])
        self.assertEqual(index.sync()["files"], 1)
        self.assertEqual(index.search("排除资料"), [])
        self.assertEqual(len(index.search("保留资料")), 1)

    def test_force_rebuild_detects_same_size_same_time_changes_and_persists_report(self) -> None:
        note = self.write("记录.txt", "原始文本")
        index = self.make_index()
        index.sync()
        before = note.stat()
        note.write_text("更新文本", encoding="utf-8")
        os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(index.sync()["updated"], 0)
        report = index.sync(force=True)
        self.assertTrue(report["rebuilt"])
        self.assertEqual(report["updated"], 1)
        self.assertEqual(index.search("原始文本"), [])
        self.assertEqual(len(index.search("更新文本")), 1)
        self.close_index(index)
        reopened = self.make_index()
        self.assertIn(report["finished_at"], reopened._connection.execute("SELECT value FROM index_metadata WHERE key='last_sync'").fetchone()[0])

    def test_rebuild_failure_rolls_back_content_and_previous_report(self) -> None:
        note = self.write("记录.txt", "原始文本")
        index = self.make_index()
        index.sync()
        old_report = index._connection.execute("SELECT value FROM index_metadata WHERE key='last_sync'").fetchone()[0]
        note.write_text("更新文本", encoding="utf-8")
        with patch("paragraph_search.engine._paragraphs", side_effect=RuntimeError("unexpected failure")):
            with self.assertRaises(RuntimeError):
                index.sync(force=True)
        self.assertEqual(len(index.search("原始文本")), 1)
        self.assertEqual(index._connection.execute("SELECT value FROM index_metadata WHERE key='last_sync'").fetchone()[0], old_report)

    def test_skip_diagnostics_are_bounded_but_counts_remain_complete(self) -> None:
        for number in range(60):
            self.write(f"large-{number}.txt", "资料" * 20)
        report = self.make_index(max_file_bytes=10).sync()
        self.assertEqual(report["skipped"], 60)
        self.assertEqual(report["issue_count"], 60)
        self.assertEqual(report["reasons"], {"too_large": 60})
        self.assertEqual(len(report["issues"]), 50)
        self.assertEqual(len(report["warnings"]), 50)

    @unittest.skipUnless(os.name == "nt", "Directory junctions are a Windows feature")
    def test_external_windows_junction_is_not_indexed(self) -> None:
        external = self.base / "junction-target"
        external.mkdir()
        (external / "private.txt").write_text("连接外部暗语\n", encoding="utf-8")
        junction = self.notes / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("Creating a directory junction is unavailable on this system")
        self.write("local.txt", "本地正常段落\n")
        index = self.make_index()
        index.sync()

        self.assertEqual(index.search("连接外部暗语"), [])
        self.assertEqual(len(index.search("本地正常段落")), 1)


if __name__ == "__main__":
    unittest.main()
