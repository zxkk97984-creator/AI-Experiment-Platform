# 单校教学功能界面与历史边界验证

本文件分别保留2026-10-08的验收修复补验与此前教学升级验证，数量不跨阶段累加。浏览器库存读取超时后使用仓库已安装的Playwright，未增加浏览器依赖。

## 2026-10-08 验收修复补验

修复管理员章节作业/考试入口及返回路径、授课候选只读首100条、班级Cell配置缺少`.py`上传入口。保留管理员重置密码、排行榜仅展示和工具官网链接/ZIP规则；没有迁移业务数据库。

| 检查 | 本次结果与证据范围 |
|---|---|
| 前端全量 | 键盘焦点修复后`npm run check`通过：ESLint、119个文件/1042项测试、生产构建。日志`/tmp/dai-repair-frontend-check-20261008.log`。 |
| 后端相关回归 | 资料15项、脚本上传与Cell API 49项通过，均为独立SQLite测试库。日志`/tmp/dai-material-navigation-green-backend.log`、`/tmp/dai-check-script-backend.log`；不代表全量MySQL回归。 |
| 既有浏览器fixture | 资料、成绩簿、受众、Cell、授课历史五个文件25项通过。日志`/tmp/dai-repair-fixture-20261008.log`；所有业务API由fixture拦截。 |
| 新增候选浏览器fixture | `offering-candidates.spec.js`的3项通过，覆盖101条候选、205名学生、跨页确认/取消、局部失败重试、学期/实例切换，以及删除已选标签后Escape关闭并将焦点恢复原按钮。日志`/tmp/dai-candidate-playwright.log`，截图位于`/tmp/dai-offering-candidates-playwright/`；和既有25项不重叠，共28项。 |
| 管理员真实导航 | 390/1440视口：第102门课程不在首100条时补取；章节作业、考试进入具体编辑页，刷新后返回原课程及教学班。 |
| 管理员真实候选 | 102门课程、102学期、105教师、207学生的合成库：完整学期、候选第6页、教师精确搜索与取消、学生第1/11页多选及真实提交均通过。 |
| 教师真实脚本上传 | 390/1440视口：上传、显式保存、重新加载通过；共享标准和另一班配置保持不变。真实HTTP确认管理员/本班教师200，其他教师/学生403；20,000/20,001字节、UTF-8及语法边界符合预期，上传不执行脚本。 |
| 页面健康 | 上述三类真实流程身份与内容正确、无Vite错误覆盖层、无console/pageerror或document横向溢出；仅阻断外部字体请求。查看了窄屏分页弹窗和教师上传界面截图。 |
| Docker专项 | 本机`dai-lab-cpu:20261003`开启真实Docker及PyTorch选项，运行专项27项通过。日志`/tmp/dai-repair-docker-20261008.log`；独立于浏览器上传保存验证，不宣称新脚本已在浏览器流程中发布执行。 |

真实联调地址为`http://127.0.0.1:5177`（Vue/Vite）及`http://127.0.0.1:18083`（FastAPI）。后端使用`/tmp/dai-repair-live-20261008/verification.db`独立SQLite和专用临时Redis容器；没有mock业务API，没有使用业务数据库。该环境的health字段名称仍为`mysql`，实际数据库以此处配置为准，不能据此计为MySQL验收。

可复现脚本和日志在`/tmp/dai-repair-live-20261008/`：`navigation.cjs`/`navigation.log`、`candidates.cjs`/`candidates.log`、`upload.cjs`/`upload.log`、`api_checks.py`/`api-checks.log`。截图为`navigation-return-{390,1440}.png`、`students-last-page-{390,1440}.png`、`class-script-{390,1440}.png`。目录含合成测试配置，不随源码交付。修前缺陷复现保留在`/tmp/dai-acceptance-defects-confirmed-20261008.log`；新增单元回归覆盖加载失败重试、旧请求失效、跨页取消及上传迟到隔离。

这里的390px为Chromium视口模拟；本次不宣称完成生产部署、MySQL并发、容量、专用效率节点或学校目标服务器验收。以下为之前阶段的证据，不能当作本次新增测试数量。

## 前次教学升级验证结果

验证日期：2026-10-08；前次fixture页面地址为`http://127.0.0.1:5175`。

| 检查 | 结果与范围 |
|---|---|
| 前端全量 `npm run check` | 资料上限及实验实例导航修改后重新完成：ESLint、116 个测试文件 / 988 个测试、生产 build 均通过 |
| 资料上传上限增量 | 两个组件测试文件 14 / 14 通过；相关 ESLint 与生产 build 通过 |
| 资料、成绩簿、任务受众、Cell 浏览器交互 | 首轮四个文件共 17 / 17 通过；资料上限修改后资料文件 7 / 7 通过，新增一项配置边界交互 |
| 三端授课安排与历史操作浏览器交互 | 7 / 7 通过；管理员、教师、学生均覆盖 390px 和 1440px |
| 历史、任务范围、资料及名单 API 回归 | SQLite 41 / 41 通过，使用 FastAPI TestClient 和独立测试数据库 |
| 新增历史边界回归 | SQLite 14 / 14 通过 |
| 课程发布条件回归 | SQLite 12 / 12 通过；完整课程的测试 setup 改为管理员显式创建授课安排 |
| 资料结构与替换回归 | SQLite 48 / 48 通过；包含35个结构检查和13个资料API测试，PDF fixture另经 pdfinfo核验 |
| 授课安排页面身份、非空、错误覆盖层、console / pageerror | 全部通过；仅排除 fixture 主动中止的外部字体 CDN 请求 |
| 授课安排页面宽度 | 390px 和 1440px 没有 document 横向溢出；表格允许内部横滑 |

