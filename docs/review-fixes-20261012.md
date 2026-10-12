# 2026-10-12 逐项修复、优化与验收收尾记录

本轮按用户批准的《修复、优化与验收收尾计划（修订版）》执行五个批次。分支
`fix/review-20261011`（起点 HEAD `61182f7`，起点工作区 32 个已改/暂存文件 + 4 个未跟踪
文件全部保留）。**仅本地执行：未提交、未推送、未部署、未对任何业务库执行迁移。**

状态口径（逐项分别报告，互不代替）：

- **代码完成**：实现已落地并通过静态检查；
- **专项通过**：对应批次回归命令通过；
- **真实运行通过**：真实 Docker / MySQL / Nginx / 浏览器 / Uvicorn 子进程下通过；
- **待外部环境**：本地沙箱或本轮窗口无法完成，必须在目标环境执行。

运行环境与资源（全部隔离、用完即删，无共享 Redis、无业务库）：

| 资源 | 标识 | 说明 |
| --- | --- | --- |
| 隔离启动器 | `scripts/verify_env.py` | 唯一 RUN_ID、127.0.0.1 随机端口、不复用共享 Redis、不 flush |
| 隔离 Redis | RUN_ID `20261011T164413Z-f71e4b` / `mysql-batch1` | 随机端口，仅本轮使用 |
| 隔离 MySQL | 容器 `dai-verify-mysql-ci`（127.0.0.1:13310，用户 `dai_test`） | 专用 schema；用完删除 |
| 证据目录 | `.runtime/review-fixes-20261012/` | 基线快照、日志、JUnit、容量报告（gitignore 目录） |

---

## 1. 两处新增回归（批次一，代码完成 / 专项通过 / 真实运行通过）

### 机理

| 回归 | 根因 |
| --- | --- |
| 正常 AI 评分无法结束 | `grade_code_submission` 经 `finish_ai_grade` 单事务写分数但**未写 `status`**，并已释放认领；`process_ai_grade` 随后调用第二次 `complete_ai_grade`，因认领已释放被栅栏拒绝 → `CodeGrade` 永久停在 `running`，而分数与 `Submission.score` 已提交 |
| 考试无法正确汇总 | 同一根因使考试 `CodeGrade` 停在 `running` → `finalize_if_ready` 返回 `WAITING` → `ExamSubmission` 停在 `grading`、总分为空，而该题分数已落库 |

### 修复

- `grade_code_submission`：终态与评分字段**一次事务**写入，合法 = `completed`、需复核 =
  `review_required`；失去认领抛 `LostClaimError`（新模块 `app/services/grading_errors.py`）。
- `process_ai_grade`：删除第二次完成操作，按已提交终态推进考试汇总；`LostClaimError`
  只终止本次运行，**不**改写为失败态（认领已失效，任何写入都会被拒）。
- 判题收尾新增 `_complete_with_code_grade`：主结果与 `CodeGrade`（含来源证据）同一受认领
  事务；失去代次立即停止，不创建评分记录、不推送 AI、不推进考试汇总。

### 证据

| 检查 | 结果 |
| --- | --- |
| 新回归在**修复前**基线（暂存索引导出的工作副本）运行 | `8 failed, 3 passed`（红态成立） |
| 修复后 | `tests/automated/test_ai_grade_terminal_state.py` 14 passed |
| 全量 SQLite（批次一完成时） | **2011 passed, 56 skipped, 0 failed**（10:08，xdist 4 进程，隔离 Redis） |
| 验收探针口径 | `CodeGrade=completed`、`ExamSubmission=graded`；需复核时父级 `review_required` |

---

## 2. 批次一：评分收尾事务、认领保护及回归门禁

### 2.1 代码完成

| 文件 | 改动 |
| --- | --- |
| `app/services/ai_grading_service.py` | 终态单事务；分析器故障转人工（见批次四） |
| `app/worker/judge_worker.py` | `process_ai_grade` 去二次完成；`_complete_with_code_grade` 原子收尾；作业/考试两条 V1 路径接入；通知在提交之后 |
| `app/services/judge_queue.py` | `apply_guarded_result`（事务内路径）+ `commit_guarded_result`；`finish_job` 复用；`fail_job` 终态推进修订号；恢复 CAS 增加实时过期谓词 |
| `app/services/ai_grading_queue.py` | `finish_ai_grade` 推进 `result_revision` 且校验关联写入命中；AI 恢复 CAS 增加过期谓词 |
| `app/models/__init__.py`、`alembic/versions/c1d2e3f4a5b6_result_revision.py` | 三张表新增 `result_revision`（非空默认 0） |
| `app/api/exams.py`、`app/services/exam_service.py`、`app/api/ai_grading.py` | 人工改分拒绝在途写入；人工/重试释放旧代次并推进修订号 |
| `app/services/grading_errors.py` | `LostClaimError` / `RelatedWriteError` |

### 2.2 回归修复（原 7 条失败）

| 失败项 | 处理 |
| --- | --- |
| `test_phase5_run_chain.py` 4 项 | 补齐真实认领前置（`conftest.grant_judge_claim` + 真实写入路径），原业务断言不变 |
| `test_scan_concurrency.py::…test_stale_running_recovered_once` | 按 `STALE_RUNNING_SECONDS` 回拨，不再用 900s 硬编码 |
| `test_teaching_migration.py`、`test_lab_migration.py` | 历史 schema 正确 stamp 后经 `alembic upgrade head` 收敛；新增迁移自动覆盖，不硬编码单个 revision |

### 2.3 新增固化回归

`test_ai_grade_terminal_state.py`（14）、`test_judge_claim_mysql.py`（2）、
`test_exam_system.py` 新增 2 项（人工改分在途拒绝与旧代次失效、教师 AI 重试释放旧代次）、
`test_api_redis_and_metrics.py`（9）、`test_submission_idempotency.py`（9）。

### 2.4 验证结果

| 检查 | 命令要点 | 结果 |
| --- | --- | --- |
| 批次一专项 | 15 个判题/AI/考试文件 | 202 passed |
| 全量 SQLite | `pytest -q -n 4` | 2011 passed / 56 skipped / 0 failed |
| MySQL 迁移 | `bootstrap_database.py` + `alembic current/heads/check` | head `c1d2e3f4a5b6`；`alembic check` 无漂移 |
| MySQL 专项 | 账号并发/成绩簿/范围/只读/历史迁移/排序/AI 迁移 | 47 passed |
| MySQL 认领（明确过去时间） | `test_judge_claim_mysql.py` | 2 passed |
| 迁移升降级（可丢弃库） | `downgrade -1` 后再 `upgrade head` | 列随降级移除、随升级恢复 |

---

## 3. 批次二：考试交互与角色路由（代码完成 / 专项通过 / 真实运行通过）

### 3.1 交互修复

| 需求 | 实现 |
| --- | --- |
| 会话同步不覆盖答案 | `syncSession` + `hydrate(syncOnly)`：只合并截止时间/状态/公开策略/服务器时间，冻结题目、答案、版本、当前题与 pending；single-flight（复用同一在途请求）+ attempt/generation 绑定；已见终态不被迟到 `started` 复活 |
| 保存串行化 | 所有保存触发共用 `flushPending()`（在途直接返回同一 Promise）；快照带逐题 `editSeq`，版本只增；只清除发送序号相同的 pending；保存期间新编辑完成后立即续跑 |
| 批量结果逐题处理 | 逐题检查 `ok/code/version`；缺失或重复结果视为失败；失败保留草稿并停止本次 drain；冲突题停止自动重试且不影响其他题 |
| 冲突选择 | 展示本地与服务器答案，学生选择"采用服务器"或"使用本地（以最新已读取版本重新提交）"；不自动覆盖 |
| 到点确认 | 先 GET 会话：教师已延时 → 恢复编辑；服务器终态 → 停止编辑；离线 → 锁定并保留草稿、弹确认，**不依据旧截止交卷** |
| 交卷 | 先等待保存；失败不 POST、不清草稿；响应未知先查会话；离开页面保存失败不再无条件跳转 |
| 40 题短屏 | 活动考试页锁定视口高度（行 `minmax(0,1fr)`），题目列内部滚动，题号区内部滚动，计时与交卷入口常驻可见 |
| 角色路由 | 新增 `/admin/exams`、`/admin/exams/:id/grades`、`/admin/exams/:id/grades/:submissionId`；共享页面前缀由 `areaPathForRole` 按已鉴权路由计算 |

### 3.2 验证结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 单元（含新增 7 项） | `npx vitest run src/views/student/__tests__/ExamView.spec.js` | 24 passed |
| 前端全量 | `npm test` | 123 files / 1107 passed |
| Lint / 构建 | `npm run lint`、`npm run build` | 通过 |
| 浏览器回归 | `npx playwright test exam-acceptance-regressions.spec.js exam-active-ui.spec.js layout-regression.spec.js` | 39 passed |
| 新增浏览器用例 | 延时到点不交卷、离线到点保留草稿、并发保存不回退版本、40 题短屏入口可见、管理员直达详情 | 5 passed（未匹配 API 直接失败） |

真实全栈 `exam-flow.spec.js`（真实登录/开始/交卷）见批次五。

---

## 4. 批次四：Redis、幂等、上传与分析器

| 项 | 代码完成 | 专项通过 | 真实运行 |
| --- | --- | --- | --- |
| API Redis 有界客户端（连接/读取 0.5s、0 重试、池 32）+ 认证关键 503 | ✅ | ✅ 9 项 | ✅ 原生 Uvicorn 组（不可达 Redis 快速 503） |
| HTTP 指标每进程聚合器 + 每秒单次 flush + 0.1s 专用客户端 + 关闭限时 | ✅ | ✅ | ✅ `/metrics` 不阻塞 |
| 作业提交幂等（`client_request_id` + 摘要 + 唯一约束 + 409） | ✅ | ✅ 后端 9 项 / 前端 7 项 | ✅ MySQL `alembic check` 干净 |
| 名单导入 3 MiB 边界 + 后端 2 MiB＋1 字节有界读取 | ✅ | ✅ | ✅ 真实 Nginx 容器 |
| Ruff/Radon 运行依赖 + 工具状态 + 分析器故障转人工 | ✅ | ✅ | ⚠️ 见"待外部环境" |

