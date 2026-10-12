# 修复、优化与验收收尾计划——修订版

## 1. 基线、目标与边界

本计划可完整替换上传计划。核对时分支为 fix/review-20261011，HEAD 为 61182f7，已有代次修复处于工作树/暂存区。开始实施时重新记录 HEAD、暂存/未暂存 diff、未跟踪文件和源文件摘要，不将历史文件数量当作固定事实。

保留 Vue/FastAPI/MySQL/Redis 架构，以及功能60＋边界10＋AI算法20＋AI代码质量10的评分方式。legacy/shadow/active 仍是成绩采用策略；data_v1/manual_review 是独立的执行方式。

现有数据库是否仅含可丢弃测试数据尚未验证，默认完整保留，不清库、不重写历史成绩、不改成绩簿快照。首先在独立空库验证迁移和演示数据，再通过明确的预检/确认处理现有内容。实际启用前核查进行中考试和存量任务，不在实施时自行假定不存在教学影响。不 reset/clean、不自动提交、推送或部署。

原计划引用的临时探针和测试计数仅作为历史证据。关键场景必须固化为仓库测试，不能依赖 /tmp 脚本仍然存在，也不能沿用旧通过数字。

资源约定：运行ID唯一；临时容器、Compose项目、卷与端口均独立，绑定127.0.0.1；启动器发现实际端口并传给子进程，不固定16319，不复用共享Redis，不执行flush。每项检查记录命令、环境、退出码和实际结果，仅清理本轮创建的资源。

当前执行沙箱读取Docker daemon会返回权限不足；宿主机有Docker不等于当前工具可访问。实施时先确认工具访问，再用自己的一次性容器验证绑定文件、退出和清理。访问失败时继续不依赖Docker的工作，将真实运行组列为待验，不能虚报通过。

## 2. 批次一：评分收尾事务、认领保护及回归门禁

### 2.1 AI一次性终态提交

- 正常合法AI结果写 CodeGrade.status=completed；需人工复核写 review_required。
- 评分字段、完成时间、认领释放及 active 模式的关联正式分，在同一受当前token保护的事务中提交。关联目标不存在或写入失败，整个事务回滚。
- process_ai_grade 不再执行第二次 complete_ai_grade，按已提交终态推进考试汇总。
- 用专用 LostClaimError 表示资格失效，Worker直接停止；其他异常仍走有界错误处理，不将所有RuntimeError吞成失去认领。
- 正确验收状态为 CodeGrade=completed、ExamSubmission=graded；ExamSubmission没有completed状态。需复核时父考试为review_required。

### 2.2 判题结果与后续评分记录原子提交

- 在评分服务提供专用收尾操作；底层认领保护写入提供不自行提交的事务内路径，保留原独立调用门面。
- 同一事务执行 Submission/ExamAnswer 的token条件UPDATE、建立或受控更新对应CodeGrade、写来源证据，再commit。
- 资格失效立即rollback并停止，不创建CG、不推送AI、不推进考试。
- 成功返回实际评分记录ID及终态结果；唯一约束或CG写入失败必须回滚主结果。
- CG来源证据包含评分版本、结果修订号和原判题认领标识。不得无条件复用另一代次的CG；更新已有CG仅允许显式受控重评且不存在在途AI，保存原证据与审计，否则返回冲突。
- commit后通知AI队列，通知丢失依靠数据库pending/queued恢复，不承诺Redis消息恰好一次。
- 恢复扫描UPDATE同时包含token及实时过期谓词；判题按heartbeat/started_at，AI按started_at，使用NULL安全比较。SELECT后续期不得被旧扫描重置。
- 判题、AI终态、人工改分、受控重评原子推进结果修订号；手动操作与重试必须检查状态/版本，并使旧认领失效。

### 2.3 收口已知回归

- phase5测试不再把claim_job mock成True后期待真实写入。使用真实认领与真实数据库验证状态，Docker调用仍可替身；纯mock用例明确断言实际写入接口。
- stale测试引用实际阈值，并设置明确过去的started_at/heartbeat_at，避免MySQL时间精度造成0秒边界偶发失败。
- 保留历史单个迁移的语义单测。另建历史schema正确stamp后通过Alembic command.upgrade到head的收敛检查，不硬编码仅调用b7迁移；后续新增版本也自动覆盖。
- 验收：正常/需复核AI、关联更新失败、旧任务被拒后不建CG、CG创建失败整体回滚、通知失败恢复、续期与扫描交错、教师覆盖与旧Worker交错。
- 本批及修复前7条失败专项通过，才扩展评分协议；不得降低原业务断言。

