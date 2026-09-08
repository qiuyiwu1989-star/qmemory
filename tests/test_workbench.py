"""Real schema, synthetic data only. Never reads the user's archive."""
import hashlib
from http.client import HTTPConnection
import json
import re
import sqlite3
import threading

import pytest

from qmemory.sources import SOURCE_SCHEMA
from qmemory.store import SCHEMA
from qmemory.workbench import ReadWorkspace, server


@pytest.fixture
def archive(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    with sqlite3.connect(state / 'sources.sqlite3') as db:
        db.executescript(SOURCE_SCHEMA)
        for pid in ('p1', 'p2', 'hidden'):
            db.execute('INSERT INTO projects(project_id,display_name,first_seen_at,last_seen_at,ignored) VALUES(?,?,?,?,?)',
                       (pid, pid, '2026-09-08', '2026-09-08', pid == 'hidden'))
        for cid, project, kind, duplicate, parent in (
            ('root', 'p1', 'codex', None, None),
            ('claude:copy', 'p1', 'claude-code', 'root', None),
            ('branch', 'p1', 'codex', None, 'root'),
            ('other', 'p2', 'codex', None, None),
        ):
            db.execute('''INSERT INTO conversations(conversation_id,project_id,source_kind,title,
                project_root,source_path,mirror_path,mirror_size,mirror_sha256,source_mtime_ns,
                message_count,user_message_count,agent_message_count,imported_at,updated_at,duplicate_of,parent_conversation_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (cid, project, kind, '<script>alert(1)</script>', '/private/path', '/private/source',
                 '/private/mirror', 0, 'version-1', 0, 35, 35, 0, '2026-09-08', '2026-09-08', duplicate, parent))
            for n in range(1, 36):
                content = '好的' if n in (1, 2) else f'{cid} unique message {n}'
                if n == 3:
                    content = 'api_key=sk-' + 'a' * 48
                if n == 4:
                    content = 'x' * 21000
                db.execute('INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?)',
                           (f'{cid}-{n}', cid, n, 'user', content, '2026-09-08', n, 0, f'hash-{n}'))
    with sqlite3.connect(state / 'qmemory.sqlite3') as db:
        db.executescript(SCHEMA)
        for mid, pid, ref in [('m1', 'p1', 'claude-code://copy#message-5'),
                              ('unrelated', 'p1', 'codex://not-this-task#message-1'),
                              ('private', 'p2', 'codex://root#message-1')]:
            db.execute('''INSERT INTO memories(memory_id,project_id,memory_type,holder,subject,
                statement,status,as_of,source_kind,source_ref,created_at,created_by_device)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (mid, pid, 'decision', 'agent', 'Test subject', 'Test judgment', 'proposed',
                 '2026-09-08', 'agent', ref, '2026-09-08', 'fixture'))
    return tmp_path


def test_groups_keep_all_sources_without_assuming_completion(archive):
    view = ReadWorkspace(archive)
    assert len(view.projects()['projects']) == 2
    tasks = view.tasks('p1')['tasks']
    assert len(tasks) == 1
    assert tasks[0]['source_count'] == 3
    assert tasks[0]['branch_count'] == 1
    assert tasks[0]['work_status'] == 'unverified'
    detail = view.detail('p1', 'root')
    assert {s['id'] for s in detail['sources']} == {'root', 'claude:copy', 'branch'}
    assert [m['memory_id'] for m in detail['memories']] == ['m1']
    assert detail['sharing'] == {'visibility': 'private', 'published': False}
    assert detail['story_status'] == 'not_generated'
    assert '/private/' not in json.dumps(detail)


def test_messages_preserve_occurrences_paginate_and_redact(archive):
    view = ReadWorkspace(archive)
    first = view.messages('p1', 'root', 'claude:copy')
    assert len(first['messages']) == 30
    assert first['next_offset'] == 30
    assert first['messages'][0]['text'] == first['messages'][1]['text'] == '好的'
    assert first['messages'][0]['evidence_id'] != first['messages'][1]['evidence_id']
    assert 'a' * 48 not in first['messages'][2]['text']
    assert first['messages'][3]['truncated']
    assert len(first['messages'][3]['text']) == 20000
    assert first['messages'][4]['source_ref'] == 'claude-code://copy#message-5'
    last = view.messages('p1', 'root', 'claude:copy', 30)
    assert len(last['messages']) == 5 and last['next_offset'] is None
    exact = view.messages('p1', 'root', 'claude:copy', sequence=5)
    assert exact['messages'][0]['evidence_id'] == first['messages'][4]['evidence_id']
    for source in ('root', 'branch'):
        assert source + ' unique message 5' == view.messages('p1', 'root', source, sequence=5)['messages'][0]['text']


@pytest.mark.parametrize('operation', [
    lambda v: v.tasks('hidden'), lambda v: v.detail('p2', 'root'),
    lambda v: v.messages('p1', 'root', 'other'),
    lambda v: v.messages('p1', 'root', 'root', sequence=999),
])
def test_scope_and_missing_evidence_rejected(archive, operation):
    with pytest.raises(LookupError):
        operation(ReadWorkspace(archive))


def test_reading_does_not_change_database_bytes_or_create_missing_home(archive, tmp_path):
    def hashes():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (archive / 'state').glob('*.sqlite3')}
    before = hashes()
    view = ReadWorkspace(archive)
    view.projects(); view.tasks('p1'); view.detail('p1', 'root'); view.messages('p1', 'root', 'root')
    with view.db() as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute('DELETE FROM projects')
    assert hashes() == before
    missing = tmp_path / 'absent-home'
    with pytest.raises(FileNotFoundError):
        ReadWorkspace(missing).projects()
    assert not missing.exists()


