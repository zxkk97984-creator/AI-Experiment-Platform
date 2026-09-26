<script setup>
// 学生提交结果（V2）：页头 + 提交面板 + 轮询状态 + AI 评分 + 终端输出。
// 业务与轮询逻辑不变，输出区使用 V2 深色 code/term 视觉。

import { ref, onMounted, onUnmounted } from 'vue'
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

async function fetchResult() {
  try {
    const res = await judgeAPI.getResult(route.params.id)
    submission.value = res.data
    if (isResultComplete(res.data)) {
      polling.value = false
    }
  } catch { /* ignore */ }
}

onMounted(() => {
  fetchResult()
  timer = setInterval(() => { if (polling.value) fetchResult() }, 1500)
})

onUnmounted(() => { if (timer) clearInterval(timer) })
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

      <div v-if="!submission" class="empty">
        <div class="empty-mark"><span class="skeleton" style="width: 20px; height: 20px;"></span></div>
        <h3>正在读取提交结果</h3>
      </div>

      <template v-else>
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