## 3. 批次二：考试交互与角色路由

- 活动期间syncSession仅合并截止时间、状态、公开策略及服务器时间，冻结题目内容，不重置本地答案、版本、当前题和pending。
- 同步请求single-flight；响应绑定attempt ID及页面generation。已看到终态的同一attempt不能被迟到started响应复活；终态按服务器visibility更新最终展示。
- 新增小范围答案保存队列。flushPending直接返回同一个在途Promise；快照含逐题editSeq，ack版本只能增加，只清除与发送序号相同的pending，新编辑继续排下一批。
- 批量HTTP200也逐题检查ok/code/version；缺失或重复结果视为失败。失败保留草稿并停止本次drain，冲突题停止自动重试，不影响其他可保存题。
- 冲突展示本地与服务器答案：明确采用服务器，或确认使用本地并以最新已读取版本提交；再次冲突重新核对，不能自动覆盖。
- online先同步会话再保存；到零立即锁编辑并GET会话。服务器延时则恢复，已收卷则显示终态，离线则保留草稿等待确认，不依据旧截止POST自动交卷。
- 主动交卷先等待保存；失败不POST、不清草稿。交卷响应未知时先查会话再决定重试。离开页面保存失败不得通过finally无条件跳转。
- 题号nav设置min-height:0与内部纵向滚动；计时、保存状态、交卷按钮不收缩。覆盖首屏及滚动后，6/40/100题、1366×768、1440×900、390宽。
- 增加管理员考试管理/成绩列表/详情路由，复用现有页面；返回、编辑及AI详情链接按已鉴权角色计算固定前缀，保留服务端授权。
- 将上次浏览器失败场景固化回归，并补多标签、保存期间连续编辑、终态迟到响应及离线交卷。single-flight回归须断言最多一个在途请求、后续编辑最终保存，不沿用等待两个并发请求才能结束的旧复现脚本。

## 4. 批次三：可信判题、评分版本及人工闭环

该批范围最大，分为五个可独立验证的小步骤：3A值编码/比较器/受限runner；3B版本模型、绑定及迁移；3C编辑器、生成与转换预检；3D人工评分与安全结果投影；3E独立演示库及作业/考试端到端启用。3A与3B可并行只读设计或分文件实现，其余按依赖推进。各步骤完成自己的专项测试；3A～3D未全部通过时，不启用新的自动正式评分。

### 4.1 评分版本和绑定

新增不可变judge_grading_versions：

- judge_question_id或exam_question_id严格二选一；分别对题目标识＋version设置唯一约束。
- 保存execution_mode、grading_mode_snapshot、entry_function、完整payload、比较规则、score_cap_rules、rubric_id、source_digest、spec_digest、作者及确认记录；评分上下文快照包含题干、函数签名、teacher_constraints等会影响AI评审的字段。
- draft可编辑，确认后payload不可修改；active用于新绑定，retired仅禁止新绑定，不使历史绑定失效。编辑已确认版本必须创建新版。
- 两类题目增加current_grading_version_id；Submission、ExamAnswer增加可空grading_version_id。
- ExamSubmission开始时冻结逐代码题的版本ID及题目分值；创建、补建答案只能从父快照复制。考试代码题入口必须明确保存，禁止从题干猜函数。
- Worker只读取绑定版本中的mode、比较规则、cap、评分上下文及Rubric，环境和import策略继续使用已有快照；不得混用最新题目配置。
- 旧会话无法恢复当时基线时，保持答题/交卷可用，缺基线的后续评分转人工，不悄悄绑定最新版本。

payload保留两种结构：

    schema_version、entry_function、execution_model
    binary_cases
    groups[{id,name,dimension,max_score,cases}]

- legacy：可信binary_cases按原二元规则计0/100。
- shadow：正式分来自可信binary_cases，可信F/R及AI结果用于影子评分。
- active：可信F/R组满分60/10，AI算法/质量满分20/10，最终采用现有公式。
- legacy只是成绩策略，不允许继续信任原pytest输出生成新自动正式成绩。

### 4.2 受限数据协议与可信比较