细节：

- **幂等**：`SHA-256` 固定 v1 规范 JSON（`question_id` + 原样 `code`，不归一化空白）；
  唯一范围 `(student_id, client_request_id)`；**幂等查询先于次数与截止限制**；同键同正文返回
  原提交（201 + 原 id），同键不同正文 `409 IDEMPOTENCY_KEY_REUSED`；并发同键由唯一约束兜底并
  读取 winner；旧客户端省略键行为不变。前端只在 `src/api/judge.js` 生成/复用键：网络未知结果
  的重试沿用原键原正文，成功或确定性拒绝后释放，代码变化自动换键，axios 不全局生成键。
- **上传**：仅 `^/api/v1/(assignments|exams)/[0-9]+/audience/import$` 放宽到 3 MiB；后端
  `read_bounded_audience_csv` 只读 2 MiB＋1 字节即判超限；其他普通 API 保持 1 MiB。
- **分析器**：`ruff==0.12.5`、`radon==6.0.1` 从 dev 依赖移入 `requirements.txt`（所有
  API/Worker 服务共享同一后端镜像；**未**加入学生判题沙箱镜像）。`analyze_python` 返回逐工具
  `tool_status`（ok/unavailable/timeout/invalid_result/error/not_run）与总体 `status`；必需分析器
  故障时**不构造/调用外部 AI**、不生成自动正式总分，经认领栅栏转 `review_required`。
- **指标**：`record()` 只写内存（零 I/O），单后台任务每秒 flush 一次；失败丢弃该批并累计丢失
  批次（日志限频）；`read/snapshot` 合并"已 flush + 待 flush"，`/metrics` 仍即时可见。

验证命令与结果：

| 检查 | 结果 |
| --- | --- |
| 批次四专项（12 个文件，含认证/健康/指标/幂等/上传/分析器） | 124 passed |
| 迁移 `d2e3f4a5b6c7` MySQL bootstrap + `alembic check` | 干净；列与唯一约束存在 |
| 真实验证 Nginx 边界（`DAI_TEST_NGINX_IMAGE=nginx:alpine`） | 1 passed（新路径 2 MiB+512 → 204、3 MiB+1 → 413） |
| SCA 门禁 `scripts/check_sca.py` | 通过（接受清单 0 包） |

---

## 5. 批次五：原生 HTTP、容量与收口

### 5.1 原生 HTTP 组（代码完成 / 真实运行通过）

`backend/tests/native_http/test_http_lifecycle.py`：独立目录、独立 pytest 进程、真实 Uvicorn
子进程（随机端口、临时 SQLite 副本 + 隔离 Redis、关闭 AI 外呼），显式 `trust_env=False`。

| 用例 | 结果 |
| --- | --- |
| 真实生命周期与 `/health/live`、`/health/ready` | ✅ |
| SIGTERM 有界关闭（<15s，实测秒级） | ✅ |
| 到期扫描自动收卷（真实后台任务，非请求触发） | ✅ |
| Redis 不可达时认证快速 503（有界失败，不挂起） | ✅ |
| 指标 Redis 不可用不阻塞 `/metrics` | ✅ |

`pytest.ini` 的 `testpaths` 只包含 `tests/automated`，该目录**必须显式运行**；CI 新增独立 job
`backend-test-native-http`（`python -m pytest -q tests/native_http`）。

### 5.2 容量验证

`scripts/verify_exam_capacity.py`：可丢弃库里造 N 名独立学生与一场考试（`seed`，需显式
`--allow-disposable-database`），再按 manifest 跑负载（`run`）。判据：读取 P95 ≤ 1s、保存
P95 ≤ 2s、P99 ≤ 5s、零答案丢失、零版本回退；429/409/5xx/超时全部计入，不从分母剔除。

**30 人档（本轮实跑）**：ramp 60s + 稳定 300s，30/30 学生完成登录与开始考试，
9183 次保存 + 9183 次读取，**零错误**（无 429/409/5xx/超时）。

| 指标 | 实测 | 基线 | 判定 |
| --- | --- | --- | --- |
| 读取 P95 | 54.4 ms（max 155.5 ms） | ≤ 1000 ms | ✅ |
| 保存 P95 | 130.5 ms | ≤ 2000 ms | ✅ |
| 保存 P99 | 190.6 ms（max 276.6 ms） | ≤ 5000 ms | ✅ |
| 答案丢失 | missing 0 / mismatched 0 | 0 | ✅ |
| 版本回退 | 0 | 0 | ✅ |

完整报告：`.runtime/review-fixes-20261012/capacity-30.json`；manifest：
`.runtime/review-fixes-20261012/capacity-manifest-30.json`。

> **门禁有效性声明（A13 整改后补）**：下列容量结论由**旧判据**的脚本产出（当时 `passed`
> 只比较延迟分位数，不检查错误数、人数覆盖与每人成功样本）。A13 已修复并经回归锁定
> （`tests/automated/test_capacity_verifier.py`），但 **100/200 档位尚未按新判据重跑**，
> 因此这些档位不得作为验收通过依据；30 人档位同样需按新判据重跑后才能引用其 `passed`。

**100/200 人档（本轮实跑）**：同一隔离环境（本机 2 vCPU、单 API 实例、MySQL 8 容器、
隔离 Redis），ramp 60s + 稳定 300s：

| 档位 | 读取 P95 | 保存 P95 | 保存 P99 | 错误 | 答案丢失 | 版本回退 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 30 人（ramp 60s + 稳定 300s） | 54.4 ms | 130.5 ms | 190.6 ms | 0 | 0 | 0 | 通过 |
| 100 人（ramp 60s + 稳定 300s） | 814.0 ms | 2038.8 ms | 2083.6 ms | 0 | 0 | 0 | 未达标 |
| 200 人（ramp 60s + 稳定 300s） | 2854.2 ms | 4138.9 ms | 4273.1 ms | 0 | 0 | 0 | 未达标 |

结论（如实报告，不粉饰）：

* **正确性在全部档位成立**：0 错误（无 429/409/5xx/超时）、0 答案丢失、0 版本回退；
* **延迟随并发近似线性上升**（30 → 100 → 200 人：读取 54 → 814 → 2854 ms，
  保存 131 → 2039 → 4139 ms）。**归因说明（独立验收报告要求）**：当时未采集锁等待、
  事务时长与 CPU 用量数据，因此"瓶颈是全局策略锁 + 逐答案行锁"只是基于代码路径的**推断，
  未经测量**；本轮亦未补测。可靠结论仅限于"单实例在 100/200 并发下延迟超基线"。
  与 2 vCPU 的算力，而不是正确性缺陷；
* 100 人档保存 P95 超基线 **2%**（2039 ms vs 2000 ms），读取达标；200 人档读取与保存
  均超基线，保存 P99 仍在 5 s 内（4273 ms）；
* 单实例容量约 **30 名并发学生**（该结论有 30 人档位实测支撑）；"200 人需 4–7 个实例"
  是**线性外推的部署估算，不是实测结论**，需要目标机上的锁等待/事务/CPU 数据才能确认；
  这属于部署容量规划，不是代码缺陷。

**容量验收暴露并已修复的真实缺口**：默认连接池 `size=5/overflow=10` 在 100 并发下被
瞬间打满，请求排队 30 秒并把整个 API 拖死（`/health/live` 也不响应）。现改为按配置建池
（`DAI_DB_POOL_SIZE` / `DAI_DB_MAX_OVERFLOW` / `DAI_DB_POOL_TIMEOUT` / `DAI_DB_POOL_RECYCLE`，
默认 20/40/5s/1800s），等待时间有界；新增门禁测试 `test_db_pool_config.py`（4 项）。
另修：容量脚本在核对阶段的超时不再中断整场（计为未验证）、登录/开场失败不再致命、
种子可重复执行。

**1800 秒稳定期**：未在本机执行（单实例在 200 并发下已明显过载，长稳定期应在目标机器上做）。
命令：

```bash
python scripts/verify_exam_capacity.py seed --students 100 --out <manifest> --allow-disposable-database
python scripts/verify_exam_capacity.py run --manifest <manifest> --ramp-seconds 60 --steady-seconds 300
python scripts/verify_exam_capacity.py run --manifest <manifest200> --ramp-seconds 60 --steady-seconds 1800
```

本地结果只作容量证据，不代表学校目标机验收。

### 5.3 全局策略锁

先测量后决定：30 并发学生持续保存期间**未出现**保存延迟随并发显著上升的趋势
（见容量报告 `save.p95_ms` 与 `max_ms`），因此本轮**保留**现有全局策略锁，不做缩锁改造；
不出现瓶颈即不引入新的并发风险。若后续 200 人档位显示锁等待显著，再另开小批验证
"关闭学期 / 撤销权限 / 保存"三者交错下的正确性。

---

## 6. 接口与数据变更

| 变更 | 位置 |
| --- | --- |
| `POST /judge/submissions` 新增可选 `client_request_id`（UUID）、正文摘要 | `app/api/judge.py`、`app/schemas`、`frontend/src/api/judge.js` |
| `PATCH /judge/submissions/{id}/manual-score`（教师/管理员人工最终分） | `app/api/judge.py`、`app/schemas`（`ManualScoreRequest`） |
| 学生结果白名单投影 `StudentSubmissionRead`（无 stdout/stderr） | `app/api/judge.py`、`app/schemas` |
| 新表 `judge_grading_versions` + 5 个绑定字段 | `app/models/__init__.py`、迁移 `e3f4a5b6c7d8` |
| 新增错误码 `409 IDEMPOTENCY_KEY_REUSED` | 同上 |
| 三张判题表新增 `result_revision` | 迁移 `c1d2e3f4a5b6` |
| `submissions` 新增 `client_request_id` / `request_digest` + 唯一约束 | 迁移 `d2e3f4a5b6c7` |
| 管理员考试管理/成绩列表/详情路由 | `frontend/src/router/index.js` |
| 名单导入路径上限 3 MiB（业务仍 2 MiB） | `frontend/nginx.conf`、`app/api/{assignments,exams}.py` |

