<script setup>
// 学生练习作答页：编辑代码 → 提交（202）→ 排队/判题 → 测试结果与 AI 反馈 → 异议。
// 六态：加载 / 空数据（题目不存在或已归档）/ 失败 / 排队 / 额度耗尽 / 权限不足。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import PracticeCodeEditor from '../../components/practice/PracticeCodeEditor.vue'
import PracticeSubmissionFeedback from '../../components/practice/PracticeSubmissionFeedback.vue'
import { usePracticeLoad } from '../../components/practice/usePracticeLoad.js'
import {
  difficultyMeta, gradingModeMeta, isActiveStatus, newClientRequestId,
  practiceErrorMessage,
} from '../../components/practice/practiceHelpers.js'
import { practiceAPI } from '../../api/practice.js'
import { useAppStore } from '../../stores/app.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const problemId = computed(() => route.params.id)
const problem = ref(null)
const submission = ref(null)
const code = ref('')
const submitting = ref(false)
const submitError = ref('')
const quotaBlocked = ref(false)
const missing = ref(false)
const lastPayload = ref(null)
let pollTimer = null
let pollCount = 0
const POLL_MS = 3000
const MAX_POLLS = 200

const { loading, error, forbidden, quotaExhausted, load: loadProblem } = usePracticeLoad(async () => {
  const res = await practiceAPI.getProblem(problemId.value)
  problem.value = res.data
  if (!code.value) code.value = res.data?.starter_code || ''
  if (res.data?.latest_submission_id && !submission.value) await loadSubmission(res.data.latest_submission_id)
  return res
}, { fallbackMessage: '题目加载失败，请稍后重试' })

const difficulty = computed(() => difficultyMeta(problem.value?.difficulty))
const mode = computed(() => gradingModeMeta(problem.value?.grading_mode))
const archived = computed(() => problem.value?.status === 'archived')
const canSubmit = computed(() =>
  Boolean(problem.value) && !archived.value && !submitting.value && !forbidden.value && !quotaBlocked.value && code.value.trim().length > 0)

function stopPolling() {
  if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null }
}

async function loadSubmission(id) {
  const res = await practiceAPI.getSubmission(id)
  submission.value = res.data
  if (isActiveStatus(res.data?.grading_status) && pollCount < MAX_POLLS) {
    pollCount += 1
    stopPolling()
    pollTimer = window.setTimeout(() => loadSubmission(id).catch(() => stopPolling()), POLL_MS)
  } else {
    stopPolling()
  }
  return submission.value
}

async function refreshSubmission() {
  if (!submission.value?.id) return
  pollCount = 0
  await loadSubmission(submission.value.id).catch(() => {})
}

/** 同一份作答连点只发一次：client_request_id 在成功前保持不变，成功后换新的 */
function payloadFor() {
  if (!lastPayload.value || lastPayload.value.code !== code.value) {
    lastPayload.value = { code: code.value, client_request_id: newClientRequestId() }
  }
  return lastPayload.value
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  submitError.value = ''
  const payload = payloadFor()
  try {
    const res = await practiceAPI.submit(problemId.value, payload.code, payload.client_request_id)
    lastPayload.value = null
    pollCount = 0
    app.showToast('已提交，正在排队判题', 'success')
    await loadSubmission(res.data.id)
  } catch (e) {
    if (e.response?.status === 429 || e.response?.data?.detail?.code === 'QUOTA_EXHAUSTED') {
      quotaBlocked.value = true
      app.showToast('今日额度已用完', 'error')
    } else {
      submitError.value = practiceErrorMessage(e, '提交失败，请稍后重试')
      app.showToast(submitError.value, 'error')
    }
  } finally {
    submitting.value = false
  }
}

function resetCode() {
  code.value = problem.value?.starter_code || ''
}

