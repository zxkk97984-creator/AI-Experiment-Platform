# 2026-10-09 逐项修复与验证记录

本轮基于当前源码完成审查计划中的 16 项工作；前 15 项各自提交代码及回归，第 16 项同步 README、架构与本记录。分支 `Xiaoxiao/new-features`，仅本地提交；未推送、未部署、未迁移业务库。原有未跟踪 `tasks/experiment-upgrade-plan.md` 保留且未提交。

## 修复对应关系

下表命令中的后端测试文件位于 `backend/tests/automated/`，前端单元测试位于 `frontend/src/`，浏览器测试位于 `frontend/e2e/`。完整运行方式见下一节。

| 项目 | 提交 | 修复与回归依据 | 逐项验证结果 |
| --- | --- | --- | --- |
| 1 教学只读边界 | `8093e44` | `test_teaching_readonly.py`、`test_teaching_readonly_mysql.py`；DELETE/PATCH 与学生写入同事务重查，所有 started 考试阻止关闭 | 新增 SQLite 35 项覆盖；相关 SQLite 73 + 25 通过；MySQL 6 通过，含两种并发先后顺序 |
| 2 作业受众 | `3b74f71` | `test_assignment_student_access.py`、`test_assignment_student_access_mysql.py`；详情、题目、自测、提交共用受众守卫 | SQLite 30 + 2 通过；MySQL 4 通过，拒绝时不调用运行器 |
| 3 切题草稿 | `e685480` | `AssignmentDetailDrafts.spec.js`、`assignment-drafts.spec.js`；按题保留页面内草稿与离开提示 | 相关单元 21、浏览器 2 通过；当时完整前端 1053 通过 |
| 4 作业迟到响应 | `7918ae4` | `AssignmentDetailRequests.spec.js`、`assignment-response-isolation.spec.js`；绑定题目与请求代际 | 单元 42、浏览器 7 通过（包含第 3 项），可控倒序响应与卸载验证 |
| 5 成绩簿历史上下文 | `a0a2078` | `GradebookView` 单元回归、`gradebook-response-isolation.spec.js`、`school-gradebook-flow.spec.js` | 单元 20、浏览器 11 通过；实际下载 CSV 内容与文件名对应当前班级 |
| 6 繁忙队列恢复 | `e91cdd4` | `test_judge_worker_loop_recovery.py` 及角色、租约、队列可靠性回归；循环顶部恢复，网络超时与退避 | 恢复/角色/租约 26、队列可靠性 17 通过 |
| 7 FIFO 与公平消费 | `908ce09` | `test_judge_worker_fifo.py`；保留 RPUSH，改 BLPOP 并轮转，统一作业队列配置 | 组合 99 项中 98 通过、1 个深 JSON 测试深度不足；修正测试后 FIFO/恢复 42 项通过；最终全量重新覆盖 |
| 8 扫描失败阻断 | `ead113c` | `scripts/check_secrets.py` 及 `test_secret_scan.py`；根目录、受控 tracked source 范围、执行失败非零退出，不打印匹配内容 | 合成仓库 8 项通过；当前 tracked source 扫描干净 |
| 9 导入代理限制 | `e085f1e` | `test_import_proxy_limits.py`；XLSX 请求 6 MiB / 文件 5 MiB，CSV 请求 3 MiB / 文件 2 MiB | 真实 Nginx 与账号/名单共 21 项、前端 1 项通过 |
| 10 结果轮询 | `e3eda8c` | `SubmissionView` 单元回归、`submission-polling.spec.js`；串行请求、403/404 停止、退避/重试、隐藏暂停 | 单元 6、浏览器 2 通过；正常间隔 1.5 秒，连续五次临时失败停止，自动重试间隔 1.5/3/6/12 秒（算法上限 24 秒） |
| 11 实验队列扫描 | `d308456` | `test_lab_queue_scan.py` 及队列/基准回归；100 条批次、固定上界、跨轮游标 | 24 项通过；包含第 101 个任务、持续新增、资源恢复、并发领取 |
| 12 成绩簿批量读取 | `7e399e0` | `test_gradebook_batch_queries.py`、`test_gradebook.py`、`test_gradebook_mysql_concurrency.py` | SQLite 16、MySQL 11 通过；30/100/200 人均 13 次 SELECT，原路径 220/710/1410 次，分数/证据/预览摘要完全一致 |
| 13 真实 Docker 门禁 | `e3aee08` | CI `lab-runtime`、`check_runtime_junit.py`、`test_runtime_ci_gate.py`；同时修复服务队列 Redis 测试隔离 | 门禁/队列 23、API 隔离 8、真实 Judge/Lab 10 项通过，真实组零跳过 |
| 14 作业个人进度 | `ee71682` | `test_assignment_progress.py`；`GET /assignments/{id}/my-progress` 与页面适配 | 后端 12、前端 43、浏览器 9 通过；105 条其他作业提交不影响旧进度，最近失败仍保留历史成功标记 |
| 15 旧提交列表授课范围 | `36c613a` | `test_legacy_submission_scope.py`、课程作业/授课实例回归；列表和计数复用 `teacher_task_condition` | 11 项通过；授课教师可见、仅内容负责人不可见，分页/详情/统一列表一致 |
| 16 当前文档与验证边界 | 本文件所在文档提交 | README、架构设计总览、本记录 | 见下方最终检查；历史报告未改写 |

