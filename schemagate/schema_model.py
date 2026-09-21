"""Schema 内容的规范化与指纹。

相同内容、不同键顺序的提交必须得到同一指纹，因此先递归排序对象键，
再用紧凑分隔符序列化后取 SHA-256。
"""
from __future__ import annotations

import hashlib
import json


def canonicalize(node):
    if isinstance(node, dict):
        return {key: canonicalize(node[key]) for key in sorted(node)}
    if isinstance(node, list):
        return [canonicalize(item) for item in node]
    return node


def canonical_json(doc) -> str:
    return json.dumps(
        canonicalize(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def fingerprint(doc) -> str:
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
