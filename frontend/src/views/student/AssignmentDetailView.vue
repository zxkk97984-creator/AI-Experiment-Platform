<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import StudentAIGradingResult from '../../components/ai/StudentAIGradingResult.vue'
import { assignmentsAPI } from '../../api/assignments.js'
import { judgeAPI } from '../../api/judge.js'
import { useAppStore } from '../../stores/app.js'
import { sanitizeHtml } from '../../utils/sanitize.js'
import { formatDateTime, parseApiDateTime } from '../../utils/format.js'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const assignment = ref(null)
const questions = ref([])
const activeQ = ref(0)
// Drafts belong to this mounted page and each question, never to a shared browser account.
const drafts = reactive(new Map())
const code = computed({
  get: () => drafts.get(questions.value[activeQ.value]?.id)?.code || '',
  set: (value) => {
    const draft = drafts.get(questions.value[activeQ.value]?.id)
    if (draft && draft.code !== value) {
      draft.code = value
      draft.edited = true
    }
  },
})
const hasUnsavedDrafts = computed(() => [...drafts.values()].some(draft => draft.code !== draft.baseline))
const leaveTarget = ref(null)
const allowLeave = ref(false)
const submitting = ref(false)
const testing = ref(false)
const bottomTab = ref('self-test')
const showProblem = ref(true)
const testResult = ref(null)
const deadlineClock = ref(Date.now())

const MAX_POLL_COUNT = 120
let pageActive = true
let questionGeneration = 0
const assignmentRequests = createLatestRequestGuard()
const sampleRequests = createLatestRequestGuard()
// Historical restoration, submission and polling all write the same result panel.
const resultRequests = createLatestRequestGuard()
let submissionSequence = 0
const acknowledgedSubmissions = new Map()

function questionContext(question) {
  return { questionId: question.id, generation: questionGeneration }
}

function isCurrentQuestion(context) {
  return pageActive && context.generation === questionGeneration
    && questions.value[activeQ.value]?.id === context.questionId
}

const lineCount = computed(() => {
  let count = 1
  const s = code.value || ''
  for (let i = 0; i < s.length; i++) {
    if (s[i] === '\n') count++
  }
  return count
})

const lineNumbers = computed(() => {
  const nums = []
  for (let i = 1; i <= lineCount.value; i++) nums.push(i)
  return nums.join('\n')
})

const descriptionHtml = computed(() => {
  const desc = questions.value[activeQ.value]?.description
  if (!desc) return ''
  return sanitizeHtml(desc.replace(/\n/g, '<br>'))
})

const publicCasesPretty = computed(() => {
  const cases = questions.value[activeQ.value]?.public_cases
  if (!cases?.length) return ''
  return JSON.stringify(cases, null, 2)
})

const TERMINAL_ERROR_STATUSES = ['wrong_answer', 'runtime_error', 'time_limit_exceeded', 'system_error']

const TEST_STATUS_ICON = { queued: '⏳', running: '🔄', accepted: '✓', wrong_answer: '✗', runtime_error: '✗', time_limit_exceeded: '⏱', system_error: '✗', no_public_cases: '—' }
const TEST_STATUS_LABEL = { queued: '排队等待判题...', running: '正在判题中...', accepted: '全部测试通过', wrong_answer: '答案错误', runtime_error: '运行错误', time_limit_exceeded: '执行超时', system_error: '系统错误', no_public_cases: '无公开样例' }
const TEST_STATUS_CLASS = { queued: 'muted', running: 'muted', accepted: 'success', wrong_answer: 'error', runtime_error: 'error', time_limit_exceeded: 'warning', system_error: 'error', no_public_cases: 'muted' }

const SUBMIT_STATUS_LABEL = { queued: '⏳ 排队等待判题...', running: '🔄 正在判题中...', accepted: '✓ 全部通过', graded: '✓ AI 评分完成', review_required: '⚠ 待教师复核', wrong_answer: '✗ 答案错误', runtime_error: '✗ 运行错误', time_limit_exceeded: '⏱ 执行超时', system_error: '⚠ 系统错误' }

const submitResult = ref(null)
const submitPolling = ref(false)
const completedQuestions = ref(new Set())
const latestSubmissionByQuestion = new Map()
const currentGradingMode = computed(() => questions.value[activeQ.value]?.grading_mode || 'legacy')
const submitSucceeded = computed(() => ['accepted', 'graded'].includes(submitResult.value?.status))

// ── Phase 5：学生端环境提示与 import 诊断 ────────────────────
// 当前题目的有效环境摘要：题目覆盖优先（本题环境），否则作业默认（本作业环境）
const currentEnvSummary = computed(() => {
  const q = questions.value[activeQ.value]
  return q?.environment_summary || assignment.value?.environment_summary || null
})
const envLabel = computed(() => {
  const q = questions.value[activeQ.value]
  return q?.environment_summary ? '本题环境' : '本作业环境'
})
const envImportsText = computed(() => {
  const imports = currentEnvSummary.value?.imports || []
  return imports.join(' · ')
})
const envAllowedText = computed(() => {
  const summary = currentEnvSummary.value
  if (!summary || summary.import_policy_mode !== 'restricted') return ''
  return (summary.allowed_imports || []).join(' · ')
})
// 判题/自测结果优先显示结构化 diagnostic（安全中文文案，无裸 traceback）
const submitDiagnostic = computed(() => submitResult.value?.diagnostic || null)
const testDiagnostic = computed(() => testResult.value?.diagnostic || null)
const deadlineClosed = computed(() => {
  if (!assignment.value?.due_at) return false
  const due = parseApiDateTime(assignment.value.due_at).getTime()
  return !Number.isNaN(due) && deadlineClock.value >= due
})

