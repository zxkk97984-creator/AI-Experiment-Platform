# Docker Socket 主机隔离与远程 Runner 触发条件（TASK-031 / F-19）

> 决策记录（ADR）。状态：**代码侧已实现，风险接受待部署方签字**（§6 待办）；
> 代码侧事实（Socket 持有者、学生容器隔离基线）已由测试与验证脚本守护。

本文件中的“待部署方验证”不是业务功能缺失，而是必须在真实部署主机完成的基础设施验收。受限开发沙箱没有 Docker socket 权限时，不能据此推断生产主机的 daemon 状态，也不能据此宣称真实镜像构建/Registry 推送已经完成。

## 1. 决策

以符合单机规模的方式降低 Docker Socket 宿主机风险：

1. **Socket 仅由确需服务持有**——只有需要创建/销毁判题与内核容器的服务挂载
   `/var/run/docker.sock`，其余服务一律不持有；
2. **部署主机专机专用**——Compose 部署主机不承载其他业务或敏感工作负载
   （外部事实，见 §6 待确认）；
3. **Docker API 不对网络开放**——dockerd 只监听本地 socket，不启用 TCP 端口
   （2375/2376）与 SSH/TLS 远程访问；
4. **学生代码容器与 Socket 物理隔离**——学生提交只进入判题/内核沙箱容器，
   沙箱 `--network none`、无 Socket 挂载、资源受限（见 §3）；
5. **不建设 Socket Proxy/远程 Runner 服务**——操作性隔离不能消除
   “容器 RCE 后主机控制”风险，本任务以专用主机 + 风险接受方式处理；
   远程 Runner 为独立项目，触发条件见 §5。

## 2. Socket 持有者清单（2026-10-14 更新）

| 服务 | 是否挂载 Socket | 用途 |
| --- | --- | --- |
| api | 是 | 同步判题路径（legacy 判题/实验执行）需要启动判题容器 |
| worker | 是 | 确定性作业/考试队列消费者，启动判题容器 |
| lab-worker | 是 | 实验训练、独立推理与检查容器的可信调度控制进程 |
| kernel-worker | 是 | 独立消费异步单元格队列并接管/管理持久交互内核 |
| practice-worker | 是 | 独立队列中的自主练习确定性 F/R 判题；AI 阶段仍由无 Socket 的 ai-worker 处理 |
| account-worker | 否 | 账号批次与密码摘要，不执行学生代码 |
| ai-worker | 否 | 可选 AI 队列消费者；只调用 AI 服务，不执行 Docker 判题，默认 profile 不启动 |
| environment-builder | 是 | 环境档位镜像构建（低频管理任务） |
| frontend / mysql / redis / migrate | 否 | 无容器生命周期职责 |

守护：`backend/tests/automated/test_docker_socket_isolation.py` 断言清单
与 `docker-compose.prod.yml` 一致（多挂少挂都失败），并明确断言 `ai-worker`
不继承 Docker Socket、`judge-work` 或 Registry Secret。AI worker 的队列/角色隔离
由 `test_phase4_worker_isolation.py` 一并守护。`kernel-worker` 与 `practice-worker`
只获得各自运行职责所需的 Docker 权限，不共享 Redis 队列或判题容量；完整试判的 AI 阶段
仍运行在不挂 Docker Socket 的 `ai-worker` 中。

实验升级新增 `lab-worker`：它是可信调度控制进程，持有 Docker socket，用于创建训练、推理与检查容器；学生容器永不继承 socket、数据库／对象存储凭据或 AI 密钥。任务仅挂载对应公开数据或无标签测试输入，隐藏标签由容器退出后的平台评分器读取。运行目录使用有容量上限的临时卷，停止和回收必须确认容器删除后才释放资源。

## 3. 学生容器隔离基线

学生提交的代码只在判题、交互内核及实验训练／推理容器中执行，两者均满足：

- `--network none`（无网络，不能访问宿主机/内网/外网）
- `--cap-drop ALL` + `no-new-privileges` + `--read-only` + tmpfs
- `--user 1000:1000`（非 root）、PID 数量限制
- CPU/内存配额（判题 `--cpus`/`--memory`，内核按环境最低要求和实验预算分配；训练与内核共享资源账本）
- 不挂载 `/var/run/docker.sock`、无 `--privileged`、无 host 网络

