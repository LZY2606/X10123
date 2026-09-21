"""HTTP 层测试：页面、检查接口、幂等与冲突状态码。"""
import json
import threading
import urllib.request

import pytest

from schemagate.server import make_server


@pytest.fixture()
def server(tmp_path):
    httpd = make_server("127.0.0.1", 0, str(tmp_path / "http.db"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(base, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_index_page_has_gate_title(server):
    with urllib.request.urlopen(server + "/") as res:
        body = res.read().decode()
    assert "兼容性闸门" in body


def test_check_endpoint_structural_not_string_diff(server):
    old = {"type": "object", "properties": {"a": {"type": "string"},
                                           "b": {"type": "integer"}}}
    # 仅键顺序不同：结构上完全等价，必须判兼容
    new = {"properties": {"b": {"type": "integer"}, "a": {"type": "string"}},
           "type": "object"}
    status, result = call(server, "POST", "/api/check",
                          {"old": old, "new": new, "direction": "full"})
    assert status == 200
    assert result["verdict"] == "compatible"
    assert result["issues"] == []


def test_publish_idempotency_and_head_conflict_over_http(server):
    v1 = {"type": "object", "properties": {"id": {"type": "string"}}}
    call(server, "POST", "/api/schemas", {"name": "s", "version": "v1", "content": v1})
    payload = {"name": "s", "base_version": "v1", "version": "v2", "content": v1,
               "direction": "full", "consumers": [], "idempotency_key": "k1"}
    s1, d1 = call(server, "POST", "/api/publish", payload)
    s2, d2 = call(server, "POST", "/api/publish", payload)
    assert (s1, s2) == (200, 200)
    assert d1["id"] == d2["id"] and d2["idempotent_replay"]
    conflict = dict(payload, idempotency_key="k2", version="v3")
    s3, d3 = call(server, "POST", "/api/publish", conflict)
    assert s3 == 409 and d3["kind"] == "HeadConflict"


def test_export_import_roundtrip_over_http(server, tmp_path):
    v1 = {"type": "object", "properties": {"id": {"type": "string"}}}
    call(server, "POST", "/api/schemas", {"name": "s", "version": "v1", "content": v1})
    call(server, "POST", "/api/publish",
         {"name": "s", "base_version": "v1", "version": "v2", "content": v1,
          "direction": "full", "consumers": [], "idempotency_key": "k1"})
    _, exported = call(server, "GET", "/api/export")

    other = make_server("127.0.0.1", 0, str(tmp_path / "other.db"))
    threading.Thread(target=other.serve_forever, daemon=True).start()
    base2 = "http://127.0.0.1:%d" % other.server_address[1]
    try:
        status, _ = call(base2, "POST", "/api/import", exported)
        assert status == 200
        _, state = call(base2, "GET", "/api/state")
        fps = {s["fingerprint"] for s in state["schemas"]}
        assert fps == {s["fingerprint"] for s in exported["schemas"]}
        assert [d["id"] for d in state["decisions"]] == [1]
        _, replay = call(base2, "GET", "/api/decisions/1/replay")
        assert replay["replay"] == "ok"
    finally:
        other.shutdown()
        other.server_close()
