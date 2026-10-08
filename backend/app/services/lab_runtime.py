"""Offline Docker execution of untrusted lab code, never on the API host.

All writable paths are size-limited tmpfs mounts. Outputs are copied only after
freezing the container, using a bounded extractor which rejects every link and
special file. The API receives files, never deserialized model objects.
"""
from __future__ import annotations

import io
import codecs
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath


class RuntimeFailure(RuntimeError):
    def __init__(self, message, code="EXECUTION_FAILED", *, infrastructure=False):
        super().__init__(message)
        self.code = code
        self.infrastructure = infrastructure


@dataclass(frozen=True)
class RuntimeResult:
    exit_code: int
    logs: str
    elapsed_seconds: float
    output_dir: Path
    program_elapsed_seconds: float | None = None


_IMAGE = re.compile(r"(?:sha256:|[^\s,]+@sha256:)[a-f0-9]{64}\Z")
_TOKEN = re.compile(r"[a-fA-F0-9-]{16,36}\Z")
_PHASES = ("train", "predict", "check")

# This controller runs *inside* the sandbox and creates a fresh ipykernel. Code
# goes through Jupyter execute requests, so %%writefile, %time and shell magics
# retain Notebook semantics. Its output is student evidence, never grade truth.
TRAINING_RUNNER = r'''import ast, json, os, sys
from queue import Empty
from jupyter_client import BlockingKernelClient

cells = json.load(open("/runner/cells.json", encoding="utf-8"))
observations = json.load(open("/runner/observations.json", encoding="utf-8"))
kc = BlockingKernelClient(connection_file='/runner/connection.json')
kc.load_connection_file()
kc.start_channels()
failed = False
def emit(kind, **values):
    print(json.dumps({'kind': kind, **values}, ensure_ascii=False, allow_nan=False), flush=True)
try:
    kc.wait_for_ready(timeout=30)
    for i, cell in enumerate(cells):
        if cell.get("cell_type", "code") != "code":
            continue
        code = cell.get("source", "")
        if isinstance(code, list): code = "".join(code)
        if not code.strip(): continue
        ident = cell.get("id", str(i))
        expressions = {}
        for rule in observations:
            if rule["cell_id"] != ident: continue
            variable = rule["variable"]
            if rule["kind"] == "shape":
                expression = "list(%s.shape) if hasattr(%s, 'shape') else [len(%s)]" % (variable, variable, variable)
                expressions[rule["id"]] = "__import__('json').dumps({'shape': %s}, allow_nan=False)" % expression
            else:
                expressions[rule["id"]] = "__import__('json').dumps({'value': float(%s)}, allow_nan=False)" % variable
        item = {"stdout": "", "values": {}, "completed": True}
        visible = not cell.get('source_hidden')
        if visible: emit('log', text="[cell %d]\n" % (i + 1))
        msg_id = kc.execute(code, stop_on_error=True, user_expressions=expressions)
        while True:
            try: message = kc.get_iopub_msg(timeout=1)
            except Empty:
                continue
            if message.get("parent_header", {}).get("msg_id") != msg_id: continue
            kind, content = message.get("msg_type"), message["content"]
            if kind == "stream":
                text = content.get("text", "")
                if visible: emit('log', text=text)
                if content.get("name", "stdout") == "stdout": item["stdout"] = (item["stdout"] + text)[:32768]
            elif kind == "error":
                failed = True
                item["completed"] = False
                if visible: emit('log', text=content.get("ename", "Error") + ": " + content.get("evalue", "") + '\n')
            elif kind in ("execute_result", "display_data"):
                text = content.get("data", {}).get("text/plain")
                if text and visible: emit('log', text=text + '\n')
            elif kind == "status" and content.get("execution_state") == "idle": break
        while True:
            reply = kc.get_shell_msg(timeout=30)
            if reply.get("parent_header", {}).get("msg_id") == msg_id: break
        for key, observation in reply.get("content", {}).get("user_expressions", {}).items():
            if key not in expressions or observation.get("status") != "ok": continue
            text = observation.get("data", {}).get("text/plain", "")
            if len(text) > 10000: continue
            try: item["values"][key] = json.loads(ast.literal_eval(text))
            except (ValueError, SyntaxError, TypeError): pass
        if visible: emit('cell', cell_id=ident, observation=item)
        if failed: break
finally:
    kc.shutdown()
    kc.stop_channels()
sys.exit(1 if failed else 0)
'''


