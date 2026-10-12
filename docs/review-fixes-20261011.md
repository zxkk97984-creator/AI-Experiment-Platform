# 2026-10-11 逐项修复与验证记录

本轮针对 HEAD `5e201aa` 的 11 项缺陷与 3 项待验证项执行修复。分支 `fix/review-20261011`，
**仅本地提交；未推送、未部署、未对业务库执行迁移**。

工作区原有的账号管理/密码重置导入改动（11 个已修改 + 5 个未跟踪文件）全程保留；
其中 `frontend/nginx.conf` 与 `backend/tests/automated/test_import_proxy_limits.py`
属于 Task 10 的叠加基线。

本轮测试环境（每项独立隔离，不使用共享资源）：

- 独立 Redis 容器 `dai-review-redis`（`127.0.0.1:16319`）——本机 6379 属于其他项目，全程未触碰。
- 后端 Python：`.runtime/school-security-venv/bin/python`（3.12.13）。
- 后端命令均带 `env -u DAI_DATABASE_URL DAI_REDIS_URL=redis://127.0.0.1:16319/15`，
  防止测试指向业务库。

---

## Task 2A 判题结果完整性（P1，立即减损）

### 修复内容

| 位置 | 改动 |
| --- | --- |
| `services/deterministic_scoring.py` | 新增 `PYTEST_SUMMARY_RE`、`MAX_CASE_COUNT`、行锚定 `_RESULT_LINE_RE`；`parse_result_json` 改为要求结果行**恰好一条**；新增 `parse_group_result(stdout, stderr, returncode)` 做完整可验证性判定 |
| `worker/judge_worker.py` | 删除与 `deterministic_scoring` **同源的第二份解析拷贝**（两份都取"第一个匹配"，都允许伪造行胜出）；`run_test_groups` 改用 `parse_group_result`；`_status_from_pytest` 在 `rc=0` 时要求存在 pytest 汇总行 |
| `api/judge.py` | `max_attempts` 计数排除 `grading_status == 'system_error'`——平台自身故障不消耗学生次数 |

校验规则（全部在可信的主机侧）：结果行恰好一条；计数为有界非负整数（拒绝 bool）；
退出码只能是 0 或 1（124 是超时，其他为内部/收集错误）；两种退出码都必须伴随 pytest 汇总行；
`rc=0` 不得报告失败或错误，`rc=1` 必须报告至少一个。

不满足即判为不可验证 → 记入 `system_errors` → 四条 runner 路径
（作业 legacy/V1、考试 legacy/V1）均已确认走 `system_error` + `score=None` + 可重试，
**不按 0 分计入**。

### 为什么这不是完整安全修复

学生代码仍与 pytest 插件同解释器，因此**仍可**同时伪造汇总行与结果行。本层只保证：
伪造/矛盾/不完整的输出不会变成自动成绩，并消灭了审查中可复现的两条路径（抢先打印伪造结果行、
`os._exit(0)` 形态的提前退出）。真正的信任边界由 Task 2B 承担。

### 回归证据

新增 `tests/automated/test_judge_result_integrity.py`（17 项）与
`tests/automated/test_judge_attempt_quota.py`（3 项）。

修复前（未改动源码时运行）：

```
14 failed, 3 passed
```

其中集成用例直接复现了审查结论——两个真实失败的测试组 + 伪造满分行仍被判为 2/2 通过：

```
assert out["results"]["F1"]["passed"] == 0
E       assert 2 == 0
```

次数退款用例在还原 `api/judge.py` 到 HEAD 后运行，证明其在修复前确实失败：

```
>       assert response.status_code == 201
E       assert 400 == 201
1 failed
```

修复后：

```
tests/automated/test_judge_result_integrity.py + test_deterministic_scoring.py
25 passed
tests/automated/test_judge_attempt_quota.py
3 passed
```

受影响模块回归（`-k "judge or deterministic or ai_test_groups or ai_regression or test_group"`）：

```
232 passed, 2 skipped, 1805 deselected
```

2 项跳过为需要真实 Docker 的门禁用例（`DAI_RUN_DOCKER_SMOKE=1`），**不计作通过**，
其覆盖范围见 Task 2B 的真实 Docker 组。