逐项计数有交集，不能相加当成独立用例总数。测试、运行日志、JUnit、截图、构建产物和本机运行配置均未提交。

## 最终检查与实际命令

执行位置为仓库根目录。Python 使用本机已安装的隔离虚拟环境，前端使用现有锁文件依赖；未更新依赖。SQLite 测试每项创建临时库；Redis 单独启动在 `127.0.0.1:16319/15`。MySQL 8.4 单独启动在 `127.0.0.1:13319`，使用测试保留的空 schema，测试结束销毁其表。

```bash
PY=.runtime/school-security-venv/bin/python
# 全量 SQLite：按文件分配到八个隔离测试进程，不启用真实服务用例
# 首次串行运行主动中止后改为并行。首轮并行在新文档尚未写出时
# 产生两处断链（1953 通过、2 失败、56 跳过），链接复验 66 项通过后完整重跑。
env -u DAI_DATABASE_URL DAI_REDIS_URL=redis://127.0.0.1:16319/15 \
  "$PY" -m pytest -q -n 8 --dist loadfile backend/tests/automated \
  --junitxml=.runtime/review-fixes-20261009/full-sqlite-final.xml

npm --prefix frontend run check
.runtime/school-security-venv/bin/ruff check backend/app/
python3 scripts/check_secrets.py
git diff --check
```

MySQL 专项使用本轮一次性凭据设置以下变量，未指向业务库：

- `DAI_TEST_READONLY_MYSQL_URL`：`dai_school_readonly_verify`（只读与受众并发）。
- `DAI_TEST_SCOPE_MYSQL_URL`：`dai_school_scope_verify`（授课范围并发）。
- `DAI_TEST_GRADEBOOK_MYSQL_URL`：`dai_school_gradebook_verify`（成绩发布并发）。

```bash
env -u DAI_DATABASE_URL DAI_REDIS_URL=redis://127.0.0.1:16319/15 \
  "$PY" -m pytest -q \
  backend/tests/automated/test_teaching_readonly_mysql.py \
  backend/tests/automated/test_assignment_student_access_mysql.py \
  backend/tests/automated/test_school_write_scope_mysql.py \
  backend/tests/automated/test_gradebook_mysql_concurrency.py \
  --junitxml=.runtime/review-fixes-20261009/final-mysql.xml

# 使用相同 DAI_TEST_SCOPE_MYSQL_URL，补验共享内容发布的当前读
# 本文件内另有 6 个 SQLite 用例，合计 12 通过。
env -u DAI_DATABASE_URL DAI_REDIS_URL=redis://127.0.0.1:16319/15 \
  "$PY" -m pytest -q backend/tests/automated/test_shared_release_current_read.py \
  --junitxml=.runtime/review-fixes-20261009/final-release-mysql.xml
```

真实运行组基于当前 `backend/docker/judge`、`backend/docker/kernel` Dockerfile 构建。首次默认代理构建失败；主机网络通过代理下载过慢；最终通过主机网络、清空本次 build 的 HTTP/HTTPS/ALL_PROXY 参数完成构建。本机使用 Dockerfile 默认 `python:3.12-slim` 基础镜像；新 CI 任务使用工作流中固定 digest，两者不冒充同一次构建证据。

