# 上线前最终评估

评估基线：当前工作树代码（2026-08-22）。本报告以源码、迁移、运行配置、测试和
脚本为准；README 只作为待核对的操作入口。学校部署机尚未提供，因此真实镜像、TLS、
备份恢复、容量和宿主机隔离仍属于外部发布门禁。

## 1. 当前项目实际架构概览

| 层次 | 当前实现 |
| --- | --- |
| 前端 | Vue 3 + Vite + Pinia + Vue Router + Axios；学生、教师、管理员工作区；生产由 Nginx 提供 SPA 并反代 `/api`。 |
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2 + Pydantic Settings；课程、教务、作业、考试、成绩、实验、存储、通知和 AI 评分 API。 |
| 数据库 | SQLite 用于开发/自动化测试，MySQL 用于 Compose/生产；Alembic 当前单一 head `20260820_0003`。 |
| 异步运行时 | Redis 保存令牌、限流和队列状态；Judge Worker 处理普通判题/考试，Environment Builder 负责环境镜像，AI Worker 默认关闭。 |
| 隔离执行 | 判题和 Notebook 通过 Docker socket 启动受限容器；生产 API、确定性 Worker、Environment Builder 挂载 socket，AI Worker 不挂载。 |
| 存储与部署 | local 或 S3-compatible 对象存储；`docker-compose.prod.yml` 编排 MySQL、Redis、migrate、API、Worker、Environment Builder 和 Nginx。学校网关/LB 负责外层 TLS。 |

Demo 数据由 `seed-demo` 生成，能复现数据库中的业务样例；本仓库不会自动携带本机 MySQL、
上传文件、Docker 镜像、Registry 凭据或生产 `.env`。

## 2. 当前完成度评估

| 维度 | 结论 |
| --- | --- |
| 核心业务闭环 | 约 **90%**：账号/RBAC、课程、作业、考试、成绩、实验和存储均有真实 API、模型和前端路径。 |
| 数据库/API 一致性 | **良好**：迁移、SQLite 和 MySQL 兼容性已覆盖；成绩排序已改为 MySQL/SQLite 共同支持的 CASE NULL 排序。 |
| 自动化质量 | **良好**：前端 lint/Vitest/build（898 tests）、Ruff、SCA 和 Compose 静态配置通过；后端 SQLite 全量回归曾达到 1343 passed/3 skipped，唯一失败是宿主 `backend/.env` 污染测试配置，已隔离 fixture 且受影响 suite 21 passed；MySQL 成绩/考试回归 19 passed。 |
| 跨机器复现 | **代码与 Demo 可复现**：锁文件使用 `npm ci`，bootstrap/seed/reset 有固定入口；真实 Docker/Registry/生产数据仍需部署机证据。 |
| 生产准备度 | **仓库侧完成，外部条件未完成**：可以进入学校机器的发布演练，但不能宣称正式生产已验收。 |

## 3. 已经达到上线要求的部分

- 成绩查询的分数升/降序、提交时间排序不再生成 MySQL 不支持的 `NULLS LAST`；回归测试覆盖 MySQL/SQLite 方言。
- 教师成绩导出会按当前搜索、状态、分数段和排序条件抓取全部服务端分页结果，不再只导出当前页。
- 登录限流默认 fail-closed；`DAI_TRUSTED_PROXY_CIDRS` 已进入 `.env.example`、Compose 和文档，代理链、伪造 XFF 和共享 IP 桶有测试。
- 生产 Compose 的数据库 root/user/password 均为必填变量，缺失即在配置阶段失败，不再静默使用默认密码。
- basic 环境脚本支持真实 digest 发布模式、镜像 smoke、`docker save` 归档或 Registry 推送；拒绝全零/占位基础镜像 digest。Judge/Kernel 直接 Python 依赖已锁定版本，应用/前端构建提供基础镜像 digest 参数。
- `bootstrap_database.py`、`seed-demo`、`--reset-demo` 组成清晰的空库、首次播种、重复播种和重置流程；Demo 规模与文档已按当前实现修正（Notebook 模板/版本为 25/25）。
- AI 默认关闭；关闭时确定性判题和人工评分路径仍可用。AI Worker 不持有 Docker socket、judge-work 或 Registry Secret。

