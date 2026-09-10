from pathlib import Path
from datetime import datetime, timedelta, timezone
import concurrent.futures
import json
import subprocess
import time

import pytest

from qmemory.service import MemoryService


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / 'opendesign-docs'
    project.mkdir()
    service = MemoryService(tmp_path / 'home', device_id='consumer-test')
    return service, project


def test_confirmed_baseline_survives_natural_query(setup):
    service, project = setup
    ids = set()
    for kind in ('decision', 'constraint', 'incident', 'status', 'handoff'):
        item = service.remember(project, kind, 'OpenDesign 已验证的产品边界与阶段验收',
                                subject=kind, confirmed=True, source_ref='codex://fixture#message-1')
        ids.add(item['memory_id'])
    service.remember(project, 'decision', '尚未批准的建议')
    assert service.search(project, 'OpenDesign')
    assert not service.search(project, '隔天 继续 导出 交付 重构')
    for query in ('隔天 继续 导出 交付 重构', 'resume publishing tomorrow', '帮我接着做'):
        pack = service.context_pack(project, query, limit=5)
        assert {m['memory_id'] for m in pack['memories']} == ids
        assert all(m['why_selected'] and m['status'] == 'active' for m in pack['memories'])
        assert all('stale' in m and 'commit_sha' in m for m in pack['memories'])


def test_context_does_not_append_exposure_events(setup):
    service, project = setup
    service.remember(project, 'constraint', 'Keep evidence', confirmed=True)
    before = service.status()['events']
    service.context_pack(project, 'Keep')
    assert service.status()['events'] == before
    assert service.retrieval_stats(project)['impressions'] == 1


def test_handoff_normalized_duplicate_is_noop(setup):
    service, project = setup
    first = service.session_handoff(project, 'Tests pass;\nnext: export', source_ref='codex://a#message-1')
    before = service.status()['events']
    second = service.session_handoff(project, '  Tests pass;  \r\nnext: export  ', source_ref='codex://b#message-2')
    assert second['noop'] is True
    assert second['memory_id'] == first['memory_id']
    assert second['source_ref'] == first['source_ref']
    assert service.status()['events'] == before


def test_bootstrap_fresh_and_unchanged_skip_archive(setup, tmp_path, monkeypatch):
    service, project = setup
    codex, claude = tmp_path / 'codex', tmp_path / 'claude'
    codex.mkdir(); claude.mkdir()
    first = service.project_bootstrap(project, codex_home=codex, claude_home=claude)
    assert first['sync_performed']
    from qmemory.sources import CodexConversationArchive
    monkeypatch.setattr(CodexConversationArchive, 'sync_all', lambda *a, **kw: pytest.fail('unnecessary sync'))
    second = service.project_bootstrap(project, codex_home=codex, claude_home=claude)
    assert not second['sync_performed'] and second['sync_reason'] == 'fresh'
    third = service.project_bootstrap(project, ttl_seconds=0, codex_home=codex, claude_home=claude)
    assert not third['sync_performed'] and third['sync_reason'] == 'unchanged'
    assert third['bytes_copied'] == 0
    assert third['estimated_payload_tokens'] <= 1500


def test_twenty_opendesign_queries_and_project_isolation(setup, tmp_path):
    service, project = setup
    fixture = json.loads((Path(__file__).parent / 'fixtures/opendesign_consumer.json').read_text())
    old = service.remember(project, 'status', 'Obsolete status', confirmed=True,
                           as_of='2020-01-01T00:00:00Z')
    expected = set()
    for item in fixture['baseline']:
        m = service.remember(project, item['type'], item['statement'], confirmed=True,
                             source_ref='codex://synthetic-opendesign#message-1')
        expected.add(m['memory_id'])
    service.remember(project, 'fact', 'export unrelated historical noise', confirmed=True)
    unconfirmed = service.remember(project, 'constraint', 'export approved? no')
    other = tmp_path / 'other'; other.mkdir()
    private = service.remember(other, 'constraint', 'other project private', confirmed=True)
    for query in fixture['queries']:
        pack = service.context_pack(project, query, 5)
        ids = {m['memory_id'] for m in pack['memories']}
        assert ids == expected
        assert not ids & {old['memory_id'], unconfirmed['memory_id'], private['memory_id']}
        assert pack['estimated_payload_tokens'] <= 1500
    stats = service.usage_stats(project)
    assert stats['context_calls'] == 20 and stats['empty_recall_rate'] == 0


