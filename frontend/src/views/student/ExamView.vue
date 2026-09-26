<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import { examsAPI } from '../../api/exams.js'
import { useAppStore } from '../../stores/app.js'
import { useServerClock } from '../../composables/useServerClock.js'
import { formatDateTime } from '../../utils/format.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const examId = Number(route.params.id)

const loading = ref(true)
const session = ref(null)
const exam = computed(() => session.value?.exam || null)
const submission = computed(() => session.value?.submission || null)
const questions = computed(() => session.value?.questions || [])
const visibility = computed(() => session.value?.visibility || {})
const savedAnswerMap = computed(() => new Map((session.value?.saved_answers || []).map((answer) => [answer.question_id, answer])))
const answers = ref({})
const versions = ref({})
const codeRunVersions = ref({})
const codeRuns = ref({})
const pending = ref({})
const saveState = ref('saved')
const locked = ref(false)
const warningVisible = ref(false)
const dialog = ref(null)
const secondaryTab = ref(false)
const autoSubmitting = ref(false)
const currentQuestion = ref(null)
const allowLeave = ref(false)
let debounceTimer = null
let fallbackTimer = null
let warningTimer = null
let channel = null
const joinedAt = performance.now()
const tabId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`

const queueKey = computed(() => `exam-answer-queue:${examId}:${submission.value?.id || 'pending'}`)
const codeRunKey = computed(() => `exam-code-runs:${examId}:${submission.value?.id || 'pending'}`)
const active = computed(() => submission.value?.status === 'started' && exam.value?.student_status === 'in_progress')
const completed = computed(() => Boolean(submission.value && submission.value.status !== 'started'))
const scoreVisible = computed(() => Boolean(submission.value?.score_visible && submission.value?.score != null))
const canEdit = computed(() => active.value && !locked.value && !secondaryTab.value && !autoSubmitting.value)
const saveLabel = computed(() => ({
  saving: '保存中…', saved: '已保存', pending: '等待保存', offline: '离线待同步', error: '保存失败',
}[saveState.value] || '已保存'))
const saveTone = computed(() => ['offline', 'error'].includes(saveState.value) ? 'danger' : saveState.value)

const { nowMs, calibrate } = useServerClock(async () => {
  if (loading.value) return null
  const response = await examsAPI.getSession(examId)
  return response.data.server_now
})

function parseServerTimestamp(value) {
  if (!value) return Number.NaN
  const raw = String(value)
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`
  return Date.parse(normalized)
}

const attemptExpiresAt = computed(() => submission.value?.expires_at || session.value?.expires_at || null)
const secondsLeft = computed(() => {
  if (!active.value) return 0
  const expiresMs = parseServerTimestamp(attemptExpiresAt.value)
  if (!Number.isFinite(expiresMs) || !nowMs.value) return null
  return Math.max(0, Math.ceil((expiresMs - nowMs.value) / 1000))
})
const timeDisplay = computed(() => {
  if (secondsLeft.value == null) return '--:--:--'
  const hours = Math.floor(secondsLeft.value / 3600)
  const minutes = Math.floor((secondsLeft.value % 3600) / 60)
  const seconds = secondsLeft.value % 60
  return [hours, minutes, seconds].map(value => String(value).padStart(2, '0')).join(':')
})
const answeredCount = computed(() => questions.value.filter(question => isAnswered(question, answers.value[question.id])).length)
const unansweredCount = computed(() => Math.max(0, questions.value.length - answeredCount.value))
const hasUnanswered = computed(() => active.value && unansweredCount.value > 0)
const progressPercent = computed(() => questions.value.length ? Math.round(answeredCount.value * 100 / questions.value.length) : 0)

const resultTitle = computed(() => {
  if (submission.value?.status === 'review_required') return '已交卷，等待教师复核'
  if (['submitted', 'grading'].includes(submission.value?.status)) return '已交卷，等待评分'
  if (scoreVisible.value) return '成绩已开放'
  return '考试已完成'
})
const resultMessage = computed(() => {
  if (submission.value?.status === 'review_required') return '评分遇到异常，已转教师人工处理，不会按 0 分计入。'
  if (['submitted', 'grading'].includes(submission.value?.status)) return '系统正在处理试卷，成绩完成后仍需由教师决定是否公开。'
  if (!scoreVisible.value) return '成绩暂未开放，请等待教师通知。'
  return visibility.value.review_released ? '讲评已开放，可在下方查看教师允许公开的内容。' : '逐题讲评尚未开放。'
})

function isAnswered(question, value) {
  if (question.question_type === 'code') {
    const code = String(value || '')
    return code.trim().length > 0 &&
      Object.hasOwn(codeRunVersions.value, question.id) &&
      codeRunVersions.value[question.id] === (versions.value[question.id] || 0)
  }
  if (question.question_type === 'fill_blank') {
    const ids = blankIds(question)
    return ids.length > 0 && ids.every(id => String(value?.[id] || '').trim())
  }
  if (Array.isArray(value)) return value.length > 0
  return String(value || '').trim().length > 0
}

function blankIds(question) {
  return [...String(question.prompt || '').matchAll(/\[\[blank:([A-Za-z0-9_-]+)\]\]/g)].map(match => match[1])
}

