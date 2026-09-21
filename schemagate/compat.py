"""结构化兼容性引擎。

按读取方向比较两份 JSON Schema 风格的记录定义：
- backward: 新版本作为读取方，旧版本作为写入方（新代码能读旧数据）。
- forward:  旧版本作为读取方，新版本作为写入方（旧代码能读新数据）。
- full:     两个方向都要成立。

每个不兼容点都绑定结构路径、受影响消费者和一个最小反例。
不做任何字符串 diff，全部基于解析后的 schema 树做语义判断。
"""
from __future__ import annotations

PRIMITIVE_DEFAULTS = {
    "null": None,
    "boolean": False,
    "integer": 0,
    "number": 0,
    "string": "",
}

MISSING_FIELD_RULES = {"required-field-missing", "field-may-be-absent"}


def type_set(schema):
    """返回 schema 允许的类型集合；None 表示类型不受约束。"""
    if not isinstance(schema, dict):
        return None
    t = schema.get("type")
    if t is None:
        if "properties" in schema or "required" in schema:
            types = {"object"}
        elif "items" in schema:
            types = {"array"}
        else:
            types = None
    elif isinstance(t, list):
        types = set(t)
    else:
        types = {t}
    if types is not None and schema.get("nullable"):
        types = set(types) | {"null"}
    return types


def _is_object(schema):
    return isinstance(schema, dict) and (
        "properties" in schema or "object" in (type_set(schema) or set())
    )


def _is_array(schema):
    return isinstance(schema, dict) and (
        "items" in schema or "array" in (type_set(schema) or set())
    )


def _value_of_type(t):
    if t == "object":
        return {}
    if t == "array":
        return []
    return PRIMITIVE_DEFAULTS.get(t)


def _minimal_object(schema):
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    return {k: _minimal_value(v) for k, v in props.items() if k in required}


def _minimal_value(schema):
    if not isinstance(schema, dict):
        return None
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    union = schema.get("anyOf") or schema.get("oneOf")
    if union:
        return _minimal_value(union[0])
    types = type_set(schema)
    if not types:
        return None
    for t in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if t in types:
            if t == "object":
                return _minimal_object(schema)
            return _value_of_type(t)
    return None


def _outside_enum_value(writer, renum):
    types = type_set(writer) or set()
    candidates = []
    if "string" in types:
        candidates.append(" out-of-enum")
    if types & {"integer", "number"}:
        nums = [v for v in renum if isinstance(v, (int, float)) and not isinstance(v, bool)]
        candidates.append((max(nums) + 1) if nums else 0)
    if "boolean" in types:
        candidates.extend([True, False])
    if "null" in types:
        candidates.append(None)
    for c in candidates:
        if c not in renum:
            return c
    return None


def _issue(path, rule, direction, message, counterexample):
    return {
        "path": path or "/",
        "rule": rule,
        "direction": direction,
        "message": message,
        "severity": "error",
        "counterexample": counterexample,
        "consumers": [],
    }


def _join(path, seg):
    return (path or "") + "/" + seg


def _check(writer, reader, path, renames, direction, issues):
    """检查 writer 产出的数据能否被 reader 安全读取。"""
    if not isinstance(writer, dict) or not isinstance(reader, dict):
        return

    w_union = writer.get("anyOf") or writer.get("oneOf")
    r_union = reader.get("anyOf") or reader.get("oneOf")
    if w_union or r_union:
        w_branches = w_union or [writer]
        r_branches = r_union or [reader]
        for wb in w_branches:
            for rb in r_branches:
                sub = []
                _check(wb, rb, path, renames, direction, sub)
                if not sub:
                    break
            else:
                issues.append(_issue(
                    path, "union-branch-not-accepted", direction,
                    "写入方 union 分支不被读取方任何分支接受",
                    _minimal_value(wb),
                ))
        return

    wtypes = type_set(writer)
    rtypes = type_set(reader)
    if wtypes is not None and rtypes is not None:
        for t in sorted(wtypes - rtypes):
            issues.append(_issue(
                path, "type-not-accepted", direction,
                "写入方可能产生类型 %s，读取方不接受" % t,
                _value_of_type(t),
            ))
    elif wtypes is None and rtypes is not None and "enum" not in writer:
        issues.append(_issue(
            path, "unconstrained-writer", direction,
            "写入方类型不受约束，读取方无法保证兼容", None,
        ))

    wenum = writer.get("enum")
    renum = reader.get("enum")
    if wenum is not None and renum is not None:
        extra = [v for v in wenum if v not in renum]
        if extra:
            issues.append(_issue(
                path, "enum-value-not-accepted", direction,
                "枚举值 %r 不在读取方枚举中" % (extra,), extra[0],
            ))
    elif renum is not None and wenum is None:
        candidate = _minimal_value(writer)
        if candidate in renum:
            candidate = _outside_enum_value(writer, renum)
        if candidate is not None and candidate not in renum:
            issues.append(_issue(
                path, "value-maybe-outside-enum", direction,
                "写入方可能产生读取方枚举之外的值", candidate,
            ))

    wmin, wmax = writer.get("minimum"), writer.get("maximum")
    rmin, rmax = reader.get("minimum"), reader.get("maximum")
    if rmin is not None and (wmin is None or wmin < rmin):
        issues.append(_issue(
            path, "minimum-narrowed", direction,
            "读取方要求 minimum=%s，写入方可产生更小值" % rmin,
            wmin if wmin is not None else rmin - 1,
        ))
    if rmax is not None and (wmax is None or wmax > rmax):
        issues.append(_issue(
            path, "maximum-narrowed", direction,
            "读取方要求 maximum=%s，写入方可产生更大值" % rmax,
            wmax if wmax is not None else rmax + 1,
        ))

    if _is_object(writer) and _is_object(reader):
        _check_object(writer, reader, path, renames, direction, issues)

    if _is_array(writer) and _is_array(reader):
        _check(writer.get("items") or {}, reader.get("items") or {},
               _join(path, "[]"), renames, direction, issues)