后端新边界验证精确断言了：源班或目标班待重评时拒绝转入；`queued`、`running`、`recovering` 实验运行中拒绝转入；模板或评分版本不符、任一班已归档时拒绝转入。每次拒绝均保留原归属、原分数 82.25、原反馈，并且没有写入成功转入审计。21 条实验记录和 21 项历史任务分别以 20 / 1 分页，无重叠、无遗漏。

未归属历史实验同样覆盖三种非终态运行与目标实例待重评：拒绝时原 71.25 分、反馈和 `legacy` 归属保持不变；运行完成或待重评清除后管理员可确认归属，分数仍保留。

## 已验证的界面操作

- 课程资料：学生仅看已发布资料，PDF 预览开启、关闭，本班章节活动链接正确；教师只读共享资料，内容负责人保存文件后发布；服务失败有重试恢复。
- 上传上限：页面使用资料列表 API 的 `ordinary_max_bytes` 和 `large_max_bytes`，按扩展名进行选择后的大小预检。没有取得配置时不使用硬编码上限；超限文件不能触发保存或元数据写入。浏览器验证 1024 字节普通文件上限、2048 字节 ZIP 上限，边界值可选、超出一字节立即阻止；组件覆盖四种大文件扩展名和配置更新后的重新校验。
- 成绩簿：教师、管理员配置两级权重、录分、发布、归档、有原因调分，旧档案保持原分；学生仅看本人官方分项和总评；来源版本变化 409 后保留原因并刷新重审。
- 作业与考试受众：实例任务只显示本班和在册学生，已转出学生与其他班不进入候选；不请求全校学生列表。
- Cell：运行显示通过、代码编辑后反馈过期、本班匿名榜单同分同名次，界面数据来自 fixture；其他状态由组件和后端测试覆盖，实际 Docker 执行另记于总验收证据。
- 授课安排：管理员填写原因后确认归属、明确转入历史实验成绩、确认整项任务；请求携带准确的源实例 ID 和原因。归档班级历史操作禁用；教师、学生没有管理员历史入口且不发起历史核对请求。
- 历史翻页失败后保留已加载页码和数据，可以重试；切换班级时先清除旧名单、旧模板、旧历史，加载失败不会残留上一班资料。

本轮修复 `OfferingsView.vue` 的两处错误恢复行为：翻页状态只有响应成功后更新；切换教学班时清空旧状态。`CourseMaterialsView.vue` 将 API 配置传给 `MaterialEditor.vue`，替换原固定大小提示，并在上传前预检。新增 `school-offerings-history.spec.js`、`MaterialEditor.spec.js` 与 `test_school_history_boundaries.py` 防止回归。

## 证据边界

本文件记录的浏览器 fixture 检查全部 `/api/v1` 请求均由 Playwright 本地 fixture 响应，未访问或修改运行中的业务数据库。首轮24项与后续7项存在复测重叠，不累加为独立总数。它们证明 Vue 页面交互、角色入口、请求内容和响应处理；真实鉴权由 TestClient API 回归覆盖。真实浏览器到 API、独立MySQL、Docker判分及已发布成绩已另行联调，结果见[交付验证](school-release-verification.md)。390px 是 Chromium 视口模拟，并非真实手机或 Safari。

本文件不声称已部署上线，也不声称完成目标服务器 200 在线用户、30 分钟混合负载、TLS、不可变镜像或真实备份恢复验收。

## 复现与本地证据

先在仓库根目录建立被 Git 忽略的证据目录：

```bash
cd /home/zxk/Projects/dai-experiment-platform
mkdir -p .runtime/school-verification
cd frontend
npm run check
npm run dev -- --host 127.0.0.1 --port 5175 --strictPort
```

另开终端执行：

```bash
cd /home/zxk/Projects/dai-experiment-platform/frontend
E2E_BASE_URL=http://127.0.0.1:5175 npx playwright test e2e/school-materials-flow.spec.js e2e/school-gradebook-flow.spec.js e2e/school-audience-scope.spec.js e2e/cell-check-flow.spec.js e2e/school-offerings-history.spec.js --workers=2
cd ..
# 独立验证虚拟环境安装步骤见 school-release-verification.md。
env -u DAI_DATABASE_URL -u DAI_REDIS_URL .runtime/verify-venv/bin/pytest backend/tests/automated/test_school_history_review.py backend/tests/automated/test_school_history_boundaries.py backend/tests/automated/test_school_task_scope_review.py backend/tests/automated/test_course_materials.py backend/tests/automated/test_roster_business_regressions.py backend/tests/automated/test_academic_rosters.py -q
```

验证日志位于 `.runtime/school-verification/{frontend-check,fixture-e2e,offerings-history-e2e,materials-history-backend,history-boundaries}.log`。授课安排三角色截图位于同目录 `offerings-{admin,teacher,student}-{390,1440}.png`，使用虚构身份和业务数据；这些证据文件被 Git 忽略，不作为源代码提交。

资料上限增量证据为同目录 `materials-limit-{frontend,lint,build,e2e}.log`；课程发布 setup 的回归证据为 `course-publish-requirements.log`。服务端上限调整后还需同步 Nginx 请求体限额，实际部署配置验收见上线文档。

文件结构校验兼容范围、拒绝条件与AV/CDR边界见 [课程资料上传校验](course-material-validation.md)，相关证据为 `materials-structure.log`、`materials-structure-unit.log` 和 `minimal-pdf{,-stream}-info.log`。
