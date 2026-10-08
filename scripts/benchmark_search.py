"""Compare cached searches using synthetic documents and a local Git baseline."""

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import types

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from paragraph_search.engine import ParagraphIndex, Root
from paragraph_search.plugin import hit_result


def identity(hits):
    return [(hit.alias, str(hit.path), hit.heading, hit.text, hit.line_start, hit.line_end, hit.score) for hit in hits]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare-ref', required=True, help='Baseline commit already available in your local Git history')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    ref = subprocess.run(['git', 'rev-parse', '--verify', args.compare_ref + '^{commit}'], cwd=BASE,
                         check=True, capture_output=True, text=True).stdout.strip()
    source = subprocess.run(['git','show',ref + ':paragraph_search/engine.py'], cwd=BASE,
                            check=True,capture_output=True).stdout.decode('utf-8')
    baseline = types.ModuleType('paragraph_search._search_baseline')
    sys.modules[baseline.__name__] = baseline
    exec(compile(source,'<local-baseline-engine>','exec'),baseline.__dict__)
    with tempfile.TemporaryDirectory(prefix='paragraphbox-search-') as folder:
        base = Path(folder)
        writing, work = base / '写作', base / '工作'
        (writing / '备份').mkdir(parents=True)
        work.mkdir()
        for number in range(1000):
            parent = writing if number < 450 else work if number < 900 else writing / '备份'
            text = '\n\n'.join(f'第 {part} 段，中文检索材料，文档编号 {number:04}。' +
                                ('ＡＩ Straße' if part == 9 and number % 17 == 0 else '') for part in range(10))
            (parent / f'资料-{number:04}.md').write_text('# 人工资料\n\n' + text,encoding='utf-8')
        roots = [Root('写作',writing,('备份',)),Root('工作',work)]
        cache = base / 'index.sqlite3'
        with ParagraphIndex(cache,roots) as index:
            assert index.sync()['paragraphs'] == 9000
        def refresh():
            with ParagraphIndex(cache,roots) as index:
                index.sync()
        def request(engine, query, alias):
            started = time.perf_counter()
            with engine(cache,roots) as index:
                report = index.sync(min_interval=2)
                assert report['cache_reused']
                search = {}
                query_started = time.perf_counter()
                hits = index.search(query,alias=alias,limit=30,metrics=search) if engine is ParagraphIndex else index.search(query,alias=alias,limit=30)
                search_ms = (time.perf_counter() - query_started) * 1000
                formatted = time.perf_counter()
                results = [hit_result(hit,query) for hit in hits]
                format_ms = (time.perf_counter() - formatted) * 1000
            return {'pipeline_ms':(time.perf_counter()-started)*1000,'search_ms':search_ms,'format_ms':format_ms,
                    'stages':search,'hits':identity(hits),'results':results}
        cases = [('broad','中文 检索',None),('alias','中文 检索','写作'),('sparse','文档编号 0007',None),
                 ('heading','人工资料',None),('unicode','ai STRASSE',None),('no_match','不存在的关键词',None)]
        report_cases = []
        for name, query, alias in cases:
            old, new = [], []
            for attempt in range(5):
                refresh()
                # Alternate order to reduce the effect of an always-first run.
                pair = [ParagraphIndex,baseline.ParagraphIndex] if attempt % 2 else [baseline.ParagraphIndex,ParagraphIndex]
                measured = {engine:request(engine,query,alias) for engine in pair}
                before, after = measured[baseline.ParagraphIndex], measured[ParagraphIndex]
                assert before['hits'] == after['hits'] and before['results'] == after['results']
                old.append(before)
                new.append(after)
            report_cases.append({'case':name,'matched':new[-1]['stages']['matched'],'returned':len(new[-1]['hits']),
                                 'baseline_pipeline_median_ms':round(statistics.median(item['pipeline_ms'] for item in old),2),
                                 'current_pipeline_median_ms':round(statistics.median(item['pipeline_ms'] for item in new),2),
                                 'baseline_search_median_ms':round(statistics.median(item['search_ms'] for item in old),2),
                                 'current_search_median_ms':round(statistics.median(item['search_ms'] for item in new),2),
                                 'current_format_median_ms':round(statistics.median(item['format_ms'] for item in new),2),
                                 'current_stage_medians_ms':{key:round(statistics.median(item['stages'][key] for item in new),2)
                                                             for key in ('query_ms','ranking_ms','result_ms')}})
        memory = {}
        for name,engine in [('baseline',baseline.ParagraphIndex),('current',ParagraphIndex)]:
            with engine(cache,roots) as index:
                tracemalloc.start()
                try:
                    hits = index.search('中文 检索',limit=30)
                    memory[name + '_peak_bytes'] = tracemalloc.get_traced_memory()[1]
                    assert len(hits) == 30
                finally:
                    tracemalloc.stop()
        result = {'baseline_ref':ref,'synthetic_files':1000,'indexed_files':900,'paragraphs':9000,'runs_per_case':5,
                  'scope':'Independent connections, recent-index reuse, keyword matching, ranking and unchanged Flow formatting; excludes Python process startup and UI rendering',
                  'stage_scope':'query_ms includes SQLite execute/fetch; ranking_ms includes score calculation and top-result selection; result_ms builds selected SearchHit objects',
                  'cases':report_cases,'broad_search_memory':memory,
                  'checks':'Scores, stable order, ContextData and JSON actions identical to baseline across all cases'}
        document = json.dumps(result,indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(document + '\n',encoding='utf-8')
        print(document)


if __name__ == '__main__':
    main()
