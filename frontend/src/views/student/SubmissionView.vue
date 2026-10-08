<script setup>
// 学生提交结果（V2）：页头 + 提交面板 + 轮询状态 + AI 评分 + 终端输出。
// 串行轮询，永久错误停止，短暂故障有限退避。

import { ref, watch, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import StudentAIGradingResult from '../../components/ai/StudentAIGradingResult.vue'
import { judgeAPI } from '../../api/judge.js'
import { statusBadge, JUDGE_STATUS_MAP } from '../../utils/status.js'
import { formatDateTime } from '../../utils/format.js'

const route = useRoute()
const submission = ref(null)
const polling = ref(true)
let timer = null

const TERMINAL_STATUSES = ['accepted', 'wrong_answer', 'runtime_error', 'time_limit_exceeded', 'system_error']

function isResultComplete(result) {
  if (TERMINAL_STATUSES.includes(result.status)) return true
  return result.status === 'graded' && Boolean(result.grading_breakdown)
}

const error = ref('')
let active = true
let generation = 0
let inFlight = false
let refreshPending = false
let failures = 0

function stopTimer() {
  if (timer !== null) clearTimeout(timer)
  timer = null
}
function visible() { return document.visibilityState !== 'hidden' }

async function fetchResult() {
  if (!active || !polling.value || !visible() || inFlight) return
  stopTimer()
  inFlight = true
  const current = generation
  let delay = 1500
  try {
    const res = await judgeAPI.getResult(route.params.id)
    if (!active || current !== generation) return
    submission.value = res.data
    failures = 0
    error.value = ''
    polling.value = !isResultComplete(res.data)
  } catch (e) {
    if (!active || current !== generation) return
    const status = e.response?.status
    const permanent = status >= 400 && status < 500 && ![408, 429].includes(status)
    failures++
    polling.value = !permanent && failures < 5
    if (permanent) submission.value = null
    error.value = status === 404 ? '提交不存在或已不可访问'
      : status === 403 ? '无权查看该提交'
        : permanent ? '无法读取提交，请检查登录状态或链接'
          : polling.value ? '结果服务暂不可用，正在重试…' : '暂时无法读取提交结果，请稍后重试'
    delay = Math.min(1500 * 2 ** (failures - 1), 24000)
  } finally {
    inFlight = false
    if (active && refreshPending) {
      refreshPending = false
      fetchResult()
    } else if (active && current === generation && polling.value && visible()) {
      timer = setTimeout(fetchResult, delay)
    }
  }
}

function retry() {
  generation++
  stopTimer()
  submission.value = null
  error.value = ''
  failures = 0
  polling.value = true
  if (inFlight) refreshPending = true
  else fetchResult()
}
function visibilityChanged() {
  if (visible()) fetchResult()
  else stopTimer()
}
watch(() => route.params.id, retry)
onMounted(() => {
  document.addEventListener('visibilitychange', visibilityChanged)
  fetchResult()
})
onUnmounted(() => {
  active = false
  generation++
  stopTimer()
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>

<template>
  <AppLayout content-mode="list">
    <div class="page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">学习 / 提交结果</p>
          <h1>判题结果</h1>
        </div>
      </section>

      <div v-if="error" class="panel error-panel" role="alert">
        <p>{{ error }}</p><button class="btn-ghost" @click="retry">重试</button>
      </div>
      <div v-if="!submission && !error" class="empty">
        <div class="empty-mark"><span class="skeleton" style="width: 20px; height: 20px;"></span></div>
        <h3>正在读取提交结果</h3>
      </div>

      <template v-if="submission">
        <section class="panel submission-panel">
          <div class="panel-head">
            <div class="ph-label"><p class="eyebrow">Submission</p><h3>提交 #{{ submission.id }}</h3></div>
            <span class="badge" :class="'badge-' + statusBadge(JUDGE_STATUS_MAP, submission.status).color">
              <span class="dot"></span>{{ statusBadge(JUDGE_STATUS_MAP, submission.status).label }}
            </span>
          </div>
          <div class="panel-body">
            <div class="row-wrap">
              <span class="meta">状态：{{ submission.status }}</span>
              <span v-if="submission.score != null" class="meta">得分：<strong>{{ submission.score }}</strong></span>
              <span v-if="submission.execution_time_ms != null" class="meta">执行时间：<strong>{{ submission.execution_time_ms }}ms</strong></span>
              <span class="meta">提交时间：{{ formatDateTime(submission.created_at) }}</span>
            </div>
          </div>
        </section>

        <div v-if="submission.diagnostic" class="error-panel diagnostic-card">
          <div class="grow"><div class="e-title">运行诊断</div><div class="e-body">{{ submission.diagnostic.message }}</div></div>
        </div>

        <div v-if="polling" class="row polling-hint" role="status">
          <span class="score-bar grow"><i style="width: 70%;"></i></span>
          <span class="meta">判题进行中，自动刷新中…</span>
        </div>

        <StudentAIGradingResult
          v-if="submission.grading_breakdown"
          :breakdown="submission.grading_breakdown"
        />

        <div v-if="submission.stdout" class="term">
          <div class="term-head"><span class="term-dot"></span><span class="term-dot"></span><span class="term-dot"></span><span class="t-title">标准输出</span></div>
          <pre>{{ submission.stdout }}</pre>
        </div>
        <div v-if="submission.stderr" class="term">
          <div class="term-head"><span class="term-dot"></span><span class="term-dot"></span><span class="term-dot"></span><span class="t-title">标准错误</span></div>
          <pre class="fail">{{ submission.stderr }}</pre>
        </div>
      </template>
    </div>
  </AppLayout>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--space-4); }
.submission-panel .panel-body { padding: 14px 16px; }
.polling-hint { padding: 8px 0; }
.polling-hint .score-bar { max-width: 220px; }

.term pre { max-height: 420px; overflow: auto; overscroll-behavior: contain; }
.submission-panel .row-wrap { gap: 12px 24px; }
</style>