function promptSegments(question) {
  const text = String(question.prompt || '')
  const segments = []
  let lastIndex = 0
  for (const match of text.matchAll(/\[\[blank:([A-Za-z0-9_-]+)\]\]/g)) {
    if (match.index > lastIndex) segments.push({ type: 'text', text: text.slice(lastIndex, match.index) })
    segments.push({ type: 'blank', id: match[1] })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) segments.push({ type: 'text', text: text.slice(lastIndex) })
  return segments
}

function typeLabel(type) {
  return { single_choice: '单选题', multi_choice: '多选题', fill_blank: '填空题', code: '编程题' }[type] || type
}

function correctKeys(question) {
  return (question.correct_answer?.correct || []).map(String)
}

function correctOptionLabels(question) {
  return correctKeys(question).map((key) => `${key}：${question.options?.[key] ?? ''}`)
}

function blankAnswers(question) {
  return (question.correct_answer?.blanks || []).map((blank, index) => ({
    id: blank.id || `blank${index + 1}`,
    accepted: blank.accepted_answers || [],
  }))
}

function codeReferenceSolution(question) {
  return question.reference_solution || ''
}

function hydrate(payload, { mergeQueue = true } = {}) {
  calibrate(payload.server_now)
  session.value = payload
  const nextAnswers = {}
  const nextVersions = {}
  for (const question of payload.questions || []) {
    nextAnswers[question.id] = question.question_type === 'fill_blank' ? {} : question.question_type === 'code' ? (question.starter_code || '') : []
  }
  for (const saved of payload.saved_answers || []) {
    const question = (payload.questions || []).find(item => item.id === saved.question_id)
    if (!question) continue
    if (question.question_type === 'fill_blank') nextAnswers[saved.question_id] = saved.text_answers || {}
    else if (question.question_type === 'code') nextAnswers[saved.question_id] = saved.code_answer || ''
    else nextAnswers[saved.question_id] = saved.selected_options || []
    nextVersions[saved.question_id] = saved.version || 0
  }
  answers.value = nextAnswers
  versions.value = nextVersions
  codeRunVersions.value = {}
  restoreCodeRuns()
  currentQuestion.value = payload.questions?.[0]?.id || null
  if (mergeQueue && active.value) restoreLocalQueue()
  if (!active.value) clearLocalQueue()
  if (active.value) activateTabCoordination()
}

async function load() {
  loading.value = true
  try {
    const response = await examsAPI.getSession(examId)
    hydrate(response.data)
    if (response.data.exam?.student_status === 'scheduled') {
      dialog.value = { kind: 'scheduled' }
    }
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '考试信息加载失败', 'error')
  } finally {
    loading.value = false
  }
}

function answerPayload(questionId, value) {
  const question = questions.value.find(item => item.id === Number(questionId))
  const base = { question_id: Number(questionId), expected_version: versions.value[questionId] || 0 }
  if (question?.question_type === 'fill_blank') return { ...base, text_answers: value || {} }
  if (question?.question_type === 'code') return { ...base, code_answer: value || '' }
  return { ...base, selected_options: value || [] }
}

function stageAnswer(questionId, value) {
  if (!canEdit.value) return
  answers.value = { ...answers.value, [questionId]: value }
  const question = questions.value.find(item => item.id === Number(questionId))
  if (question?.question_type === 'code' && Object.hasOwn(codeRunVersions.value, questionId)) {
    const nextRuns = { ...codeRunVersions.value }
    delete nextRuns[questionId]
    codeRunVersions.value = nextRuns
    persistCodeRuns()
  }
  pending.value = { ...pending.value, [questionId]: value }
  saveState.value = navigator.onLine ? 'pending' : 'offline'
  persistLocalQueue()
  window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(flushPending, 800)
}

function stageBlank(questionId, blankId, value) {
  stageAnswer(questionId, { ...(answers.value[questionId] || {}), [blankId]: value })
}

async function flushPending() {
  const entries = Object.entries(pending.value)
  if (!entries.length || !active.value || secondaryTab.value) return { ok: true }
  if (!navigator.onLine) { saveState.value = 'offline'; return { ok: false } }
  saveState.value = 'saving'
  const snapshot = Object.fromEntries(entries)
  try {
    const response = await examsAPI.saveAnswers(examId, entries.map(([id, value]) => answerPayload(id, value)))
    calibrate(response.data.server_now)
    let allOk = true
    const nextPending = { ...pending.value }
    for (const result of response.data.results || []) {
      if (result.ok) {
        versions.value = { ...versions.value, [result.question_id]: result.version }
        if (JSON.stringify(nextPending[result.question_id]) === JSON.stringify(snapshot[result.question_id])) delete nextPending[result.question_id]
      } else {
        allOk = false
        if (result.code === 'ANSWER_VERSION_CONFLICT') {
          app.showToast('检测到另一页面更新了答案，请刷新后核对', 'error')
        }
      }
    }
    pending.value = nextPending
    saveState.value = Object.keys(nextPending).length ? (allOk ? 'pending' : 'error') : 'saved'
    persistLocalQueue()
    return { ok: allOk && Object.keys(nextPending).length === 0 }
  } catch (error) {
    saveState.value = navigator.onLine ? 'error' : 'offline'
    persistLocalQueue()
    return { ok: false, error }
  }
}

