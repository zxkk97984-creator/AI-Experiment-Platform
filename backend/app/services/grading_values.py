"""可信值编码（批次三 3A）：带类型的版本化值协议。

用途：数据型判题里，**容器只回传实际值**，平台可信侧在拿到期望值后自行比较。
因此需要一个明确、有界、可校验的线格式，而不是任意序列化（pickle/eval/对象）。

协议（每个值是一个 JSON 对象，``t`` 是类型标签）：

.. code-block:: text

    {"t": "none"}                                  # None
    {"t": "bool",   "v": true/false}
    {"t": "int",    "v": 123}                      # 十进制整数（有界）
    {"t": "float",  "v": 1.5}                      # 必须有限
    {"t": "str",    "v": "..."}                    # UTF-8，长度有界
    {"t": "bytes",  "v": "<base64>"}
    {"t": "tuple",  "v": [<value>, ...]}           # 与 list 区分：Python 相等语义依赖它
    {"t": "list",   "v": [<value>, ...]}
    {"t": "dict",   "v": [[<key>, <value>], ...]}  # 保留插入顺序与键类型
    {"t": "array",  "dtype": "float64", "shape": [2, 3], "data": [...]}

设计约束（与计划一致）：

* 支持：普通字面量、tuple/list/dict、bool/int/float 密集 ndarray、有限内置异常标识；
* 拒绝：自定义对象、生成器/闭包、object dtype、非有限浮点、超深度/超元素/超长度；
* **不导入 numpy**：平台侧只按 dtype/shape/data 处理，容器侧负责编码真实 ndarray。
"""

from __future__ import annotations

import base64
import math
from typing import Any

# ── 有界上限（新增设计默认值，先在独立题库验证兼容性；超限内容标注为需调整/人工） ──
MAX_VALUE_DEPTH = 16
MAX_CONTAINER_ITEMS = 4096
MAX_ARRAY_ELEMENTS = 4096
MAX_STRING_BYTES = 65536
# 整数按位长限制（避免 str(int) 触发 CPython 的 4300 位转换上限）
MAX_INT_BITS = 4096
MAX_ENCODED_BYTES = 65536
# 每组/每题上限（供计划校验与 Worker 读取使用）
MAX_CASES_PER_GROUP = 256
MAX_GROUPS_PER_QUESTION = 32

# 允许的数组 dtype（有限内置数值/布尔类型）
ALLOWED_ARRAY_DTYPES = {
    "bool", "int8", "int16", "int32", "int64",
    "uint8", "uint16", "uint32", "uint64",
    "float16", "float32", "float64",
}

# 允许回传的异常类型（有限内置名单；自定义异常与超时另行处理）
ALLOWED_EXCEPTION_TYPES = frozenset({
    "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
    "ArithmeticError", "OverflowError", "StopIteration", "AssertionError",
    "RecursionError", "NotImplementedError", "AttributeError", "UnboundLocalError",
    "RuntimeError", "MemoryError",
})

_SCALARS = ("none", "bool", "int", "float", "str", "bytes")
_CONTAINERS = ("tuple", "list", "dict")
_ARRAY = "array"


class ValueEncodingError(ValueError):
    """值不符合协议（不可序列化或超出上限）——属于需要教师/人工处理的配置问题。"""


def _fail(message: str) -> "ValueEncodingError":
    return ValueEncodingError(message)


def encode_value(value: Any, *, depth: int = 0) -> dict:
    """把 Python 值编码成协议对象；不支持或超限时抛 ``ValueEncodingError``。"""
    if depth > MAX_VALUE_DEPTH:
        raise _fail(f"值嵌套超过 {MAX_VALUE_DEPTH} 层")

    if value is None:
        return {"t": "none"}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        if value.bit_length() > MAX_INT_BITS:
            raise _fail(f"整数位数超过 {MAX_INT_BITS} 位")
        return {"t": "int", "v": value}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("非有限浮点不受支持")
        return {"t": "float", "v": value}
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise _fail(f"字符串超过 {MAX_STRING_BYTES} 字节")
        return {"t": "str", "v": value}
    if isinstance(value, (bytes, bytearray)):
        if len(value) > MAX_STRING_BYTES:
            raise _fail(f"字节串超过 {MAX_STRING_BYTES} 字节")
        return {"t": "bytes", "v": base64.b64encode(bytes(value)).decode("ascii")}

    array = _encode_array(value, depth=depth)
    if array is not None:
        return array

    if isinstance(value, tuple):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise _fail(f"元组元素超过 {MAX_CONTAINER_ITEMS}")
        return {"t": "tuple", "v": [encode_value(item, depth=depth + 1) for item in value]}
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise _fail(f"列表元素超过 {MAX_CONTAINER_ITEMS}")
        return {"t": "list", "v": [encode_value(item, depth=depth + 1) for item in value]}
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise _fail(f"字典条目超过 {MAX_CONTAINER_ITEMS}")
        return {"t": "dict", "v": [[encode_value(key, depth=depth + 1),
                                   encode_value(item, depth=depth + 1)]
                                  for key, item in value.items()]}

    raise _fail(f"不支持的值类型: {type(value).__name__}")