`ruff check app/` 通过。

### 顺带修正的既有缺陷

两处测试替身对"成功"场景返回了不含 pytest 汇总行的输出——真实 pytest 通过时必定打印汇总行，
而"rc=0 且无汇总行"正是本次要拒绝的形态：

| 文件 | 原替身 | 现替身 |
| --- | --- | --- |
| `test_courses_assignments_judge.py` | `('...', '', 0, 100)` | `('1 passed in 0.01s', '', 0, 100)` |
| `test_phase5_run_chain.py` | `('', '', 0, 10)` | `('1 passed in 0.01s', '', 0, 10)` |

这是**修正失真的夹具**，不是放宽校验；两处断言（`status == "accepted"`）保持不变。

### 受影响模块回归的完整结果

`-k "exam or submission or courses_assignments"`（在修正上述第二处替身之前运行）：

```
1 failed, 243 passed, 2 skipped
```

唯一失败即 `test_phase5_run_chain.py::test_historical_submission_resubmit_uses_snapshot_digest`，
原因是替身返回 `('', '', 0, 10)`；修正替身后该文件 `19 passed`。

### 未验证边界

- 未使用真实 Docker 验证减损层：单元与集成断言均基于 `_run_docker_pytest` 的替身。
  真实容器内的行为属 Task 2B 的验收范围。
- 未验证真实 AI 总分路径（AI 默认关闭）。

---

## Task 1 判题认领代次（P1）

### 修复内容

| 位置 | 改动 |
| --- | --- |
| `models/__init__.py` | `submissions` / `exam_answers` 增加 `claim_token`（有索引）+ `heartbeat_at`；`code_grades` 增加 `claim_token` |
| `alembic/versions/b7c8d9e0f101_judge_claim_token.py` | 新增迁移（`down_revision = "20261003_0001"`），带 inspect 判存以支持两阶段 bootstrap 的部分重跑 |
| `services/judge_queue.py` | `claim_job` 铸造并返回 token（+ `extra_values` 使 `status='running'` 与认领原子落地）；新增 `finish_job` 为**唯一**终态写入口；`complete_job` / `fail_job` 改为受栅栏保护并返回是否命中；新增 `heartbeat_job`；running 恢复重置改用 token 栅栏（NULL 安全）并清空代次 |
| `worker/judge_worker.py` | 删除全部"先 ORM 赋值 + commit、再 CAS"的结果写入（作业 legacy/V1、考试 legacy/V1、导入诊断、题目缺失、Worker 未知异常）；测试组之间续 `heartbeat_at`；考试 legacy 的完成 CAS 不再是空操作 |
| `services/ai_grading_queue.py` | `claim_ai_grade` 铸造 token；`finish_ai_grade` / `complete_ai_grade` / `fail_ai_grade` 受栅栏保护；`finish_ai_grade` 支持把 `Submission.score` / `ExamAnswer.score` 纳入**同一事务**；stale 阈值 600 → 1800 秒并用 token 作重置栅栏 |
| `services/ai_grading_service.py` | 评分结果改为经 `finish_ai_grade` 的条件 UPDATE 落库，active 模式的正式分同事务写入；失去认领即抛错，不继续推进 |
| `app/config.py` | 新增 `DAI_JUDGE_STALE_RUNNING_SECONDS`（默认 1800） |

**为什么代次不能复用 `attempt_count`**：它是重试预算，会被 `exam_service` 的受控重试
（`:449,:461`）与教师 AI 重试（`api/ai_grading.py:794,934,961`）重置为 0；重置后重新入队会让
旧代次重新命中栅栏，等于把本缺陷重新引入。故采用只增不减的独立随机 token，
与 `LabRun.lease_token` / `EnvironmentBuildJob.lease_token` 一致。

**门面放置**：token 记在 `db.info['judge_claims']` / `db.info['ai_claims']`，
`claim_job` 自动登记，因此 20+ 个 `fail_job` 调用点无需逐层改签名；
未认领却写结果一律**拒绝**（WARNING），避免产生无栅栏写入。

### 回归证据