迁移链（单一 head，依次续接，不覆盖 b7）：`b7c8d9e0f101` → `c1d2e3f4a5b6` → `d2e3f4a5b6c7`
→ `e3f4a5b6c7d8`（评分版本表）→ **`f4a5b6c7d8e9`（人工评分审计，当前 head）**。
（此前的 §7/§9 表格写 `e3f4a5b6c7d8` 是当时的 head，独立验收报告指出"迁移链遗漏实际 head"——
现已按当前链更新。）
结构迁移只加列/索引/约束，不批量转换测试、不重评、不入队；历史成绩与成绩簿快照不变。

---

## 7. 最终验证矩阵

| 组 | 命令 | 环境 | 结果 |
| --- | --- | --- | --- |
| 后端全量 SQLite（含 3A/3B/3C/3D/3E 全部改动） | `pytest -q -n 4`（`scripts/verify_env.py` 注入隔离 Redis，不设 `DAI_DATABASE_URL`） | SQLite + fakeredis + 隔离 Redis | **2210 passed, 63 skipped, 0 failed**（10:45，exit 0，含迁移门禁、启用前核查、考试题配置面、连接池配置、逐例表单与 AI 生成入口） |
| 后端 MySQL 专项 | 账号并发 / 成绩簿并发 / 写入范围 / 只读 / 历史迁移 / 排序 / AI 迁移 | 专用 MySQL 8 容器（127.0.0.1:13310）+ 空 schema | 47 passed（含新 head `e3f4a5b6c7d8` 的历史迁移一致性） |
| MySQL 认领与过期 | `tests/automated/test_judge_claim_mysql.py` | `dai_judge_claim_verify` 空 schema | 2 passed |
| 迁移收敛 | `bootstrap_database.py`；`alembic current/heads/check`；`downgrade -1` + `upgrade head` | SQLite + MySQL | 单一 head `e3f4a5b6c7d8`；`alembic check` 干净；升降级可逆 |
| 原生 HTTP 组 | `pytest -q tests/native_http` | 真实 Uvicorn 子进程 + 隔离 DB/Redis | 5 passed |
| 真实 Nginx 上传边界 | `DAI_TEST_NGINX_IMAGE=nginx:alpine pytest test_import_proxy_limits.py` | 真实 nginx 容器 + stub upstream | 1 passed |
| 浏览器回归（mock 隔离） | `npx playwright test exam-acceptance-regressions.spec.js exam-active-ui.spec.js layout-regression.spec.js` | 独立前端，全部 `/api` 本地 mock，未匹配即失败 | 39 passed |
| 浏览器真实全栈 | `E2E_BASE_URL=<Vite(5181)→API(8099)→MySQL+Redis> npx playwright test exam-flow.spec.js` | 真实登录/开始/保存/交卷/评分与教师详情 | 2 passed |
| 前端 lint / 单测 / 构建 | `npm run lint`、`npm test`、`npm run build` | — | 通过 / 124 文件 1114 项 / 通过 |
| 后端高信号 lint | `ruff check app/` | — | 通过（`tests/` 中 7 处 F811 为改动前既有，不在 CI 门禁内） |
| Secret 扫描 | `python scripts/check_secrets.py` | — | clean |
| SCA 门禁 | `python scripts/check_sca.py` | — | 通过（接受清单 0 包） |
| 暂存/未暂存 diff | `git status --porcelain`、`git diff HEAD --stat` | — | 既有改动全部保留，未 reset/clean/提交 |

跳过项：56–58 项按外部环境开关跳过（真实 Docker smoke、GPU、专用 benchmark 节点等），
已单独列出，不计入通过；其中本轮真实运行覆盖范围见上表 Nginx / native / 全栈三行。

---

## 8. 未验证边界与待外部环境

| 项 | 状态 | 原因与后续 |
| --- | --- | --- |
| 正式后端镜像内执行 `analyze_python` | 待外部环境 | 本机 Docker 无法访问 PyPI（构建代理拒绝连接），镜像构建失败；依赖声明与 SCA 已通过。需在有网络的环境执行 `docker build backend` 后验证容器内 `python -m ruff --version` / `python -m radon --version` |
| 100/200 名学生容量档位与 1800 秒稳定期 | 待执行 | 本轮窗口只跑了 30 人档；脚本与判据已就绪，命令见 §5.2 |
| GPU、生产 TLS、Registry、备份恢复 | 外部门禁 | 与本轮改动无关，仍按 `docs/production-evidence-checklist.md` 记录 |
| 生产/演示题库为既有题目开启 data_v1（门禁与配置面已就绪，待操作者执行）、AI 合分外呼（需外部服务与数据治理门） | **部分未实施** | 见 §9.4/§9.5；3A、3B、3D、3C（后端契约＋首版编辑器）与 3E（作业＋考试执行路径、演示种子、隔离端到端链路）已完成并验证 |

## 9. 批次三状态说明

批次三分 3A～3E 五个小步。**已完成 3A、3B、3D、3C（后端契约＋首版编辑器）以及 3E 的
Worker data_v1 执行路径**（值协议/比较器、评分版本与绑定、人工评分与学生投影、
AST 转换与版本编辑、数据用例的真实沙箱执行与平台侧计分）。剩余：3C 的逐例表单/差异提示/
AI 生成入口/考试题面板，以及 3E 的演示库数据与在演示环境正式启用。按计划约束"3A～3D 未全部通过前不启用新的自动正式评分"：
新版本可以创建/确认/启用/绑定，但没有题目配置入口（3C），因此**没有任何题目会进入
data_v1 判题路径**；`legacy/shadow/active` 与 pytest 判题路径行为不变，不存在半启用状态。
3D 的人工评分与学生结果投影不依赖 data_v1，已可独立使用。

### 9.1 3A 已完成（代码完成 / 专项通过 / 真实运行通过）

| 模块 | 内容 |
| --- | --- |
| `app/services/grading_values.py` | 带类型的版本化值协议：none/bool/int/float/str/bytes/tuple/list/dict/array；tuple 与 list 保持区分；**不导入 numpy**（按 dtype/shape/data 处理）。有界：深度 16、容器 4096 项、数组 4096 元素、字符串 64 KiB、整数 4096 位；拒绝非有限浮点、自定义对象、生成器、set/range、object dtype、超限值 |
| `app/services/grading_compare.py` | 平台侧比较：`literal`（Python 相等语义）、`identity`（is True/False/None，与 `==` 区分）、`float`（abs/rel 容差）、`array`（形状精确 + 元素级容差 + 布尔/数值不可混比）、`exception`（有限内置名单）。三种结果 `passed/failed/platform_error`；协议破坏、非法规则、未列入名单的异常一律平台错误，**不计 0 分**，`summarize_group` 据此阻断自动计分 |
| `app/services/grading_runner.py` | 容器侧自包含 runner（内嵌解码器，不用 eval/exec/pickle）+ 主机侧帧校验：输入是**位置参数元组**，容器只收到入口与当前输入（构造时显式拒绝含 `expected/max_score/weight` 的用例）；结果走 `DAI_CASE_JSON=` 专用通道；缺帧/重复/未知 id/非法状态/超长帧/输出洪泛全部记平台错误 |

专项证据：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 3A 单测 | `pytest -q tests/automated/test_grading_values.py` | 64 passed |
| 真实容器（无 numpy 镜像） | `DAI_RUN_DOCKER_SMOKE=1 DAI_JUDGE_IMAGE=python:3.12-slim` | 通过：普通值/异常/不支持对象 |
| 真实容器（判题镜像 numpy 2.5.2） | `DAI_RUN_DOCKER_SMOKE=1 DAI_JUDGE_IMAGE=dai-judge-python:latest` | 通过：ndarray → dtype/shape/data，平台侧数组比较正确通过、错误答案失败 |
| 证据日志 | `.runtime/review-fixes-20261012/evidence/batch3a/grading-values.log` | exit 0 |

单测还捕获并修掉了一个真实缺陷：超大整数触发 CPython 的 `str(int)` 位数上限
（4300 位）抛出的原生 `ValueError`，现改为按 `bit_length` 判定并转成协议错误。

### 9.2 3B 已完成（版本模型、迁移与绑定）

| 模块 | 内容 |
| --- | --- |
| `app/models/__init__.py` | 新表 `judge_grading_versions`（作业题 XOR 考试题 CHECK、同题版本唯一、状态索引）+ 绑定字段：`judge_questions/exam_questions.current_grading_version_id`、`submissions/exam_answers.grading_version_id`、`exam_submissions.grading_version_map` |
| `alembic/versions/e3f4a5b6c7d8_grading_versions.py` | 链在 `d2e3f4a5b6c7` 之后；只加表/列/索引，全部可空（历史数据保持 NULL），inspect 判存支持部分重跑 |
| `app/services/grading_version_service.py` | 草稿创建（payload 结构 + F≤60/R≤10 校验、摘要计算）、确认（摘要不符拒绝）、启用（同题旧 active 自动退役）、退役、按题取 active、提交绑定、考试冻结映射、答案从映射复制、Rubric 绑定校验 |
| 绑定接线（不改变既有行为） | `POST /judge/submissions` 创建时绑定 active 版本；`start_exam` 冻结逐代码题映射；`save_answer` 与人工评分补建答案时从映射复制。**没有 active 版本时一律留空**，因此现有题库行为完全不变 |

关键语义：版本严格"作业题 XOR 考试题"；`draft` 可改（另存新版本），`confirmed` 后内容不可改
（摘要变化即拒绝确认）；同题最多一个 `active`，`retired` 只禁止新绑定、不影响历史绑定；
考试中途改题目配置不影响已开始的考试（答案只认冻结映射）；旧会话没有基线时保持 NULL
并转人工，绝不静默绑定"最新配置"。

