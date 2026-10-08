export const taskLabels = {
  notebook: 'Notebook 代码实验', tabular_regression: '表格回归',
  tabular_classification: '表格分类', image_classification: '图像分类',
}
export const statusLabels = {
  queued: '排队中', pending: '等待处理', running: '运行中', validating: '校验中',
  succeeded: '已完成', completed: '已完成', ready: '已就绪', failed: '处理失败',
  cancelled: '已取消', cancel_requested: '正在取消', timed_out: '已超时',
  needs_review: '待教师复核', recovering: '正在恢复任务',
  review_required: '待教师复核', published: '已发布', draft: '草稿',
}
export const statusLabel = (value) => statusLabels[value] || '处理中'
export const isActive = (status) => ['queued', 'pending', 'running', 'validating', 'cancel_requested', 'recovering'].includes(status)
export const metricLabels = { macro_f1: 'Macro-F1', accuracy: '准确率', rmse: 'RMSE', mae: 'MAE' }
export const clone = (value) => JSON.parse(JSON.stringify(value))
export function errorMessage(error, fallback = '操作失败，请重试') {
  if (error?.response?.status === 413) return '文件超过服务器允许的大小，请检查数据集或成果上传限制'
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  if (Array.isArray(detail)) return detail.map((item) => item.msg).join('；')
  return error?.message && !error?.response ? error.message : fallback
}
export function builtinCriteria(taskType) {
  if (taskType !== 'notebook') return [
    { id: 'performance', name: '模型效果', description: '平台独立测试的指标线性折算', source: 'metric', max_score: 50, levels: [] },
    { id: 'implementation', name: 'Notebook 单元格结果', description: '完成指定单元格的计算与输出要求', source: 'cell', max_score: 50, levels: [] },
  ]
  const ai = (id, name, description, max) => ({ id, name, description, source: 'ai', max_score: max, levels: [
    { id: 'missing', description: '未完成要求，或缺少能支持判断的实现与说明', points: 0 },
    { id: 'partial', description: '完成部分要求，存在可定位的遗漏或错误', points: max / 2 },
    { id: 'complete', description: '完整实现本项要求，代码与分析能够支持结论', points: max },
  ] })
  const criteria = [
    ai('implementation', '实现与训练方法', '数据处理、算法和实现符合任务要求，训练与验证分开', taskType === 'notebook' ? 70 : 40),
    ai('analysis', '实验分析', '说明实现方法，比较结果并解释误差与改进方向', taskType === 'notebook' ? 30 : 20),
  ]
  return criteria
}
export function defaultConfig(taskType = 'notebook') {
  return {
    task_type: taskType, teaching_mode: 'guided', requirements: '', dataset_id: null,
    environment_version_id: null, gpu_environment_version_id: null, allow_external: true,
    require_platform_training: false, max_submissions: 5, grade_selection: 'highest',
    metric: taskType === 'tabular_regression' ? 'rmse' : 'macro_f1',
    zero_baseline: taskType === 'tabular_regression' ? 1 : 0,
    full_target: taskType === 'tabular_regression' ? 0 : 1,
    criteria: builtinCriteria(taskType),
    resources: { cpus: 1, memory_mb: 4096, timeout_seconds: 1800, disk_mb: 2048 },
    evaluation_resources: { cpus: 1, memory_mb: 4096, timeout_seconds: 300, disk_mb: 2048 },
    reference_artifact_id: null, reference_cells: [], check_script: '', cell_checks: [],
    efficiency_full_seconds: 1, efficiency_zero_seconds: 60, confirmed: false,
  }
}
export function rubricErrors(config) {
  const errors = []
  if (!config.criteria?.length) errors.push('请至少设置一项评分标准')
  if (Math.abs(config.criteria.reduce((sum, c) => sum + Number(c.max_score), 0) - 100) > 0.000001) errors.push('评分项满分合计必须为 100 分')
  for (const c of config.criteria) {
    if (!c.name.trim() || !c.description.trim()) errors.push('请填写每项标准的名称与具体要求')
    if (c.source === 'ai' && (c.levels.length < 2 || Math.min(...c.levels.map(l => Number(l.points))) !== 0 || Math.max(...c.levels.map(l => Number(l.points))) !== Number(c.max_score))) errors.push(`${c.name || '评分项'}的等级须包含零分与满分`)
  }
  if (config.criteria.some(c => c.source === 'efficiency') && !(config.efficiency_zero_seconds > config.efficiency_full_seconds && config.efficiency_full_seconds > 0)) errors.push('效率零分耗时须大于正数满分耗时')
  for (const rule of config.cell_checks || []) {
    if (!rule.cell_id) errors.push('请选择每项检查对应的代码单元格')
    if (rule.kind === 'python' && !rule.script.trim()) errors.push('请上传或填写自定义 Python 检查脚本')
    if (rule.kind === 'shape' && (!rule.shape.length || rule.shape.some(v => !Number.isInteger(v) || v < 0))) errors.push('形状维度须为非负整数')
  }
  return [...new Set(errors)]
}
export function formatMetric(value) {
  return typeof value === 'number' && Number.isFinite(value) ? Number(value.toFixed(4)).toString() : '—'
}
export async function downloadLabFile(api, file) {
  const response = await api.getFile(file.id)
  const url = URL.createObjectURL(response.data)
  const a = document.createElement('a')
  a.href = url
  a.download = file.path?.split('/').pop() || file.name || '实验文件'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export function submissionStatus(submission) {
  if (!submission?.lab_enabled) return submission?.score == null ? '待评分' : '已评分'
  if (submission.review_requested || ['needs_review', 'review_required'].includes(submission.lab_status)) return '待复核'
  if (submission.lab_status === 'queued') return '等待评测'
  if (isActive(submission.lab_status)) return '评测中'
  if (submission.lab_status === 'failed') return '评测失败，待处理'
  if (submission.score != null) return '成绩已发布'
  return submission.lab_status === 'succeeded' ? '等待发布成绩' : '待评测'
}
