# Production evidence checklist

本清单把仓库内可复现的代码/CI 证据与部署方必须在真实环境提供的证据分开。
仓库测试、disposable Compose smoke 或占位 digest **不能替代**生产证据；未勾选的外部门禁保持 NO-GO。

单校教学升级的功能、迁移和试点步骤见[教学升级操作](school-teaching-upgrade.md)，本轮本机验证范围见[交付验证](school-release-verification.md)。200名独立学生、30分钟混合负载的准备和运行方法见[容量验收](school-capacity.md)，本地只读smoke不计入该门禁。

## 仓库内可复现证据

- [x] 空库 bootstrap 使用统一的两阶段入口：迁移到 `b4c5d6e7f890`、写入真实 basic 环境、再迁移到 head。
- [x] production seed 对缺失、占位或格式错误的 `DAI_BASIC_ENVIRONMENT_IMAGE_DIGEST` fail-closed。
- [x] 运行镜像只安装 `backend/requirements.txt`；测试/质量工具在 `requirements-dev.txt`。
- [x] 确定性 Judge/Exam Worker 与可选 AI Worker 使用互斥队列；AI profile 默认不启动。
- [x] CI、前端测试、Compose 配置和 disposable smoke 结果由对应 CI job/本地命令记录。

## 语义边界与验证命令

| 语义 | 可以证明什么 | 不能证明什么 |
| --- | --- | --- |
| 本地开发 | 代码、SQLite/本地依赖和开发流程可运行 | 生产主机、Registry、TLS、容量或备份恢复 |
| CI | 固定 runner 上的 pytest/Ruff、前端门禁和 disposable smoke | 真实生产数据、真实密钥和外部审批 |
| disposable smoke | Compose wiring、空库 bootstrap、健康检查和安全头 | 生产镜像推送/回拉、真实外部服务或容量 |
| production | 只有部署方证据完整且签字后才可标记通过 | 不能由本清单或受限开发环境代签 |

仓库内验证入口：

```bash
cd backend
.venv/bin/python -m pytest -q
.venv/bin/ruff check app/
cd ../frontend
npm run lint
npm test
npm run build
npm audit --omit=dev --audit-level=high
```

Compose 配置检查必须使用部署方批准的真实环境文件；不要把 `.env`、Registry Secret 或生产凭据写入仓库，也不要用假 digest 代替配置：

```bash
docker compose --env-file <approved-production-env-file> \
  -f docker-compose.prod.yml config --quiet
```

## 外部生产门禁（部署方填写）

以下条目由责任方在真实部署环境填写证据路径、日期和签字；当前均未由本次仓库修改验证，属于待部署方门禁。

### 备份恢复 — NO-GO

- [ ] 责任方：运维/DBA；证据路径：待回填；阻断条件：没有加密备份、校验和、隔离恢复日志、RPO/RTO 和签字。
- 参考：[`docs/backup-restore.md`](backup-restore.md)

### TLS/证书 — NO-GO

- [ ] 责任方：部署方/网关负责人；证据路径：待回填；阻断条件：TLS 终止位置、HSTS、证书续期和告警未在真实边界确认。
- 参考：[`docs/security/tls-topology.md`](security/tls-topology.md)

### Docker 主机与 Socket — NO-GO

- [ ] 责任方：运维；证据路径：待回填；阻断条件：专机、Socket 权限、2375/2376 未监听和风险接受未现场验证。
- 参考：[`docs/security/docker-socket-isolation.md`](security/docker-socket-isolation.md)

### Registry 与环境镜像 — NO-GO

- [ ] 责任方：发布/平台运维；证据路径：待回填；阻断条件：3.10/3.11/3.12 的真实 digest build、push、pull-back 和运行 smoke 不完整。
- 生产 `DAI_BASIC_ENVIRONMENT_IMAGE_DIGEST` 必须来自已验证产物；禁止 `000...`、`111...` 或 disposable 值。
- 参考：[`docs/environment-profiles.md`](environment-profiles.md)

### 容量与告警 — NO-GO

- [ ] 责任方：运维/性能负责人；证据路径：待回填；阻断条件：目标并发阶梯、Kernel 准入、队列/内存/磁盘阈值和告警未实测。

### AI 数据治理 — 未批准则保持关闭

- [ ] 责任方：信息安全/业务负责人；证据路径：待回填；阻断条件：治理审批、供应商/模型、数据流和责任签字未完成。
- 在审批证据完成前必须保持 `DAI_AI_ENABLED=false`，不要启动 Compose 的 `ai` profile。
- 参考：[`docs/ai-data-governance.md`](ai-data-governance.md)