def _encode_array(value: Any, *, depth: int) -> dict | None:
    """尽力编码 ndarray（不导入 numpy，靠 duck typing）。非数组返回 None。"""
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    tolist = getattr(value, "tolist", None)
    if shape is None or dtype is None or not callable(tolist):
        return None
    kind = getattr(dtype, "kind", None)
    name = getattr(dtype, "name", None)
    if name not in ALLOWED_ARRAY_DTYPES:
        raise _fail(f"不支持的数组 dtype: {name!r}（object/结构化 dtype 拒绝）")
    if kind not in ("b", "i", "u", "f"):
        raise _fail(f"不支持的数组 dtype 类别: {kind!r}")
    total = 1
    for size in shape:
        total *= int(size)
    if total > MAX_ARRAY_ELEMENTS:
        raise _fail(f"数组元素超过 {MAX_ARRAY_ELEMENTS}")
    flat = _flatten(tolist())
    if len(flat) != total:
        raise _fail("数组元素数与 shape 不一致")
    for item in flat:
        if isinstance(item, bool):
            continue
        if not isinstance(item, (int, float)):
            raise _fail("数组元素必须是数值或布尔")
        if isinstance(item, float) and not math.isfinite(item):
            raise _fail("数组包含非有限浮点")
    return {"t": "array", "dtype": name, "shape": [int(size) for size in shape], "data": flat}


def _flatten(nested: Any) -> list:
    if isinstance(nested, list):
        result: list = []
        for item in nested:
            result.extend(_flatten(item))
        return result
    return [nested]


def decode_value(payload: Any, *, depth: int = 0) -> Any:
    """严格解码协议对象；任何形态不符都抛 ``ValueEncodingError``。"""
    if not isinstance(payload, dict) or "t" not in payload:
        raise _fail("值必须是带 t 标签的对象")
    if depth > MAX_VALUE_DEPTH:
        raise _fail(f"值嵌套超过 {MAX_VALUE_DEPTH} 层")
    kind = payload["t"]

    if kind == "none":
        return None
    if kind == "bool":
        if not isinstance(payload.get("v"), bool):
            raise _fail("bool 值必须是布尔")
        return payload["v"]
    if kind == "int":
        if isinstance(payload.get("v"), bool) or not isinstance(payload.get("v"), int):
            raise _fail("int 值必须是整数")
        return payload["v"]
    if kind == "float":
        value = payload.get("v")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail("float 值必须是数值")
        value = float(value)
        if not math.isfinite(value):
            raise _fail("非有限浮点不受支持")
        return value
    if kind == "str":
        if not isinstance(payload.get("v"), str):
            raise _fail("str 值必须是字符串")
        return payload["v"]
    if kind == "bytes":
        raw = payload.get("v")
        if not isinstance(raw, str):
            raise _fail("bytes 值必须是 base64 字符串")
        try:
            data = base64.b64decode(raw.encode("ascii"), validate=True)
        except Exception as exc:  # noqa: BLE001 - 统一转协议错误
            raise _fail(f"bytes base64 非法: {exc}") from exc
        if len(data) > MAX_STRING_BYTES:
            raise _fail(f"字节串超过 {MAX_STRING_BYTES} 字节")
        return data
    if kind in _CONTAINERS:
        items = payload.get("v")
        if not isinstance(items, list):
            raise _fail(f"{kind} 的 v 必须是数组")
        if len(items) > MAX_CONTAINER_ITEMS:
            raise _fail(f"{kind} 元素超过 {MAX_CONTAINER_ITEMS}")
        if kind == "dict":
            result = {}
            for entry in items:
                if not isinstance(entry, list) or len(entry) != 2:
                    raise _fail("dict 条目必须是 [key, value]")
                key = decode_value(entry[0], depth=depth + 1)
                try:
                    result[key] = decode_value(entry[1], depth=depth + 1)
                except TypeError as exc:
                    raise _fail(f"dict 键不可哈希: {exc}") from exc
            return result
        decoded = [decode_value(item, depth=depth + 1) for item in items]
        return tuple(decoded) if kind == "tuple" else decoded
    if kind == _ARRAY:
        name = payload.get("dtype")
        shape = payload.get("shape")
        data = payload.get("data")
        if name not in ALLOWED_ARRAY_DTYPES:
            raise _fail(f"不支持的数组 dtype: {name!r}")
        if not isinstance(shape, list) or not all(isinstance(size, int) and size >= 0 for size in shape):
            raise _fail("数组 shape 必须是非负整数数组")
        if not isinstance(data, list):
            raise _fail("数组 data 必须是扁平数组")
        expected = 1
        for size in shape:
            expected *= size
        if len(data) != expected:
            raise _fail("数组 data 长度与 shape 不一致")
        if len(data) > MAX_ARRAY_ELEMENTS:
            raise _fail(f"数组元素超过 {MAX_ARRAY_ELEMENTS}")
        return {"__dai_array__": True, "dtype": name, "shape": list(shape), "data": list(data)}
    raise _fail(f"未知的值类型标签: {kind!r}")


def is_array(value: Any) -> bool:
    return isinstance(value, dict) and value.get("__dai_array__") is True


def dtype_kind(dtype: str) -> str:
    if dtype == "bool":
        return "bool"
    if dtype.startswith("float"):
        return "float"
    if dtype.startswith("uint"):
        return "uint"
    if dtype.startswith("int"):
        return "int"
    return "unknown"


def encoded_size(payload: Any) -> int:
    """编码后的近似字节数（用于上限校验）。"""
    import json

    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