def test_context_bounds_stale_revision_and_query_supplement(setup):
    service, project = setup
    old = service.remember(project, 'constraint', '旧约束' * 150, confirmed=True,
                           as_of='2020-01-01T00:00:00Z')
    extra = service.remember(project, 'environment', 'Python interpreter export', confirmed=True)
    pack = service.context_pack(project, 'Python unrelated missing', 2)
    assert len(pack['memories']) == 2
    assert pack['memories'][0]['stale']
    assert pack['memories'][0]['statement_truncated']
    assert pack['memories'][1]['why_selected'] == 'query_relevance'
    assert pack['revision'] == service.context_pack(project, 'Python unrelated missing', 2)['revision']
    service.supersede(project, old['memory_id'], 'Updated constraint')
    assert pack['revision'] != service.context_pack(project, 'Python unrelated missing', 2)['revision']


def test_large_baseline_budget_and_source_refs_not_silently_cut(setup):
    service, project = setup
    for kind in ('decision', 'constraint', 'incident', 'status', 'handoff'):
        service.remember(project, kind, '很长的可追溯项目判断' * 500, subject='主体' * 100,
                         confirmed=True, source_ref='codex://' + 'x' * 500 + '#message-1')
    pack = service.context_pack(project, '自然语言 ' * 200, 5)
    assert len(pack['memories']) == 5
    assert pack['estimated_payload_tokens'] <= 1500
    assert all(m['source_ref'] is None and m['source_ref_omitted'] for m in pack['memories'])


def test_handoff_parallel_noop_and_changed_state(setup):
    service, project = setup
    def write(_):
        return service.session_handoff(project, 'Changed tests; remaining package; next build')
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(write, range(4)))
    assert len({r['memory_id'] for r in results}) == 1
    assert sum(r['noop'] for r in results) == 3
    assert service.status()['events'] == 1
    changed = service.session_handoff(project, 'Changed package; remaining install; next install')
    assert not changed['noop'] and service.status()['events'] == 2
    assert service.usage_stats(project)['handoff_noop_rate'] == .6
    with pytest.raises(ValueError):
        service.session_handoff(project, 'x' * 4001)


def test_bootstrap_detects_new_changed_deleted_sources_and_restart(setup, tmp_path, monkeypatch):
    service, project = setup
    codex, claude = tmp_path / 'codex', tmp_path / 'claude'
    sessions = codex / 'sessions'; sessions.mkdir(parents=True); claude.mkdir()
    service.project_bootstrap(project, codex_home=codex, claude_home=claude)
    path = sessions / 'rollout-fixture.jsonl'
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'fixture', 'cwd': str(project)}}) + '\n')
    new = service.project_bootstrap(project, ttl_seconds=0, codex_home=codex, claude_home=claude)
    assert new['sync_performed'] and new['new_conversations'] == 1
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'A new occurrence'}}) + '\n')
    updated = service.project_bootstrap(project, ttl_seconds=0, codex_home=codex, claude_home=claude)
    assert updated['updated_conversations'] == 1 and updated['new_conversations'] == 0
    restarted = MemoryService(service.settings.home)
    assert restarted.project_bootstrap(project, codex_home=codex, claude_home=claude)['sync_reason'] == 'fresh'
    path.unlink()
    deleted = restarted.project_bootstrap(project, ttl_seconds=0, codex_home=codex, claude_home=claude)
    assert deleted['sync_performed']  # Recheck, never delete the preserved evidence.
    assert len(service.conversations(project)) == 1


def test_explicit_sync_seeds_freshness(setup, tmp_path, monkeypatch):
    service, project = setup
    codex, claude = tmp_path / 'codex', tmp_path / 'claude'
    service.conversation_sources_sync(codex, claude)
    from qmemory import bootstrap
    monkeypatch.setattr(bootstrap, 'fingerprint', lambda *a: pytest.fail('fresh cache should not scan'))
    assert service.project_bootstrap(project, codex_home=codex, claude_home=claude)['sync_reason'] == 'fresh'