function refreshDeadlineClock() {
  deadlineClock.value = Date.now()
}

async function refreshAssignment(isRelevant = () => true) {
  if (!pageActive || !isRelevant()) return
  const request = assignmentRequests.begin()
  refreshDeadlineClock()
  try {
    const response = await assignmentsAPI.get(route.params.id)
    if (pageActive && assignmentRequests.isLatest(request) && isRelevant()) assignment.value = response.data
  } catch {
    // 焦点刷新失败不清空当前页面，后续提交仍由服务端门禁兜底。
  }
}

function handleFocus() { refreshAssignment() }

function handleVisibilityChange() {
  if (document.visibilityState === 'visible') refreshAssignment()
}

function isDeadlineError(error) {
  return error.response?.data?.detail?.code === 'ASSIGNMENT_DEADLINE_PASSED'
}

function onBeforeUnload(event) {
  if (!hasUnsavedDrafts.value) return
  event.preventDefault()
  event.returnValue = ''
}

onBeforeRouteLeave((to, _from, next) => {
  if (allowLeave.value || !hasUnsavedDrafts.value) { next(); return }
  next(false)
  leaveTarget.value = to.fullPath
})

async function confirmLeave() {
  const target = leaveTarget.value
  leaveTarget.value = null
  allowLeave.value = true
  try { await router.push(target) }
  finally { allowLeave.value = false }
}

onMounted(async () => {
  window.addEventListener('beforeunload', onBeforeUnload)
  window.addEventListener('focus', handleFocus)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  deadlineTimer = window.setInterval(refreshDeadlineClock, 30000)
  const assignmentRequest = assignmentRequests.begin()
  const results = await Promise.allSettled([
    assignmentsAPI.get(route.params.id),
    assignmentsAPI.getQuestions(route.params.id),
    assignmentsAPI.myProgress(route.params.id),
  ])
  if (!pageActive) return
  if (results[0].status === 'fulfilled') {
    // A failed or still-pending focus refresh must not discard our only usable response.
    if (!assignment.value || assignmentRequests.isLatest(assignmentRequest)) assignment.value = results[0].value.data
  } else if (assignmentRequests.isLatest(assignmentRequest)) app.showToast('加载作业失败', 'error')
  if (results[1].status === 'fulfilled') {
    const qData = results[1].value.data
    questions.value = qData.items || qData || []
    for (const question of questions.value) {
      const initial = question.starter_code || ''
      drafts.set(question.id, { code: initial, baseline: initial, edited: false })
    }
  }
  if (results[2].status === 'fulfilled') {
    const subs = results[2].value.data.items || results[2].value.data || []
    for (const s of subs) {
      if (s.latest_submission_id != null) {
        latestSubmissionByQuestion.set(s.question_id, { id: s.latest_submission_id, question_id: s.question_id, status: s.status })
      }
      if (s.completed) completedQuestions.value.add(s.question_id)
    }
  }
  if (!assignment.value && questions.value.length === 0) {
    app.showToast('加载作业失败', 'error')
  }
  if (questions.value.length > 0) {
    await restoreLatestSubmission(questions.value[0])
  }
})

const currentCompleted = computed(() => {
  const qid = questions.value[activeQ.value]?.id
  return qid != null && completedQuestions.value.has(qid)
})

let deadlineTimer = null
onUnmounted(() => {
  pageActive = false
  questionGeneration++
  assignmentRequests.invalidate()
  sampleRequests.invalidate()
  stopSubmitPolling()
  if (deadlineTimer) window.clearInterval(deadlineTimer)
  window.removeEventListener('focus', handleFocus)
  window.removeEventListener('beforeunload', onBeforeUnload)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})

async function selectQuestion(idx) {
  questionGeneration++
  sampleRequests.invalidate()
  stopSubmitPolling()
  activeQ.value = idx
  testResult.value = null
  submitResult.value = null
  submitting.value = false
  testing.value = false
  submitPolling.value = false
  bottomTab.value = 'self-test'
  await restoreLatestSubmission(questions.value[idx])
}

async function restoreLatestSubmission(question) {
  if (!question || !pageActive) return
  const latest = latestSubmissionByQuestion.get(question.id)
  if (!latest) return

  stopSubmitPolling()
  const context = questionContext(question)
  const request = resultRequests.begin()
  const isCurrent = () => isCurrentQuestion(context) && resultRequests.isLatest(request)
  bottomTab.value = 'submit'
  try {
    const res = await judgeAPI.getResult(latest.id)
    if (!isCurrent()) return
    submitResult.value = res.data
    if (['accepted', 'graded'].includes(res.data.status)) completedQuestions.value.add(question.id)
    const draft = drafts.get(question.id)
    if (draft && !draft.edited && typeof res.data.code === 'string') {
      draft.code = res.data.code
      draft.baseline = res.data.code
    }
    if (!isSubmissionComplete(res.data, question.grading_mode || 'legacy')) {
      submitting.value = true
      pollSubmitResult(latest.id, question.id, question.grading_mode || 'legacy', isCurrent)
    }
  } catch {
    // 历史结果回载失败不阻塞做题；用户仍可从提交记录页查看。
  }
}