专项证据：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 3B 单测（含真实 API 提交绑定） | `pytest -q tests/automated/test_grading_version_service.py` | 26 passed |
| SQLite 迁移 | `bootstrap_database.py` → head `e3f4a5b6c7d8`、`downgrade -1`、`upgrade head` | 表/5 列随升降级出现与移除，可逆 |
| MySQL 迁移 | 同上（专用 MySQL 8 容器） | `alembic check` 干净；CHECK `ck_grading_version_xor_target`、`uq_grading_version_judge/exam` 均在位；降级后可再升级 |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵（接线后复跑） |

### 9.3 3D 已完成（人工评分闭环与学生结果白名单）

| 变更 | 内容 |
| --- | --- |
| `PATCH /api/v1/judge/submissions/{id}/manual-score` | 新增：教师/管理员对作业提交给出人工最终分与说明。独立于自动判题与 AI——只产生**新的结果版本**（`result_revision` 原子 +1），记录评分者/角色/说明/时间（保留最近 10 条），释放认领并置 `grading_status=completed`；不覆盖 stdout/stderr、不改写 AI `CodeGrade` |
| 拒绝条件 | 非教师/管理员或范围外教师 → 403/404（教学范围过滤器按"不存在"处理，避免存在性泄露）；判题在途 `grading_status=running` → 409 `GRADING_IN_PROGRESS`；`expected_result_revision` 不匹配 → 409 `RESULT_REVISION_CONFLICT`；请求体越界/多余字段 → 422 |
| 学生结果白名单投影 | `GET /judge/submissions/{id}` 与 `/result` 对**学生**返回 `StudentSubmissionRead`：仅 id/题目/状态/分数/通过计数/耗时/逐组得分/安全诊断/人工复核摘要/时间；**不含** `stdout`、`stderr`、`result_details`、隐藏测试内容与教师备注。教师/管理员保持完整视图（复核需要原始输出） |
| 成绩簿闭环 | 人工分进入作业来源分：来源解析读取 `Submission.score`（按 `highest/latest` 选择），下一次确认分项时体现；**已确认快照不被改写**（符合"不重写成绩簿快照"约束） |

专项证据：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 3D 单测 | `pytest -q tests/automated/test_manual_score_and_projection.py` | 15 passed |
| 白名单闸门 | 断言学生响应字段集合**完全等于**白名单，且响应体不含 `SECRET-HIDDEN-TEST`/`stdout`/`result_details` | 通过（未来新增字段若泄漏会直接失败） |
| 在途保护 | `grading_status=running` 时人工分被 409 拒绝且分数不变 | 通过 |
| 乐观并发 | 旧 `expected_result_revision` → 409；当前版本 → 200 | 通过 |
| AI 隔离 | 人工评分后 `CodeGrade.final_score_100` 不变 | 通过 |
| 成绩簿联动 | 人工 95 分后 `source_score` 返回 `95.00`（`10.00` → `95.00`） | 通过 |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵 |

### 9.4 3C 后端契约已完成（编辑器 UI 未实施）

| 模块 | 内容 |
| --- | --- |
| `app/services/grading_conversion.py` | pytest 源码 → 数据用例的**静态 AST 预检**：只接受可证明等价的形态（`assert f(字面量实参) == 字面量`、`assert f(...) is None/True/False`、`with pytest.raises(内置异常): f(...)`），其余逐条给出原因（变量实参、关键字实参、非入口函数、控制流/赋值、不支持的运算符等）。**从不执行源码**、不导入被测模块；源码 64 KiB、用例 256 条上限；只接受 `ast.literal_eval` 字面量 |
| `app/api/grading_versions.py`（9 个端点） | 列表 / 创建草稿 / 详情（含摘要与规则）/ 确认（摘要冻结）/ 启用（同题旧版本自动退役并更新题目绑定）/ 退役（清空绑定）/ 转换预检（只报告不写库）/ 转换应用（落草稿）/ AI 草稿（`source_kind=ai_draft`）。教师须在该作业授课范围内，学生与范围外教师 403 |
| AI 草稿约束 | AI 只能产生 `draft`；`activate` 对未确认版本返回 409；AI 输出同样要过用例结构校验（F≤60/R≤10、用例 id 唯一、input/expected 必须是协议值），无法绕过 |
| 闭环 | 转换/草稿 → 确认 → 启用后，新提交自动绑定该版本（与 3B 绑定契约联动）；退役后新提交不再绑定（转人工），历史版本仍可读 |

专项证据：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 3C 单测（静态分析 + 真实 HTTP 端点） | `pytest -q tests/automated/test_grading_conversion_and_editor.py` | 23 passed |
| "绝不执行源码" | 源码内含模块级与函数级文件写入副作用，断言文件未被创建 | 通过 |
| 预检不写库 | 预检后 `judge_grading_versions` 计数为 0 | 通过 |
| 未确认不可启用 | 草稿/AI 草稿直接 activate → 409 | 通过 |
| AI 模式需 Rubric | `grading_mode=active` 无 Rubric → 422 `RUBRIC_REQUIRED`；不存在的 Rubric → 422 | 通过 |
| 权限 | 学生 403；范围外教师 403；题目不存在 404 | 通过 |
| 聚焦回归 | 转换/版本/人工分/值协议/考试生命周期/判题队列 | 154 passed, 3 skipped |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵 |

#### 3C 前端编辑器（本轮已完成首版）

| 变更 | 内容 |
| --- | --- |
| `frontend/src/api/gradingVersions.js` | 9 个端点的前端契约（列表/草稿/详情/确认/启用/退役/预检/应用/AI 草稿） |
| `frontend/src/components/grading/GradingVersionPanel.vue` | 教师面板：当前启用版本、转换预检结果（可转换/需人工处理 + 逐条原因）、生成草稿、版本表（状态徽标、来源、用例数、摘要短哈希）、确认/启用/退役三态操作；确认与启用必须经确认对话框；接口失败展示后端中文信息 |
| `frontend/src/views/teacher/QuestionEditView.vue` | 在题目编写区新增"可信数据判题"区（仅已有题目 id 时显示），位于 AI 评分配置之前 |
| 单测 | `src/components/grading/__tests__/GradingVersionPanel.spec.js` 8 项：列表与当前版本、空态、预检只报告不生成、无可转换时禁用生成、生成后刷新、确认/启用必须二次确认、失败信息展示 |

验证：`npm run lint` 通过；`npm test` **125 文件 / 1122 项通过**；`npm run build` 通过，
构建产物 `dist/assets/QuestionEditView-*.js` 含新面板。

**仍未实施**：数据用例的逐例可视化表单（目前草稿由转换或 AI/手工 JSON 产生）、
草稿差异与"未确认变更"提示、AI 生成入口按钮（后端端点已就绪）、考试题目的同类面板。
因此教师可完成"预检 → 生成草稿 → 确认 → 启用"的完整闭环，但**没有题目会自动进入
data_v1 判题路径**（Worker 执行接入属 3E 范围，且需 3A～3D 全部通过）。

### 9.5 3E 部分完成（Worker data_v1 执行已就绪；演示库与正式启用未做）

| 变更 | 内容 |
| --- | --- |
| `app/services/data_grading.py` | 平台侧编排：`plan_cases`（容器输入**只含入口与逐例输入**，显式拒绝 expected/分值/权重）、`write_runner_files`、`grade_output`（逐例 `compare_case` → 组内等权计分，F/R 分离）、`single_case_preview` |
| `app/worker/judge_worker.py` | 新增 data_v1 执行路径：`_resolve_bound_version`（提交绑定版本优先；未绑定但题目已启用 data_v1 → **fail closed**，绝不静默用最新配置）→ `_data_v1_judge_submission`（rubric 门禁 → 用例规划 → 沙箱运行 runner → 帧校验 → 计分 → 与 CodeGrade 同一受认领事务 → 入队 AI）。安全参数（`--network none`/`--cap-drop ALL`/只读根/非 root/内存与 PID 限制）与 pytest 路径完全一致，通过 `_run_docker_argv` 复用 |
| 失败关闭语义 | 平台错误（缺帧/重复/非法值/超限/配置不合法）→ 终态 `system_error`、`score=None`、`result_details.platform_errors` 记录原因，**不计 0 分**、不创建 CodeGrade；Docker/基础设施异常 → 可重试（不消耗次数）；`grading_mode != active` 或缺少锁定 Rubric → 配置错误终态 |
| 证据链 | CodeGrade `deterministic_details.grading_version_id` 与 `source.grading_version_id` 记录实际使用的版本；`source.judge_claim_id`/`result_revision` 沿用批次一 |

专项证据：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| data_v1 单测（平台侧 + Worker 端到端，假容器） | `pytest -q tests/automated/test_data_grading.py` | 17 passed |
| 真实容器端到端 | `DAI_RUN_DOCKER_SMOKE=1 DAI_JUDGE_IMAGE=dai-judge-python:latest pytest -k real_container` | 2 passed：正确解 F=60/R=10、3/3；返回不支持对象按**学生失败**处理（0 分而非平台错误） |
| 容器输入不含期望值 | 断言写入容器的 `dai_cases.json` 不含 `expected/max_score/weight/compare` | 通过 |
| 平台错误不计分 | 缺帧/重复/非法值 → `needs_review`、该组 `scored=False`、终态 `score=None`、无 CodeGrade | 通过 |
| fail-closed | 模式不一致、题目已启用 data_v1 但提交无版本基线 → 终态且**不执行任何容器** | 通过 |
| 未启用时行为不变 | 无 data_v1 版本（含已退役）→ 仍走 pytest 路径 | 通过 |
| 聚焦回归 | 判题 Worker/队列/恢复/AI 终态/转换/版本 | 129 passed, 2 skipped |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵 |

**考试编程题也已接入**（同一 ``data_grading`` 编排与 runner）：