代码位置：`app/worker/judge_worker.py`（`run_pytest_in_docker`）、
`app/services/kernel_manager.py`、`app/services/lab_runtime.py`。回归：test_docker_socket_isolation.py
静态断言上述参数在代码中存在且全仓无 `--privileged` / host 网络模式。

## 4. 威胁模型与剩余风险

| 威胁 | 缓解 | 剩余风险 |
| --- | --- | --- |
| 学生代码容器逃逸（内核漏洞等）→ 主机控制 | 沙箱参数基线（§3）+ 宿主专机（§6-1） | **容器逃逸后宿主机 Docker daemon 权限等价主机 root**——操作性隔离无法消除；缓解依赖沙箱参数、内核补丁与主机不承载敏感数据 |
| 判题容器内横向访问（内网/宿主服务） | `--network none` | 依赖沙箱参数持续正确（测试守护） |
| Docker API 远程暴露 | 仅本地 socket（§6-2 待部署方验证） | 待部署方确认 daemon 配置 |
| 恶意镜像/供应链（环境构建路径） | 构建任务由受控目录驱动（TASK-008 控制面）；镜像只接受 digest | 构建 Worker 拥有 daemon 权限，其输入面需持续审计 |
| 资源耗尽（fork 炸弹/内存） | pids/cpu/内存配额 | 单主机资源争抢影响教学可用性 |

## 5. 远程 Runner 触发条件（冻结）

出现**任一**条件即启动远程 Runner 独立项目（XL），不在本仓库内实现：

1. **公网多租户**：平台面向非校内/不受控用户开放；
2. **独立扩容**：判题吞吐需要与 API/DB 独立水平扩容；
3. **强隔离要求**：需要“逃逸不触及宿主机”级别的隔离保证（如教育/合规要求）；
4. **主机共享**：部署主机必须与其他业务/敏感负载共享。

## 6. 外部待办（部署方确认后回填并签字）

| # | 待确认事实 | 负责方 | 状态 |
| --- | --- | --- | --- |
| 1 | 部署主机专机专用（不承载其他业务/敏感负载）；如否 → 触发 §5 条件 4 | 部署方 | 待确认 |
| 2 | dockerd 仅本地 socket（`ss -lnt` 无 2375/2376；daemon.json 无 `hosts: tcp`）——可用 `scripts/verify_host_isolation.sh` 现场验证 | 部署方 | 待验证 |
| 3 | 剩余风险（§4 首行：逃逸=主机控制）接受签字 | 项目负责人 | 待签字 |
| 4 | 判题/内核沙箱参数定期回归纳入发布检查（CI 已含静态断言） | 运维 | 已由 CI 覆盖 |

## 7. 验证

- `backend/tests/automated/test_docker_socket_isolation.py`：Socket 持有者清单、
  沙箱参数静态断言（CI 常绿门禁）；
- `scripts/verify_host_isolation.sh`：部署主机现场验证——Socket 持有者、
  Docker API 未对网络开放、沙箱基线；输出报告，告警退出码 1。

部署方上线前至少应在实际 Compose 主机执行：

```bash
docker info
docker compose -f docker-compose.prod.yml config --quiet
ss -lnt | grep -E ':(2375|2376)\b' && echo '拒绝：Docker TCP API 不应监听' || true
```

`docker info` 的 `permission denied` 应通过 rootful Docker 的受控用户组或 rootless
Docker 的正确 socket 路径解决；不要将 `/var/run/docker.sock` 改为 `0666`，也不要
为了让构建通过而开放未保护的 TCP Docker API。`environment-builder` 能够成功构建、
推送并按 digest 回拉 Registry 镜像，才算环境 V2 的部署验收通过。

演练记录（2026-08-14，本机 Docker daemon）：
- 脚本 5 项断言全部通过（含本机 2375/2376 无监听）；
- 用 worker 同一组参数实际启动判题容器：pytest 沙箱内 1 passed，
  `docker inspect` 实测 `network=none capdrop=[ALL] readonly=true pids=50
  user=1000:1000`，挂载仅工作目录 bind（ro，无 Socket）；
- 内核容器隔离参数由 pytest 静态断言守护。
