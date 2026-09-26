# 账号、名单导入与密码管理

## 使用流程

管理员进入「教务管理」建立学期，再到「名单与批次」下载教学班模板、上传并预检，确认后等待后台完成。
学生模板以学号和姓名识别学生，院系可选；多班导入先选择学期，未知班级需先创建。班级编码为空时只建账号。
同一学生在多个班仅创建一个账号。任何冲突阻断整批；重复导入不覆盖旧密码、不移除名单中缺席者。
模板的学号和班级编码必须保持文本格式。原始上传文件不会长期保存。

新学生使用学号和系统临时密码登录；首次只开放改密页。初始默认值为 `tzcaicode`，有效期7天。
管理员可以在「账号安全与审计」更改默认密码或1～30天有效期，不会自动更改已有账号。
学生自己设置密码后重新登录；旧Access、Refresh和签名媒体地址失效。
默认密码不在管理接口中回显。教职工账号不使用共享学生默认密码。

用户列表可勾选当前页学生或预览全部筛选结果；整班重置在「名单与批次」中操作。
确认前核对固定人员、填写原因和管理员当前密码。禁用/考试中的学生跳过。
批次显示准备状态和逐人结果；网络超时后打开原批次查询，不要盲目重复建任务。
已成功的重置不会重放；学生在等待中自己改密或管理员确认新重置后，旧任务会跳过版本冲突。

班级归档、学期关闭只冻结教务关系；作业考试按各自期限继续，教师可继续批改。
转班在一次事务中完成。人工授权优先，不会因班级同步被覆盖。
学生「学习记录」展示本人历史提交及公开成绩，退班后保留，不能继续访问原课程或补交。

## 部署

1. 备份数据库、Storage文件和独立加密密钥；先在隔离副本迁移、验证恢复。
2. 在后端虚拟环境安装更新后的 `requirements.txt`。
3. 生成独立Fernet密钥，设置 `DAI_ACCOUNT_SECURITY_KEYS`，不要使用JWT密钥，也不要提交到Git。
   本地写入自己的 `backend/.env`；生产由部署环境向API和account-worker注入相同值。
4. 通过现有 `scripts/bootstrap_database.py` 升级至 `20260921_0001`。
5. 启动 `python -m app.worker.account_worker`；生产Compose已包含 `account-worker`，本地dev-up/dev-down也包含它。
6. 部署前端，从一个测试班验证后扩大使用。缺密钥时策略修改、含凭据的批次确认返回503，不写明文。

生成密钥（仅在受控终端执行，输出放入秘密配置）：

```bash
cd backend
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

`DAI_ACCOUNT_SECURITY_KEYS` 支持逗号分隔的密钥列表，新密钥放前面，旧密钥保留以读取旧策略和未结束批次。
使用新密钥重新保存密码策略会重加密；等待旧批次完成后才能移除不再使用的旧密钥。
数据库备份离不开对应密钥备份；恢复验证须验证策略解密和待执行批次。

Worker默认两个散列线程、120秒租约，每批准备40份摘要后续租；数据库每5秒轮询，可恢复丢失Redis唤醒。
各账户独立bcrypt成本12。导入可能持续数分钟，准备进度不等于账号已创建。
所有业务写入和成功终态一起提交；不持有数据库事务计算整批摘要。
重置则逐人提交，因此部分成功是真实终态，需按明细检查，不承诺整批回滚。

## 验证命令

仅对隔离数据库运行测试，共享测试fixture会创建和删除表，绝不可设置生产 `DAI_DATABASE_URL`。

```bash
cd backend
env -u DAI_DATABASE_URL -u DAI_REDIS_URL .venv/bin/pytest -q tests/automated/test_account_batches.py tests/automated/test_account_business_regressions.py tests/automated/test_roster_business_regressions.py tests/automated/test_student_history.py tests/automated/test_account_migration.py
cd ../frontend
npm run check
```

MySQL并发测试单独使用 `DAI_TEST_MYSQL_URL`，只接受空的 `dai_test_account_*` 数据库；运行 `test_account_mysql_concurrency.py`。

```bash
# 从仓库根执行；仅创建并清理自己生成的临时SQLite文件
PYTHONPATH=backend backend/.venv/bin/python scripts/verify_account_capacity.py
```

容量验证脚本对5000人导入和重置使用测试专用bcrypt成本4，验证事务、数量和不同盐；另外实测60个生产成本12摘要。
这是功能容量与计算样本，不是生产5000人吞吐SLA。学校真实并发、资源争用及公网拓扑需在部署机验收。

## 回滚和保留

暂停新批次时停止account-worker，已完成结果保留；重新启动后会恢复过期租约。
不得回退到忽略 `must_change_password` 的旧认证代码。
数据库降级默认拒绝。仅非生产的可丢弃测试库可显式设置 `DAI_ALLOW_ACCOUNT_MANAGEMENT_DOWNGRADE=true`。
任务明细保留90天，审计及批次摘要365天；可通过对应配置调整。
新规则不会批量重置旧学生。旧学号/用户名不一致需管理员逐个核对，导入不会自动改名。

旧身份只读核对：在后端运行 `python -m app.cli audit-student-identities`，输出缺失学号或登录名不一致的账号ID/学号/登录名，不修改数据。

生产account-worker使用公共配置，但关闭环境编辑器和AI功能，不挂载Registry秘密或Docker socket；`DAI_ENV_BASE_IMAGE`仍应按生产配置约定填写digest引用。
