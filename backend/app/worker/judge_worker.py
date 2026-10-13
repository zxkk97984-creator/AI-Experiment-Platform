"""判题 Worker——消费判题队列 + 考试队列 + AI 评分队列"""
import json as _json
import logging
import math
import os
import secrets
import subprocess
import threading
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import (
    JudgeGradingVersion,
    CodeGrade, ExamAnswer, ExamQuestion, ExamSubmission, JudgeQuestion, QuestionRubric, Submission,
)
from app.services.environment_service import (
    installed_imports_for_version,
    resolve_run_image_ref,
)
from app.services.import_policy import ImportPolicy, classify_imports

logger = logging.getLogger("dai.worker")

EXAM_JUDGE_QUEUE = "judge:exam:queue"


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════

def _make_work_dir(root: Path, prefix: str):
    import shutil as _shutil
    suffix = secrets.token_hex(8)
    workdir = root / f"{prefix}{suffix}"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir, lambda: _shutil.rmtree(workdir, ignore_errors=True)


def _settings_path_or_none(value):
    """把设置项安全转成 Path：仅接受非空 str/Path，其余返回 None。

    防再犯：os.fspath(MagicMock) 在 Python 3.12+ 返回 'MagicMock/mock.xxx/<id>'
    这类伪路径，误传 mock settings 会在仓库根下递归建垃圾目录；
    返回 None 让调用方回退到 TemporaryDirectory。
    """
    if not isinstance(value, (str, Path)):
        return None
    text = os.fspath(value)
    return Path(text) if text else None


def _get_timeout(question: JudgeQuestion, settings: Settings) -> int:
    raw = max(question.time_limit_ms, 1)
    return max(min(math.ceil(raw / 1000), settings.judge_timeout_seconds), 1)


def _status_from_pytest(returncode, stdout, stderr):
    output = f"{stdout}\n{stderr}"
    if returncode == 0:
        # rc=0 必须伴随 pytest 汇总行：否则说明进程在 pytest 汇报之前就退出了
        # （学生代码 os._exit(0) 等），不能据此给满分。
        from app.services.deterministic_scoring import PYTEST_SUMMARY_RE
        if not PYTEST_SUMMARY_RE.search(output):
            return "system_error", 0
        return "accepted", 100
    if returncode == 124:
        return "time_limit_exceeded", 0
    if "AssertionError" in output or "assert " in output:
        return "wrong_answer", 0
    if returncode == 1:
        return "runtime_error", 0
    return "system_error", 0


# ═══════════════════════════════════════════════════════════════
# 传统文件写入（兼容旧判题路径）
# ═══════════════════════════════════════════════════════════════

def _write_submission_files(workdir: Path, submission: Submission, question: JudgeQuestion) -> Path:
    user_code = workdir / "user_code.py"
    test_file = workdir / "test_user_code.py"
    user_code.write_text(submission.code, encoding="utf-8")
    hidden_tests = question.hidden_tests
    if "import user_code" not in hidden_tests and "from user_code" not in hidden_tests:
        hidden_tests = f"from user_code import *\n\n{hidden_tests}"
    test_file.write_text(hidden_tests, encoding="utf-8")
    return test_file


def enqueue_exam_answer(submission_id: int, answer_id: int, question: ExamQuestion):
    from app.services.judge_queue import enqueue_job as _enq
    from app.database import SessionLocal
    with SessionLocal() as db:
        _enq(db, job_type="exam", object_id=answer_id)


# ═══════════════════════════════════════════════════════════════
# Docker 沙箱
# ═══════════════════════════════════════════════════════════════

def _run_docker_argv(workdir: Path, settings: Settings, timeout_seconds: int,
                     argv: list[str], *, memory_limit_mb: int = 256,
                     host_workdir: Path | None = None,
                     image_ref: str | None = None) -> tuple[str, str, int, int]:
    """在隔离沙箱中执行任意容器命令（安全参数与 pytest 路径完全一致）。"""
    host_path = host_workdir if host_workdir is not None else workdir
    container_name = f"dai-judge-{secrets.token_hex(8)}"
    cmd = [
        "docker", "run", "--rm", "--name", container_name,
        "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:exec,size=64m",
        "--cpus", str(settings.judge_cpu_limit),
        "--memory", f"{memory_limit_mb}m",
        "--pids-limit", "50", "--user", "1000:1000",
        "-v", f"{host_path}:/work:ro", "-w", "/work",
        image_ref or settings.judge_image,
        *argv,
    ]
    started = time.perf_counter()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result.stdout, result.stderr, result.returncode, elapsed_ms
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        return "", "Execution timed out", 124, elapsed_ms


