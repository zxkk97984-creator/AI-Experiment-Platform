"""批次三 3A：可信值协议、平台侧比较与受限 runner 的结果校验。

这些用例固化信任边界：容器只回传实际值；期望值、分值、权重一律不进入容器；
平台侧负责比较与计分；协议破坏/非法值/超限一律 ``platform_error``（不计 0 分）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.grading_compare import (
    FAILED,
    PASSED,
    PLATFORM_ERROR,
    compare_case,
    summarize_group,
)
from app.services.grading_runner import (
    RESULT_PREFIX,
    build_case_runner_source,
    build_cases_payload,
    parse_case_frames,
    to_actual_value,
)
from app.services.grading_values import (
    MAX_ARRAY_ELEMENTS,
    MAX_CASES_PER_GROUP,
    MAX_VALUE_DEPTH,
    ValueEncodingError,
    decode_value,
    encode_value,
)

BACKEND = Path(__file__).resolve().parents[2]


def _run_isolated_case_containers(workdir: Path, image: str):
    """Opt-in smoke helper that mirrors the production one-case/one-container contract."""
    import tempfile

    job = json.loads((workdir / "dai_cases.json").read_text(encoding="utf-8"))
    output_stdout = []
    output_stderr = []
    returncodes = []
    for case in job["cases"]:
        case_dir = Path(tempfile.mkdtemp(prefix="isolated-case-", dir=workdir))
        case_dir.chmod(0o755)
        try:
            for name in ("dai_case_runner.py", "user_code.py"):
                shutil.copyfile(workdir / name, case_dir / name)
            (case_dir / "dai_cases.json").write_text(json.dumps({
                "entry_function": job["entry_function"], "cases": [case],
            }), encoding="utf-8")
            result = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--read-only", "--tmpfs",
                 "/tmp:exec,size=64m", "--memory", "256m", "--pids-limit", "50",
                 "--user", "1000:1000", "-v", f"{case_dir}:/work:ro", "-w", "/work",
                 image, "python", "dai_case_runner.py"],
                capture_output=True, text=True, timeout=180,
            )
            returncodes.append(result.returncode)
            output_stdout.append(result.stdout)
            output_stderr.append(result.stderr)
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)
    return returncodes, "".join(output_stdout), "".join(output_stderr)


# ─────────────────────────────────────────────────────────────
# 1. 值协议：往返与有界性
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    None, True, False, 0, -17, 3.5, -0.25, "文本", "", b"bytes", bytearray(b"\x00\x01"),
    (1, 2, 3), [1, "a", None], {"a": 1, "b": [2, 3]}, (("nested",), [{"k": (1, 2)}]),
])
def test_codec_roundtrip_scalars_and_containers(value):
    assert decode_value(encode_value(value)) == value


def test_codec_distinguishes_tuple_and_list():
    encoded_tuple = encode_value((1, 2))
    encoded_list = encode_value([1, 2])
    assert encoded_tuple["t"] == "tuple" and encoded_list["t"] == "list"
    assert decode_value(encoded_tuple) == (1, 2)
    assert decode_value(encoded_list) == [1, 2]
    assert decode_value(encoded_tuple) != decode_value(encoded_list), "元组与列表必须保持区分"


def test_codec_encodes_array_via_duck_typing():
    class FakeArray:
        shape = (2, 2)
        dtype = type("dt", (), {"kind": "f", "name": "float64"})()

        def tolist(self):
            return [[1.0, 2.0], [3.0, 4.0]]

    payload = encode_value(FakeArray())
    assert payload == {"t": "array", "dtype": "float64", "shape": [2, 2],
                       "data": [1.0, 2.0, 3.0, 4.0]}
    decoded = decode_value(payload)
    assert decoded["shape"] == [2, 2] and decoded["data"] == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize("value,reason", [
    (float("nan"), "非有限浮点"),
    (float("inf"), "非有限浮点"),
    ({1, 2}, "不支持的值类型"),
    ((item for item in range(3)), "不支持的值类型"),
    (object(), "不支持的值类型"),
    (lambda: None, "不支持的值类型"),
])
def test_codec_rejects_unsupported_values(value, reason):
    with pytest.raises(ValueEncodingError) as error:
        encode_value(value)
    assert reason in str(error.value)


def test_codec_rejects_object_dtype_array():
    class ObjectArray:
        shape = (1,)
        dtype = type("dt", (), {"kind": "O", "name": "object"})()

        def tolist(self):
            return [{"a": 1}]

    with pytest.raises(ValueEncodingError) as error:
        encode_value(ObjectArray())
    assert "dtype" in str(error.value)


def test_codec_enforces_bounds():
    with pytest.raises(ValueEncodingError):
        encode_value(list(range(MAX_ARRAY_ELEMENTS + 1)) and _FakeBigArray())
    with pytest.raises(ValueEncodingError):
        encode_value("x" * 70000)
    with pytest.raises(ValueEncodingError):
        encode_value(10 ** 5000)
    nested = 1
    for _ in range(MAX_VALUE_DEPTH + 2):
        nested = [nested]
    with pytest.raises(ValueEncodingError):
        encode_value(nested)


class _FakeBigArray:
    shape = (MAX_ARRAY_ELEMENTS + 1,)
    dtype = type("dt", (), {"kind": "i", "name": "int64"})()

    def tolist(self):
        return list(range(MAX_ARRAY_ELEMENTS + 1))


@pytest.mark.parametrize("payload", [
    None, 42, {}, {"t": "unknown"}, {"t": "int", "v": "1"}, {"t": "bool", "v": 1},
    {"t": "str", "v": 3}, {"t": "float", "v": float("nan")},
    {"t": "bytes", "v": "not-base64!!"},
    {"t": "list", "v": "no"}, {"t": "dict", "v": [1, 2]},
    {"t": "array", "dtype": "object", "shape": [1], "data": [1]},
    {"t": "array", "dtype": "float64", "shape": [2], "data": [1.0]},
    {"t": "array", "dtype": "float64", "shape": [-1], "data": []},
])
def test_decoder_is_strict(payload):
    with pytest.raises(ValueEncodingError):
        decode_value(payload)


# ─────────────────────────────────────────────────────────────
# 2. 平台侧比较
# ─────────────────────────────────────────────────────────────

def _actual(value):
    return encode_value(value)


def test_literal_rule_uses_python_equality():
    assert compare_case(_actual(3), encode_value(3), {"kind": "literal"})["outcome"] == PASSED
    assert compare_case(_actual((1, 2)), encode_value((1, 2)), {"kind": "literal"})["outcome"] == PASSED
    outcome = compare_case(_actual([1, 2]), encode_value((1, 2)), {"kind": "literal"})
    assert outcome["outcome"] == FAILED, "列表与元组在字面量规则下不相等"


def test_identity_rule_distinguishes_bool_and_int():
    """is True 与 == 1 不同：1 在 identity 规则下不通过。"""
    assert compare_case(_actual(True), encode_value(True), {"kind": "identity"})["outcome"] == PASSED
    assert compare_case(_actual(1), encode_value(True), {"kind": "identity"})["outcome"] == FAILED
    assert compare_case(_actual(None), encode_value(None), {"kind": "identity"})["outcome"] == PASSED


def test_float_tolerance_rule():
    rule = {"kind": "float", "abs_tol": 1e-6, "rel_tol": 0.0}
    assert compare_case(_actual(1.0000001), encode_value(1.0), rule)["outcome"] == PASSED
    assert compare_case(_actual(1.01), encode_value(1.0), rule)["outcome"] == FAILED
    rel = {"kind": "float", "abs_tol": 0.0, "rel_tol": 1e-3}
    assert compare_case(_actual(1000.5), encode_value(1000.0), rel)["outcome"] == PASSED
    assert compare_case(_actual("x"), encode_value(1.0), rule)["outcome"] == PLATFORM_ERROR


def test_array_rule_shape_tolerance_and_dtype():
    rule = {"kind": "array", "shape": "exact", "abs_tol": 1e-9, "rel_tol": 0.0}
    expected = {"t": "array", "dtype": "float64", "shape": [2], "data": [1.0, 2.0]}
    assert compare_case(_actual_payload("float64", [2], [1.0, 2.0]), expected, rule)["outcome"] == PASSED
    assert compare_case(_actual_payload("float64", [2], [1.0, 2.0000000001]),
                        expected, rule)["outcome"] == PASSED
    assert compare_case(_actual_payload("float64", [2], [1.0, 2.5]), expected,
                        rule)["outcome"] == FAILED
    shape = compare_case(_actual_payload("float64", [1, 2], [1.0, 2.0]), expected, rule)
    assert shape["outcome"] == FAILED and "形状" in shape["reason"]
    kind = compare_case(_actual_payload("bool", [2], [True, False]), expected, rule)
    assert kind["outcome"] == PLATFORM_ERROR, "布尔数组与数值数组不可比"


def test_exception_rule():
    rule = {"kind": "exception", "types": ["ValueError"]}
    raised = {"t": "exception", "type": "ValueError", "message": "bad"}
    assert compare_case(raised, encode_value(None), rule)["outcome"] == PASSED
    other = {"t": "exception", "type": "TypeError", "message": "bad"}
    assert compare_case(other, encode_value(None), rule)["outcome"] == FAILED
    assert compare_case(_actual(1), encode_value(None), rule)["outcome"] == FAILED
    custom = {"t": "exception", "type": "MyCustomError", "message": "x"}
    assert compare_case(custom, encode_value(None), rule)["outcome"] == PLATFORM_ERROR
    assert compare_case(raised, encode_value(None),
                        {"kind": "exception", "types": ["NoSuchError"]})["outcome"] == PLATFORM_ERROR


def test_unexpected_exception_is_student_failure():
    frame = {"t": "exception", "type": "ZeroDivisionError", "message": "division by zero"}
    outcome = compare_case(frame, encode_value(4), {"kind": "literal"})
    assert outcome["outcome"] == FAILED


@pytest.mark.parametrize("rule", [
    {}, {"kind": "nope"}, {"kind": "float", "abs_tol": -1},
    {"kind": "array", "shape": "wild"}, {"kind": "exception", "types": []},
])
def test_invalid_rule_is_platform_error(rule):
    outcome = compare_case(_actual(1), encode_value(1), rule)
    assert outcome["outcome"] == PLATFORM_ERROR


def test_malformed_actual_is_platform_error():
    assert compare_case({"t": "array", "dtype": "float64", "shape": [2], "data": [1.0]},
                        encode_value([1.0, 2.0]), {"kind": "literal"})["outcome"] == PLATFORM_ERROR
    assert compare_case("not-a-frame", encode_value(1), {"kind": "literal"})["outcome"] == PLATFORM_ERROR


def test_group_summary_blocks_scoring_on_platform_errors():
    outcomes = [
        {"outcome": PASSED, "reason": None},
        {"outcome": FAILED, "reason": "字面量不相等"},
    ]
    summary = summarize_group(outcomes)
    assert summary == {"passed": 1, "failed": 1, "platform_errors": [], "scored": True}

    blocked = summarize_group(outcomes + [{"outcome": PLATFORM_ERROR, "reason": "协议破坏"}])
    assert blocked["scored"] is False
    assert blocked["platform_errors"] == ["协议破坏"]


def _actual_payload(dtype: str, shape: list[int], data: list) -> dict:
    return {"t": "array", "dtype": dtype, "shape": shape, "data": data}


# ─────────────────────────────────────────────────────────────
# 3. 容器输入与结果帧
# ─────────────────────────────────────────────────────────────

def test_container_payload_excludes_expectations():
    payload = build_cases_payload("solve", [
        {"id": "F1-1", "input": encode_value([1, 2]), "expected": encode_value(3)},
    ]) if False else None
    assert payload is None
    with pytest.raises(ValueError):
        build_cases_payload("solve", [
            {"id": "F1-1", "input": encode_value([1, 2]), "expected": encode_value(3)},
        ])
    with pytest.raises(ValueError):
        build_cases_payload("solve", [{"id": "F1-2", "input": encode_value(1), "weight": 60}])
    ok = build_cases_payload("solve", [{"id": "F1-3", "input": encode_value([1, 2])}])
    assert ok == {"entry_function": "solve", "cases": [{"id": "F1-3", "input": encode_value([1, 2])}]}
    assert "expected" not in json.dumps(ok)


def test_container_payload_is_bounded_by_case_count():
    cases = [{"id": f"F1-{index}", "input": encode_value(index)}
             for index in range(MAX_CASES_PER_GROUP + 1)]
    with pytest.raises(ValueError):
        build_cases_payload("solve", cases)


def _sign(frame: dict) -> str:
    payload = json.dumps(frame, ensure_ascii=False, sort_keys=True)
    return RESULT_PREFIX + payload


def _frame(case_id: str, value=3) -> str:
    return _sign({"id": case_id, "status": "ok", "value": encode_value(value)})


def test_parse_frames_happy_path():
    stdout = "\n".join([_frame("F1-1", 3), "普通 stdout 行", _frame("F1-2", 4)]) + "\n"
    frames, errors = parse_case_frames(stdout, ["F1-1", "F1-2"])
    assert errors == []
    assert set(frames) == {"F1-1", "F1-2"}
    assert to_actual_value(frames["F1-1"]) == encode_value(3)


def test_parse_frames_rejects_missing_duplicate_unknown_and_bad_status():
    frames, errors = parse_case_frames(_frame("F1-1"), ["F1-1", "F1-2"])
    assert any("缺少用例结果" in item for item in errors)

    frames, errors = parse_case_frames(_frame("F1-1") + "\n" + _frame("F1-1"), ["F1-1"])
    assert any("重复" in item for item in errors)

    frames, errors = parse_case_frames(_frame("F9-9"), ["F1-1"])
    assert any("未知用例结果" in item for item in errors)

    bad_status = _sign({"id": "F1-1", "status": "passed"})
    frames, errors = parse_case_frames(bad_status, ["F1-1"])
    assert any("状态不合法" in item for item in errors)

    not_json = RESULT_PREFIX + "{not json"
    frames, errors = parse_case_frames(not_json, ["F1-1"])
    assert any("合法 JSON" in item for item in errors)


def test_parse_frames_bounds_line_and_output_size():
    huge = _sign({"id": "F1-1", "status": "ok", "value": {"t": "str", "v": "x" * 70000}})
    frames, errors = parse_case_frames(huge, ["F1-1"])
    assert any("单帧超过上限" in item for item in errors)

    frames, errors = parse_case_frames("x" * (5 * 1024 * 1024), ["F1-1"])
    assert errors == ["容器协议输出超过上限"]


def test_exception_frame_maps_to_actual_value():
    frame_line = _sign({"id": "F1-1", "status": "exception",
                        "exception": {"type": "ValueError", "message": "boom"}})
    frames, errors = parse_case_frames(frame_line, ["F1-1"])
    assert errors == []
    actual = to_actual_value(frames["F1-1"])
    assert actual == {"t": "exception", "type": "ValueError", "message": "boom"}
    assert compare_case(actual, encode_value(None), {"kind": "exception", "types": ["ValueError"]})["outcome"] == PASSED


def test_invalid_request_frame_never_scores_zero():
    """用例输入不是位置参数元组：容器回传 invalid_request，平台侧按平台错误处理。"""
    line = _sign({"id": "F1-1", "status": "invalid_request",
                  "reason": "case input must be a tuple of positional arguments"})
    frames, errors = parse_case_frames(line, ["F1-1"])
    assert errors == []
    actual = to_actual_value(frames["F1-1"])
    outcome = compare_case(actual, encode_value(1), {"kind": "literal"})
    assert outcome["outcome"] == PLATFORM_ERROR
    summary = summarize_group([outcome])
    assert summary["scored"] is False


def test_runner_source_is_self_contained_and_one_case_only():
    """runner 每次只加载一个用例，且容器内不存在结果签名密钥。"""
    source = build_case_runner_source()
    assert "dai_user_code_" in source, "每个用例必须重新加载独立模块命名空间"
    assert "DAI_RESULT=" in source
    assert "len(cases) != 1" in source
    assert "DAI_RESULT_KEY" not in source and "hmac" not in source
    for forbidden in ("eval(", "exec(", "pickle", "subprocess", "socket"):
        assert forbidden not in source, forbidden


# ─────────────────────────────────────────────────────────────
# 4. 真实容器执行（opt-in）：普通值、异常、数组回退
# ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_runner_executes_in_real_container(tmp_path):
    image = os.environ.get("DAI_JUDGE_IMAGE", "python:3.12-slim")
    cases = {
        "entry_function": "solve",
        # 输入是**位置参数元组**：solve([1, 2])
        "cases": [
            {"id": "F1-1", "input": encode_value(([1, 2],))},
            {"id": "F1-2", "input": encode_value(([-1],))},
            {"id": "F1-3", "input": encode_value(([0],))},
        ],
    }
    (tmp_path / "dai_cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (tmp_path / "dai_case_runner.py").write_text(build_case_runner_source(), encoding="utf-8")
    (tmp_path / "user_code.py").write_text(
        "def solve(values):\n"
        "    total = sum(values)\n"
        "    if total == 0:\n"
        "        raise ValueError('zero')\n"
        "    return total\n",
        encoding="utf-8",
    )
    returncodes, _, protocol = _run_isolated_case_containers(tmp_path, image)
    assert returncodes == [0, 0, 0], returncodes
    frames, errors = parse_case_frames(protocol, ["F1-1", "F1-2", "F1-3"])
    assert errors == [], (errors, protocol)
    assert to_actual_value(frames["F1-1"]) == encode_value(3)
    assert to_actual_value(frames["F1-2"]) == encode_value(-1)
    assert to_actual_value(frames["F1-3"])["type"] == "ValueError"
    # 平台侧按规则判定：前两例通过，第三例按"期望异常"规则通过
    assert compare_case(to_actual_value(frames["F1-1"]), encode_value(3),
                        {"kind": "literal"})["outcome"] == PASSED
    assert compare_case(to_actual_value(frames["F1-3"]), encode_value(None),
                        {"kind": "exception", "types": ["ValueError"]})["outcome"] == PASSED


@pytest.mark.skipif(os.environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_runner_rejects_unsupported_return_in_container(tmp_path):
    """生成器/自定义对象在容器侧被拒绝（回传不支持标记而非伪造成值）。"""
    image = os.environ.get("DAI_JUDGE_IMAGE", "python:3.12-slim")
    (tmp_path / "dai_cases.json").write_text(json.dumps({
        "entry_function": "solve", "cases": [{"id": "F1-1", "input": encode_value(([1],))}],
    }), encoding="utf-8")
    (tmp_path / "dai_case_runner.py").write_text(build_case_runner_source(), encoding="utf-8")
    (tmp_path / "user_code.py").write_text(
        "def solve(values):\n"
        "    return (item for item in values)\n",
        encoding="utf-8",
    )
    returncodes, _, protocol = _run_isolated_case_containers(tmp_path, image)
    assert returncodes == [0], returncodes
    frames, errors = parse_case_frames(protocol, ["F1-1"])
    assert errors == []
    actual = to_actual_value(frames["F1-1"])
    assert actual["t"] == "exception"
    # 不支持的对象不是学生错误，也不是自动成绩
    assert compare_case(actual, encode_value([1]), {"kind": "literal"})["outcome"] == FAILED


def test_exit_code_zero_when_no_cases(tmp_path):
    """空用例集合不产生任何帧（由上层按配置错误处理，不在这里伪造结果）。"""
    frames, errors = parse_case_frames("", [])
    assert frames == {} and errors == []
    assert sys.version_info >= (3, 11)
    assert BACKEND.name == "backend"


@pytest.mark.skipif(os.environ.get("DAI_RUN_DOCKER_SMOKE") != "1",
                    reason="需要真实 Docker；设置 DAI_RUN_DOCKER_SMOKE=1 启用")
def test_runner_encodes_numpy_array_in_container(tmp_path):
    """容器内有 numpy 时，ndarray 按 dtype/shape/data 回传；平台侧按数组规则比较。"""
    image = os.environ.get("DAI_JUDGE_IMAGE", "python:3.12-slim")
    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c",
         "import numpy; print(numpy.__version__)"],
        capture_output=True, text=True, timeout=120,
    )
    if probe.returncode != 0:
        pytest.skip(f"镜像 {image} 内没有 numpy")

    (tmp_path / "dai_cases.json").write_text(json.dumps({
        "entry_function": "solve",
        "cases": [{"id": "F1-1", "input": encode_value(([1.0, 2.0],))}],
    }), encoding="utf-8")
    (tmp_path / "dai_case_runner.py").write_text(build_case_runner_source(), encoding="utf-8")
    (tmp_path / "user_code.py").write_text(
        "import numpy as np\n"
        "def solve(values):\n"
        "    return np.array(values, dtype='float64') * 2\n",
        encoding="utf-8",
    )
    returncodes, _, protocol = _run_isolated_case_containers(tmp_path, image)
    assert returncodes == [0], returncodes
    frames, errors = parse_case_frames(protocol, ["F1-1"])
    assert errors == []
    actual = to_actual_value(frames["F1-1"])
    assert actual["t"] == "array" and actual["dtype"] == "float64" and actual["shape"] == [2]
    assert actual["data"] == [2.0, 4.0]
    rule = {"kind": "array", "shape": "exact", "abs_tol": 1e-9, "rel_tol": 0.0}
    expected = {"t": "array", "dtype": "float64", "shape": [2], "data": [2.0, 4.0]}
    assert compare_case(actual, expected, rule)["outcome"] == PASSED
    wrong = {"t": "array", "dtype": "float64", "shape": [2], "data": [2.0, 4.5]}
    assert compare_case(actual, wrong, rule)["outcome"] == FAILED


# ─────────────────────────────────────────────────────────────
# A11：主机与容器 runner 的类型契约必须逐类型一致
# ─────────────────────────────────────────────────────────────

def _load_runner_module(tmp_path):
    """把生成的 runner 源码当模块加载，直接验证其编解码（不执行学生代码）。"""
    import importlib.util

    source_path = tmp_path / "dai_case_runner.py"
    source_path.write_text(build_case_runner_source(), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("dai_case_runner_under_test", source_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", [
    None, True, False, 0, -17, 3.5, "文本", "", b"bytes", bytearray(b"\x00\x01"),
    (1, 2), [1, "a", None], {"a": 1, "b": [2, 3]}, {"nested": {"x": (1, 2)}},
])
def test_runner_codec_matches_host_contract(tmp_path, value):
    runner = _load_runner_module(tmp_path)
    host_encoded = encode_value(value)
    # 主机编码 → 容器解码：类型与值必须完全一致（bytes 此前无法解码）
    decoded = runner.decode_value(host_encoded)
    assert decoded == value, (value, decoded)
    # 容器编码 → 主机解码：容器必须能编码主机允许的每一种类型（普通 dict 此前缺失）
    container_encoded = runner.encode_value(value)
    assert decode_value(container_encoded) == value, (value, container_encoded)


def test_runner_rejects_object_dtype_and_unsupported_types(tmp_path):
    runner = _load_runner_module(tmp_path)
    with pytest.raises(Exception):
        runner.encode_value(object())
    with pytest.raises(Exception):
        runner.encode_value({1, 2})
    with pytest.raises(Exception):
        runner.decode_value({"t": "array", "dtype": "object", "shape": [1], "data": [1]})
