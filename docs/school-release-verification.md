# 单校教学升级交付验证

验证日期：2026-10-08。所有操作使用合成身份、可丢弃数据库、私有验收文件与专用容器。未迁移现有学校/开发业务数据库，未向生产发布。

## 同日验收修复补验

后续已修复管理员章节作业/考试跳转及返回、授课候选首100条限制，并补齐本班Cell规则`.py`上传。最终前端119个文件/1042项测试、ESLint和构建通过；本次相关后端64项及Docker运行专项27项通过。390/1440的真实浏览器到FastAPI、独立SQLite、专用Redis联调覆盖三类修复流程，另有28项API模拟浏览器回归通过。详情与日志见[界面验证的验收修复补验](school-ui-verification.md#2026-10-08-验收修复补验)。

此处为增量修复结果，未重新运行下方全量MySQL历史集合；不将SQLite联调标成MySQL证据，也不将上传保存等同于脚本发布执行。下方原阶段测试数量和生产验收边界保留。

## 已取得的证据

| 验证 | 已确认结果与边界 |
|---|---|
| 前端全量 | 116个文件、988项测试，以及ESLint和生产构建通过。 |
| 浏览器fixture | 三端资料/成绩簿/受众/Cell/历史操作共24项；后续动态上传上限7项通过，部分复测有重叠，不累加成独立总数。 |
| 真实三端联调 | Chromium390/1440连接FastAPI、独立MySQL和Docker：资料、Cell检查、正式提交、教师班级范围、配置/录分/发布/归档/Excel导出、学生本人明细、管理员历史入口全部通过，页面无console/pageerror或document横向溢出。 |
| 正式分与版本 | 干净Notebook重放100分；两级总评80×30%+100×40%+80×30%=88.00；此前归档80.00保留不变。 |
| 真实Docker | CPU实验27项，Cell证据与伪造防护、PyTorch执行等通过；没有GPU硬件验收或生产Registry推送/回拉证据。 |
| 历史迁移 | 真实历史DDL到当前head，SQLite及独立MySQL合计7项通过；旧用户hash、会话/凭据版本、提交/成绩ID和分数保留，旧绑定仅pending，生产拒绝降级。 |
| 成绩并发 | 真实MySQL11项：CAS两发布只一成功，预览后转班/撤权拒绝旧发布，current配置/录分/调分，正式提交与发布两种抢锁顺序，旧档案保持不变。 |
| 账号并发 | 独立MySQL3项通过，包含保留最后一个管理员、批次只应用一次及并发身份变更冲突。 |
| 授权与共享发布并发 | 独立MySQL授课撤权9项及共享发布6项通过：REPEATABLE READ旧快照在等待锁后不能继续使用撤销的账号/授课/名单/课程/学期/班级或共享发布、模板、评测；晚到记录和当前实例版本也不能被覆盖。 |
| 真实临时凭据 | HTTP与独立MySQL：学号`00099`前导零保留，导出的随机密码长度16且可登录；首次登录必须改密，改密后旧凭据导出失效。仅合成验收身份，没有保存明文到报告或日志。 |
| 资料结构 | 48项格式与API检查通过；拒绝畸形替换并保留旧文件。PDF/ZIP仅有界结构检查，未接入AV/CDR。 |
| 真实资料链路 | 最新API与独立MySQL：PDF草稿不可读、发布后浏览器打开预览、Range返回206及准确字节，缓存为private/no-store；畸形替换被拒绝且旧文件/地址保持可用，归档后旧地址失效。合成PDF为空白单页，不代表复杂课件视觉兼容验收。 |
| 功能容量smoke | 真实API2人、10秒、75次只读请求，p95上界29ms，无错误；脚本明确输出完整容量证据不足，不代表200人30分钟混合验收。 |
| 静态部署配置 | Nginx真实容器`nginx -t`、生产Compose配置展开通过；Compose使用disposable配置值，不代表生产镜像/秘密配置已批准。 |

运行依赖已更新：FastAPI/Starlette、PyJWT及前端Axios/DOMPurify/Vue；Python运行依赖和开发依赖、前端运行依赖审计均未报告已知漏洞，接受清单为空。Ruff、`pip check`及`git diff --check`通过；此结论以本轮审计时点为准，不代表今后没有新披露漏洞。

## 全量结果与复现

独立候选环境完整SQLite执行：1799通过、39跳过、0失败（1838项快照）。后续新增凭据再次验证、导出表头、实验实例列表、共享模块及发布当前读回归分别在SQLite/MySQL补测通过；最后发布相关SQLite集为28通过、6专用MySQL跳过。不能把这些重叠复验简单累加为另一轮完整SQLite运行。

MySQL使用4个独立可丢弃库分组，初始1764项覆盖互不重叠：1725通过、30跳过、9失败。失败涉及旧文档链接、框架路由统计、ONLY_FULL_GROUP_BY列表查询、CHECK错误类型、容量脚本停止时序及种子随机数依赖自增ID；均已定位并修复，整文件复验和后续增量结果按去重覆盖记录。中断的旧MySQL日志不计为通过证据。

最终源码收集1866项、164个文件，普通MySQL测试环境的完整分组、失败修复复验与新增用例按节点去重后：1821通过、45按条件跳过、0未解决失败、0遗漏。45项为15个Docker开关用例及30个专用MySQL用例；后者的账号3项、成绩11项、迁移1项、授课范围9项和共享发布6项，均在对应独立MySQL库另行实际通过。专项实跑与普通集合的跳过分别保留，不能重复相加；Docker27项实际执行与通用开关用例也不混计。

这不是单次1866项从头执行全部通过的声明。逐节点证据位于`mysql-final-coverage.json`，汇总为`mysql-final-summary.json`，保留最初失败及后续整文件复验日志。MySQL 8.4.11保持`REPEATABLE READ`与`ONLY_FULL_GROUP_BY`；可丢弃实例为加速DDL设置`innodb_flush_log_at_trx_commit=2`、`sync_binlog=0`，仅证明功能与并发正确性，不构成生产持久性或容量证据。

通用复现命令（仅独立测试库，fixture会创建和删除表）：

```bash
# 从仓库根建立独立验证环境，保留旧开发虚拟环境。
python3 -m venv .runtime/verify-venv
.runtime/verify-venv/bin/python -m pip install -r backend/requirements-dev.txt pip-audit
env -u DAI_DATABASE_URL -u DAI_REDIS_URL .runtime/verify-venv/bin/pytest -q backend/tests/automated
.runtime/verify-venv/bin/ruff check backend/app
cd frontend
npm run check
npm audit --omit=dev --audit-level=high --registry=https://registry.npmjs.org
cd ..
.runtime/verify-venv/bin/python scripts/check_sca.py
```

当前源码要求`backend/requirements-dev.txt`中的新依赖；原开发虚拟环境没有被原地修改。本轮独立环境为`.runtime/school-security-venv`。本机保留的验证产物与完整日志在`.runtime/school-verification`，该目录被Git忽略，不发布凭据、数据库、截图或中间脚本。

本地开发可安装当前依赖到自己的虚拟环境，或使用`DAI_PYTHON`指定已更新的环境。`scripts/dev-up.sh`会在启动数据库和迁移之前检查PyJWT，旧环境缺依赖会明确停止。实际业务库迁移仍须先备份并核对授课候选；本轮联调用的API、Vite和MySQL容器已停止，不占用学校或开发服务端口。

## 上线仍需部署方的证据

真实TLS/证书续期、可信代理、不可变生产镜像build/push/pull-back、真实数据加密备份恢复与RPO/RTO、专用效率基准节点的硬件/挂载一致性、200名独立学生30分钟混合负载与队列/恢复/磁盘告警，均未在学校目标机器验收。

请先按[操作与迁移说明](school-teaching-upgrade.md)完成试点准备，再按[生产证据清单](production-evidence-checklist.md)记录实际环境证据。运行中的读取/入队/评分、已发布成绩和物理硬件容量分别验收；默认效率权重保持零，未配置专用节点不能启用。