onMounted(async () => {
  await loadProblem()
  if (error.value && !forbidden.value && problem.value === null) missing.value = true
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <AppLayout content-mode="detail">
    <section class="practice-solve">
      <nav class="crumb-row" aria-label="面包屑">
        <button type="button" class="btn btn-ghost btn-sm" @click="router.push('/student/practice')">
          <AppIcon name="back" :size="14" />返回练习中心
        </button>
      </nav>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        仅学生账号可以作答自主练习题目。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted || quotaBlocked" tone="danger" title="今日额度已用完" assertive>
        今日提交额度或 AI 预算已用尽，明天 0 点后恢复；已完成的成绩与排名不受影响。
      </PracticeNotice>

      <DashboardAsyncState
        :loading="loading"
        :error="Boolean(error) && !forbidden && !missing"
        :empty="missing"
        empty-title="题目不存在或已下架"
        empty-body="该题可能已归档，历史提交仍可在「我的练习」中查看。"
        @retry="loadProblem"
      >
        <template v-if="problem">
          <header class="problem-head">
            <div class="ph-title">
              <p class="eyebrow">PRACTICE PROBLEM</p>
              <h1>{{ problem.title }}</h1>
              <p class="meta-row">
                <UiStatusPill :tone="difficulty.tone" :label="difficulty.label" />
                <UiStatusPill :tone="mode.tone" :label="mode.label" />
                <span v-if="problem.time_limit_ms" class="meta mono">限时 {{ Math.round(problem.time_limit_ms / 1000) }} 秒</span>
                <span class="meta mono">通过 {{ problem.distinct_solvers ?? 0 }} 人</span>
              </p>
            </div>
            <div class="head-score">
              <span class="score-label">我的最高分</span>
              <strong class="score-value mono">{{ problem.my_best_score ?? '—' }}</strong>
            </div>
          </header>

          <PracticeNotice v-if="archived" tone="info" title="该题已归档，不能再提交">
            已有提交记录与成绩保留在「我的练习」中。
          </PracticeNotice>
          <PracticeNotice v-if="problem.grading_standard_updated" tone="info" title="本题评分标准已更新">
            新提交按当前评分版本计分，旧成绩保留在历史记录里。
          </PracticeNotice>

          <section class="problem-body">
            <article class="statement">
              <h2 class="section-title">题目描述</h2>
              <p v-if="problem.prompt" class="prompt-text">{{ problem.prompt }}</p>
              <p v-else class="prompt-text faint">该题暂未填写描述。</p>
              <div v-if="(problem.tags || []).length" class="tag-row">
                <span v-for="tag in problem.tags" :key="tag" class="tag">#{{ tag }}</span>
              </div>
            </article>

            <form class="answer-form" @submit.prevent="submit">
              <PracticeCodeEditor
                v-model="code"
                :starter-code="problem.starter_code || ''"
                :disabled="submitting || archived"
                label="我的作答"
                @reset="resetCode"
                @submit="submit"
              />
              <p v-if="submitError" class="submit-error" role="alert">{{ submitError }}</p>
              <div class="answer-actions">
                <button
                  type="submit"
                  class="btn btn-primary btn-lg"
                  :disabled="!canSubmit"
                >
                  {{ submitting ? '提交中…' : '提交作答' }}
                </button>
                <span class="answer-hint">
                  排队与判题进度会显示在本页；每题只累计当前评分版本下的历史最高分。
                </span>
              </div>
            </form>
          </section>

          <section v-if="submission" class="feedback-wrap" aria-label="我的提交反馈">
            <PracticeSubmissionFeedback :submission="submission" @changed="refreshSubmission" />
          </section>
        </template>
      </DashboardAsyncState>
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-solve { display: flex; flex-direction: column; gap: var(--space-4); }
.crumb-row { display: flex; }
.problem-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; }
.problem-head h1 { margin: 0; font-family: var(--font-display); font-size: var(--text-3xl); overflow-wrap: anywhere; }
.meta-row { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; margin: var(--space-3) 0 0; }
.meta { color: var(--muted); font-size: var(--text-sm); }
.head-score { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-3) var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.score-label { color: var(--muted); font-size: var(--text-sm); }
.score-value { font-size: var(--text-2xl); color: var(--accent); }
.problem-body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); gap: var(--space-4); align-items: start; }
.statement, .answer-form { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.section-title { margin: 0; font-size: var(--text-md); }
.prompt-text { margin: 0; color: var(--fg); font-size: var(--text-md); line-height: var(--lh-body); white-space: pre-wrap; overflow-wrap: anywhere; }
.prompt-text.faint { color: var(--faint); }
.tag-row { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.tag { font-size: var(--text-sm); color: var(--muted); background: var(--surface-subtle); border-radius: var(--radius-sm); padding: 2px var(--space-2); }
.answer-actions { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.answer-hint { color: var(--faint); font-size: var(--text-sm); }
.submit-error { margin: 0; color: var(--danger); font-size: var(--text-md); }
.feedback-wrap { padding-top: var(--space-2); border-top: 1px solid var(--border); }
@media (max-width: 900px) {
  .problem-body { grid-template-columns: 1fr; }
}
</style>
