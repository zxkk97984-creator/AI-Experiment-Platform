# 实验教学、训练与自动评测

实验继续使用平台自己的 Notebook 界面与隔离 Python 内核。旧实验维持人工评分；只有通过参考试评并发布的实验版本启用新流程。课程内 Notebook 与独立实验模块共用该功能。

## 教师备课

1. 在实验 Studio 打开「实验备课与评测」。选择普通 Notebook、表格回归、表格分类或图像分类，以及引导式／开放式教学。
2. 上传数据。文件先入库，独立 lab-worker 校验后才可用于发布。数据集版本不可覆盖；修正数据时上传新版本。
3. 选择 Notebook／CPU训练与评测环境。需要 PyTorch 模板时选择已验证的 PyTorch 环境，不能选只有基础 Python 的环境。GPU 环境另选，仅用于后台训练。
4. 可导入内置教学模板，修改讲解、代码和任务。Markdown 单元格打开「学生填写」后成为报告区域。普通保存不会提前发布实验。
5. 写明预期结果、算法要求与分析问题，使用内置量表或 AI 生成后编辑。AI 服务未配置时仍能手动编辑量表，但不能把含 AI 项的配置假装试评通过。
6. 参考实验必须完整可运行。可以用完整参考 Notebook 从头训练，也可以上传已有参考模型包。勾选确认量表，再运行参考试评。修改 Notebook、环境、数据或量表后必须重新试评。
7. 试评通过后发布，同时激活 Notebook 和评分版本。已开始的学生记录继续绑定原版本，不会被悄悄换题；新版本用于新记录。

普通 Notebook 的代码检查在独立容器运行；模型实验的正式评测只执行推理，不重复训练。参考模型分数用于确认流程和阈值，不要求所有数据集统一达到某个准确率。

### 评分

模型模板默认效果40分、方法40分、分析20分。教师可调整，总分100。AI 项的等级需包含0分和满分，AI只选择已发布等级，实际合分由后端完成。默认分类以Macro-F1计分、展示准确率；回归以RMSE计分、展示MAE。零分基线与满分目标之间线性折算，超出目标不加额外分。

正常评分自动发布。指标低按量表正常给低分；运行异常、模型不可加载、AI响应无效、证据不存在或无法确定的判断进入人工复核。基础设施失败有限重试，最终失败不记零分并退回提交额度。AI只阅读代码、分析文字和平台实测汇总指标，不接收隐藏标签；学生打印的准确率和外部日志不是可信指标。

教师可在提交详情查看分项、复核、填写改分原因或重评。每次改分保存独立修订，旧任务不能覆盖人工分。班级评分标准在“授课管理”中分别设置；纠正数据或标准时，必须包含本班该实验的全部正式提交，整个批次成功后才切换正式成绩，避免不同标准混算。切换期间仍可练习，但暂停正式提交。模板内容变化的历史记录不支持直接换题重评。

后台任务在成绩发布前再次检查授课权限、学期及班级状态。教师移交、账号停用、学期关闭或授课归档后，晚到的普通自动评分保留证据并进入复核，已发布成绩保持原值。未确认班级归属的历史记录，只允许管理员明确核对后通过历史重评入口操作；全局模板重评入口不能更新班级记录。

## 数据格式

### 表格 CSV

默认UTF-8（支持BOM）。目标列默认`label`，样本ID默认`sample_id`；省略ID配置时允许自动生成。字段名不可重复，特征列至少一个，回归标签必须是有限数值。

```csv
sample_id,temperature,humidity,label
s001,18.2,0.65,0
s002,24.8,0.42,1
```

单CSV默认固定seed=42按70%／15%／15%划分。也可指定`split`列，其值为`train`、`validation`、`test`，或上传包含`train.csv`、`validation.csv`、`test.csv`的ZIP。不得混用部分自动、部分手工划分。重复样本ID、重复内容和分区重叠会被拒绝。时间序列或同一对象的关联样本应由教师先完成合理分区。

### 图片 ZIP

支持JPEG、PNG、GIF、WebP。使用`类别/图片`或`train/类别/图片`等目录；也可提供根目录`labels.csv`：

```csv
sample_id,image_path,label,split
s001,images/001.png,cat,train
s002,images/002.png,dog,validation
s003,images/003.png,cat,test
```

标签清单必须覆盖每张图片恰好一次。保留测试图片的导出路径和样本ID会被重新编码，避免类别目录泄露答案。教师上传包和完整测试数据不提供给学生下载。

默认压缩上传500MiB、展开2GiB、最多100000个文件、200000个样本；图片头部尺寸上限4000万像素、边长16384。路径穿越、链接、特殊文件、加密ZIP及超限包被拒绝。像素解码与模型加载只在限额容器中进行。修改上传上限还须同步反向代理限制。

### 学生读取与下载

```text
/course/                         教师教材附件
/course/train/samples.csv         训练样本，无label列
/course/train/labels.csv          sample_id,label
/course/validation/samples.csv    公开验证样本
/course/validation/labels.csv     公开验证标签
```

图像样本清单的`image_path`相对于所在分区目录。Notebook按`sample_id`关联样本和标签。学生可下载独立生成的`train.zip`和`validation.zip`进行本地训练；下载包不含测试分区。概览只列清单和下载包，不为每张图片产生查询或页面条目。

## 学生训练与提交