function syncScroll() {
  const editor = document.getElementById('code-editor')
  const gutter = document.getElementById('code-gutter')
  if (editor && gutter) gutter.scrollTop = editor.scrollTop
}

// ── Self-test ─────────────────────────────────────────────────────────
// sample-run 同步返回结果，无需轮询
async function handleSelfTest() {
  const q = questions.value[activeQ.value]
  if (!q) return
  refreshDeadlineClock()
  if (deadlineClosed.value) {
    app.showToast('作业已截止，请联系教师延长截止时间后再试', 'error')
    return
  }
  const context = questionContext(q)
  const request = sampleRequests.begin()
  const isCurrent = () => isCurrentQuestion(context) && sampleRequests.isLatest(request)
  testing.value = true
  testResult.value = null
  try {
    const res = await judgeAPI.sampleRun(q.id, {
      question_id: q.id,
      code: code.value,
    })
    if (!isCurrent()) return
    // sample-run 直接返回 {output, status, execution_time_ms}，无 submission ID
    testResult.value = res.data
  } catch (e) {
    if (!isCurrent()) return
    if (isDeadlineError(e)) await refreshAssignment(isCurrent)
    if (!isCurrent()) return
    const msg = e.response?.data?.detail?.message || '自测请求失败'
    app.showToast(msg, 'error')
  } finally {
    if (isCurrent()) testing.value = false
  }
}

// ── Submit ────────────────────────────────────────────────────────────
async function handleSubmit() {
  const q = questions.value[activeQ.value]
  if (!q) return
  refreshDeadlineClock()
  if (deadlineClosed.value) {
    app.showToast('作业已截止，请联系教师延长截止时间后再试', 'error')
    return
  }
  stopSubmitPolling()
  const context = questionContext(q)
  const request = resultRequests.begin()
  const isCurrent = () => isCurrentQuestion(context) && resultRequests.isLatest(request)
  const sequence = ++submissionSequence
  submitting.value = true
  submitResult.value = null
  bottomTab.value = 'submit'
  const submittingQId = q.id
  const submittedCode = code.value
  try {
    const res = await judgeAPI.submit({ question_id: q.id, code: submittedCode })
    if (!pageActive) return
    if (res.data.id != null) {
      // A received acknowledgement belongs to its question even after a switch,
      // but an older request cannot undo a newer acknowledged submission.
      if (sequence > (acknowledgedSubmissions.get(submittingQId) || 0)) {
        acknowledgedSubmissions.set(submittingQId, sequence)
        latestSubmissionByQuestion.set(submittingQId, { ...res.data, question_id: submittingQId })
        const draft = drafts.get(submittingQId)
        if (draft) {
          draft.baseline = submittedCode
          draft.edited = true
        }
      }
      if (isCurrent()) pollSubmitResult(res.data.id, submittingQId, q.grading_mode || 'legacy', isCurrent)
    } else if (isCurrent()) {
      app.showToast('提交未返回有效ID', 'error')
    }
  } catch (e) {
    if (!isCurrent()) return
    if (isDeadlineError(e)) await refreshAssignment(isCurrent)
    if (!isCurrent()) return
    const msg = e.response?.data?.detail?.message || '提交失败'
    app.showToast(msg, 'error')
  } finally {
    if (isCurrent() && !submitPolling.value) submitting.value = false
  }
}

let submitPollTimer = null

// Resizable editor
const editorHeight = ref(300)
let editorDragStartY = 0
let editorDragStartH = 0
let editorDragging = false

function onEditorDragStart(e) {
  e.preventDefault()
  editorDragging = true
  editorDragStartY = e.clientY
  const el = document.getElementById('code-editor')
  editorDragStartH = el?.offsetHeight || editorHeight.value
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'ns-resize'
  document.addEventListener('mousemove', onEditorDragMove)
  document.addEventListener('mouseup', onEditorDragEnd)
}

function onEditorDragMove(e) {
  if (!editorDragging) return
  const delta = e.clientY - editorDragStartY
  if (Math.abs(delta) < 5) return
  e.preventDefault()
  editorHeight.value = Math.max(120, editorDragStartH + delta)
}

function onEditorDragEnd() {
  editorDragging = false
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
  document.removeEventListener('mousemove', onEditorDragMove)
  document.removeEventListener('mouseup', onEditorDragEnd)
}

function isSubmissionComplete(result, gradingMode) {
  if (!result) return false
  // 后端终态（人工定分/进入复核/completed）优先：停止轮询，避免"判题超时"（A06）
  if (result.processing_terminal) return true
  if (TERMINAL_ERROR_STATUSES.includes(result.status)) return true
  if (gradingMode === 'active') {
    // CodeGrade 与 Submission 分两次落库时可能短暂出现 graded 但尚无明细；
    // 此时继续轮询，避免学生永远错过 AI 评分结果。
    return result.status === 'graded' && Boolean(result.grading_breakdown)
  }
  return ['accepted', 'graded'].includes(result.status)
}

function pollSubmitResult(submissionId, questionId, gradingMode, isCurrent) {
  if (!isCurrent()) return
  submitPolling.value = true
  let submitPollCount = 0
  const poll = async () => {
    if (!isCurrent()) return
    submitPollTimer = null
    submitPollCount++
    try {
      const res = await judgeAPI.getResult(submissionId)
      if (!isCurrent()) return
      submitResult.value = res.data
      if (isSubmissionComplete(res.data, gradingMode)) {
        stopSubmitPolling()
        submitting.value = false
        submitPolling.value = false
        if (['accepted', 'graded'].includes(res.data.status) && questionId != null) {
          completedQuestions.value.add(questionId)
        }
        return
      }
    } catch { /* ignore */ }
    if (!isCurrent()) return
    if (submitPollCount >= MAX_POLL_COUNT) {
      stopSubmitPolling()
      submitting.value = false
      submitPolling.value = false
      app.showToast('判题超时，请重试', 'error')
      return
    }
    submitPollTimer = setTimeout(poll, 1000)
  }
  submitPollTimer = setTimeout(poll, 1000)
}