def _run_docker_data_runner(workdir: Path, settings: Settings, timeout_seconds: int,
                            memory_limit_mb: int = 256,
                            host_workdir: Path | None = None,
                            image_ref: str | None = None,
                            cases: list[dict] | None = None,
                            heartbeat_callback=None) -> tuple[str, str, int, int]:
    """每个测试用例使用唯一工作目录和独立容器（isolated_case_v2）。

    容器只挂载 runner、学生代码和当前用例输入。expected/权重/评分状态只留在宿主，
    不传 HMAC 密钥，也不使用结果前缀推进输入流。结果帧只携带当前用例的实际值；
    宿主逐帧校验 ID/唯一性后按冻结 expected 比较。
    """
    import shutil

    from app.services.data_grading import RUNNER_FILENAME
    from app.services.grading_runner import (
        CASE_FRAME_MAX_BYTES, MAX_CASES, MAX_STDOUT_BYTES,
    )

    host_path = host_workdir if host_workdir is not None else workdir
    case_list = list(cases or [])
    if not case_list or len(case_list) > MAX_CASES:
        return "", "case count outside supported range", 125, 0
    runner_source = workdir / RUNNER_FILENAME
    student_source = workdir / "user_code.py"
    try:
        job = _json.loads((workdir / "dai_cases.json").read_text(encoding="utf-8"))
        entry_function = job["entry_function"]
    except Exception as exc:
        return "", f"invalid host case manifest: {type(exc).__name__}", 125, 0
    if not runner_source.is_file() or not student_source.is_file():
        return "", "runner or student source missing", 125, 0

    started = time.perf_counter()
    per_case_seconds = max(int(timeout_seconds), 1) + 2
    question_deadline = started + per_case_seconds * len(case_list)
    all_stdout: list[bytes] = []
    all_stderr: list[bytes] = []
    total_stdout = total_stderr = 0
    result_code = 0

    for case in case_list:
        encoded = _json.dumps(case, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > CASE_FRAME_MAX_BYTES:
            return b"".join(all_stdout).decode("utf-8", "replace"), \
                b"".join(all_stderr).decode("utf-8", "replace"), 125, \
                int((time.perf_counter() - started) * 1000)
        remaining = question_deadline - time.perf_counter()
        if remaining <= 0:
            result_code = 124
            break

        relative = f"case-{secrets.token_hex(8)}"
        case_workdir = workdir / relative
        case_hostdir = host_path / relative
        case_workdir.mkdir(mode=0o700)
        container_name = f"dai-case-{secrets.token_hex(8)}"
        try:
            shutil.copyfile(runner_source, case_workdir / RUNNER_FILENAME)
            shutil.copyfile(student_source, case_workdir / "user_code.py")
            (case_workdir / "dai_cases.json").write_bytes(_json.dumps({
                "entry_function": entry_function, "cases": [case],
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            cmd = [
                "docker", "run", "--rm", "--name", container_name,
                "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:exec,size=64m",
                "--cpus", str(settings.judge_cpu_limit),
                "--memory", f"{memory_limit_mb}m",
                "--pids-limit", "50", "--user", "1000:1000",
                "-v", f"{case_hostdir}:/work:ro", "-w", "/work",
                image_ref or settings.judge_image, "python", RUNNER_FILENAME,
            ]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            chunks = {"stdout": [], "stderr": []}
            capped = {"stdout": False, "stderr": False}
            lock = threading.Lock()

            def pump(stream, name, already_total):
                local_total = 0
                read = getattr(stream, "read1", None) or stream.read
                while True:
                    block = read(4096)
                    if not block:
                        break
                    local_total += len(block)
                    if already_total + local_total > MAX_STDOUT_BYTES:
                        with lock:
                            capped[name] = True
                        try:
                            subprocess.run(["docker", "rm", "-f", container_name],
                                           capture_output=True, timeout=10)
                        except Exception:
                            pass
                        try:
                            process.kill()
                        except Exception:
                            pass
                        break
                    with lock:
                        chunks[name].append(block)
                try:
                    stream.close()
                except Exception:
                    pass

            threads = [
                threading.Thread(target=pump, args=(process.stdout, "stdout", total_stdout),
                                 daemon=True),
                threading.Thread(target=pump, args=(process.stderr, "stderr", total_stderr),
                                 daemon=True),
            ]
            for thread in threads:
                thread.start()
            case_deadline = min(question_deadline,
                                time.perf_counter() + per_case_seconds)
            timed_out = False
            try:
                process.wait(timeout=max(case_deadline - time.perf_counter(), 0.05))
            except subprocess.TimeoutExpired:
                timed_out = True
                result_code = 124
                try:
                    subprocess.run(["docker", "rm", "-f", container_name],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
            finally:
                for thread in threads:
                    thread.join(timeout=5)
            out = b"".join(chunks["stdout"])
            err = b"".join(chunks["stderr"])
            total_stdout += len(out)
            total_stderr += len(err)
            all_stdout.append(out)
            all_stderr.append(err)
            if timed_out:
                break
            if capped["stdout"] or capped["stderr"] \
                    or total_stdout > MAX_STDOUT_BYTES or total_stderr > MAX_STDOUT_BYTES:
                result_code = 125
                break
            if process.returncode != 0:
                result_code = process.returncode or 125
                break
            if heartbeat_callback is not None and not heartbeat_callback():
                result_code = 125
                all_stderr.append(b"\njob claim lost between isolated cases\n")
                break
        finally:
            # Only remove this call's unique, direct-child case directory/container.
            shutil.rmtree(case_workdir, ignore_errors=True)

    stdout = b"".join(all_stdout).decode("utf-8", errors="replace")
    stderr = b"".join(all_stderr).decode("utf-8", errors="replace")
    return stdout, stderr, result_code, int((time.perf_counter() - started) * 1000)


def _run_docker_pytest(workdir: Path, settings: Settings, timeout_seconds: int,
                       memory_limit_mb: int = 256, test_filename: str = "test_user_code.py",
                       host_workdir: Path | None = None,
                       extra_args: list[str] | None = None,
                       image_ref: str | None = None) -> tuple[str, str, int, int]:
    """在隔离 Docker 沙箱中运行 pytest（Phase 5：镜像参数使用环境 digest）。

    - image_ref：环境版本不可变镜像引用（image ID 或 repository@sha256 形式）；
      None 仅用于未绑定环境版本的存量兼容路径（exam 路径显式不传，保持旧配置镜像）。
    - 安全参数全部保留：--network none / --cap-drop ALL / no-new-privileges /
      --read-only / tmpfs / pids / cpu / 内存限制 / 非 root UID 1000——只改镜像，不放松安全。
    """
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if extra_args:
        argv.extend(extra_args)
    argv.append(test_filename)
    return _run_docker_argv(workdir, settings, timeout_seconds, argv,
                            memory_limit_mb=memory_limit_mb,
                            host_workdir=host_workdir, image_ref=image_ref)


# ═══════════════════════════════════════════════════════════════
# pytest 结果插件 + 解析
# ═══════════════════════════════════════════════════════════════

PLUGIN_CODE = """import json
COUNTS = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
def pytest_runtest_logreport(report):
    if report.when == "call":
        if report.passed: COUNTS["passed"] += 1
        elif report.failed: COUNTS["failed"] += 1
        elif report.skipped: COUNTS["skipped"] += 1
    elif report.when in ("setup", "teardown") and report.failed:
        COUNTS["errors"] += 1
def pytest_sessionfinish(session, exitstatus):
    print("DAI_RESULT_JSON=" + json.dumps(COUNTS, separators=(",", ":")))
"""


def _parse_group_result(stdout, stderr, returncode) -> dict | None:
    """兼容薄封装——校验逻辑统一走 deterministic_scoring 的唯一实现。

    原先此处与 deterministic_scoring 各有一份"取第一个匹配"的同源拷贝，
    两份都允许伪造行胜出；现在只有一份实现。
    """
    from app.services.deterministic_scoring import parse_group_result
    return parse_group_result(stdout, stderr, returncode)


def _parse_result_json(output: str) -> dict | None:
    """保留旧名以便既有调用/测试引用；语义与 deterministic_scoring.parse_result_json 一致。"""
    from app.services.deterministic_scoring import parse_result_json
    return parse_result_json(output)


# ═══════════════════════════════════════════════════════════════
# 结构化测试组评分
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# Phase 5：运行环境上下文（digest 镜像 + import 诊断）
# ═══════════════════════════════════════════════════════════════

class EnvironmentUnavailableError(Exception):
    """绑定环境版本无可用镜像——fail closed 为 system_error，不扣分。"""

    def __init__(self, message: str = "运行环境暂不可用，本次提交不会扣分，请稍后重试"):
        super().__init__(message)
        self.diagnostic_code = "ENVIRONMENT_IMAGE_MISSING"
        self.message = message


def _submission_env_context(db: Session, submission: Submission) -> tuple[str | None, ImportPolicy | None, set[str]]:
    """从提交快照解析运行环境上下文（计划 8.3）。

    返回 (image_ref, policy, installed_imports)：
    - 未绑定环境版本（存量兼容路径）→ (None, None, None)，判题使用 settings.judge_image；
    - 已绑定但版本无 digest / 不可用 → 抛 EnvironmentUnavailableError（fail closed）。
    历史提交重判：环境取自 Submission 快照字段，不受作业重新发布影响。
    """
    env_id = submission.environment_version_id
    if env_id is None:
        return None, None, None
    try:
        image_ref = resolve_run_image_ref(db, env_id)
    except Exception as exc:
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None) or str(exc)
        raise EnvironmentUnavailableError(message) from exc
    policy = ImportPolicy.from_mode(
        submission.import_policy_mode_snapshot or "unrestricted",
        list(submission.allowed_imports_snapshot or []),
    )
    installed = installed_imports_for_version(db, env_id)
    return image_ref, policy, installed


def _diagnose_submission(submission: Submission, policy: ImportPolicy | None,
                         installed_imports: set[str]):
    """判题前的 import 预检——只分析学生源代码（计划 8.3）。

    返回第一个诊断 ImportDiagnosticRead 或 None（unrestricted / 无违规时不诊断）。
    语法错误返回 None——不伪装成 import 错误，交给原判题输出。
    """
    if policy is None or not policy.restricted:
        return None
    diagnostics = classify_imports(submission.code, policy, installed_imports)
    return diagnostics[0] if diagnostics else None


def _handle_import_diagnostic(db, submission, diag, fail_job) -> Submission | None:
    """按诊断类型落终态。返回终态 submission；None 表示继续正常判题。"""
    if diag is None:
        return None
    details = dict(submission.result_details or {})
    details["diagnostic"] = {
        "code": diag.code, "module": diag.module, "message": diag.message,
    }
    if diag.code == "IMPORT_NOT_ALLOWED":
        # 学生错误：不跑 Docker、不计基础设施重试、明确扣分
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"学生代码使用了不允许的导入: {diag.module}", retryable=False,
                 values={"status": "runtime_error", "score": 0, "result_details": details,
                         "stdout": "", "stderr": diag.message})
        return submission
    # IMPORT_NOT_INSTALLED / ENVIRONMENT_DRIFT：平台配置问题，不扣分，按系统错误策略可重试
    fail_job(db, job_type="assignment", object_id=submission.id,
             error=f"运行环境缺少允许导入的模块: {diag.module}", retryable=True,
             values={"status": "system_error", "score": None, "result_details": details,
                     "stderr": diag.message})
    return submission


def run_test_groups(
    workdir: Path, host_workdir: Path, code: str,
    test_groups: list[dict], settings: Settings,
    timeout_seconds: int, memory_limit_mb: int,
    image_ref: str | None = None, heartbeat=None,
) -> dict:
    results = {}
    system_errors = []
    for group in test_groups:
        # 每组开始前续期存活标记：多测试组判题的墙钟时间可超过 stale 阈值，
        # 没有心跳会让活着的 Worker 被恢复机制误判为崩溃（结果被丢弃并重跑）。
        if heartbeat is not None:
            heartbeat()
        gid = group["id"]
        tests_code = group.get("tests", "")
        if not tests_code:
            system_errors.append(f"测试组 {gid} 没有测试代码")
            results[gid] = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
            continue
        (workdir / "user_code.py").write_text(code, encoding="utf-8")
        (workdir / "dai_result_plugin.py").write_text(PLUGIN_CODE, encoding="utf-8")
        test_content = tests_code
        if "import user_code" not in test_content and "from user_code" not in test_content:
            test_content = f"from user_code import *\n\n{test_content}"
        (workdir / "test_group.py").write_text(test_content, encoding="utf-8")
        try:
            stdout, stderr, returncode, elapsed = _run_docker_pytest(
                workdir, settings, timeout_seconds, memory_limit_mb,
                test_filename="test_group.py", host_workdir=host_workdir,
                extra_args=["-p", "dai_result_plugin"],
                image_ref=image_ref,
            )
        except Exception as exc:
            system_errors.append(f"测试组 {gid} Docker 执行异常: {exc}")
            results[gid] = {"passed": 0, "failed": 0, "errors": 1, "skipped": 0}
            continue
        counts = _parse_group_result(stdout, stderr, returncode)
        if counts is None:
            # 矛盾/不完整/不可验证的结果不得计分，也不得按 0 分计入
            system_errors.append(f"测试组 {gid} 结果不可验证，已拒绝自动计分")
            results[gid] = {"passed": 0, "failed": 0, "errors": 1, "skipped": 0}
        else:
            results[gid] = counts
    return {"results": results, "system_errors": system_errors}


def _calculate_fr(group: dict, counts: dict, sys_errs: list[str]) -> tuple[float, dict]:
    from app.services.deterministic_scoring import calculate_group_score, DeterministicSystemError
    try:
        score = calculate_group_score(group.get("max_score", 0), counts)
    except DeterministicSystemError as exc:
        sys_errs.append(f"测试组 {group['id']}: {exc}")
        return 0.0, {"id": group["id"], "name": group.get("name", ""),
                      "dimension": group.get("dimension"), "max_score": group.get("max_score", 0),
                      "score": 0, "counts": counts, "error": str(exc)}
    return score, {"id": group["id"], "name": group.get("name", ""),
                   "dimension": group.get("dimension"), "max_score": group.get("max_score", 0),
                   "score": score, "counts": counts}


def _calc_fr_scores(groups, results, sys_errs):
    f_total, r_total, details = 0.0, 0.0, []
    for g in groups:
        counts = results.get(g["id"])
        if counts is None:
            sys_errs.append(f"测试组 {g['id']} 缺少结果")
            continue
        s, d = _calculate_fr(g, counts, sys_errs)
        details.append(d)
        if g.get("dimension") == "F":
            f_total += s
        elif g.get("dimension") == "R":
            r_total += s
    return round(f_total, 4), round(r_total, 4), details


# ═══════════════════════════════════════════════════════════════
# 作业判题
# ═══════════════════════════════════════════════════════════════

def _legacy_judge_submission(db, redis_client, settings, submission, question, workdir, host_workdir, timeout_s, mem_mb):
    """传统判题路径：隐藏测试全过/不过 → accepted/0"""
    from app.services.judge_queue import complete_job, fail_job
    if not question.hidden_tests or not question.hidden_tests.strip():
        # 永久配置错误：立即终态，不消耗重试
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="缺少隐藏测试", retryable=False,
                 values={"status": "system_error", "score": None})
        return submission
    # Phase 5：解析提交快照环境（digest 镜像 + import 策略），fail closed
    try:
        image_ref, policy, installed = _submission_env_context(db, submission)
    except EnvironmentUnavailableError as exc:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=exc.message, retryable=True,
                 values={"status": "system_error", "score": None,
                         "result_details": {
                             "diagnostic": {"code": "ENVIRONMENT_IMAGE_MISSING",
                                            "module": "", "message": exc.message},
                         }})
        return submission
    # import 预检：只分析学生源代码；IMPORT_NOT_ALLOWED 学生错误，IMPORT_NOT_INSTALLED 平台配置
    terminal = _handle_import_diagnostic(db, submission,
                                         _diagnose_submission(submission, policy, installed),
                                         fail_job)
    if terminal is not None:
        return terminal
    _write_submission_files(workdir, submission, question)
    try:
        stdout, stderr, returncode, elapsed = _run_docker_pytest(
            workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir,
            image_ref=image_ref)
    except Exception as e:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"Docker 判题失败: {e}", retryable=True,
                 values={"status": "system_error", "score": None,
                         "stderr": f"Docker 判题失败: {e}"})
        return submission

    final_status, score = _status_from_pytest(returncode, stdout, stderr)
    if final_status == "system_error":
        fail_job(
            db,
            job_type="assignment",
            object_id=submission.id,
            error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
            retryable=True,
            values={"status": "system_error", "score": None,
                    "stdout": stdout[-8000:], "stderr": stderr[-8000:],
                    "execution_time_ms": elapsed,
                    "result_details": {"returncode": returncode}},
        )
        return submission
    complete_job(
        db, job_type="assignment", object_id=submission.id, score=score,
        values={"status": final_status, "stdout": stdout[-8000:],
                "stderr": stderr[-8000:], "execution_time_ms": elapsed,
                "result_details": {"returncode": returncode}},
    )
    return submission


