"""Task 2A：判题结果完整性——矛盾、不完整或不可验证的结果不得产生正式成绩。

本文件覆盖"立即减损"层。注意：本层**不是**完整安全修复（真正的隔离见 Task 2B /
`judge_trusted_runner`），它只保证不可验证的结果不会变成自动成绩。
"""
import pytest

GENUINE_OUTPUT = (
    "collected 2 items\n"
    "\n"
    "..                                                                       [100%]\n"
    "2 passed in 0.02s\n"
)


def _one(counts: dict) -> str:
    import json
    return "DAI_RESULT_JSON=" + json.dumps(counts, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────
# 结果行唯一性：伪造行不得胜出
# ─────────────────────────────────────────────────────────────

def test_forged_result_line_before_genuine_is_rejected():
    """学生代码抢先打印的伪造结果行不得胜出（pytest 失败报告回显会早于插件输出）。"""
    from app.services.deterministic_scoring import parse_group_result

    forged = _one({"passed": 2, "failed": 0, "errors": 0, "skipped": 0})
    genuine = _one({"passed": 0, "failed": 2, "errors": 0, "skipped": 0})
    stdout = (
        "collected 2 items\n"
        f"{forged}\n"
        "FF                                                                       [100%]\n"
        "2 failed in 0.02s\n"
        f"{genuine}\n"
    )

    assert parse_group_result(stdout, "", 1) is None


def test_duplicate_result_lines_rejected():
    """两条结果行（重复/伪造）无法判定归属，必须拒绝。"""
    from app.services.deterministic_scoring import parse_group_result

    line = _one({"passed": 1, "failed": 0, "errors": 0, "skipped": 0})
    stdout = f"collected 1 item\n{line}\n1 passed in 0.01s\n{line}\n"

    assert parse_group_result(stdout, "", 0) is None


# ─────────────────────────────────────────────────────────────
# 退出码与结果的交叉一致性
# ─────────────────────────────────────────────────────────────

def test_exit_zero_without_pytest_summary_is_rejected():
    """rc=0 但没有 pytest 汇总行 ⇒ 进程在 pytest 汇报前退出（如 os._exit(0)）。"""
    from app.services.deterministic_scoring import parse_group_result

    forged = _one({"passed": 3, "failed": 0, "errors": 0, "skipped": 0})
    assert parse_group_result(f"{forged}\n", "", 0) is None


def test_exit_zero_with_failures_is_rejected():
    """rc=0 却报告有失败/错误 ⇒ 自相矛盾。"""
    from app.services.deterministic_scoring import parse_group_result

    counts = {"passed": 1, "failed": 1, "errors": 0, "skipped": 0}
    stdout = f"collected 2 items\n1 failed, 1 passed in 0.02s\n{_one(counts)}\n"

    assert parse_group_result(stdout, "", 0) is None


def test_exit_one_with_all_passed_is_rejected():
    """rc=1 却报告全部通过 ⇒ 自相矛盾。"""
    from app.services.deterministic_scoring import parse_group_result

    counts = {"passed": 2, "failed": 0, "errors": 0, "skipped": 0}
    stdout = f"collected 2 items\n2 passed in 0.02s\n{_one(counts)}\n"

    assert parse_group_result(stdout, "", 1) is None


def test_timeout_never_produces_counts():
    """超时（rc=124）永不计分，即使输出里有结果行与汇总行。"""
    from app.services.deterministic_scoring import parse_group_result

    counts = {"passed": 5, "failed": 0, "errors": 0, "skipped": 0}
    stdout = f"collected 5 items\n5 passed in 0.02s\n{_one(counts)}\n"

    assert parse_group_result(stdout, "", 124) is None


def test_absurd_counts_rejected():
    """天文数字或越界计数必须拒绝（防止伪造满分）。"""
    from app.services.deterministic_scoring import parse_group_result

    stdout = f"collected 1 item\n1 passed in 0.01s\n{_one({'passed': 10 ** 9, 'failed': 0, 'errors': 0, 'skipped': 0})}\n"

    assert parse_group_result(stdout, "", 0) is None


def test_missing_result_line_rejected():
    """只有汇总行没有结果行 ⇒ 拒绝。"""
    from app.services.deterministic_scoring import parse_group_result

    assert parse_group_result("collected 1 item\n1 passed in 0.01s\n", "", 0) is None


def test_negative_counts_rejected():
    """负数计数必须拒绝（原有校验保持）。"""
    from app.services.deterministic_scoring import parse_group_result

    stdout = f"collected 1 item\n1 passed in 0.01s\n{_one({'passed': -1, 'failed': 0, 'errors': 0, 'skipped': 0})}\n"

    assert parse_group_result(stdout, "", 0) is None


# ─────────────────────────────────────────────────────────────
# 正常合法结果必须保持通过（不得把减损做成回归）
# ─────────────────────────────────────────────────────────────

def test_genuine_all_passed_accepted():
    """合法全通过：rc=0 + 汇总行 + 结果行 ⇒ 接受。"""
    from app.services.deterministic_scoring import parse_group_result

    counts = {"passed": 2, "failed": 0, "errors": 0, "skipped": 0}
    stdout = f"collected 2 items\n2 passed in 0.02s\n{_one(counts)}\n"

    assert parse_group_result(stdout, "", 0) == counts


def test_genuine_failures_accepted():
    """合法失败：rc=1 + 汇总行 + 结果行 ⇒ 接受（失败必须仍能计分）。"""
    from app.services.deterministic_scoring import parse_group_result

    counts = {"passed": 1, "failed": 2, "errors": 0, "skipped": 0}
    stdout = f"collected 3 items\n2 failed, 1 passed in 0.02s\n{_one(counts)}\n"

    assert parse_group_result(stdout, "", 1) == counts


def test_skipped_only_group_accepted():
    """全部 skip 的组：rc=0 + skipped 汇总行 ⇒ 接受（由计分器判为无可计分用例）。"""
    from app.services.deterministic_scoring import parse_group_result

    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 2}
    stdout = f"collected 2 items\n2 skipped in 0.02s\n{_one(counts)}\n"

    assert parse_group_result(stdout, "", 0) == counts