function stopSubmitPolling() {
  resultRequests.invalidate()
  if (submitPollTimer) { clearTimeout(submitPollTimer); submitPollTimer = null }
  submitPolling.value = false
}
</script>

<template>
  <AppLayout content-mode="workspace">
    <div v-if="!assignment" class="empty-state">
      <p class="skeleton" style="width:240px;height:28px;margin:0 auto 12px"></p>
      <p class="skeleton" style="width:360px;height:14px;margin:0 auto"></p>
    </div>

    <template v-else>
      <!-- ── Header ──────────────────────────────────────────────────── -->
      <div class="editor-header">
        <button class="btn-ghost btn-sm back-btn" @click="router.push(`/student/courses/${assignment.course_id}`)">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M10 3L5 8l5 5"/>
          </svg>
          返回
        </button>
        <div class="header-info">
          <h1 class="assignment-title">{{ assignment.title }}</h1>
          <span class="question-nav" v-if="questions.length > 1">
            <button
              v-for="(q, i) in questions"
              :key="q.id"
              class="q-dot"
              :class="{ active: i === activeQ, done: completedQuestions.has(q.id) }"
              @click="selectQuestion(i)"
              :title="q.title"
            >{{ i + 1 }}</button>
          </span>
        </div>
      </div>

      <div class="deadline-banner" :class="{ closed: deadlineClosed }" role="status">
        <div>
          <strong>{{ deadlineClosed ? '作业已截止' : (assignment.due_at ? '作业进行中' : '长期开放') }}</strong>
          <span>{{ assignment.due_at ? `截止时间：${formatDateTime(assignment.due_at)}` : '本作业未设置截止时间' }}</span>
        </div>
        <small v-if="deadlineClosed">教师延长截止时间后，重新聚焦或刷新页面即可继续自测和提交。</small>
      </div>

      <div class="assignment-workspace">
      <!-- ── Problem Description ─────────────────────────────────────── -->
      <div class="problem-card" :class="{ collapsed: !showProblem }">
        <button type="button" class="problem-header" :aria-expanded="showProblem" @click="showProblem = !showProblem">
          <div class="problem-label">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2 3h12v10H2z"/><path d="M5 6h6M5 9h4"/>
            </svg>
            题目描述
          </div>
          <span class="btn-ghost btn-sm collapse-btn" :title="showProblem ? '收起' : '展开'">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path :d="showProblem ? 'M3 9l4-4 4 4' : 'M3 5l4 4 4-4'"/>
            </svg>
          </span>
        </button>
        <transition name="problem-collapse">
          <div v-if="showProblem" class="problem-body">
            <div class="problem-desc" v-if="descriptionHtml"
              v-html="descriptionHtml"></div>
            <!-- Phase 5：环境提示（学生无需选择/切换/确认，只读展示） -->
            <div class="env-banner" v-if="currentEnvSummary">
              <div class="env-line">
                <span class="env-label">{{ envLabel }}：</span>
                <span class="env-value">{{ currentEnvSummary.display_name }} {{ currentEnvSummary.version_label }}</span>
              </div>
              <div class="env-line" v-if="envImportsText">
                <span class="env-label">可用库：</span>
                <span class="env-value env-imports">{{ envImportsText }}</span>
              </div>
              <div class="env-line" v-if="envAllowedText">
                <span class="env-label">本题允许导入：</span>
                <span class="env-value env-imports">{{ envAllowedText }}</span>
              </div>
            </div>
            <div class="problem-meta" v-if="questions[activeQ]">
              <div class="meta-item" v-if="questions[activeQ]?.signature || questions[activeQ]?.function_name">
                <span class="meta-label">函数签名</span>
                <code class="meta-code">{{ questions[activeQ]?.signature || questions[activeQ]?.function_name }}</code>
              </div>
              <div class="meta-item" v-if="questions[activeQ]?.public_cases?.length">
                <span class="meta-label">公开样例</span>
                <code class="meta-code pre">{{ publicCasesPretty }}</code>
              </div>
            </div>
          </div>
        </transition>
      </div>

      <div v-if="questions.length === 0" class="empty-state">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1" opacity="0.3">
          <rect x="8" y="8" width="32" height="32" rx="4"/>
          <path d="M18 20h12M18 26h8"/>
        </svg>
        <p>暂无题目</p>
      </div>

      <!-- ── Code Editor ─────────────────────────────────────────────── -->
      <div class="editor-panel" v-if="questions.length > 0">
        <!-- File tab bar -->
        <div class="file-tabs">
          <div class="file-tab active">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" class="file-icon">
              <path d="M3.5 1.5h3l3 3v6.5H3.5V1.5z" stroke="currentColor" stroke-width="0.8" fill="none"/>
              <path d="M6.5 1.5V4a.5.5 0 00.5.5h2.5" stroke="currentColor" stroke-width="0.8" fill="none"/>
            </svg>
            {{ questions[activeQ]?.title || 'solution' }}.py
            <span class="file-close">
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round">
                <path d="M2 2l6 6M8 2l-6 6"/>
              </svg>
            </span>
          </div>
        </div>

        <!-- Editor body -->
        <div class="editor-body" :style="{ height: editorHeight + 'px' }">
          <div class="editor-gutter" id="code-gutter">
            <pre>{{ lineNumbers }}</pre>
          </div>
          <textarea
            id="code-editor"
            class="editor-textarea"
            :class="{ completed: currentCompleted }"
            v-model="code"
            @scroll="syncScroll"
            @keydown.tab.prevent="code += '    '"
            spellcheck="false"
            :placeholder="currentCompleted ? '该题目已完成' : '# 在这里编写 Python 代码...'"
            :disabled="currentCompleted"
          ></textarea>
        </div>

        <!-- Editor resize handle -->
        <div class="editor-resize-handle" @mousedown="onEditorDragStart">
          <div class="editor-resize-grip"></div>
        </div>

      <!-- ── Action Buttons ───────────────────────────────────────────── -->
      <div class="action-bar" v-if="questions.length > 0">
        <div v-if="currentCompleted" class="completed-badge">✓ 已完成</div>
        <template v-else>
        <button
          class="btn-self-test"
          :disabled="testing || submitting || deadlineClosed"
          @click="handleSelfTest"
        >
          <svg v-if="!testing" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M5 2.5L1.5 8 5 13.5"/><path d="M11 2.5l3.5 5.5L11 13.5"/><path d="M9 1.5l-2 13"/>
          </svg>
          <span v-if="testing" class="spinner-sm"></span>
          {{ testing ? '运行中...' : '自测' }}
        </button>
        <button
          class="btn-submit-code"
          :disabled="testing || submitting || deadlineClosed"
          @click="handleSubmit"
        >
          <svg v-if="!submitting" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M8 1.5v9M3.5 6.5L8 11l4.5-4.5"/><path d="M2.5 13.5v1.5h11V13.5"/>
          </svg>
          <span v-if="submitting" class="spinner-sm"></span>
          {{ submitting ? '提交中...' : '提交代码' }}
        </button>
        </template>
      </div>

        <!-- Bottom action tabs -->
        <div class="bottom-section">
          <div class="bottom-tabs">
            <button
              class="bottom-tab"
              :class="{ active: bottomTab === 'self-test' }"
              @click="bottomTab = 'self-test'"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4.5 2L1 7l3.5 5"/><path d="M9.5 2L13 7l-3.5 5"/><path d="M8 1l-2 12"/>
              </svg>
              自测
            </button>
            <button
              class="bottom-tab"
              :class="{ active: bottomTab === 'submit' }"
              @click="bottomTab = 'submit'"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
                <path d="M7 1v8M3 5l4 4 4-4"/><path d="M2 11v1.5h10V11"/>
              </svg>
              提交
            </button>
          </div>

          <!-- Self-test content -->
          <transition name="tab-fade">
            <div v-if="bottomTab === 'self-test'" class="tab-content" key="self-test">
              <!-- Terminal output -->
              <div class="terminal-panel" v-if="testResult || testing">
                <div class="terminal-header">
                  <span class="terminal-dot red"></span>
                  <span class="terminal-dot yellow"></span>
                  <span class="terminal-dot green"></span>
                  <span class="terminal-title">运行输出</span>
                </div>
                <div class="terminal-body">
                  <div v-if="testing" class="terminal-line muted">
                    <span class="prompt">$</span> 运行测试中...
                  </div>
                  <template v-else>
                    <!-- Phase 5：import 诊断优先显示安全中文文案，不展示裸 traceback -->
                    <div v-if="testDiagnostic" class="terminal-line error">
                      <span class="check">✗</span>
                      {{ testDiagnostic.message }}
                    </div>
                    <div class="terminal-line muted">
                      <span class="prompt">$</span> pytest test_{{ questions[activeQ]?.function_name || 'solution' }}.py
                    </div>
                    <div
                      v-if="testResult?.status && TEST_STATUS_LABEL[testResult.status]"
                      class="terminal-line"
                      :class="TEST_STATUS_CLASS[testResult.status] || 'error'"
                    >
                      <span class="check">{{ TEST_STATUS_ICON[testResult.status] || '✗' }}</span>
                      {{ TEST_STATUS_LABEL[testResult.status] }}
                      <span v-if="testResult.execution_time_ms != null" class="time">({{ testResult.execution_time_ms }}ms)</span>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <!-- Submit content -->
            <div v-else class="tab-content submit-tab" key="submit">
              <div v-if="!submitResult && !submitPolling" class="submit-hint">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="8" cy="8" r="7"/><path d="M8 5v3M8 11.5v.01"/>
                </svg>
                <span>提交后不可修改，建议先通过自测验证代码正确性</span>
              </div>
              <!-- Submit result -->
              <div v-if="submitPolling || submitResult" class="submit-result-card" :class="{
                'result-pass': submitSucceeded,
                'result-fail': !submitPolling && submitResult && !submitSucceeded,
                'result-pending': submitPolling
              }">
                <div v-if="submitPolling" class="result-status">
                  <span class="spinner-sm"></span>
                  {{ currentGradingMode === 'active' ? 'AI 评分中...' : '判题中...' }}
                </div>
                <template v-else>
                  <div class="result-status">
                    <span>{{ SUBMIT_STATUS_LABEL[submitResult?.status] || '✗ ' + submitResult?.status }}</span>
                  </div>
                  <!-- Phase 5：结构化诊断优先显示安全中文文案（IMPORT_NOT_ALLOWED 等），
                       不把底层 Docker 信息或裸 traceback 作为主要提示 -->
                  <div v-if="submitDiagnostic" class="result-diagnostic">
                    <span class="diag-icon">⚠</span>
                    {{ submitDiagnostic.message }}
                  </div>
                  <div class="result-meta">
                    <span v-if="submitResult?.execution_time_ms != null">{{ submitResult.execution_time_ms }}ms</span>
                  </div>
                </template>
              </div>

              <StudentAIGradingResult
                v-if="submitResult?.grading_breakdown"
                :breakdown="submitResult.grading_breakdown"
              />

              <div
                v-if="!submitResult?.grading_breakdown && currentGradingMode === 'shadow' && submitResult?.status === 'accepted'"
                class="shadow-mode-note"
              >
                <strong>影子评分：</strong>AI 评分结果仅供教师复核，不向学生展示，也不影响本次成绩。
              </div>
            </div>
          </transition>
        </div>
      </div>

      </div>
    </template>
    <ConfirmDialog
      v-if="leaveTarget"
      title="有尚未提交的代码"
      message="未提交的修改仅保留在当前页面。离开后这些修改将丢失，确定离开吗？"
      confirm-text="放弃草稿并离开"
      cancel-text="继续编辑"
      @confirm="confirmLeave"
      @cancel="leaveTarget = null"
    />
  </AppLayout>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════════
   Assignment Detail — IDE-style code workspace
   Design: "Precision Instrument" palette
   ═══════════════════════════════════════════════════════════════════════ */

