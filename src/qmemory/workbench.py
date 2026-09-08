"""Loopback-only read workspace. No MemoryService constructors, migrations or sync side effects."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import default_home
from .redaction import redact_text
from .sources import CodexConversationArchive


def clean(value):
    return redact_text(str(value or ""))[0]


class ReadWorkspace:
    def __init__(self, home: Path):
        self.home = home.resolve()

    @contextmanager
    def db(self, name="sources.sqlite3"):
        path = self.home / "state" / name
        if not path.is_file():
            raise FileNotFoundError("index_unavailable")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    def projects(self):
        with self.db() as db:
            rows = db.execute("""SELECT p.project_id,p.display_name,p.canonical_root,p.remote,COUNT(c.conversation_id) AS source_count,
                MAX(c.imported_at) AS indexed_at FROM projects p LEFT JOIN conversations c
                ON c.project_id=p.project_id WHERE p.ignored=0 GROUP BY p.project_id
                ORDER BY indexed_at DESC""").fetchall()
        return {"projects": [{"id": r["project_id"], "name": clean(r["display_name"] or
                 (Path(r["canonical_root"]).name if r["canonical_root"] else "") or
                 (str(r["remote"]).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") if r["remote"] else "") or "未命名项目"),
                 "identity_kind": "repository" if r["project_id"].startswith("git:") else "directory",
                 "source_count": r["source_count"], "indexed_at": r["indexed_at"]} for r in rows],
                "mode": "local_readonly", "cloud_connected": False}

    @staticmethod
    def groups(db, project):
        if not db.execute("SELECT 1 FROM projects WHERE project_id=? AND ignored=0", (project,)).fetchone():
            raise LookupError("project_not_found")
        rows = {r["conversation_id"]: dict(r) for r in db.execute(
            "SELECT conversation_id,source_kind,title,updated_at,imported_at,duplicate_of,parent_conversation_id,mirror_sha256,message_count "
            "FROM conversations WHERE project_id=?", (project,))}
        groups = {}
        for cid, row in rows.items():
            root = CodexConversationArchive._task_root_id(cid, rows)
            groups.setdefault(root, []).append(row)
        return groups

    @staticmethod
    def summary(task_id, members):
        root = next((r for r in members if r["conversation_id"] == task_id), members[0])
        return {"id": task_id, "title": clean(root["title"]), "source_count": len(members),
                "agents": sorted({r["source_kind"] for r in members}),
                "branch_count": sum(bool(r["parent_conversation_id"]) for r in members),
                "updated_at": max((r["updated_at"] or "" for r in members), default=""),
                "work_status": "unverified"}

    def tasks(self, project):
        with self.db() as db:
            groups = self.groups(db, project)
        values = [self.summary(k, v) for k, v in groups.items()]
        values.sort(key=lambda t: t["updated_at"], reverse=True)
        return {"tasks": values, "total": len(values), "relationship_basis": "existing_archive_links"}

    def detail(self, project, task):
        with self.db() as db:
            members = self.groups(db, project).get(task)
            if members is None:
                raise LookupError("task_not_found")
            sources = []
            for row in members:
                cid = row["conversation_id"]
                count = db.execute("SELECT COUNT(*) FROM conversation_messages WHERE conversation_id=?", (cid,)).fetchone()[0]
                sources.append({"id": cid, "agent": row["source_kind"], "version": row["mirror_sha256"],
                    "relation": "branch" if row["parent_conversation_id"] else "copy" if row["duplicate_of"] else "root",
                    "title": clean(row["title"]), "message_count": count, "indexed_at": row["imported_at"]})
        memories, available = [], True
        try:
            with self.db("qmemory.sqlite3") as db:
                refs = {"%s://%s#" % (r["source_kind"], r["conversation_id"].removeprefix("claude:")) for r in members}
                rows = db.execute("SELECT memory_id,statement,subject,status,source_ref,as_of,superseded_by FROM memories WHERE project_id=?", (project,))
                for r in rows:
                    if any(str(r["source_ref"] or "").startswith(ref) for ref in refs):
                        memories.append({k: clean(r[k]) for k in r.keys()})
        except FileNotFoundError:
            available = False
        return {**self.summary(task, members), "sources": sources, "memories": memories,
                "memory_index_available": available, "story_status": "not_generated",
                "scope": {"space_id": "local-private", "project_id": project, "task_id": task},
                "sharing": {"visibility": "private", "published": False}}

    def messages(self, project, task, source, offset=0, sequence=None):
        with self.db() as db:
            members = self.groups(db, project).get(task, [])
            row = next((r for r in members if r["conversation_id"] == source), None)
            if row is None:
                raise LookupError("source_not_in_task")
            total = db.execute("SELECT COUNT(*) FROM conversation_messages WHERE conversation_id=?", (source,)).fetchone()[0]
            if sequence is not None:
                if not db.execute("SELECT 1 FROM conversation_messages WHERE conversation_id=? AND sequence=?", (source, sequence)).fetchone():
                    raise LookupError("evidence_not_found")
                offset = db.execute("SELECT COUNT(*) FROM conversation_messages WHERE conversation_id=? AND sequence<?", (source, sequence)).fetchone()[0]
            rows = db.execute("SELECT sequence,role,text,occurred_at FROM conversation_messages WHERE conversation_id=? ORDER BY sequence LIMIT 30 OFFSET ?", (source, offset)).fetchall()
            items = []
            for m in rows:
                text = clean(m["text"])
                identity = json.dumps([project, source, row["mirror_sha256"], m["sequence"]])
                items.append({"evidence_id": hashlib.sha256(identity.encode()).hexdigest(),
                    "source_ref": "%s://%s#message-%d" % (row["source_kind"], source.removeprefix("claude:"), m["sequence"]),
                    "sequence": m["sequence"], "role": m["role"], "text": text[:20000],
                    "truncated": len(text) > 20000, "occurred_at": m["occurred_at"]})
        return {"messages": items, "offset": offset, "total": total, "next_offset": offset + len(items) if offset + len(items) < total else None,
                "version": row["mirror_sha256"], "view": "redacted_current_index"}


def server(home: Path, port=8790):
    workspace, token = ReadWorkspace(home), secrets.token_urlsafe(32)
    static = Path(__file__).parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # No private project IDs, source refs, credentials or text in access logs.

        def send(self, value, status=200, content_type="application/json"):
            data = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            origin = "http://127.0.0.1:%d" % self.server.server_port
            if self.headers.get("Host") != origin.removeprefix("http://") or self.headers.get("Origin", origin) != origin or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.send({"error": "origin_rejected"}, 403)
            url = urlparse(self.path)
            if url.path in ("/", "/qmemory.html"):
                return self.send((static / "workbench.html").read_bytes().replace(b"__SESSION__", token.encode()), content_type="text/html")
            if url.path in ("/workbench.js", "/workbench.css"):
                return self.send((static / url.path[1:]).read_bytes(), content_type="application/javascript" if url.path.endswith(".js") else "text/css")
            if not hmac.compare_digest(self.headers.get("X-QMemory-Session", "").encode("utf-8"), token.encode("utf-8")):
                return self.send({"error": "session_required"}, 401)
            try:
                args = parse_qs(url.query, max_num_fields=8)
                def param(key):
                    values = args.get(key)
                    if not values or len(values) != 1 or len(values[0]) > 1024:
                        raise ValueError("invalid_parameter")
                    return values[0]
                if url.path == "/api/projects":
                    data = workspace.projects()
                elif url.path == "/api/tasks":
                    data = workspace.tasks(param("project"))
                elif url.path == "/api/task":
                    data = workspace.detail(param("project"), param("task"))
                elif url.path == "/api/messages":
                    offset = int(param("offset")) if "offset" in args else 0
                    sequence = int(param("sequence")) if "sequence" in args else None
                    if not 0 <= offset < 2**63 or (sequence is not None and not 1 <= sequence < 2**63):
                        raise ValueError("invalid_parameter")
                    data = workspace.messages(param("project"), param("task"), param("source"), offset, sequence)
                else:
                    return self.send({"error": "not_found"}, 404)
                self.send(data)
            except (OSError, sqlite3.Error):
                self.send({"error": "index_unavailable", "hint": "请先用 QMemory 同步生成索引；本工作台不会迁移或重建。"}, 503)
            except LookupError:
                self.send({"error": "not_found_or_out_of_scope"}, 404)
            except ValueError:
                self.send({"error": "invalid_parameter"}, 400)

        def do_POST(self):
            self.send({"error": "readonly"}, 405)
        do_PUT = do_PATCH = do_DELETE = do_POST

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    app = server(args.home, args.port)
    print("QMemory local read-only workspace: http://127.0.0.1:%d/qmemory.html" % app.server_port, flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server_close()


if __name__ == "__main__":
    main()