| 项 | 说明 |
| --- | --- |
| 版本解析 | 答案绑定的版本优先；父级冻结映射含 data_v1 而答案未绑定 → **fail closed**（终态、不计分） |
| 持久化差异 | `exam_answers` 无 stdout/stderr 列，原始输出以有界片段写入 `result_details.runner_stdout_tail`；其余（Rubric 门禁、模式校验、计分、CodeGrade 同事务、证据链）与作业路径一致 |
| 汇总 | 终态后调用既有 `_maybe_finalize_exam`；active 模式等 AI 收尾 |

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 考试 data_v1 单测 | `pytest -q tests/automated/test_data_grading.py` | 22 passed（含 5 项考试路径） |
| 真实容器（复跑） | `DAI_RUN_DOCKER_SMOKE=1 DAI_JUDGE_IMAGE=dai-judge-python:latest -k real_container` | 2 passed |
| 考试聚焦回归 | 考试系统/生命周期/AI 考试评分/考试判题/汇总并发/重试/Worker | 103 passed, 2 skipped |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵 |

**演示数据与端到端链路已完成**（隔离环境实跑）：

| 组件 | 内容 |
| --- | --- |
| `scripts/seed_grading_demo.py` | 可丢弃库演示种子（需显式 `--allow-disposable-database`）：教师/N 名学生/学期/教学班/课程/授课实例 + 作业 + 编程题（`grading_mode=active`）+ **锁定 Rubric** + **已确认并启用**的 data_v1 版本（F 60 ＋ R 10）；`--judge-image` 会解析本地镜像真实 image ID 并写入环境档位，使演示开箱可判题；幂等 |
| 端到端链路 | 隔离 SQLite（经 `bootstrap_database.py` 迁移到 head）+ 隔离 Redis + 真实 Uvicorn + 真实 Worker 进程 + **真实判题容器**：学生登录 → 提交 → Worker 沙箱判题 → 教师人工最终分 → 学生结果投影 |
| 实测结果 | 提交 201；判题 `grading_status=completed`、`status=running`（active 等 AI）、**3/3 通过、F=60.0、R=10.0**、`grading_version_id=1`、无平台错误；人工评分 200；学生投影 15 个白名单字段、`score=88.0`、**无 stdout/stderr** |
| 证据 | `.runtime/review-fixes-20261012/evidence/batch3e-chain/`（`chain-report.json`、`demo-summary.json`、驱动脚本） |

**本轮顺带修掉的真实缺陷**：新表 `judge_grading_versions` 的迁移最初漏写 `created_at/updated_at`
的 `server_default`，导致"经 alembic 建库"的库插入评分版本时撞 NOT NULL（`create_all` 建库的
测试掩盖了它，`alembic check` 默认不比较 server_default）。已修正迁移并新增门禁测试
`test_migrated_schema_supports_version_insert`（走历史 schema → `upgrade head` → ORM 插入，
同时断言两列的 `server_default` 存在）。

**启用前核查（本轮新增）**——计划要求"实际启用前核查进行中考试和存量任务，不自行假定不存在教学影响"：

| 项 | 说明 |
| --- | --- |
| `GET /judge/questions/{id}/grading-versions/preflight` | 影响面报告：当前启用版本、**在途任务数**（queued/running）、**无版本基线的历史记录数**（启用后若重判将转人工）、**进行中的考试场次数**（这些场次用开考冻结版本，不受切换影响）、`blocking`/`warnings`/`ready` |
| 启用门禁 | `POST …/activate` 在存在在途任务时返回 **409 `IN_FLIGHT_GRADING`**（附原因）；教师确认无影响后可带 `?acknowledge_in_flight=true` 继续。禁止"判到一半"切换评分协议 |
| 编辑器面板 | 面板常驻显示"在途任务 / 无基线历史 / 进行中考试"；核查未通过时确认框列出原因并把确认按钮改为危险样式，确认后携带 acknowledge 参数 |

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 预检与门禁单测 | `pytest -q tests/automated/test_grading_conversion_and_editor.py` | 26 passed（含在途统计、无基线统计、409 门禁与显式确认放行、越权 403） |
| 面板单测 | `npx vitest run src/components/grading/__tests__/GradingVersionPanel.spec.js` | 12 passed |
| 前端全量 | `npm run lint` / `npm test` / `npm run build` | 通过 / 1126 项 / 通过 |

**考试题配置面（本轮补齐）**——此前考试 data_v1 执行路径可用但无法配置：

| 变更 | 内容 |
| --- | --- |
| `app/api/grading_versions_exam.py`（9 个端点） | `/exams/questions/{id}/grading-versions`（列表/创建/预检/转换预检/转换应用/AI 草稿）与 `/exams/grading-versions/{id}`（详情/确认/启用/退役）；与作业侧共用服务层与视图，差别仅权限范围与题目表 |
| 权限 | 教师须拥有该考试所属课程或授课实例（`teacher_offering_ids` 或课程所有者），学生与范围外教师 403 |
| 入口函数 | `exam_questions` 没有 `function_name` 列：入口取请求显式值或 `correct_answer.function_name`，两者都缺 → **422 `ENTRY_FUNCTION_REQUIRED`**（不猜默认值） |
| 编辑器 | `ExamQuestionEditView.vue` 的编程题编辑弹窗新增"可信数据判题"区块（`kind="exam"`）；面板按 kind 分派端点前缀 |
| 联动 | 启用/退役同步维护 `exam_questions.current_grading_version_id`；开考冻结映射 → 答案绑定 → 考试 data_v1 判题（批次三 3E 已验证） |

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 考试版本端点单测 | `pytest -q tests/automated/test_grading_conversion_and_editor.py` | 31 passed（含生命周期/转换/预检门禁/权限/入口函数 422） |
| 面板单测 | `npx vitest run src/components/grading/__tests__/GradingVersionPanel.spec.js` | 14 passed（含 kind=exam 端点前缀与参数） |
| 前端全量 | lint / test / build | 通过 / 1128 项 / 通过 |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵 |

**逐例可视化表单（本轮补齐）**——此前草稿只能来自转换/AI/手写 JSON：

| 变更 | 内容 |
| --- | --- |
| `POST /judge/grading-versions/literal` | 教师输入的 **Python 字面量** → 值协议。服务端 `ast.literal_eval`（只解析字面量，不执行代码），再走 `encode_value` 的上限校验；非法字面量 422 `INVALID_LITERAL`，不支持的值 422 `VALUE_NOT_SUPPORTED` |
| `POST /judge/grading-versions/literal/decode` | 协议值 → 可编辑字面量文本（编辑器回显）；数组返回 `<array dtype shape>` 标记 |
| 面板"新建草稿（逐例表单）" | 入口函数 + 用例行（ID / 组 / 维度 F·R / 组满分 / 输入 / 期望值 / 比较规则 literal·identity·float·exception 及其容差或异常类型）+ 添加·删除行；保存时逐行编码字面量、按组聚合、提交新草稿；逐行必填校验与后端错误展示 |
| 草稿差异提示 | 存在比启用版本更新的草稿时给出"需先确认再启用"提示 |

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 字面量与表单后端单测 | `pytest -q tests/automated/test_grading_conversion_and_editor.py` | 42 passed（含 11 项字面量编解码：往返、元组/列表区分、非字面量拒绝、数组标记、权限） |
| 面板单测 | `npx vitest run src/components/grading/__tests__/GradingVersionPanel.spec.js` | 22 passed（含表单保存 payload、必填校验、编码失败、浮点规则） |
| 前端全量 | lint / test / build | 通过 / **1136 项** / 通过 |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵 |

**AI 生成入口（本轮补齐）**——生成结果必须过静态转换才会成为草稿：

| 变更 | 内容 |
| --- | --- |
| `POST /judge/questions/{id}/grading-versions/ai-generate` | 复用既有 AI 测试组生成（快照/限流/错误映射一致），把生成的测试代码拼接后做 **AST 预检**：可转换 → 落 `ai_draft` **草稿**（附逐条转换摘要与 warnings）；不可转换 → **422 `AI_GENERATION_NOT_CONVERTIBLE`** 并给原因，**不留任何版本** |
| 门禁 | AI 未就绪 → 503 `AI_NOT_READY`；限流沿用每用户 60 秒 5 次（Redis 故障 fail-closed）；非教师/范围外 403；入口函数非法 422 |
| 面板 | 作业题显示"AI 生成草稿"按钮；成功提示"AI 已生成草稿版本 #N（M 条用例，需确认后启用）"；失败（如未配置 API Key）原样展示后端中文信息 |
| 边界 | 考试题不提供 AI 生成（`exam_questions` 没有 `function_name`，无法安全生成入口调用）；考试题仍可用转换预检/逐例表单/`ai-draft` |

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| AI 生成单测（假生成器） | `pytest -q tests/automated/test_grading_conversion_and_editor.py` | 47 passed（含：可转换→草稿且**不自动启用**、不可转换→422 且零版本、服务异常映射 504、AI 未就绪 503、权限与入口函数 422） |
| 面板单测 | `npx vitest run src/components/grading/__tests__/GradingVersionPanel.spec.js` | 25 passed（含 AI 按钮成功/未就绪/考试题不显示） |
| 前端全量 | lint / test / build | 通过 / **1139 项** / 通过 |
| 全量回归 | `pytest -q -n 4`（SQLite） | 见 §7 矩阵 |

**仍未实施**：在生产/演示题库中为**既有**题目开启 data_v1（本轮只在一次性隔离环境验证链路；
开关未打开，既有题目行为完全不变）；"AI 合分"外呼需要外部 AI 服务与数据治理门（当前
`DAI_AI_ENABLED=false`，active 模式停在等待 AI，自动 F/R 分已完整落库）。

- 未改动题目编辑器与 AI 生成路径（仍以 pytest 字符串为中间格式），未新增 AST 转换预检/确认接口（3C）；
- **data_v1 尚未接入 Worker 执行**：版本可创建/确认/启用/绑定，人工评分与学生投影已就绪（3D），
  但没有题目配置入口（3C），因此没有任何题目会进入新判题路径；旧 `legacy/shadow/active` 与
  pytest 判题路径行为不变；