/* ── Editor Header ───────────────────────────────────────────────────── */
.editor-header {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
}

.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  color: var(--muted);
  font-size: var(--text-sm);
}
.back-btn:hover { color: var(--fg); }

.header-info {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-width: 0;
}

.assignment-title {
  font-family: var(--font-display);
  font-size: var(--text-xl);
  font-weight: 600;
  color: var(--fg);
  letter-spacing: -0.01em;
  margin: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.question-nav { display: flex; gap: 4px; flex-shrink: 0; }

.q-dot {
  width: 26px;
  height: 26px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface);
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--muted);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  transition: all var(--duration-fast) var(--ease-out);
}
.q-dot:hover { border-color: var(--accent); color: var(--accent); }
.q-dot.active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--surface);
}
.q-dot.done {
  background: var(--success-bg);
  border-color: var(--success);
  color: var(--success);
}

.deadline-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
  padding: 11px 14px;
  border: 1px solid var(--info-bg);
  border-radius: var(--radius-md, 8px);
  background: var(--accent-soft);
  color: var(--accent-hover);
}
.deadline-banner>div { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.deadline-banner strong { font-size: var(--text-sm); white-space: nowrap; }
.deadline-banner span,.deadline-banner small { font-size: var(--text-xs, 12px); line-height: 1.5; }
.deadline-banner.closed { border-color: var(--danger-bg); background: var(--danger-bg); color: var(--danger); }
.deadline-banner.closed small { color: var(--danger); text-align: right; }

/* ── Problem Card ────────────────────────────────────────────────────── */
.problem-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  margin-bottom: var(--space-4);
  transition: border-color var(--duration-normal) var(--ease-out);
}
.problem-card:hover { border-color: var(--border); }

