"""有理微升容量与状态序列化。

所有容量一律使用 fractions.Fraction 表达（单位: 微升），
序列化为 {"__frac__": "n/d"}，杜绝浮点累积误差。
"""
from __future__ import annotations

import copy
import hashlib
import json
from fractions import Fraction

FRAC_MARK = "__frac__"


def frac(value) -> Fraction:
    """从 int / str('n/d' 或 'n') / Fraction 构造有理数。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    raise TypeError(f"无法解析为有理微升: {value!r}")


def volume(contents: dict) -> Fraction:
    """成分字典 {成分: 体积} 的总体积。"""
    total = Fraction(0)
    for amount in contents.values():
        total += amount
    return total


def draw(contents: dict, vol: Fraction):
    """按比例从成分字典中抽取 vol，返回抽出的成分字典。

    有理数精确运算: sum(draw) == vol 恒成立，不会凭空增减。
    余量不足返回 None。
    """
    total = volume(contents)
    if total < vol:
        return None
    if total == 0:
        return {} if vol == 0 else None
    out = {}
    for comp, amount in contents.items():
        if amount:
            out[comp] = amount * vol / total
    return out


def to_jsonable(obj):
    if isinstance(obj, Fraction):
        return {FRAC_MARK: f"{obj.numerator}/{obj.denominator}"}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def from_jsonable(obj):
    if isinstance(obj, dict):
        if set(obj.keys()) == {FRAC_MARK}:
            return Fraction(obj[FRAC_MARK])
        return {k: from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_jsonable(v) for v in obj]
    return obj


def state_to_json(state: dict) -> str:
    return json.dumps(to_jsonable(state), sort_keys=True, ensure_ascii=False)


def state_from_json(text: str) -> dict:
    return from_jsonable(json.loads(text))


def state_hash(state: dict) -> str:
    canonical = state_to_json(state)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def deep_copy(state: dict) -> dict:
    return copy.deepcopy(state)


def fmt_frac(value: Fraction) -> str:
    """展示用: 整数直接显示，否则显示 n/d。"""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"