- 未做演示库数据与端到端启用（3E）。

后续按 3C（编辑器/生成/转换预检）→ 3E（演示库与端到端启用）推进；3A～3D 全部通过前不启用
新的自动正式评分（3D 的人工评分与学生投影**不依赖** data_v1，已可独立使用）。
批次一引入的 `result_revision`、`_complete_with_code_grade`，以及 3A 协议/比较器、
3B 版本绑定、3D 人工闭环是其前置基础。


---

## 10. 证据索引

| 内容 | 路径 |
| --- | --- |
| 基线快照（HEAD、diffstat、源文件摘要） | `.runtime/review-fixes-20261012/baseline/` |
| 隔离运行环境（RUN_ID → 端口/URL） | `.runtime/review-fixes-20261012/env/` |
| 每项检查的命令、环境、退出码、日志 | `.runtime/review-fixes-20261012/evidence/<RUN_ID>/` |
| 修复前红态（暂存基线工作副本） | `/tmp/pre-batch1`（临时；结论已写入本文档与 `PROGRESS.md`） |
| 容量报告与 manifest | `.runtime/review-fixes-20261012/capacity-30.json`、`capacity-manifest-30.json` |
| 执行进度与分批结果 | `.runtime/review-fixes-20261012/PROGRESS.md` |

`scripts/verify_env.py` 记录的每次运行都带 `exit_code`、`duration_s` 与实际
`DAI_REDIS_URL`/`DAI_DATABASE_URL`，可直接复核。

## 11. 回滚说明

1. **代码**：本轮改动全部为增量（新增模块/字段/错误码/路由/测试），旧客户端省略
   `client_request_id` 时行为不变；回退镜像即可恢复旧行为。
2. **数据库**：迁移链 `b7c8d9e0f101 → c1d2e3f4a5b6 → d2e3f4a5b6c7` 均为结构增量，
   已在可丢弃库验证 `downgrade -1` 后再 `upgrade head`。**正式环境不自动降级**：
   已产生 `result_revision` / 请求键数据时优先前向修复（新增列可空或带默认值，
   删除会丢审计语义）。
3. **只读回退**：本轮未改历史成绩、未改成绩簿快照、未重评、未入队任何历史任务，
   因此不存在需要回滚的业务数据。
4. **运行配置**：Nginx 名单导入 3 MiB 与 Redis 有界超时均为配置级改动，回退配置
   即可恢复；已放行的请求不受影响。

---

## 12. 最终验收收口（本轮全组复跑）

在全部改动落地后，按 §7 矩阵逐组复跑（同一隔离启动器、随机端口、不复用共享 Redis）：

| 组 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 SQLite | `scripts/verify_env.py run final-accept -- pytest -q -n 4` | **2190 passed / 63 skipped / 0 failed**（10:51，exit 0） |
| MySQL 专项 | 账号并发 / 成绩簿并发 / 写入范围 / 只读 / 历史迁移 / 排序 / AI 迁移 | 47 passed |
| MySQL 认领与过期 | `test_judge_claim_mysql.py`（专用空 schema） | 2 passed |
| 原生 HTTP 组 | `pytest -q tests/native_http`（真实 Uvicorn 子进程） | 5 passed |
| 真实 Nginx 上传边界 | `DAI_TEST_NGINX_IMAGE=nginx:alpine pytest test_import_proxy_limits.py` | 1 passed |
| 浏览器 mock 组 | `exam-acceptance-regressions` + `exam-active-ui` + `layout-regression` | 39 passed |
| 浏览器真实全栈 | 新建 MySQL schema + `bootstrap` + `seed_e2e` + 真实 API(8099) + Vite 代理(5181) → `exam-flow.spec.js` | 2 passed（真实登录/开始/自动保存/交卷/评分 + 教师成绩详情打印） |
| 前端 lint / 单测 / 构建 | `npm run lint` / `npm test` / `npm run build` | 通过 / **1132 项** / 通过 |
| 高信号 lint | `ruff check app/` | 通过 |
| Secret 扫描 | `python scripts/check_secrets.py` | clean |
| SCA 门禁 | `python scripts/check_sca.py` | 通过（接受清单 0 包） |

差异规模：69 个已跟踪文件改动（+3289 / −565），另有新增文件与测试；工作区既有改动、
未跟踪文件全部保留（未 reset/clean/提交）。

**收口结论**：计划中"仓库内可执行"的修复、优化与验证项均已完成并留有可复核证据；
未完成项全部属于外部环境门禁或需操作者显式执行的动作（见 §8、§9.4、§9.5），
本轮未对任何外部事项声明通过。

---

## 13. 2026-10-12 独立验收整改（进行中）

独立验收结论为**不通过**，列出 A01–A18 阻断项。本节逐项记录整改状态；
未完成项一律标注"未修复"，不并入 §12 的收口结论。

### 批次① 权限 / 投影 / 人工事务（已修复并验证）

| 编号 | 问题（验收原文要点） | 修复 | 回归 |
| --- | --- | --- | --- |
| A01 | 作业路由只对 `judge_question_id` 非空校验权限，拿考试版本 ID 可跳过全部授权（学生 GET 200 且含隐藏 expected） | `require_assignment_version`：目标必须是作业题，否则 404；`require_assignment_version_editor` 统一按真实目标做角色 + 教学范围校验。get/confirm/activate/retire 全部改用它；考试路由侧原有的"拒绝作业版本"保持不变 | `test_acceptance_blockers.py::test_a01_*`（3 项）：学生与范围外教师读/确认/启用/退役均 404、响应不含 expected/999、状态未被改写；反向（考试路由拒作业版本）通过 |
| A02 | 提交 POST 与同键重放返回完整 `SubmissionRead`，可取回 GET 已隐藏的 stdout/stderr/result_details | 提交端点改为 `response_model=None`；**三条返回路径**（幂等命中 / 并发 winner / 新建）统一返回 `student_projection` 白名单 | `test_a02_*`（3 项）：提交响应字段集合 == 白名单；预置敏感 stdout/result_details 后同键重放无泄漏且仍给出分数；并发 winner 同样投影 |
| A03 | 人工评分只拒绝主判题 running；queued 提交与 pending AI 随后用旧认领覆盖人工分（实测 88 → 70）；修订号先读再 ORM 写 | 在途检查覆盖 `queued/running` 主判题 **与** `pending/running` CodeGrade（409 `AI_GRADING_IN_PROGRESS`）；写入改为**单条条件 UPDATE**：以读到的 `result_revision` 为 CAS 条件，同事务写分数/终态/修订号 +1/释放认领；并对残留在途 CG 失效认领并标记"教师人工评分已覆盖"（保留 AI 分数与明细证据） | `test_a03_*`（4 项）：queued 409、pending AI 409 且分数不变、两个教师并发第二个 409 且修订号只 +1、AI 证据保留 |

红→绿证据：临时恢复旧行为后，A01/A03 回归 **5 failed**；恢复修复后 **7 passed**（`test_acceptance_blockers.py -k "a01 or a03"`）。

### 批次① 附：真实前端契约（A04 部分）

| 编号 | 问题 | 修复 | 回归 |
| --- | --- | --- | --- |
| A04 | `frontend/src/api/gradingVersions.js` 缺少 `encodeLiteral/decodeLiteral/aiGenerate`，且 URL 固定 `/judge`（考试面板会打到作业资源）；面板调用的方法实际不存在 | 重写该模块：`base(kind)` 分派 `/judge` 与 `/exams`，补齐 13 个方法（含 preflight/convert*/aiDraft/aiGenerate/literal 编解码），全部版本级与题目级端点按 kind 走正确前缀 | 新增 `frontend/src/api/__tests__/gradingVersions.spec.js`：**直接导入真实模块**（只替换底层 axios），断言方法齐全、作业/考试全部 URL、考试题不含 `/judge/` |

验证：`npx vitest run src/api/__tests__/gradingVersions.spec.js src/components/grading/__tests__/GradingVersionPanel.spec.js` → **30 passed**；前端全量 `lint` 通过、**126 文件 / 1144 项通过**、`build` 通过。

批次① 全量回归（含 A01–A03、A06 后端、审计、AI override、Redis 503 与文档门禁）：
`scripts/verify_env.py run accept-fix2 -- pytest -q -n 4`
→ **2226 passed / 63 skipped / 0 failed**（12:52，exit 0）；`test_acceptance_blockers.py` 14 passed。

### 顺带修复的验收工具门禁

`docs/review-acceptance-20261012.md`（独立验收报告）中的 25 个源码引用使用了绝对路径的
Markdown 链接（链接目标形如 `/home/zxk/.../file.py:行号`），触发仓库文档门禁
`test_docs_links.py` 断链失败
（该门禁会剥离 `#锚点`，但不认 `:行号`）。已在不改动报告文字与行号的前提下，把链接目标
规范为相对路径 + 行锚点（`../backend/app/api/grading_versions.py#L291`），
`tests/automated/test_docs_links.py` → **72 passed**。

### 批次① 后端剩余项（本轮补齐）