.problem-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-3) var(--space-5);
  cursor: pointer;
  user-select: none;
}
.problem-header:hover { background: var(--surface-subtle); }
.problem-header:first-child { border-radius: var(--radius-lg) var(--radius-lg) 0 0; }

.problem-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
  letter-spacing: 0.01em;
}
.problem-label svg { color: var(--muted); }

.collapse-btn {
  color: var(--muted);
  padding: 2px 6px;
  width: auto;
}

.problem-body {
  padding: 0 var(--space-5) var(--space-5);
  border-top: 1px solid var(--border);
}

.problem-desc {
  font-size: var(--text-sm);
  line-height: 1.75;
  color: var(--fg);
  padding-top: var(--space-4);
}
.problem-desc :deep(strong) { color: var(--fg); font-weight: 600; }
.problem-desc :deep(code) {
  background: var(--surface-subtle);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.85em;
  color: var(--danger);
}

.problem-meta { margin-top: var(--space-4); display: flex; flex-direction: column; gap: var(--space-3); }

/* Phase 5：学生端环境提示（只读展示，学生无需选择/确认） */
.env-banner {
  margin-top: var(--space-4);
  padding: var(--space-3) var(--space-4);
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-md);
  display: flex; flex-direction: column; gap: 6px;
  font-size: var(--text-sm);
}
.env-line { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.env-label { color: var(--muted); font-weight: 500; white-space: nowrap; }
.env-value { color: var(--fg); font-weight: 600; }
.env-imports { color: var(--muted); font-weight: 400; font-family: var(--font-mono); font-size: 0.92em; }

.meta-item { display: flex; flex-direction: column; gap: 4px; }

.meta-label {
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
}

.meta-code {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  background: var(--surface-subtle);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  color: var(--fg);
  line-height: 1.55;
}
.meta-code.pre { white-space: pre; overflow-x: auto; }

/* Collapse transition */
.problem-collapse-enter-active,
.problem-collapse-leave-active {
  transition: all var(--duration-slow) var(--ease-out);
  overflow: hidden;
}
.problem-collapse-enter-from,
.problem-collapse-leave-to {
  opacity: 0;
  max-height: 0;
  padding-top: 0;
  padding-bottom: 0;
}
.problem-collapse-enter-to,
.problem-collapse-leave-from {
  opacity: 1;
  max-height: 800px;
}

/* ── Editor Panel ────────────────────────────────────────────────────── */
.editor-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  transition: border-color var(--duration-normal) var(--ease-out);
  box-shadow: var(--shadow-sm);
}
.editor-panel:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px oklch(0.52 0.095 158 / 0.1);
}

