"""
Kernel Session 管理器

架构：
- docker run 启动持久 ipykernel（network none，无 -p），conn file 在 /tmp/conn.json
- 每次 execute 用 docker exec -i <container> python /.dai/kernel_runner.py
- trusted runner 在容器内通过 jupyter_client.BlockingKernelClient 连接 ipykernel
- 学生代码通过 stdin JSON 传入，不出现于 argv
- 单格超时来自模板版本/配置（10–1800s）：超时只中断当前执行，保留 kernel 与内存变量；
  只有内核探测无响应才销毁重建，并在结果里标记 state_lost（「状态已丢失，请从头重跑」）
- Redis 执行锁从「nx ex=60 无续期」改为租约 + 心跳续期，长执行不会被放行第二个执行者
- Redis 持久化 session + hidden init marker
"""
import json
import logging
import math
import os
import secrets
import subprocess
import threading
import time
from typing import Optional

from app.config import Settings, get_settings

logger = logging.getLogger("kernel_manager")

# 兼容回退值：生产调用方（学生执行 / 隐藏初始化 / 教师预览）必须显式传
# timeout_seconds（模板版本或配置），只有未传的老调用点才落到这里。
KERNEL_HARD_TIMEOUT = 30
# 超时后探测内核是否还能响应（runner 会等 ipykernel 的 idle 状态）。
KERNEL_PROBE_TIMEOUT = 8
# 超时/取消后等 runner 自然收尾的宽限期：拿到部分输出并确认当前执行已结束。
KERNEL_INTERRUPT_GRACE = 5
# 执行锁租约：TTL 必须大于续期间隔（TTL/3），否则长执行中途锁会过期。
EXEC_LOCK_TTL = 60
RUNNER_PATH = "/opt/dai/kernel_runner.py"
LEGACY_RUNNER_PATH = "/.dai/kernel_runner.py"

# 超时只中断当前执行（保留变量）时的用户可见文案
TIMEOUT_PRESERVED_MESSAGE = (
    "单元格执行超过 {seconds} 秒，已中断本次执行：内核与内存变量已保留；"
    "中断不撤销代码已经产生的修改"
)
# 内核无响应、必须销毁重建时的用户可见文案（状态确实丢失）
STATE_LOST_MESSAGE = (
    "内核无响应，已重建：内存中的变量、导入与模型状态已丢失，请从头重新运行全部单元格"
)


class KernelSession:
    """单个 kernel session 的运行时状态

    Phase 5：增加 environment_version_id / image_ref，Redis 元数据与 Docker label
    均持久化环境身份，恢复时校验；环境不匹配即销毁重建（计划 9.3）。
    """

    def __init__(self, record_id: int, container_name: str, conn_info: dict,
                 lesson_storage_dir: str = "",
                 initialized_template_version_id: int | None = None,
                 environment_version_id: int | None = None,
                 image_ref: str | None = None,
                 runner_path: str | None = None,
                 memory_mb: int = 256,
                 cpus: float = 1.0,
                 resource_token: str | None = None):
        self.record_id = record_id
        self.container_name = container_name
        self.conn_info = conn_info
        self.lesson_storage_dir = lesson_storage_dir
        self.initialized_template_version_id = initialized_template_version_id
        self.environment_version_id = environment_version_id
        self.image_ref = image_ref
        self.runner_path = runner_path
        self.memory_mb = memory_mb
        self.cpus = cpus
        self.resource_token = resource_token
        self.last_active_at = time.time()

    @property
    def is_alive(self) -> bool:
        result = subprocess.run(
            ["docker", "ps", "-q", "-f", f"name={self.container_name}"],
            capture_output=True, text=True,
        )
        return bool(result.stdout.strip())

    def matches_environment(self, image_ref: str | None,
                            environment_version_id: int | None) -> bool:
        """环境身份是否与期望一致。

        期望值 None 表示「未绑定环境版本」的存量兼容路径——不强制匹配（旧 session 可复用）；
        期望值非 None 时，session 记录缺失（旧格式）或不一致 → 不匹配，需重建。
        """
        if environment_version_id is not None:
            if self.environment_version_id != environment_version_id:
                return False
        if image_ref is not None:
            if self.image_ref != image_ref:
                return False
        return True

    def to_redis_dict(self) -> dict:
        return {
            "container_name": self.container_name,
            "conn_info": self.conn_info,
            "lesson_storage_dir": self.lesson_storage_dir,
            "initialized_template_version_id": self.initialized_template_version_id,
            "environment_version_id": self.environment_version_id,
            "image_ref": self.image_ref,
            "runner_path": self.runner_path,
            "memory_mb": self.memory_mb,
            "cpus": self.cpus,
            "resource_token": self.resource_token,
        }

    @classmethod
    def from_redis_dict(cls, record_id: int, data: dict) -> "KernelSession":
        return cls(
            record_id=record_id,
            container_name=data["container_name"],
            conn_info=data["conn_info"],
            lesson_storage_dir=data.get("lesson_storage_dir", ""),
            initialized_template_version_id=data.get("initialized_template_version_id"),
            environment_version_id=data.get("environment_version_id"),
            image_ref=data.get("image_ref"),
            runner_path=data.get("runner_path"),
            memory_mb=data.get("memory_mb", 256),
            cpus=data.get("cpus", 1.0),
            resource_token=data.get("resource_token"),
        )


