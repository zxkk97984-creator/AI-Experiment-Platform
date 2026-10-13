<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { academicsAPI } from '../../api/academics.js'
import { announcementsAPI } from '../../api/announcements.js'
import { dashboardAPI } from '../../api/dashboard.js'
import AnnouncementComposer from '../../components/dashboard/AnnouncementComposer.vue'
import AnnouncementPanel from '../../components/dashboard/AnnouncementPanel.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { useAuthStore } from '../../stores/auth.js'

const auth = useAuthStore()
const router = useRouter()
const now = new Date()

const loading = ref(true)
const error = ref(false)
const dashboard = ref(null)
const currentTerm = ref('')
const queueFilter = ref('all')
const showComposer = ref(false)
const panelRef = ref(null)

const teacherName = computed(() => (
  auth.user?.real_name || auth.user?.username || '老师'
).slice(0, 8))

const todayText = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: 'long', day: 'numeric', weekday: 'long',
}).format(now)
const timeFmt = new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
})

const workStatusMap = {
  pending_grading: { label: '待评分', tone: 'badge-warning' },
  review_required: { label: '待复核', tone: 'badge-info' },
  pending_release: { label: '待发布', tone: 'badge-neutral' },
  graded: { label: '已评分', tone: 'badge-success' },
  failed: { label: '失败', tone: 'badge-danger' },
}

const allWorkItems = computed(() => dashboard.value?.work_items || [])
const aiReviewItems = computed(() => allWorkItems.value.filter((item) => (
  item.kind === 'ai_review' || item.status === 'review_required'
)))
const focusItems = computed(() => aiReviewItems.value.length ? aiReviewItems.value : allWorkItems.value)
const focusBreakdown = computed(() => focusItems.value.slice(0, 3))
const focusCount = computed(() => {
  if (aiReviewItems.value.length) {
    return aiReviewItems.value.reduce((total, item) => total + Number(item.count || 0), 0)
      || Number(dashboard.value?.summary?.pending_review_count || 0)
  }
  return Number(focusItems.value[0]?.count || dashboard.value?.summary?.pending_grading_count || 0)
})
const focusKind = computed(() => aiReviewItems.value.length ? 'AI 评分复核' : '待评分工作')
const focusTitle = computed(() => (
  focusItems.value.length ? `${focusCount.value} 份结果等待教师确认` : '当前没有需要教师处理的事项'
))
const focusMeta = computed(() => {
  const courses = [...new Set(focusItems.value.map((item) => item.course_title).filter(Boolean))]
  if (!courses.length) return '课程运行平稳，可以继续查看教学进度'
  return `来自 ${courses.slice(0, 2).join('、')}${courses.length > 2 ? '等课程' : ''}`
})
const focusRoute = computed(() => focusItems.value[0]?.route || '/teacher/courses')
const filteredWorkItems = computed(() => {
  if (queueFilter.value === 'ai') return aiReviewItems.value
  if (queueFilter.value === 'grading') {
    return allWorkItems.value.filter((item) => item.status === 'pending_grading')
  }
  return allWorkItems.value
})
const recentSubmissions = computed(() => (dashboard.value?.recent_submissions || []).slice(0, 10))
const visibleAnnouncements = computed(() => (dashboard.value?.announcements || []).slice(0, 3))

function resolveCurrentTerm(terms) {
  const todayKey = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
  ].join('-')
  const includesToday = (term) => (
    typeof term?.start_date === 'string'
    && typeof term?.end_date === 'string'
    && term.start_date <= todayKey
    && todayKey <= term.end_date
  )
  const selected = terms.find((term) => term.status === 'active' && includesToday(term))
    || terms.find((term) => term.status === 'active')
    || terms.find(includesToday)
  return selected?.name?.trim() || ''
}