def _make_heartbeat(db, job_type: str, object_id: int):
    """构造 run_test_groups 的续期回调（以本次认领的 token 为条件）。"""
    def _beat():
        from app.services.judge_queue import heartbeat_job
        heartbeat_job(db, job_type=job_type, object_id=object_id)
    return _beat


def _test_counts_from_group_results(results: dict) -> tuple[int, int]:
    """归一化 tests_passed/tests_total（纯函数：结果字段必须经栅栏写入，不直接改 ORM 对象）。"""
    passed = sum(int(counts.get("passed", 0)) for counts in results.values())
    total = sum(
        int(counts.get(key, 0))
        for counts in results.values()
        for key in ("passed", "failed", "errors", "skipped")
    )
    return passed, total


def _set_test_counts_from_group_results(submission, results: dict) -> None:
    """把测试组结果归一化写入 tests_passed/tests_total，供工作台最近提交表直读。"""
    submission.tests_passed, submission.tests_total = _test_counts_from_group_results(results)


def _claim_id(claim_token: str | None) -> str | None:
    """认领标识的证据形式：只在库内保留短哈希，便于关联而不泄露原始 token。"""
    if not claim_token:
        return None
    import hashlib
    return hashlib.sha256(claim_token.encode("utf-8")).hexdigest()[:16]


def _complete_with_code_grade(db, *, job_type: str, object_id: int, values: dict,
                              target_field: str, target_id: int, code_grade_factory):
    """受认领保护地把**主结果与 CodeGrade** 写进同一个事务。

    这是批次一 2.2 的收尾操作：原先 Worker 先 ``complete_job`` 提交，再另起事务
    ``db.add(cg); db.commit()``，于是"已失去认领"的旧 Worker 虽然写不回主结果，
    却照样能创建评分记录并推送 AI（探针 stale_worker_after_denied_complete 复现）。

    返回 ``(outcome, code_grade)``：

    * ``("lost_claim", None)``：已失去认领 → 主结果、CodeGrade 都未写入；调用方必须
      **立即停止**（不建评分记录、不推 AI、不推进考试汇总）。
    * ``("created", cg)`` / ``("existing", cg)``：主结果与评分记录已在同一事务提交。

    关联写入失败（唯一约束冲突等）→ 回滚整个事务并抛出异常：主结果不得落库。
    """
    from app.services.judge_queue import (
        apply_guarded_result,
        commit_guarded_result,
        current_claim_token,
    )

    token = current_claim_token(db, job_type, object_id)
    if not token:
        logger.warning("拒绝无认领的结果写入: %s:%s", job_type, object_id)
        db.rollback()
        return "lost_claim", None

    # 终态必须与结果字段一起写入：缺少 grading_status 会把行留在 running
    # （认领已释放、状态未终态），既无法被恢复扫描识别，也让前端永久判题中。
    payload = dict(values)
    payload.setdefault("grading_status", "completed")
    if not apply_guarded_result(db, job_type=job_type, object_id=object_id, values=payload):
        db.rollback()
        return "lost_claim", None

    # 同一事务内读取刚推进的修订号，写入评分记录的来源证据
    model = Submission if job_type == "assignment" else ExamAnswer
    result_revision = db.scalar(select(model.result_revision).where(model.id == object_id))
    bound_version = db.scalar(select(model.grading_version_id).where(model.id == object_id))
    source = {"judge_claim_id": _claim_id(token), "result_revision": result_revision,
              "grading_version_id": bound_version}

    existing = db.scalar(
        select(CodeGrade).where(getattr(CodeGrade, target_field) == target_id)
    )
    if existing is not None:
        # 已存在同源评分记录：不无条件复用另一代次的记录，只补写本次来源证据
        details = dict(existing.deterministic_details or {})
        details.setdefault("source", source)
        existing.deterministic_details = details
        commit_guarded_result(db, job_type=job_type, object_id=object_id)
        return "existing", existing

    code_grade = code_grade_factory(source)
    db.add(code_grade)
    try:
        db.flush()
    except Exception:
        db.rollback()
        logger.exception("评分记录写入失败，已回滚主结果: %s:%s", job_type, object_id)
        raise
    commit_guarded_result(db, job_type=job_type, object_id=object_id)
    return "created", code_grade


def _enqueue_ai_after_commit(db, redis_client, code_grade) -> bool:
    """提交之后才通知 AI 队列；通知丢失由数据库 pending/queued 恢复接管。"""
    if code_grade is None or code_grade.status != "pending":
        return False
    from app.services.ai_grading_queue import enqueue_ai_grade
    return enqueue_ai_grade(db, redis_client, code_grade.id)


