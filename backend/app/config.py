import re
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_REGISTRY_REPOSITORY_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?"
    r"(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*$"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DAI_",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(self, **values):
        """Honor ``DAI_ENV_FILE`` at the settings boundary.

        An explicit ``_env_file`` (including ``None``) supplied by a caller wins.
        Otherwise a configured path is used; an empty value disables dotenv, while
        an absent variable preserves the normal local ``.env`` behavior.
        """
        if "_env_file" not in values and "DAI_ENV_FILE" in os.environ:
            configured = os.environ["DAI_ENV_FILE"].strip()
            values["_env_file"] = configured or None
        super().__init__(**values)

    app_name: str = "DAI Experiment Platform API"
    environment: str = "development"
    # ── 文件日志（结构化 JSON，按大小轮转；管理员日志页直读此文件）──────
    # 置空字符串可禁用。API 与 worker 默认同目录不同文件，避免多进程写同一文件。
    log_dir: str = "logs"
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 10
    database_url: str = "mysql+pymysql://dai:dai_password@localhost:3306/dai_platform"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = Field(default="change-me-in-production", min_length=1)
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    jupyter_base_url: str = "http://localhost:8888"
    judge_queue_name: str = "judge:queue"
    judge_image: str = "dai-judge-python:latest"
    judge_use_docker: bool = True
    judge_timeout_seconds: int = 10
    # 数据库连接池（容量验收暴露的配置缺口）：默认 5+10 在 100 并发学生下会被打满，
    # 请求排队 30 秒并把整个 API 拖死（连 /health/live 都不响应）。
    # 这里的默认值按"单实例 100+ 并发学生"留出余量；等待时间有界，
    # 连接耗尽时快速失败而不是无限排队。
    db_pool_size: int = 20
    db_max_overflow: int = 40
    db_pool_timeout: float = 5.0
    db_pool_recycle: int = 1800
    judge_memory_limit_mb: int = 256
    judge_cpu_limit: float = 1.0
    # stale running 判定的上界：必须覆盖最坏判题墙钟
    # （测试组数 × 每组超时 + 容器启动 + 镜像拉取余量）。
    # 判题在测试组之间续 heartbeat_at；只有 Worker 真的崩溃/失去认领时才会超过它。
    judge_stale_running_seconds: int = 1800
    # ── Kernel 镜像（Phase 5） ────────────────────────────────
    # 已绑定环境版本的实验记录用环境 digest 启动；该配置仅用于未绑定环境版本的
    # 存量兼容路径与开发环境。安全参数不在此处放松。
    kernel_image: str = "dai-kernel-python:latest"
    jupyter_enabled: bool = False
    # ── 交互内核运行配置（Q1） ────────────────────────────────
    # 单格执行超时默认 120 秒（教师可设 10–1800 秒）；内核空闲回收默认 900 秒
    # （教师可设 300–7200 秒）。发布后锁定在模板版本上，已有实验继续用原配置。
    kernel_cell_timeout_seconds: int = Field(default=120, ge=10, le=1800)
    kernel_idle_seconds: int = Field(default=900, ge=300, le=7200)
    worker_role: Literal["judge", "ai", "kernel"] = "judge"
    studio_storage_dir: str = str(
        Path(__file__).resolve().parents[1] / "storage" / "studio"
    )
    # 教师上传视频存储目录——生产必须挂载持久卷，不能依赖容器可写层
    video_storage_dir: str = str(
        Path(__file__).resolve().parents[1] / "storage" / "videos"
    )
    video_max_upload_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    video_playback_url_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    # 课程封面存储目录——生产必须挂载持久卷（cover_data），与视频目录同规则
    cover_storage_dir: str = str(
        Path(__file__).resolve().parents[1] / "storage" / "covers"
    )
    cover_max_upload_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    material_storage_dir: str = ""
    material_max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    material_large_max_upload_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    material_url_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    # Storage backend selection is deliberately provider-neutral.  Development
    # defaults to local files; S3-compatible deployments configure the
    # endpoint and credentials without changing business services or DB keys.
    storage_backend: Literal["local", "s3"] = "local"
    storage_s3_endpoint_url: str | None = None
    storage_s3_bucket: str = "dai-platform"
    storage_s3_region: str = "us-east-1"
    storage_s3_access_key_id: SecretStr = SecretStr("")
    storage_s3_secret_access_key: SecretStr = SecretStr("")
    storage_s3_session_token: SecretStr = SecretStr("")
    storage_s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"
    storage_s3_key_prefix: str = ""
    # 判题临时文件目录——Docker Compose 下必须与 judge 容器挂载相同路径
    judge_work_dir: str = ""
    # 宿主机侧判题工作目录——DoD 模式下传给 Docker daemon 的宿主机绝对路径
    # 未设置时回退到 judge_work_dir（适用于非 DoD / 开发环境）
    judge_host_work_dir: str = ""

    # ── AI 智能代码评分（DeepSeek） ──
    # TASK-020（F-21）：AI 数据治理上线门——审批完成前默认关闭。
    # 生产通过 DAI_AI_ENABLED=true 显式启用（compose 默认 -false）；关闭时零外呼、保留人工评分。
    ai_enabled: bool = False
    ai_base_url: str = "https://aihub.codingpython.cn"
    ai_api_key: SecretStr = SecretStr("")
    ai_model: str = "deepseek-v4-flash"
    ai_timeout_seconds: float = Field(default=60.0, gt=0, le=180)
    ai_max_retries: int = Field(default=3, ge=0, le=8)
    # 测试组生成是同步教师请求，且 reasoning 模型输出大：单次调用放宽超时，
    # 模型层不自动重试（业务层 generate_test_groups 还有一次修复生成），
    # 避免“60s × 多次重试”把请求拖到前端超时之后。
    ai_test_group_timeout_seconds: float = Field(default=120.0, gt=0, le=300)
    ai_test_group_max_retries: int = Field(default=0, ge=0, le=3)
    ai_queue_name: str = "judge:ai:queue"
    # 一键 AI 配置（批次 2）：生成任务走独立队列，不挤占正式判题容量
    ai_setup_queue_name: str = "judge:ai-setup:queue"
    # 单次成绩异议（批次 2）：结果对学生可见起可申请的窗口天数
    appeal_window_days: int = Field(default=7, ge=1, le=90)

    @property
    def ai_ready(self) -> bool:
        """AI 评分是否就绪：启用且已配置 API Key"""
        return self.ai_enabled and bool(self.ai_api_key.get_secret_value().strip())

    @model_validator(mode="after")
    def _validate_production(self):
        """生产环境安全校验——不通过直接抛异常阻止启动"""
        if self.environment != "production":
            return self

        errors = []
        # 密钥校验
        if not self.secret_key or self.secret_key == "change-me-in-production":
            errors.append("DAI_SECRET_KEY 未设置或使用了默认值，生产环境必须设置唯一密钥")
        elif len(self.secret_key) < 16:
            errors.append("DAI_SECRET_KEY 长度不足（至少 16 字符）")

        # 数据库密码校验
        if "dai_password" in self.database_url or "change_me" in self.database_url:
            errors.append("DAI_DATABASE_URL 使用了默认密码，生产环境必须使用唯一密码")

        # Legacy judge/kernel fallbacks are still supported in development, but
        # production must never resolve a mutable tag such as ``latest``.
        for label, image_ref in (
            ("DAI_JUDGE_IMAGE", self.judge_image),
            ("DAI_KERNEL_IMAGE", self.kernel_image),
        ):
            if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image_ref):
                errors.append(
                    f"{label} 必须是带 @sha256: digest 的不可变镜像引用，拒绝可变标签"
                )

        # CORS 校验
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        if not origins:
            errors.append("DAI_CORS_ORIGINS 未设置，生产环境必须指定实际域名")
        # 拒绝任何 localhost/127.0.0.1 起源（生产不应使用开发地址）
        localhost_origins = [o for o in origins if "localhost" in o or "127.0.0.1" in o or "::1" in o]
        if localhost_origins:
            errors.append(f"DAI_CORS_ORIGINS 包含本地开发地址: {', '.join(localhost_origins)}，生产环境必须使用实际域名")
        if "*" in origins:
            errors.append("DAI_CORS_ORIGINS 不允许使用通配符 *")

        # Docker-outside-of-Docker 模式中，容器内目录不能直接交给宿主机
        # Docker daemon。配置了共享工作目录时，必须同时给出宿主机绝对路径。
        if self.judge_work_dir and not self.judge_host_work_dir:
            errors.append(
                "DAI_JUDGE_HOST_WORK_DIR 未设置，生产环境判题容器无法挂载宿主机工作目录"
            )

        # V1 环境档位基础镜像——V2 使用逐 Python 版本的 digest 映射，
        # 因此开启 V2 后旧的单一配置只作为兼容字段，不再阻止启动。
        if not self.environment_editor_v2_enabled and not re.fullmatch(
            r"[^\s@]+@sha256:[0-9a-f]{64}", self.env_base_image
        ):
            errors.append(
                "DAI_ENV_BASE_IMAGE 必须是带 @sha256: digest 的基础镜像引用"
                f"（当前: {self.env_base_image}），拒绝可变标签"
            )

        if self.environment_editor_v2_enabled:
            supported = {"3.10", "3.11", "3.12"}
            if not self.env_registry_repository or not _REGISTRY_REPOSITORY_RE.fullmatch(
                self.env_registry_repository
            ) or any(part in {".", ".."} for part in self.env_registry_repository.split("/")):
                errors.append(
                    "DAI_ENV_REGISTRY_REPOSITORY 未设置或格式无效，V2 必须使用可拉取的 Registry 镜像"
                )
            if not self.env_registry_allow_anonymous and not Path(
                self.env_registry_docker_config
            ).is_file():
                errors.append(
                    "DAI_ENV_REGISTRY_DOCKER_CONFIG 未挂载有效 Docker config.json；"
                    "如 Registry 允许匿名访问，请显式设置 DAI_ENV_REGISTRY_ALLOW_ANONYMOUS=true"
                )
            if set(self.env_python_base_images) != supported:
                errors.append(
                    "DAI_ENV_PYTHON_BASE_IMAGES 必须恰好配置 3.10、3.11、3.12"
                )
            for python_version, image_ref in self.env_python_base_images.items():
                if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image_ref):
                    errors.append(
                        "DAI_ENV_PYTHON_BASE_IMAGES["
                        f"{python_version}] 必须是带 @sha256: digest 的基础镜像引用"
                    )

        if errors:
            raise ValueError("生产环境配置校验失败:\n  - " + "\n  - ".join(errors))
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def studio_storage_path(self) -> Path:
        return Path(self.studio_storage_dir).resolve()

    @property
    def video_storage_path(self) -> Path:
        return Path(self.video_storage_dir).resolve()

    @property
    def cover_storage_path(self) -> Path:
        return Path(self.cover_storage_dir).resolve()

    @property
    def material_storage_path(self) -> Path:
        return Path(self.material_storage_dir).resolve() if self.material_storage_dir else self.studio_storage_path.parent / "materials"

    # Software environments are versioned separately from host resource budgets.
    lab_enabled: bool = True
    lab_worker_mode: Literal['general', 'benchmark'] = 'general'
    lab_worker_node_id: str = Field(default='', pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?$')
    lab_benchmark_node_id: str = Field(default='', pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?$')
    lab_storage_dir: str = ""
    lab_upload_max_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    lab_expanded_max_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    lab_file_max_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    lab_max_files: int = Field(default=100000, ge=1)
    lab_artifact_quota_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    lab_training_concurrency: int = Field(default=1, ge=1, le=64)
    lab_total_cpus: float = Field(default=4, gt=0, le=1024)
    lab_total_memory_mb: int = Field(default=8192, ge=1024)
    lab_interactive_reserved_memory_mb: int = Field(default=2048, ge=256)
    lab_max_kernels: int = Field(default=20, ge=1)
    lab_gpu_enabled: bool = False
    lab_gpu_devices: str = ""
    lab_log_max_bytes: int = Field(default=1048576, ge=4096, le=8 * 1024 * 1024)
    lab_queue_poll_seconds: float = Field(default=2, gt=0, le=60)
    lab_lease_seconds: int = Field(default=120, ge=30)
    lab_max_attempts: int = Field(default=3, ge=1, le=10)
    lab_min_free_disk_mb: int = Field(default=2048, ge=64)

    @model_validator(mode='after')
    def _validate_benchmark_node(self):
        if self.lab_worker_mode == 'benchmark' and (not self.lab_benchmark_node_id
                or self.lab_worker_node_id != self.lab_benchmark_node_id):
            raise ValueError('专用基准 worker 必须配置相同的本机和目标 Docker daemon ID')
        return self

    @property
    def lab_storage_path(self) -> Path:
        return Path(self.lab_storage_dir).resolve() if self.lab_storage_dir else self.studio_storage_path.parent / "lab"

    @property
    def lab_work_path(self) -> Path:
        return (Path(self.judge_work_dir).resolve() if self.judge_work_dir else self.studio_storage_path.parent / "judge-work") / "labs"

    # ── 考试秩序（批次 3） ────────────────────────────────────
    # 教师延时是明确留痕的例外：单场考试累计延长不得超过该上限。
    exam_extension_max_minutes: int = Field(default=60, ge=1, le=180)

    # ── 登录限流 ──
    account_security_keys: str = ""  # Comma-separated Fernet keys, newest first; separate from JWT.
    account_worker_hash_concurrency: int = Field(default=2, ge=1, le=8)
    account_batch_retention_days: int = Field(default=90, ge=1)
    account_audit_retention_days: int = Field(default=365, ge=90)
    account_credential_export_hours: int = Field(default=24, ge=1, le=72)
    login_rate_limit_window_seconds: int = Field(default=900, ge=60)
    login_rate_limit_user_max_failures: int = Field(default=10, ge=1)
    login_rate_limit_ip_max_attempts: int = Field(default=30, ge=1)
    # Legacy switch retained for configuration compatibility. XFF is ignored
    # unless the immediate peer also matches trusted_proxy_cidrs.
    trusted_proxy: bool = False
    # Comma-separated IP/CIDR allow-list for the immediate proxy peer and
    # every intermediate proxy hop represented in X-Forwarded-For.
    trusted_proxy_cidrs: str = ""

    # ── 环境档位控制面（Phase 1 / V2） ────────────────────────
    # V2 默认关闭：先部署 additive schema、API 和 Worker，再通过配置切换。
    environment_editor_v2_enabled: bool = False
    env_build_queue_name: str = "environment:build:queue"
    env_build_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    env_image_repository: str = "dai-env"
    # V2 的正式产物仓库。为空时开发配置仍可加载，但真实 V2 构建会在
    # preflight 阶段 fail-closed；生产配置在启动时直接拒绝。
    env_registry_repository: str | None = None
    # 标准 Docker config.json 的只读 Secret 挂载点。配置文件只允许包含
    # auths，禁止 credsStore/credHelpers/proxies，避免把宿主配置带入构建。
    env_registry_docker_config: str = "/run/secrets/config.json"
    # 仅开发/明确的公开 Registry 可打开；生产默认要求 Secret。
    env_registry_allow_anonymous: bool = False
    env_base_image: str = "python:3.12-slim"
    env_build_log_max_bytes: int = Field(default=60 * 1024, ge=1024, le=1024 * 1024)
    # 本地开发：pip 镜像源（国内网络直连 PyPI 不稳定；构建环境镜像时注入 --index-url）
    env_pip_index_url: str | None = None
    # V2：每个受支持的 Python 小版本均使用平台固定的基础镜像引用。
    # 开发默认使用可读标签；生产且 V2 开启时由 model_validator 强制 digest。
    env_python_base_images: dict[str, str] = Field(
        default_factory=lambda: {
            "3.10": "python:3.10-slim-bookworm",
            "3.11": "python:3.11-slim-bookworm",
            "3.12": "python:3.12-slim-bookworm",
        }
    )
    # 按 Python 基础镜像绑定的 Debian 快照源。构建器只读取配置，
    # 管理员不能通过请求传入源地址。
    env_apt_snapshot_sources: dict[str, list[str]] = Field(default_factory=dict)
    env_platform_python_packages: dict[str, str] = Field(
        default_factory=lambda: {"ipykernel": "6.29.5", "pytest": "8.3.4"}
    )
    env_platform_bundle_version: str = "v1"
    env_build_network_mode: Literal["default", "host"] = "default"
    env_build_http_proxy: str | None = None
    env_build_cpu_limit: float = Field(default=2.0, gt=0, le=16)
    env_build_memory_mb: int = Field(default=4096, ge=512, le=65536)
    env_build_pids_limit: int = Field(default=512, ge=64, le=32768)
    # Worker readiness is a short-lived Redis lease.  The API never assumes
    # that a running container means the asynchronous builder can consume a
    # job; it requires this key to be refreshed by a healthy worker.
    env_builder_heartbeat_key: str = "environment:v2:builder:heartbeat"
    env_builder_heartbeat_ttl_seconds: int = Field(default=30, ge=5, le=300)
    env_builder_heartbeat_interval_seconds: int = Field(default=10, ge=1, le=120)
    env_apt_deny_patterns: list[str] = Field(
        default_factory=lambda: [
            r"^(docker|docker\.io|docker-ce|docker-ce-cli|moby-engine|containerd|containerd\.io|cri-o|podman|runc)(?:-.*)?$",
            r"^(systemd|sysvinit|openrc)(?:-.*)?$",
            r"^(sudo|doas|policykit-1)(?:-.*)?$",
            r"^(openssh-server|dropbear)(?:-.*)?$",
            r"^(linux-image|linux-headers|iptables|nftables|ufw)(?:-.*)?$",
        ]
    )
    env_build_max_image_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