- 使用带类型的版本化值编码，保存tuple/list及dict键等必要语义；原==转换使用Python字面量相等语义，is True/False/None采用独立比较规则，不静默替换为==。
- 新数据题支持有限普通值、显式abs_tol/rel_tol浮点比较、bool/int/float密集NumPy数组及有限内置异常名单；拒绝非有限浮点、自定义对象、生成器、对象dtype及任意反序列化。
- 新数组规则默认形状精确匹配；旧np.allclose的广播、dtype或状态顺序无法证明等价时显示语义差异，需教师确认新规则，不能标自动等价。
- 第一版拟定上限：每题256用例、32组，值深度16，每次输入和结果各64KiB，数组4096元素，完整规范1MiB。这些是新增设计默认值，并非仓库已有约束；先对独立题库验证兼容性，将超限内容明确标为需调整/人工。API、Worker读取及容器结果校验均落实最终确定的上限。
- 容器只获得学生代码、函数入口及当前输入，不获得expected、分值、计数、权重、隐藏源代码或后续用例。
- 第一版组内按顺序共享模块实例、组间隔离；每个输入由可信侧指定请求/用例ID。平台比较actual与expected并计算组计数，不接收学生声明的passed或score。
- 协议使用专用结果通道，与普通stdout/stderr分开；仍将每帧视为不可信输入，验证当前ID、类型、唯一性及大小。
- Worker在读取阶段有界收集：单结果64KiB、每组诊断合计256KiB、整题诊断合计1MiB。超限即停止对应容器并有界清理，禁止capture_output无界读取后再截断。
- 错误返回仅以固定原因码和受限异常类型参与判断；私有输入、expected/actual、原始异常消息及traceback仅教师可见。
- 普通错误答案和支持类型的非预期异常按规则计失败；明确学生超时保持time_limit_exceeded。平台不可用可有界重试，耗尽转人工；不支持配置、结果协议破坏或输出洪泛直接转人工且不计0分，不能无限退还额度重试。

### 4.3 编辑器、AI生成、转换和启用

- 更新作业/考试题目及AI组schema为带格式判别的数据用例；保留组ID、名称、维度和各组max_score，不将data塞入tests字符串。
- AI直接生成数据草稿，经schema和参考实现预检后回填，不能自动保存或启用。参考代码仅在受限Docker内执行，不能在Worker宿主执行。
- 新数据生成与原pytest转换是不同验证：新生成不承诺与旧测试等价；转换必须完整解析所有相关测试，不允许只转换部分后删除剩余内容。
- AST转换允许必要的import user_code及from user_code import明确函数；支持字面量参数、单断言及已实现比较。其余fixture、循环、参数化、动态期望值等转人工。不用eval/exec或运行原源码推导答案。
- 统一作业和考试接口：

    POST /ai-grading/questions/{kind}/{question_id}/grading-version/preflight
    POST /ai-grading/questions/{kind}/{question_id}/grading-version/apply

- preflight返回完整候选、源码摘要、规范摘要、逐组转换结果及未支持位置；不修改正式配置。
- apply重新读取并核验摘要，变化返回409；重算/校验候选并留下教师确认和审计。修改候选属于显式新规则，不冒充自动等价。
- 两类接口统一遵守内容权限和原有发布/已提交不可编辑规则。不可原地修改的发布内容复制为新草稿转换，不借AI配置或转换接口修改进行中考试。
- 启用条件：已确认数据版本，F/R权重正确，需要AI的模式绑定匹配Rubric。未转换旧题后续提交进入manual_review；原pytest仅供查看、转换及教师复核。
- 更新独立演示库的基础和NumPy题库为真实可检查用例，去掉assert True凑分；已开始的旧测试会话无版本基线则人工，不伪造旧绑定。

### 4.4 人工评分和学生安全结果

