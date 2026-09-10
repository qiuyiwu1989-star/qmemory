"""Reproducible synthetic consumer quality/cost report; never accesses a real archive."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sqlite3
import tempfile
import time
from pathlib import Path

from qmemory.service import MemoryService


def live_readonly_comparison():
    from qmemory.config import default_home
    from qmemory.context import build_context
    from qmemory.project import ProjectIdentity
    from qmemory.store import Projection
    class ReadProjection(Projection):
        def __init__(self):
            self.path = default_home() / 'state' / 'qmemory.sqlite3'
        def connect(self):
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            return db
    projection = ReadProjection()
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'tests/fixtures/opendesign_consumer.json').read_text())
    results = []
    for pid in ('git:github.com/qiuyiwu1989-star/opendesign', 'git:github.com/qiuyiwu1989-star/opendesign-docs'):
        project = ProjectIdentity(pid, '', pid[4:], None)
        old_empty = new_empty = 0
        kinds, stale = set(), set()
        for query in fixture['queries']:
            old_empty += not bool(projection.search(pid, query, limit=5))
            context = build_context(projection, project, query, 5)
            new_empty += not bool(context['memories'])
            kinds.update(m['memory_type'] for m in context['memories'])
            stale.update(m['memory_id'] for m in context['memories'] if m['stale'])
        results.append({'scope': pid, 'queries': 20, 'old_strict_search_empty': old_empty,
                        'new_context_empty': new_empty, 'returned_types': sorted(kinds),
                        'stale_memories_flagged': len(stale)})
    return {'read_only': True, 'raw_memory_text_exported': False, 'projects': results,
            'limitation': 'nonempty baseline is not proof of semantic relevance or user helpfulness; installed MCP remains unchanged'}


def run_report():
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'tests/fixtures/opendesign_consumer.json').read_text())
    with tempfile.TemporaryDirectory(prefix='qmemory-consumer-') as directory:
        root = Path(directory)
        project = root / 'OpenDesign-Docs'; project.mkdir()
        service = MemoryService(root / 'memory', device_id='consumer-benchmark')
        ids = set()
        for item in fixture['baseline']:
            ids.add(service.remember(project, item['type'], item['statement'], confirmed=True,
                                     source_ref='codex://synthetic-opendesign#message-1')['memory_id'])
        hits, empty, payloads, latencies = 0, 0, [], []
        events_before = service.status()['events']
        for query in fixture['queries']:
            started = time.perf_counter()
            result = service.context_pack(project, query, limit=5)
            latencies.append((time.perf_counter() - started) * 1000)
            hits += int({m['memory_id'] for m in result['memories']} == ids)
            empty += int(not result['memories'])
            payloads.append(result['estimated_payload_tokens'])
        exposure_events_added = service.status()['events'] - events_before
        bootstrap_args = {'codex_home': root / 'empty-codex', 'claude_home': root / 'empty-claude'}
        first = service.project_bootstrap(project, **bootstrap_args)
        fresh = service.project_bootstrap(project, **bootstrap_args)
        unchanged = service.project_bootstrap(project, ttl_seconds=0, **bootstrap_args)
        service.session_handoff(project, 'Changed: fixture verified. Remaining: real owner export. Next: authenticated acceptance.')
        before = service.status()['events']
        handoff = service.session_handoff(project, '  Changed: fixture verified.\nRemaining: real owner export. Next: authenticated acceptance.  ')
        duplicate_events = service.status()['events'] - before
        def p95(values):
            return round(sorted(values)[math.ceil(len(values) * .95) - 1], 2)
        metrics = {'queries': len(fixture['queries']), 'baseline_top5_set_hit_rate': hits / len(fixture['queries']),
                   'empty_recall_rate': empty / len(fixture['queries']),
                   'payload_estimated_tokens_median': statistics.median(payloads),
                   'payload_estimated_tokens_p95': p95(payloads),
                   'context_wall_ms_p95': p95(latencies), 'context_exposure_events_added': exposure_events_added,
                   'duplicate_handoff_events_added': duplicate_events,
                   'bootstrap_reasons': [r['sync_reason'] for r in (first, fresh, unchanged)],
                   'bootstrap_estimated_tokens_max': max(r['estimated_payload_tokens'] for r in (first, fresh, unchanged)),
                   'unchanged_bytes_copied': unchanged['bytes_copied']}
        gates = {'baseline_hit_95': metrics['baseline_top5_set_hit_rate'] >= .95,
                 'empty_under_5': metrics['empty_recall_rate'] < .05,
                 'payload_p95_1500': metrics['payload_estimated_tokens_p95'] <= 1500,
                 'context_p95_300ms': metrics['context_wall_ms_p95'] <= 300,
                 'handoff_noop': handoff['noop'] and duplicate_events == 0,
                 'fresh_no_sync': not fresh['sync_performed'],
                 'unchanged_no_copy': not unchanged['sync_performed'] and unchanged['bytes_copied'] == 0,
                 'no_read_events': exposure_events_added == 0}
        usage = service.usage_stats(project)
        quality = service.quality_report(project)
        return {'schema_version': 1, 'fixture': 'synthetic OpenDesign Docs consumer, 20 phrasings',
                'p0_fixture_passed': all(gates.values()), 'metrics': metrics, 'gates': gates,
                'usage': usage, 'product_quality_gates': quality['gates'],
                'product_release_ready': quality['release_ready'],
                'unverified': ['real downstream Agent round trips', 'actual model billing/token-equivalent cost',
                               'follow-up search intent', 'real user helpfulness', 'installed Agent v4 adoption'],
                'host_policy_examples_not_runtime_measurement': fixture['host_policy_cases']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    parser.add_argument('--live-readonly', action='store_true', help='Opt-in comparison of existing local OpenDesign projections; no raw text exported')
    args = parser.parse_args()
    report = run_report()
    if args.live_readonly:
        report['live_readonly_comparison'] = live_readonly_comparison()
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n', encoding='utf-8')
    print(encoded)
    raise SystemExit(0 if report['p0_fixture_passed'] else 1)