/* File tabs */
.file-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 6px var(--space-3) 0;
  background: var(--surface-subtle);
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
}

.file-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 12px;
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--muted);
  background: var(--surface-subtle);
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  border: 1px solid transparent;
  cursor: default;
  white-space: nowrap;
  transition: all var(--duration-fast) var(--ease-out);
}
.file-tab.active {
  background: var(--surface);
  color: var(--fg);
  border-color: var(--border);
  border-bottom-color: var(--surface);
  margin-bottom: -1px;
}

.file-icon { color: var(--accent); flex-shrink: 0; }

.file-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  margin-left: 2px;
  color: var(--muted);
  opacity: 0.5;
  transition: all var(--duration-fast);
}
.file-close:hover { opacity: 1; background: var(--border); }

/* Editor body */
.editor-body {
  display: flex;
  position: relative;
  min-height: 120px;
  height: 300px;
}

/* Editor resize handle */
.editor-resize-handle {
  height: 4px;
  background: var(--border);
  cursor: ns-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  user-select: none;
  position: relative;
}
.editor-resize-handle::before {
  content: '';
  position: absolute;
  inset: -4px 0;
}
.editor-resize-handle:hover,
.editor-resize-handle:active {
  background: var(--border-strong);
}
.editor-resize-grip {
  width: 48px;
  height: 3px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  opacity: 0;
  transition: opacity var(--duration-fast) var(--ease-out);
}
.editor-resize-handle:hover .editor-resize-grip,
.editor-resize-handle:active .editor-resize-grip {
  opacity: 0.8;
}

.editor-gutter {
  flex-shrink: 0;
  width: 48px;
  background: var(--fg);
  border-right: 1px solid oklch(0.32 0.02 155);
  padding: var(--space-3) 0;
  overflow: hidden;
  user-select: none;
}
.editor-gutter pre {
  padding: 0 var(--space-2);
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.7;
  color: var(--muted);
  text-align: right;
  margin: 0;
}

.editor-textarea {
  flex: 1;
  background: var(--fg);
  color: var(--border);
  border: none;
  border-radius: 0;
  padding: var(--space-3) var(--space-4);
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.7;
  resize: none;
  tab-size: 4;
  overflow: auto;
  transition: none;
  box-shadow: none;
}
.editor-textarea:focus {
  outline: none;
  border: none;
  box-shadow: none;
  background: var(--fg);
}
.editor-textarea::placeholder { color: var(--muted); }
.editor-textarea.completed { opacity: 0.6; cursor: not-allowed; }
.editor-textarea.completed::placeholder { color: var(--success); }

.completed-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 9px 24px; font-size: var(--text-sm); font-weight: 600;
  color: var(--success); background: var(--success-bg);
  border: 1px solid var(--success); border-radius: var(--radius-md);
}

/* ── Bottom Section ──────────────────────────────────────────────────── */
.bottom-section {
  border-top: 1px solid var(--border);
}

.bottom-tabs {
  display: flex;
  gap: 0;
  padding: 0 var(--space-3);
  background: var(--surface-subtle);
  border-bottom: 1px solid var(--border);
}

.bottom-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 8px 16px;
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--muted);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  letter-spacing: 0.02em;
}
.bottom-tab:hover { color: var(--fg); background: var(--border); }
.bottom-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  background: transparent;
}

.tab-content {
  padding: var(--space-4);
}

.tab-fade-enter-active,
.tab-fade-leave-active {
  transition: opacity var(--duration-fast) var(--ease-out);
}
.tab-fade-enter-from,
.tab-fade-leave-to { opacity: 0; }

/* Self-test tab */
.self-test-input-group { margin-bottom: var(--space-4); }

.input-label {
  display: block;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--muted);
  margin-bottom: 5px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.custom-input {
  width: 100%;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg);
  resize: vertical;
  line-height: 1.55;
  transition: border-color var(--duration-fast) var(--ease-out);
}
.custom-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.custom-input::placeholder { color: var(--border-strong); }

/* ── Terminal Panel ──────────────────────────────────────────────────── */
.terminal-panel {
  background: var(--fg);
  border: 1px solid oklch(0.32 0.02 155);
  border-radius: var(--radius-md);
  overflow: hidden;
  box-shadow: inset 0 1px 0 oklch(0.99 0.001 95 / 0.03);
}

.terminal-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: oklch(0.32 0.02 155);
  border-bottom: 1px solid oklch(0.32 0.02 155);
  user-select: none;
}

.terminal-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.terminal-dot.red    { background: var(--danger); }
.terminal-dot.yellow { background: var(--warning); }
.terminal-dot.green  { background: var(--success); }

.terminal-title {
  margin-left: 8px;
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--faint);
  letter-spacing: 0.03em;
}

.terminal-body {
  padding: var(--space-3) var(--space-4);
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.7;
  min-height: 48px;
  max-height: 220px;
  overflow-y: auto;
}

.terminal-line {
  white-space: pre-wrap;
  word-break: break-all;
}
.terminal-line + .terminal-line { margin-top: 2px; }