- 新增Submission人工字段、结果修订号、复核原因和append-only评分审计；ExamAnswer及相关CG也有明确结果修订号。
- 新增PATCH /judge/submissions/{id}/manual-score：有限0～100分、3～1000字理由、expected_result_revision；条件UPDATE校验版本及教学权限。存在在途判题/AI时拒写，受控取消/转人工后方可评分。
- 作业现有提交详情增加人工评分表单、原因和审计，不伪造Rubric或CG；考试复用现有逐题界面，覆盖无答案行。
- 人工待处理使用review_required、score=NULL，自动重试停止；人工确认后Submission.status=graded、Submission.grading_status=completed，ExamAnswer.grading_status=completed；考试全部题可评分后ExamSubmission.status=graded。存在关联CG时，人工覆写使其进入completed并记录manual来源和审计，避免阻塞父级汇总，但不得制造AI分项。
- 无可信F/R时deterministic_status=unavailable，可信runner成功形成证据才为verified；不能把缺失当0合分。AI override需显式final_score_100；retry/regrade不得把manual_review送回旧自动路径或复用不可信F/R。
- result_revision由判题、AI、人工和重评所有结果写入原子推进。现有改分客户端同步携带版本，冲突保留表单并重读。
- 学生结果区分processing_terminal与score_finalized：待人工可停止轮询，但不能展示为已评分。增加grading_source与固定安全原因，不制造AI分项。
- 安全投影覆盖正式提交响应、列表、详情、历史、统一提交及错误响应；AI只获得可信聚合证据。学生不接收隐藏输入/expected/actual及原始stdout/stderr/result_details，教师仍按权限诊断；公开样例保留详细反馈。

## 5. 批次四：Redis、幂等、上传与分析器

### Redis与指标

- 公共API Redis工厂覆盖lifespan、依赖惰性初始化、ready fallback：连接/读取各0.5秒，0次自动重试，连接池上限32；阻塞队列Worker客户端保留独立策略。
- 限流、登录refresh写入、刷新GETDEL/SETEX、退出DELETE、鉴权及写入前EXISTS统一Redis故障为503 SERVICE_UNAVAILABLE，不能当令牌不存在或放行。
- GETDEL结果未知不重放，提示重新登录；不承诺网络故障时旧refresh token仍可使用。
- HTTP中间件只更新固定白名单计数槽；每进程一个聚合器、一个每秒flush任务、最多一个flush在途，Redis pipeline连接/读取各0.1秒、0重试。
- flush失败丢弃该批并记丢失计数，日志限频。关闭最多1秒best-effort flush，不新增队列、落盘或重放。无lifespan的单元测试注入fake/no-op聚合器。

### 作业提交幂等

- Submission新增可空client_request_id(UUID字符串)、request_digest，唯一范围为student_id＋client_request_id；请求键可选。
- SHA-256摘要使用固定v1规范JSON，包含question_id和原样code，不归一化代码空白/换行，不包含可变环境或截止配置。
- 先鉴权与本人访问检查，再查既有幂等结果，之后才检查新请求次数/截止；锁内二次检查、次数计算和插入同事务。
- 同键同正文返回原ID及安全当前结果，保持201；同键不同正文409 IDEMPOTENCY_KEY_REUSED；不同学生同键独立，新键同代码为新尝试。
- 仅捕获对应唯一约束冲突并读取winner；其他数据库错误正常失败。省略键的旧客户端保持原行为，但不承诺重试幂等。
- 前端限定作业提交helper保存UUID及不可变请求快照；结果未知时重放原请求，新尝试才换键。axios不全局生成键，不改变已有实验的record级幂等策略。

### 上传与静态分析

- 仅作业/考试audience/import精确路径放宽请求到3MiB；API只读2MiB＋1字节后校验，其他普通请求仍1MiB。
- Nginx＋stub验证请求能到upstream及代理上限；真实API另验业务2MiB限制、权限与无副作用，stub204不能证明导入成功。
- Ruff 0.12.5、Radon 6.0.1移入后端运行依赖；在正式后端/AI Worker镜像执行真实analyze_python验证F401及复杂度，不向学生判题镜像无理由添加这些工具。
- 分析结果保留旧字段，增加工具状态ok/unavailable/timeout/invalid_result/error/not_run。语法错误与平台依赖失败分开。
- 必需分析器故障不调用外部AI，不生成自动正式总分；通过代次保护转review_required，保留已有成绩及可信测试证据。工具恢复后显式重试。

## 6. 批次五：原生HTTP、容量及最终验收

