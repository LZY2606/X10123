"""兼容引擎测试：组合类型、默认值、方向语义、重命名映射。"""
from schemagate.compat import check_compatibility


def rules(result, direction=None):
    return {(i["rule"], i["direction"]) for i in result["issues"]
            if direction is None or i["direction"] == direction}


def test_numeric_narrowing_is_backward_only():
    old = {"type": "integer", "minimum": 0, "maximum": 100}
    new = {"type": "integer", "minimum": 0, "maximum": 50}
    back = check_compatibility(old, new, "backward")
    assert back["verdict"] == "incompatible"
    assert ("maximum-narrowed", "backward") in rules(back)
    issue = next(i for i in back["issues"] if i["rule"] == "maximum-narrowed")
    assert issue["counterexample"] == 100  # 最小反例：旧方可写、新方拒收的值
    fwd = check_compatibility(old, new, "forward")
    assert fwd["verdict"] == "compatible"


def test_union_new_branch_is_forward_only():
    old = {"type": ["string", "null"]}
    new = {"type": ["string", "null", "integer"]}
    fwd = check_compatibility(old, new, "forward")
    assert fwd["verdict"] == "incompatible"
    assert ("type-not-accepted", "forward") in rules(fwd)
    back = check_compatibility(old, new, "backward")
    assert back["verdict"] == "compatible"


def test_nested_object_array_enum_composite():
    old = {
        "type": "object", "required": ["items"], "additionalProperties": False,
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object", "required": ["sku"],
                "properties": {
                    "sku": {"type": "string"},
                    "level": {"type": "string", "enum": ["a", "b"]},
                },
            }},
        },
    }
    new = {
        "type": "object", "required": ["items"], "additionalProperties": False,
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object", "required": ["sku"],
                "properties": {
                    "sku": {"type": "string"},
                    "level": {"type": "string", "enum": ["a", "b", "c"]},
                },
            }},
        },
    }
    result = check_compatibility(old, new, "full")
    assert result["verdict"] == "incompatible"
    issue = result["issues"][0]
    assert issue["rule"] == "enum-value-not-accepted"
    assert issue["direction"] == "forward"          # 新枚举值只影响旧读取方
    assert issue["path"] == "/items/[]/level"       # 结构路径定位到嵌套数组元素
    assert issue["counterexample"] == "c"
    assert check_compatibility(old, new, "backward")["verdict"] == "compatible"


def test_array_item_type_change_breaks_both_directions():
    old = {"type": "array", "items": {"type": "string"}}
    new = {"type": "array", "items": {"type": "integer"}}
    result = check_compatibility(old, new, "full")
    assert ("type-not-accepted", "backward") in rules(result)
    assert ("type-not-accepted", "forward") in rules(result)


def test_delete_field_with_default_is_safe_both_ways():
    old = {
        "type": "object", "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "nick": {"type": "string", "default": "anon"},
        },
    }
    new = {"type": "object", "required": ["id"],
           "properties": {"id": {"type": "string"}}}
    result = check_compatibility(old, new, "full")
    assert result["verdict"] == "compatible", result["issues"]


def test_delete_required_field_without_default_breaks_forward():
    old = {"type": "object", "required": ["id", "nick"],
           "properties": {"id": {"type": "string"}, "nick": {"type": "string"}}}
    new = {"type": "object", "required": ["id"],
           "properties": {"id": {"type": "string"}}}
    result = check_compatibility(old, new, "full")
    assert ("required-field-missing", "forward") in rules(result)
    assert check_compatibility(old, new, "backward")["verdict"] == "compatible"


def test_add_required_field_without_default_breaks_backward():
    old = {"type": "object", "properties": {"id": {"type": "string"}}}
    new = {"type": "object", "required": ["id", "email"],
           "properties": {"id": {"type": "string"}, "email": {"type": "string"}}}
    result = check_compatibility(old, new, "backward")
    assert result["verdict"] == "incompatible"
    assert ("required-field-missing", "backward") in rules(result)


def test_rename_requires_explicit_mapping():
    old = {"type": "object", "required": ["name"], "additionalProperties": False,
           "properties": {"name": {"type": "string"}}}
    new = {"type": "object", "required": ["full_name"], "additionalProperties": False,
           "properties": {"full_name": {"type": "string"}}}
    bare = check_compatibility(old, new, "full")
    assert bare["verdict"] == "incompatible"
    mapped = check_compatibility(old, new, "full",
                                 renames={"/": {"name": "full_name"}})
    assert mapped["verdict"] == "compatible", mapped["issues"]


def test_consumer_declaration_scopes_severity_and_defaults():
    old = {"type": "object", "required": ["a", "b"],
           "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
    new = {"type": "object", "required": ["a"],
           "properties": {"a": {"type": "string"}}}
    consumers = [
        {"name": "billing", "version": "c1", "fields": [{"path": "/a"}]},
        {"name": "search", "version": "c1",
         "fields": [{"path": "/b", "default": 0}]},  # 自己声明了默认值
    ]
    result = check_compatibility(old, new, "forward", consumers=consumers)
    assert result["verdict"] == "compatible"  # 无人受影响的删除降级为 warning
    assert all(i["severity"] == "warning" for i in result["issues"])
    readers = [{"name": "report", "version": "c1", "fields": [{"path": "/b"}]}]
    result2 = check_compatibility(old, new, "forward", consumers=readers)
    assert result2["verdict"] == "incompatible"
    assert result2["issues"][0]["consumers"] == ["report@c1"]


def test_nullable_and_additional_properties():
    old = {"type": "object", "additionalProperties": False,
           "properties": {"x": {"type": "string", "nullable": True}}}
    new = {"type": "object", "additionalProperties": False,
           "properties": {"x": {"type": "string"}, "y": {"type": "string"}}}
    result = check_compatibility(old, new, "full")
    # 旧方可写 null，新方不收 -> backward；新方多写字段 y，旧方不允许未知字段 -> forward
    assert ("type-not-accepted", "backward") in rules(result)
    assert ("unexpected-field", "forward") in rules(result)