async function loadDashboard() {
  loading.value = true
  error.value = false
  currentTerm.value = ''
  try {
    const [dashboardResult, termsResult] = await Promise.allSettled([
      dashboardAPI.teacher(),
      academicsAPI.listTerms({ page: 1, page_size: 100 }),
    ])
    if (termsResult.status === 'fulfilled') {
      const payload = termsResult.value.data
      const terms = Array.isArray(payload) ? payload : payload?.items || []
      currentTerm.value = resolveCurrentTerm(terms)
    }
    if (dashboardResult.status === 'rejected') throw dashboardResult.reason
    dashboard.value = dashboardResult.value.data
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}

function go(route) {
  if (typeof route === 'string' && (route === '/teacher' || route.startsWith('/teacher/'))) {
    router.push(route)
  }
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : timeFmt.format(date)
}

function queueIcon(item) {
  if (item.kind === 'ai_review') return 'brain'
  if (item.kind === 'deadline') return 'calendar'
  if (item.kind === 'exam_release') return 'exam'
  return 'clipboard'
}
function queueAction(item) {
  if (item.status === 'review_required') return '开始复核'
  if (item.status === 'pending_grading') return '开始评分'
  if (item.status === 'pending_release') return '查看成绩'
  return '查看详情'
}
function courseStatus(row) {
  const details = [`${row.student_count || 0} 名学生`]
  if (row.pending_review_count) details.push(`${row.pending_review_count} 项待复核`)
  if (row.upcoming_deadline_count) details.push(`${row.upcoming_deadline_count} 项即将截止`)
  if (row.at_risk_expected_count) details.push(`${row.at_risk_submitted_count ?? 0}/${row.at_risk_expected_count} 已提交`)
  return details.join(' · ')
}
function submissionStatus(row) {
  return workStatusMap[row.status] || { label: row.status || '—', tone: 'badge-neutral' }
}
function submissionScore(row) {
  if (row.ai_score != null) return Number(row.ai_score).toFixed(1)
  if (row.score != null) return Number(row.score).toFixed(1)
  return '—'
}
function testsText(row) {
  if (row.tests_total == null) return '—'
  return `${row.tests_passed ?? 0} / ${row.tests_total}`
}

async function markRead(notice) {
  if (notice.is_read) return
  try {
    await announcementsAPI.markRead(notice.id)
    if (!dashboard.value) return
    dashboard.value = {
      ...dashboard.value,
      announcements: (dashboard.value.announcements || []).map((item) => (
        item.id === notice.id ? { ...item, is_read: true } : item
      )),
    }
  } catch {
    // 保留未读状态，教师可稍后重试。
  }
}
function restorePublishFocus() {
  nextTick(() => {
    panelRef.value?.$el?.querySelector('.publish-btn')?.focus()
      || document.querySelector('.publish-btn')?.focus()
  })
}
function onPublished() {
  showComposer.value = false
  loadDashboard()
  restorePublishFocus()
}
function onCloseComposer() {
  showComposer.value = false
  restorePublishFocus()
}

onMounted(loadDashboard)
</script>

<template>
  <AppLayout content-mode="list">
    <div class="teacher-dashboard">
      <section class="intro-section">
        <div>
          <p class="date-line">{{ todayText }}</p>
          <h1>你好，{{ teacherName }}</h1>
          <p class="intro-copy">先处理需要教师判断的事项，再回到课程运行节奏。</p>
        </div>
        <div class="current-term" aria-label="当前学期">
          <span>当前学期</span>
          <strong>{{ currentTerm || '暂无学期信息' }}</strong>
        </div>
      </section>

      <section class="overview-section" aria-label="教学概况">
        <div class="overview-strip">
          <div class="metric"><strong>{{ dashboard?.summary?.active_course_count ?? dashboard?.summary?.course_count ?? '—' }}</strong><span>进行中课程</span></div>
          <div class="metric"><strong>{{ dashboard?.summary?.student_count ?? '—' }}</strong><span>在册学生</span></div>
          <div class="metric metric-accent"><strong>{{ dashboard?.summary?.pending_grading_count ?? '—' }}</strong><span>待评分提交</span></div>
          <div class="metric metric-warning"><strong>{{ dashboard?.summary?.upcoming_deadline_count ?? '—' }}</strong><span>近 7 天截止</span></div>
        </div>
      </section>

      <section class="workspace-section">
        <div class="work-grid">
          <div class="main-column">
            <article class="teacher-card focus-card">
              <DashboardAsyncState :loading="loading" :error="error" :empty="false" @retry="loadDashboard">
                <div class="focus-top">
                  <div>
                    <p class="focus-kicker">今日重点 · {{ focusKind }}</p>
                    <h2>{{ focusTitle }}</h2>
                    <p class="focus-meta">{{ focusMeta }}</p>
                  </div>
                  <button type="button" class="btn btn-primary work-queue-btn" @click="go(focusRoute)">
                    {{ focusItems.length ? '开始处理' : '管理课程' }}
                    <AppIcon name="arrow-right" :size="15" />
                  </button>
                </div>
                <div v-if="focusBreakdown.length" class="review-breakdown" aria-label="重点批次概览">
                  <div v-for="item in focusBreakdown" :key="item.kind + '-' + item.id" class="review-stat">
                    <strong>{{ item.count ?? 1 }}</strong>
                    <span>{{ item.course_title || item.title }}<template v-if="item.time_at"> · {{ formatTime(item.time_at) }}</template></span>
                  </div>
                </div>
              </DashboardAsyncState>
            </article>

            <article class="teacher-card queue-card">
              <div class="card-head">
                <div><p class="eyebrow">处理队列</p><h2>待处理工作</h2></div>
                <div class="segmented" aria-label="筛选待处理工作">
                  <button v-for="option in [{ key: 'all', label: '全部' }, { key: 'ai', label: 'AI 复核' }, { key: 'grading', label: '待评分' }]" :key="option.key" type="button" class="segment" :aria-pressed="queueFilter === option.key" @click="queueFilter = option.key">{{ option.label }}</button>
                </div>
              </div>
              <DashboardAsyncState :loading="loading" :error="error" :empty="!filteredWorkItems.length" empty-title="暂无待处理工作" empty-body="当前筛选下没有需要处理的事项" @retry="loadDashboard">
                <div class="queue-list">
                  <button v-for="item in filteredWorkItems" :key="item.kind + '-' + item.id" type="button" class="queue-item" @click="go(item.route)">
                    <span class="queue-glyph"><AppIcon :name="queueIcon(item)" :size="16" /></span>
                    <span class="queue-main"><strong>{{ item.title }}</strong><small>{{ item.course_title }}<template v-if="item.time_at"> · {{ formatTime(item.time_at) }}</template></small></span>
                    <span class="queue-action">{{ queueAction(item) }}</span>
                  </button>
                </div>
              </DashboardAsyncState>
            </article>
          </div>

          <aside class="side-column" aria-label="辅助教学信息">
            <article class="teacher-card course-card">
              <div class="card-head"><div><p class="eyebrow">课程运行</p><h2>课程概况</h2></div><button type="button" class="text-btn" @click="go('/teacher/courses')">查看全部</button></div>
              <DashboardAsyncState :loading="loading" :error="error" :empty="!dashboard?.course_health?.length" empty-title="暂无课程" empty-body="创建课程后，运行概况会展示在这里" @retry="loadDashboard">
                <div class="course-health">
                  <button v-for="row in dashboard?.course_health || []" :key="row.course_id" type="button" class="course-row health-link" @click="go(row.route)">
                    <span><strong>{{ row.title }}</strong><small>{{ courseStatus(row) }}</small></span>
                    <span class="course-count">{{ row.pending_review_count || '—' }}</span>
                  </button>
                </div>
              </DashboardAsyncState>
            </article>

            <article class="teacher-card announcement-card">
              <div class="card-head"><div><p class="eyebrow">教学动态</p><h2>课程公告</h2></div><button v-if="!showComposer" type="button" class="icon-action publish-btn" aria-label="发布公告" @click="showComposer = true"><AppIcon name="plus" :size="16" /></button></div>
              <div class="announcement-body">
                <AnnouncementPanel ref="panelRef" :announcements="visibleAnnouncements" :loading="loading" :error="error" :can-publish="false" @retry="loadDashboard" @mark-read="markRead" />
              </div>
            </article>
          </aside>
        </div>
      </section>

      <section class="recent-section">
        <div class="section-heading">
          <div><p class="eyebrow">最近动态</p><h2>最近提交</h2><p>按提交时间排序，仅展示最近 10 条</p></div>
          <div class="row-wrap">
            <button type="button" class="text-btn" @click="go('/teacher/practice/leaderboard')">练习排行榜 →</button>
            <button type="button" class="text-btn" @click="go('/teacher/submissions/unified')">查看全部 →</button>
          </div>
        </div>
        <article class="teacher-card submission-card">
          <DashboardAsyncState :loading="loading" :error="error" :empty="!recentSubmissions.length" empty-title="暂无提交" empty-body="学生提交实验、作业或考试后，记录会出现在这里" @retry="loadDashboard">
            <div class="table-scroll">
              <table class="recent-table">
                <thead><tr><th>学生</th><th>实验 / 作业</th><th>状态</th><th>测试</th><th>AI 得分</th><th>提交时间</th></tr></thead>
                <tbody>
                  <tr v-for="row in recentSubmissions" :key="row.kind + '-' + row.id" @click="go(row.route)">
                    <td data-label="学生"><strong>{{ row.student_name || '未命名学生' }}</strong><small>{{ row.student_no || '—' }}</small></td>
                    <td data-label="实验 / 作业"><strong>{{ row.entry_title || '未命名任务' }}</strong><small>{{ row.course_title || '—' }}</small></td>
                    <td data-label="状态"><span class="badge" :class="submissionStatus(row).tone"><span class="dot"></span>{{ submissionStatus(row).label }}</span></td>
                    <td data-label="测试" class="numeric">{{ testsText(row) }}</td>
                    <td data-label="AI 得分" class="numeric score-cell">{{ submissionScore(row) }}</td>
                    <td data-label="提交时间" class="submission-time">{{ formatTime(row.submitted_at) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </DashboardAsyncState>
        </article>
      </section>

      <AnnouncementComposer v-if="showComposer" :courses="dashboard?.managed_courses || []" @close="onCloseComposer" @published="onPublished" />
    </div>
  </AppLayout>
</template>

<style scoped>
.teacher-dashboard { width: min(1480px, 100%); margin: 0 auto; padding: 0 0 24px; }
.intro-section { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; padding: 4px 0 16px; }
.date-line, .eyebrow, .focus-kicker { margin: 0; color: var(--muted); font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .08em; }
.intro-section h1 { margin: 6px 0 0; font-family: var(--font-display); font-size: 32px; line-height: 1.2; letter-spacing: -.02em; }
.intro-copy { margin: 8px 0 0; color: var(--muted); }
.current-term { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: var(--text-sm); }
.current-term strong { min-height: 40px; display: inline-flex; align-items: center; padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); color: var(--fg); font-size: var(--text-base); font-weight: 500; }
.overview-section, .workspace-section, .recent-section { padding: 16px 0; border-top: 1px solid var(--border); }
.overview-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.metric { min-height: 68px; padding: 12px 16px; }
.metric + .metric { border-left: 1px solid var(--border); }
.metric strong { display: block; font-family: var(--font-mono); font-size: 23px; line-height: 1; font-variant-numeric: tabular-nums; }
.metric span { display: block; margin-top: 8px; color: var(--muted); font-size: var(--text-sm); }
.metric-accent strong { color: var(--accent); }
.metric-warning strong { color: var(--warning); }
.work-grid { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(320px, .82fr); gap: 18px; align-items: start; }
.main-column, .side-column { display: grid; gap: 18px; }
.teacher-card { overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.card-head { min-height: 62px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 13px 18px; border-bottom: 1px solid var(--border); }
.card-head h2, .section-heading h2 { margin: 2px 0 0; font-family: var(--font-display); font-size: 19px; line-height: 1.25; }
.focus-card { padding: 20px; }
.focus-top { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 24px; align-items: start; }
.focus-card h2 { margin: 8px 0 0; font-family: var(--font-display); font-size: 24px; line-height: 1.3; }
.focus-meta { margin: 8px 0 0; color: var(--muted); font-size: var(--text-base); }
.review-breakdown { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 24px; border-top: 1px solid var(--border); }
.review-stat { min-width: 0; padding: 18px 14px 0 0; color: var(--muted); font-size: var(--text-sm); }
.review-stat + .review-stat { padding-left: 14px; border-left: 1px solid var(--border); }
.review-stat strong { display: block; margin-bottom: 4px; color: var(--fg); font-family: var(--font-mono); font-size: 18px; }
.review-stat span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.segmented { display: flex; gap: 3px; padding: 3px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg); }
.segment { min-height: 34px; padding: 0 10px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--muted); font-size: var(--text-xs); }
.segment[aria-pressed='true'] { background: var(--surface); color: var(--fg); box-shadow: 0 1px 0 color-mix(in oklch, var(--fg) 12%, transparent); }
.queue-list { display: grid; }
.queue-item { min-height: 82px; display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 13px 18px; border: 0; border-radius: 0; background: transparent; color: var(--fg); text-align: left; }
.queue-item + .queue-item { border-top: 1px solid var(--border); }
.queue-item:hover, .course-row:hover, .recent-table tbody tr:hover { background: var(--surface-sunken); }
.queue-glyph { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: var(--radius-md); }
.queue-main { min-width: 0; }
.queue-main strong { display: block; font-family: var(--font-display); font-size: 15px; }
.queue-main small { display: block; margin-top: 4px; overflow: hidden; color: var(--muted); font-size: var(--text-xs); text-overflow: ellipsis; white-space: nowrap; }
.queue-action { min-height: 30px; display: inline-flex; align-items: center; padding: 0 10px; border: 1px solid var(--border); border-radius: var(--radius-md); font-size: var(--text-xs); white-space: nowrap; }
.text-btn, .icon-action { border: 0; background: transparent; color: var(--fg); }
.text-btn { min-height: 36px; padding: 0 6px; font-size: var(--text-sm); }
.text-btn:hover { text-decoration: underline; text-underline-offset: 4px; }
.icon-action { width: 36px; height: 36px; display: grid; place-items: center; border-radius: var(--radius-md); }
.icon-action:hover { background: var(--surface-sunken); }
.course-health { display: grid; }
.course-row { min-height: 72px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 12px 18px; border: 0; border-radius: 0; background: transparent; color: var(--fg); text-align: left; }
.course-row + .course-row { border-top: 1px solid var(--border); }
.course-row strong { display: block; overflow: hidden; font-family: var(--font-display); font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.course-row small { display: block; margin-top: 4px; color: var(--muted); font-size: var(--text-xs); }
.course-count { font-family: var(--font-mono); font-size: var(--text-sm); }
.announcement-body { padding: 0; }
.announcement-body :deep(.announcement-panel > .panel-head) { display: none; }
.announcement-body :deep(.announcement-panel) { gap: 0; }
.announcement-body :deep(.notice-list) { gap: 0; }
.announcement-body :deep(.notice-item) { padding: 14px 18px; border: 0; border-radius: 0; }
.announcement-body :deep(.notice-item + .notice-item) { border-top: 1px solid var(--border); }
.announcement-body :deep(.mark-read-btn) { border-radius: var(--radius-sm); }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.section-heading h2 { font-size: 24px; }
.section-heading p:last-child { margin: 4px 0 0; color: var(--muted); font-size: var(--text-sm); }
.table-scroll { overflow-x: auto; }
.recent-table { width: 100%; border-collapse: collapse; font-size: var(--text-base); }
.recent-table th, .recent-table td { padding: 12px 16px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
.recent-table th { color: var(--muted); font-family: var(--font-mono); font-size: var(--text-xs); font-weight: 500; letter-spacing: .04em; }
.recent-table tbody tr { cursor: pointer; }
.recent-table tbody tr:last-child td { border-bottom: 0; }
.recent-table td strong { display: block; font-size: var(--text-base); font-weight: 600; }
.recent-table td small { display: block; margin-top: 2px; color: var(--muted); font-size: var(--text-xs); }
.numeric { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.score-cell { font-weight: 700; }
.submission-time { color: var(--muted); font-family: var(--font-mono); font-size: var(--text-sm); white-space: nowrap; }

@media (max-width: 1120px) {
  .work-grid { grid-template-columns: 1fr; }
  .side-column { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .teacher-dashboard { padding: 24px 18px 42px; }
  .intro-section { align-items: flex-start; flex-direction: column; gap: 18px; }
  .current-term { align-items: flex-start; flex-direction: column; gap: 6px; }
  .overview-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .metric:nth-child(3) { border-left: 0; border-top: 1px solid var(--border); }
  .metric:nth-child(4) { border-top: 1px solid var(--border); }
  .side-column { grid-template-columns: 1fr; }
  .focus-top { grid-template-columns: 1fr; }
  .focus-top .btn { width: 100%; }
  .review-breakdown { grid-template-columns: 1fr; }
  .review-stat + .review-stat { padding-left: 0; border-left: 0; }
  .segmented { width: 100%; }
  .segment { flex: 1; }
  .card-head { align-items: stretch; flex-direction: column; }
  .queue-item { grid-template-columns: 32px minmax(0, 1fr); }
  .queue-action { grid-column: 2; justify-self: start; }
  .recent-table thead { display: none; }
  .recent-table, .recent-table tbody, .recent-table tr, .recent-table td { display: block; }
  .recent-table tr { padding: 13px 16px; border-bottom: 1px solid var(--border); }
  .recent-table td { display: grid; grid-template-columns: 92px minmax(0, 1fr); padding: 5px 0; border: 0; }
  .recent-table td::before { content: attr(data-label); color: var(--muted); font-size: var(--text-xs); }
}
</style>