- 新增backend/tests/native_http，使用独立fixture和独立pytest进程；不导入automated conftest，不修改AnyIO线程行为。pytest.ini marker仅作分类，CI显式运行该目录，不能只加marker后遗漏收集。
- Uvicorn子进程临时cwd、显式PYTHONPATH、专用DB/Redis/日志/媒体/工作目录，AI外呼关闭；有界启动/关闭，仅终止自己创建的进程。
- 单元测试隔离.env及DAI_TEST_*；仅env -u DAI_DATABASE_URL再进入backend目录不构成完整隔离。每测试SQLite独立；MySQL使用专用空schema，不指向本地现有业务库。
- native验证真实生命周期、到期扫描、慢Redis503、指标不阻塞、并发答案保存与交卷、正常关闭。
- 容量脚本使用独立学生及会话，30/100/200人逐档ramp60秒、稳定至少300秒，200人再跑1800秒；每人最多一保存在途，按服务器版本推进，最后逐人核对答案。
- 读取P95≤1秒、保存P95≤2秒、P99≤5秒为暂定本地基线；429/409/5xx/超时全部报告，不从分母剔除。目标机容量不由本地通过代签。
- 全局策略锁先测量，无瓶颈保留；需要缩小锁范围时另批测试关闭学期、撤销权限与保存交错，不用耗时单一指标删锁。

最终检查分组运行：

1. automated全量SQLite与独立MySQL；新增评分事务专项；本机可用现有xdist4进程，CI没有该依赖时串行，不能把缺插件误报源码失败。
2. 迁移单head、空库bootstrap到head、历史schema正确stamp后upgrade head、alembic check及历史数据不变；单迁移升降级只在可丢弃库演练。
3. native_http独立进程运行，与automated全量分开。
4. mock浏览器组使用独立前端及完整API拦截，未匹配API直接失败；新增考试验收回归、exam-active、布局组明确封闭网络。
5. exam-flow等真实登录/开始/交卷用例使用本轮独立Compose项目、空库和专用seed；不得称全mock，也不运行到现有开发服务。
6. 真实Docker验证数据runner及输出上限，后端镜像验证分析器；真实Nginx验证上传。
7. 前端lint/Vitest/build、Ruff、secret scan、暂存及未暂存diff检查，独立复审评分收尾和信任边界。

命令约定：

    npm --prefix frontend run lint
    npm --prefix frontend test
    npm --prefix frontend run build
    cd frontend
    E2E_BASE_URL=<隔离mock前端> npx playwright test exam-acceptance-regressions.spec.js exam-active-ui.spec.js layout-regression.spec.js
    E2E_BASE_URL=<隔离全栈> npx playwright test exam-flow.spec.js

pytest通过统一隔离启动器调用，实际日志记录Python绝对路径、目录、测试路径及参数，不硬编码临时服务URL。修复前复现须转为失败回归，修复后通过；不重复跑无代码变更且无未解决疑点的检查。

常规AI验收使用合法模拟响应；真实供应商调用是独立验收，不擅自启用或外呼。跳过、不支持和权限阻断单独列出，不计通过。

## 7. 迁移、兼容与安全降级

- 不覆盖现有b7代次迁移。后续评分版本、人工字段、请求键迁移按实际单head串行续接，不预先假定所有新增列都可空。
- confirmed版本表有非空业务字段；历史关联字段可空。结构迁移只建表/列/索引，不批量转换测试、不重评或入队。
- 支持旧题源码读取和旧客户端省略请求键，不承诺继续旧的不可信自动评分。已有历史成绩及评分证据保留。
- 启用data_v1前需完成编辑、发布守卫、版本绑定、转换、人工和学生安全响应的整个闭环，不仅开一个runner开关。
- 故障降级停止新的data自动任务并切manual_review，保留输入、版本、认领和审计证据；代码只能回退到仍尊重新schema及人工规则的版本，不能恢复旧pytest计分或隐藏日志泄露。
- 有新版本/请求键/审计数据时不自动执行downgrade -1；优先前向修复。可丢弃库能downgrade不等于真实环境可安全回退。
- 不自动改历史成绩；显式重评另行预览目标、选择评分版本并保存原证据，最后由教师确认，不把版本启用串成历史重算。

## 8. 交付与通过标准

更新docs/review-fixes-20261012.md及架构、手动测试说明；证据放忽略的.runtime/review-fixes-20261012目录。每项记录原任务编号、当前状态、实际命令和结果、数据迁移/启用步骤、未验证边界及降级方式。

代码完成、专项通过、真实运行通过、生产环境通过分别报告。两处评分回归、原11项及仓库验证均收口后才能宣称本轮完成；TLS、生产Registry、GPU、备份恢复等仍按外部门禁记录，不因使用独立测试库而虚报。

本修订计划由源码和上传文本的只读检查形成。生成该文件不表示任何修复已实施，也不产生新的测试通过证据。