## 4. 仍存在的问题和风险

这些项目不能仅由本仓库在当前机器上证明：

- 学校部署机上的真实 basic/环境、Judge/Kernel 和应用基础镜像构建、推送、回拉及 digest 记录。
- MySQL/Redis/Storage 备份恢复演练、TLS/HSTS/证书续期、Docker daemon 专用主机与 socket 隔离。
- 目标并发、磁盘/内存/队列容量压测，监控告警、值班和回滚演练。
- Python SCA 中已有明确责任人和 2026-09-30 到期日的临时接受项，届期前必须升级或重新评审。
- 工作区未跟踪的手动测试指南和 `logs/` 属于用户文件，本轮没有覆盖或删除；正式发布应以仓库内 README/docs 为准，并在学校机按真实账号重新走一遍手册。

## 5. 按 P0 / P1 / P2 分类的问题清单

| 级别 | 当前问题 | 处理边界 |
| --- | --- | --- |
| P0-3（外部） | 真实镜像/Registry、备份恢复、TLS/HSTS/证书、Docker 主机隔离、容量压测、监控告警和回滚尚未在学校机验证。 | 只能由部署方完成；仓库不伪造通过。 |
| P1 | 生产运行仍依赖 Docker socket；单 Worker 混合判题与 AI 队列；大班级和大批量成绩的容量需按目标规模实测。 | 发布前做主机隔离、队列/资源压测和运行监控。 |
| P1 | SCA 临时接受项有到期日；基础镜像 digest 必须由部署方填入批准的 `.env`。 | 责任人按到期日处理，禁止用占位 digest。 |
| P2 | legacy Jupyter 兼容路径、章节排序未实现、旧 UI 桥接层和少量死接口仍存在。 | 不影响当前核心链路，但应在后续版本删除或明确下线。 |
| P2 | 测试 warning、覆盖率门槛、公开 OpenAPI/拆包优化仍可收敛。 | 不阻断受控试点，纳入维护计划。 |

## 6. 上线前还需要完成的事项

1. 在学校部署机复制 `.env.example`，填入真实密钥、数据库密码、可信代理 CIDR、基础镜像 digest 和 Judge 工作目录。
2. 按 README 的 staging 流程运行 `build-basic-environment-ci.sh` 发布模式，完成 basic 镜像 smoke、归档或 Registry push/pull，并把真实 digest 写入生产配置。
3. 用批准的 Compose 配置执行空库 bootstrap；保存迁移日志、镜像 digest 和健康检查结果。
4. 完成数据库、Redis、Storage 的备份恢复和隔离恢复演练，记录 RPO/RTO、责任人和回滚步骤。
5. 完成学校网关 TLS/HSTS/证书续期、Docker daemon/socket 隔离、目标并发压测、监控告警和回滚演练。
6. 在目标 MySQL 上运行后端全量测试、成绩排序/导出回归、代理链/限流回归，并保存 CI 与部署机证据。
7. SCA 接受项在 2026-09-30 前升级或重新签署风险接受；AI 在治理审批完成前保持关闭。

## 7. 最终结论：CONDITIONAL GO

仓库内可由代码、配置、测试和文档解决的问题已完成收口，项目可以进入学校机器的正式
部署验收。当前结论为：

**CONDITIONAL GO — 等待学校部署机完成 P0-3 外部门禁。**

**如果现在直接上线，这个项目最大的风险是什么？**

真实生产主机上的镜像、备份恢复和容量/隔离条件尚未验证，故障时可能同时出现服务无法启动与业务数据无法可靠恢复。