| 项 | 修复 | 回归 |
| --- | --- | --- |
| A06 服务端终态/来源 | 学生投影新增 `processing_terminal`（completed/system_error 即终态，前端可停止轮询）、`score_finalized`（有终分且无在途）、`grading_source`（manual/ai/auto） | `test_a06_projection_exposes_terminal_state_and_source`、`test_a06_manual_score_marks_source_and_is_audited_append_only` |
| 人工历史非 append-only | 新增表 `manual_score_audits`（迁移 `f4a5b6c7d8e9`，链在 `e3f4a5b6c7d8` 之后）：每次人工给分**新增一行**，记录目标、分数、理由、评分者、写入后修订号与前一次分数；`result_details.manual_review_history` 仅作展示 | `test_a06_manual_score_marks_source_and_is_audited_append_only`（三次给分保留三行，修订号 4/5/6） |
| AI override 缺失 F/R 按零合分 | 端点不再把缺失当 0：判据为 `deterministic_details.groups` 是否记录过确定性结果（列有 `default=0`，无法用数值区分"未判题"与"真 0 分"）；缺失且未显式提供 → **422 `DETERMINISTIC_SCORES_REQUIRED`**；请求体新增可选 `functional_score`/`robustness_score` | `test_a06_ai_override_requires_deterministic_scores`（缺 F/R → 422；补齐 → 200 且分数入库） |
| 撤销复查 Redis 故障未映射 503 | `teaching_query_policy.lock_current_actor` 的 `blacklist:jti` 复查改为 fail-closed：Redis 异常 → **503 `SERVICE_UNAVAILABLE`**（既不放行已撤销 Token，也不冒泡 500） | `test_revocation_recheck_fails_closed_with_503` |
| 审计迁移验证 | SQLite：`bootstrap_database.py` → head `f4a5b6c7d8e9`；`downgrade -1` 移除审计表；`upgrade head` 恢复；单一 head | 见本轮日志 |

### 批次② 前端契约与考试边界（进行中）

| 编号 | 问题 | 修复 | 回归 |
| --- | --- | --- | --- |
| A05 | 面板仅 `onMounted` 加载，父级无 key：从题 A 切到题 B 后仍显示/可操作题 A 的版本 | 面板新增 `watch([questionId, kind])`：切题或换 kind 时 `resetState()` 清空版本/预检/用例明细/确认框/表单，再重新加载；`loadGeneration` 丢弃迟到响应；父级 `QuestionEditView`/`ExamQuestionEditView` 同时按 `kind+questionId` 加 `:key` 强制重挂载（双保险） | 面板 3 项：切题清空并重载、迟到响应被丢弃、kind 切换重载；确认框在切题后关闭 |
| A07 | 多题冲突共用一个 `conflict`（处理完一题另一题永久 blocked）；离线到点后联网同步不解除到期锁 | 冲突改为队列：`conflictQueue` + `shiftConflict()`，`resolveConflict` 后自动展示下一题；抽出 `resumeIfExtended(data)` 并在 `onOnline()` 中调用——联网同步若确认"仍在进行且截止已延后"则解锁、清 `expiredHandled`、关闭离线到期对话框 | ExamView 3 项：两题冲突逐题排队且都清空、离线到点后联网延时解锁、服务器已收卷则保持锁定 |
| A18 | 批量保存响应先应用成功项再发现重复（坏响应会清掉待确认草稿）；`TeacherExamPaper` 的 AI 详情按钮固定 `/teacher` 前缀，管理员被守卫重定向 | `validateBatchResults()` 先整体校验（重复/缺失/未知题目/畸形一律整批不应用并保留草稿与版本），校验通过后才应用；`goAiReview` 改用 `areaPathForRole` 按已鉴权前缀跳转 | ExamView 2 项：校验规则 5 种非法形态 + 合法通过、重复响应整批不应用；TeacherExamPaper 8 项在新增 `useRoute` 后全部通过 |
| A04 | 见批次①（API 模块重写 + 真实模块契约测试） | — | — |

验证：`npx vitest run src/views/student/__tests__/ExamView.spec.js src/components/grading/__tests__/GradingVersionPanel.spec.js src/components/teacher/exam/__tests__/TeacherExamPaper.spec.js` → 66 passed；前端全量 `lint` 通过、**126 文件 / 1152 项通过**、`build` 通过。

### 批次② 完成 + 批次③ 进行中

| 编号 | 问题 | 修复 | 回归 |
| --- | --- | --- | --- |
| A06 前端（学生） | 学生页要求 `graded` + AI breakdown 才停止轮询：人工定分/进入复核后一直轮询到"判题超时" | `SubmissionView`/`AssignmentDetailView` 改为**后端终态优先**（`processing_terminal`）；新增可读状态行（"教师已人工评定最终分"/"已进入人工复核"） | `SubmissionPolling.spec.js` 3 项：人工定分停止轮询并提示、复核态停止轮询、仍在判题继续轮询 |
| A06 前端（教师） | 作业提交详情没有人工评分入口 | 新增人工评分表单（分数 + 必填理由 + 携带 `expected_result_revision` 的乐观并发）；409 分别提示"判题或 AI 仍在进行"与"结果已被他人更新"；`judgeAPI.manualScore` | `JudgeSubmissionDetailView.spec.js` 5 项 |
| A17 | AI 生成读取 `result.groups`，真实 schema 是 `test_groups` → 成功路径 500 | 改读 `result.test_groups`；**测试改用真实响应模型**（`TestGroupsGenerateResponse`）构造模拟结果 | 红→绿：旧字段下 2 failed，修复后 5 passed |
| A10 | 部分转换（有不可转换用例）被当作等价迁移直接落草稿 | `convert-apply`（作业+考试）新增 `allow_partial` 门禁：存在不可转换用例且未显式确认 → **422 `CONVERSION_INCOMPLETE`**（附原因与计数，且不落任何版本）；显式确认后草稿 context 标记 `partial_conversion`/`unsupported_count`/`unsupported_reasons` | 3 项：部分转换被拒且零版本、显式确认后留痕、完整转换无需标记、考试侧同样门禁 |
| A11 | 真实 runner 的 codec 缺 bytes 输入解码与普通 dict 输出编码（主机允许的类型到容器后失败/被当学生异常） | runner 源码补 `bytes` 解码与 `dict` 编码，与主机协议逐类型对齐 | 单测 13 项类型往返（主机编码→容器解码、容器编码→主机解码）+ 真实容器 1 项（bytes 输入 → dict 输出 → 可信比较 F=60） |
| A08 | manual_review 绑定或无基线时返回 None 继续走旧 pytest，可能产生不可信自动正式分 | 版本解析改为四态（`data_v1`/`manual_review`/`missing_baseline`/`pytest`）：绑定人工复核版本或绑定悬空 → **终态 system_error + `MANUAL_REVIEW_REQUIRED`，不执行任何自动评分器**；题目已启用版本但提交无基线 → fail-closed；完全没有版本体系的历史题目行为不变 | 4 项（作业 manual_review、悬空绑定、考试 manual_review、无版本体系仍走 pytest） |

验证：后端 `test_data_grading.py` 26 passed（含真实容器 3 项）、`test_grading_values.py` 76 passed、`test_grading_conversion_and_editor.py` 50 passed；前端 `lint` 通过、**127 文件 / 1160 项通过**、`build` 通过。

| A09 | data 路径只支持实时 active；AI 侧仍读实时题目/context/cap/points，评分版本未完整冻结 | 新增 `frozen_grading_facts()`：绑定版本存在时，AI 评分的**题面/上限规则/考试分值**全部取自版本冻结事实（`context_snapshot`/`payload.score_cap_rules`）；结果留痕 `deterministic_details.grading_version_id`；考试侧 `freeze_exam_versions` 在**开始考试时冻结分值**（映射值改为 `{"version_id","points"}`，旧裸 int 数据兼容读取）；无绑定版本的历史题目行为不变 | 2 项：①冻结题面进入 AI 请求、题目改写不进入、冻结上限 40 生效（实时 cap=100 被忽略）、版本号留痕；②考试分值改为 50 后已绑定答案仍按冻结的 20 折算（70/100×20=14） |

验证：`test_ai_grade_terminal_state.py` 16 passed；`test_grading_version_service.py`/`test_data_grading.py`/`test_exam_lifecycle_v2.py` 65 passed；后端全量见下。

| A12 | 无界 capture、跨组状态污染、结果与诊断通道未分离、学生可读到后续输入 | runner 协议重写：①**交互输入**——`dai_cases.json` 只含入口函数，用例由主机逐帧写 stdin，且**等当前用例的结果帧到达后才写下一帧**（学生代码执行期间后续输入不在容器内）；②**组间隔离**——每个用例用独立模块命名空间重新加载学生代码；③**独立结果通道**——结果只写 stderr，逐帧 HMAC-SHA256 签名（stdout 只作诊断留档），主机校验签名，伪造/未签名帧一律平台错误；④**有界读取**——stdout/stderr 各 4 MiB 上限、单帧 64 KiB 上限，超限立即 `docker rm -f` 终止并返回 rc 125（平台错误，绝不计 0 分） | 单测 4 项：伪造签名、未签名、stdout 注入同名帧、超长帧不写入容器；真实容器 3 项：**后续输入不可见**（select 不可读）、**用例间全局状态不共享**、**持续输出洪泛被终止且不计分**；另有真实容器 3 项既有用例改走新协议后通过 |

批次③ 修复过程中另修：`BufferedReader.read(n)` 会等满缓冲导致逐用例握手死等（改 `read1`）；主机侧 `parse_case_frames` 现要求签名帧。

验证：`test_data_grading.py` 33 passed（含真实容器 6 项，全部实跑）、`test_grading_values.py` 76 passed、`test_ai_grade_terminal_state.py` 16 passed；后端全量见下。

### 批次④ 验收工具与文档