.terminal-line .prompt  { color: var(--success); margin-right: 6px; }
.terminal-line .check   { color: var(--success); margin-right: 4px; }
.terminal-line .cross   { color: var(--danger); margin-right: 4px; }
.terminal-line .time    { color: var(--faint); margin-left: 8px; font-size: 11px; }

.terminal-line.muted   { color: var(--faint); }
.terminal-line.success { color: var(--success-bg); }
.terminal-line.error   { color: var(--danger); }
.terminal-line.warning { color: var(--warning); }
.terminal-line.stdout  { color: var(--border); }
.terminal-line.stderr  { color: var(--danger); }

/* ── Submit Tab ──────────────────────────────────────────────────────── */
.submit-tab { padding: var(--space-5) var(--space-4); }

.submit-hint {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  background: var(--warning-bg);
  border: 1px solid var(--warning);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  color: var(--warning);
  line-height: 1.6;
}
.submit-hint svg { flex-shrink: 0; margin-top: 1px; color: var(--warning); }

/* ── Submit result card ───────────────────── */
.submit-result-card {
  padding: var(--space-4); border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--surface);
}
.submit-result-card.result-pending { background: var(--surface-subtle); }

.result-status {
  font-size: var(--text-md); font-weight: 600; margin-bottom: var(--space-2);
  display: flex; align-items: center; gap: var(--space-2);
}
.result-pass .result-status { color: var(--success); }
.result-fail .result-status { color: var(--danger); }

.result-meta {
  font-size: var(--text-sm); color: var(--muted);
  display: flex; gap: var(--space-4);
}

/* Phase 5：结构化诊断（安全中文文案，不展示裸 traceback） */
.result-diagnostic {
  margin: var(--space-2) 0;
  padding: var(--space-2) var(--space-3);
  background: var(--surface);
  border: 1px dashed var(--warning, var(--warning));
  border-radius: var(--radius-sm);
  color: var(--warning, var(--warning));
  font-size: var(--text-sm);
  display: flex; align-items: center; gap: 6px;
  line-height: 1.5;
}
.diag-icon { font-size: 14px; flex-shrink: 0; }

.shadow-mode-note {
  margin-top: var(--space-4);
  padding: var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--surface);
  border: 1px dashed var(--border-strong);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}
.shadow-mode-note strong { color: var(--fg); }

/* ── Action Bar ──────────────────────────────────────────────────────── */
.action-bar {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-5);
  padding-bottom: var(--space-2);
}

.btn-self-test {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 20px;
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--accent);
  background: var(--surface);
  border: 1px solid var(--accent);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  letter-spacing: 0.02em;
}
.btn-self-test:hover {
  background: var(--accent-soft);
  border-color: var(--accent-hover);
  color: var(--accent-hover);
}
.btn-self-test:disabled { opacity: 0.45; cursor: not-allowed; }

.btn-submit-code {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 24px;
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--surface);
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  letter-spacing: 0.02em;
}
.btn-submit-code:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.btn-submit-code:disabled { opacity: 0.45; cursor: not-allowed; }

/* ── Spinner ─────────────────────────────────────────────────────────── */
.spinner-sm {
  width: 14px;
  height: 14px;
  border: 2px solid oklch(0.99 0.001 95 / 0.25);
  border-top-color: currentColor;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.btn-self-test .spinner-sm {
  border-color: oklch(0.52 0.095 158 / 0.2);
  border-top-color: var(--accent);
}

/* ── Responsive ──────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  .deadline-banner,.deadline-banner>div { align-items: flex-start; flex-direction: column; gap: 4px; }
  .deadline-banner.closed small { text-align: left; }
  .editor-body { min-height: 120px; }
  .editor-textarea { font-size: 12px; }
  .editor-gutter pre { font-size: 12px; }
  .editor-gutter { width: 36px; }
  .action-bar { flex-direction: column; gap: var(--space-2); }
  .btn-self-test,
  .btn-submit-code { width: 100%; justify-content: center; }
}

/* The task and editor share the desktop viewport; output never displaces actions. */
.assignment-workspace { display: grid; grid-template-columns: minmax(300px, .8fr) minmax(0, 1.2fr); gap: var(--space-4); align-items: start; }
.assignment-workspace > * { min-width: 0; }
.assignment-workspace .problem-card { margin: 0; max-height: calc(100dvh - 200px); overflow: auto; overscroll-behavior: contain; }
.problem-header { width: 100%; border: 0; border-radius: 0; justify-content: space-between; text-align: left; }
.assignment-workspace .problem-body { padding: var(--space-4); }
.assignment-workspace .problem-desc { font-size: 16px; line-height: 1.7; }
.assignment-workspace .editor-panel { margin: 0; min-width: 0; }
.assignment-workspace .editor-body { min-height: 300px; max-height: max(300px, calc(100dvh - 360px)); }
.assignment-workspace .editor-textarea { min-width: 0; }
.assignment-workspace .action-bar { margin: 0; padding: 10px 16px; border-bottom: 1px solid var(--border); justify-content: flex-end; }
.assignment-workspace .bottom-section { max-height: max(120px, calc(100dvh - 610px)); overflow: auto; overscroll-behavior: contain; }
.assignment-workspace .file-tab { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.assignment-workspace .meta-code { overflow-x: auto; max-width: 100%; }
@media (max-width: 920px) { .assignment-workspace { grid-template-columns: 1fr; } .assignment-workspace .problem-card { max-height: none; } }
</style>
