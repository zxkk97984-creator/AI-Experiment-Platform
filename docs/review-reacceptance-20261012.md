# 2026-10-12 整改后的第二次独立验收

**结论：仍不通过。** 此次整改已有实质进展，但不能认定 A01–A18 全部关闭。以下反例均在当前实现上确认；本轮未继续修复这些业务缺陷，未提交或推送 Git。

基线：分支 `fix/review-20261011`、HEAD `61182f7`，审查当前已暂存/未暂存/新增实现。用户后续另行授权更换 AI 服务，因此本轮仅额外修改 AI 客户端适配、对应测试和被忽略的本地配置；这与整改验收是否通过分别报告。

## 仍需关闭的问题

| 编号 / 对应原项 | 当前源码依据 | 独立验证与影响 | 最小修复及验收要求 |
| --- | --- | --- | --- |
| R01 / A03，P1 | [judge.py](../backend/app/api/judge.py#L412) 的 AI 在途检查与认领失效操作仅包含 pending/running，遗漏真实队列状态 queued。 | 重新调用真实人工接口和 `process_ai_grade`：人工返回88、CG仍queued；合法模拟AI完成后正式分70，人工审计仍88。上次核心反例仍成立。 | 在检查及收尾中统一覆盖 pending/queued/running，并验证并发认领时的事务一致性。queued 及 running AI 场景都不能在人工确认后覆盖正式分。 |
| R02 / A06，P1，新回归 | [judge.py](../backend/app/api/judge.py#L345) 仅据主判题 grading_status 定义 processing_terminal；[SubmissionView.vue](../frontend/src/views/student/SubmissionView.vue#L20) 与作业页据该字段直接停止轮询。 | 实际 CG=queued、展示status=running、score=null，却返回 processing_terminal=true。active模式确定性判题完成后，AI尚未评分，学生已停止等待并可能显示待复核。 | 终态投影需考虑关联AI状态和人工结论；AI pending/queued/running必须继续轮询，真正复核或正式定分才停止。补确定性完成→AI排队→AI完成的实际接口与UI回归。 |
| R03 / A08，P1 | [judge_worker.py](../backend/app/worker/judge_worker.py#L954) 无绑定且无active版本仍返回pytest，考试同样保留旧路径；新增测试明确期待执行pytest。 | 绑定manual_review的路径已关闭，但未转换题和退役后无active的新提交仍可旧法自动计分，与批准计划的安全降级规则不符。 | 无可证明版本基线的新评分转人工；保留源码和历史分，停止生成旧的不可信新正式分。不能以“历史行为不变”将本项关闭。 |
| R04 / A09，P1 | [ai_grading_service.py](../backend/app/services/ai_grading_service.py#L41) 从payload取cap；实际[版本创建](../backend/app/services/grading_version_service.py#L213)将cap保存于独立列。[数据Worker](../backend/app/worker/judge_worker.py#L823)仍读实时grading_mode且只允许active；AI分值未从父考试映射读取。 | 已调用真实 frozen_grading_facts：版本列中cap=40，读出的cap_rules为空。正常记录的上限会被忽略；题面为空则仍回退实时值，函数名也实时读取；冻结父考试points没有成为AI折算的唯一来源。 | 使用版本实际列及父考试冻结分值，不混用最新题目配置；落实legacy/shadow/active的约定。以真实创建接口生成版本，修改当前题面/mode/cap/points后重验旧提交，不能在测试中手工把cap塞进payload。 |
| R05 / A12，P1 | [runner](../backend/app/services/grading_runner.py#L151)在学生同一进程读取HMAC密钥；清除os.environ不清初始`/proc/self/environ`。[Worker](../backend/app/worker/judge_worker.py#L199)用未认证前缀计数驱动下一输入，事后才验证签名。 | **真实容器**验证：学生能检测到初始环境中的签名密钥；输出一条未签名前缀后，当前函数读到了F1-2的后续输入987654。最终协议被判人工，并未获得自动分，但“密钥不可读/后续输入不可见”的声明仍不成立。另同组计数器预期1/2，实际1/1，功能分30/60，说明每用例重载改变批准的组内有序共享语义。 | 签名/握手控制不得与学生代码共享可读取密钥；实时验证完整帧、当前ID、唯一性后才推进输入。保持组内共享、组间隔离，或将新语义作为显式新规则重新确认，不能冒充旧测试等价转换。加入这三个真实容器反例。 |
| R06 / A13，P1 | [verify_exam_capacity.py](../scripts/verify_exam_capacity.py#L171)在核验HTTP/逐题成功前增加saved_ok；缺失results不记失败；读取非200也未统一记错误。 | 对真实run_load注入纯内存Fake：15次保存均200但results为空、从未保存答案，报告仍passed=true/error_count=0/saved_students=1。新增build_targets单测只测试手工构造的统计参数，未验证HTTP结果如何进入统计。 | 只在唯一、完整、成功的逐题确认后累计成功样本；缺项、重复、无效版本、非200读取及任务异常均失败。对run_load使用MockTransport级回归，空results场景必须退出非零。 |
| R07 / A14，P1 | [verify_env.py](../scripts/verify_env.py#L141)设置DAI_ENV_FILE；[config.py](../backend/app/config.py#L17)仍固定env_file='.env'，get_settings没有读取这个覆盖变量。 | 在独立临时目录放合成.env，指定空DAI_ENV_FILE，实际get_settings仍读取其中的数据库URL。本轮运行选临时cwd并明确注入资源，未访问业务库；但`--cwd backend`的所谓空dotenv保护本身无效。 | 使配置层真正支持显式dotenv路径/关闭dotenv，验证Settings的最终解析值而非仅查看环境变量。所有验证目录、外呼状态和资源地址明确隔离。 |
| R08 / A17，P1 | [build_data_payload_from_generated_groups](../backend/app/services/grading_version_service.py#L472)仍拼接各组源码并生成单F1。 | 已用真实函数输入F60/R10两个合法组：输出仅F60，R维度消失。result.test_groups属性错误已修复，但组权重/边界分仍未修好。 | 按原组保留ID、名称、维度、权重和完整用例；不完整转换需人工。回归真实生成响应schema→版本→计分链路，保证R10不会静默并入F组。 |

以上均为当前代码或实际探针确认。R03/R04的全部业务场景尚未做完整端到端压测，不能将“源码仍不符契约”与“实测已通过”混写。

## 已确认进展及实际检查

- 原学生越权GET考试版本已由200变为404，隐藏expected不再返回。
- 前端真实API模块的方法和kind分派已接入；人工表单、切题状态、冲突队列及管理员前缀有实现与新增回归。
- POST安全投影、append-only审计、Ruff独立规则、dict/bytes codec及输出读取上限均有实质改动；专项通过不代表上表反例已关闭。

Python使用仓库`.runtime/school-security-venv/bin/python`；RUN_ID=`20261012T053319Z-487bbf`，Redis为本轮新建、随机回环端口43445。测试在临时cwd执行，AI外呼关闭，数据库为临时SQLite/fakeredis；仅用户另行授权的连接烟测调用了新供应商，不含学生数据。

| 本轮命令/检查 | 实际结果 | 范围 |
| --- | --- | --- |
| `verify_env.py run <RUN_ID> --cwd temp -- python -m pytest -c <backend/pytest.ini> -q <11个整改专项文件>` | **265 passed / 9 skipped**；exit0；86.77秒 | 权限/人工、data、版本/转换、codec、幂等、容量门禁、分析器和终态等。超长帧替身测试出现2个pump线程异常warning，不计为完整线程验证。 |
| `DAI_RUN_DOCKER_SMOKE=1 DAI_JUDGE_IMAGE=dai-judge-python:latest ... pytest test_data_grading.py -k real_container` | **6 passed / 30 deselected**；exit0；3.78秒 | 实际Docker，但这些原用例没有覆盖上表R05反例。镜像ID前缀dee9344a1a75。 |
| 独立进程 `pytest tests/native_http` | **5 passed**；exit0；15.10秒 | 真实Uvicorn。指标用例仍允许未鉴权401/404，不能据此认定已验证指标读实现；无慢黑洞/并发保存新增场景。 |
| `npm --prefix frontend run lint` | 通过 | 静态检查。 |
| `npm --prefix frontend test` | **127文件 / 1160项通过** | 本轮实际复跑。 |
| `npm --prefix frontend run build` | 通过；2.73秒 | 可构建。 |
| `pytest tests/automated/test_docs_links.py` | **74 passed**；exit0；3.22秒 | 含新报告链接检查；与原生HTTP组分开进程。 |
| `/tmp/dai-reaccept-grading-probe.py` | exit0；queued AI覆盖88→70，active终态投影错误，学生考试版本GET404，dict/bytes成功 | 真实服务函数、临时SQLite、fakeredis与合法模拟AI。 |
| `/tmp/dai-reaccept-config-probe.py` | exit0；空dotenv变量未生效、cap列读取丢失 | 合成.env，无业务数据库连接。 |
| `/tmp/dai-reaccept-runner-probe.py` | exit0；真实容器复现密钥可见、未签名前缀提前投喂、同组有序语义改变 | 所有输入为合成值；未读取宿主机密钥或业务文件。 |
| `/tmp/dai-reaccept-capacity-probe.py` | exit0；空results保存仍passed=true；F/R生成后只剩F | 纯内存客户端，无网络。 |
| `git diff HEAD --check` | 仍报告review-fixes-20261011.md文件尾多空行 | 未据此修改历史文件。 |

对方全量日志中的**2268 passed/67 skipped**与exit0确实存在；本轮未重新执行全量，不将它记为独立复跑结果。MySQL迁移/专项只核对其记录，未在本轮重跑。生产、容量人数档、1800秒稳定期和正式镜像分析器仍不属于本轮通过范围。

本轮证据位于被忽略的`.runtime/review-fixes-20261012/evidence/20261012T053319Z-487bbf/`，保留完整命令、退出码、日志、JUnit和最小探针。

## 用户追加授权：OpenCode Go配置

根据[官方Go文档](https://docs.opencode.ai/docs/go/)，本地基础地址改为`https://opencode.ai/zen/go/v1`，模型`deepseek-v4.1-flash`。密钥仅写入Git忽略的`backend/.env`（权限0600）；原私有配置已备份到被忽略的私有目录。

[ai_client.py](../backend/app/services/ai_client.py)仅对OpenCode Go增加真实客户端User-Agent、每客户端稳定的x-opencode-session，并补oc_sk类错误脱敏；没有改默认供应商或向其他供应商发送会话头。[对应客户端测试](../backend/tests/automated/test_ai_client.py) **20 passed**，Ruff通过。

实际项目客户端用合成Python表达式发一次最小JSON连接烟测：HTTP200，JSON为ok=true/value=2，9.69秒。没有验证真实学生作业的AI合分；应用进程当前未运行，下次本地启动读取新配置。未配置或启动生产Compose服务。

## 交付与Git状态

业务整改验收仍不通过，因此未清理项目文件、未git add/commit/push。保留原暂存内容，仅新增本报告及用户授权的AI客户端适配/测试；清理本轮临时Redis和临时测试数据库，保留验收证据。历史验收报告继续作为当时证据，不覆盖成已通过。
