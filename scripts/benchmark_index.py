"""Exercise 1,000 synthetic files; never scans the user's documents."""

import json
import argparse
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paragraph_search.engine import ParagraphIndex, Root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare-ref', help='Compare with a local Git commit using the same synthetic documents')
    parser.add_argument('--output', type=Path, help='Save the measured JSON report')
    args = parser.parse_args()
    baseline = None
    if args.compare_ref:
        repository = Path(__file__).resolve().parents[1]
        ref = subprocess.run(['git', 'rev-parse', '--verify', args.compare_ref + '^{commit}'], cwd=repository,
                             check=True, capture_output=True, text=True).stdout.strip()
        source = subprocess.run(['git', 'show', ref + ':paragraph_search/engine.py'], cwd=repository,
                                check=True, capture_output=True).stdout.decode('utf-8')
        baseline = types.ModuleType('paragraph_search._benchmark_baseline')
        sys.modules[baseline.__name__] = baseline
        exec(compile(source, '<local-baseline-engine>', 'exec'), baseline.__dict__)
    with tempfile.TemporaryDirectory(prefix="paragraphbox-benchmark-") as folder:
        base = Path(folder)
        notes, backups = base / "资料", base / "资料/备份"
        backups.mkdir(parents=True)
        for number in range(1000):
            parent = backups if number >= 900 else notes
            text = "\n\n".join(f"第 {part} 段，中文检索材料，文档编号 {number}。" for part in range(10))
            (parent / f"资料-{number:04}.md").write_text("# 人工资料\n\n" + text, encoding="utf-8")
        roots = [Root("基准", notes, ("备份",))]
        cache = base / "index.sqlite3"
        def query(engine=ParagraphIndex, target=cache, *, min_interval=2):
            started = time.perf_counter()
            with engine(target, roots) as index:
                report = index.sync(min_interval=min_interval) if engine is ParagraphIndex else index.sync()
                hits = index.search("中文 检索", limit=30)
            return (time.perf_counter() - started) * 1000, report, hits
        def identity(hits):
            return [(hit.alias, str(hit.path), hit.heading, hit.text, hit.line_start, hit.line_end, hit.score) for hit in hits]
        before_cold = query(baseline.ParagraphIndex, base / 'baseline.sqlite3')[0] if baseline else None
        cold_ms, first, hits = query()
        assert first["files"] == 900 and first["paragraphs"] == 9000 and first["excluded"] == 1
        assert hits and all("备份" not in hit.path.parts for hit in hits)
        warm, before_warm = [], []
        for _ in range(5):
            if baseline:
                elapsed, _, old_hits = query(baseline.ParagraphIndex, base / 'baseline.sqlite3')
                before_warm.append(elapsed)
                assert identity(old_hits) == identity(hits)
            # Refresh outside the measurement so every measured request starts
            # within the documented 2-second window, regardless of baseline speed.
            query(min_interval=0)
            elapsed, recent, current_hits = query()
            assert recent['cache_reused'] and identity(current_hits) == identity(hits)
            warm.append(elapsed)
        note = notes / "资料-0000.md"
        note.write_text("# 人工资料\n\n新增同步词", encoding="utf-8")
        (notes / "资料-0001.md").unlink()
        (notes / "新增.md").write_text("新增同步词", encoding="utf-8")
        _, stale, _ = query()
        assert stale['cache_reused']
        time.sleep(2.05)
        _, changed, _ = query()
        assert changed["updated"] == 2 and changed["deleted"] == 1 and changed["files"] == 900
        with ParagraphIndex(cache, roots) as index:
            before = index.search("中文 检索", limit=30)
            assert len(index.search("新增同步词")) == 2
            started = time.perf_counter()
            rebuilt = index.sync(force=True)
            rebuild_ms = (time.perf_counter() - started) * 1000
            assert index.search("中文 检索", limit=30) == before
            assert rebuilt["updated"] == 900 and rebuilt["issue_count"] == 0
        with ParagraphIndex(cache, [Root("基准", notes)]) as index:
            assert index.sync()["files"] == 1000
        with ParagraphIndex(cache, roots) as index:
            reexcluded = index.sync()
            assert reexcluded["deleted"] == 100 and reexcluded["files"] == 900
        result = {"synthetic_files":1000, "indexed_files":900, "initial_paragraphs":9000,
                  "first_query_ms":round(cold_ms, 1), "first_sync_timings_ms":first['timings_ms'],
                  "recent_query_median_ms":round(statistics.median(warm), 1),
                  "recent_query_max_ms":round(max(warm), 1), "rebuild_ms":round(rebuild_ms, 1),
                  "rebuild_timings_ms":rebuilt['timings_ms'],
                  "measurement_scope":"independent SQLite connections; includes sync and ranking; excludes Python startup and Flow UI",
                  "checks":"recent reuse and real expiry, exclusions, additions, changes, deletions, rebuild equivalence passed"}
        if baseline:
            result.update(baseline_ref=ref, baseline_first_query_ms=round(before_cold, 1),
                          baseline_warm_median_ms=round(statistics.median(before_warm), 1),
                          baseline_warm_max_ms=round(max(before_warm), 1),
                          cold_comparison_note="baseline runs first; filesystem cache can affect cold results; no cold speedup inferred")
        document = json.dumps(result, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(document + '\n', encoding='utf-8')
        print(document)


if __name__ == "__main__":
    main()