```bash
# 本机成功构建命令（同样清空小写 http_proxy/https_proxy/all_proxy）
docker build --network=host --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= \
  --build-arg ALL_PROXY= --build-arg http_proxy= --build-arg https_proxy= --build-arg all_proxy= \
  -t dai-judge-python:review-20261009 backend/docker/judge
docker build --network=host --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= \
  --build-arg ALL_PROXY= --build-arg http_proxy= --build-arg https_proxy= --build-arg all_proxy= \
  -t dai-kernel-python:review-20261009 backend/docker/kernel

env -u DAI_DATABASE_URL DAI_REDIS_URL=redis://127.0.0.1:16319/15 \
  DAI_RUN_DOCKER_SMOKE=1 DAI_JUDGE_IMAGE=dai-judge-python:review-20261009 \
  DAI_LAB_TEST_IMAGE=dai-kernel-python:review-20261009 DAI_TEST_NGINX_IMAGE=nginx:alpine \
  "$PY" -m pytest -q backend/tests/automated/test_judge_docker_smoke.py \
  backend/tests/automated/test_lab_runtime.py backend/tests/automated/test_import_proxy_limits.py \
  -k 'real and not pytorch' --junitxml=.runtime/review-fixes-20261009/final-runtime.xml
python3 scripts/check_runtime_junit.py .runtime/review-fixes-20261009/final-runtime.xml

# Vite 本轮端口 15179；以下测试均拦截 API，不读写业务服务
E2E_BASE_URL=http://127.0.0.1:15179 npm --prefix frontend run e2e -- \
  assignment-drafts.spec.js assignment-response-isolation.spec.js \
  gradebook-response-isolation.spec.js school-gradebook-flow.spec.js submission-polling.spec.js \
  --workers=1 --output=/tmp/dai-final-browser
```

| 最终门禁 | 实际结果 | 本机日志（未入 Git） |
| --- | --- | --- |
| 后端 SQLite 全量 | 1957 通过、56 条件跳过、零失败，399.11 秒 | `.runtime/review-fixes-20261009/full-sqlite-final.log` |
| 前端 lint + 全量 Vitest + build | 122 文件 / 1096 测试通过，构建通过 | `.runtime/review-fixes-20261009/full-frontend.log` |
| MySQL 专项 | 原组 30 通过；共享发布组 12 通过（其中 6 MySQL / 6 SQLite），均零跳过 | `.runtime/review-fixes-20261009/final-mysql.log`、`final-release-mysql.log` |
| 真实 Judge / Lab / Nginx | 11 通过，零跳过，19 个非目标用例 deselected | `.runtime/review-fixes-20261009/final-runtime.log` |
| 浏览器受控 API 回归 | 22 通过，1440 / 390 宽度，含迟到响应、CSV 与卸载 | `.runtime/review-fixes-20261009/final-browser.log` |
| Ruff / secret scan / diff | 通过 | `.runtime/review-fixes-20261009/final-ruff.log`；命令输出 |

SQLite 全量的 56 个条件跳过单独统计，不计入通过：36 个教学相关 MySQL、11 个 Judge/Lab/Nginx 用例已单独补验；其余 9 个为 4 个 PyTorch/教学模板、4 个独立账号/迁移 MySQL、1 个持久 Kernel 安全冒烟。实验 Cell 的真实运行证据来自 Lab CPU 组。

## 已确认修复与未验证范围

- 上表代码问题均有源码修改和对应回归；第 12 项性能结论来自合成 30/100/200 人班级的查询计数，不能推断学校容量或端到端延迟。
- 真实 Docker 覆盖基础 CPU 执行、Cell 观察、输入保护、取消、超时和容器清理；未验证 PyTorch、GPU、保留基准节点、学校并发容量及目标机资源/驱动配置。
- Nginx 使用真实容器和本机 HTTP stub 验证请求上限与 multipart 余量；后端名单解析另有 API 测试。本轮不是学校真实 XLSX 数据经生产域名导入的端到端验收。
- 浏览器采用受控 API 数据，验证实际 Vue 页面在桌面与窄屏的布局、交互、倒序响应和导出。未连接真实教师/学生业务账号，未调用外部 AI 服务。
- 新 GitHub Actions 工作流已解析并在本机运行相同核心测试，但没有推送或远程 CI 执行结果。Registry 推送/回拉、学校 TLS/代理、生产恢复和真实部署不在本轮范围。
- 测试隔离修正：早期部分既有 API 测试的服务级 Redis 工厂可能绕过 FastAPI FakeRedis，连接共享本机 6379。没有事前快照，无法归属其中的历史消息，因此没有删除共享消息。之后全部命令显式使用独立 16319；第 13 项修复公共夹具，同时替换队列服务工厂并增加回归。最终全量基于修复后的隔离夹具。不能宣称早期所有扩展测试均未触及共享 Redis。
- 后端依赖仍产生现有弃用/测试短 JWT 密钥警告，警告不等同测试失败；没有借本轮修复升级依赖或改写历史报告。

升级注意：先协调停止旧 Judge / 已启用的 AI 消费者，再启动全部新版本消费者，保留队列内容。关闭学期/归档班级前必须先完成所有 started 考试收卷；不要绕过接口直接改库规避守卫。

收尾仅移除本轮创建的 `dai-repair-20261009`、`dai-repair-redis-20261009` 容器及其匿名卷，并停止本轮 Vite（15179）；没有清理其他项目容器、共享 Redis 或业务数据。
