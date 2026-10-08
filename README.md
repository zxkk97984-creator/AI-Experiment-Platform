# 人工智能基础实验平台

人工智能基础实验平台面向人工智能、机器学习和深度学习课程，提供课程学习、在线 Python 实验、作业与考试、Docker 隔离判题、AI 辅助评分、Notebook/Studio、环境档位和教师工作台。

当前仓库已标记为 `V0.1`。核心代码、数据库迁移、自动化测试和生产 Compose 已具备；真实部署机上的 Docker daemon、Registry、镜像推送/回拉和生产数据恢复演练仍需按部署环境完成，不能用受限开发沙箱替代。发布证据边界见 [`docs/production-evidence-checklist.md`](docs/production-evidence-checklist.md)。

本轮新增单校教学功能：管理员分配授课实例、共享课程资料与章节活动、独立班级任务、逐 Cell 检查与同班排行榜、两级课程成绩簿，以及独立16位随机临时密码和限时凭据交付。旧课程绑定需管理员核对，闭学期和归档实例只读，历史成绩不自动重算。操作、迁移与试点步骤见 [`docs/school-teaching-upgrade.md`](docs/school-teaching-upgrade.md)，验证边界见 [`docs/school-release-verification.md`](docs/school-release-verification.md)。

## 项目定位

平台解决教学过程中“课程内容、在线实验、自动判题、考试评分和教师复核”分散的问题：

```text
课程学习 → 在线实验 / 作业 / 考试 → Docker 判题 → 成绩与反馈 → 教师工作台分析
```

系统有三类业务角色：

- 学生：浏览课程、学习课时、运行 Notebook、提交作业、参加考试和查看成绩；
- 教师：管理课程/课时/作业/考试/实验，查看统一提交中心，进行 AI 复核和成绩管理；
- 管理员：管理用户、教务数据、环境档位、实验模块和全局提交。

## 当前核心功能