def _v1_judge_submission(db, redis_client, settings, submission, question, workdir, host_workdir, timeout_s, mem_mb):
    """V1 评分路径：运行测试组 → 创建 CodeGrade → 入队 AI。

    配置完整性错误（缺测试组/缺锁定 Rubric/测试组缺 tests 代码）是永久错误：
    立即 system_error 终态，不消耗重试；仅 Docker/基础设施异常可重试。
    """
    from app.services.judge_queue import complete_job, fail_job

    test_groups = question.test_groups or []
    if not test_groups:
        # 永久配置错误：立即终态，不消耗重试
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="shadow/active 需要测试组", retryable=False,
                 values={"status": "system_error", "score": None})
        return submission

    # 查找锁定 Rubric（缺失则系统错误，不能让学生丢分）
    locked = db.scalar(
        select(QuestionRubric).where(
            QuestionRubric.judge_question_id == question.id,
            QuestionRubric.status == "locked",
        ).order_by(QuestionRubric.version.desc()).limit(1)
    )
    if locked is None:
        # 永久配置错误：立即终态，不消耗重试
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="shadow/active 缺少锁定 Rubric", retryable=False,
                 values={"status": "system_error", "score": None})
        return submission

    # 配置完整性：测试组必须有 tests 代码（永久错误，不重试、不进入 Docker）
    missing_tests = [g.get("id") for g in test_groups
                     if not (g.get("tests") or "").strip()]
    if missing_tests:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="测试组缺少测试代码: " + ",".join(str(x) for x in missing_tests),
                 retryable=False,
                 values={"status": "system_error", "score": None})
        return submission

    # Phase 5：解析提交快照环境（digest 镜像 + import 策略），fail closed
    try:
        image_ref, policy, installed = _submission_env_context(db, submission)
    except EnvironmentUnavailableError as exc:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=exc.message, retryable=True,
                 values={"status": "system_error", "score": None,
                         "result_details": {
                             "diagnostic": {"code": "ENVIRONMENT_IMAGE_MISSING",
                                            "module": "", "message": exc.message},
                         }})
        return submission
    # import 预检：只分析学生源代码；IMPORT_NOT_ALLOWED 学生错误，IMPORT_NOT_INSTALLED 平台配置
    terminal = _handle_import_diagnostic(db, submission,
                                         _diagnose_submission(submission, policy, installed),
                                         fail_job)
    if terminal is not None:
        return terminal

    result = run_test_groups(workdir, host_workdir, submission.code, test_groups,
                             settings, timeout_s, mem_mb, image_ref=image_ref,
                             heartbeat=_make_heartbeat(db, "assignment", submission.id))
    all_errs = list(result["system_errors"])
    f_score, r_score, details = _calc_fr_scores(test_groups, result["results"], all_errs)

    # 系统错误（Docker 执行异常/结果解析失败——配置缺失已在前面拦截）→ 不创建 CodeGrade，退回重试，不扣分
    if all_errs:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="; ".join(all_errs), retryable=True,
                 values={"status": "system_error", "score": None,
                         "result_details": {"groups": details, "system_errors": all_errs}})
        return submission

    # shadow: 保留旧规则成绩（同时跑 hidden_tests）
    if question.grading_mode == "shadow":
        _write_submission_files(workdir, submission, question)
        try:
            stdout, stderr, returncode, elapsed = _run_docker_pytest(
                workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir,
                image_ref=image_ref)
        except Exception as e:
            fail_job(db, job_type="assignment", object_id=submission.id,
                     error=f"Docker 判题失败: {e}", retryable=True,
                     values={"status": "system_error", "score": None,
                             "stderr": f"Docker 判题失败: {e}"})
            return submission
        legacy_status, legacy_score = _status_from_pytest(returncode, stdout, stderr)
        if legacy_status == "system_error":
            fail_job(
                db,
                job_type="assignment",
                object_id=submission.id,
                error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
                retryable=True,
                values={"status": "system_error", "score": None,
                        "stdout": stdout[-8000:], "stderr": stderr[-8000:],
                        "execution_time_ms": elapsed,
                        "result_details": {
                            "groups": details,
                            "system_errors": [f"Docker exit={returncode}"],
                            "f_score": f_score,
                            "r_score": r_score,
                        }},
            )
            return submission
        shadow_values = {"status": legacy_status, "score": legacy_score,
                         "stdout": stdout[-8000:], "stderr": stderr[-8000:],
                         "execution_time_ms": elapsed}
    else:
        # active: Docker 完成后不写正式分，等 AI 完成后再合分
        shadow_values = {"status": "running", "score": None}

    result_details = {"groups": details, "system_errors": all_errs,
                      "f_score": f_score, "r_score": r_score}
    tests_passed, tests_total = _test_counts_from_group_results(result["results"])
    locked_id = locked.id
    mode = question.grading_mode

    def _make_code_grade(source: dict) -> CodeGrade:
        return CodeGrade(
            submission_id=submission.id, rubric_id=locked_id,
            mode=mode, status="pending",
            functional_score=f_score, robustness_score=r_score,
            deterministic_details={"groups": details, "system_errors": all_errs,
                                   "source": source},
        )

    # 主结果与评分记录同一受认领事务：失去代次立即停止（不建 CG、不推 AI）
    # 不传 score：shadow 已设 legacy 分，active 保持 None 等 AI；这里一次写入全部结果字段
    outcome, code_grade = _complete_with_code_grade(
        db, job_type="assignment", object_id=submission.id,
        values={**shadow_values, "result_details": result_details,
                "tests_passed": tests_passed, "tests_total": tests_total},
        target_field="submission_id", target_id=submission.id,
        code_grade_factory=_make_code_grade,
    )
    if outcome == "lost_claim":
        return submission

    # 提交之后才通知 AI 队列（丢失由 DB pending/queued 恢复接管）
    _enqueue_ai_after_commit(db, redis_client, code_grade)
    return submission


def _data_v1_judge_submission(db, redis_client, settings, submission, question,
                              workdir, host_workdir, timeout_s, mem_mb, version):
    """data_v1 评分路径（批次三 3E）：容器只跑用例并回传实际值，平台侧比较与计分。

    失败关闭（fail closed）规则：

    * 版本配置不合法（缺组/缺用例/非法 dimension）→ 终态 system_error，不重试、不计分；
    * 平台错误（结果帧缺失/重复/非法值/超限）→ 终态 system_error，**不计 0 分**，
      由教师修正配置后用受控重评或人工评分处理；
    * Docker/基础设施异常 → 可重试（不消耗次数）；
    * 绑定了 data_v1 版本但题目不是 active 模式 → 终态 system_error（配置不一致，
      绝不静默改用 shadow/legacy 语义）。
    """
    from app.services.data_grading import (
        DataGradingConfigError,
        grade_output,
        plan_cases,
        write_runner_files,
    )
    from app.services.judge_queue import fail_job, heartbeat_job

    mode = version.grading_mode_snapshot or "legacy"
    if mode not in ("legacy", "shadow", "active"):
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"未知的评分策略（{mode}）",
                 retryable=False,
                 values={"status": "system_error", "score": None,
                         "result_details": {"diagnostic": {
                             "code": "DATA_VERSION_MODE_MISMATCH",
                             "module": "", "message": "评分版本与评分模式不一致，请教师确认"}}})
        return submission

    # 需要 AI 的模式必须使用版本绑定且锁定的 Rubric。
    if mode in ("shadow", "active") and version.rubric_id is None:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="data_v1 评分版本缺少绑定 Rubric", retryable=False,
                 values={"status": "system_error", "score": None,
                         "result_details": {"diagnostic": {
                             "code": "RUBRIC_REQUIRED", "module": "",
                             "message": "请为该评分版本绑定并锁定 Rubric 后再启用"}}})
        return submission
    locked_rubric = db.get(QuestionRubric, version.rubric_id) if version.rubric_id else None
    if mode in ("shadow", "active") and (locked_rubric is None
                                           or locked_rubric.status != "locked"):
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="data_v1 评分版本绑定的 Rubric 未锁定", retryable=False,
                 values={"status": "system_error", "score": None,
                         "result_details": {"diagnostic": {
                             "code": "RUBRIC_REQUIRED", "module": "",
                             "message": "绑定的 Rubric 不存在或未锁定，请教师确认"}}})
        return submission

    try:
        container_payload, expectations = plan_cases(version)
    except DataGradingConfigError as exc:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"评分版本配置错误: {exc}", retryable=False,
                 values={"status": "system_error", "score": None,
                         "result_details": {"platform_errors": [str(exc)]}})
        return submission

    try:
        image_ref, policy, installed = _submission_env_context(db, submission)
    except EnvironmentUnavailableError as exc:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=exc.message, retryable=True,
                 values={"status": "system_error", "score": None,
                         "result_details": {"diagnostic": {
                             "code": "ENVIRONMENT_IMAGE_MISSING", "module": "",
                             "message": exc.message}}})
        return submission
    terminal = _handle_import_diagnostic(db, submission,
                                         _diagnose_submission(submission, policy, installed),
                                         fail_job)
    if terminal is not None:
        return terminal

    write_runner_files(workdir, entry_function=version.entry_function,
                       container_payload=container_payload, student_code=submission.code)
    from app.services.grading_runner import build_case_stream

    try:
        stdout, stderr, returncode, elapsed = _run_docker_data_runner(
            workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir,
            image_ref=image_ref, cases=build_case_stream(container_payload),
            heartbeat_callback=lambda: heartbeat_job(
                db, job_type="assignment", object_id=submission.id))
    except Exception as exc:  # noqa: BLE001 - Docker/基础设施异常可重试
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"Docker 判题失败: {exc}", retryable=True,
                 values={"status": "system_error", "score": None,
                         "stderr": f"Docker 判题失败: {exc}"})
        return submission

    if returncode != 0:
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error=f"data_v1 runner 退出码 {returncode}: {stderr[-500:]}",
                 retryable=True,
                 values={"status": "system_error", "score": None,
                         "stdout": stdout[-8000:], "stderr": stderr[-8000:],
                         "execution_time_ms": elapsed})
        return submission

    # 结果帧走 stderr（签名协议通道）；stdout 只作学生输出/诊断留档
    graded = grade_output(stderr, expectations, entry_function=version.entry_function,
                          diagnostic_text=stdout)
    if graded["needs_review"]:
        # 平台问题：终态但不计分，由教师修正配置后受控重评/人工评分
        fail_job(db, job_type="assignment", object_id=submission.id,
                 error="data_v1 结果不可验证: " + "; ".join(graded["platform_errors"][:5]),
                 retryable=False,
                 values={"status": "system_error", "score": None,
                         "stdout": stdout[-8000:], "stderr": stderr[-8000:],
                         "execution_time_ms": elapsed,
                         "result_details": {
                             "groups": graded["groups"],
                             "platform_errors": graded["platform_errors"],
                             "grading_version_id": version.id,
                             "f_score": graded["f_score"], "r_score": graded["r_score"]}})
        return submission

    result_details = {"groups": graded["groups"], "system_errors": [],
                      "grading_version_id": version.id,
                      "f_score": graded["f_score"], "r_score": graded["r_score"],
                      "binary_score": graded["binary_score"]}
    if mode == "legacy":
        from app.services.judge_queue import complete_job

        complete_job(db, job_type="assignment", object_id=submission.id, values={
            "status": "graded", "score": graded["binary_score"],
            "stdout": stdout[-8000:], "stderr": stderr[-8000:],
            "execution_time_ms": elapsed,
            "tests_passed": graded["tests_passed"], "tests_total": graded["tests_total"],
            "result_details": result_details,
        })
        return submission

    locked_id = version.rubric_id
    f_score, r_score = graded["f_score"], graded["r_score"]

    def _make_code_grade(source: dict) -> CodeGrade:
        return CodeGrade(
            submission_id=submission.id, rubric_id=locked_id,
            mode=mode, status="pending",
            functional_score=f_score, robustness_score=r_score,
            deterministic_details={"groups": graded["groups"], "source": source,
                                   "grading_version_id": version.id},
        )

    outcome, code_grade = _complete_with_code_grade(
        db, job_type="assignment", object_id=submission.id,
        values={"status": "graded" if mode == "shadow" else "running",
                "score": graded["binary_score"] if mode == "shadow" else None,
                "stdout": stdout[-8000:],
                "stderr": stderr[-8000:], "execution_time_ms": elapsed,
                "result_details": result_details,
                "tests_passed": graded["tests_passed"], "tests_total": graded["tests_total"]},
        target_field="submission_id", target_id=submission.id,
        code_grade_factory=_make_code_grade,
    )
    if outcome == "lost_claim":
        return submission
    _enqueue_ai_after_commit(db, redis_client, code_grade)
    return submission