# ─────────────────────────────────────────────────────────────
# legacy 路径：不得单凭 rc=0 给满分
# ─────────────────────────────────────────────────────────────

def test_legacy_exit_zero_without_summary_is_not_accepted():
    """legacy 路径 rc=0 但无 pytest 汇总行（os._exit(0) 形态）不得判 accepted/100。"""
    from app.worker.judge_worker import _status_from_pytest

    status, score = _status_from_pytest(0, "", "")
    assert (status, score) != ("accepted", 100)


def test_legacy_genuine_pass_still_accepted():
    """legacy 路径正常通过仍为 accepted/100。"""
    from app.worker.judge_worker import _status_from_pytest

    status, score = _status_from_pytest(0, "1 passed in 0.01s", "")
    assert (status, score) == ("accepted", 100)


def test_legacy_failure_semantics_unchanged():
    """错误类型识别不得被破坏：断言失败=wrong_answer，其他 rc=1=runtime_error，超时=time_limit_exceeded。"""
    from app.worker.judge_worker import _status_from_pytest

    assert _status_from_pytest(1, "E   AssertionError: assert 1 == 2", "")[0] == "wrong_answer"
    assert _status_from_pytest(1, "E   ZeroDivisionError: division by zero", "")[0] == "runtime_error"
    assert _status_from_pytest(124, "", "Execution timed out")[0] == "time_limit_exceeded"


# ─────────────────────────────────────────────────────────────
# 集成：run_test_groups 不得让伪造结果变成 counts
# ─────────────────────────────────────────────────────────────

def _fake_docker(monkeypatch, stdout, stderr="", returncode=0):
    def _run(*_args, **_kwargs):
        return stdout, stderr, returncode, 12
    monkeypatch.setattr("app.worker.judge_worker._run_docker_pytest", _run)


def test_run_test_groups_rejects_forged_full_pass(monkeypatch, tmp_path):
    """两个真实失败的测试组 + 伪造满分行 ⇒ 必须记为 errors 并进 system_errors。"""
    from app.worker.judge_worker import run_test_groups

    forged = _one({"passed": 2, "failed": 0, "errors": 0, "skipped": 0})
    _fake_docker(monkeypatch, f"collected 2 items\n{forged}\n2 failed in 0.02s\n", returncode=1)

    groups = [{"id": "F1", "name": "基础", "dimension": "F", "max_score": 60,
               "tests": "def test_a(): assert False"}]
    out = run_test_groups(tmp_path, tmp_path, "def f(): pass", groups,
                          _settings(), 10, 256)

    assert out["results"]["F1"]["passed"] == 0
    assert out["results"]["F1"]["errors"] == 1
    assert out["system_errors"]


def test_run_test_groups_accepts_genuine_result(monkeypatch, tmp_path):
    """合法结果仍被接受（不得把减损做成回归）。"""
    from app.worker.judge_worker import run_test_groups

    counts = {"passed": 2, "failed": 0, "errors": 0, "skipped": 0}
    _fake_docker(monkeypatch, f"collected 2 items\n2 passed in 0.02s\n{_one(counts)}\n", returncode=0)

    groups = [{"id": "F1", "name": "基础", "dimension": "F", "max_score": 60,
               "tests": "def test_a(): pass"}]
    out = run_test_groups(tmp_path, tmp_path, "def f(): pass", groups,
                          _settings(), 10, 256)

    assert out["results"]["F1"] == counts
    assert out["system_errors"] == []


class _settings:
    """最小 settings 替身：run_test_groups 仅用它取镜像与 CPU 限制。"""
    judge_cpu_limit = 1
    judge_image = "dai-judge-python:latest"
