// 自主练习（批次 5）共享展示逻辑：状态枚举 → 文案/色调、六态判定与错误映射。
// 页面与组件只从这里取文案，避免同一状态在多个视图里各写一套。

export const DIFFICULTY_META = {
  easy: { label: '简单', tone: 'success' },
  medium: { label: '中等', tone: 'warning' },
  hard: { label: '困难', tone: 'danger' },
}

export const PROBLEM_STATUS_META = {
  draft: { label: '草稿', tone: 'neutral' },
  published: { label: '已发布', tone: 'success' },
  archived: { label: '已归档', tone: 'info' },
}

export const GRADING_MODE_META = {
  deterministic: { label: '普通判题', tone: 'neutral' },
  ai: { label: 'AI 综合判题', tone: 'accent' },
}

// 提交终态：queued/running 之后才会进入其中之一。AI 未完成绝不等于 0 分。
export const GRADING_STATUS_META = {
  queued: { label: '排队中', tone: 'warning' },
  pending: { label: '排队中', tone: 'warning' },
  running: { label: '判题中', tone: 'info' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '判题失败', tone: 'danger' },
  review_required: { label: '待人工复核', tone: 'warning' },
  quota_exhausted: { label: '今日额度已用完', tone: 'danger' },
}

export const AI_STATUS_META = {
  pending: 'AI 评分未完成',
  running: 'AI 评分未完成',
  failed: 'AI 评分未完成',
  skipped: '本次未启用 AI 评分',
  completed: 'AI 评分已完成',
}

export const ACTIVE_STATUSES = ['queued', 'pending', 'running']
const TERMINAL_STATUSES = ['completed', 'failed', 'review_required', 'quota_exhausted']

export function difficultyMeta(value) {
  return DIFFICULTY_META[value] || { label: '未标注', tone: 'neutral' }
}

export function problemStatusMeta(value) {
  return PROBLEM_STATUS_META[value] || { label: value || '未知', tone: 'neutral' }
}

export function gradingModeMeta(value) {
  return GRADING_MODE_META[value] || { label: '普通判题', tone: 'neutral' }
}

export function gradingStatusMeta(value) {
  return GRADING_STATUS_META[value] || { label: value || '未知状态', tone: 'neutral' }
}

export function isActiveStatus(value) {
  return ACTIVE_STATUSES.includes(value)
}

export function isTerminalStatus(value) {
  return TERMINAL_STATUSES.includes(value)
}

/** 列表接口既可能返回数组，也可能返回 { items, total }（既有页面同样兼容两种） */
export function listOf(data) {
  if (Array.isArray(data)) return data
  return data?.items || []
}

/** 排队名次只在排队中展示；后端未给出名次时返回 null（不猜名次） */
export function queuePositionText(submission) {
  if (!submission || submission.grading_status !== 'queued') return null
  const position = Number(submission.queue_position)
  if (!Number.isFinite(position) || position <= 0) return '排队中，等待判题'
  return `排队中（第 ${position} 位）`
}

export function isQuotaError(error) {
  const detail = error?.response?.data?.detail
  const code = typeof detail === 'string' ? detail : detail?.code
  return error?.response?.status === 429 || code === 'QUOTA_EXHAUSTED'
}

export function isPermissionError(error) {
  return error?.response?.status === 403
}

export function isNotFoundError(error) {
  return error?.response?.status === 404
}

const ERROR_CODE_TEXT = {
  QUOTA_EXHAUSTED: '今日额度已用完',
  AI_PREFLIGHT_FAILED: 'AI 评分标准尚未准备完成，请先补齐未就绪题目',
  DEVICE_SUPERSEDED: '当前提交已被更新的请求取代，请刷新后重试',
  GRADING_IN_PROGRESS: '判题仍在进行，请稍后重试',
  AI_GRADING_IN_PROGRESS: 'AI 评分仍在进行，请稍后重试',
  PROBLEM_NOT_FOUND: '题目不存在或已下架',
  SUBMISSION_NOT_FOUND: '提交记录不存在',
}

/** 学生可见的错误文案：额度、权限、未找到、限流各有明确说法 */
export function practiceErrorMessage(error, fallback = '操作失败，请稍后重试') {
  if (!error?.response) return '网络异常，请检查连接后重试'
  if (isQuotaError(error)) return ERROR_CODE_TEXT.QUOTA_EXHAUSTED
  if (isPermissionError(error)) return '当前账号没有自主练习的管理权限'
  if (isNotFoundError(error)) return '题目或提交记录不存在'
  if (error.response.status === 429) return '操作过于频繁，请稍后再试'
  const detail = error.response.data?.detail
  const code = detail?.code
  if (code && ERROR_CODE_TEXT[code]) return ERROR_CODE_TEXT[code]
  if (detail?.message) return detail.message
  if (typeof detail === 'string' && detail) return detail
  return fallback
}

/** AI 未完成/失败：只展示已完成的确定性分，绝不显示 0 分或完整 AI 成绩 */
export function aiIncomplete(submission) {
  if (!submission) return false
  if (submission.grading_mode !== 'ai') return false
  return submission.ai_status !== 'completed'
}

export function aiStatusText(submission) {
  if (!submission?.grading_mode || submission.grading_mode !== 'ai') return ''
  return AI_STATUS_META[submission.ai_status] || 'AI 评分未完成'
}

export function scoreText(submission) {
  if (!submission) return '—'
  const score = submission.score
  const max = submission.max_score
  if (score === null || score === undefined) return '—'
  if (max === null || max === undefined) return String(score)
  return `${formatScore(score)} / ${formatScore(max)}`
}

export function formatScore(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '—'
  return Number.isInteger(num) ? String(num) : num.toFixed(1)
}

export function testSummary(submission) {
  const deterministic = submission?.deterministic
  if (!deterministic) return ''
  const passed = Number(deterministic.passed_count)
  const total = Number(deterministic.total_count)
  if (!Number.isFinite(passed) || !Number.isFinite(total)) return ''
  return `${passed} / ${total} 组测试通过`
}

/** 完成状态筛选（学生视角） */
export const COMPLETION_OPTIONS = [
  { value: 'all', label: '全部题目' },
  { value: 'unsolved', label: '未通过' },
  { value: 'attempted', label: '做过未通过' },
  { value: 'solved', label: '已通过' },
]

export const DIFFICULTY_OPTIONS = [
  { value: '', label: '全部难度' },
  { value: 'easy', label: '简单' },
  { value: 'medium', label: '中等' },
  { value: 'hard', label: '困难' },
]

export const BOARD_OPTIONS = [
  { value: 'deterministic', label: '普通测试榜' },
  { value: 'ai', label: 'AI 综合榜' },
]

// AI 预检未就绪原因：后端 missing 取值是稳定代码，前端统一翻成中文再展示
export const MISSING_REQUIREMENT_LABELS = {
  locked_rubric: '评分标准未锁定',
  frozen_rubric: '评分标准未随发布冻结',
  active_version: '缺少已启用的评分版本',
  test_groups: '缺少测试组',
}

export function missingRequirementsText(missing) {
  const list = Array.isArray(missing) ? missing : []
  if (!list.length) return ''
  return list.map((item) => MISSING_REQUIREMENT_LABELS[item] || String(item)).join('、')
}

export function newClientRequestId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `practice-${Date.now()}-${Math.random().toString(16).slice(2)}`
}