def _resolve_bound_version(db: Session, submission: Submission, question: JudgeQuestion):
    """返回 ``(version, action)``；没有可验证数据版本时一律转人工。

    A08 安全降级契约：

    * 绑定 data_v1 → 走可信数据判题；
    * 绑定 manual_review（或绑定指向不存在的版本）→ **终态转人工，不执行旧评分器**；
    * 未绑定但题目已启用任何版本 → fail-closed（绝不静默用最新配置判分）；
    * 无绑定版本（包括历史题目）→ 人工复核，不再运行旧 pytest 正式评分器。
    """
    if submission.grading_version_id is not None:
        version = db.get(JudgeGradingVersion, submission.grading_version_id)
        if version is None:
            return None, "missing_baseline"
        if version.execution_mode == "data_v1":
            payload = version.payload or {}
            if payload.get("schema_version") == 2 \
                    and payload.get("execution_model") == "isolated_case_v2":
                return version, "data_v1"
            return version, "manual_review"
        return version, "manual_review"
    from app.services.grading_version_service import active_version_for

    active = active_version_for(db, question_kind="assignment", question_id=question.id)
    if active is not None:
        return None, "missing_baseline"
    return None, "missing_baseline"


def process_submission(db: Session, redis_client, settings: Settings, submission_id: int) -> Submission:
    from app.services.judge_queue import claim_job, fail_job

    submission = db.get(Submission, submission_id)
    if not submission:
        return None
    if submission.grading_status == "completed":
        return submission

    try:
        # status="running" 与认领在同一条条件 UPDATE 内落地：失去认领的旧 Worker
        # 不能再把已完成提交的展示状态改回"判题中"。
        if not claim_job(db, job_type="assignment", object_id=submission_id,
                         extra_values={"status": "running"}):
            return submission

        question = db.get(JudgeQuestion, submission.question_id)
        if not question:
            fail_job(db, job_type="assignment", object_id=submission_id,
                     error="题目不存在", retryable=False,
                     values={"status": "system_error", "score": None})
            return submission

        _work_root = _settings_path_or_none(settings.judge_work_dir)
        _host_root = _settings_path_or_none(settings.judge_host_work_dir)
        _cleanup = None
        try:
            if _work_root:
                workdir, _cleanup = _make_work_dir(_work_root, "dai-judge-")
                host_workdir = _host_root / workdir.relative_to(_work_root) if _host_root else workdir
            else:
                _temp = tempfile.TemporaryDirectory(prefix="dai-judge-")
                workdir = Path(_temp.name)
                host_workdir = workdir
                _cleanup = lambda: _temp.cleanup()

            timeout_s = _get_timeout(question, settings)
            mem_mb = question.memory_limit_mb or settings.judge_memory_limit_mb

            bound_version, bound_action = _resolve_bound_version(db, submission, question)
            if bound_action == "missing_baseline":
                from app.services.judge_queue import complete_job

                complete_job(db, job_type="assignment", object_id=submission_id, values={
                    "status": "review_required", "score": None,
                    "result_details": {"manual_review": {
                        "required": True,
                        "reason": "该提交没有可验证的评分版本基线，请教师人工评定",
                    }, "diagnostic": {
                        "code": "MISSING_GRADING_BASELINE", "module": "",
                        "message": "缺少可信评分版本，已转人工复核"}},
                })
                return submission
            if bound_action == "manual_review":
                # 该版本或旧协议需要教师明确确认；不得调用旧 pytest 评分器。
                from app.services.judge_queue import complete_job

                complete_job(db, job_type="assignment", object_id=submission_id, values={
                    "status": "review_required", "score": None,
                    "result_details": {
                        "grading_version_id": bound_version.id if bound_version else None,
                        "manual_review": {"required": True, "reason": "该版本需教师人工复核"},
                        "diagnostic": {"code": "MANUAL_REVIEW_REQUIRED", "module": "",
                                       "message": "该题使用人工复核评分版本，请教师人工给分"}},
                })
                return submission
            if bound_action == "data_v1":
                result = _data_v1_judge_submission(
                    db, redis_client, settings, submission, question,
                    workdir, host_workdir, timeout_s, mem_mb, bound_version)
                db.refresh(submission)
                redis_client.setex(f"judge:result:{submission.id}", 3600,
                                   getattr(submission, 'status', 'unknown'))
                logger.info("Submission %s data_v1 判题完成: %s", submission_id,
                            getattr(submission, 'status', '?'))
                return result

            gmode = getattr(question, 'grading_mode', 'legacy') or 'legacy'
            if gmode == "legacy":
                result = _legacy_judge_submission(
                    db, redis_client, settings, submission, question,
                    workdir, host_workdir, timeout_s, mem_mb)
            else:
                result = _v1_judge_submission(
                    db, redis_client, settings, submission, question,
                    workdir, host_workdir, timeout_s, mem_mb)

            db.refresh(submission) if result else None
            redis_client.setex(f"judge:result:{submission.id}", 3600, getattr(submission, 'status', 'unknown'))
            logger.info("Submission %s 判题完成: %s", submission_id, getattr(submission, 'status', '?'))
            return result

        finally:
            if _cleanup:
                _cleanup()
            # A15：Worker 没有 lifespan，每个任务结束时把指标写入 Redis
            from app.services import op_metrics as _op_metrics
            _op_metrics.flush_for_worker()

    except Exception:
        logger.exception("Submission %s 未知异常", submission_id)
        try:
            fail_job(db, job_type="assignment", object_id=submission_id,
                     error="Worker 未知异常", retryable=True)
        except Exception:
            pass
        return submission


# ═══════════════════════════════════════════════════════════════
# 考试判题
# ═══════════════════════════════════════════════════════════════

def _maybe_finalize_exam(submission_id: int, db: Session):
    from app.services.exam_grading import finalize_if_ready
    finalize_if_ready(submission_id, db)


