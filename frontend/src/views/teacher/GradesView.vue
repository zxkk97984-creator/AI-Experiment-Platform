<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import { examsAPI } from '../../api/exams.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'
import { EXAM_GRADE_STATUS_MAP, statusBadge } from '../../utils/status.js'
import { useServerClock } from '../../composables/useServerClock.js'
import { areaPathForRole } from '../../router/roleHome.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
// 教师与管理员复用同一页面：链接前缀取自当前已鉴权路由（服务端授权不变）
const area = (suffix) => areaPathForRole((route.path || '').startsWith('/admin') ? 'admin' : 'teacher', suffix)
const grades = ref([])
const exam = ref({})
const summary = ref({ expected_count: 0, submitted_count: 0, graded_count: 0, average_score: null, highest_score: null, pass_rate: 0, excellent_rate: 0 })
const distribution = ref([])
const loading = ref(true)
const query = ref('')
const statusFilter = ref('all')
const scoreFilter = ref('all')
const sortOrder = ref('score-desc')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)
const actionDialog = ref(null)
const actionBusy = ref(false)
const exportBusy = ref(false)
const extensionMinutes = ref(10)

const { nowMs, calibrate } = useServerClock(async () => {
  const res = await examsAPI.getGrades(route.params.id, gradeQuery())
  applyGradeData(res.data)
  return res.data.exam?.server_now
})

const filteredGrades = computed(() => grades.value)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const pagedGrades = computed(() => grades.value)
const paginationItems = computed(() => {
  const total = pageCount.value
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1)
  const candidates = new Set([1, total, page.value - 1, page.value, page.value + 1])
  const pages = [...candidates].filter(value => value >= 1 && value <= total).sort((a, b) => a - b)
  const output = []
  pages.forEach((value, index) => {
    if (index && value - pages[index - 1] > 1) output.push(`ellipsis-${index}`)
    output.push(value)
  })
  return output
})
const maxDistribution = computed(() => Math.max(1, ...distribution.value.map((item) => item.count)))
const donutStyle = computed(() => ({ background: `conic-gradient(var(--accent) 0 ${summary.value.pass_rate || 0}%, var(--danger) ${summary.value.pass_rate || 0}% 100%)` }))
const statusCards = computed(() => {
  const counts = summary.value.status_counts || {}
  return [
    ['未开始', (counts.scheduled || 0) + (counts.ready || 0), 'scheduled'],
    ['进行中', counts.in_progress || 0, 'in_progress'],
    ['已交卷', counts.submitted || 0, 'submitted'],
    ['评分中', counts.grading || 0, 'grading'],
    ['已评分', counts.graded || 0, 'graded'],
    ['待复核', counts.review_required || 0, 'review_required'],
    ['缺考', counts.missed || 0, 'missed'],
  ]
})
const canReleaseReview = computed(() => Boolean(
  exam.value.end_at
  && nowMs.value >= new Date(exam.value.end_at).getTime()
  && !(summary.value.status_counts?.in_progress),
))

function applyGradeData(data) {
  grades.value = data.items || []
  total.value = Number(data.total ?? grades.value.length)
  exam.value = data.exam || {}
  summary.value = { ...summary.value, ...(data.summary || {}) }
  distribution.value = data.distribution || []
}

function gradeQuery(overrides = {}) {
  const params = {
    page: overrides.page ?? page.value,
    page_size: overrides.page_size ?? pageSize.value,
    sort: sortOrder.value.replace('-', '_'),
  }
  if (query.value.trim()) params.q = query.value.trim()
  if (statusFilter.value !== 'all') params.status = statusFilter.value
  if (scoreFilter.value !== 'all') params.score = scoreFilter.value
  return params
}