| 编号 | 问题 | 修复 | 回归 |
| --- | --- | --- | --- |
| A13 | 容量脚本 `passed` 只看延迟分位数：纯内存 Fake 探针 15 次保存全 500 仍 `passed=true` | 抽出 `build_targets()` 门禁：延迟分位数 **加** `zero_errors`、`full_coverage`（人数 + 样本量）、`every_student_saved`、`final_answers_verified`；报告新增 `coverage` 段；失败退出码非零 | `test_capacity_verifier.py` 7 项：全员 500 必须失败、延迟达标但未验证学生失败、零学生失败、健康场景通过并给出覆盖数据、退出码口径；另实跑"无服务"场景确认退出码非零 |
| A14 | 启动器不清理 `DAI_TEST_*`、不隔离 dotenv、以 backend 为 cwd、可能回退默认 Redis | `child_environment()`：删除全部 `DAI_TEST_*` 与 `DAI_DATABASE_URL/DAI_REDIS_URL/DAI_SECRET_KEY/DAI_AI_API_KEY`，注入本轮隔离 Redis，`DAI_ENV_FILE` 指向空 dotenv；`run` 默认 `--cwd temp`（显式临时目录），需要仓库相对路径时显式 `--cwd repo|backend|路径`；命令内相对可执行路径按调用者目录解析（不 `resolve()`，避免绕开 venv 符号链接） | 2 项单测 + 实跑：父进程设 `DAI_TEST_LEAK` 与真实库 URL，子进程看到 `DAI_TEST_LEAK=None`、`DAI_DATABASE_URL=None`、隔离 Redis、空 dotenv、cwd=/tmp/dai-verify-… |
| A15 | `op_metrics.record` 只进本进程内存，仅 API lifespan 启动 flush：Judge/AI Worker 指标到不了 Redis | 新增 `flush_for_worker(force=False)`（限频 1s、失败丢弃并计数），在 Judge 作业/考试两条任务结束路径的 `finally` 中调用（异常路径同样冲刷） | 3 项：Worker 冲刷写入 Redis（聚合值正确、内存清空）、限频窗口内不重复写、两条 Worker 路径都调用 flush |
| A16 | Ruff 继承仓库高信号配置（F401 在 backend cwd 得到空诊断）；Radon 任意 JSON 都记 ok | Ruff 改为 `--isolated --no-cache --select <固定规则集>`；Ruff/Radon 输出都做**结构校验**（诊断项必须含 code/message/location；复杂度必须是 `{file: [block]}` 且 complexity ≥ 1、rank 合法），结构不符记 `invalid_result` 而非 ok | 4 项：F401/F841 必报、源码含 isolated/no-cache/固定规则、非法 Radon/Ruff JSON 记 invalid_result 且整体非 ok、真实 Radon 输出结构合法 |
| 文档校正 | 测试数字/完成状态混用、迁移链遗漏实际 head、容量归因缺数据、"未实施"与已实现并存、旧容量结论当门禁 | 迁移链补实际 head `f4a5b6c7d8e9`；容量章节加**门禁有效性声明**（旧判据产出，100/200 档需按新判据重跑，不得作验收依据）与**归因更正**（锁/CPU 归因为推断、未测量；4–7 实例为线性外推估算）；§13 逐项列出本轮修复与回归 | 文档门禁 `test_docs_links.py` 通过 |

### 批次①–④ 收口验证（A01–A18 全部整改后）

| 组 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 SQLite | `verify_env.py run accept-fix4 --cwd backend -- pytest -q -n 4` | 见本轮最终数字（下方） |
| 真实容器（data_v1 runner） | `DAI_RUN_DOCKER_SMOKE=1 pytest -k real_container` | **6 passed**（含 A11 bytes/dict、A12 输入不可见/组间隔离/输出洪泛） |
| 前端 | `npm run lint` / `npm test` / `npm run build` | 通过 / **127 文件 1160 项** / 通过 |
| 迁移 | SQLite `bootstrap_database.py` → head `f4a5b6c7d8e9`、`downgrade -1`、`upgrade head` | 单一 head、可逆 |
| 文档门禁 | `test_docs_links.py` | 通过 |

**最终数字**（A01–A18 全部整改后的最终一轮）：

| 组 | 结果 |
| --- | --- |
| 后端全量 SQLite（`accept-fix4-final-sqlite`） | **2268 passed / 67 skipped / 0 failed**（12:02，exit 0） |
| 真实容器 data_v1 runner（opt-in） | **6 passed**（A11 bytes/dict、A12 输入不可见/组间隔离/输出洪泛） |
| MySQL 专项（保留 schema：`dai_test_account_*` / `dai_school_*_verify`） | **29 passed / 9 skipped / 0 failed**（skip 为未设置额外保留 schema 的自跳过） |
| MySQL 迁移 | `bootstrap_database.py` → `f4a5b6c7d8e9 (head)`；`alembic check` → No new upgrade operations detected |
| 原生 HTTP（真实 Uvicorn 子进程） + 文档门禁 | **77 passed** |
| 前端 lint / 单测 / 构建 | 通过 / **127 文件 1160 项** / 通过 |

证据目录：`.runtime/review-fixes-20261012/evidence/accept-fix4/`（含 `accept-fix4-final-sqlite.log`）。

### 尚未修复（下一步）

A01–A18 与三项补充（人工审计 append-only、AI override 缺失 F/R、撤销复查 Redis 503）
均已修复并有回归覆盖。剩余为**操作者/外部事项**（不会被自动整改覆盖）：

1. 按新判据（A13）重跑 30/100/200 人数档位容量验证——旧判据产出的 `passed` 不再有效；
2. 目标机 1800 秒稳定期、锁等待/事务/CPU 采样，据此确认单实例容量与实例数估算；
3. 正式后端镜像内运行 `ruff`/`radon` 的 `analyze_python` 冒烟（本机无 PyPI 网络）；
4. 为既有题目开启 data_v1 前的影响面预检（在途任务/无基线历史/进行中考试）；
5. AI 合分外呼所需的 AI 服务与数据治理审批；GPU、生产 TLS、Registry、备份恢复演练。

## 14. 2026-10-12 最终集成验收与交付状态

本节是本轮实际复跑结果，优先于前文历史阶段数字；不把历史通过数当作本次证据。

| 门禁 | 实际命令/环境 | 结果 |
| --- | --- | --- |
| 后端 SQLite 全量 | `python3 scripts/verify_env.py run close-20261012-r01-a7f2 --slug sqlite-full-final --cwd backend -- /home/zxk/Projects/dai-experiment-platform/.runtime/school-security-venv/bin/python -m pytest -q -rs -n 4`；隔离 Redis、SQLite | **2287 passed / 68 skipped / 0 failed**；759.28 秒；exit 0 |
| R01–R08 专项 | 隔离环境运行本轮判题/评分版本/转换/容量工具/提交幂等专项 | **312 passed / 10 skipped / 0 failed**；跳过均为 opt-in Docker 用例 |
| 真实逐例 Docker | `DAI_RUN_DOCKER_SMOKE=1`，本机 `dai-judge-python:latest` | **116 passed / 0 skipped**；bytes/dict/异常协议、用例隔离和有界输出在真实容器中验证 |
| 原生 HTTP | `python3 scripts/verify_env.py run close-20261012-r01-a7f2 --slug native-http-final --cwd backend -- /home/zxk/Projects/dai-experiment-platform/.runtime/school-security-venv/bin/python -m pytest -q -rs tests/native_http`；真实 Uvicorn、独立 SQLite、隔离 Redis | **5 passed**；15.26 秒；exit 0 |
| Nginx 上传边界 | `python3 scripts/verify_env.py run close-20261012-r01-a7f2 --slug nginx-upload-boundary-final --cwd backend -- env DAI_TEST_NGINX_IMAGE=nginx:alpine /home/zxk/Projects/dai-experiment-platform/.runtime/school-security-venv/bin/python -m pytest -q -rs tests/automated/test_import_proxy_limits.py` | **1 passed**；真实容器；exit 0 |
| 文档链接与秘密扫描门禁 | `pytest -q -rs tests/automated/test_secret_scan.py tests/automated/test_docs_links.py` | **83 passed**；秘密扫描回归包含 OpenCode `oc_sk_` 格式，匹配内容不会写入输出 |
| 浏览器 mock UI | `E2E_BASE_URL=http://127.0.0.1:5181 npm --prefix frontend run e2e -- exam-acceptance-regressions.spec.js admin-account-reset-ui.spec.js` | **9 passed**；考试保存/离线到期/管理员路由/账号重置宽窄屏；未匹配 API 会使测试失败 |
| 前端门禁 | `npm --prefix frontend run check` | lint 通过；**127 文件 / 1160 项通过**；build 通过。Vite 保留一条 router 静态与动态导入提示，不影响构建 |
| Ruff | `/home/zxk/Projects/dai-experiment-platform/.runtime/school-security-venv/bin/python -m ruff check backend/app` | All checks passed |
| SCA / 秘密扫描 | `/home/zxk/Projects/dai-experiment-platform/.runtime/school-security-venv/bin/python scripts/check_sca.py`；`python3 scripts/check_secrets.py`；提交候选扫描 | SCA 与接受清单一致（0 包）；跟踪源码 clean；提交候选 138 个改动/未跟踪文件扫描 clean，含 OpenCode `oc_sk_` 模式 |
| 结构与差异 | `/home/zxk/Projects/dai-experiment-platform/.runtime/school-security-venv/bin/python -m compileall -q backend/app backend/tests/automated backend/tests/native_http scripts`；`git diff --check` | 通过 |

SQLite 全量 68 个跳过项来自未提供的专用 MySQL schema、可选 Docker/Nginx 与实验镜像。Nginx 和可信数据 runner 已另行真实容器通过；Docker Lab、MySQL 专项仍未在本轮运行。此次没有 MySQL 测试 URL，`docker ps` 中也没有测试 MySQL 容器；没有访问或迁移业务数据库。

全量 pytest 产生约 20,185 条临时目录清理警告，根路径是既有共享 `backend/.pytest-temp-root`，包含非本轮目录，本轮保留未清理。已停止本轮 Vite 和独立 Redis；删除了本轮 Playwright 输出截图及 `frontend/test-results`。`frontend/dist` 是可重生成构建目录，不加入 Git。私有恢复补丁、`.runtime` 证据目录及历史报告均保留。

仍需另行完成：按 A13 新判据重跑 30/100/200 人容量档（现有旧判据结果不得引用）、目标机 1800 秒稳定期及锁/事务/CPU 采样、正式镜像内 `analyze_python` 冒烟、为既有题目启用 `data_v1` 前的影响面预检，以及 AI 外呼审批、GPU、生产 TLS/Registry、备份恢复。浏览器真实全栈与 MySQL 专项因无专用 MySQL 环境未运行。上述事项不改变本轮仓库级门禁结果。