新增 `tests/automated/test_judge_claim_fence.py`（9 项），覆盖：

- 认领 → 完成正常可用；重复消息不能再次认领已完成任务；未认领写入被拒且不落库。
- **核心**：旧 Worker 认领 → 恢复重排 → 新 Worker 认领并完成 → 旧 Worker 携带旧 token
  再写成功/失败/退回 pending **全部被拒**，新结果（分数、状态、`claim_token`）保持不变。
- 考试答案同栅栏；考试 legacy 完成写入 `finished_at`（原空操作 CAS 的回归）。
- AI：旧运行（重试跨过 stale 阈值的真实路径）不能覆盖新评分。

调试中发现并修复的真实缺陷：running 恢复重置最初写成 `claim_token == job.claim_token`，
NULL 与 NULL 在 SQL 中不是 TRUE，**迁移前/旧代码认领的行（token 为 NULL）将永远无法被恢复**。
已改为 `is_not_distinct_from`（SQLite `IS` / MySQL `<=>`）。

### 既有测试的适配（均为前置条件补齐，非放宽断言）

| 类别 | 处理 |
| --- | --- |
| `claim_job` / `claim_ai_grade` 现在返回 token | `assert ok is True/False` → 真值断言（仅限判题/评分相关变量；`test_scheduler_lease.py` 的无关改动已回滚） |
| 直接调用 `_v1_judge_submission` / `_legacy_judge_submission` 的测试 | 补 `grant_judge_claim(...)` 前置——生产序列本就是认领后才进入判题函数 |
| 直接调用 `fail_ai_grade` / `complete_ai_grade` 的测试 | 先把种子状态改为 `queued` 并认领，再失败/完成（真实 Worker 序列） |
| stale running 恢复测试 | 除 `started_at` 外同时回拨 `heartbeat_at`——存活信号现在是心跳 |
| 全 mock 的 db（`test_judge_worker.py`、`test_checkpoint3_kernel_judge.py`） | 结果写入已从 ORM 赋值改为条件 UPDATE，mock 会话无法反映内存副作用；改为用 `executed_update_values()` 断言发出的 SQL 值。真实落库行为由 `test_judge_claim_fence.py` 覆盖 |

### 验证命令

```bash
cd backend
env -u DAI_DATABASE_URL DAI_REDIS_URL=redis://127.0.0.1:16319/15 \
  ../.runtime/school-security-venv/bin/python -m pytest -q \
  tests/automated/test_judge_claim_fence.py tests/automated/test_judge_queue_reliability.py \
  tests/automated/test_judge_worker_recovery.py tests/automated/test_ai_grading_pipeline.py \
  tests/automated/test_ai_queue_recovery.py tests/automated/test_ai_regression_v3.py \
  tests/automated/test_ai_regression_v4.py tests/automated/test_ai_regression_v5.py \
  tests/automated/test_ai_test_groups.py tests/automated/test_judge_worker.py \
  tests/automated/test_checkpoint3_kernel_judge.py tests/automated/test_op_metrics.py \
  tests/automated/test_scan_concurrency.py
```

迁移单独验证（临时 SQLite，先 `alembic stamp 20261003_0001` 再 `upgrade head`）：
三张表均新增列与索引，`downgrade -1` 可完整回退。

### 未验证边界与残留

- **未在真实 MySQL 上验证**该迁移与 token 栅栏的并发交错；SQLite 通过不能替代。
  设计上已避开 `DATETIME(0)` 截断（栅栏用随机 token 而非时间戳），但需 MySQL 实测确认。
- **未覆盖教师手动改分/覆盖**（`api/exams.py:1229`、`api/ai_grading.py:868+`、
  `api/experiments.py:1499`）：它们是授权人员的显式动作，方向与"旧自动任务覆盖新结果"相反，
  保持普通 ORM 写入。教师 AI 重试已有"评分进行中不重复入队"守卫。
- **未覆盖考试选择题/填空题的同步计分**（`exam_service.py:300-311`）：它们不在判题队列内。
- 未使用真实 Docker + 双 Worker 进程做端到端抢占交错验证；该项由 Task 2B 的
  真实运行组一并覆盖更合适。