async function load() {
  loading.value = true
  try {
    const res = await examsAPI.getGrades(route.params.id, gradeQuery())
    applyGradeData(res.data)
    calibrate(res.data.exam?.server_now)
  } catch { app.showToast('加载成绩失败', 'error') }
  finally { loading.value = false }
}
function resetPage() { page.value = 1; load() }
function changePage(nextPage) { page.value = nextPage; load() }
function viewDetail(item) { if (item.submission_id) router.push(area(`/exams/${route.params.id}/grades/${item.submission_id}`)) }
function remainingTime(item) {
  if (item.status !== 'in_progress' || !item.expires_at || !nowMs.value) return '—'
  const total = Math.max(0, Math.ceil((new Date(item.expires_at).getTime() - nowMs.value) / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

async function confirmAction() {
  const current = actionDialog.value
  if (!current) return
  actionBusy.value = true
  try {
    if (current.kind === 'review') {
      await examsAPI.releaseReview(route.params.id)
      app.showToast('讲评已发布，学生将按公开策略看到相应内容', 'success')
    } else if (current.kind === 'force') {
      await examsAPI.forceSubmit(route.params.id, current.item.submission_id)
      app.showToast(`已强制提交 ${current.item.student_name} 的试卷`, 'success')
    } else if (current.kind === 'extend') {
      await examsAPI.extendSubmission(route.params.id, current.item.submission_id, Number(extensionMinutes.value))
      app.showToast(`已为 ${current.item.student_name} 延长 ${extensionMinutes.value} 分钟`, 'success')
    }
    actionDialog.value = null
    await load()
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '操作失败', 'error')
  } finally { actionBusy.value = false }
}
async function exportGrades() {
  exportBusy.value = true
  try {
    // The table is server-paginated.  Fetch every matching page so export is
    // complete while retaining the active search, filters and sort order.
    const exportPageSize = 100
    const first = await examsAPI.getGrades(route.params.id, gradeQuery({ page: 1, page_size: exportPageSize }))
    const data = first.data || {}
    const allItems = [...(data.items || [])]
    const exportTotal = Number(data.total ?? allItems.length)
    const pageCount = Math.ceil(exportTotal / exportPageSize)
    for (let exportPage = 2; exportPage <= pageCount; exportPage += 1) {
      const response = await examsAPI.getGrades(route.params.id, gradeQuery({ page: exportPage, page_size: exportPageSize }))
      allItems.push(...(response.data?.items || []))
    }

    const rows = [['学生姓名', '学号', '状态', '得分', '提交时间'], ...allItems.map((item) => [item.student_name, item.student_number, statusBadge(EXAM_GRADE_STATUS_MAP, item.status).label, item.score ?? '', formatDateTime(item.submitted_at, { seconds: true })])]
    const csv = `\ufeff${rows.map((row) => row.map((cell) => `"${String(cell ?? '').replaceAll('"', '""')}"`).join(',')).join('\n')}`
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${exam.value.title || '考试'}-成绩.csv`
    link.click()
    URL.revokeObjectURL(url)
  } catch {
    app.showToast('导出成绩失败', 'error')
  } finally {
    exportBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="grades-page">
      <button class="back-link" @click="router.push(area('/exams'))"><AppIcon name="back" :size="17" />返回考试管理</button>
      <header class="page-head"><div><h1>{{ exam.title || '考试成绩' }} · 成绩总览</h1><p>查看本场考试成绩、统计与学生表现</p></div><button class="btn-primary export-button" :disabled="!grades.length || exportBusy" @click="exportGrades"><AppIcon name="download" :size="18" />{{ exportBusy ? '导出中…' : '导出成绩' }}</button></header>

      <section class="review-control">
        <div><p class="control-kicker">讲评与公开</p><h2>{{ exam.review_released_at ? '讲评已发布' : '讲评尚未发布' }}</h2><p>成绩：{{ exam.show_score_after_grading ? '评分后公开' : '隐藏' }} · 题目：{{ exam.show_questions_after_review ? '讲评后公开' : '隐藏' }} · 答案：{{ exam.show_answers_after_review ? '讲评后公开' : '隐藏' }}</p></div>
        <div class="review-actions"><button class="btn-ghost" @click="router.push(area(`/exams/${route.params.id}/edit`))">调整公开策略</button><button v-if="!exam.review_released_at" class="btn-primary" :disabled="!canReleaseReview" :title="canReleaseReview ? '' : '最晚进入时间结束且无人正在考试后才可发布'" @click="actionDialog = { kind: 'review' }">发布讲评</button><span v-else>{{ formatDateTime(exam.review_released_at) }}</span></div>
      </section>

      <section class="status-overview" aria-label="考试进度统计"><article v-for="status in statusCards" :key="status[0]" :class="status[2]"><strong>{{ status[1] }}</strong><span>{{ status[0] }}</span></article></section>

      <section class="summary-grid" aria-label="成绩统计">
        <article v-for="item in [
          { label: '应考人数', value: summary.expected_count, icon: 'user', tone: 'blue' },
          { label: '已交卷', value: summary.submitted_count, icon: 'clipboard', tone: 'green' },
          { label: '平均分', value: summary.average_score ?? '—', icon: 'chart', tone: 'orange' },
          { label: '及格率', value: summary.pass_rate + '%', icon: 'pie', tone: 'purple' },
          { label: '最高分', value: summary.highest_score ?? '—', icon: 'trophy', tone: 'blue' },
        ]" :key="item.label" class="summary-card"><span class="summary-icon" :class="item.tone"><AppIcon :name="item.icon" :size="24" /></span><span><small>{{ item.label }}</small><strong>{{ item.value }}</strong></span></article>
      </section>

      <section class="results-panel">
        <div class="filter-bar"><label class="search-control"><AppIcon name="search" :size="18" /><input v-model="query" placeholder="搜索学生姓名或学号" @input="resetPage" /></label><DsSelect v-model="statusFilter" @change="resetPage"><option value="all">状态：全部</option><option value="scheduled">未开始</option><option value="ready">可开始</option><option value="in_progress">进行中</option><option value="submitted">已交卷</option><option value="graded">已评分</option><option value="review_required">待复核</option><option value="grading">评分中</option><option value="missed">缺考</option></DsSelect><DsSelect v-model="scoreFilter" @change="resetPage"><option value="all">分数段：全部</option><option value="excellent">90–100</option><option value="good">80–89</option><option value="pass">60–79</option><option value="fail">0–59</option></DsSelect><DsSelect v-model="sortOrder" @change="resetPage"><option value="score-desc">排序：分数从高到低</option><option value="score-asc">排序：分数从低到高</option><option value="time">排序：最近提交</option><option value="name">排序：学生姓名</option></DsSelect></div>
        <div v-if="loading" class="loading-list"><span v-for="i in 5" :key="i" class="skeleton"></span></div>
        <div v-else-if="!filteredGrades.length" class="empty-state"><AppIcon name="chart" :size="34" /><strong>暂无符合条件的成绩</strong></div>
        <div v-else class="table-scroll"><table class="grade-table"><thead><tr><th>学生</th><th>学号</th><th>状态</th><th>剩余时间 / 最后保存</th><th>得分</th><th>提交时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in pagedGrades" :key="item.id"><td data-label="学生"><span class="student-cell"><i>{{ item.student_name?.slice(0, 1) || '学' }}</i><strong>{{ item.student_name }}</strong></span></td><td data-label="学号">{{ item.student_number }}</td><td data-label="状态"><span class="status-pill" :class="item.status">{{ statusBadge(EXAM_GRADE_STATUS_MAP, item.status).label }}</span></td><td data-label="剩余时间 / 最后保存" class="muted-cell"><strong v-if="item.status === 'in_progress'">剩余 {{ remainingTime(item) }}</strong><span v-else>{{ item.last_saved_at ? formatDateTime(item.last_saved_at, { seconds: true }) : '—' }}</span></td><td data-label="得分"><strong v-if="item.score != null" class="score">{{ item.score }}</strong><span v-else>—</span></td><td data-label="提交时间" class="muted-cell">{{ formatDateTime(item.submitted_at, { seconds: true }) }}</td><td data-label="操作"><div class="row-actions"><button class="detail-action" :disabled="!item.submission_id" @click="viewDetail(item)">查看详情</button><template v-if="item.status === 'in_progress'"><button class="extend-action" @click="extensionMinutes = 10; actionDialog = { kind: 'extend', item }">延时</button><button class="force-action" @click="actionDialog = { kind: 'force', item }">强制交卷</button></template></div></td></tr></tbody></table></div>
        <footer v-if="!loading && total" class="pagination-bar"><span>共 {{ total }} 条</span><div class="pagination"><button aria-label="上一页" :disabled="page === 1" @click="changePage(page - 1)">‹</button><template v-for="item in paginationItems" :key="item"><span v-if="typeof item === 'string'" class="pagination-ellipsis">…</span><button v-else :aria-label="`第 ${item} 页`" :class="{ active: page === item }" @click="changePage(item)">{{ item }}</button></template><button aria-label="下一页" :disabled="page === pageCount" @click="changePage(page + 1)">›</button></div><span>{{ pageSize }} 条/页</span></footer>
      </section>
      <details class="grade-analysis"><summary>分数分布与考试表现</summary>
      <section class="charts-grid">
        <article class="chart-card"><h2>分数分布</h2><div v-if="distribution.length" class="bar-chart"><div v-for="item in distribution" :key="item.label" class="bar-column"><span class="bar-count">{{ item.count }}</span><span class="bar" :style="{ height: `${Math.max(6, item.count / maxDistribution * 100)}%` }"></span><small>{{ item.label }}</small></div></div><div v-else class="chart-empty">暂无可统计成绩</div></article>
        <article class="chart-card performance-card"><h2>考试表现</h2><div class="performance-body"><div class="donut" :style="donutStyle"><span><strong>{{ summary.pass_rate }}%</strong><small>及格率</small></span></div><div class="performance-stats"><span><small>及格率（≥60分）</small><strong>{{ summary.pass_rate }}%</strong></span><span><small>优秀率（≥90分）</small><strong>{{ summary.excellent_rate }}%</strong></span><p><i class="legend pass"></i>及格 {{ summary.graded_count ? Math.round(summary.graded_count * summary.pass_rate / 100) : 0 }} 人 <i class="legend fail"></i>未及格 {{ summary.graded_count ? summary.graded_count - Math.round(summary.graded_count * summary.pass_rate / 100) : 0 }} 人</p></div></div></article>
      </section>

      </details>
    </section>

    <div v-if="actionDialog?.kind === 'extend'" class="modal-backdrop" @click.self="actionDialog = null"><div class="extension-panel" v-dialog-focus role="dialog" aria-modal="true"><h2>延长个人考试时间</h2><p>仅影响 {{ actionDialog.item.student_name }} 的个人截止时间。</p><label>延长分钟数<input v-model.number="extensionMinutes" type="number" min="1" max="180"></label><div><button class="btn-ghost" :disabled="actionBusy" @click="actionDialog = null">取消</button><button class="btn-primary" :disabled="actionBusy || extensionMinutes < 1 || extensionMinutes > 180" @click="confirmAction">确认延长</button></div></div></div>
    <ConfirmDialog v-else-if="actionDialog" :title="actionDialog.kind === 'review' ? '确认发布讲评' : '确认强制交卷'" :message="actionDialog.kind === 'review' ? '发布后，学生将按当前公开策略查看题目或答案。此操作不会自动开放隐藏内容。' : `系统会立即锁定并提交 ${actionDialog.item.student_name} 的当前已保存答案。`" :confirm-text="actionDialog.kind === 'review' ? '发布讲评' : '强制交卷'" :danger="actionDialog.kind === 'force'" :busy="actionBusy" @confirm="confirmAction" @cancel="actionDialog = null" />
  </AppLayout>
</template>

<style scoped>
.grades-page{display:flex;min-width:0;container-type:inline-size;flex-direction:column;gap:18px}.back-link{align-self:flex-start;padding:0;border:0;color:var(--accent);background:transparent}.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.page-head h1{margin:0 0 5px;color:var(--fg);font-size:28px;letter-spacing:-.025em}.page-head p{margin:0;color:var(--muted);font-size:14px}.export-button{min-height:46px;padding:0 19px}
.review-control{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:19px 21px;border:1px solid var(--info-bg);border-radius: var(--radius-lg);background:var(--surface-sunken)}.control-kicker{margin:0 0 4px!important;color:var(--accent)!important;font-size:10px!important;font-weight:750;letter-spacing:.1em}.review-control h2{margin:0;color:var(--fg);font-size:16px}.review-control p{margin:5px 0 0;color:var(--muted);font-size:12px}.review-actions{display:flex;align-items:center;gap:9px}.review-actions>span{color:var(--success);font-size:11px}.status-overview{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:9px}.status-overview article{display:flex;align-items:baseline;justify-content:space-between;padding:12px 13px;border:1px solid var(--border);border-radius: var(--radius-md);background:var(--surface)}.status-overview strong{color:var(--fg);font-size:21px}.status-overview span{color:var(--muted);font-size:11px}.status-overview .in_progress{border-color:var(--warning-bg);background:var(--surface)}.status-overview .review_required{border-color:var(--warning-bg);background:var(--surface)}.status-overview .missed{border-color:var(--danger-bg);background:var(--surface)}
.summary-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px}.summary-card{display:flex;align-items:center;gap:14px;min-height:96px;padding:17px;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:none}.summary-icon{display:grid;place-items:center;width:48px;height:48px;border-radius: var(--radius-lg)}.summary-icon.blue{color:var(--accent);background:var(--accent-soft)}.summary-icon.green{color:var(--success);background:var(--success-bg)}.summary-icon.orange{color:var(--warning);background:var(--surface)}.summary-icon.purple{color:var(--info);background:var(--info-bg)}.summary-card span:last-child{display:grid;gap:4px}.summary-card small{color:var(--muted);font-size:13px}.summary-card strong{color:var(--fg);font-size:25px;line-height:1}
.charts-grid{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}.chart-card{min-height:250px;padding:18px 20px;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:none}.chart-card h2{margin:0;color:var(--fg);font-size:15px}.bar-chart{display:flex;align-items:flex-end;justify-content:space-around;height:190px;padding:25px 22px 0;border-bottom:1px solid var(--border)}.bar-column{display:grid;grid-template-rows:20px 1fr 26px;align-items:end;justify-items:center;width:14%;height:100%}.bar-count{color:var(--muted);font-size:12px}.bar{width:38px;max-height:130px;border-radius: var(--radius-sm) 3px 0 0;background:var(--accent)}.bar-column small{align-self:end;padding-top:7px;color:var(--muted);font-size:12px;white-space:nowrap}.chart-empty{display:grid;place-items:center;height:190px;color:var(--faint)}.performance-body{display:flex;align-items:center;justify-content:space-around;height:210px;gap:24px}.donut{display:grid;place-items:center;width:150px;height:150px;border-radius:50%;position:relative}.donut::after{content:'';position:absolute;width:104px;height:104px;border-radius:50%;background:var(--surface)}.donut span{z-index:1;display:grid;text-align:center}.donut strong{color:var(--fg);font-size:27px}.donut small{color:var(--muted);font-size:12px}.performance-stats{display:grid;gap:15px;min-width:190px}.performance-stats>span{display:flex;align-items:end;justify-content:space-between;padding-bottom:10px;border-bottom:1px dashed var(--border)}.performance-stats small{color:var(--muted)}.performance-stats strong{color:var(--fg);font-size:24px}.performance-stats p{margin:0;color:var(--muted);font-size:12px}.legend{display:inline-block;width:9px;height:9px;margin-right:4px;border-radius: var(--radius-sm)}.legend.pass{background:var(--accent)}.legend.fail{background:var(--danger)}
.results-panel{min-width:0;overflow:hidden;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface)}.filter-bar{display:grid;min-width:0;grid-template-columns:minmax(220px,1.2fr) repeat(3,minmax(150px,.75fr));gap:12px;padding:15px;border-bottom:1px solid var(--border)}.filter-bar .ds-select,.search-control{height:42px;min-width:0}.search-control{display:flex;align-items:center;gap:8px;min-width:0;padding:0 12px;border:1px solid var(--border);border-radius: var(--radius-md);color:var(--faint)}.search-control input{min-width:0;padding:0;border:0;box-shadow:none!important}.filter-bar .ds-select{cursor:pointer}.table-scroll{min-width:0;overflow-x:hidden}.grade-table{width:100%;min-width:0;table-layout:fixed}.grade-table th{height:42px;padding:0 14px;overflow:hidden;text-transform:none;letter-spacing:0;text-overflow:ellipsis;white-space:nowrap}.grade-table th:nth-child(1){width:20%}.grade-table th:nth-child(2){width:13%}.grade-table th:nth-child(3){width:12%}.grade-table th:nth-child(4){width:22%}.grade-table th:nth-child(5){width:9%}.grade-table th:nth-child(6){width:15%}.grade-table th:nth-child(7){width:17%}.grade-table td{height:61px;min-width:0;padding:8px 14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.student-cell{display:flex;align-items:center;min-width:0;gap:10px}.student-cell strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg)}.student-cell i{display:grid;place-items:center;flex:0 0 auto;width:32px;height:32px;border-radius:50%;color:var(--accent);background:var(--accent-soft);font-style:normal;font-size:13px}.status-pill{display:inline-flex;padding:4px 10px;border-radius: var(--radius-full);font-size:12px;font-weight:600}.status-pill.graded{color:var(--success);background:var(--success-bg)}.status-pill.review_required{color:var(--warning);background:var(--warning-bg)}.status-pill.grading,.status-pill.submitted,.status-pill.started{color:var(--accent);background:var(--accent-soft)}.status-pill.absent{color:var(--danger);background:var(--danger-bg)}.score{color:var(--accent);font-size:17px}.muted-cell{color:var(--muted);font-size:13px}.detail-action{padding:4px;border:0;color:var(--accent);background:transparent;white-space:nowrap}.detail-action:disabled{color:var(--faint)}.pagination-bar{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:13px 16px;border-top:1px solid var(--border);color:var(--muted);font-size:13px}.pagination{display:flex;align-items:center;gap:6px}.pagination button{width:33px;height:33px;padding:0}.pagination button.active{border-color:var(--accent);color:var(--surface);background:var(--accent)}.pagination-ellipsis{display:grid;place-items:center;width:20px;color:var(--faint)}.pagination-bar>span:last-child{justify-self:end}.loading-list{display:grid;gap:1px;background:var(--border)}.loading-list .skeleton{height:61px;border-radius:0}.empty-state{display:grid;place-items:center;gap:8px}.empty-state strong{color:var(--fg)}
.status-pill.grading,.status-pill.submitted,.status-pill.started,.status-pill.in_progress{color:var(--accent);background:var(--accent-soft)}.status-pill.absent,.status-pill.missed{color:var(--danger);background:var(--danger-bg)}.status-pill.scheduled,.status-pill.ready{color:var(--muted);background:var(--surface-subtle)}.muted-cell strong{display:block;color:var(--danger);font-size:12px}.row-actions{display:flex;align-items:center;gap:2px}.detail-action,.extend-action,.force-action{padding:4px;border:0;background:transparent;white-space:nowrap}.detail-action{color:var(--accent)}.extend-action{color:var(--success)}.force-action{color:var(--danger)}
.modal-backdrop{position:fixed;z-index:70;inset:0 0 0 var(--modal-left,0);display:grid;place-items:center;background:oklch(0.2 0.01 150 / 0.28)}.extension-panel{width:min(420px,calc(100% - 32px));padding:24px;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:var(--shadow-xl)}.extension-panel h2{margin:0;color:var(--fg);font-size:17px}.extension-panel p{color:var(--muted);font-size:12px}.extension-panel label{display:grid;gap:7px;color:var(--muted);font-size:12px}.extension-panel input{padding:10px}.extension-panel>div{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
@media(max-width:1250px){.summary-grid{grid-template-columns:repeat(3,1fr)}.filter-bar{grid-template-columns:1fr 1fr}.status-overview{grid-template-columns:repeat(4,1fr)}}@media(max-width:900px){.charts-grid{grid-template-columns:1fr}.summary-grid{grid-template-columns:repeat(2,1fr)}.review-control{align-items:flex-start;flex-direction:column}}@media(max-width:680px){.page-head{align-items:stretch;flex-direction:column}.page-head h1{font-size:24px}.summary-grid{grid-template-columns:1fr 1fr;gap:9px}.summary-card{min-height:82px;padding:12px}.summary-icon{width:42px;height:42px}.summary-card strong{font-size:21px}.status-overview{grid-template-columns:repeat(2,1fr)}.review-actions{align-items:stretch;flex-direction:column;width:100%}.filter-bar{grid-template-columns:1fr;padding:12px}.performance-body{flex-direction:column;height:auto;padding-top:18px}.pagination-bar{grid-template-columns:1fr auto}.pagination{grid-column:1/-1;grid-row:1;justify-content:center}}
@container (max-width:1050px){.filter-bar{grid-template-columns:minmax(0,1.2fr) repeat(3,minmax(0,.75fr))}.grade-table th:nth-child(1){width:25%}.grade-table th:nth-child(2){width:17%}.grade-table th:nth-child(3){width:15%}.grade-table th:nth-child(4){width:22%}.grade-table th:nth-child(5){width:12%}.grade-table th:nth-child(7){width:24%}.grade-table th:nth-child(6),.grade-table td:nth-child(6){display:none}}
@container (max-width:760px){.filter-bar{grid-template-columns:1fr;padding:12px}.table-scroll{overflow:visible;padding:12px;background:var(--surface-subtle)}.grade-table,.grade-table tbody{display:block;width:100%}.grade-table thead{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.grade-table tbody{display:grid;gap:12px}.grade-table tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));overflow:hidden;border:1px solid var(--border);border-radius: var(--radius-md);background:var(--surface)}.grade-table td,.grade-table td:nth-child(3),.grade-table td:nth-child(6){display:flex;width:auto;height:auto;min-height:58px;padding:10px 12px;flex-direction:column;justify-content:center;gap:5px;border-bottom:1px solid var(--border);overflow:visible;white-space:normal}.grade-table td::before{content:attr(data-label);color:var(--faint);font-size:11px;font-weight:500}.grade-table td:nth-child(1),.grade-table td:nth-child(3),.grade-table td:nth-child(7){grid-column:1/-1}.grade-table td:nth-child(6),.grade-table td:nth-last-child(-n+2){border-bottom:0}.grade-table .student-cell{align-items:center}.grade-table .detail-action{align-self:flex-end;min-height:34px}.grade-table td strong{overflow:visible;white-space:normal}}

.grade-analysis summary { cursor: pointer; font-size: var(--text-lg); font-weight: 600; padding: 16px 0; }
.summary-grid { gap: 12px; }
.summary-card { min-height: 68px; padding: 12px 16px; }
.summary-icon { width: 32px; height: 32px; }
.status-overview article { padding: 8px 12px; }
.review-control { padding: 12px 16px; }
.grade-table th:nth-child(1) { width: 18%; }
.grade-table th:nth-child(2) { width: 13%; }
.grade-table th:nth-child(3) { width: 12%; }
.grade-table th:nth-child(4) { width: 17%; }
.grade-table th:nth-child(5) { width: 8%; }
.grade-table th:nth-child(6) { width: 16%; }
.grade-table th:nth-child(7) { width: 16%; }
.row-actions { flex-wrap: wrap; }
</style>
