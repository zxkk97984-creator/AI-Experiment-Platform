# Python 依赖安全门禁

2026-10-08 清除了 2026-09-30 已到期的 Starlette、pyasn1、ecdsa、pytest 临时接受记录。当前接受清单为空，运行依赖审计要求零已知漏洞；旧记录不能用于放行旧镜像或恢复旧依赖。

本次升级 FastAPI 到 0.142.4、Starlette 到 1.7.0、pytest 到 9.1.1，并将 python-jose 替换为 PyJWT 2.15.1。pyasn1 和 ecdsa 不再属于应用运行依赖树。JWT 仍采用配置指定的单一 HMAC 算法，继续读取此前签发的 `sub/type/jti/exp/sv`；数据库会话版本检查和撤销语义保持一致。

运行审计：

```bash
python -m pip_audit -r backend/requirements.txt --format json
python scripts/check_sca.py
```

审计工具必须成功完成；不可解析的结果、工具失败、未修复漏洞均阻断。审计的是当前 requirements 的解析结果，发布时还需对最终镜像的实际安装清单再次审计。单次零漏洞结果只代表执行时数据库已收录的公告。

候选环境与原本地虚拟环境隔离，验证日志保存在 `.runtime/school-verification/security-candidate-*`，不包含用户凭据。SQLite 全量回归及兼容性回归完成后才可使用升级后的依赖发布。
