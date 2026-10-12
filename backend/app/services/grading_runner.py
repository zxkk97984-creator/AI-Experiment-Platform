"""数据型判题 runner 的生成与逐用例结果帧解析。

A12 安全契约：

* 每个用例由宿主创建独立目录和独立容器，容器中只有当前输入；
* 不向容器传签名密钥或期望值。宿主忽略容器声称的分数/通过计数，只比较实际值；
* **有界读取**：单帧与整段输出都有字节上限，超限由调用方按平台错误处理。
"""
from __future__ import annotations

import json

RESULT_PREFIX = "DAI_RESULT="
# 单帧上限：主机写入与容器回传都按此上限校验（超长帧直接判平台错误）
CASE_FRAME_MAX_BYTES = 64 * 1024
MAX_FRAME_BYTES = 65536
MAX_CASES = 256
MAX_FRAMES = MAX_CASES
MAX_STDOUT_BYTES = 1024 * 1024

# ok / exception 来自学生代码；invalid_request 表示用例配置或输入协议有问题（平台侧）
ALLOWED_FRAME_STATUSES = ("ok", "exception", "invalid_request")

_RUNNER_SOURCE = '''"""(自动生成) 数据型判题 runner：只接收输入，只回传实际值或异常类型。"""
import importlib.util
import json
import math
import sys

MAX_DEPTH = 16
MAX_ITEMS = 4096
MAX_STRING = 65536
ALLOWED_DTYPES = {
    "bool", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32",
    "uint64", "float16", "float32", "float64",
}
RESULT_PREFIX = "DAI_RESULT="
MAX_CASES = 256


class _RequestError(Exception):
    """用例请求本身不合法（平台侧配置问题，不是学生错误）。"""


def _fail(message):
    raise _RequestError(message)


def _np():
    try:
        import numpy
    except Exception:
        return None
    return numpy


def decode_value(payload, depth=0):
    if depth > MAX_DEPTH:
        _fail("depth")
    if not isinstance(payload, dict) or "t" not in payload:
        _fail("bad value")
    kind = payload["t"]
    if kind == "none":
        return None
    if kind == "bool":
        return bool(payload["v"])
    if kind == "int":
        return int(payload["v"])
    if kind == "float":
        value = float(payload["v"])
        if not math.isfinite(value):
            _fail("non-finite")
        return value
    if kind == "str":
        return str(payload["v"])
    if kind == "bytes":
        import base64
        return base64.b64decode(payload["v"])
    if kind == "tuple":
        return tuple(decode_value(item, depth + 1) for item in payload["v"])
    if kind == "list":
        return [decode_value(item, depth + 1) for item in payload["v"]]
    if kind == "dict":
        return {decode_value(k, depth + 1): decode_value(v, depth + 1) for k, v in payload["v"]}
    if kind == "array":
        numpy = _np()
        name = payload["dtype"]
        if name not in ALLOWED_DTYPES:
            _fail("dtype")
        shape = [int(size) for size in payload["shape"]]
        data = list(payload["data"])
        if len(data) > MAX_ITEMS * 4:
            _fail("too many elements")
        if numpy is None:
            return {"__dai_array__": True, "dtype": name, "shape": shape, "data": data}
        return numpy.array(data, dtype=name).reshape(shape) if shape else numpy.array(data, dtype=name)
    _fail("unknown type")


def encode_value(value, depth=0):
    if depth > MAX_DEPTH:
        _fail("depth")
    if value is None:
        return {"t": "none"}
    numpy = _np()
    if numpy is not None and isinstance(value, numpy.ndarray):
        if value.dtype.name not in ALLOWED_DTYPES:
            _fail("dtype")
        flat = [item.item() if hasattr(item, "item") else item for item in value.reshape(-1)]
        if len(flat) > MAX_ITEMS * 4:
            _fail("too many elements")
        for item in flat:
            if isinstance(item, float) and not math.isfinite(item):
                _fail("non-finite")
        return {"t": "array", "dtype": value.dtype.name, "shape": [int(s) for s in value.shape],
                "data": flat}
    if isinstance(value, dict) and value.get("__dai_array__"):
        return {"t": "array", "dtype": value["dtype"], "shape": value["shape"], "data": value["data"]}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": value}
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("non-finite")
        return {"t": "float", "v": value}
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING:
            _fail("string too long")
        return {"t": "str", "v": value}
    if isinstance(value, (bytes, bytearray)):
        import base64
        return {"t": "bytes", "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {"t": "dict", "v": [[encode_value(key, depth + 1), encode_value(item, depth + 1)]
                                   for key, item in value.items()]}
    if isinstance(value, tuple):
        return {"t": "tuple", "v": [encode_value(item, depth + 1) for item in value]}
    if isinstance(value, list):
        return {"t": "list", "v": [encode_value(item, depth + 1) for item in value]}
    if isinstance(value, (set, frozenset, range)):
        _fail("unsupported container")
    _fail("unsupported object: " + type(value).__name__)


def _result_channel():
    """输出实际值帧。信任边界由每例容器和宿主比较器提供，不使用进程内密钥。"""
    def emit(frame):
        payload = json.dumps(frame, ensure_ascii=False, sort_keys=True)
        sys.stderr.write(RESULT_PREFIX + payload + "\\n")
        sys.stderr.flush()

    return emit


def _load_student_function(path, entry, index):
    """每个用例使用独立模块命名空间：组间/用例间状态互不污染。"""
    spec = importlib.util.spec_from_file_location("dai_user_code_%d" % index, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, entry, None)
    if not callable(function):
        raise SystemExit("entry function missing")
    return function


def main():
    with open("dai_cases.json", "r", encoding="utf-8") as handle:
        job = json.load(handle)
    entry = job["entry_function"]
    student_path = "user_code.py"
    emit = _result_channel()
    cases = job.get("cases")
    if not isinstance(cases, list) or len(cases) != 1:
        emit({"id": "", "status": "invalid_request", "reason": "one isolated case required"})
        return
    case = cases[0]
    case_id = str(case.get("id", ""))
    frame = {"id": case_id}
    try:
        args = decode_value(case["input"])
        if not isinstance(args, tuple):
            raise _RequestError("case input must be a tuple of positional arguments")
    except _RequestError as exc:
        frame["status"] = "invalid_request"
        frame["reason"] = str(exc)[:400]
        emit(frame)
        return
    try:
        function = _load_student_function(student_path, entry, 1)
        value = function(*args)
        frame["status"] = "ok"
        frame["value"] = encode_value(value)
    except SystemExit:
        raise
    except BaseException as exc:  # 异常也要成为有界结果，而不是崩溃
        frame["status"] = "exception"
        frame["exception"] = {"type": type(exc).__name__, "message": str(exc)[:400]}
    emit(frame)


if __name__ == "__main__":
    main()
'''