function persistLocalQueue() {
  if (!submission.value?.id) return
  if (!Object.keys(pending.value).length) { localStorage.removeItem(queueKey.value); return }
  localStorage.setItem(queueKey.value, JSON.stringify({ submissionId: submission.value.id, answers: answers.value, pending: pending.value }))
}

function restoreLocalQueue() {
  try {
    const raw = localStorage.getItem(queueKey.value)
    if (!raw) return
    const saved = JSON.parse(raw)
    if (saved.submissionId !== submission.value?.id) { clearLocalQueue(); return }
    answers.value = { ...answers.value, ...(saved.answers || {}) }
    pending.value = saved.pending || {}
    if (Object.keys(pending.value).length) {
      saveState.value = navigator.onLine ? 'pending' : 'offline'
      window.setTimeout(flushPending, 100)
    }
  } catch { clearLocalQueue() }
}

function clearLocalQueue() {
  localStorage.removeItem(queueKey.value)
  pending.value = {}
  saveState.value = 'saved'
}

function persistCodeRuns() {
  if (!submission.value?.id) return
  if (!Object.keys(codeRunVersions.value).length) { localStorage.removeItem(codeRunKey.value); return }
  localStorage.setItem(codeRunKey.value, JSON.stringify(codeRunVersions.value))
}

function restoreCodeRuns() {
  try {
    const raw = localStorage.getItem(codeRunKey.value)
    const parsed = raw ? JSON.parse(raw) : {}
    codeRunVersions.value = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    localStorage.removeItem(codeRunKey.value)
    codeRunVersions.value = {}
  }
}

async function runCode(question) {
  if (!canEdit.value || codeRuns.value[question.id]?.running) return
  const code = String(answers.value[question.id] || '')
  codeRuns.value = { ...codeRuns.value, [question.id]: { running: true, result: null } }
  stageAnswer(question.id, code)
  window.clearTimeout(debounceTimer)
  const saved = await flushPending()
  if (!saved.ok) {
    codeRuns.value = { ...codeRuns.value, [question.id]: { running: false, result: { status: 'error', output: '答案保存失败，请检查网络后重试。' } } }
    return
  }
  try {
    const response = await examsAPI.sampleRun(examId, question.id, { code })
    codeRunVersions.value = { ...codeRunVersions.value, [question.id]: versions.value[question.id] || 0 }
    persistCodeRuns()
    codeRuns.value = { ...codeRuns.value, [question.id]: { running: false, result: response.data } }
  } catch (error) {
    const message = error.response?.data?.detail?.message || '自测请求失败'
    codeRuns.value = { ...codeRuns.value, [question.id]: { running: false, result: { status: 'error', output: message } } }
    app.showToast(message, 'error')
  }
}

function runStatusLabel(status) {
  return {
    accepted: '公开样例全部通过', wrong_answer: '公开样例未通过', runtime_error: '代码运行错误',
    time_limit_exceeded: '运行超时', system_error: '判题服务异常', no_public_cases: '暂无公开样例', error: '自测失败',
  }[status] || '自测完成'
}

function activateTabCoordination() {
  if (channel || !('BroadcastChannel' in window)) return
  channel = new BroadcastChannel(`exam-attempt:${examId}:${submission.value?.id}`)
  channel.onmessage = ({ data }) => {
    if (!data || data.sender === tabId) return
    if (data.type === 'hello' && joinedAt <= data.joinedAt) {
      channel.postMessage({ type: 'active', target: data.sender, sender: tabId })
    }
    if (data.type === 'active' && data.target === tabId) secondaryTab.value = true
  }
  channel.postMessage({ type: 'hello', sender: tabId, joinedAt })
}

async function startExam() {
  dialog.value = null
  try {
    const response = await examsAPI.start(examId)
    hydrate(response.data)
    app.showToast('考试已开始，计时以服务器时间为准', 'success')
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '暂时无法开始考试', 'error')
    await load()
  }
}

async function submitExam({ forced = false, confirmed = false } = {}) {
  if (autoSubmitting.value || completed.value) return
  if (!forced && !confirmed) { dialog.value = { kind: hasUnanswered.value ? 'submit-incomplete' : 'submit' }; return }
  dialog.value = null
  autoSubmitting.value = true
  locked.value = true
  if (secondsLeft.value > 0) {
    const saved = await flushPending()
    if (!saved.ok && !forced) {
      app.showToast('仍有答案未成功保存，请检查网络后再交卷', 'error')
      locked.value = false
      autoSubmitting.value = false
      return
    }
  }
  try {
    const response = await examsAPI.submit(examId)
    hydrate(response.data, { mergeQueue: false })
    clearLocalQueue()
    app.showToast(secondsLeft.value === 0 ? '考试时间已到，系统已自动交卷' : '交卷成功', 'success')
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '交卷请求失败，系统会继续重试', 'error')
    if (secondsLeft.value === 0) window.setTimeout(() => submitExam({ forced: true }), 1500)
    else locked.value = false
  } finally {
    autoSubmitting.value = false
  }
}

