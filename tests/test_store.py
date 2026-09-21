"""持久化测试：指纹归一化、映射环、幂等发布、head 冲突、决策重放、导入导出。"""
import pytest

from schemagate.schema_model import fingerprint
from schemagate.store import HeadConflict, MigrationCycle, Store

SCHEMA_V1 = {"type": "object", "required": ["id"],
             "properties": {"id": {"type": "string"}}}
SCHEMA_V2 = {"type": "object", "required": ["id"],
             "properties": {"id": {"type": "string"},
                            "tag": {"type": "string", "default": ""}}}


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_fingerprint_ignores_key_order():
    a = {"type": "object", "properties": {"x": {"type": "string"},
         "y": {"enum": [1, 2], "type": "integer"}}}
    b = {"properties": {"y": {"type": "integer", "enum": [1, 2]},
         "x": {"type": "string"}}, "type": "object"}
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint({"type": "object"})


def test_migration_chain_cycle_rejected(store):
    store.add_migration("order", "/", "a", "b")
    store.add_migration("order", "/", "b", "c")
    with pytest.raises(MigrationCycle):
        store.add_migration("order", "/", "c", "a")
    with pytest.raises(MigrationCycle):
        store.add_migration("order", "/", "x", "x")
    # 不同路径上的同名映射互不影响
    store.add_migration("order", "/sub", "c", "a")


def test_idempotent_publish_returns_original_decision(store):
    store.register_schema("order", "v1", SCHEMA_V1)
    d1 = store.publish("order", "v1", "v2", SCHEMA_V2, "full", [], "rel-1")
    d2 = store.publish("order", "v1", "v2", SCHEMA_V2, "full", [], "rel-1")
    assert d1["id"] == d2["id"]
    assert d2["idempotent_replay"] is True
    assert len(store.list_decisions()) == 1
    assert store.head("order") == "v2"


def test_head_conflict_only_one_publish_wins(store):
    store.register_schema("order", "v1", SCHEMA_V1)
    store.publish("order", "v1", "v2", SCHEMA_V2, "full", [], "rel-a")
    with pytest.raises(HeadConflict):
        store.publish("order", "v1", "v3", SCHEMA_V2, "full", [], "rel-b")
    assert store.head("order") == "v2"
    assert len(store.list_decisions()) == 1


def test_decision_replay_uses_historical_consumer_declaration(store):
    store.register_schema("order", "v1", SCHEMA_V1)
    store.register_consumer("billing", "c1", [{"path": "/id"}])
    consumers = [store.get_consumer("billing", "c1")]
    d = store.publish("order", "v1", "v2", SCHEMA_V2, "full", consumers, "rel-1")
    # 消费者声明演进为新版本后，旧决策仍引用当时的 c1
    store.register_consumer("billing", "c2", [{"path": "/id"}, {"path": "/tag"}])
    replay = store.replay(d["id"])
    assert replay["replay"] == "ok"
    assert d["consumers"][0]["version"] == "c1"


def test_export_import_roundtrip_preserves_fingerprints_and_decision_ids(store, tmp_path):
    store.register_schema("order", "v1", SCHEMA_V1)
    store.register_consumer("billing", "c1", [{"path": "/id"}])
    store.add_migration("order", "/", "old_name", "new_name")
    store.publish("order", "v1", "v2", SCHEMA_V2, "full",
                  [store.get_consumer("billing", "c1")], "rel-1")
    exported = store.export_all()

    rebuilt = Store(str(tmp_path / "rebuilt.db"))
    rebuilt.import_all(exported)
    assert [s["fingerprint"] for s in rebuilt.list_schemas()] == \
           [s["fingerprint"] for s in store.list_schemas()]
    assert [d["id"] for d in rebuilt.list_decisions()] == \
           [d["id"] for d in store.list_decisions()]
    assert rebuilt.head("order") == store.head("order") == "v2"
    assert rebuilt.replay(1)["replay"] == "ok"
    rebuilt.close()
