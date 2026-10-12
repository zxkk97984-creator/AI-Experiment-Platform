# 2026-10-12 实施后的独立验收

结论：**验收不通过，暂不清理项目源码或提交 Git。** 当前实现修复了正常 AI 终态及部分事务问题，但可信判题、权限隔离、人工评分和真实前端接入仍有明确缺口。现有回归全部通过不能覆盖这些缺口。

验收基线：`fix/review-20261011`，HEAD `61182f7`；对照修订计划、当前工作树和新增实现，包含原已暂存及未暂存改动。本轮不修改业务代码、配置或暂存区；仅新增本报告及被忽略的验收证据。原有账号导入等改动保留。

## 已确认的阻断项

| 编号/优先级 | 问题及源码依据 | 实际影响 | 修复与验收要求 |
| --- | --- | --- | --- |
| A01 / P1 | [grading_versions.py:291](../backend/app/api/grading_versions.py#L291) 只对 `judge_question_id` 非空的版本检查教师权限；确认、启用、退役存在同一问题。 | 作业路由传入考试版本 ID 时跳过角色/教学范围检查。学生身份 ASGI GET 实测 200，响应含隐藏 expected。 | 按真实目标统一授权，或作业路由拒绝考试版本。学生和范围外教师访问两类版本的读/确认/启用/退役均须拒绝，状态和 payload 不得泄露。 |
| A02 / P1 | [judge.py:102](../backend/app/api/judge.py#L102)、135、187：提交 POST 及幂等回放仍使用完整 `SubmissionRead`。 | 已完成提交的同键重放可取回 GET 已隐藏的 stdout、stderr、result_details。 | 所有学生返回统一安全投影；验收提交后带敏感诊断的同键重放、并发 winner 返回和普通提交响应。 |
| A03 / P1 | [judge.py:388](../backend/app/api/judge.py#L388) 只拒绝主判题 running；人工写入不处理排队/运行中的 CG，修订号使用先读再 ORM 写入。 | 实际人工接口给 88 分后，合法排队 AI 再写为 70 分，人工记录仍为 88。主展示状态也仍为 running。 | 检查判题与 AI 全部在途状态；同事务 CAS 修订号及终态、使旧认领失效、处理关联 CG 并保留原证据。验证人工与 queued/running AI、两个教师并发、旧 Worker 交错。 |
| A04 / P1 | [gradingVersions.js:6](../frontend/src/api/gradingVersions.js#L6) 无 encodeLiteral/decodeLiteral/aiGenerate，全部 URL 固定 `/judge`；[GradingVersionPanel.vue:280](../frontend/src/components/grading/GradingVersionPanel.vue#L280)、404 实际调用缺失方法。 | 逐例保存、回显和 AI 生成报 TypeError；考试面板请求作业资源，可能操作同 ID 的其他题目。 | 补真实 API 方法和按 kind 的目标分派；以实际 API 模块验证两类资源全部 URL。面板 mock 不能虚构未实现的方法。 |
| A05 / P1 | [GradingVersionPanel.vue:487](../frontend/src/components/grading/GradingVersionPanel.vue#L487) 仅 onMounted；[QuestionEditView.vue:820](../frontend/src/views/teacher/QuestionEditView.vue#L820) 无按题目 key。 | 从题 A 切到题 B 后仍显示并可操作题 A 的版本。 | 按 kind+questionId 重挂载，或监听并清空全部状态、拒绝迟到请求；回归在请求未完成和确认框打开时切题。 |
| A06 / P1 | 作业教师详情未接人工表单；[SubmissionView.vue:20](../frontend/src/views/student/SubmissionView.vue#L20)、[AssignmentDetailView.vue:405](../frontend/src/views/student/AssignmentDetailView.vue#L405) 仍要求 graded 且有 AI breakdown。 | 人工评分/等待复核没有完整入口及终态呈现；学生持续轮询或收到判题超时。 | 接人工表单、理由、结果修订号和审计；服务端区分 processing_terminal/score_finalized/grading_source，前端据此展示人工状态并停止轮询。 |
| A07 / P1 | [ExamView.vue:368](../frontend/src/views/student/ExamView.vue#L368) 多冲突共用一个 conflict；640 online 同步不重置到期锁。 | 同批两题冲突处理完一题后，另一题永久 blocked；离线到点后教师延时、恢复网络，输入仍锁定。 | 冲突逐题排队；成功同步后统一判断延时并恢复编辑/清理旧对话框。补两题冲突、离线到期后延时恢复、恢复时已收卷等回归。 |
| A08 / P1 | [judge_worker.py:819](../backend/app/worker/judge_worker.py#L819)、936：manual_review 绑定或无数据基线返回 None，继续旧 pytest 路径。 | 未转换题或转人工题仍可能产生旧的不可信自动正式分；安全降级契约未实施。 | 显式区分 data_v1/manual_review/缺基线，后两者终态转人工，不执行旧评分器；保留原源码、历史成绩及证据。 |
| A09 / P1 | [judge_worker.py:695](../backend/app/worker/judge_worker.py#L695) data 只支持实时 active；[ai_grading_service.py:90](../backend/app/services/ai_grading_service.py#L90)、137、147 仍读实时题目/context/cap/points。 | 可确认启用的 legacy/shadow 数据版本无法执行；旧提交仍受新配置影响，评分版本未完整冻结。 | 从绑定版本读取全部评分事实，开始考试冻结分值；实现计划约定的三种采用策略。修改当前题目及启用新版本后，旧提交结果应保持原基线。 |
| A10 / P1 | [grading_conversion.py:219](../backend/app/services/grading_conversion.py#L219)、[grading_versions.py:410](../backend/app/api/grading_versions.py#L410) 不要求 fully_convertible，只生成可转换部分。 | 一个简单断言加一个循环的原测试，实际生成仅一个 F1 用例的候选，遗漏循环且没有完整 F/R 权重。 | 不完整转换保持需人工；完整补齐后作为显式新规则确认。不能将部分结果当等价迁移启用；保留组维度、名称和权重。 |
| A11 / P1 | [grading_runner.py:97](../backend/app/services/grading_runner.py#L97)、126：真实生成 runner 的 codec 缺 bytes 输入解码和普通 dict 输出编码。 | 主机允许的合法类型到了 runner 后失败，普通 dict 答案可能被当学生异常扣分。 | 主机与容器逐类型契约一致；平台编码错误不能冒充学生异常。对生成 runner 做输入→执行→输出→可信比较回归。 |
| A12 / P1 | [judge_worker.py:127](../backend/app/worker/judge_worker.py#L127) 仍无界 capture_output；[grading_runner.py:152](../backend/app/services/grading_runner.py#L152) 一次读全部输入，各组共用模块、结果使用普通 stdout 前缀。 | 事后字节检查无法限制宿主内存；学生可看到后续输入，组间状态污染，协议与诊断通道未分离。 | 按当前输入交互、组间隔离、独立结果通道；读取阶段限制流量并有界终止容器。验收持续输出、超长帧、跨组状态和后续输入不可见。 |
| A13 / P1（验收工具） | [verify_exam_capacity.py:273](../scripts/verify_exam_capacity.py#L273) passed 未检查 error_count、人数覆盖及成功样本。 | 纯内存 Fake 探针 15 次保存全部 500，仍报告 passed=true。已有容量“通过”不能作为可靠门禁。 | 全部 HTTP/协议/任务异常纳入失败，要求完整人数、每人成功保存和最终答案校验；失败场景退出非零。 |
| A14 / P1（隔离） | [verify_env.py:126](../scripts/verify_env.py#L126) 未清 DAI_TEST_* 或隔离 dotenv；native 子进程以 backend 为 cwd 且可回退默认 Redis。 | 操作者直接运行可能继承真实配置或共享资源。本轮外部显式注入隔离 Redis、从 /tmp 启动全量，不代表启动器自身已保证隔离。 | 明确临时 cwd、DB及全部存储目录，清理测试覆盖变量；native 缺隔离 Redis 时自行建专用资源或明确失败。 |
| A15 / P2 | [op_metrics.py:78](../backend/app/services/op_metrics.py#L78) 所有 record 只进本进程内存；只有 API lifespan 启动 flush。 | Judge/AI Worker 的失败、调用/token 等指标不再汇入 Redis，API 无法看到。 | 为每种 Worker 明确指标 sink 及生命周期；独立进程验证 Worker→Redis→API。 |
| A16 / P2 | [static_analysis.py:43](../backend/app/services/static_analysis.py#L43) Ruff 继承仓库高信号配置；Radon 任意 JSON 可标 ok。 | 未使用 import 在 backend cwd 得到空诊断，无法满足计划 F401 质量分析；非法 Radon 输出可能被误接受。 | 分析器使用固定独立规则，校验结果结构；正式后端镜像运行实际 analyze_python 样例。 |
| A17 / P2 | [grading_versions.py:492](../backend/app/api/grading_versions.py#L492) 使用 result.groups；真实响应 schema 只有 test_groups。 | AI 生成成功后出现属性错误，返回 500。 | 合法模拟响应使用真实 schema；保持 F/R 各组而不是合成单 F1，验证成功路径。 |
| A18 / P2 | [ExamView.vue:332](../frontend/src/views/student/ExamView.vue#L332)、348先应用成功项再发现重复；[TeacherExamPaper.vue:235](../frontend/src/components/teacher/exam/TeacherExamPaper.vue#L235) 仍教师前缀。 | 重复响应清掉待确认草稿；管理员 AI 详情按钮被角色守卫重定向。 | 批量响应先校验再应用；所有共享角色链接取已鉴权前缀。补无效响应及管理员 AI 按钮回归。 |

补充：人工历史只保留最近十条，未实现 append-only 审计；AI override 仍可用缺失 F/R 按零合分；写入前撤销复查存在原始 RedisError 未映射 503 的路径。以上均应随 A03/A06/Redis 专项收口。

## 实际检查与结果

Python 使用 `/home/zxk/Projects/dai-experiment-platform/.runtime/school-security-venv/bin/python`。后端经 `scripts/verify_env.py run codex-accept-20261012` 注入本轮 Redis（随机回环端口59705），父进程 cwd=/tmp；不配置业务数据库，AI 外呼关闭。运行日志记录完整命令、环境和退出码。

| 命令/检查 | 实际结果 | 支持范围 |
| --- | --- | --- |
| `python -m pytest -c <backend/pytest.ini> -q -n 4 <backend/tests/automated> --basetemp=/tmp/dai-accept-pytest --junitxml=/tmp/dai-accept-sqlite.xml` | **2210 passed / 63 skipped**；exit0；382.25秒 | 既有回归通过；不证明未覆盖的权限、跨类型 API 或人工 AI 交错正确。63项跳过不计通过。 |
| 独立进程 `python -m pytest -c <backend/pytest.ini> -q <backend/tests/native_http> --basetemp=/tmp/dai-accept-native --junitxml=/tmp/dai-accept-native.xml` | **5 passed**；exit0；15.22秒 | 真实 Uvicorn 生命周期/到期等；metrics 用例允许未认证401/404，不能证明真正访问指标实现。连接拒绝不能代替慢黑洞故障。 |
| `npm --prefix frontend run lint` | 通过 | 静态检查。 |
| `npm --prefix frontend test` | **125文件 / 1139项通过** | 面板 mock 虚构 API 方法，未覆盖真实接入缺口。 |
| `npm --prefix frontend run build` | 通过；2.76秒 | 可构建，不证明点击路径正确。 |
| `python -m ruff check backend/app` | All checks passed；exit0 | 仓库高信号规则；不同于学生代码分析器的质量规则。 |
| `python scripts/check_secrets.py` | Secret scan clean | 当前 tracked source 扫描；本轮未暂存新增实现，不能冒充新增文件的提交前扫描。 |
| `python scripts/check_sca.py` | **exit1**；pip_audit 临时环境升级 pip/wheel/setuptools 失败 | 环境/网络依赖检查未完成，不据此声称发现依赖漏洞或门禁通过。 |
| `git diff HEAD --check` | 有 `docs/review-fixes-20261011.md:195` 文件尾空行问题 | 当前 diff 尚不干净。 |
| `node /tmp/dai-accept-api-probe.mjs` | exit0；三个方法均 undefined；exam请求仍 `/judge/...` | 无网络，导入真实前端 API 模块并用内存记录器替代客户端发送。 |
| `env -u DAI_DATABASE_URL DAI_REDIS_URL=redis://127.0.0.1:1/15 <Python> /tmp/dai-accept-final-grading-probe.py` | exit0；复现隐藏expected读取、88→70覆写、dict/bytes codec错误及部分转换 | 临时 SQLite/fakeredis、真实服务函数与合法模拟 AI；ASGI注入学生身份。没有供应商外呼或真实学生代码宿主执行。 |

证据目录：[本轮验收证据](/home/zxk/Projects/dai-experiment-platform/.runtime/review-fixes-20261012/evidence/codex-accept-20261012)。全量和原生组日志含完整命令；前端日志、最小探针和 JUnit 一并保留。

## 审查范围与未验证部分

已审查正常评分终态、代次事务、版本/转换/runner、权限投影、人工及前端接入、考试保存与到点逻辑、Redis/指标、幂等、分析器、迁移测试和容量验收工具。

本轮未重新执行 MySQL、Nginx、真实学生容器、Playwright 浏览器全栈、100/200人数档位、1800秒稳定期、正式后端镜像分析器、生产/GPU/TLS/Registry/备份恢复。旧文档中的这些结果属于此前运行，不能算本轮已验证。已发现阻断问题后，未扩大真实业务写入检查。

验收文档需校正：不同阶段测试数字/完成状态混用；迁移链遗漏实际 head；已实现与“未实施”描述并存；将容量延迟直接归因锁/CPU、据此建议4–7实例缺少锁等待/事务/CPU数据。容量脚本本身 A13 修好前，不能以它作最终通过门禁。

## 清理与 Git 决定

没有确认可删除的已跟踪废弃源码。评分迁移、历史报告、计划及新增测试保留；不对 `.runtime` 或任务目录整体删除，不清业务数据。

本轮仅清理自己创建的临时 Redis 容器及不再需要的临时测试数据库，保留日志、JUnit和最小复现。既有暂存 diff 与开始时完全相同，108个已记录业务/配置/实现文件摘要未变化；只新增本验收报告。

**未执行 git add、commit 或 push。** 用户要求验收通过后才清理和提交，目前不满足。建议先按权限/投影与人工事务、真实前端契约/考试边界、可信版本/runner、验收工具与文档四个小批次修复；补能复现上述缺口的回归，再验收提交。