def _resolve_exam_bound_version(db: Session, answer: ExamAnswer, question: ExamQuestion):
    """考试答案的版本解析，返回 ``(version, action)``（语义同作业侧 A08）。"""
    if answer.grading_version_id is not None:
        version = db.get(JudgeGradingVersion, answer.grading_version_id)
        if version is None:
            return None, "missing_baseline"
        if version.execution_mode == "data_v1":
            payload = version.payload or {}
            if payload.get("schema_version") == 2 \
                    and payload.get("execution_model") == "isolated_case_v2":
                return version, "data_v1"
            return version, "manual_review"
        return version, "manual_review"
    submission = db.get(ExamSubmission, answer.submission_id)
    mapping = (submission.grading_version_map or {}) if submission is not None else {}
    from app.services.grading_version_service import version_id_from_map_entry

    mapped_entry = mapping.get(str(answer.question_id))
    mapped = version_id_from_map_entry(mapped_entry)
    if mapped:
        return None, "missing_baseline"
    return None, "missing_baseline"


def _data_v1_judge_exam_answer(db, redis_client, settings, answer, question,
                               workdir, host_workdir, timeout_s, mem_mb, version):
    """考试编程题的 data_v1 路径（批次三 3E）。

    与作业路径同源（同一 ``data_grading`` 编排与 runner），但持久化字段不同：
    ``exam_answers`` 没有 stdout/stderr 列，原始输出以有界片段存入 ``result_details``；
    完成后按既有语义调用 ``_maybe_finalize_exam``（active 模式等 AI 合分）。
    """
    from app.services.data_grading import (
        DataGradingConfigError,
        grade_output,
        plan_cases,
        write_runner_files,
    )
    from app.services.judge_queue import fail_job, heartbeat_job

    mode = version.grading_mode_snapshot or "legacy"
    if mode not in ("legacy", "shadow", "active"):
        fail_job(db, job_type="exam", object_id=answer.id,
                 error=f"未知的评分策略（{mode}）",
                 retryable=False,
                 values={"score": None, "system_error": "评分版本与评分模式不一致",
                         "result_details": {"diagnostic": {
                             "code": "DATA_VERSION_MODE_MISMATCH", "module": "",
                             "message": "评分版本与评分模式不一致，请教师确认"}}})
        _maybe_finalize_exam(answer.submission_id, db)
        return answer

    locked_rubric = db.get(QuestionRubric, version.rubric_id) if version.rubric_id else None
    if mode in ("shadow", "active") and (locked_rubric is None
                                           or locked_rubric.status != "locked"):
        fail_job(db, job_type="exam", object_id=answer.id,
                 error="data_v1 评分版本缺少已锁定的 Rubric", retryable=False,
                 values={"score": None, "system_error": "缺少已锁定 Rubric",
                         "result_details": {"diagnostic": {
                             "code": "RUBRIC_REQUIRED", "module": "",
                             "message": "请为该评分版本绑定并锁定 Rubric 后再启用"}}})
        _maybe_finalize_exam(answer.submission_id, db)
        return answer

    try:
        container_payload, expectations = plan_cases(version)
    except DataGradingConfigError as exc:
        fail_job(db, job_type="exam", object_id=answer.id,
                 error=f"评分版本配置错误: {exc}", retryable=False,
                 values={"score": None, "system_error": f"评分版本配置错误: {exc}",
                         "result_details": {"platform_errors": [str(exc)]}})
        _maybe_finalize_exam(answer.submission_id, db)
        return answer

    write_runner_files(workdir, entry_function=version.entry_function,
                       container_payload=container_payload,
                       student_code=answer.code_answer or "")
    from app.services.grading_runner import build_case_stream

    try:
        stdout, stderr, returncode, elapsed = _run_docker_data_runner(
            workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir,
            cases=build_case_stream(container_payload),
            heartbeat_callback=lambda: heartbeat_job(
                db, job_type="exam", object_id=answer.id))
    except Exception as exc:  # noqa: BLE001 - 基础设施异常可重试
        fail_job(db, job_type="exam", object_id=answer.id,
                 error=f"Docker 判题失败: {exc}", retryable=True,
                 values={"score": None, "system_error": f"Docker 判题失败: {exc}"})
        return answer

    if returncode != 0:
        fail_job(db, job_type="exam", object_id=answer.id,
                 error=f"data_v1 runner 退出码 {returncode}: {stderr[-500:]}",
                 retryable=True,
                 values={"score": None, "system_error": f"runner 退出码 {returncode}",
                         "result_details": {"runner_stderr_tail": stderr[-2000:]}})
        return answer

    graded = grade_output(stderr, expectations, entry_function=version.entry_function,
                          diagnostic_text=stdout)
    if graded["needs_review"]:
        fail_job(db, job_type="exam", object_id=answer.id,
                 error="data_v1 结果不可验证: " + "; ".join(graded["platform_errors"][:5]),
                 retryable=False,
                 values={"score": None, "system_error": "结果不可验证，需教师处理",
                         "result_details": {
                             "groups": graded["groups"],
                             "platform_errors": graded["platform_errors"],
                             "grading_version_id": version.id,
                             "runner_stdout_tail": stdout[-2000:],
                             "f_score": graded["f_score"], "r_score": graded["r_score"]}})
        _maybe_finalize_exam(answer.submission_id, db)
        return answer

    locked_id = version.rubric_id
    f_score, r_score = graded["f_score"], graded["r_score"]
    exam_result_details = {"groups": graded["groups"], "system_errors": [],
                           "grading_version_id": version.id,
                           "runner_stdout_tail": stdout[-2000:],
                           "f_score": f_score, "r_score": r_score,
                           "binary_score": graded["binary_score"]}

    if mode in ("legacy", "shadow"):
        from app.services.exam_service import round_score
        from app.services.grading_version_service import frozen_points_for_question

        exam_submission = db.get(ExamSubmission, answer.submission_id)
        points = frozen_points_for_question(exam_submission, answer.question_id) \
            if exam_submission is not None else None
        if points is None or graded["binary_score"] is None:
            fail_job(db, job_type="exam", object_id=answer.id,
                     error="可信 binary 分值或开考分值快照缺失，转人工复核",
                     retryable=False,
                     values={"score": None, "system_error": "缺少可信分值基线",
                             "result_details": {**exam_result_details,
                                 "diagnostic": {"code": "MISSING_SCORE_BASELINE",
                                                "module": "",
                                                "message": "缺少可信分值基线，请教师人工处理"}}})
            _maybe_finalize_exam(answer.submission_id, db)
            return answer
        official_score = round_score(float(points) * float(graded["binary_score"]) / 100.0)
        if mode == "legacy":
            from app.services.judge_queue import complete_job

            complete_job(db, job_type="exam", object_id=answer.id, values={
                "score": official_score, "system_error": None,
                "tests_passed": graded["tests_passed"], "tests_total": graded["tests_total"],
                "result_details": exam_result_details,
            })
            _maybe_finalize_exam(answer.submission_id, db)
            return answer

    def _make_exam_code_grade(source: dict) -> CodeGrade:
        return CodeGrade(
            exam_answer_id=answer.id, rubric_id=locked_id,
            mode=mode, status="pending",
            functional_score=f_score, robustness_score=r_score,
            deterministic_details={"groups": graded["groups"], "source": source,
                                   "grading_version_id": version.id},
        )

    outcome, code_grade = _complete_with_code_grade(
        db, job_type="exam", object_id=answer.id,
        values={"score": (official_score if mode == "shadow" else None),
                "system_error": None,
                "tests_passed": graded["tests_passed"],
                "tests_total": graded["tests_total"],
                "result_details": exam_result_details},
        target_field="exam_answer_id", target_id=answer.id,
        code_grade_factory=_make_exam_code_grade,
    )
    if outcome == "lost_claim":
        return answer

    _enqueue_ai_after_commit(db, redis_client, code_grade)
    # active 模式：等 AI 完成后由 AI 收尾路径汇总本场考试
    return answer