def test_bootstrap_fail_open_and_busy(setup, tmp_path, monkeypatch):
    service, project = setup
    service.remember(project, 'constraint', 'Keep a known baseline', confirmed=True)
    from qmemory import bootstrap
    monkeypatch.setattr(bootstrap, 'fingerprint', lambda *a: (_ for _ in ()).throw(OSError('secret-path')))
    result = service.project_bootstrap(project)
    assert result['fail_open'] and result['memories']
    assert 'secret-path' not in json.dumps(result)
    import fcntl
    with (service.settings.state_dir / 'bootstrap-sync.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = service.project_bootstrap(project)
        assert result['sync_reason'] == 'busy' and result['memories']
    monkeypatch.setattr(service.projection, 'connect', lambda: (_ for _ in ()).throw(OSError('secret-path')))
    result = service.context_pack(project)
    assert not result['available'] and result['memories'] == []


def test_worktree_subdirectory_symlink_share_context(setup, tmp_path):
    service, project = setup
    def git(*args):
        subprocess.run(['git', '-C', str(project), *args], check=True, capture_output=True)
    git('init'); git('config', 'user.email', 'test@example.invalid'); git('config','user.name','Fixture')
    git('commit', '--allow-empty', '-m', 'fixture')
    git('remote', 'add', 'origin', 'git@github.com:example/OpenDesign.git')
    worktree = tmp_path / 'worktree'; git('worktree', 'add', '-b', 'test-branch', str(worktree))
    sub = project / 'src'; sub.mkdir()
    link = tmp_path / 'alias'; link.symlink_to(project, target_is_directory=True)
    memory = service.remember(project, 'constraint', 'Shared canonical identity', confirmed=True)
    for path in (worktree, sub, link):
        pack = service.context_pack(path, 'resume tomorrow')
        assert pack['project']['project_id'] == 'git:github.com/example/opendesign'
        assert pack['memories'][0]['memory_id'] == memory['memory_id']


def test_mcp_bootstrap_and_noop_contract(setup, tmp_path, monkeypatch):
    pytest.importorskip('mcp.server.fastmcp')
    import asyncio
    from qmemory.mcp_server import create_server
    service, project = setup
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude'))
    server = create_server(service.settings.home)
    def call(name, **args):
        result = asyncio.run(server.call_tool(name, args))
        structured = result[1] if isinstance(result, tuple) else result
        return structured['result'] if set(structured) == {'result'} else structured
    one = call('project_bootstrap', project_path=str(project), query='跨日续作')
    two = call('project_bootstrap', project_path=str(project))
    assert one['sync_performed'] and not two['sync_performed']
    call('session_handoff', project_path=str(project), statement='Verified; next build')
    assert call('session_handoff', project_path=str(project), statement='Verified; next build')['noop']
    usage = call('usage_stats')
    assert usage['mcp_tool_calls'] == 4
    assert usage['operations']['tool:project_bootstrap']['calls'] == 2


def test_failed_sync_does_not_mark_fresh(setup, tmp_path, monkeypatch):
    service, project = setup
    from qmemory.sources import CodexConversationArchive
    monkeypatch.setattr(CodexConversationArchive, 'sync_all', lambda *a, **kw: {'imported':0,'failures':1,'bytes_copied':0})
    options = {'codex_home':tmp_path/'codex', 'claude_home':tmp_path/'claude'}
    assert service.project_bootstrap(project, **options)['sync_reason'] == 'partial_failure'
    again = service.project_bootstrap(project, **options)
    assert again['sync_performed'] and again['last_sync_at'] is None


def test_usage_keeps_prompts_and_secrets_out(setup):
    service, project = setup
    secret = 'sk-' + 'z' * 48
    result = service.context_pack(project, 'sensitive task phrase ' + secret)
    assert secret not in json.dumps(result)
    raw = (service.settings.state_dir / 'usage.sqlite3').read_bytes()
    assert b'sensitive task phrase' not in raw and secret.encode() not in raw