def _check_object(writer, reader, path, renames, direction, issues):
    wprops = writer.get("properties") or {}
    rprops = reader.get("properties") or {}
    wreq = set(writer.get("required") or [])
    rreq = set(reader.get("required") or [])
    w2r = renames.get(path or "/", {})
    r2w = {v: k for k, v in w2r.items()}
    additional = reader.get("additionalProperties", True)

    for rname, rsub in rprops.items():
        wname = r2w.get(rname, rname)
        if wname in wprops:
            if rname in rreq and wname not in wreq and "default" not in rsub:
                issues.append(_issue(
                    _join(path, rname), "field-may-be-absent", direction,
                    "读取方要求字段 %s 必填，写入方可能缺省" % rname,
                    _minimal_object(writer),
                ))
            _check(wprops[wname], rsub, _join(path, rname), renames, direction, issues)
        else:
            if "default" in rsub:
                continue  # 读取方用默认值回填
            if rname in rreq:
                issues.append(_issue(
                    _join(path, rname), "required-field-missing", direction,
                    "读取方必填字段 %s 在写入方不存在" % rname,
                    _minimal_object(writer),
                ))
    for wname, wsub in wprops.items():
        rname = w2r.get(wname, wname)
        if rname not in rprops and additional is False:
            issues.append(_issue(
                _join(path, wname), "unexpected-field", direction,
                "写入方字段 %s 不被读取方接受（additionalProperties=false）" % wname,
                {wname: _minimal_value(wsub)},
            ))


def _norm(path):
    return tuple(seg for seg in (path or "").split("/") if seg and seg != "[]")


def _paths_intersect(a, b):
    ta, tb = _norm(a), _norm(b)
    n = min(len(ta), len(tb))
    return ta[:n] == tb[:n]


def _attach_consumers(issues, consumers):
    for issue in issues:
        affected = []
        for c in consumers or []:
            for field in c.get("fields") or []:
                fpath = field.get("path", "/")
                if not _paths_intersect(issue["path"], fpath):
                    continue
                if (issue["rule"] in MISSING_FIELD_RULES
                        and "default" in field
                        and _norm(fpath) == _norm(issue["path"])):
                    continue  # 消费者自己声明了默认值，可容忍缺省
                affected.append("%s@%s" % (c.get("name"), c.get("version")))
                break
        issue["consumers"] = sorted(set(affected))
        if consumers:
            issue["severity"] = "error" if affected else "warning"
        else:
            issue["severity"] = "error"


def check_compatibility(old, new, direction="full", renames=None, consumers=None):
    """比较 old 与 new，返回 {verdict, direction, issues}。

    renames: {路径: {旧字段名: 新字段名}} 的显式迁移映射（根路径用 "/"）。
    consumers: [{"name","version","fields":[{"path","default"?}]}] 已解析的声明。
    """
    renames = renames or {}
    issues = []
    if direction in ("backward", "full"):
        _check(old, new, "", renames, "backward", issues)
    if direction in ("forward", "full"):
        inverted = {p: {to: frm for frm, to in m.items()} for p, m in renames.items()}
        _check(new, old, "", inverted, "forward", issues)
    _attach_consumers(issues, consumers)
    issues.sort(key=lambda i: (0 if i["severity"] == "error" else 1, i["path"], i["rule"]))
    verdict = "compatible" if not any(i["severity"] == "error" for i in issues) else "incompatible"
    return {"verdict": verdict, "direction": direction, "issues": issues}
