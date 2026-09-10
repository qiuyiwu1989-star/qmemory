"""Local, text-free operational metrics. Never part of the memory event ledger."""
from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager


class Usage:
    def __init__(self, settings):
        self.path = settings.state_dir / 'usage.sqlite3'

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.05)
        try:
            db.row_factory = sqlite3.Row
            db.executescript('''
                CREATE TABLE IF NOT EXISTS totals (
                  project_id TEXT, operation TEXT, calls INTEGER DEFAULT 0,
                  empty INTEGER DEFAULT 0, noop INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
                  returned INTEGER DEFAULT 0, bytes_copied INTEGER DEFAULT 0,
                  files_scanned INTEGER DEFAULT 0, PRIMARY KEY(project_id,operation));
                CREATE TABLE IF NOT EXISTS samples (
                  id INTEGER PRIMARY KEY, project_id TEXT, operation TEXT,
                  duration_ms REAL, context_ms REAL, tokens INTEGER);
                CREATE TABLE IF NOT EXISTS freshness (source_key TEXT PRIMARY KEY,
                  fingerprint TEXT, checked_at REAL, last_sync_at REAL);
            ''')
            yield db
            db.commit()
        finally:
            db.close()

    def record(self, project_id, operation, *, returned=0, empty=0, noop=0, errors=0,
               duration_ms=0, context_ms=0, tokens=0, bytes_copied=0, files_scanned=0):
        try:
            with self.connect() as db:
                db.execute('''INSERT INTO totals VALUES(?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(project_id,operation) DO UPDATE SET calls=calls+1,
                  empty=empty+excluded.empty,noop=noop+excluded.noop,errors=errors+excluded.errors,
                  returned=returned+excluded.returned,bytes_copied=bytes_copied+excluded.bytes_copied,
                  files_scanned=files_scanned+excluded.files_scanned''',
                  (project_id, operation, 1, empty, noop, errors, returned, bytes_copied, files_scanned))
                db.execute('INSERT INTO samples(project_id,operation,duration_ms,context_ms,tokens) VALUES(?,?,?,?,?)',
                           (project_id, operation, duration_ms, context_ms, tokens))
                db.execute('DELETE FROM samples WHERE id NOT IN (SELECT id FROM samples ORDER BY id DESC LIMIT 1000)')
            return True
        except (OSError, sqlite3.Error):
            return False

    def stats(self, project_id=None):
        try:
            with self.connect() as db:
                where, params = (' WHERE project_id=?', (project_id,)) if project_id else ('', ())
                rows = [dict(r) for r in db.execute('SELECT * FROM totals' + where, params)]
                samples = [dict(r) for r in db.execute('SELECT * FROM samples' + where, params)]
            ops = {}
            for r in rows:
                op = ops.setdefault(r['operation'], {k: 0 for k in r if k not in ('operation', 'project_id')})
                for key in op:
                    op[key] += r[key]
            contexts = [v for k, v in ops.items() if k in ('project_context', 'project_bootstrap')]
            calls = sum(v['calls'] for v in contexts)
            handoff = ops.get('session_handoff', {})
            bootstrap = ops.get('project_bootstrap', {})
            def ratio(a, b):
                return round(a / b, 4) if b else None
            def percentile(key, operations=('project_context', 'project_bootstrap')):
                values = sorted(r[key] for r in samples if r['operation'] in operations and r[key] is not None)
                return round(values[max(0, math.ceil(len(values) * .95) - 1)], 2) if values else None
            return {'available': True, 'operations': ops,
                    'mcp_tool_calls': sum(v['calls'] for k, v in ops.items() if k.startswith('tool:')) if project_id is None else None,
                    'context_calls': calls, 'context_impressions': sum(v['returned'] for v in contexts),
                    'empty_recall_rate': ratio(sum(v['empty'] for v in contexts), calls),
                    'handoff_noop_rate': ratio(handoff.get('noop', 0), handoff.get('calls', 0)),
                    'sync_noop_rate': ratio(bootstrap.get('noop', 0), bootstrap.get('calls', 0)),
                    'payload_tokens_p95': percentile('tokens'), 'context_ms_p95': percentile('context_ms'),
                    'bootstrap_ms_p95_including_sync': percentile('duration_ms', ('project_bootstrap',)),
                    'followup_search_rate': None, 'model_round_trips': None, 'token_equivalent_cost': None,
                    'measurement_scope': 'local service calls; model round trips and follow-up intent require host telemetry',
                    'sample_window': 'latest 1000 local operations; totals since instrumentation'}
        except (OSError, sqlite3.Error):
            return {'available': False, 'context_impressions': 0}
