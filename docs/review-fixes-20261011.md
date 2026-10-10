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