「开始训练」先保存，再冻结代码和环境，从全新内核按顺序执行。训练与交互内核不共写目录，关闭网页不取消后台任务。模型结果包含对应的代码版本；选择成果后提交，不能把旧模型和新代码混成一个版本。

模型任务需生成根目录`predict.py`和所需模型／预处理文件。平台模板使用`model.py`、`model.pt`、`preprocess.json`，并将每轮`metrics.json`用于绘制学习曲线。外部成果ZIP还须包含训练代码或Notebook。不会在API进程中导入或反序列化上传模型。

```bash
python predict.py --input /input/samples.csv --model-dir /submission \
  --output /output/predictions.csv --device cpu
```

预测CSV必须且只能包含`sample_id,prediction`，每个样本恰好一行；类别值须合法，回归预测须有限。正式推理只有无标签输入；容器退出后，平台独立读取测试标签计分。正式评测原始日志及逐样本结果不向学生公开。

公开验证不占正式提交次数，但与训练一样排队。同一学生最多一个未完成训练任务和一个未完成公开验证。默认5次正式提交、取最高分；教师可改为取最后一次。服务端以收到提交的时间判断截止，排队中的有效提交可在截止后完成。重复请求不重复计次。成绩发布后仍可修改、重训、重交；学生可在成绩详情申请复核。

外部与平台训练采用相同效果／代码／分析标准。外部日志仅标记为学生提供；要求证明实际训练过程的实验须在发布前选择「必须平台训练」。每记录默认保留20份成果，总存储额度2GiB；可主动删除未被提交、运行或发布参考引用的成果。删除后由现有reconcile/GC安全回收物理对象，共享对象不会直接删除。

## 部署与运维

1. 备份数据库及Storage，按现有bootstrap流程升级到`20260926_0001`。迁移只新增表，不改旧实验成绩。生产禁止降级删除实验依据。
2. API与lab-worker必须使用相同数据库、对象存储及资源预算。生产Compose已添加`lab-worker`与`lab_data`持久卷；本地`start.sh`／`dev-up.sh`也管理该进程。
3. 按学校设备设置预算：`DAI_LAB_TOTAL_CPUS`、`DAI_LAB_TOTAL_MEMORY_MB`、`DAI_LAB_INTERACTIVE_RESERVED_MEMORY_MB`、`DAI_LAB_MAX_KERNELS`、`DAI_LAB_TRAINING_CONCURRENCY`。预算应先扣除API、数据库、现有判题／构建Worker和lab-worker本身的占用。初始后台并发1、任务30分钟。
4. 构建并检查新的CPU镜像，使用注册工具创建新环境版本，不覆盖旧的`torch-cpu`版本：

```bash
# 在仓库根目录执行；若构建网络使用宿主回环代理，可加 --network=host。
docker build -f backend/docker/lab/Dockerfile -t dai-lab-cpu:20260926 .
backend/.venv/bin/python scripts/register_lab_environment.py --image dai-lab-cpu:20260926
# 明确选择配置中的目标数据库和有效管理员ID后才执行：
backend/.venv/bin/python scripts/register_lab_environment.py \
  --image dai-lab-cpu:20260926 --apply --admin-id <管理员ID>
```

注册脚本默认只检查镜像，不写数据库；`--apply`读取`backend/.env`及环境变量。生产先自行推送学校Registry，使用`repository@sha256:digest`注册，并保持各执行节点可用。镜像验证包括PyTorch、数据依赖、非root导入和CPU运算；旧已知受影响PyTorch版本拒绝注册。依赖版本固定在Dockerfile，仍应执行学校的镜像安全审查。

5. AI复用既有`DAI_AI_ENABLED`、模型地址／密钥／模型名。缺失或故障时进入明确的未就绪／人工复核流程，不生成假分。
6. GPU默认关闭。部署方须准备NVIDIA运行环境，使用合适的PyTorch CUDA wheel索引构建独立镜像，先以注册脚本`--device gpu --gpu-device 0`实测，再配置`DAI_LAB_GPU_ENABLED=true`、`DAI_LAB_GPU_DEVICES=0`。GPU整卡独占，只供后台任务；正式评分保持CPU基准。没有显卡时不能宣称GPU已验收。
7. Worker使用数据库任务与租约，Redis只唤醒。恢复会先确认原容器和临时卷已清理，才释放额度或重试。取消／超时终止容器；无法确认清理时保留预算。内核空闲回收也释放预算。
8. 学生容器断网、非root、只读根目录，CPU、RAM、PID、时间、日志、单文件和输出卷均有限额。训练写入受限tmpfs卷，文件也计入容器内存，配置时要预留模型体积。发布参考试跑与真实课上负载决定最终限额。

训练日志默认保留末尾1MiB，`DAI_LAB_LOG_MAX_BYTES`可设为4KiB～8MiB；MySQL使用MEDIUMTEXT存放日志，UTF-8截断不会突破字节额度。资源首次准入采用原子锁记录；取消已领取的任务时，须等容器清理完成才释放CPU／内存／GPU预算。

管理员环境页面展示生效额度与队列。`./start.sh logs lab`查看本地Worker日志。备份必须包括数据库、`lab_data`（或对应S3前缀）、Studio资料及现有工作区。临时教材缓存可以重建；正式模型、数据版本和评分历史不可当缓存清除。

100～200人是在线教学目标，不意味着同数量训练任务同时运行。已提供功能模拟脚本，生产MySQL、HTTP并发、磁盘和实际训练吞吐需在部署机测量。