def process_exam_answer(db: Session, redis_client, settings: Settings, answer_id: int) -> ExamAnswer:
    from app.services.judge_queue import claim_job, complete_job, fail_job

    answer = db.get(ExamAnswer, answer_id)
    if not answer:
        return None
    if answer.grading_status == "completed":
        _maybe_finalize_exam(answer.submission_id, db)
        return answer

    try:
        if not claim_job(db, job_type="exam", object_id=answer_id):
            return answer

        question = db.get(ExamQuestion, answer.question_id)
        if not question:
            fail_job(db, job_type="exam", object_id=answer_id,
                     error="题目不存在", retryable=False,
                     values={"score": None, "system_error": "题目不存在"})
            _maybe_finalize_exam(answer.submission_id, db)
            return answer

        _work_root = _settings_path_or_none(settings.judge_work_dir)
        _host_root = _settings_path_or_none(settings.judge_host_work_dir)
        _cleanup = None
        try:
            if _work_root:
                workdir, _cleanup = _make_work_dir(_work_root, "dai-exam-judge-")
                host_workdir = _host_root / workdir.relative_to(_work_root) if _host_root else workdir
            else:
                _temp = tempfile.TemporaryDirectory(prefix="dai-exam-judge-")
                workdir = Path(_temp.name)
                host_workdir = workdir
                _cleanup = lambda: _temp.cleanup()

            timeout_s = max(min(math.ceil((question.time_limit_ms or 10000) / 1000), settings.judge_timeout_seconds), 1)
            mem_mb = question.memory_limit_mb or settings.judge_memory_limit_mb
            gmode = getattr(question, 'grading_mode', 'legacy') or 'legacy'

            exam_version, exam_action = _resolve_exam_bound_version(db, answer, question)
            if exam_action == "missing_baseline":
                fail_job(db, job_type="exam", object_id=answer_id,
                         error="该考试已启用评分版本，但本答案没有版本基线",
                         retryable=False,
                         values={"score": None, "system_error": "缺少评分版本基线",
                                 "result_details": {"platform_errors": [
                                     "答案缺少评分版本基线，需教师确认后重评"]}})
                _maybe_finalize_exam(answer.submission_id, db)
                return answer
            if exam_action == "manual_review":
                # 安全降级：人工复核版本不得执行旧评分器
                fail_job(db, job_type="exam", object_id=answer_id,
                         error="该评分版本要求人工复核，不执行自动判分",
                         retryable=False,
                         values={"score": None, "system_error": "该题使用人工复核评分版本",
                                 "result_details": {
                                     "grading_version_id": exam_version.id if exam_version else None,
                                     "diagnostic": {
                                         "code": "MANUAL_REVIEW_REQUIRED", "module": "",
                                         "message": "该题使用人工复核评分版本，请教师人工给分"}}})
                _maybe_finalize_exam(answer.submission_id, db)
                return answer
            if exam_action == "data_v1":
                return _data_v1_judge_exam_answer(
                    db, redis_client, settings, answer, question,
                    workdir, host_workdir, timeout_s, mem_mb, exam_version)

            if gmode == "legacy":
                # legacy 路径
                if not question.hidden_tests or not question.hidden_tests.strip():
                    # 永久配置错误：立即终态，不消耗重试
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="缺少隐藏测试", retryable=False,
                             values={"score": None, "system_error": "缺少隐藏测试"})
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                user_code = workdir / "user_code.py"
                test_file = workdir / "test_user_code.py"
                user_code.write_text(answer.code_answer or "", encoding="utf-8")
                ht = question.hidden_tests
                if "import user_code" not in ht and "from user_code" not in ht:
                    ht = f"import user_code\n\n{ht}"
                test_file.write_text(ht, encoding="utf-8")
                try:
                    stdout, stderr, returncode, elapsed = _run_docker_pytest(
                        workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir)
                except Exception as e:
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error=f"Docker 判题失败: {e}", retryable=True,
                             values={"score": None,
                                     "system_error": f"Docker 判题失败: {e}"})
                    return answer

                final_status, _ = _status_from_pytest(returncode, stdout, stderr)
                if final_status == "system_error":
                    fail_job(
                        db,
                        job_type="exam",
                        object_id=answer_id,
                        error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
                        retryable=True,
                        values={"score": None,
                                "system_error": f"Docker 判题系统错误（exit={returncode}）",
                                "result_details": {"returncode": returncode}},
                    )
                    return answer
                # 完成 CAS 与结果写入合并为一次条件 UPDATE（原先先 ORM 置 completed 再调
                # complete_job，后者的 running 条件必然不命中 ⇒ finished_at 永不写入）
                complete_job(
                    db, job_type="exam", object_id=answer_id,
                    values={"score": float(question.points) if final_status == "accepted" else 0.0,
                            "result_details": {"returncode": returncode}},
                )
                _maybe_finalize_exam(answer.submission_id, db)
            else:
                # V1 路径：测试组 → FR 分 → 校验 → 创建 CodeGrade → 入队 AI
                test_groups = question.test_groups or []
                if not test_groups:
                    # 永久配置错误：立即终态，不消耗重试
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="shadow/active 需要测试组", retryable=False,
                             values={"score": None, "system_error": "缺少测试组"})
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                # 查找锁定 Rubric（缺失则系统错误，禁止继续）
                locked = db.scalar(
                    select(QuestionRubric).where(
                        QuestionRubric.exam_question_id == question.id,
                        QuestionRubric.status == "locked",
                    ).order_by(QuestionRubric.version.desc()).limit(1)
                )
                if locked is None:
                    # 永久配置错误：立即终态，不消耗重试
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="shadow/active 缺少锁定 Rubric", retryable=False,
                             values={"score": None, "system_error": "缺少锁定 Rubric"})
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                # 配置完整性：测试组必须有 tests 代码（永久错误，不重试、不进入 Docker）
                missing_tests = [g.get("id") for g in test_groups
                                 if not (g.get("tests") or "").strip()]
                if missing_tests:
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="测试组缺少测试代码: " + ",".join(str(x) for x in missing_tests),
                             retryable=False,
                             values={"score": None, "system_error": "测试组缺少测试代码"})
                    _maybe_finalize_exam(answer.submission_id, db)
                    return answer

                result = run_test_groups(workdir, host_workdir, answer.code_answer or "",
                                         test_groups, settings, timeout_s, mem_mb,
                                         heartbeat=_make_heartbeat(db, "exam", answer_id))
                all_errs = list(result["system_errors"])
                f_score, r_score, details = _calc_fr_scores(test_groups, result["results"], all_errs)

                # 系统错误（Docker/解析失败）→ 立即停止，不创建 CodeGrade，不 finalize
                if all_errs:
                    fail_job(db, job_type="exam", object_id=answer_id,
                             error="; ".join(all_errs), retryable=True,
                             values={"score": None, "system_error": "; ".join(all_errs),
                                     "result_details": {"groups": details, "system_errors": all_errs,
                                                        "f_score": f_score, "r_score": r_score}})
                    return answer

                if gmode == "shadow":
                    # 保留旧二元评分：全通过才满分
                    user_code = workdir / "user_code.py"
                    test_file = workdir / "test_user_code.py"
                    user_code.write_text(answer.code_answer or "", encoding="utf-8")
                    ht = question.hidden_tests
                    if "import user_code" not in ht and "from user_code" not in ht:
                        ht = f"import user_code\n\n{ht}"
                    test_file.write_text(ht, encoding="utf-8")
                    try:
                        stdout, stderr, returncode, _ = _run_docker_pytest(
                            workdir, settings, timeout_s, mem_mb, host_workdir=host_workdir)
                    except Exception as exc:
                        # 基础设施异常→fail_job 可重试，不扣分，不 finalize
                        fail_job(db, job_type="exam", object_id=answer_id,
                                 error=f"Docker 判题失败: {exc}", retryable=True,
                                 values={"score": None,
                                         "system_error": f"Docker 判题失败: {exc}"})
                        return answer
                    legacy_status, _ = _status_from_pytest(returncode, stdout, stderr)
                    if legacy_status == "system_error":
                        fail_job(
                            db,
                            job_type="exam",
                            object_id=answer_id,
                            error=f"Docker 判题系统错误（exit={returncode}）: {stderr[-1000:]}",
                            retryable=True,
                            values={"score": None,
                                    "system_error": f"Docker 判题系统错误（exit={returncode}）",
                                    "result_details": {
                                        "groups": details,
                                        "system_errors": [f"Docker exit={returncode}"],
                                        "f_score": f_score,
                                        "r_score": r_score,
                                    }},
                        )
                        return answer
                    final_score = float(question.points) if legacy_status == "accepted" else 0.0
                else:
                    # active: 等 AI 完成后才定分
                    final_score = None

                # 完成 CAS 与全部结果字段合并为一次条件 UPDATE（与作业 V1 路径同一写法）
                tests_passed, tests_total = _test_counts_from_group_results(result["results"])
                exam_result_details = {"groups": details, "system_errors": all_errs,
                                       "f_score": f_score, "r_score": r_score}
                locked_id = locked.id

                def _make_exam_code_grade(source: dict) -> CodeGrade:
                    return CodeGrade(
                        exam_answer_id=answer_id, rubric_id=locked_id,
                        mode=gmode, status="pending",
                        functional_score=f_score, robustness_score=r_score,
                        deterministic_details={"groups": details, "system_errors": all_errs,
                                               "source": source},
                    )

                # 主结果与评分记录同一受认领事务：失去代次立即停止（不建 CG、不推 AI、不推进汇总）
                outcome, code_grade = _complete_with_code_grade(
                    db, job_type="exam", object_id=answer_id,
                    values={"score": final_score,
                            "tests_passed": tests_passed, "tests_total": tests_total,
                            "result_details": exam_result_details},
                    target_field="exam_answer_id", target_id=answer_id,
                    code_grade_factory=_make_exam_code_grade,
                )
                if outcome == "lost_claim":
                    return answer

                # 提交之后才通知 AI 队列（locked rubric 已确保存在）
                _enqueue_ai_after_commit(db, redis_client, code_grade)

                # shadow 立即汇总; active 等 AI
                if gmode == "shadow":
                    _maybe_finalize_exam(answer.submission_id, db)

            return answer
        finally:
            if _cleanup:
                _cleanup()
            # A15：Worker 没有 lifespan，每个任务结束时把指标写入 Redis
            from app.services import op_metrics as _op_metrics
            _op_metrics.flush_for_worker()

    except Exception:
        logger.exception("ExamAnswer %s 未知异常", answer_id)
        try:
            fail_job(db, job_type="exam", object_id=answer_id, error="Worker 未知异常", retryable=True)
        except Exception:
            pass
        try:
            _maybe_finalize_exam(answer.submission_id, db)
        except Exception:
            pass
        return answer