def build_case_runner_source() -> str:
    """返回自包含 runner 源码（写入工作目录后由容器执行）。"""
    return _RUNNER_SOURCE


def build_cases_payload(entry_function: str, cases: list[dict]) -> dict:
    """构造容器输入：**只有**入口与当前用例输入，不含期望值/分值/计数。"""
    if len(cases) > MAX_CASES:
        raise ValueError(f"用例数超过 {MAX_CASES}")
    cleaned = []
    for case in cases:
        if "expected" in case or "max_score" in case or "weight" in case:
            raise ValueError("容器输入不得包含期望值、分值或权重")
        cleaned.append({"id": str(case["id"]), "input": case["input"]})
    return {"entry_function": entry_function, "cases": cleaned}


def build_case_stream(container_payload: dict) -> list[dict]:
    """容器载荷 → 独立运行用例列表；调用方为每个元素创建新容器。"""
    return [{"id": str(case["id"]), "input": case["input"]}
            for case in container_payload.get("cases", [])]


def to_actual_value(frame: dict) -> dict:
    """runner 帧 → 平台比较器使用的协议对象。"""
    if not isinstance(frame, dict):
        return {"t": None}
    status = frame.get("status")
    if status == "ok":
        value = frame.get("value")
        return value if isinstance(value, dict) else {"t": None}
    if status == "exception":
        info = frame.get("exception") or {}
        return {"t": "exception", "type": info.get("type"), "message": info.get("message")}
    return {"t": "invalid_request", "reason": frame.get("reason")}


def parse_case_frames(protocol_text: str,
                      expected_ids: list[str]) -> tuple[dict[str, dict], list[str]]:
    """校验当前隔离容器的结果帧；返回 ``(frames, errors)``。

    * 每个用例**恰好一帧**：缺失、重复、未知 id 都算破坏；
    * ``status`` 只能是 ok / exception / invalid_request，且对应字段齐备；
    * 单帧与整段输出都有字节上限（防输出洪泛）。
    """
    errors: list[str] = []
    frames: dict[str, dict] = {}
    expected = {str(item) for item in expected_ids}
    text = protocol_text or ""
    if len(text.encode("utf-8", errors="replace")) > MAX_STDOUT_BYTES:
        return {}, ["容器协议输出超过上限"]
    seen_lines = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith(RESULT_PREFIX):
            continue
        seen_lines += 1
        if seen_lines > MAX_FRAMES:
            errors.append("结果帧数量超过上限")
            break
        payload_text = line[len(RESULT_PREFIX):]
        if len(payload_text.encode("utf-8")) > MAX_FRAME_BYTES:
            errors.append("单帧超过上限")
            continue
        try:
            frame = json.loads(payload_text)
        except Exception:
            errors.append("结果帧不是合法 JSON")
            continue
        if not isinstance(frame, dict):
            errors.append("结果帧结构不合法")
            continue
        case_id = str(frame.get("id", ""))
        if case_id not in expected:
            errors.append(f"未知用例结果 {case_id}")
            continue
        if case_id in frames:
            errors.append(f"重复用例结果 {case_id}")
            continue
        status = frame.get("status")
        if status not in ALLOWED_FRAME_STATUSES:
            errors.append(f"用例 {case_id} 结果状态不合法")
            continue
        allowed_fields = {
            "ok": {"id", "status", "value"},
            "exception": {"id", "status", "exception"},
            "invalid_request": {"id", "status", "reason"},
        }[status]
        if set(frame) - allowed_fields:
            errors.append(f"用例 {case_id} 含非结果字段")
            continue
        if status == "ok" and "value" not in frame:
            errors.append(f"用例 {case_id} 缺少返回值")
            continue
        if status == "exception" and not isinstance(frame.get("exception"), dict):
            errors.append(f"用例 {case_id} 缺少异常信息")
            continue
        frames[case_id] = frame
    missing = expected - set(frames)
    if missing:
        errors.append("缺少用例结果: " + ", ".join(sorted(missing)))
    return frames, errors