class KernelManager:
    """管理所有 kernel session 的生命周期"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._sessions: dict[int, KernelSession] = {}
        # Factories come only from an authenticated caller's db bind. Never
        # silently reach the application's global DB from a test or preview.
        self._resource_factories = {}
        self._resource_tokens = {}

    def _reserve(self, record_id, token, memory_mb, cpus, factory):
        from app.services.lab_resources import reserve_resources, touch_resources
        with factory() as db:
            reservation = reserve_resources(db, key=f"kernel:{record_id}", token=token,
                kind="kernel", cpus=cpus, memory_mb=memory_mb, settings=self.settings)
            if reservation is None:
                raise RuntimeError("交互计算资源已占满，请稍后再试")
            touch_resources(db, f"kernel:{record_id}", token, self.settings)
            db.commit()
        self._resource_factories[record_id] = factory
        self._resource_tokens[record_id] = token

    def _touch_resources(self, record_id, session):
        factory = self._resource_factories.get(record_id)
        if factory is None:
            return
        from app.services.lab_resources import touch_resources
        with factory() as db:
            if not touch_resources(db, f"kernel:{record_id}", session.resource_token, self.settings):
                raise RuntimeError("交互计算资源租约已失效，请重启实验内核")
            db.commit()

    def _adopt_resources(self, session, factory):
        factory = factory or self._resource_factories.get(session.record_id)
        if factory is not None:
            token = session.resource_token or secrets.token_hex(16)
            self._reserve(session.record_id, token, session.memory_mb, session.cpus, factory)
            session.resource_token = token
            self._write_session_redis(session.record_id, session)
        session.last_active_at = time.time()

    @staticmethod
    def _remove_container(container_name):
        try:
            result = subprocess.run(["docker", "rm", "-f", container_name],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("无法确认实验容器已停止，计算资源暂不释放") from exc
        if result.returncode and "No such container" not in (result.stderr or ""):
            raise RuntimeError("无法确认实验容器已停止，计算资源暂不释放")

    def _make_container_name(self, record_id: int) -> str:
        return f"dai-kernel-rec-{record_id}"

    def _make_lock_token(self) -> str:
        return secrets.token_hex(16)

    def _kernel_path(self, *parts: str) -> str:
        """返回 API/Worker 容器内可写的 Kernel 共享目录路径。"""
        if self.settings.judge_work_dir:
            return os.path.join(self.settings.judge_work_dir, *parts)
        return os.path.join(os.environ.get("TEMP", "/tmp"), *parts)

    def _docker_host_path(self, container_path: str) -> str:
        """把共享目录中的容器路径转换为 Docker daemon 可见的宿主机路径。"""
        container_root = self.settings.judge_work_dir.replace("\\", "/").rstrip("/")
        host_root = self.settings.judge_host_work_dir.replace("\\", "/").rstrip("/")
        normalized = container_path.replace("\\", "/")
        if container_root and host_root and (
            normalized == container_root or normalized.startswith(f"{container_root}/")
        ):
            relative = normalized[len(container_root):].lstrip("/")
            return f"{host_root}/{relative}" if relative else host_root
        return container_path

    def _generate_conn_file(self, record_id: int) -> tuple[str, dict]:
        from jupyter_client import write_connection_file

        tmp_dir = self._kernel_path("kernels")
        os.makedirs(tmp_dir, exist_ok=True)
        conn_path = os.path.join(tmp_dir, f"kernel-rec-{record_id}.json")
        key = secrets.token_hex(24).encode("ascii")
        # Kernel containers use ``--network none`` and do not publish ports, so
        # every container has its own network namespace.  Supplying the ports
        # explicitly avoids jupyter_client opening host sockets merely to
        # discover free ports.  That probe is both unnecessary here and fails
        # in restricted workers where socket creation is denied.
        write_connection_file(
            conn_path,
            ip="0.0.0.0",
            key=key,
            shell_port=20001,
            iopub_port=20002,
            stdin_port=20003,
            control_port=20004,
            hb_port=20005,
        )
        with open(conn_path) as f:
            conn_info = json.load(f)
        conn_info["ip"] = "0.0.0.0"
        with open(conn_path, "w") as f:
            json.dump(conn_info, f)
        # Kernel sandbox 以 UID 1000 运行；文件仍以 :ro 挂载，但必须可读。
        os.chmod(conn_path, 0o644)
        return conn_path, conn_info

    def _write_session_redis(self, record_id: int, session: KernelSession):
        """写 Redis session 元数据"""
        try:
            import redis
            r = redis.from_url(self.settings.redis_url)
            r.setex(
                f"kernel:session:{record_id}", 3600,
                json.dumps(session.to_redis_dict()),
            )
        except Exception as e:
            raise RuntimeError(f"Redis 写入失败: {e}") from e

    def _read_session_redis(self, record_id: int) -> KernelSession | None:
        try:
            import redis
            r = redis.from_url(self.settings.redis_url)
            data_str = r.get(f"kernel:session:{record_id}")
            if data_str:
                return KernelSession.from_redis_dict(record_id, json.loads(data_str))
        except Exception:
            logger.debug("Redis 读取 kernel session %d 失败", record_id, exc_info=True)
        return None

    def is_template_initialized(self, record_id: int, version_id: int) -> bool:
        """检查 template version 是否已初始化"""
        try:
            import redis
            r = redis.from_url(self.settings.redis_url)
            marker = r.get(f"kernel:init:{record_id}")
            if marker:
                return int(marker) == version_id
        except Exception:
            logger.debug("Redis 读取 kernel init 标记 %d 失败", record_id, exc_info=True)
        session = self._sessions.get(record_id)
        return (session is not None and
                session.initialized_template_version_id == version_id)

    def mark_template_initialized(self, record_id: int, version_id: int):
        """标记 template version 已初始化（Redis + 内存）"""
        session = self._sessions.get(record_id)
        if not session:
            raise RuntimeError(f"Kernel session {record_id} 不可用")

        previous_version_id = session.initialized_template_version_id
        try:
            import redis
            r = redis.from_url(self.settings.redis_url)
            r.setex(f"kernel:init:{record_id}", 3600, str(version_id))
            session.initialized_template_version_id = version_id
            self._write_session_redis(record_id, session)
        except Exception as exc:
            session.initialized_template_version_id = previous_version_id
            try:
                r.delete(f"kernel:init:{record_id}")
            except Exception:
                logger.warning("Redis 删除 kernel init 标记 %d 失败", record_id, exc_info=True)
            raise RuntimeError(f"Kernel 初始化标记持久化失败: {exc}") from exc

    def create_session(self, record_id: int, lesson_storage_dir: str = "", *,
                       image_ref: str | None = None, environment_version_id: int | None = None,
                       memory_mb: int | None = None, cpus: float | None = None,
                       resource_factory=None) -> KernelSession:
        from app.services.lab_node import require_interactive_node
        require_interactive_node(self.settings)
        memory_mb = int(memory_mb if memory_mb is not None else self.settings.judge_memory_limit_mb)
        cpus = float(cpus if cpus is not None else self.settings.judge_cpu_limit)
        if memory_mb < 128 or not math.isfinite(cpus) or cpus <= 0:
            raise RuntimeError("实验运行资源配置无效")
        factory = resource_factory or self._resource_factories.get(record_id)
        token = secrets.token_hex(16) if factory is not None else None
        if factory is not None:
            self._reserve(record_id, token, memory_mb, cpus, factory)
        try:
            return self._launch_session(record_id, lesson_storage_dir, image_ref=image_ref,
                environment_version_id=environment_version_id, memory_mb=memory_mb,
                cpus=cpus, resource_token=token)
        except Exception:
            # The reservation stays occupied if Docker cannot confirm removal.
            self.destroy(record_id)
            raise

    def _launch_session(self, record_id: int,
                       lesson_storage_dir: str = "",
                       *,
                       image_ref: str | None = None,
                       environment_version_id: int | None = None,
                       memory_mb: int, cpus: float, resource_token: str | None) -> KernelSession:
        """创建 Kernel 容器（Phase 5：镜像参数使用环境 digest）。

        - 位置参数保持旧签名兼容：create_session(record_id, lesson_storage_dir)。
        - image_ref：环境版本不可变镜像引用；None 仅用于未绑定环境版本的存量兼容
          路径（回退 settings.kernel_image），与 exam 判题路径同一原则。
        - environment_version_id：持久化到 Redis 元数据与 Docker label，供恢复校验。
        - 安全参数全部保留：--network none / --cap-drop ALL / no-new-privileges /
          --read-only / tmpfs / pids / cpu / 内存限制——只改镜像，不放松安全。
        """
        from app.services.lab_node import require_interactive_node
        require_interactive_node(self.settings)
        container_name = self._make_container_name(record_id)
        self._remove_container(container_name)

        conn_path, conn_info = self._generate_conn_file(record_id)

        work_dir = self._kernel_path("workspaces", f"student_{record_id}")
        os.makedirs(work_dir, exist_ok=True)
        host_conn_path = self._docker_host_path(conn_path)
        host_work_dir = self._docker_host_path(work_dir)

        # OpenBLAS 默认按宿主机核数创建线程（如 20 线程），与 ipykernel 自身线程、
        # docker exec 进程竞争 pids 配额，曾导致 "pthread_create failed" 后容器
        # 半死、exec 报 "procReady not received"。限制 BLAS 线程数后，
        # pids-limit 保持安全基线 50 即可满足沙箱内线程需求。
        cmd = [
            "docker", "run", "-d", "--name", container_name,
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:exec,size=64m",
            "--cpus", f"{cpus:g}", "--memory", f"{memory_mb}m", "--pids-limit", "50",
            "-e", "OPENBLAS_NUM_THREADS=4",
            "-e", "OMP_NUM_THREADS=4",
            "-e", "MKL_NUM_THREADS=4",
            "-l", f"dai.record_id={record_id}",
        ]
        if environment_version_id is not None:
            cmd.extend(["-l", f"dai.environment_version_id={environment_version_id}"])
        if image_ref:
            cmd.extend(["-l", f"dai.image_digest={image_ref}"])
        if resource_token:
            cmd.extend(["-l", f"dai.lab_resource_token={resource_token}"])
        cmd.extend(["-l", f"dai.memory_mb={memory_mb}", "-l", f"dai.cpus={cpus:g}"])
        if lesson_storage_dir:
            cmd.extend(["-l", f"dai.course_dir={lesson_storage_dir}"])
        cmd.extend([
            "-v", f"{host_conn_path}:/tmp/conn.json:ro",
            "-v", f"{host_work_dir}:/work:rw",
        ])
        if os.path.isdir(lesson_storage_dir):
            cmd.extend(["-v", f"{self._docker_host_path(lesson_storage_dir)}:/course:ro"])
        cmd.extend([image_ref or self.settings.kernel_image,
                    "python", "-m", "ipykernel_launcher", "-f", "/tmp/conn.json"])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Docker 启动失败: {result.stderr}")

        time.sleep(4)
        for _ in range(10):
            alive = subprocess.run(
                ["docker", "ps", "-q", "-f", f"name={container_name}"],
                capture_output=True, text=True,
            )
            if alive.stdout.strip():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Kernel 容器未存活")

        # 容器 alive ≠ 容器可执行：ipykernel 启动（导入 numpy/pandas 等）期间
        # docker exec 会报 "procReady not received"。轮询探测 exec 可用后再返回，
        # runner 内部另有 wait_for_ready(10s) 等待 kernel 就绪。
        for _ in range(15):
            try:
                probe = subprocess.run(
                    ["docker", "exec", container_name, "python", "-c", "import os"],
                    capture_output=True, text=True,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                # 探测异常（如超时销毁重建场景）视为暂不可用，继续重试
                logger.warning("Kernel 容器 exec 探测异常 record_id=%d: %s", record_id, exc)
                probe = None
            if probe is not None and probe.returncode == 0:
                break
            time.sleep(1)
        else:
            # 探测始终未通过：容器已死则销毁并报错；容器仍存活则降级继续
            # （runner 内部 wait_for_ready 兜底），避免超时销毁重建路径丢失 session
            alive = subprocess.run(
                ["docker", "ps", "-q", "-f", f"name={container_name}"],
                capture_output=True, text=True,
            )
            if not alive.stdout.strip():
                raise RuntimeError("Kernel 容器 exec 探测超时")
            logger.warning(
                "Kernel 容器 exec 探测未就绪，降级继续 record_id=%d", record_id
            )

        runner_path = LEGACY_RUNNER_PATH
        if environment_version_id is not None:
            runner_probe = subprocess.run(
                ["docker", "exec", container_name, "test", "-s", RUNNER_PATH],
                capture_output=True, text=True,
            )
            if runner_probe.returncode != 0:
                detail = (runner_probe.stderr or runner_probe.stdout or "runner missing").strip()
                raise RuntimeError(
                    f"环境镜像缺少可用 Kernel runner {RUNNER_PATH}: {detail}"
                )
            runner_path = RUNNER_PATH

        session = KernelSession(record_id, container_name, conn_info,
                                lesson_storage_dir=lesson_storage_dir,
                                environment_version_id=environment_version_id,
                                image_ref=image_ref or self.settings.kernel_image,
                                runner_path=runner_path, memory_mb=memory_mb,
                                cpus=cpus, resource_token=resource_token)
        self._write_session_redis(record_id, session)
        self._sessions[record_id] = session
        return session

    def get_or_create_session(self, record_id: int,
                              lesson_storage_dir: str = "",
                              *,
                              image_ref: str | None = None,
                              environment_version_id: int | None = None,
                              memory_mb: int | None = None, cpus: float | None = None,
                              resource_factory=None) -> KernelSession:
        """获取或创建 Kernel session（计划 9.3 重建策略）。

        位置参数保持旧签名兼容：get_or_create_session(record_id, lesson_storage_dir)。

        同一 record 期望的环境与内存/Redis/Docker label 记录不一致时，
        销毁旧容器后按新 digest 重建；环境 profile 新增 v2 不主动重建任何旧 record
        （期望环境未变时仍复用）；900 秒空闲清理策略保持不变。
        """
        if resource_factory is not None:
            self._resource_factories[record_id] = resource_factory
        def matches(session):
            return (session.matches_environment(image_ref, environment_version_id)
                and (memory_mb is None or session.memory_mb == memory_mb)
                and (cpus is None or session.cpus == cpus)
                and (not lesson_storage_dir or session.lesson_storage_dir == lesson_storage_dir))

        session = self._sessions.get(record_id)
        if session:
            if session.is_alive and matches(session):
                self._adopt_resources(session, resource_factory)
                return session
            logger.info("Kernel session %d 已停止或配置不匹配，销毁重建（期望 env=%s）",
                        record_id, environment_version_id)
            self.destroy(record_id)

        session = self._read_session_redis(record_id)
        if session:
            alive = subprocess.run(
                ["docker", "ps", "-q", "-f", f"name={session.container_name}"],
                capture_output=True, text=True,
            )
            if alive.stdout.strip():
                if matches(session):
                    self._adopt_resources(session, resource_factory)
                    self._sessions[record_id] = session
                    return session
                # Redis 记录的环境与期望不一致（含旧格式无环境字段）：销毁重建
                logger.info("Kernel session %d Redis 记录环境不匹配，销毁重建", record_id)
                self._sessions[record_id] = session
                self.destroy(record_id)
            else:
                self._sessions[record_id] = session
                self.destroy(record_id)

        return self.create_session(record_id, image_ref=image_ref,
                                   environment_version_id=environment_version_id,
                                   lesson_storage_dir=lesson_storage_dir, memory_mb=memory_mb,
                                   cpus=cpus, resource_factory=resource_factory)

    def execute(self, record_id: int, code: str, *, timeout_seconds: int | None = None,
                interrupt_event=None) -> dict:
        """通过 docker exec + trusted runner 在持久 ipykernel 中执行代码。

        timeout_seconds：本次执行的超时（模板版本/配置，10–1800s）。
        interrupt_event：worker 的取消信号（threading.Event），置位时中断当前执行。

        超时语义（规格）：先只中断当前执行、保留 kernel 与变量；只有探测确认内核
        无响应才销毁重建，且返回 state_lost=True + 明确文案。
        返回 outcome ∈ succeeded | timeout | cancelled；异常仍抛 RuntimeError。
        """
        from app.services.lab_node import require_interactive_node
        require_interactive_node(self.settings)
        session = self._sessions.get(record_id)
        if not session or not session.is_alive:
            raise RuntimeError(f"Kernel session {record_id} 不可用")
        self._touch_resources(record_id, session)

        timeout = int(timeout_seconds or KERNEL_HARD_TIMEOUT)
        if timeout <= 0:
            raise RuntimeError("执行超时配置无效")

        # Redis 互斥锁（fail closed）：nx + TTL 租约，运行期间由心跳线程续期
        import redis
        lock_key = f"kernel:lock:{record_id}"
        lock_token = self._make_lock_token()
        try:
            r = redis.from_url(self.settings.redis_url)
            acquired = r.set(lock_key, lock_token, nx=True, ex=EXEC_LOCK_TTL)
            if not acquired:
                raise RuntimeError("Kernel 正忙，请等待当前代码执行完成")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Redis 不可用，无法获取执行锁: {e}") from e

        renew_stop = threading.Event()
        renew_thread = threading.Thread(
            target=self._renew_exec_lock, args=(r, lock_key, lock_token, renew_stop),
            kwargs={"ttl": EXEC_LOCK_TTL},
            daemon=True, name=f"kernel-exec-lock-{record_id}",
        )
        renew_thread.start()
        started = time.perf_counter()
        interrupted = False
        try:
            # docker exec 运行 trusted runner，代码通过 stdin JSON 传入
            runner_path = session.runner_path or (
                RUNNER_PATH if session.environment_version_id is not None
                else LEGACY_RUNNER_PATH
            )
            exec_cmd = [
                "docker", "exec", "-i", session.container_name,
                "python", runner_path,
            ]
            try:
                proc = subprocess.Popen(
                    exec_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
            except OSError as exc:
                raise RuntimeError(f"Kernel runner 启动失败: {exc}") from exc

            deadline = started + timeout
            payload = json.dumps({"code": code})
            stdout = stderr = None
            timed_out = False
            first_call = True
            while True:
                if interrupt_event is not None and interrupt_event.is_set() and not interrupted:
                    interrupted = True
                    self._send_interrupt(session)
                remaining = deadline - time.perf_counter()
                try:
                    # 轮询式等待：既能及时响应取消，也能在超时后先中断再判断。
                    # 代码只在首次 communicate(input=...) 写入 stdin —— 不能手动 close()
                    # 后再 communicate()（CPython 3.12 会 flush 已关闭的 stdin 抛 ValueError）。
                    if first_call:
                        stdout, stderr = proc.communicate(
                            input=payload, timeout=min(0.5, max(0.05, remaining)))
                    else:
                        stdout, stderr = proc.communicate(
                            timeout=min(0.5, max(0.05, remaining)))
                    break
                except subprocess.TimeoutExpired:
                    if time.perf_counter() >= deadline:
                        timed_out = True
                        self._send_interrupt(session)
                        stdout, stderr = self._collect_after_interrupt(proc)
                        break
                finally:
                    first_call = False
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            if timed_out:
                return self._timeout_outcome(session, record_id, stdout, stderr,
                                            elapsed_ms, timeout)
            if interrupted:
                # 取消：交回已产生的输出与耗时，是否落库由 worker 的终态判定决定
                return {"outputs": self._parse_runner_output(stdout, stderr),
                        "execution_time_ms": elapsed_ms, "outcome": "cancelled",
                        "state_lost": False, "timeout_seconds": timeout,
                        "message": "已中断本次执行"}
            if proc.returncode != 0:
                runner_error = (stderr or stdout or "unknown runner error").strip()
                raise RuntimeError(f"Kernel runner 执行失败: {runner_error}")
            return {"outputs": self._parse_runner_output(stdout, stderr),
                    "execution_time_ms": elapsed_ms, "outcome": "succeeded",
                    "state_lost": False, "timeout_seconds": timeout, "message": None}

        finally:
            renew_stop.set()
            renew_thread.join(timeout=5)
            # token-safe unlock
            try:
                r.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                    1, lock_key, lock_token,
                )
            except Exception:
                logger.debug("锁释放失败或已过期 record_id=%d", record_id, exc_info=True)
            session.last_active_at = time.time()

    @staticmethod
    def _renew_exec_lock(r, lock_key, lock_token, stop_event, *, ttl=EXEC_LOCK_TTL,
                         interval=None):
        """执行锁心跳：只有仍持有该 token 时才续期（CAS），避免续到别人的锁。"""
        wait = interval if interval is not None else max(0.05, ttl / 3)
        script = ("if redis.call('get', KEYS[1]) == ARGV[1] "
                  "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end")
        while not stop_event.wait(wait):
            try:
                if not r.eval(script, 1, lock_key, lock_token, ttl):
                    return
            except Exception:
                # Redis 抖动：下个周期再试。锁 TTL 仍在，不会放行第二个执行者。
                logger.debug("执行锁续期失败 %s", lock_key, exc_info=True)

    @staticmethod
    def _send_interrupt(session):
        """只中断容器内当前执行（SIGINT 给 ipykernel），不动容器、不丢变量。"""
        try:
            subprocess.run(
                ["docker", "exec", session.container_name, "kill", "-INT", "1"],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("中断 kernel 当前执行失败 record=%s: %s", session.record_id, exc)

    @classmethod
    def _collect_after_interrupt(cls, proc, *, grace=KERNEL_INTERRUPT_GRACE):
        """中断后等 runner 收尾以取回部分输出；超宽限期则杀掉 runner 进程。"""
        try:
            return proc.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                return proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                return None, None

    @staticmethod
    def _parse_runner_output(stdout, stderr) -> list[dict]:
        outputs = []
        try:
            data = json.loads((stdout or "").strip() or "{}")
            if data.get("error"):
                outputs.append({"msg_type": "error", "content": {"text": data["error"]}})
            for o in data.get("outputs", []):
                outputs.append(o)
        except json.JSONDecodeError:
            if stdout:
                outputs.append({"msg_type": "stream", "content": {"name": "stdout", "text": stdout}})
            if stderr:
                outputs.append({"msg_type": "stream", "content": {"name": "stderr", "text": stderr}})
        return outputs

    def _probe_kernel(self, session, *, timeout=KERNEL_PROBE_TIMEOUT) -> bool:
        """内核是否还能响应：跑一次最小 runner（等 ipykernel idle），失败即无响应。"""
        runner_path = session.runner_path or (
            RUNNER_PATH if session.environment_version_id is not None
            else LEGACY_RUNNER_PATH
        )
        try:
            probe = subprocess.run(
                ["docker", "exec", "-i", session.container_name, "python", runner_path],
                input=json.dumps({"code": "pass"}),
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return probe.returncode == 0

    def _timeout_outcome(self, session, record_id, stdout, stderr, elapsed_ms, timeout) -> dict:
        """超时结果：能恢复 → 保留变量；不能恢复 → 销毁重建并明确提示状态丢失。"""
        outputs = self._parse_runner_output(stdout, stderr)
        if self._probe_kernel(session):
            return {"outputs": outputs, "execution_time_ms": elapsed_ms, "outcome": "timeout",
                    "state_lost": False, "timeout_seconds": timeout,
                    "message": TIMEOUT_PRESERVED_MESSAGE.format(seconds=timeout)}
        # 内核无响应：销毁重建（内存状态确实丢失）
        rebuild_error = None
        old_storage = session.lesson_storage_dir
        try:
            self.destroy(record_id)
            self.create_session(record_id, old_storage, image_ref=session.image_ref,
                                environment_version_id=session.environment_version_id,
                                memory_mb=session.memory_mb, cpus=session.cpus)
        except Exception as exc:  # noqa: BLE001 - 重建失败也要给出明确状态丢失结论
            rebuild_error = exc
            logger.warning("Kernel %d 超时后重建失败: %s", record_id, exc, exc_info=True)
        message = STATE_LOST_MESSAGE if rebuild_error is None else (
            f"{STATE_LOST_MESSAGE}（重建失败：{rebuild_error}）"
        )
        return {"outputs": outputs, "execution_time_ms": elapsed_ms, "outcome": "failed",
                "state_lost": True, "timeout_seconds": timeout, "message": message}

    def interrupt(self, record_id: int, *, resource_factory=None):
        session = self._sessions.get(record_id) or self._read_session_redis(record_id)
        if session and session.is_alive:
            self._adopt_resources(session, resource_factory)
            self._sessions[record_id] = session
            self._touch_resources(record_id, session)
            self._send_interrupt(session)

    def restart(self, record_id: int,
                lesson_storage_dir: str = "",
                *,
                image_ref: str | None = None,
                environment_version_id: int | None = None,
                memory_mb: int | None = None, cpus: float | None = None,
                resource_factory=None) -> KernelSession:
        if resource_factory is not None:
            self._resource_factories[record_id] = resource_factory
        existing = self._sessions.get(record_id) or self._read_session_redis(record_id)
        if existing:
            self._sessions[record_id] = existing
        if not lesson_storage_dir:
            existing = self._sessions.get(record_id)
            if existing:
                lesson_storage_dir = existing.lesson_storage_dir
        # 未显式传入环境时沿用现有 session 的环境身份（重建不丢环境）
        if image_ref is None or environment_version_id is None:
            existing = self._sessions.get(record_id)
            if existing:
                if image_ref is None:
                    image_ref = existing.image_ref
                if environment_version_id is None:
                    environment_version_id = existing.environment_version_id
        self.destroy(record_id)
        return self.create_session(record_id, lesson_storage_dir,
                                   image_ref=image_ref,
                                   environment_version_id=environment_version_id,
                                   memory_mb=memory_mb if memory_mb is not None else existing.memory_mb if existing else None,
                                   cpus=cpus if cpus is not None else existing.cpus if existing else None,
                                   resource_factory=resource_factory)

    def destroy(self, record_id: int):
        session = self._sessions.get(record_id) or self._read_session_redis(record_id)
        container_name = session.container_name if session else self._make_container_name(record_id)
        self._remove_container(container_name)
        factory = self._resource_factories.get(record_id)
        token = (session.resource_token if session else None) or self._resource_tokens.get(record_id)
        if factory is not None and token:
            from app.services.lab_resources import release_resources
            with factory() as db:
                release_resources(db, f"kernel:{record_id}", token)
                db.commit()
        self._sessions.pop(record_id, None)
        self._resource_tokens.pop(record_id, None)
        try:
            import redis
            r = redis.from_url(self.settings.redis_url)
            r.delete(f"kernel:session:{record_id}")
            r.delete(f"kernel:init:{record_id}")
        except Exception:
            logger.warning("Redis 删除 kernel 记录 %d 失败", record_id, exc_info=True)

    def cleanup_idle(self, max_idle_seconds: int = 900, *, db=None):
        """回收空闲 kernel session。

        正在执行（queued/running）的内核不算空闲：回收会丢掉学生内存状态，
        因此先查 kernel_executions 是否有在途执行；查不到就 fail-closed 保留。
        """
        now = time.time()
        for rid, session in list(self._sessions.items()):
            if now - session.last_active_at <= max_idle_seconds:
                continue
            if self._has_in_flight_execution(rid, db):
                logger.info("内核 %s 有在途执行，跳过空闲回收", rid)
                continue
            try:
                self.destroy(rid)
            except RuntimeError:
                logger.warning("空闲内核 %s 暂时无法停止，保留其资源租约", rid, exc_info=True)

    def _has_in_flight_execution(self, record_id: int, db=None) -> bool:
        from app.services.kernel_queue import has_in_flight_execution
        if db is not None:
            try:
                return has_in_flight_execution(db, record_id)
            except Exception:
                logger.warning("在途执行检查失败 record=%s，保留内核", record_id, exc_info=True)
                return True
        factory = self._resource_factories.get(record_id)
        if factory is None:
            # 未绑定数据库的存量调用点（教师预览/单元测试）：保持原有回收语义
            return False
        try:
            with factory() as own:
                return has_in_flight_execution(own, record_id)
        except Exception:
            logger.warning("在途执行检查失败 record=%s，保留内核", record_id, exc_info=True)
            return True

    def recover_from_docker(self):
        """从 Docker label dai.record_id 恢复所有未记录的 session，重建 Redis 元数据"""
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", "label=dai.record_id"],
            capture_output=True, text=True,
        )
        container_ids = result.stdout.strip().split()
        for cid in container_ids:
            if not cid:
                continue
            try:
                name_result = subprocess.run(
                    ["docker", "inspect", "-f", "{{.Name}}", cid],
                    capture_output=True, text=True,
                )
                container_name = name_result.stdout.strip().lstrip("/")
                if not container_name or not container_name.startswith("dai-kernel-rec-"):
                    continue

                label_result = subprocess.run(
                    ["docker", "inspect", "-f", '{{index .Config.Labels "dai.record_id"}}', cid],
                    capture_output=True, text=True,
                )
                try:
                    record_id = int(label_result.stdout.strip())
                except ValueError:
                    continue

                # Phase 5（计划 9.3）：校验环境 label——缺失环境身份的旧容器不复用
                env_label_result = subprocess.run(
                    ["docker", "inspect", "-f", '{{index .Config.Labels "dai.environment_version_id"}}', cid],
                    capture_output=True, text=True,
                )
                digest_label_result = subprocess.run(
                    ["docker", "inspect", "-f", '{{index .Config.Labels "dai.image_digest"}}', cid],
                    capture_output=True, text=True,
                )
                env_label = env_label_result.stdout.strip()
                digest_label = digest_label_result.stdout.strip()
                try:
                    env_version_id = int(env_label) if env_label else None
                except ValueError:
                    env_version_id = None
                image_ref = digest_label or None
                if env_label and env_version_id is None:
                    # 环境 label 存在但非法：视为无环境身份，不复用
                    logger.warning("Kernel 容器 %s 环境 label 非法，不复用", cid)
                    continue
                if not env_label or not digest_label:
                    # 旧版本部署的容器（无环境 label）：不复用（get_or_create 会重建）
                    logger.info("Kernel 容器 %s 缺少环境 label，跳过恢复", cid)
                    continue

                if record_id in self._sessions:
                    continue

                # 从容器内读 conn file
                conn_info = None
                cp_result = subprocess.run(
                    ["docker", "exec", cid, "cat", "/tmp/conn.json"],
                    capture_output=True, text=True, timeout=5,
                )
                if cp_result.returncode == 0:
                    try:
                        conn_info = json.loads(cp_result.stdout)
                    except json.JSONDecodeError:
                        pass

                if not conn_info:
                    # fallback: try Redis
                    session = self._read_session_redis(record_id)
                    if session:
                        conn_info = session.conn_info

                if conn_info:
                    extra_labels = {}
                    for label in ("lab_resource_token", "memory_mb", "cpus", "course_dir"):
                        extra = subprocess.run(["docker", "inspect", "-f",
                            '{{index .Config.Labels "dai.' + label + '"}}', cid],
                            capture_output=True, text=True)
                        extra_labels[label] = extra.stdout.strip()
                    session = KernelSession(
                        record_id, container_name, conn_info,
                        environment_version_id=env_version_id,
                        image_ref=image_ref,
                        resource_token=extra_labels["lab_resource_token"] or None,
                        memory_mb=int(extra_labels["memory_mb"] or self.settings.judge_memory_limit_mb),
                        cpus=float(extra_labels["cpus"] or self.settings.judge_cpu_limit),
                        lesson_storage_dir=extra_labels["course_dir"],
                    )
                    self._sessions[record_id] = session
                    # 重建 Redis 元数据
                    try:
                        self._write_session_redis(record_id, session)
                    except Exception:
                        logger.warning("恢复 kernel session %d Redis 写入失败", record_id, exc_info=True)
                    logger.info("Recovered kernel session %d from Docker label", record_id)
            except Exception as e:
                logger.warning("Failed to recover container %s: %s", cid, e)


_kernel_manager: Optional[KernelManager] = None


def get_kernel_manager() -> KernelManager:
    global _kernel_manager
    if _kernel_manager is None:
        _kernel_manager = KernelManager()
    return _kernel_manager
