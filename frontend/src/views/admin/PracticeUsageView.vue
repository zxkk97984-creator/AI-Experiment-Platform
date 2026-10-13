<script setup>
// 管理员练习用量页：按日查看提交量、AI token / 金额消耗与练习队列占用。
// 六态：加载 / 空数据（所选日期没有用量）/ 失败 / 排队（队列积压）/ 额度耗尽 / 权限不足。
import { computed, onMounted, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { practiceErrorMessage } from '../../components/practice/practiceHelpers.js'
import { adminPracticeAPI } from '../../api/practice.js'
import { useAppStore } from '../../stores/app.js'

const app = useAppStore()

const today = new Date().toISOString().slice(0, 10)
const date = ref(today)
const usage = ref(null)
const loading = ref(true)
const error = ref('')
const forbidden = ref(false)

const queue = computed(() => usage.value?.queue || {})
const tokensUsed = computed(() => Number(usage.value?.ai_tokens_used || 0))
const tokensBudget = computed(() => Number(usage.value?.ai_budget_tokens || 0))
const amountUsed = computed(() => Number(usage.value?.ai_amount_used || 0))
const amountBudget = computed(() => Number(usage.value?.ai_budget_amount || 0))
const quotaExhausted = computed(() =>
  (tokensBudget.value > 0 && tokensUsed.value >= tokensBudget.value)
  || (amountBudget.value > 0 && amountUsed.value >= amountBudget.value))
const metrics = computed(() => [
  { label: '今日提交', value: usage.value?.submissions_today ?? 0 },
  { label: 'AI tokens', value: `${tokensUsed.value} / ${tokensBudget.value || '不限'}` },
  { label: 'AI 金额', value: `${amountUsed.value} / ${amountBudget.value || '不限'}` },
  { label: '队列（排队 / 运行 / 上限）', value: `${queue.value.queued ?? 0} / ${queue.value.running ?? 0} / ${queue.value.max_concurrent ?? '—'}` },
])
const daily = computed(() => usage.value?.daily || [])
const pendingJobs = computed(() => Number(queue.value.pending_ai_jobs ?? 0))

async function load() {
  loading.value = true
  error.value = ''
  forbidden.value = false
  try {
    const res = await adminPracticeAPI.getUsage({ date: date.value })
    usage.value = res.data
  } catch (e) {
    if (e.response?.status === 403) forbidden.value = true
    error.value = practiceErrorMessage(e, '用量数据加载失败，请稍后重试')
    app.showToast(error.value, 'error')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="detail">
    <section class="practice-usage">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE ADMIN</p>
          <h1>练习用量</h1>
          <p class="lead subtitle">提交量、AI 预算消耗与练习队列占用；练习队列与正式作业、考试隔离</p>
        </div>
        <div class="ph-actions">
          <label class="date-field">
            <span class="field-label">统计日期</span>
            <input v-model="date" class="input" type="date" aria-label="统计日期" @change="load" />
          </label>
          <button type="button" class="btn btn-secondary" @click="load">
            <AppIcon name="refresh" :size="14" />刷新
          </button>
        </div>
      </section>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        练习用量仅管理员可查看。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        今日 AI 预算已用尽，新的 AI 判题提交会被拒绝并显示「今日额度已用完」，普通判题不受影响。
      </PracticeNotice>

      <PracticeNotice
        v-if="!forbidden && Number(queue.queued) > 0"
        tone="warning"
        :title="`练习队列排队 ${queue.queued} 项`"
      >
        运行中 {{ queue.running ?? 0 }} / 上限 {{ queue.max_concurrent ?? '—' }}，超额部分会明确排队并展示名次。
      </PracticeNotice>

      <PracticeNotice
        v-if="!forbidden && pendingJobs > 0"
        tone="info"
        :title="`AI 配置任务排队 ${pendingJobs} 项`"
      >
        一键配置任务在独立队列中执行，不影响正式判题。
      </PracticeNotice>

      <DashboardAsyncState
        v-if="!forbidden"
        :loading="loading"
        :error="Boolean(error)"
        :empty="false"
        @retry="load"
      >
        <template v-if="usage">
          <div class="metric-strip" aria-label="用量概览">
            <div v-for="metric in metrics" :key="metric.label" class="metric">
              <span class="m-value">{{ metric.value }}</span>
              <span class="m-label">{{ metric.label }}</span>
            </div>
          </div>

          <section class="usage-panel">
            <h2 class="panel-title">近 30 天用量</h2>
            <div v-if="daily.length" class="table-wrap">
              <div class="table-scroll">
                <table class="ds-table">
                  <caption class="sr-only">练习每日用量</caption>
                  <thead>
                    <tr>
                      <th scope="col">日期</th>
                      <th scope="col">提交数</th>
                      <th scope="col">AI tokens</th>
                      <th scope="col">AI 金额</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in daily" :key="row.date">
                      <td class="mono">{{ row.date }}</td>
                      <td class="mono">{{ row.submissions ?? 0 }}</td>
                      <td class="mono">{{ row.ai_tokens ?? 0 }}</td>
                      <td class="mono">{{ row.ai_amount ?? 0 }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            <p v-else class="usage-empty">所选日期还没有练习用量记录。</p>
          </section>
        </template>
      </DashboardAsyncState>
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-usage { display: flex; flex-direction: column; gap: var(--space-4); }
.date-field { display: flex; align-items: center; gap: var(--space-2); }
.usage-panel { display: flex; flex-direction: column; gap: var(--space-3); }
.panel-title { margin: 0; font-size: var(--text-md); }
.usage-empty { margin: 0; padding: var(--space-4); border: 1px dashed var(--border-strong); border-radius: var(--radius-md); text-align: center; color: var(--muted); font-size: var(--text-md); }
</style>