def container_name(run_id, token, phase):
    if not isinstance(run_id, int) or run_id < 1 or not _TOKEN.fullmatch(token or "") or phase not in _PHASES:
        raise ValueError("Invalid internal sandbox identity")
    return f"dai-lab-{run_id}-{token}-{phase}"


def stop_run_containers(run_id, lease_token):
    """Confirm removal of exactly this lease's containers, or fail closed."""
    for phase in _PHASES:
        name = container_name(run_id, lease_token, phase)
        try:
            result = subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode and "No such container" not in result.stderr:
            return False
        try:
            volume = subprocess.run(["docker", "volume", "rm", f"{name}-output"],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if volume.returncode and "no such volume" not in volume.stderr.lower():
            return False
    return True


def _host_path(path, settings):
    path = Path(path).resolve()
    local = getattr(settings, "judge_work_dir", "")
    host = getattr(settings, "judge_host_work_dir", "")
    if local and host:
        try:
            relative = path.relative_to(Path(local).resolve())
        except ValueError as exc:
            raise RuntimeFailure("运行文件未位于共享工作目录。", "WORKSPACE_MAPPING", infrastructure=True) from exc
        result = str(Path(host) / relative)
    else:
        result = str(path)
    if "," in result or "\n" in result:
        raise RuntimeFailure("工作目录名称不受支持。", "WORKSPACE_MAPPING", infrastructure=True)
    return result


def _check_mount_tree(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeFailure("输入目录不存在或包含不安全的链接。", "UNSAFE_INPUT")
    for root, directories, files in os.walk(path, followlinks=False):
        for entry in directories + files:
            candidate = Path(root) / entry
            if candidate.is_symlink() or not (candidate.is_dir() or candidate.is_file()):
                raise RuntimeFailure("输入目录包含不安全的链接或特殊文件。", "UNSAFE_INPUT")


def _normalise_limits(resources):
    cpu = float(resources.get("cpus", 1))
    memory = int(resources.get("memory_mb", 1024))
    timeout = int(resources.get("timeout_seconds", 1800))
    disk = int(resources.get("disk_mb", 2048))
    if not math.isfinite(cpu) or not 0 < cpu <= 256 or not 128 <= memory <= 1048576:
        raise ValueError("Invalid CPU or memory limits")
    if not 1 <= timeout <= 86400 or not 1 <= disk <= 1048576:
        raise ValueError("Invalid time or disk limits")
    return cpu, memory, timeout, disk


def docker_create_command(*, name, image_ref, mounts, output_mount, resources, settings, gpu=None):
    if not _IMAGE.fullmatch(image_ref or ""):
        raise RuntimeFailure("运行环境必须绑定经过验证的不可变镜像。", "INVALID_IMAGE", infrastructure=True)
    cpu, memory, _, disk = _normalise_limits(resources)
    argv = ["docker", "run", "--detach", "--pull", "never", "--name", name,
        "--label", "dai.lab=true", "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--read-only", "--user", "1000:1000",
        "--cpus", str(cpu), "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
        "--pids-limit", "128", "--ulimit", "nofile=1024:1024",
        "--ulimit", f"fsize={disk * 1048576}:{disk * 1048576}",
        "--log-driver", "none", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        # A local-driver tmpfs volume is visible to Docker's archive API.
        # Direct --tmpfs mounts are namespace-local and docker cp sees an empty
        # directory, even while running. Both enforce the same kernel quota.
        "--mount", f"type=volume,source={name}-output,target={output_mount},volume-nocopy",
        "--env", "HOME=/tmp", "--env", "PYTHONUNBUFFERED=1", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", f"DAI_LAB_DEVICE={'gpu' if gpu is not None else 'cpu'}",
        "--env", "IPYTHONDIR=/tmp/ipython", "--env", "MPLCONFIGDIR=/tmp/matplotlib",
        "--env", f"OMP_NUM_THREADS={max(1, int(cpu))}",
        "--env", f"OPENBLAS_NUM_THREADS={max(1, int(cpu))}",
        "--workdir", output_mount, "--entrypoint", "python"]
    if gpu is not None:
        if not settings.lab_gpu_enabled or gpu not in [x.strip() for x in settings.lab_gpu_devices.split(",")]:
            raise RuntimeFailure("GPU 未通过资源准入检查。", "GPU_UNAVAILABLE", infrastructure=True)
        argv.extend(["--gpus", f"device={gpu}"])
    for source, target in mounts:
        _check_mount_tree(source)
        argv.extend(["--mount", f"type=bind,source={_host_path(source, settings)},target={target},readonly"])
    argv.extend([image_ref, "-c", "import time; time.sleep(172800)"])
    return argv


def _extract_output_tar(stream, destination, *, max_bytes, max_files=10000):
    """Copy regular members manually; never call tarfile.extract/extractall."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total = count = 0
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            count += 1
            if count > max_files:
                raise RuntimeFailure("产物超过文件数量上限。", "OUTPUT_LIMIT")
            parts = PurePosixPath(member.name).parts
            if not parts or parts == (".",):
                continue
            if member.name.startswith("/") or ".." in parts or "\\" in member.name or len(member.name) > 1024:
                raise RuntimeFailure("产物包含不安全路径。", "UNSAFE_OUTPUT")
            target = destination.joinpath(*parts)
            if member.isdir():
                if target.exists() and not target.is_dir():
                    raise RuntimeFailure("产物路径冲突。", "UNSAFE_OUTPUT")
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size < 0:
                raise RuntimeFailure("产物只允许普通文件，不接受链接或特殊文件。", "UNSAFE_OUTPUT")
            total += member.size
            if total > max_bytes or count > max_files:
                raise RuntimeFailure("产物超过文件数量或大小上限。", "OUTPUT_LIMIT")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise RuntimeFailure("产物中存在重复路径。", "UNSAFE_OUTPUT")
            source = archive.extractfile(member)
            with target.open("xb") as output:
                while data := source.read(1024 * 1024):
                    output.write(data)
    return total


def _copy_outputs(name, mount, destination, max_bytes):
    proc = subprocess.Popen(["docker", "cp", f"{name}:{mount}/.", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        _extract_output_tar(proc.stdout, destination, max_bytes=max_bytes)
        if proc.wait(timeout=30):
            raise RuntimeFailure("读取运行产物失败。", "OUTPUT_COPY_FAILED", infrastructure=True)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _execute(*, run_id, lease_token, phase, image_ref, mounts, command, output_mount,
             output_dir, resources, settings, gpu=None, should_continue=None, on_log=None,
             setup_command=None, controller_user=None):
    _, _, timeout, disk = _normalise_limits(resources)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise RuntimeFailure("每次运行必须使用新的空产物目录。", "OUTPUT_NOT_EMPTY", infrastructure=True)
    if shutil.disk_usage(output_dir).free < (settings.lab_min_free_disk_mb + disk) * 1048576:
        raise RuntimeFailure("运行节点可用磁盘不足，请稍后重试。", "DISK_UNAVAILABLE", infrastructure=True)
    name = container_name(run_id, lease_token, phase)
    argv = docker_create_command(name=name, image_ref=image_ref, mounts=mounts,
        output_mount=output_mount, resources=resources, settings=settings, gpu=gpu)
    started = time.monotonic()
    logs = bytearray()
    proc = None
    created = False
    selector = selectors.DefaultSelector()
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    try:
        if should_continue:
            should_continue()
        # Mark before invoking Docker: a CLI timeout does not prove create failed.
        created = True
        volume = subprocess.run(["docker", "volume", "create", "--driver", "local",
            "--label", "dai.lab=true", "--opt", "type=tmpfs", "--opt", "device=tmpfs",
            "--opt", f"o=nosuid,nodev,size={disk}m,uid=1000,gid=1000,mode=0700",
            f"{name}-output"], capture_output=True, text=True, timeout=30)
        if volume.returncode:
            raise RuntimeFailure("无法创建有容量上限的运行目录。", "SANDBOX_UNAVAILABLE", infrastructure=True)
        creation = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if creation.returncode:
            raise RuntimeFailure("无法启动隔离运行环境。", "SANDBOX_UNAVAILABLE", infrastructure=True)
        if gpu is not None:
            probe = subprocess.run(["docker", "exec", name, "python", "-c",
                "import torch; assert torch.cuda.is_available(); torch.ones(1, device='cuda').cpu()"],
                capture_output=True, timeout=30)
            if probe.returncode:
                raise RuntimeFailure("分配的GPU或环境CUDA支持不可用。", "GPU_NOT_READY", infrastructure=True)
        program_started = time.monotonic()
        if setup_command:
            setup = subprocess.run(['docker', 'exec', '--detach', name, *setup_command], capture_output=True, timeout=30)
            if setup.returncode:
                raise RuntimeFailure('无法启动学生内核。', 'SANDBOX_UNAVAILABLE', infrastructure=True)
        controller = ['docker', 'exec']
        if controller_user is not None:
            controller.extend(['--user', str(controller_user), '--workdir', '/tmp'])
        proc = subprocess.Popen([*controller, name, *command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        eof = False
        while not (eof and proc.poll() is not None):
            if should_continue:
                should_continue()
            if time.monotonic() - started > timeout:
                raise RuntimeFailure("运行超过实验时间限制。", "EXECUTION_TIMEOUT")
            for key, _ in selector.select(timeout=0.25):
                data = os.read(key.fd, 8192)
                if not data:
                    if on_log:
                        tail = decoder.decode(b'', final=True)
                        if tail: on_log(tail)
                    selector.unregister(key.fileobj)
                    eof = True
                    continue
                logs.extend(data)
                if len(logs) > settings.lab_log_max_bytes:
                    raise RuntimeFailure("输出日志超过实验上限，请减少逐批打印。", "LOG_LIMIT")
                if on_log:
                    on_log(decoder.decode(data))
        exit_code = proc.wait(timeout=5)
        program_elapsed = time.monotonic() - program_started
        if should_continue:
            should_continue()
        if exit_code == 0:
            paused = subprocess.run(["docker", "pause", name], capture_output=True, timeout=20)
            if paused.returncode:
                raise RuntimeFailure("无法冻结运行成果。", "SANDBOX_UNAVAILABLE", infrastructure=True)
            _copy_outputs(name, output_mount, output_dir, disk * 1048576)
        return RuntimeResult(exit_code=exit_code, logs=logs.decode("utf-8", "replace"),
            elapsed_seconds=time.monotonic() - started, output_dir=output_dir,
            program_elapsed_seconds=program_elapsed)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeFailure("隔离运行服务暂时不可用。", "SANDBOX_UNAVAILABLE", infrastructure=True) from exc
    finally:
        selector.close()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            proc.stdout.close()
        if created and not stop_run_containers(run_id, lease_token):
            # Do NOT release capacity on this error; durable recovery must first
            # establish that the isolated process no longer exists.
            raise RuntimeFailure("运行环境清理待恢复，请勿释放资源租约。", "SANDBOX_CLEANUP_FAILED", infrastructure=True)


def _runner_dir(settings, run_id, token, phase):
    root = Path(settings.lab_work_path) / "controllers"
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{run_id}-{token}-{phase}-", dir=root))
    directory.chmod(0o755)
    return directory


def execute_training(*, run_id, lease_token, image_ref, cells, course_dir, work_dir,
                     resources, settings, device="cpu", gpu=None, should_continue=None, on_log=None,
                     observations=None):
    if device not in {"cpu", "gpu"} or (device == "gpu") != (gpu is not None):
        raise RuntimeFailure("运行设备与资源租约不匹配。", "DEVICE_MISMATCH", infrastructure=True)
    directory = _runner_dir(settings, run_id, lease_token, "train")
    try:
        (directory / "run.py").write_text(TRAINING_RUNNER, encoding="utf-8")
        (directory / "cells.json").write_text(json.dumps(cells, ensure_ascii=False), encoding="utf-8")
        (directory / "observations.json").write_text(json.dumps(observations or [], ensure_ascii=False), encoding="utf-8")
        # The kernel runs as the container's unprivileged default user. The
        # controller is a distinct UID, cannot be modified/signalled by student
        # code, and has no host mounts, credentials or private grading rules.
        connection = {'transport': 'tcp', 'ip': '127.0.0.1', 'key': uuid.uuid4().hex,
            'signature_scheme': 'hmac-sha256', 'shell_port': 57101, 'iopub_port': 57102,
            'stdin_port': 57103, 'control_port': 57104, 'hb_port': 57105}
        (directory / 'connection.json').write_text(json.dumps(connection), encoding='utf-8')
        captured, plain_logs = {}, []
        pending = ''
        def collect(chunk):
            nonlocal pending
            pending += chunk
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                try:
                    event = json.loads(line)
                except ValueError:
                    # Controller diagnostics are not student observations.
                    if on_log: on_log(line + '\n')
                    plain_logs.append(line + '\n')
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('kind') == 'log' and isinstance(event.get('text'), str):
                    plain_logs.append(event['text'])
                    if on_log: on_log(event['text'])
                elif event.get('kind') == 'cell' and isinstance(event.get('cell_id'), str):
                    captured[event['cell_id']] = event.get('observation')
        result = _execute(run_id=run_id, lease_token=lease_token, phase="train", image_ref=image_ref,
            mounts=[(directory, "/runner"), (course_dir, "/course")],
            command=["python", "/runner/run.py"], output_mount="/work", output_dir=work_dir,
            resources=resources, settings=settings, gpu=gpu, should_continue=should_continue, on_log=collect,
            setup_command=['python', '-m', 'ipykernel_launcher', '-f', '/runner/connection.json'], controller_user=0)
        # Written only after the sandbox has stopped: student-created files with
        # this name can neither substitute observations nor race this write.
        if result.exit_code == 0:
            evidence = Path(work_dir) / '.platform-cell-evidence.json'
            if evidence.is_dir():
                shutil.rmtree(evidence)
            elif evidence.exists():
                evidence.unlink()
            evidence.write_text(json.dumps(captured, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        return replace(result, logs=''.join(plain_logs))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def execute_prediction(*, run_id, lease_token, image_ref, artifact_dir, input_dir, output_dir,
                       resources, settings, should_continue=None, on_log=None):
    return _execute(run_id=run_id, lease_token=lease_token, phase="predict", image_ref=image_ref,
        mounts=[(artifact_dir, "/submission"), (input_dir, "/input")],
        command=["python", "/submission/predict.py", "--input", "/input/samples.csv",
            "--model-dir", "/submission", "--output", "/output/predictions.csv", "--device", "cpu"],
        output_mount="/output", output_dir=output_dir, resources=resources, settings=settings,
        should_continue=should_continue, on_log=on_log)


def execute_check(*, run_id, lease_token, image_ref, script, evidence_dir, output_dir,
                  resources, settings, should_continue=None, on_log=None):
    """Teacher checker sees code/predictions/aggregate evidence, never secrets."""
    directory = _runner_dir(settings, run_id, lease_token, "check")
    try:
        (directory / "check.py").write_text(script, encoding="utf-8")
        return _execute(run_id=run_id, lease_token=lease_token, phase="check", image_ref=image_ref,
            mounts=[(directory, "/runner"), (evidence_dir, "/evidence")],
            command=["python", "/runner/check.py", "--evidence-dir", "/evidence", "--output-dir", "/output"],
            output_mount="/output", output_dir=output_dir, resources=resources, settings=settings,
            should_continue=should_continue, on_log=on_log)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