- 课程、章节、Markdown/视频课时、课程封面、学习进度和名单 CSV 导入；
- Python 函数题、公开样例自测、隐藏测试、异步 Redis 队列和 Docker 沙箱；
- 限时考试、选择题/代码题、自动交卷、重判、成绩汇总和教师逐题改分；
- 实验模块、不可变 Notebook 模板版本、内置 Notebook Player、Studio 编辑器、导入/导出和资源清单；
- AI 代码评分的测试组、Rubric、`legacy`/`shadow`/`active` 模式、输出校验和教师终审；
- 结构化日志与可观测性：JSON 行文件日志（按大小轮转）、request_id 贯穿、敏感字段脱敏和管理员系统日志页（见 [系统日志与可观测性](#系统日志与可观测性)）；
- 环境 Profile/Draft/immutable Version、Python/pip/apt 包校验、异步构建、Registry digest 校验、发布和回滚；
- 教师 Dashboard、统一提交中心、全局搜索、通知、成绩统计、班级/学员、设置和环境选择；
- JWT Access Token + HttpOnly Refresh Token、Redis 限流/黑名单、角色和资源级权限。

AI 生产开关默认关闭。开启前必须完成 [`docs/ai-data-governance.md`](docs/ai-data-governance.md) 中的审批门；关闭时不产生外部 AI 调用，人工评分路径仍可用。

## 技术栈

| 层 | 当前实现 |
| --- | --- |
| 前端 | Vue 3.5.39、Vite 6.4.3、Pinia 2.3.1、Vue Router 4.6.4、CodeMirror 6、Marked |
| 前端测试 | Vitest 4.1.10、Vue Test Utils、Playwright 1.62.0、ESLint 10.8.1 |
| 后端 | Python 3.12、FastAPI 0.115.6、Uvicorn 0.32.1、SQLAlchemy 2.0.36、Pydantic Settings 2.7.0 |
| 数据库迁移 | Alembic 1.14.0；生产/CI 使用 MySQL 8.0，本地 Compose 使用 MySQL 8.4 |
| 缓存/队列 | Redis 7（本地镜像 7.4-alpine，生产/CI 镜像 7-alpine） |
| 判题与交互实验 | Docker、pytest、ipykernel、jupyter-client |
| 文件存储 | 本地 Storage backend 或 S3-compatible backend（boto3） |
| CI 基线 | Node.js 20、Python 3.12、SQLite/MySQL 双库测试、Docker Compose smoke |

## 系统架构

本地开发时，MySQL 和 Redis 运行在 Docker，FastAPI、确定性 Judge Worker、Environment Builder Worker 和 Vite 在宿主机运行。生产时使用 `docker-compose.prod.yml` 将迁移、API、确定性 Worker 和 Nginx 前端容器化；AI Worker 仅在审批后显式启用 `ai` profile。

```text
浏览器
  ├─ 本地：Vite :5173 ───────────────┐
  └─ 生产：Nginx :8080 ─ /api ───────┤
                                     ▼
                           FastAPI :8000
                             ├─ MySQL
                             ├─ Redis
                             ├─ Judge Worker → Docker 判题容器
                             ├─ Kernel Manager → Docker Kernel 容器
                             ├─ Environment Builder → Docker / Registry
                             └─ AI-compatible API（显式启用时）
```

详细模块边界、数据流和兼容路径见 [`docs/架构设计总览.md`](docs/架构设计总览.md)。

## 仓库结构

```text
.
├── backend/
│   ├── app/
│   │   ├── api/              # FastAPI Router
│   │   ├── models/           # SQLAlchemy 模型
│   │   ├── schemas/          # Pydantic 请求/响应模型
│   │   ├── services/         # 业务、判题、AI、环境、存储和 Studio 服务
│   │   ├── seed_demo/        # 幂等 Demo 数据播种与校验
│   │   └── worker/           # Judge Worker、Environment Builder Worker
│   ├── alembic/versions/     # 数据库迁移
│   ├── docker/judge/         # 判题镜像
│   ├── docker/kernel/        # Notebook Kernel 镜像
│   ├── lesson_content/       # 课程教学内容
│   ├── storage/              # 本地运行/业务文件存储
│   ├── logs/                 # 运行时文件日志（gitignore，管理员日志页读取）
│   └── tests/automated/      # 后端自动化测试
├── frontend/
│   ├── src/                  # Vue 页面、组件、API、Store 和工具
│   ├── e2e/                  # Playwright 端到端测试
│   └── public/               # 静态资源
├── deploy/                   # Registry Secret 示例
├── docker/                   # 本地兼容 Jupyter 配置
├── docs/                     # 当前架构、运维、安全和设计规范
├── scripts/                  # 启动、关闭、seed、检查和回填脚本
├── docker-compose.yml        # 本地 MySQL/Redis，含 legacy Jupyter profile
├── docker-compose.prod.yml   # 生产全栈编排
├── docker-compose.e2e-local.yml # E2E 本地端口覆盖
└── README.md
```

`docs/archive/` 只保存历史调查、收口报告和已完成计划，不作为当前运行或部署依据。构建产物、依赖目录、测试截图、`.env` 和 `judge-work/` 均不应提交。

## 环境要求

- Docker Engine/Desktop，且当前用户有权访问 Docker daemon；
- Python 3.12（版本基线见 `backend/.python-version`）；
- Node.js 20（CI、Vitest 和 Playwright 基线）；
- Docker Compose v2。

各平台启动方式：

| 平台 | 支持情况 |
| --- | --- |
| Linux | 完整支持：一键脚本和手动命令都可用（一键脚本额外需要 `curl`、`setsid`、`ps`） |
| macOS | 手动命令全部可用（`.venv/bin/python` 等 Unix 路径写法一致）；一键脚本不兼容，原因见下文 |
| Windows | 原生 PowerShell 仅支持手动模式；需要一键脚本时请使用 WSL2 并按 Linux 流程操作 |

下文 Bash 示例统一使用 `.venv/bin/python` 写法；Windows PowerShell 中对应 `.venv\Scripts\python.exe`。

## 跨机器复现边界

另一台机器从 Git 克隆**已推送的提交**后，可以重建源码、依赖、空数据库结构和确定性
Demo 业务数据；Git 仓库不会携带当前电脑上的 MySQL 数据卷、Redis AOF、上传文件、
Docker 镜像、Registry 凭据、生产 `.env` 或运行日志。

| 内容 | 能否仅靠 Git/README 复现 | 正确方式 |
| --- | --- | --- |
| 前后端源码与依赖 | 可以 | Python 3.12 安装 `requirements-dev.txt`；Node 20 使用 `npm ci` |
| 空库结构与 Demo 业务数据 | 可以 | 统一 bootstrap 后执行 `seed-demo --force-fixture`；结果不依赖本机 Docker 判题镜像 |
| 真实判题/Notebook 镜像 | 不可以自动复制 | 在目标机重建，或从批准的 Registry/镜像归档拉取并验证 |
| 当前 MySQL/上传文件 | 不可以 | 按备份恢复方案单独迁移 MySQL 与 Storage；不要把 Demo seed 当作生产数据备份 |
| 生产 TLS、域名、代理 CIDR、容量 | 不可以 | 由学校部署方在目标拓扑中配置和验收 |

最小跨机功能复现建议先保持 AI 关闭并使用确定性 Fixture：

```bash
git clone https://github.com/zxkk97984-creator/dai-experiment-platform.git
cd dai-experiment-platform
git checkout Xiaoxiao/pre-release-remediation

cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
# 编辑 backend/.env：保持 DAI_ENVIRONMENT=development，并设置 DAI_AI_ENABLED=false

cd ..
docker compose up -d --wait mysql redis
cd backend
.venv/bin/python ../scripts/bootstrap_database.py
.venv/bin/python -m app.cli seed-demo --force-fixture

cd ../frontend
npm ci
npm test
npm run build
```

上述流程用于验证代码、数据库结构和 Demo 主链路，不是生产发布证据。需要真实 Docker
判题时，再按下文构建 Judge/Kernel 和 basic 环境镜像。

账号、Excel 导入、批量重置、独立账号 Worker 和迁移说明见 [账号与名单管理](docs/account-management.md)。

实验数据、后台训练、模型评测与 AI 评分的使用和部署见 [实验教学与自动评测](docs/lab-learning.md)。

## 本地开发

### 1. 安装后端依赖与开发配置

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Windows PowerShell 对应命令：

```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

`backend/app/config.py` 从当前工作目录的 `.env` 加载配置，因此开发配置应放在 `backend/.env`。至少检查数据库、Redis、密钥和 CORS：

```env
DAI_DATABASE_URL=mysql+pymysql://dai:dai_password@localhost:3306/dai_platform
DAI_REDIS_URL=redis://localhost:6379/0
DAI_SECRET_KEY=replace-with-a-long-random-secret
DAI_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

`backend/.env.example` 还包含判题、Kernel、Storage 和 AI 开发配置。不要把真实 API key 或生产密钥写入 Git。

### 2. 启动基础设施、迁移和管理员

手动模式先启动本地 MySQL/Redis：

```bash
cd ..
docker compose up -d mysql redis

cd backend
.venv/bin/python ../scripts/bootstrap_database.py
.venv/bin/python -m app.cli create-admin \
  --username admin \
  --password 'Test1234!' \
  --real-name Administrator
```

Windows PowerShell 对应命令：

```powershell
cd ..
docker compose up -d mysql redis

cd backend
.venv\Scripts\python.exe ..\scripts\bootstrap_database.py
.venv\Scripts\python.exe -m app.cli create-admin `
  --username admin `
  --password 'Test1234!' `
  --real-name Administrator
```

首次需要判题和 Notebook 镜像时构建（三个平台的 `docker build` 命令完全一致）：

```bash
cd ..
docker build -t dai-judge-python:latest backend/docker/judge
docker build -t dai-kernel-python:latest backend/docker/kernel
```

### 3. 推荐：一键启动完整开发栈

在项目根目录可直接使用统一入口：

```bash
./start.sh                 # 启动并打开浏览器
./start.sh --no-browser    # 启动但不打开浏览器
./start.sh status          # 查看状态
./start.sh logs api        # 查看 API 日志
./start.sh stop            # 停止项目
```

`start.sh` 会复用下方的 `scripts/dev-up.sh` / `scripts/dev-down.sh`，两种入口行为一致。

> **平台兼容性说明**：该脚本仅支持 Linux。它依赖 Linux 特有的 `setsid` 命令创建独立进程组，并用 `/proc/<pid>/cwd` 校验进程归属；macOS 没有内置 `setsid`，也没有 `/proc` 文件系统，Windows 原生 shell 两者皆无，因此这两个平台无法直接运行此脚本。macOS 用户请使用第 4 节的手动模式（命令完全一致）；Windows 用户建议在 WSL2 内按本节执行（Docker Desktop 启用 WSL2 后端后，WSL2 内可直接使用 `docker compose` 和 `npm`）。

```bash
DAI_DEV_NO_BROWSER=1 ./scripts/dev-up.sh
```

脚本会按顺序执行 MySQL/Redis 健康检查、统一 disposable 两阶段数据库 bootstrap、判题镜像构建、API、确定性 Judge Worker、Environment Builder Worker 和 Vite 启动，并检查 API ready 与前端 HTTP 状态。脚本会对子进程固定 `DAI_ENVIRONMENT=development` 与 `DAI_MIGRATION_MODE=disposable`，因此即使 `backend/.env` 是生产模板也不会把本地入口切换成生产迁移；生产真实 digest 只在生产 Compose/手动入口提供。AI Worker 不会随默认开发栈启动。

常用变量：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `DAI_PYTHON` | `backend/.venv/bin/python` | 后端解释器 |
| `DAI_DEV_API_PORT` | `8000` | API 监听端口 |
| `DAI_DEV_RUN_DIR` | `/tmp/dai-dev` | PID 与日志目录 |
| `DAI_DEV_NO_BROWSER` | 未设置 | 设为 `1` 禁止自动打开浏览器 |

关闭开发栈：

```bash
./scripts/dev-down.sh
```

### 4. 手动分别启动

以下命令适用于 Linux 和 macOS（每个服务一个终端）：

```bash
# 终端 1：API（在 backend 目录）
cd backend
.venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2：作业/考试确定性判题 Worker（默认不消费 AI 队列）
cd backend
.venv/bin/python -m app.worker.judge_worker

# 只有完成 AI 审批并显式启用时，另开终端启动 AI Worker
DAI_WORKER_ROLE=ai DAI_AI_ENABLED=true .venv/bin/python -m app.worker.judge_worker

# 终端 3：环境构建 Worker（需要 Docker daemon）
cd backend
.venv/bin/python -m app.worker.environment_builder_worker

# 终端 4：前端
cd frontend
npm ci
npm run dev
```

Windows PowerShell 原生环境对应命令（Docker Desktop 需正在运行）：

```powershell
# 终端 1：API（在 backend 目录）
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2：作业/考试确定性判题 Worker（默认不消费 AI 队列）
cd backend
.venv\Scripts\python.exe -m app.worker.judge_worker

# 终端 3：环境构建 Worker
cd backend
.venv\Scripts\python.exe -m app.worker.environment_builder_worker

# 终端 4：前端
cd frontend
npm ci
npm run dev
```

API 端口变化时同步设置前端代理：

```bash
DAI_DEV_API_PORT=8001 ./scripts/dev-up.sh
# 或手动：
VITE_API_PROXY_TARGET=http://localhost:8001 npm run dev
```

PowerShell 中环境变量需要用 `$env:` 前缀设置：

```powershell
$env:VITE_API_PROXY_TARGET = "http://localhost:8001"
npm run dev
```

访问地址：

- 前端：<http://localhost:5173>
- API Swagger：<http://localhost:8000/docs>
- 存活检查：<http://localhost:8000/api/v1/health/live>
- 就绪检查：<http://localhost:8000/api/v1/health/ready>

### 5. Demo 数据与环境种子

环境档位种子（需要数据库；`--enqueue` 还需要 Redis 和 Environment Builder）：

```bash
cd backend
.venv/bin/python -m app.cli seed-environments
.venv/bin/python -m app.cli seed-environments --enqueue
```

Demo 数据是幂等的；统一 disposable bootstrap 会创建可用于 Fixture 演示的 basic 记录。
跨机器验证推荐显式使用 `--force-fixture`，避免把目标机尚未准备好的 Docker 镜像误当成
Demo 数据前置。需要验证真实判题时，才执行 `seed-environments --enqueue` 并启动
Environment Builder：

```bash
cd backend
.venv/bin/python -m app.cli seed-demo
.venv/bin/python -m app.cli seed-demo --force-fixture
.venv/bin/python -m app.cli seed-demo --reset-demo
```

Windows PowerShell 用户将上述命令中的 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe` 即可。

详细账号、前置条件、故事线和重置边界见 [`docs/demo-data.md`](docs/demo-data.md)。

## 环境变量与端口

### 配置文件

| 文件 | 用途 |
| --- | --- |
| `backend/.env.example` | 宿主机开发 API/Worker 配置 |
| `.env.example` | 生产 Compose 配置示例，复制为根目录 `.env` |
| `deploy/registry-docker-config.example.json` | 不含凭据的 Registry Docker config 示例 |

关键变量按职责分组如下，完整默认值以两个 `.env.example` 和 Compose 文件为准：

| 组 | 变量 |
| --- | --- |
| 基础连接 | `DAI_ENVIRONMENT`、`DAI_DATABASE_URL`、`DAI_REDIS_URL`、`DAI_SECRET_KEY`、`DAI_CORS_ORIGINS`、`DAI_TRUSTED_PROXY_CIDRS` |
| 日志 | `DAI_LOG_DIR`、`DAI_LOG_MAX_BYTES`、`DAI_LOG_BACKUP_COUNT` |
| Compose 数据库 | `DAI_DB_USER`、`DAI_DB_PASSWORD`、`DAI_DB_ROOT_PASSWORD` |
| 判题 | `DAI_JUDGE_IMAGE`、`DAI_KERNEL_IMAGE`、`DAI_JUDGE_TIMEOUT_SECONDS`、`DAI_JUDGE_MEMORY_LIMIT_MB`、`DAI_JUDGE_CPU_LIMIT`、`DAI_JUDGE_HOST_WORK_DIR` |
| Storage | `DAI_STORAGE_BACKEND`、`DAI_STORAGE_S3_*`、`DAI_VIDEO_*`、`DAI_COVER_*` |
| 数据库 bootstrap | `DAI_MIGRATION_MODE`、生产真实 `DAI_BASIC_ENVIRONMENT_IMAGE_DIGEST`、`DAI_BASIC_ENVIRONMENT_BASE_IMAGE` |
| 可复现镜像 | `DAI_MYSQL_IMAGE`、`DAI_REDIS_IMAGE`、`DAI_BACKEND_BASE_IMAGE`、`DAI_FRONTEND_NODE_BASE_IMAGE`、`DAI_FRONTEND_NGINX_BASE_IMAGE`；Judge/Kernel 构建参数 `DAI_JUDGE_BASE_IMAGE`、`DAI_KERNEL_BASE_IMAGE` |
| 环境 V2 | `DAI_ENVIRONMENT_EDITOR_V2_ENABLED`、`DAI_ENV_BASE_IMAGE`、`DAI_ENV_PYTHON_BASE_IMAGES`、`DAI_ENV_REGISTRY_REPOSITORY`、`DAI_ENV_REGISTRY_DOCKER_CONFIG_FILE` 及构建资源限制变量 |
| Worker/AI | `DAI_WORKER_ROLE`、`DAI_AI_ENABLED`、`DAI_AI_BASE_URL`、`DAI_AI_API_KEY`、`DAI_AI_MODEL`、超时/重试/队列变量 |

### 端口与服务

| 场景 | 服务 | 默认端口 |
| --- | --- | --- |
| 本地 Compose | MySQL | `127.0.0.1:3306` |
| 本地 Compose | Redis | `127.0.0.1:6379` |
| 本地 Vite | 前端 | `127.0.0.1:5173` |
| 本地 API | FastAPI | `0.0.0.0:8000`，可由 `DAI_DEV_API_PORT` 改变 |
| legacy profile | Jupyter | `127.0.0.1:8888`，默认不启动 |
| 生产 Compose | Nginx 前端 | `${FRONTEND_PORT:-8080}:80` |
| E2E 覆盖 | MySQL / Redis | `3309:3306` / `6380:6379` |

生产 Compose 不发布 MySQL、Redis 或 API 的宿主机端口，应用通过内部网络访问它们。

登录限流默认 fail-closed：API 只有在 `DAI_TRUSTED_PROXY_CIDRS` 显式包含直接反向代理
peer（以及 X-Forwarded-For 中除最左客户端地址外的可信中间跳）时才解析 XFF；未配置时使用
直连 peer。生产 Compose 中直连 peer 是 Nginx 容器，留空会让全部浏览器共享该容器的
IP 限流桶（默认 15 分钟 30 次尝试），因此留空只适合首次配置检查，**正式多人使用前必须填写**。
边界代理必须先清洗客户端提交的 XFF，再按真实代理链追加地址，不能把任意客户端可控的
header 直接转发给 API。

## Docker Compose 生产部署

生产部署必须在真实部署机执行，且部署机用户能够访问 Docker daemon。先复制配置并替换所有占位值：

```bash
cp .env.example .env
mkdir -p /opt/dai/judge-work
# 编辑 .env：密钥、CORS、数据库密码、判题目录、digest 和 Registry 配置
docker compose -f docker-compose.prod.yml config -q
```

至少需要确认：

- `DAI_SECRET_KEY` 是唯一的生产密钥；
- `DAI_CORS_ORIGINS` 是实际前端来源，不能是 `*` 或 localhost；
- `DAI_DB_USER`、`DAI_DB_PASSWORD`、`DAI_DB_ROOT_PASSWORD` 已替换；
- `DAI_TRUSTED_PROXY_CIDRS` 只填写直接 Nginx 所在 Compose 子网，以及 XFF 中真实可信的外层
  LB/代理 CIDR；不确定时先停止多人发布，不要长期留空，也禁止使用 `0.0.0.0/0`；
- `DAI_JUDGE_HOST_WORK_DIR` 是部署机绝对路径；
- `DAI_ENV_BASE_IMAGE` 带真实 `@sha256:` digest；
- `DAI_BACKEND_BASE_IMAGE`、前端 Node/Nginx 及 Judge/Kernel 的基础镜像在构建时使用已审核的真实 digest；
- `DAI_JUDGE_IMAGE`、`DAI_KERNEL_IMAGE` 已替换为本次 smoke 产物的完整不可变
  `repository@sha256:...` Registry 引用；生产校验不接受 `latest` 或裸本机 image ID；
- 全新库 bootstrap 提供真实的 `DAI_BASIC_ENVIRONMENT_IMAGE_DIGEST`，不能用 `000...`、`111...` 或 disposable digest；
- `DAI_ENV_REGISTRY_DOCKER_CONFIG_FILE` 指向部署机 root-owned、只读的真实 Docker config；
- 生产 AI 默认保持 `DAI_AI_ENABLED=false`，启用前完成数据治理审批。

启动 MySQL/Redis 后可先确定当前 Compose 项目和内部网络，再把实际子网写回 `.env`：

```bash
docker network ls --filter label=com.docker.compose.project=dai-experiment-platform
# 默认项目名对应以下网络；如果使用了 -p，请替换项目名前缀。只复制 Subnet，不复制 Gateway
docker network inspect dai-experiment-platform_internal \
  --format '{{(index .IPAM.Config 0).Subnet}}'
```

如果学校网关/LB 位于 Nginx 之前，还必须把 XFF 中会出现的可信中间代理 CIDR 一并加入，
然后用两个不同客户端地址验证不会共享同一个 Redis `rl:ip:*` 键。

全新库或缺少 basic 可用环境版本时，必须使用 Compose 的统一 bootstrap 入口：

> 先完成下文“Judge / Kernel 生产镜像”和“首个 basic 环境镜像”，并把验证后的引用写入
> `.env`；没有这些产物时不要执行生产 bootstrap。

```bash
docker compose -f docker-compose.prod.yml up -d --wait mysql redis
# .env 必须提供真实 basic digest 和基础镜像 digest；不得使用 000/111 或 disposable 值
docker compose -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.prod.yml up -d --wait
docker compose -f docker-compose.prod.yml ps
# 审批完成后才可额外启用 AI Worker；默认不启动 ai profile。
# ai-worker 只消费 AI 队列，不持有 Docker Socket、judge-work 或 Registry Secret。
docker compose --profile ai -f docker-compose.prod.yml up -d ai-worker
```

检查健康状态和环境构建日志：

```bash
curl -fsS http://localhost:8080/api/v1/health/live
curl -fsS http://localhost:8080/api/v1/health/ready
docker compose -f docker-compose.prod.yml logs --tail=200 environment-builder
```

环境 V2 的真实构建必须完成 Docker build、smoke、Registry push、按 digest pull-back、数据库 `available` 和教师旧绑定回归；仅完成 API 测试不等于生产验收。

## 系统日志与可观测性

平台内置两层可观测性设施，用于快速定位 AI 评分、判题与基础设施异常。

### 文件日志（API 与 Worker）

- API 与判题 Worker 分别写 `backend/logs/dai-api.log`、`backend/logs/dai-worker.log`（生产 Compose 中为共享卷 `/app/logs`）；
- 统一 JSON 行格式：时间、级别、logger、`request_id`（rid）与结构化附加字段（如 `operation`、`completion_tokens`、`finish_reason`、`attempts`）；
- 按大小轮转（默认 20MB × 10 份），目录不可写时自动降级为仅控制台，不阻断启动；
- 敏感字段（API Key、Authorization 等）在 JSON 输出时自动剔除，学生代码原文按约定从不入日志；
- 配置：`DAI_LOG_DIR`（置空禁用）、`DAI_LOG_MAX_BYTES`、`DAI_LOG_BACKUP_COUNT`。

### 管理员系统日志页

管理员登录后，侧栏「系统 → 系统日志」进入（`/admin/logs`），能力包括：

- API 服务 / 判题 Worker 双来源切换，含轮转副本；
- 级别过滤（ERROR / WARNING 及以上）、按消息内容、logger 或 request_id 的关键词搜索；
- 「AI 事件」一键过滤 `ai_` 前缀事件（`ai_chat_completed` / `ai_retries_exhausted` 等），快速定位 AI 调用失败；
- 结构化附加字段与异常堆栈展开、可选 10 秒自动刷新；
- 仅 admin 角色可访问（后端同样校验），读取路径限定在日志目录内，防路径穿越。

对应后端 API：`GET /api/v1/admin/logs` 与 `GET /api/v1/admin/logs/files`。

### 常见故障的日志特征速查

| 日志特征 | 含义 | 处置 |
| --- | --- | --- |
| `finish_reason=length` / `completion_tokens == max_tokens` | completion 预算耗尽（推理模型推理挤占正文） | 调高 `OPERATION_MAX_TOKENS` 对应预算 |
| `ai_retries_exhausted` | AI 调用重试耗尽，任务转人工 | AI 评分复核工作台重试或覆盖 |
| `ExamSubmission ... 转 review_required` | 考试提交转人工复核 | 成绩详情逐题处理（补救通道可自动收口） |
| `http_401/403`（AI） | AI API Key 失效 | 更换 `DAI_AI_API_KEY` 后重启 |
| `timeout` / `http_5xx`（AI） | 上游 AI 服务抖动 | 稍后重试或联系服务商 |

### 生产日志边界（已知限制）

- stdout 日志由 Compose `json-file` 驱动限制（50MB × 5 份），文件日志存于 `app_logs` 卷，随卷生命周期保留；
- 当前为单机日志视图；将来 API 多副本部署时需引入集中式日志（Loki/ELK）后再扩展；
- 尚无主动告警：错误率突增或 `review_required` 积压超阈值时的通知（邮件/webhook）属于后续增强。

## 数据库迁移、初始化与回滚

开发库：

```bash
cd backend
.venv/bin/python ../scripts/bootstrap_database.py
.venv/bin/python -m alembic current
.venv/bin/python -m alembic heads
```

生产迁移由 Compose 的 `migrate` 一次性服务执行。生产默认只做 forward migration，不用 `downgrade` 代替业务回滚；备份、恢复和 V2 回滚约束见：

- [`docs/environment-profiles.md`](docs/environment-profiles.md)
- [`docs/backup-restore.md`](docs/backup-restore.md)

历史提交测试计数需要回填时：

```bash
cd backend
.venv/bin/python ../scripts/backfill_test_counts.py --dry-run
.venv/bin/python ../scripts/backfill_test_counts.py
```

## 测试、Lint 与构建

### 后端

```bash
cd backend
.venv/bin/python -m pytest -q
.venv/bin/ruff check app/
.venv/bin/python -m alembic check  # 需要可连接的数据库
```

CI 会分别运行 SQLite 和 MySQL 测试，并跳过需要真实 Docker/Kernel 的历史实验测试文件；完整 CI 门禁见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

### 前端

```bash
cd frontend
npm ci
npm run lint
npm test
npm run build

# 首次运行 E2E 前安装 Playwright 浏览器（一次即可）
npx playwright install

# 等 API、前端和数据库由外部提供后运行
npm run e2e
```

#### Judge / Kernel 生产镜像

生产 `Settings` 只接受 `repository@sha256:...`，不会接受 `latest` 或裸本机 image ID。
因此必须先登录学校批准的 Registry，使用固定基础镜像构建、smoke、推送并回拉：

```bash
# 把仓库地址替换为学校实际 Registry；基础镜像值复制自已审核的生产 .env
RELEASE_TAG="$(git rev-parse --short=12 HEAD)"
JUDGE_TAG="registry.school.example/dai/judge:${RELEASE_TAG}"
KERNEL_TAG="registry.school.example/dai/kernel:${RELEASE_TAG}"
JUDGE_BASE_REF='python:3.12-slim@sha256:<真实多架构digest>'
KERNEL_BASE_REF='python:3.12-slim@sha256:<真实多架构digest>'

docker build --build-arg DAI_JUDGE_BASE_IMAGE="$JUDGE_BASE_REF" \
  -t "$JUDGE_TAG" backend/docker/judge
docker run --rm --network none "$JUDGE_TAG" \
  python -c "import pytest, numpy, pandas, sklearn"
docker push "$JUDGE_TAG"
docker image inspect "$JUDGE_TAG" \
  --format '{{index .RepoDigests 0}}'

docker build --build-arg DAI_KERNEL_BASE_IMAGE="$KERNEL_BASE_REF" \
  -t "$KERNEL_TAG" backend/docker/kernel
docker run --rm --network none "$KERNEL_TAG" \
  python -c "import ipykernel, numpy, pandas, sklearn, matplotlib"
docker push "$KERNEL_TAG"
docker image inspect "$KERNEL_TAG" \
  --format '{{index .RepoDigests 0}}'
```

将两条 `RepoDigests` 的完整输出分别写入 `DAI_JUDGE_IMAGE` 和 `DAI_KERNEL_IMAGE`，再执行
`docker pull repository@sha256:...` 与 `docker image inspect repository@sha256:...` 回拉验证。

#### 首个 basic 环境镜像

全新生产库不能用占位 digest 通过 bootstrap。当前 Legacy basic seed 字段保存的是目标
Docker daemon 可直接解析的本机 image ID（裸 `sha256:...`），而环境 V2 才使用完整的
`repository@sha256:...`。两者不能混填。

首发支持的跨机路径是：在**全新、隔离、无业务数据**的 disposable Compose 项目中构建，
执行 smoke 后 `docker save`；在学校目标机 `docker load` 并重新读取 image ID。不要把脚本
指向已有开发库或生产库，因为构建器读取该隔离库中的首个 basic 版本：

```bash
# 在隔离项目完成 disposable bootstrap；.env.basic-build 不得复用生产数据库或数据卷
cp .env.example .env.basic-build
# 编辑 .env.basic-build：使用独立密码，并设置：
# DAI_ENVIRONMENT=development
# DAI_MIGRATION_MODE=disposable
# DAI_DB_ROOT_PASSWORD=staging-root-password（必须与下方脚本参数相同）
# DAI_ENV_BASE_IMAGE=python:3.12-slim@sha256:<真实多架构digest>
docker compose -p dai-basic-build --env-file .env.basic-build \
  -f docker-compose.prod.yml up -d --wait mysql redis
docker compose -p dai-basic-build --env-file .env.basic-build \
  -f docker-compose.prod.yml run --rm migrate python /scripts/bootstrap_database.py

# DAI_ENV_BASE_IMAGE 必须与隔离库 basic v1 的 base_image_ref 相同且为真实 digest
DAI_BASIC_BUILD_RELEASE=true \
DAI_BASIC_IMAGE_ARCHIVE="$PWD/artifacts/dai-env-basic.tar" \
DAI_DB_ROOT_PASSWORD='staging-root-password' \
DAI_ENV_BASE_IMAGE='python:3.12-slim@sha256:<真实多架构digest>' \
COMPOSE_FILES='-p dai-basic-build --env-file .env.basic-build -f docker-compose.prod.yml' \
bash scripts/build-basic-environment-ci.sh
```

把归档安全传到目标机后执行：

```bash
docker load -i /path/to/dai-env-basic.tar
docker image inspect dai-env-basic:ci --format '{{.Id}}'
docker run --rm --network none dai-env-basic:ci \
  python -c "import ipykernel, pytest; print('basic image smoke: ok')"
```

把目标机 `docker image inspect` 返回的裸 `sha256:...` 写入
`DAI_BASIC_ENVIRONMENT_IMAGE_DIGEST`。当前不要把
`DAI_BASIC_IMAGE_REGISTRY_REPOSITORY` 返回的完整 Registry 引用写入该 Legacy 字段；
它与 seed 校验及本机运行语义不同。禁止使用临时 tag、全 0/全 1 digest 或示例值。
basic 构建不会自动复制 MySQL、上传文件、Registry 凭据或生产 `.env`。

也可以使用：

```bash
npm run check  # lint + unit test + production build
```

### 安全与 Compose 检查

```bash
# 根目录执行
python3 scripts/check_sca.py
docker compose -f docker-compose.prod.yml config -q
./scripts/verify_host_isolation.sh

# 生产 Compose 启动后
BASE_URL=http://localhost:8080 ./scripts/check_security_headers.sh
```

`check_sca.py` 需要当前 Python 环境已安装 `pip-audit`；生产安全头检查需要运行中的 Nginx 前端。

## API 与兼容路径注意事项

- API 前缀统一为 `/api/v1`；Swagger 在 `/docs`，live/ready 在 `/api/v1/health/live` 和 `/api/v1/health/ready`。
- `/api/v1/notebooks` 已终止兼容转发，旧路径统一返回 `410 DEPRECATED` 并提示迁移；新代码使用 `/api/v1/experiments` 和 `/api/v1/studio`。
- `/api/v1/jupyter/entry` 仅在显式启用 legacy Jupyter 时可用；模板列表/复制接口返回 `410 JUPYTER_TEMPLATES_RETIRED`，新流程使用内置 Notebook Player。
- 生产 `worker` 只消费普通判题/考试队列；`ai-worker` 位于默认关闭的 `ai` profile，只消费 AI 队列。
- 环境已绑定版本运行时使用不可变 digest；`latest` 只允许出现在未绑定存量兼容路径或本地开发配置。
- `backend/storage/`、命名 Docker volume 和 `judge-work/` 含运行/业务数据，清理缓存时不要递归删除。
- 生产 Docker socket 具有主机级风险，不要用 `chmod 666 /var/run/docker.sock`，也不要开放 2375/2376。

## 当前状态与后续收尾

当前 V0.1 的代码和自动化门禁已完成，仍需部署环境配合的事项：

1. 在真实部署机验证 Docker daemon、socket 权限、Registry Secret 和环境 V2 真实构建/推送/回拉；
2. 完成生产数据库、Redis、Storage 的备份恢复演练，并补齐责任人和外部签字记录；
3. 逐页移除前端 `teacher-management.css` 迁移桥接层，计划见 [`docs/ui-v2-migration-plan.md`](docs/ui-v2-migration-plan.md)；
4. 部署方按 [`docs/production-evidence-checklist.md`](docs/production-evidence-checklist.md) 补齐真实外部门禁；在证据完成前保持 AI 关闭。

## 当前文档地图

- [`docs/架构设计总览.md`](docs/架构设计总览.md)：当前系统怎么设计；
- [`docs/environment-profiles.md`](docs/environment-profiles.md)：环境 V2、迁移、Registry 和运维；
- [`docs/demo-data.md`](docs/demo-data.md)：Demo 数据播种与验证；
- [`docs/backup-restore.md`](docs/backup-restore.md)：备份恢复上线边界；
- [`docs/production-evidence-checklist.md`](docs/production-evidence-checklist.md)：生产证据责任方、路径和阻断条件；
- [`docs/ai-data-governance.md`](docs/ai-data-governance.md)：AI 数据治理上线门；
- [`docs/dai-design-system-v2.md`](docs/dai-design-system-v2.md)：前端设计系统；
- [`docs/security/`](docs/security/)：Docker socket、TLS、安全依赖和安全头；
- [`DAI智能代码评分方案_V1.md`](DAI智能代码评分方案_V1.md)：AI 代码评分公式和 Rubric 设计方案；
- [`docs/archive/`](docs/archive/)：历史报告和已完成计划，仅供追溯。
