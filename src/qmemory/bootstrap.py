"""TTL fast path, metadata fingerprints and nonblocking synchronization for bootstrap."""
from __future__ import annotations

import fcntl
import hashlib
import json
import time

from .sources import CodexConversationArchive
from .usage import Usage


def source_key(codex_home=None, claude_home=None):
    return hashlib.sha256(json.dumps([str(CodexConversationArchive.codex_home(codex_home)),
                                    str(CodexConversationArchive.claude_home(claude_home))]).encode()).hexdigest()


def fingerprint(codex_home=None, claude_home=None):
    values = []
    for kind, paths in CodexConversationArchive.discover_all(codex_home, claude_home).items():
        for path in paths:
            stat = path.stat()
            values.append((kind, str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest(), len(values)


def sync_if_needed(service, *, codex_home=None, claude_home=None, ttl_seconds=300, force=False):
    ttl = max(0, min(int(ttl_seconds), 86400))
    key = source_key(codex_home, claude_home)
    usage = Usage(service.settings)
    result = {'sync_performed': False, 'sync_reason': 'unavailable', 'sync_age': None,
              'last_sync_at': None, 'new_conversations': 0, 'updated_conversations': 0,
              'imported': 0, 'bytes_copied': 0, 'files_scanned': 0, 'sync_failures': 0}
    # Nonblocking: another client archiving must not hold up local development.
    try:
        with (service.settings.state_dir / 'bootstrap-sync.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result['sync_reason'] = 'busy'
                return result
            now = time.time()
            with usage.connect() as db:
                row = db.execute('SELECT * FROM freshness WHERE source_key=?', (key,)).fetchone()
            if row:
                age = now - row['last_sync_at']
                result.update(sync_age=round(max(0, age), 3), last_sync_at=row['last_sync_at'])
                if not force and 0 <= now - row['checked_at'] < ttl and service.settings.source_database_path.exists():
                    result['sync_reason'] = 'fresh'
                    return result
            current, scanned = fingerprint(codex_home, claude_home)
            result['files_scanned'] = scanned
            if not force and row and row['fingerprint'] == current and service.settings.source_database_path.exists():
                with usage.connect() as db:
                    db.execute('UPDATE freshness SET checked_at=? WHERE source_key=?', (now, key))
                result['sync_reason'] = 'unchanged'
                return result
            archive = CodexConversationArchive(service.settings)
            try:
                before = archive.connection.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
                result.update(sync_performed=True, sync_reason='forced' if force else 'changed_or_uninitialized')
                sync = archive.sync_all(codex_home, claude_home)
                after = archive.connection.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
            finally:
                archive.close()
            result.update(imported=int(sync['imported']), bytes_copied=int(sync['bytes_copied']),
                          sync_failures=int(sync['failures']), new_conversations=max(0, after - before),
                          updated_conversations=max(0, int(sync['imported']) - max(0, after - before)))
            if sync['failures']:
                result['sync_reason'] = 'partial_failure'
            else:
                done = time.time()
                # Save pre-sync fingerprint, so a concurrent source append is caught next check.
                with usage.connect() as db:
                    db.execute('INSERT OR REPLACE INTO freshness VALUES(?,?,?,?)', (key, current, done, done))
                result.update(last_sync_at=done, sync_age=0)
            return result
    except Exception as exc:
        # Deliberately omit exception text (paths/credentials may be present).
        result.update(sync_reason='unavailable', sync_error=type(exc).__name__)
        return result
