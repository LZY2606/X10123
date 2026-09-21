"""本地 HTTP 服务：纯标准库，不接外网、不依赖独立数据库进程。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .compat import check_compatibility
from .store import HeadConflict, MigrationCycle, Store

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _resolve_consumers(store, refs):
    resolved = []
    for ref in refs or []:
        if "fields" in ref:
            resolved.append({
                "name": ref.get("name", "inline"),
                "version": ref.get("version", "-"),
                "fields": ref["fields"],
            })
        else:
            c = store.get_consumer(ref["name"], ref["version"])
            if c is None:
                raise ValueError("consumer %s@%s 不存在" % (ref["name"], ref["version"]))
            resolved.append(c)
    return resolved


def make_server(host, port, db_path):
    store = Store(db_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "schemagate/0.1"

        def log_message(self, *args):
            pass

        # ---- helpers ----
        def _send(self, code, obj, headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _fail(self, code, exc):
            self._send(code, {"error": str(exc), "kind": type(exc).__name__})

        # ---- GET ----
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._index()
            elif path == "/api/state":
                self._send(200, {
                    "schemas": store.list_schemas(),
                    "consumers": store.list_consumers(),
                    "migrations": store.list_migrations(),
                    "heads": store.list_heads(),
                    "decisions": store.list_decisions(),
                })
            elif path == "/api/export":
                self._send(200, store.export_all(), {
                    "Content-Disposition": "attachment; filename=schemagate-export.json"
                })
            elif path.startswith("/api/decisions/"):
                parts = path.strip("/").split("/")
                try:
                    decision_id = int(parts[2])
                except (IndexError, ValueError):
                    return self._send(404, {"error": "not found"})
                if len(parts) == 4 and parts[3] == "replay":
                    result = store.replay(decision_id)
                    return self._send(200, result) if result else self._send(
                        404, {"error": "decision 不存在"})
                d = store.get_decision(decision_id)
                self._send(200, d) if d else self._send(404, {"error": "decision 不存在"})
            else:
                self._send(404, {"error": "not found"})

        def _index(self):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---- POST ----
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = self._body()
            except Exception as exc:
                return self._fail(400, exc)
            try:
                if path == "/api/schemas":
                    return self._send(200, store.register_schema(
                        body["name"], body["version"], body["content"]))
                if path == "/api/consumers":
                    return self._send(200, store.register_consumer(
                        body["name"], body["version"], body.get("fields", [])))
                if path == "/api/migrations":
                    return self._send(200, store.add_migration(
                        body["name"], body.get("path", "/"),
                        body["from"], body["to"]))
                if path == "/api/check":
                    return self._check(body)
                if path == "/api/publish":
                    return self._publish(body)
                if path == "/api/import":
                    return self._send(200, store.import_all(body))
                self._send(404, {"error": "not found"})
            except HeadConflict as exc:
                self._send(409, {"error": str(exc), "kind": "HeadConflict",
                                 "current_head": exc.current_head})
            except (ValueError, MigrationCycle, KeyError) as exc:
                self._fail(400, exc)

        def _check(self, body):
            consumers = _resolve_consumers(store, body.get("consumers"))
            renames = dict(body.get("renames") or {})
            if body.get("name"):
                for p, m in store.rename_map(body["name"]).items():
                    renames.setdefault(p, {}).update(m)
            result = check_compatibility(
                body["old"], body["new"], body.get("direction", "full"),
                renames=renames, consumers=consumers)
            self._send(200, result)

        def _publish(self, body):
            consumers = _resolve_consumers(store, body.get("consumers"))
            decision = store.publish(
                name=body["name"],
                base_version=body.get("base_version"),
                version=body["version"],
                content=body["content"],
                direction=body.get("direction", "full"),
                consumers=consumers,
                idempotency_key=body["idempotency_key"],
            )
            self._send(200, decision)

    return ThreadingHTTPServer((host, port), Handler)


def serve(host, port, db_path):
    httpd = make_server(host, port, db_path)
    print("兼容性闸门 listening on http://%s:%d (db=%s)" % (host, port, db_path))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