function confirmDialog() {
  if (dialog.value?.kind === 'start') return startExam()
  if (['submit', 'submit-incomplete'].includes(dialog.value?.kind)) return submitExam({ confirmed: true })
  if (['leave-incomplete', 'leave-unsynced'].includes(dialog.value?.kind)) {
    const target = dialog.value.target
    dialog.value = null
    allowLeave.value = true
    flushPending().finally(() => router.push(target))
    return
  }
  dialog.value = null
  if (exam.value?.student_status === 'scheduled') router.replace('/student/exams')
}

function cancelDialog() {
  if (dialog.value?.kind === 'scheduled') router.replace('/student/exams')
  dialog.value = null
}

function scrollToQuestion(id) {
  currentQuestion.value = id
  document.getElementById(`question-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function onBeforeUnload(event) {
  if (!active.value || (!hasUnanswered.value && !Object.keys(pending.value).length)) return
  event.preventDefault()
  event.returnValue = ''
}

function onOnline() { if (active.value) flushPending() }

watch(secondsLeft, (seconds, previous) => {
  if (!active.value) return
  const warningKey = `exam-one-minute-warning:${submission.value?.id}`
  if (seconds > 0 && seconds <= 60 && (previous > 60 || previous === undefined || !sessionStorage.getItem(warningKey))) {
    sessionStorage.setItem(warningKey, '1')
    warningVisible.value = true
    window.clearTimeout(warningTimer)
    warningTimer = window.setTimeout(() => { warningVisible.value = false }, 3000)
  }
  if (seconds === 0) submitExam({ forced: true })
}, { immediate: true })

onBeforeRouteLeave((to, _from, next) => {
  if (allowLeave.value || !active.value) { next(); return }
  if (hasUnanswered.value) {
    next(false)
    dialog.value = { kind: 'leave-incomplete', target: to.fullPath }
    return
  }
  if (Object.keys(pending.value).length) {
    next(false)
    dialog.value = { kind: 'leave-unsynced', target: to.fullPath }
    return
  }
  next()
})

onMounted(() => {
  load()
  fallbackTimer = window.setInterval(() => { if (active.value) flushPending() }, 10_000)
  window.addEventListener('beforeunload', onBeforeUnload)
  window.addEventListener('online', onOnline)
})

onBeforeUnmount(() => {
  window.clearTimeout(debounceTimer)
  window.clearTimeout(warningTimer)
  window.clearInterval(fallbackTimer)
  window.removeEventListener('beforeunload', onBeforeUnload)
  window.removeEventListener('online', onOnline)
  channel?.close()
})
</script>

<template>
  <AppLayout content-mode="workspace">
    <div v-if="loading" class="loading-card"><div class="skeleton title-line"></div><div class="skeleton text-line"></div></div>

    <section v-else-if="exam" class="exam-page">
      <header class="exam-heading">
        <div>
          <button type="button" class="back-link" @click="router.push('/student/exams')">← 返回考试中心</button>
          <p class="eyebrow">{{ completed ? '考试结果' : active ? '答题进行中' : '考试说明' }}</p>
          <h1>{{ exam.title }}</h1>
        </div>
      </header>

      <div v-if="warningVisible" class="minute-warning" role="alert">
        <strong>考试即将结束</strong><span>剩余不足 1 分钟，请尽快完成并检查答案。</span>
      </div>

      <section v-if="secondaryTab && active" class="tab-warning">
        <strong>此页面已切换为只读</strong>
        <span>检测到同一场考试已在另一个标签页打开，请回到原标签继续作答，避免答案互相覆盖。</span>
      </section>

      <section v-if="completed" class="result-panel">
        <div class="result-mark">✓</div>
        <div class="result-copy"><p class="result-kicker">{{ resultTitle }}</p><h2 v-if="scoreVisible">{{ submission.score }} <span>/ {{ exam.max_score }} 分</span></h2><h2 v-else>已完成</h2><p>{{ resultMessage }}</p></div>
        <dl><div><dt>交卷时间</dt><dd>{{ formatDateTime(submission.submitted_at) }}</dd></div><div><dt>交卷方式</dt><dd>{{ submission.submission_reason === 'time_expired' ? '到时自动交卷' : submission.submission_reason === 'teacher_forced' ? '教师强制交卷' : '主动交卷' }}</dd></div></dl>
      </section>

      <section v-if="completed && visibility.questions" class="review-section">
        <div class="section-head"><div><p class="eyebrow">REVIEW</p><h2>试题讲评</h2></div><span class="review-badge">{{ visibility.answers ? '题目与答案已开放' : '仅题目已开放' }}</span></div>
        <article v-for="(question, index) in questions" :key="question.id" class="review-card">
          <header>
            <strong>第 {{ index + 1 }} 题</strong>
            <div class="review-card-meta">
              <span>{{ typeLabel(question.question_type) }} · {{ question.points }} 分</span>
              <b v-if="visibility.score && savedAnswerMap.get(question.id)?.score != null" class="question-score">本题得分 {{ savedAnswerMap.get(question.id).score }} / {{ question.points }} 分</b>
            </div>
          </header>
          <p class="review-prompt">{{ question.prompt }}</p>
          <div v-if="['single_choice', 'multi_choice'].includes(question.question_type)" class="review-options" aria-label="题目选项">
            <div v-for="(option, key) in question.options || {}" :key="key" class="review-option" :class="{ correct: visibility.answers && correctKeys(question).includes(String(key)) }">
              <b>{{ key }}</b><span>{{ option }}</span><small v-if="visibility.answers && correctKeys(question).includes(String(key))">正确答案</small>
            </div>
          </div>
          <div v-if="visibility.answers" class="standard-answer">
            <span>{{ question.question_type === 'code' ? '参考答案' : '标准答案' }}</span>
            <strong v-if="['single_choice', 'multi_choice'].includes(question.question_type)">{{ correctOptionLabels(question).join('、') || '未配置' }}</strong>
            <ul v-else-if="question.question_type === 'fill_blank'">
              <li v-for="blank in blankAnswers(question)" :key="blank.id"><span>{{ blank.accepted.join(' / ') || '未配置' }}</span></li>
            </ul>
            <pre v-else-if="question.question_type === 'code' && codeReferenceSolution(question)" class="review-code"><code>{{ codeReferenceSolution(question) }}</code></pre>
            <code v-else-if="question.question_type === 'code'">暂未配置参考答案</code>
          </div>
          <div v-if="visibility.score && savedAnswerMap.get(question.id)?.manual_score_reason" class="teacher-score-note">
            <span>教师改分说明</span>
            <p>{{ savedAnswerMap.get(question.id).manual_score_reason }}</p>
            <small>教师改分后得分：{{ savedAnswerMap.get(question.id).score }} / {{ question.points }} 分</small>
          </div>
        </article>
      </section>

      <section v-else-if="!active && exam.student_status === 'ready'" class="start-panel">
        <div class="start-icon">▶</div><h2>准备开始考试</h2>
        <p>确认开始后，系统会立即创建考试记录并按完整时长计时。刷新或退出后可继续，但倒计时不会暂停。</p>
        <dl><div><dt>考试时长</dt><dd>{{ exam.duration_minutes }} 分钟</dd></div><div><dt>满分</dt><dd>{{ exam.max_score }} 分</dd></div><div><dt>最晚进入</dt><dd>{{ formatDateTime(exam.end_at) }}</dd></div></dl>
        <button type="button" class="btn-primary start-button" @click="dialog = { kind: 'start' }">确认并开始考试</button>
      </section>

      <section v-else-if="!active && exam.student_status === 'missed'" class="start-panel missed">
        <div class="start-icon">!</div><h2>已错过最晚进入时间</h2><p>你尚未开始本场考试，系统已将状态标记为缺考。如有特殊情况，请联系任课教师。</p>
      </section>

      <div v-else-if="active" class="workspace">
        <aside class="exam-sidebar">
          <div class="timer" :class="{ urgent: secondsLeft != null && secondsLeft <= 60 }">
            <span>剩余时间</span><strong>{{ timeDisplay }}</strong><small>服务器计时</small>
          </div>
          <div class="save-row"><span class="save-dot" :class="saveTone"></span><strong>{{ saveLabel }}</strong></div>
          <div class="progress-line"><span :style="{ width: progressPercent + '%' }"></span></div>
          <p>已答 {{ answeredCount }} / {{ questions.length }} 题</p>
          <nav aria-label="题目导航">
            <button v-for="(question, index) in questions" :key="question.id" type="button" :class="{ answered: isAnswered(question, answers[question.id]), current: currentQuestion === question.id }" @click="scrollToQuestion(question.id)">{{ index + 1 }}</button>
          </nav>
          <button type="button" class="submit-button" :disabled="autoSubmitting || secondaryTab" @click="submitExam()">{{ autoSubmitting ? '正在交卷…' : '提交试卷' }}</button>
          <small>交卷前会先同步待保存答案</small>
        </aside>

        <section class="questions-column">
          <article v-for="(question, index) in questions" :id="`question-${question.id}`" :key="question.id" class="question-card" @focusin="currentQuestion = question.id">
            <header><div><span class="question-index">{{ String(index + 1).padStart(2, '0') }}</span><span class="type-chip">{{ typeLabel(question.question_type) }}</span></div><strong>{{ question.points }} 分</strong></header>
            <p v-if="question.question_type !== 'fill_blank'" class="prompt">{{ question.prompt }}</p>
            <div v-else class="fill-prompt">
              <template v-for="(segment, segmentIndex) in promptSegments(question)" :key="`${segment.type}-${segmentIndex}`">
                <span v-if="segment.type === 'text'">{{ segment.text }}</span>
                <input v-else :value="answers[question.id]?.[segment.id] || ''" :disabled="!canEdit" :aria-label="`填空 ${segment.id}`" autocomplete="off" @input="stageBlank(question.id, segment.id, $event.target.value)" @blur="flushPending" />
              </template>
            </div>

            <div v-if="question.question_type === 'single_choice'" class="options">
              <label v-for="(option, key) in question.options" :key="key" :class="{ selected: (answers[question.id] || []).includes(key) }"><input type="radio" :name="`q-${question.id}`" :disabled="!canEdit" :checked="(answers[question.id] || []).includes(key)" @change="stageAnswer(question.id, [key])"><b>{{ key }}</b><span>{{ option }}</span></label>
            </div>
            <div v-else-if="question.question_type === 'multi_choice'" class="options">
              <label v-for="(option, key) in question.options" :key="key" :class="{ selected: (answers[question.id] || []).includes(key) }"><input type="checkbox" :disabled="!canEdit" :checked="(answers[question.id] || []).includes(key)" @change="stageAnswer(question.id, (answers[question.id] || []).includes(key) ? answers[question.id].filter(item => item !== key) : [...(answers[question.id] || []), key])"><b>{{ key }}</b><span>{{ option }}</span></label>
            </div>
            <template v-else-if="question.question_type === 'code'">
              <textarea class="code-editor" :value="answers[question.id] || ''" :disabled="!canEdit" rows="12" spellcheck="false" @input="stageAnswer(question.id, $event.target.value)" @blur="flushPending"></textarea>
              <div class="code-actions">
                <button type="button" class="run-button" :data-action="`run-code-${question.id}`" :disabled="!canEdit || codeRuns[question.id]?.running" @click="runCode(question)">
                  {{ codeRuns[question.id]?.running ? '运行中…' : '运行自测' }}
                </button>
                <span>仅运行教师公开样例，不影响最终得分</span>
              </div>
              <div v-if="codeRuns[question.id]?.result" class="run-result" :class="codeRuns[question.id].result.status" role="status">
                <strong>{{ runStatusLabel(codeRuns[question.id].result.status) }}</strong>
                <pre v-if="codeRuns[question.id].result.output">{{ codeRuns[question.id].result.output }}</pre>
              </div>
            </template>
          </article>
        </section>
      </div>
    </section>

    <ConfirmDialog
      v-if="dialog"
      :title="dialog.kind === 'scheduled' ? '考试尚未开始' : dialog.kind === 'start' ? '确认开始考试' : dialog.kind.startsWith('submit') ? '确认提交试卷' : '确认退出考试'"
      :message="dialog.kind === 'scheduled' ? `本场考试将于 ${formatDateTime(exam?.start_at)} 开放，系统以服务器时间为准。` : dialog.kind === 'start' ? `开始后将获得 ${exam?.duration_minutes} 分钟完整作答时间，倒计时不会因退出或刷新暂停。` : dialog.kind === 'submit-incomplete' ? '当前尚有未完成的题目，确定交卷吗' : dialog.kind === 'submit' ? '系统会先保存最新答案。交卷完成后将无法继续修改。' : dialog.kind === 'leave-incomplete' ? '当前尚有未完成的题目，退出将暂存题目' : '仍有答案尚未同步，确定保存并退出吗？'"
      :confirm-text="dialog.kind === 'scheduled' ? '返回考试中心' : dialog.kind === 'start' ? '开始计时' : ['submit-incomplete', 'leave-incomplete'].includes(dialog.kind) ? '确定' : dialog.kind === 'submit' ? '确认交卷' : '保存并离开'"
      :cancel-text="dialog.kind === 'scheduled' ? '关闭' : '取消'"
      :danger="dialog.kind.startsWith('submit')"
      @confirm="confirmDialog"
      @cancel="cancelDialog"
    />
  </AppLayout>
</template>

<style scoped>
.loading-card { padding: 56px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }.title-line{height:22px;width:260px;margin-bottom:14px}.text-line{height:13px;width:380px}
.exam-page { display: flex; flex-direction: column; gap: 24px; }.exam-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px; }.back-link { margin: 0 0 20px; padding: 0; border: 0; background: transparent; color: var(--muted); cursor: pointer; }.eyebrow { margin: 0 0 7px; color: var(--accent); font: 700 11px/1 var(--font-mono); letter-spacing: .14em; }.exam-heading h1 { margin: 0; color: var(--fg); font-size: 29px; letter-spacing: -.03em; }
.timer { min-width: 174px; padding: 12px 18px; border: 1px solid var(--warning-bg); border-radius: var(--radius-lg); background: var(--warning-bg); text-align: center; color: var(--danger); }.timer span,.timer small { display:block;font-size:10px;letter-spacing:.08em}.timer strong { display:block;margin:2px 0;font:750 25px/1.2 var(--font-mono);letter-spacing:.05em}.timer.urgent { color:var(--danger);border-color:var(--danger-bg);background:var(--danger-bg);animation:pulse 1.4s ease-in-out infinite }
.minute-warning { position:fixed;z-index:70;top:22px;left:50%;transform:translateX(-50%);display:flex;gap:14px;align-items:center;min-width:min(520px,calc(100vw - 32px));padding:15px 18px;border-radius:13px;background:var(--danger);color:white;box-shadow:0 14px 35px oklch(0.54 0.20 25 / 0.25); }.minute-warning span { font-size:13px;opacity:.9 }.tab-warning { display:flex;align-items:center;gap:12px;padding:14px 18px;border:1px solid var(--warning-bg);border-radius: var(--radius-lg);background:var(--warning-bg);color:var(--warning);font-size:13px}.tab-warning span{color:var(--warning)}
.result-panel { display:grid;grid-template-columns:auto 1fr minmax(220px,auto);align-items:center;gap:22px;padding:28px;border:1px solid var(--border);border-radius:18px;background:var(--surface);box-shadow:0 10px 30px oklch(0.2 0.01 150 / 0.04)}.result-mark{display:grid;place-items:center;width:52px;height:52px;border-radius: var(--radius-lg);background:var(--success-bg);color:var(--success);font-size:26px}.result-kicker{margin:0 0 5px;color:var(--muted);font-size:12px;font-weight:650}.result-copy h2{margin:0;color:var(--fg);font-size:30px}.result-copy h2 span{font-size:16px;color:var(--muted);font-weight:500}.result-copy>p:last-child{margin:6px 0 0;color:var(--muted);font-size:13px}.result-panel dl,.start-panel dl{margin:0;display:grid;gap:8px}.result-panel dl div,.start-panel dl div{display:flex;justify-content:space-between;gap:30px;font-size:12px}.result-panel dt,.start-panel dt{color:var(--faint)}.result-panel dd,.start-panel dd{margin:0;color:var(--fg);font-weight:600}
.start-panel{max-width:640px;margin:34px auto;padding:42px;border:1px solid var(--border);border-radius:18px;background:var(--surface);text-align:center}.start-icon{display:grid;place-items:center;width:52px;height:52px;margin:0 auto 15px;border-radius: var(--radius-lg);background:var(--accent-soft);color:var(--accent);font-size:18px}.start-panel h2{margin:0 0 10px;color:var(--fg);font-size:21px}.start-panel>p{max-width:510px;margin:0 auto 24px;color:var(--muted);font-size:13px;line-height:1.8}.start-panel dl{padding:18px;border-radius: var(--radius-lg);background:var(--surface-subtle);text-align:left}.start-button{margin-top:22px;padding:11px 30px}.start-panel.missed .start-icon{background:var(--danger-bg);color:var(--danger)}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:22px;align-items:start}.questions-column{grid-column:1;grid-row:1}.exam-sidebar{grid-column:2;grid-row:1;position:sticky;top:calc(var(--header-height) + var(--page-pad));height:calc(100vh - var(--header-height) - 48px);min-width:0;padding:20px;border:1px solid var(--border);border-radius:15px;background:var(--surface);display:flex;flex-direction:column;box-sizing:border-box}.exam-sidebar .timer{min-width:0;width:100%;box-sizing:border-box;margin-bottom:16px}.save-row{display:flex;align-items:center;gap:8px;color:var(--fg);font-size:12px}.save-dot{width:7px;height:7px;border-radius:50%;background:var(--success)}.save-dot.saving,.save-dot.pending{background:var(--warning)}.save-dot.danger{background:var(--danger)}.progress-line{height:6px;margin:16px 0 8px;border-radius: var(--radius-full);background:var(--border);overflow:hidden}.progress-line span{display:block;height:100%;border-radius:inherit;background:var(--accent);transition:width .25s}.exam-sidebar>p,.exam-sidebar>small{color:var(--muted);font-size:11px}.exam-sidebar nav{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin:20px 0;flex:1;align-content:start}.exam-sidebar nav button{box-sizing:border-box;width:100%;min-width:0;min-height:44px;aspect-ratio:1;border:1px solid var(--border);border-radius: var(--radius-md);background:white;color:var(--muted);cursor:pointer}.exam-sidebar nav button.answered{border-color:var(--success-bg);background:var(--success-bg);color:var(--success)}.exam-sidebar nav button.current{outline:2px solid var(--info-bg)}.submit-button{width:100%;padding:10px;border:0;border-radius: var(--radius-md);background:var(--fg);color:white;font-weight:650;cursor:pointer}.submit-button:disabled{opacity:.5}.exam-sidebar>small{display:block;margin-top:9px;text-align:center;line-height:1.5}
.questions-column{display:grid;min-width:0;gap:16px}.question-card{min-width:0;scroll-margin-top:20px;padding:25px;border:1px solid var(--border);border-radius:15px;background:var(--surface)}.question-card>header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.question-card>header>div{display:flex;align-items:center;gap:9px}.question-index{font:750 18px/1 var(--font-mono);color:var(--fg)}.type-chip{padding:4px 8px;border-radius: var(--radius-full);background:var(--accent-soft);color:var(--accent-hover);font-size:10px;font-weight:700}.question-card>header>strong{color:var(--muted);font-size:12px}.prompt,.fill-prompt{margin:0 0 20px;color:var(--fg);font-size:14px;line-height:1.9;white-space:pre-wrap}.fill-prompt input{display:inline-block;width:150px;margin:3px 7px;padding:7px 9px;border:0;border-bottom:2px solid var(--info-bg);background:var(--accent-soft);color:var(--fg);font:600 13px var(--font-sans);outline:none}.fill-prompt input:focus{border-color:var(--accent);background:var(--info-bg)}.options{display:grid;gap:8px}.options label{display:grid;grid-template-columns:auto 28px 1fr;align-items:center;gap:9px;padding:11px 13px;border:1px solid var(--border);border-radius: var(--radius-md);cursor:pointer}.options label.selected{border-color:var(--info-bg);background:var(--accent-soft)}.options input{accent-color:var(--accent)}.options b{color:var(--muted);font-size:12px}.options span{color:var(--fg);font-size:13px}.code-editor{box-sizing:border-box;width:100%;padding:16px;border:1px solid oklch(0.32 0.02 155);border-radius: var(--radius-md);background:var(--fg);color:var(--border);font:13px/1.65 var(--font-mono);resize:vertical;outline:none}.code-editor:focus{border-color:var(--warning);box-shadow:0 0 0 3px oklch(0.66 0.14 75 / 0.14)}.code-actions{display:flex;align-items:center;gap:12px;margin-top:12px}.code-actions span{color:var(--muted);font-size:11px}.run-button{min-height:38px;padding:0 15px;border:1px solid var(--info-bg);border-radius: var(--radius-md);background:var(--accent-soft);color:var(--accent-hover);font-weight:650;cursor:pointer}.run-button:disabled{cursor:not-allowed;opacity:.55}.run-result{margin-top:12px;padding:12px;border:1px solid var(--info-bg);border-radius: var(--radius-md);background:var(--surface-subtle);color:var(--muted);font-size:12px}.run-result.accepted{border-color:var(--success-bg);background:var(--success-bg);color:var(--success)}.run-result strong{display:block}.run-result pre{max-height:180px;margin:8px 0 0;overflow:auto;white-space:pre-wrap;word-break:break-word;font:11px/1.6 var(--font-mono)}
.review-section{display:grid;gap:14px}.section-head{display:flex;justify-content:space-between;align-items:end}.section-head h2{margin:0;color:var(--fg)}.review-badge{padding:6px 10px;border-radius: var(--radius-full);background:var(--success-bg);color:var(--success);font-size:11px}.review-card{padding:20px;border:1px solid var(--border);border-radius:13px;background:var(--surface)}.review-card header{display:flex;justify-content:space-between;align-items:center;gap:12px}.review-card-meta{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap}.review-card header span{color:var(--muted);font-size:12px}.question-score{padding:4px 8px;border-radius:var(--radius-full);background:var(--accent-soft);color:var(--accent-hover);font-size:12px;white-space:nowrap}.review-prompt{white-space:pre-wrap;line-height:1.7}.review-options{display:grid;gap:8px;margin:0 0 12px}.review-option{display:grid;grid-template-columns:30px 1fr auto;align-items:center;gap:9px;padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius-md);color:var(--fg);font-size:13px}.review-option.correct{border-color:var(--success-bg);background:var(--success-bg)}.review-option b{color:var(--muted);font-size:12px}.review-option small{color:var(--success);font-size:11px}.standard-answer{display:flex;gap:12px;padding:12px;border-radius: var(--radius-md);background:var(--success-bg)}.standard-answer>span{flex:none;color:var(--success);font-size:12px}.standard-answer strong,.standard-answer li span{color:var(--success);font-size:13px}.standard-answer ul{display:grid;gap:5px;margin:0;padding:0;list-style:none}.standard-answer li{display:flex;gap:8px;align-items:baseline}.standard-answer code{white-space:pre-wrap;word-break:break-all;color:var(--success)}.standard-answer pre{min-width:0;margin:0;overflow:auto;white-space:pre-wrap;word-break:break-word}.review-code{flex:1}
.teacher-score-note{margin-top:10px;padding:12px;border:1px dashed var(--warning-bg);border-radius: var(--radius-md);background:var(--warning-bg)}.teacher-score-note span{display:block;color:var(--warning);font-size:12px;font-weight:650}.teacher-score-note p{margin:6px 0;color:var(--fg);font-size:13px;line-height:1.7;white-space:pre-wrap}.teacher-score-note small{color:var(--muted);font-size:11px}
@keyframes pulse{50%{transform:scale(1.025)}}
@media(max-width:860px){.workspace{grid-template-columns:1fr}.exam-sidebar{position:static;height:auto;grid-column:auto;grid-row:auto}.questions-column{grid-column:auto;grid-row:auto;margin-top:14px}.exam-sidebar nav{flex:0 0 auto;grid-template-columns:repeat(8,1fr)}.exam-sidebar nav button{min-height:0;aspect-ratio:1}.result-panel{grid-template-columns:auto 1fr}.result-panel dl{grid-column:1/-1;padding-top:15px;border-top:1px solid var(--border)}}
@media(max-width:620px){.exam-heading{align-items:flex-start;flex-direction:column}.workspace{display:block}.exam-sidebar{height:auto}.questions-column{margin-top:14px}.exam-sidebar nav{grid-template-columns:repeat(6,1fr)}.question-card{padding:18px}.result-panel{grid-template-columns:1fr;text-align:center}.result-mark{margin:auto}.minute-warning{align-items:flex-start;flex-direction:column;gap:4px}.start-panel{padding:28px 20px}.fill-prompt input{width:120px}}

@media (min-width: 1100px) {
  .exam-sidebar { top: calc(var(--header-height) + 16px); max-height: calc(100dvh - var(--header-height) - 32px); }
  .prompt, .fill-prompt { font-size: 16px; line-height: 1.7; }
  .options span { font-size: 14px; }
  .question-card { padding: 20px 24px; }
}
</style>
