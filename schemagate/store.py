"""SQLite 持久化：版本、消费者声明、迁移映射、发布决策与 head 指针。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .compat import check_compatibility
from .schema_model import fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS schemas(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  content TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS consumers(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  fields TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS migrations(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  from_field TEXT NOT NULL,
  to_field TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  base_version TEXT,
  candidate_version TEXT NOT NULL,
  candidate_fingerprint TEXT NOT NULL,
  direction TEXT NOT NULL,
  consumers TEXT NOT NULL,
  renames TEXT NOT NULL,
  verdict TEXT NOT NULL,
  issues TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heads(
  name TEXT PRIMARY KEY,
  version TEXT
);
"""

TABLES = ("schemas", "consumers", "migrations", "decisions", "heads")


class HeadConflict(Exception):
    def __init__(self, current_head):
        super().__init__("head 已移动到 %r，发布基于过期版本" % (current_head,))
        self.current_head = current_head


class MigrationCycle(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _creates_cycle(edges, new_edge):
    adj = {}
    for frm, to in list(edges) + [new_edge]:
        adj.setdefault(frm, []).append(to)
    target = new_edge[0]
    stack = [new_edge[1]]
    seen = set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, []))
    return False


class Store:
    def __init__(self, path="schemagate.db"):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- schema 版本 ----
    def register_schema(self, name, version, content):
        fp = fingerprint(content)
        with self._lock:
            try:
                self.db.execute(
                    "INSERT INTO schemas(name, version, content, fingerprint, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (name, version, json.dumps(content, ensure_ascii=False), fp, _now()),
                )
                self.db.commit()
            except sqlite3.IntegrityError:
                raise ValueError("schema %s@%s 已存在" % (name, version))
            # 首个登记的版本自动成为 head
            self.db.execute(
                "INSERT INTO heads(name, version) VALUES(?,?)"
                " ON CONFLICT(name) DO NOTHING",
                (name, version),
            )
            self.db.commit()
        return {"name": name, "version": version, "fingerprint": fp}

    def get_schema(self, name, version):
        row = self.db.execute(
            "SELECT * FROM schemas WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if not row:
            return None
        return self._schema_row(row)

    def list_schemas(self):
        rows = self.db.execute("SELECT * FROM schemas ORDER BY id").fetchall()
        return [self._schema_row(r) for r in rows]

    @staticmethod
    def _schema_row(row):
        return {
            "name": row["name"],
            "version": row["version"],
            "content": json.loads(row["content"]),
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
        }

    # ---- 消费者声明（带版本，历史决策引用当时的声明） ----
    def register_consumer(self, name, version, fields):
        with self._lock:
            try:
                self.db.execute(
                    "INSERT INTO consumers(name, version, fields, created_at) VALUES(?,?,?,?)",
                    (name, version, json.dumps(fields, ensure_ascii=False), _now()),
                )
                self.db.commit()
            except sqlite3.IntegrityError:
                raise ValueError("consumer %s@%s 已存在" % (name, version))
        return {"name": name, "version": version, "fields": fields}

    def get_consumer(self, name, version):
        row = self.db.execute(
            "SELECT * FROM consumers WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if not row:
            return None
        return {"name": row["name"], "version": row["version"],
                "fields": json.loads(row["fields"])}

    def list_consumers(self):
        rows = self.db.execute("SELECT * FROM consumers ORDER BY id").fetchall()
        return [{"name": r["name"], "version": r["version"],
                 "fields": json.loads(r["fields"])} for r in rows]

    # ---- 迁移映射（字段重命名链，不允许成环） ----
    def add_migration(self, name, path, from_field, to_field):
        path = path or "/"
        if from_field == to_field:
            raise MigrationCycle("自环映射 %s -> %s 不被允许" % (from_field, to_field))
        with self._lock:
            rows = self.db.execute(
                "SELECT from_field, to_field FROM migrations WHERE name=? AND path=?",
                (name, path),
            ).fetchall()
            edges = [(r["from_field"], r["to_field"]) for r in rows]
            if _creates_cycle(edges, (from_field, to_field)):
                raise MigrationCycle(
                    "映射 %s -> %s 会与现有迁移链成环" % (from_field, to_field))
            self.db.execute(
                "INSERT INTO migrations(name, path, from_field, to_field) VALUES(?,?,?,?)",
                (name, path, from_field, to_field),
            )
            self.db.commit()
        return {"name": name, "path": path, "from": from_field, "to": to_field}

    def list_migrations(self, name=None):
        if name is None:
            rows = self.db.execute("SELECT * FROM migrations ORDER BY id").fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM migrations WHERE name=? ORDER BY id", (name,)).fetchall()
        return [{"name": r["name"], "path": r["path"],
                 "from": r["from_field"], "to": r["to_field"]} for r in rows]

    def rename_map(self, name):
        """{路径: {旧字段名: 新字段名}}"""
        result = {}
        for m in self.list_migrations(name):
            result.setdefault(m["path"], {})[m["from"]] = m["to"]
        return result

    # ---- head 指针 ----
    def head(self, name):
        row = self.db.execute("SELECT version FROM heads WHERE name=?", (name,)).fetchone()
        return row["version"] if row else None

    def list_heads(self):
        rows = self.db.execute("SELECT * FROM heads ORDER BY name").fetchall()
        return [{"name": r["name"], "version": r["version"]} for r in rows]

    # ---- 发布决策 ----
    def publish(self, name, base_version, version, content, direction,
                consumers, idempotency_key):
        """幂等 + 乐观并发：同一 idempotency_key 返回原决策；
        base_version 与当前 head 不一致时报 HeadConflict。"""
        with self._lock:
            existing = self.db.execute(
                "SELECT * FROM decisions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                decision = self._decision_row(existing)
                decision["idempotent_replay"] = True
                return decision

            current_head = self.head(name)
            if current_head != base_version:
                raise HeadConflict(current_head)

            fp = fingerprint(content)
            try:
                self.db.execute(
                    "INSERT INTO schemas(name, version, content, fingerprint, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (name, version, json.dumps(content, ensure_ascii=False), fp, _now()),
                )
            except sqlite3.IntegrityError:
                raise ValueError("schema %s@%s 已存在" % (name, version))

            if base_version is None:
                base_content = {}
            else:
                base = self.get_schema(name, base_version)
                if base is None:
                    raise ValueError("base 版本 %s@%s 不存在" % (name, base_version))
                base_content = base["content"]

            renames = self.rename_map(name)
            result = check_compatibility(base_content, content, direction,
                                         renames=renames, consumers=consumers)
            cur = self.db.execute(
                "INSERT INTO decisions(name, base_version, candidate_version,"
                " candidate_fingerprint, direction, consumers, renames, verdict,"
                " issues, idempotency_key, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (name, base_version, version, fp, direction,
                 json.dumps(consumers or [], ensure_ascii=False),
                 json.dumps(renames, ensure_ascii=False),
                 result["verdict"],
                 json.dumps(result["issues"], ensure_ascii=False),
                 idempotency_key, _now()),
            )
            self.db.execute(
                "INSERT INTO heads(name, version) VALUES(?,?)"
                " ON CONFLICT(name) DO UPDATE SET version=excluded.version",
                (name, version),
            )
            self.db.commit()
            decision = self.get_decision(cur.lastrowid)
            decision["idempotent_replay"] = False
            return decision

    @staticmethod
    def _decision_row(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "base_version": row["base_version"],
            "candidate_version": row["candidate_version"],
            "candidate_fingerprint": row["candidate_fingerprint"],
            "direction": row["direction"],
            "consumers": json.loads(row["consumers"]),
            "renames": json.loads(row["renames"]),
            "verdict": row["verdict"],
            "issues": json.loads(row["issues"]),
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
        }

    def get_decision(self, decision_id):
        row = self.db.execute(
            "SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return self._decision_row(row) if row else None

    def list_decisions(self):
        rows = self.db.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        return [self._decision_row(r) for r in rows]

    def replay(self, decision_id):
        """用决策当时记录的消费者声明与迁移映射重放，验证结论可复现。"""
        d = self.get_decision(decision_id)
        if d is None:
            return None
        if d["base_version"] is None:
            base_content = {}
        else:
            base = self.get_schema(d["name"], d["base_version"])
            base_content = base["content"] if base else None
        candidate = self.get_schema(d["name"], d["candidate_version"])
        if base_content is None or candidate is None:
            return {"decision_id": decision_id, "replay": "missing-schema"}
        result = check_compatibility(
            base_content, candidate["content"], d["direction"],
            renames=d["renames"], consumers=d["consumers"])
        ok = result["verdict"] == d["verdict"] and result["issues"] == d["issues"]
        return {
            "decision_id": decision_id,
            "replay": "ok" if ok else "mismatch",
            "verdict": result["verdict"],
            "issues": result["issues"],
        }

    # ---- 导入 / 导出 ----
    def export_all(self):
        with self._lock:
            data = {"format": "schemagate-export@1"}
            for table in TABLES:
                rows = self.db.execute(
                    "SELECT * FROM %s ORDER BY id" % table if table != "heads"
                    else "SELECT * FROM heads ORDER BY name"
                ).fetchall()
                data[table] = [dict(r) for r in rows]
            return data

    def import_all(self, data):
        if data.get("format") != "schemagate-export@1":
            raise ValueError("无法识别的导出格式")
        with self._lock:
            for table in TABLES:
                self.db.execute("DELETE FROM %s" % table)
            for table in TABLES:
                for row in data.get(table, []):
                    if table == "schemas":
                        # 指纹由内容派生，重建后必然一致；这里显式重算校验
                        row["fingerprint"] = fingerprint(json.loads(row["content"]))
                    cols = ",".join(row.keys())
                    marks = ",".join("?" for _ in row)
                    self.db.execute(
                        "INSERT INTO %s(%s) VALUES(%s)" % (table, cols, marks),
                        list(row.values()),
                    )
            self.db.commit()
        return {"imported": {t: len(data.get(t, [])) for t in TABLES}}