def test_missing_memory_index_is_not_reported_as_empty_memory(archive):
    (archive / 'state' / 'qmemory.sqlite3').unlink()
    detail = ReadWorkspace(archive).detail('p1', 'root')
    assert not detail['memory_index_available']


def test_project_label_falls_back_without_exposing_full_path(archive):
    with sqlite3.connect(archive / 'state' / 'sources.sqlite3') as db:
        db.execute("UPDATE projects SET display_name=NULL,canonical_root='/private/folder/qmemory' WHERE project_id='p1'")
    project = next(p for p in ReadWorkspace(archive).projects()['projects'] if p['id'] == 'p1')
    assert project['name'] == 'qmemory'
    assert project['identity_kind'] == 'directory'
    assert '/private/' not in json.dumps(project)


@pytest.fixture
def http(archive):
    app = server(archive, 0)
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    def request(path, headers=None, method='GET'):
        c = HTTPConnection('127.0.0.1', app.server_port, timeout=3)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        result = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return result
    try:
        yield request
    finally:
        app.shutdown(); app.server_close(); thread.join(3)


def auth(http):
    status, headers, body = http('/qmemory.html')
    assert status == 200
    assert headers['Cache-Control'] == 'no-store'
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    token = re.search(rb'name="qmemory-session" content="([^"]+)"', body).group(1).decode()
    return {'X-QMemory-Session': token}


def test_http_security_and_readonly_routes(http):
    headers = auth(http)
    assert http('/api/projects')[0] == 401
    assert http('/api/projects', {'X-QMemory-Session': 'é'})[0] == 401
    assert http('/api/projects', headers)[0] == 200
    assert http('/qmemory.html', {'Host': 'evil.example'})[0] == 403
    assert http('/qmemory.html', {'Origin': 'https://evil.example'})[0] == 403
    assert http('/qmemory.html', {'Sec-Fetch-Site': 'cross-site'})[0] == 403
    assert http('/api/projects', headers, 'POST')[0] == 405
    assert http('/../state/sources.sqlite3', headers)[0] == 404
    assert http('/workbench.js')[0] == http('/workbench.css')[0] == 200


def test_http_validation_and_scope(http):
    headers = auth(http)
    base = '/api/messages?project=p1&task=root&source=root'
    assert http(base, headers)[0] == 200
    for extra in ('&offset=-1', '&offset=1&offset=2', '&offset=999999999999999999999', '&sequence=0'):
        assert http(base + extra, headers)[0] == 400
    assert http('/api/task?project=p1&task=other', headers)[0] == 404
    assert http('/api/tasks', headers)[0] == 400
    assert http(base + '&sequence=999', headers)[0] == 404


def test_http_unavailable_index_does_not_initialize(archive, http):
    headers = auth(http)
    path = archive / 'state' / 'sources.sqlite3'
    path.unlink()
    assert http('/api/projects', headers)[0] == 503
    assert not path.exists()