# ═══════════════════════════════════════════════════════════════
# AI 评分处理
# ═══════════════════════════════════════════════════════════════

def process_ai_grade(db: Session, redis_client, settings: Settings, code_grade_id: int,
                     metrics_sink=None) -> CodeGrade:
    from app.services.ai_grading_queue import claim_ai_grade, fail_ai_grade
    from app.services.ai_grading_service import grade_code_submission
    from app.services.ai_client import AIServiceError, DeepSeekClient
    from app.services.grading_errors import LostClaimError
    from app.services.op_metrics import ai_metrics_sink

    if not claim_ai_grade(db, code_grade_id):
        return None

    if not settings.ai_ready:
        # TASK-020：AI 关闭（未审批/未配置）时零外呼——
        # 不构造客户端、不发任何请求；任务以不可重试终态转人工评分
        db.rollback()
        fail_ai_grade(
            db, redis_client, code_grade_id,
            "AI 服务未启用（DAI_AI_ENABLED=false 或未配置 API Key）",
            retryable=False,
        )
        return None

    try:
        client = DeepSeekClient(settings, metrics_sink=metrics_sink or ai_metrics_sink())
        cg = grade_code_submission(db, client, code_grade_id)
        # 终态（completed / review_required）已由 grade_code_submission 在**同一事务**
        # 内写入并释放认领；这里只按已提交终态推进考试汇总。
        # 原先的第二次 complete_ai_grade 必然被栅栏拒绝，使 CodeGrade 永久停在 running。
        db.refresh(cg)
        if cg.status not in ("completed", "review_required"):
            logger.warning("AI 评分终态异常，未推进考试汇总: grade=%s status=%s",
                           code_grade_id, cg.status)
            return cg
        if cg.exam_answer_id and cg.mode == "active":
            ans = db.get(ExamAnswer, cg.exam_answer_id)
            if ans:
                from app.services.exam_grading import (
                    finalize_if_ready,
                    promote_review_required_if_complete,
                )
                finalize_if_ready(ans.submission_id, db)
                if cg.status == "completed":
                    # 教师在工作台显式重试成功后的补救：失败原因已消除则收口
                    promote_review_required_if_complete(ans.submission_id, db)
        return cg
    except LostClaimError:
        # 认领已被恢复机制释放/接管：任何写入（包括失败写入）都会被拒，立即停止。
        logger.warning("AI 评分认领失效，结果丢弃（等待恢复重排）: grade=%s", code_grade_id)
        return None
    except AIServiceError as exc:
        db.rollback()
        fail_ai_grade(db, redis_client, code_grade_id, str(exc), retryable=exc.retryable)
        return None
    except Exception as exc:
        db.rollback()
        fail_ai_grade(db, redis_client, code_grade_id, str(exc), retryable=True)
        return None


# ═══════════════════════════════════════════════════════════════
# Worker 主循环
# ═══════════════════════════════════════════════════════════════

def _recover_stale(db, redis_client, owner_id: str) -> bool:
    """在 grading-recovery 租约下执行 judge + AI stale recovery。

    多 Worker 实例同一时刻只有一个执行；租约被他人持有时跳过本轮。
    """
    from app.services.scheduler_lease import try_acquire_lease
    if not try_acquire_lease(db, "grading-recovery", owner_id, ttl_seconds=120):
        return False
    from app.services.judge_queue import requeue_stale_jobs
    from app.services.ai_grading_queue import recover_stale_ai_grades
    # 阈值必须覆盖最坏判题墙钟（组数 × 每组超时 + 容器启动 + 镜像拉取）：
    # 判题在测试组之间续 heartbeat_at，未续期时才回退 started_at。
    j_stats = requeue_stale_jobs(
        db, stale_running_seconds=get_settings().judge_stale_running_seconds
    )
    a_stats = recover_stale_ai_grades(db, redis_client)
    if any(v > 0 for v in j_stats.values()) or any(v > 0 for v in a_stats.values()):
        logger.info("Grading 恢复: judge=%s ai=%s", j_stats, a_stats)
    return True


def _worker_queue_names(settings: Settings) -> tuple[str, ...]:
    """Return only the queues owned by this worker role.

    The deterministic worker is deliberately the default.  An AI worker is
    opt-in and has no consumption queues at all while AI is disabled.
    The AI worker additionally consumes the one-click AI setup queue
    (rubric/test-group generation and draft trial runs).
    """
    if settings.worker_role == "judge":
        return settings.judge_queue_name, EXAM_JUDGE_QUEUE
    if settings.worker_role == "ai":
        if not settings.ai_enabled:
            return ()
        return settings.ai_queue_name, settings.ai_setup_queue_name
    if settings.worker_role == "practice":
        # 练习判题由 app.worker.practice_worker 独占消费 practice:*
        # 队列；正式 worker 不监听它们，避免互相挤占容量。
        return ()
    raise ValueError(f"unsupported worker role: {settings.worker_role}")


def run_worker_loop():
    import redis as _redis

    settings = get_settings()
    worker_role = settings.worker_role
    queues = list(_worker_queue_names(settings))
    if not queues:
        logger.info("AI worker 未启用，退出消费循环")
        return

    redis_client = _redis.Redis.from_url(
        settings.redis_url, decode_responses=True, encoding_errors="replace",
        socket_connect_timeout=2, socket_timeout=7,
    )
    import socket
    owner_id = f"worker:{socket.gethostname()}:{os.getpid()}"

    logger.info("%s worker 启动，监听队列: %s", worker_role, queues)

    next_recovery = time.monotonic()
    while True:
        # Redis is only a wake-up transport. Busy or unavailable queues must
        # not prevent repairing durable jobs, including on worker startup.
        if worker_role == "judge" and time.monotonic() >= next_recovery:
            try:
                with SessionLocal() as db:
                    _recover_stale(db, redis_client, owner_id)
            except Exception:
                logger.exception("Grading 恢复异常，稍后重试")
                next_recovery = time.monotonic() + 5
            else:
                # A healthy scan and a lease owned by another worker both use
                # the normal interval. Retry failures without pausing work.
                next_recovery = time.monotonic() + 120

        try:
            result = redis_client.blpop(queues, timeout=5)
            if result is None:
                continue

            queue_name, raw_data = result
            # Rotate every consumed message before decoding or claiming it.
            # Invalid/duplicate messages must not monopolize the first queue.
            queues.remove(queue_name)
            queues.append(queue_name)
        except Exception:
            logger.exception("blpop 异常")
            time.sleep(1)
            continue

        try:
            payload = _json.loads(raw_data)
            if not isinstance(payload, dict):
                raise ValueError("queue message must be an object")
            msg_type = payload.get("type")
            job_id = payload.get("id")
            if queue_name == settings.ai_setup_queue_name:
                expected_types = ("ai_setup", "ai_trial")
            elif worker_role == "ai":
                expected_types = ("ai_grade",)
            else:
                expected_types = ("exam",) if queue_name == EXAM_JUDGE_QUEUE else ("assignment",)
            if (msg_type not in expected_types or type(job_id) is not int or job_id <= 0
                    or ("attempt" in payload and
                        (type(payload["attempt"]) is not int or payload["attempt"] < 0))):
                raise ValueError("invalid queue message fields")
        except Exception:
            # The parser can also reject deeply nested JSON with RecursionError.
            # Keep this boundary around decoding/validation, never execution.
            logger.warning("无效判题消息，已忽略: queue=%s", queue_name)
            continue

        if queue_name == settings.ai_setup_queue_name:
            from app.services import ai_grading_setup_service as setup_service

            try:
                with SessionLocal() as db:
                    if msg_type == "ai_setup":
                        setup_service.run_setup_job(db, redis_client, settings, job_id)
                    else:
                        setup_service.run_trial_run(db, redis_client, settings, job_id)
            except Exception:
                logger.exception("AI 配置任务异常: id=%s", job_id)
        elif worker_role == "ai":
            try:
                with SessionLocal() as db:
                    process_ai_grade(db, redis_client, settings, job_id)
            except Exception:
                logger.exception("AI 评分异常: id=%s", job_id)
        elif msg_type == "exam":
            try:
                with SessionLocal() as db:
                    process_exam_answer(db, redis_client, settings, job_id)
            except Exception:
                logger.exception("考试判题异常: id=%s", job_id)
        else:
            try:
                with SessionLocal() as db:
                    process_submission(db, redis_client, settings, job_id)
            except Exception:
                logger.exception("判题异常: id=%s", job_id)


if __name__ == "__main__":
    from app.logging_config import setup_logging
    setup_logging(get_settings().environment, process_name="worker", settings=get_settings())
    run_worker_loop()
