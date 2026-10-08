# 实验升级验证记录

日期：2026-09-26～2026-09-27。验证只使用临时数据库、合成教学数据和独立测试容器；未迁移现有开发／生产数据库，未改真实学生成绩。

## 2026-10-08 班级评测补充验收

验证日志保存于本地忽略目录 `.runtime/school-verification/`；以下使用隔离 SQLite、合成教学数据和测试自己创建的 Docker 容器。

- 真实 Docker CPU 运行回归 27 项通过：包括完整 Notebook 重放、Cell 观测、拒绝伪造观测文件及输出协议、程序耗时、取消清理、超时、文件上限、PyTorch CPU 跨容器训练及预测。
- Notebook 镜像使用 `dai-lab-cpu:20261003`，模型 CPU 测试使用现有 `dai-env:torch-cpu-v1`，均由测试解析为不可变镜像 ID；Notebook 镜像的 `/opt/dai/kernel_runner.py` 已另行检查存在。
- 班级重评拒绝全局入口绕过、只允许管理员操作未归属历史，校验提交 ID 唯一且为正整数。新标准切换前复查全部提交 ID、运行版本、评分证据版本及当前权限，发现变化则保留原成绩并进入复核。
- 普通晚到评分覆盖学期关闭、班级归档、授课归档、教师移交和账号停用：自动证据保留，原成绩及选定成绩版本不被覆盖。

复现本次真实 CPU 检查：

```bash
env -u DAI_DATABASE_URL -u DAI_REDIS_URL \
  DAI_LAB_TEST_IMAGE=dai-lab-cpu:20261003 \
  DAI_LAB_TEST_TORCH_IMAGE=dai-env:torch-cpu-v1 \
  backend/.venv/bin/python -m pytest -q backend/tests/automated/test_lab_runtime.py
```

本次 CPU 容器证据不代表目标服务器 200 人负载或 GPU 驱动验收。

## 已完成的证据

- 教师配置、参考试评、版本发布、学生保存／提交、最高分、额度、幂等、截止、权限与人工改分都有自动化回归。
- 数据与成果覆盖local及模拟S3；压缩包越界、链接、超限、图像尺寸、分区重叠、隐藏标签、文件完整性、删除引用保护和分页均有测试。
- 全量后端按四个独立进程执行：1542项通过、6项按既有外部环境开关跳过，1项旧判题路径回归失败。原因是Python的MagicMock被当成os.PathLike；改为仅接受实际配置使用的str/Path后，该模块19项全部通过。新增迁移降级测试开关、旧内核测试适配及Docker持有者文档也已修复。
- 最后变更另行复验：API／队列24项、日志／迁移／环境注册12项、文档链接／静态检查55项通过。上述为有重叠的复验数量，不能与全量数量简单相加。
- 独立临时MySQL关键API／评分／队列：51项通过。修复首次资源锁竞争、重评锁顺序及领取与取消竞争，包含真实数据库并发请求；未使用现有MySQL数据。
- MEDIUMTEXT日志调整后，另一个临时MySQL运行11项队列／幂等回归通过，包含超过64KiB的中文日志与1MiB字节截断。所有临时MySQL容器均已删除。
- 前端ESLint、108个测试文件共946项Vitest、生产构建通过，包含成果分页、版本不兼容禁用和重评状态。
- Playwright覆盖学生320/768/1024/1440、教师320/1440、人工复核与管理员面板，共8项通过。浏览器使用隔离API fixtures，未操作真实教学数据。
- 实际构建`dai-lab-cpu:20260926`，PyTorch 2.14.0+cpu、Python 3.12.13。注册工具的真实CPU张量／依赖检查通过；未执行`--apply`写入现有环境目录。
- 三个内置教学Notebook（表格回归、表格分类、图像分类）在新镜像完成训练、权重保存与全新CPU容器推理。测试输入容器无labels.csv，标签在容器退出后独立计分。
- 真实Docker额外验证了非root、公开数据只读、Notebook魔法命令、取消清理、超时终止、输出卷容量限制和PyTorch权重跨容器预测。
- 使用现有配置的真实AI服务提交一份合成加法代码，仅一个明确评分项；结构、等级和原文引用校验通过。该检查证明接口与验证链路可用，不代表真实课堂评分质量已校准。
- 200个身份、1400个ASGI请求的功能模拟通过：保存与提交、重复训练幂等、跨学生403、预算仅准入1个任务。临时SQLite＋fakeRedis、最多8个在途请求；不代表200同时训练或生产HTTP/MySQL吞吐。
- Nginx真实容器配置检查、生产Compose配置展开、启动脚本语法、Ruff及`git diff --check`通过。

## 复现

```bash
# 仓库根目录，后端使用隔离数据库，绝不能传入真实业务库。
env -u DAI_DATABASE_URL -u DAI_REDIS_URL \
  DAI_LAB_TEST_IMAGE=dai-lab-cpu:20260926 \
  DAI_LAB_TEST_TORCH_IMAGE=dai-lab-cpu:20260926 \
  backend/.venv/bin/python -m pytest -q backend/tests/automated/test_lab_*.py
npm --prefix frontend run check
# 需要单独启动前端；fixture拦截业务API，不依赖真实教学数据。
E2E_BASE_URL=http://127.0.0.1:5173 npm --prefix frontend run e2e -- lab-assessment-flow.spec.js --workers=2
backend/.venv/bin/python backend/scripts/lab_load_smoke.py --clients 200
```

没有显卡的本机未验证真实GPU执行；GPU单卡独占、配置校验和拒绝静默CPU回退有代码及测试，真实驱动／CUDA／显存与课堂容量仍须部署机验收。真实教学AI标准由教师以参考实验试评、抽查和纠错持续校准。依赖弃用warning属既有测试噪声，不计为通过数量。
