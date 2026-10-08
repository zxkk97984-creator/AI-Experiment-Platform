"""
Kernel Session 管理器

架构：
- docker run 启动持久 ipykernel（network none，无 -p），conn file 在 /tmp/conn.json
- 每次 execute 用 docker exec -i <container> python /.dai/kernel_runner.py
- trusted runner 在容器内通过 jupyter_client.BlockingKernelClient 连接 ipykernel
- 学生代码通过 stdin JSON 传入，不出现于 argv
- 硬超时 30s → rm -f 旧容器 + 立即 create_session 重建
- Redis 持久化 session + hidden init marker
"""
import json
import logging
import math
import os
import secrets
import subprocess
import time
from typing import Optional

from app.config import Settings, get_settings

logger = logging.getLogger("kernel_manager")

KERNEL_HARD_TIMEOUT = 30
RUNNER_PATH = "/opt/dai/kernel_runner.py"
LEGACY_RUNNER_PATH = "/.dai/kernel_runner.py"


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

    def execute(self, record_id: int, code: str) -> dict:
        """通过 docker exec + trusted runner 在持久 ipykernel 中执行代码"""
        from app.services.lab_node import require_interactive_node
        require_interactive_node(self.settings)
        session = self._sessions.get(record_id)
        if not session or not session.is_alive:
            raise RuntimeError(f"Kernel session {record_id} 不可用")
        self._touch_resources(record_id, session)

        # Redis 互斥锁（fail closed）
        import redis
        lock_key = f"kernel:lock:{record_id}"
        lock_token = self._make_lock_token()
        try:
            r = redis.from_url(self.settings.redis_url)
            acquired = r.set(lock_key, lock_token, nx=True, ex=60)
            if not acquired:
                raise RuntimeError("Kernel 正忙，请等待当前代码执行完成")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Redis 不可用，无法获取执行锁: {e}") from e

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
            start = time.perf_counter()
            try:
                result = subprocess.run(
                    exec_cmd,
                    input=json.dumps({"code": code}),
                    capture_output=True, text=True,
                    timeout=KERNEL_HARD_TIMEOUT,
                )
                elapsed_ms = int((time.perf_counter() - start) * 1000)
            except subprocess.TimeoutExpired:
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                # 保存旧 session 信息
                old_storage = session.lesson_storage_dir
                # 销毁旧容器
                self.destroy(record_id)
                # 立即重建
                try:
                    self.create_session(record_id, old_storage, image_ref=session.image_ref,
                        environment_version_id=session.environment_version_id,
                        memory_mb=session.memory_mb, cpus=session.cpus)
                except Exception as rebuild_err:
                    raise RuntimeError(
                        f"代码执行超时（>{KERNEL_HARD_TIMEOUT}s），重建 kernel 失败: {rebuild_err}"
                    ) from rebuild_err
                raise RuntimeError(
                    f"代码执行超时（>{KERNEL_HARD_TIMEOUT}s），kernel 已重建"
                )

            if result.returncode != 0:
                runner_error = (result.stderr or result.stdout or "unknown runner error").strip()
                raise RuntimeError(f"Kernel runner 执行失败: {runner_error}")

            # 解析 runner 输出
            outputs = []
            try:
                data = json.loads(result.stdout.strip() or "{}")
                if data.get("error"):
                    outputs.append({"msg_type": "error", "content": {"text": data["error"]}})
                for o in data.get("outputs", []):
                    outputs.append(o)
            except json.JSONDecodeError:
                if result.stdout:
                    outputs.append({"msg_type": "stream", "content": {"name": "stdout", "text": result.stdout}})
                if result.stderr:
                    outputs.append({"msg_type": "stream", "content": {"name": "stderr", "text": result.stderr}})

            return {"outputs": outputs, "execution_time_ms": elapsed_ms}

        finally:
            # token-safe unlock
            try:
                r = redis.from_url(self.settings.redis_url)
                r.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                    1, lock_key, lock_token,
                )
            except Exception:
                logger.debug("锁释放失败或已过期 record_id=%d", record_id, exc_info=True)
            session.last_active_at = time.time()

    def interrupt(self, record_id: int, *, resource_factory=None):
        session = self._sessions.get(record_id) or self._read_session_redis(record_id)
        if session and session.is_alive:
            self._adopt_resources(session, resource_factory)
            self._sessions[record_id] = session
            self._touch_resources(record_id, session)
            subprocess.run(
                ["docker", "exec", session.container_name, "kill", "-INT", "1"],
                capture_output=True,
            )

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

    def cleanup_idle(self, max_idle_seconds: int = 900):
        now = time.time()
        for rid, session in list(self._sessions.items()):
            if now - session.last_active_at > max_idle_seconds:
                try:
                    self.destroy(rid)
                except RuntimeError:
                    logger.warning("空闲内核 %s 暂时无法停止，保留其资源租约", rid, exc_info=True)

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
