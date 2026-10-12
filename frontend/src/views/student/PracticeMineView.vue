<script setup>
// 学生「我的练习」：个人统计 + 提交历史（含进行中的排队提交）。
// 六态：加载 / 空数据 / 失败 / 排队 / 额度耗尽 / 权限不足。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import StudentPagination from '../../components/student/StudentPagination.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { listErrorVisible, usePracticeLoad } from '../../components/practice/usePracticeLoad.js'
import {
  difficultyMeta, formatScore, gradingModeMeta, gradingStatusMeta, listOf, queuePositionText,
} from '../../components/practice/practiceHelpers.js'
import { practiceAPI } from '../../api/practice.js'
import { formatDateTime } from '../../utils/format.js'

const router = useRouter()

const PAGE_SIZE = 10
const summary = ref({})
const history = ref([])
const total = ref(0)
const page = ref(1)

const { loading, error, forbidden, quotaExhausted, load } = usePracticeLoad(async () => {
  const res = await practiceAPI.getMine({ page: page.value, page_size: PAGE_SIZE })
  summary.value = res.data?.summary || {}
  history.value = listOf(res.data)
  total.value = Number(res.data?.total ?? history.value.length)
  return res
}, { fallbackMessage: '练习统计加载失败，请稍后重试' })

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const metrics = computed(() => [
  { label: '已通过题目', value: summary.value.solved_count ?? 0, unit: '道' },
  { label: '累计总分', value: formatScore(summary.value.total_score ?? 0), unit: '' },
  { label: '平均分', value: formatScore(summary.value.avg_score ?? 0), unit: '' },
  { label: '正确率', value: summary.value.accuracy ?? 0, unit: '%' },
])
const cap = computed(() => Number(summary.value.daily_submission_cap))
const used = computed(() => Number(summary.value.daily_submissions_used))
const quotaText = computed(() => {
  if (!Number.isFinite(cap.value) || cap.value <= 0) return ''
  return `今日已提交 ${Number.isFinite(used.value) ? used.value : 0} / ${cap.value} 次`
})
const hasCap = computed(() => quotaText.value !== '')
const queuedRows = computed(() => history.value.filter((row) => row.grading_status === 'queued'))

function changePage(next) {
  page.value = next
  load()
}

function openFeedback(row) {
  router.push(`/student/practice/submissions/${row.id}`)
}

function rowScore(row) {
  if (row.score === null || row.score === undefined) return '—'
  return row.max_score ? `${formatScore(row.score)} / ${formatScore(row.max_score)}` : formatScore(row.score)
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="practice-mine">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">MY PRACTICE</p>
          <h1>我的练习</h1>
          <p class="lead subtitle">练习成绩与排名独立于课程总评，仅用于自主训练</p>
        </div>
        <div class="ph-actions">
          <button type="button" class="btn btn-primary" @click="router.push('/student/practice')">
            <AppIcon name="arrow-right" :size="14" />去练习中心
          </button>
        </div>
      </section>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        仅学生账号可以查看个人练习统计。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        今日提交额度或 AI 预算已用尽，明天 0 点后恢复；历史成绩与排名不受影响。
      </PracticeNotice>

      <PracticeNotice
        v-for="row in queuedRows"
        :key="row.id"
        tone="warning"
        :title="queuePositionText(row) || '排队中'"
      >
        《{{ row.problem_title }}》
        <router-link class="notice-link" :to="`/student/practice/submissions/${row.id}`">查看进度</router-link>
      </PracticeNotice>

      <div v-if="!forbidden" class="metric-strip" aria-label="练习统计">
        <div v-for="metric in metrics" :key="metric.label" class="metric">
          <span class="m-value">{{ metric.value }}<small v-if="metric.unit">{{ metric.unit }}</small></span>
          <span class="m-label">{{ metric.label }}</span>
        </div>
      </div>
      <p v-if="hasCap && !quotaExhausted" class="quota-line">{{ quotaText }}</p>

      <DashboardAsyncState
        :loading="loading"
        :error="listErrorVisible(error, forbidden)"
        :empty="!history.length"
        empty-title="还没有练习记录"
        empty-body="在练习中心选择一道题并提交后，记录会出现在这里。"
        @retry="load"
      >
        <div v-if="history.length" class="table-wrap">
          <div class="table-scroll">
            <table class="ds-table">
              <caption class="sr-only">我的练习提交历史</caption>
              <thead>
                <tr>
                  <th scope="col">题目</th>
                  <th scope="col">难度</th>
                  <th scope="col">得分</th>
                  <th scope="col">状态</th>
                  <th scope="col">判题方式</th>
                  <th scope="col">提交时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in history" :key="row.id">
                  <td class="title-cell">{{ row.problem_title || `题目 #${row.problem_id}` }}</td>
                  <td>
                    <UiStatusPill
                      :tone="difficultyMeta(row.difficulty).tone"
                      :label="difficultyMeta(row.difficulty).label"
                    />
                  </td>
                  <td class="mono">{{ rowScore(row) }}</td>
                  <td>
                    <UiStatusPill
                      :tone="gradingStatusMeta(row.grading_status).tone"
                      :label="gradingStatusMeta(row.grading_status).label"
                    />
                  </td>
                  <td>{{ gradingModeMeta(row.grading_mode).label }}</td>
                  <td class="time">{{ row.created_at ? formatDateTime(row.created_at) : '—' }}</td>
                  <td>
                    <button type="button" class="btn btn-ghost btn-sm" @click="openFeedback(row)">查看反馈</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </DashboardAsyncState>

      <StudentPagination
        v-if="!loading && history.length && pageCount > 1"
        :current-page="page"
        :page-count="pageCount"
        :total="total"
        :page-size="PAGE_SIZE"
        aria-label="练习记录分页"
        total-suffix="条"
        @change="changePage"
      />
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-mine { display: flex; flex-direction: column; gap: var(--space-4); }
.metric .m-value small { margin-left: 2px; font-size: var(--text-sm); color: var(--muted); }
.quota-line { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.title-cell { white-space: normal; overflow-wrap: anywhere; max-width: 320px; }
.time { color: var(--muted); }
.notice-link { color: var(--accent); font-weight: 600; }
</style>
