<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { academicsAPI } from '../../api/academics.js'
import { announcementsAPI } from '../../api/announcements.js'
import { coursesAPI } from '../../api/courses.js'
import { dashboardAPI } from '../../api/dashboard.js'
import { progressAPI } from '../../api/progress.js'
import { useAuthStore } from '../../stores/auth.js'

const auth = useAuthStore()
const router = useRouter()

const loading = ref(true)
const error = ref(false)
const dashboard = ref(null)
const activeTaskFilter = ref('all')
const currentTerm = ref('')
const focusLessons = ref([])
const focusProgress = ref(null)

const taskFilters = [
  { value: 'all', label: '全部' },
  { value: 'assignment', label: '作业' },
  { value: 'exam', label: '考试' },
]

const kindLabel = {
  assignment: '作业',
  exam: '考试',
  experiment: '实验',
}

const kindIcon = {
  assignment: 'assignment',
  exam: 'exam',
  experiment: 'experiment',
}

const now = new Date()
const firstName = computed(() => (
  auth.user?.real_name || auth.user?.username || '同学'
).slice(0, 8))

const studentContext = computed(() => ({
  className: compactClassName(dashboard.value?.teaching_classes?.[0] || ''),
  currentTerm: currentTerm.value,
}))

const todayText = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  weekday: 'long',
}).format(now).replace(/(\d{4})年/, '$1 年 ').replace(/(\d+)月/, '$1 月 ').replace(/(\d+)日/, '$1 日 · ')

const todayDay = String(now.getDate()).padStart(2, '0')
const todayMonth = new Intl.DateTimeFormat('zh-CN', { month: 'long' }).format(now)
const todayWeekday = new Intl.DateTimeFormat('zh-CN', { weekday: 'long' }).format(now)

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: 'long',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const shortDateFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
})

const focusItem = computed(() => dashboard.value?.continue_learning || null)
const focusTitle = computed(() => focusItem.value?.subtitle || focusItem.value?.title || '')
const focusRecord = computed(() => (
  focusItem.value?.subtitle ? focusItem.value.title : ''
))

const focusSteps = computed(() => {
  if (focusLessons.value.length) {
    return focusLessons.value.map((lesson) => {
      const progressItem = focusProgress.value?.items?.find((item) => item.lesson_id === lesson.id)
      const status = progressItem?.status || (
        focusProgress.value?.next_lesson_id === lesson.id ? 'in_progress' : 'not_started'
      )
      const isCompleted = status === 'completed'
      const isCurrent = status === 'in_progress' || (
        !isCompleted && focusProgress.value?.next_lesson_id === lesson.id
      )
      return {
        ...lesson,
        status,
        isCompleted,
        isCurrent,
        description: isCompleted
          ? '已完成本节学习'
          : isCurrent
            ? '继续巩固当前内容'
            : '完成当前内容后继续',
      }
    })
  }

  if (focusItem.value?.kind !== 'module_experiment') return []
  const feedbackSteps = (dashboard.value?.recent_feedback || [])
    .slice(0, 2)
    .reverse()
    .map((item) => ({
      id: `feedback-${item.id}`,
      title: item.title,
      description: item.feedback || (item.score != null ? `最近评分 ${item.score} 分` : '已获得学习反馈'),
      isCompleted: true,
      isCurrent: false,
    }))
  if (!feedbackSteps.length) return []
  return [...feedbackSteps, {
    id: `focus-${focusItem.value.route}`,
    title: focusItem.value.title,
    description: '继续当前学习记录',
    isCompleted: false,
    isCurrent: true,
  }]
})

const filteredTasks = computed(() => {
  const items = dashboard.value?.priority_items || []
  if (activeTaskFilter.value === 'all') return items
  return items.filter((item) => item.kind === activeTaskFilter.value)
})

const scheduleItems = computed(() => (dashboard.value?.priority_items || [])
  .filter((item) => ['assignment', 'exam'].includes(item.kind) && validDate(item.time_at))
  .sort((a, b) => new Date(a.time_at).getTime() - new Date(b.time_at).getTime())
  .slice(0, 2))

const scheduleNote = computed(() => {
  const count = scheduleItems.value.filter((item) => {
    const date = new Date(item.time_at)
    return date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate()
  }).length
  return count > 0
    ? `今天有 ${count} 项安排，建议优先完成。`
    : '今天没有截止任务，可以专注推进当前课程。'
})

const visibleAnnouncements = computed(() => (dashboard.value?.announcements || []).slice(0, 3))

const visibleCourses = computed(() => {
  const courses = dashboard.value?.courses || []
  const filtered = currentTerm.value
    ? courses.filter((course) => course.academic_term?.trim() === currentTerm.value)
    : courses
  return filtered.slice(0, 3)
})

function validDate(value) {
  if (!value) return false
  return !Number.isNaN(new Date(value).getTime())
}

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

function formatDateTime(value) {
  return validDate(value) ? dateTimeFormatter.format(new Date(value)) : ''
}

function formatScheduleDate(value) {
  if (!validDate(value)) return ''
  return shortDateFormatter.format(new Date(value)).replace('/', ' / ')
}

function taskMeta(item) {
  const parts = []
  if (item.course_title) parts.push(item.course_title)
  if (validDate(item.time_at)) {
    const prefix = item.kind === 'assignment' ? '截止 ' : item.kind === 'experiment' ? '更新于 ' : ''
    parts.push(`${prefix}${formatDateTime(item.time_at)}`)
  }
  return parts.join(' · ')
}

function announcementPriority(notice) {
  return notice.priority === 'important' ? '重要' : '普通'
}

function compactClassName(value) {
  if (!value) return ''
  return value.match(/([^\s·（）()]+班)\s*$/u)?.[1] || value
}

function courseMeta(course) {
  const currentClasses = (dashboard.value?.teaching_classes || []).map(compactClassName)
  const matchingClasses = currentClasses.filter((className) => (
    (course.teaching_classes || []).some((name) => name?.includes(className))
  ))
  const fallbackClasses = (course.teaching_classes || [])
    .filter((name) => name && name !== course.academic_term)
    .map(compactClassName)
    .slice(0, 1)
  const classes = matchingClasses.length ? matchingClasses.slice(0, 1) : fallbackClasses
  return [...classes, course.academic_term || '未设置学期'].join(' · ')
}

function courseStatus(course) {
  if ((course.pending_assignment_count || 0) > 0) {
    return `${course.pending_assignment_count} 份待交`
  }
  if ((course.upcoming_exam_count || 0) > 0) {
    return `${course.upcoming_exam_count} 场考试`
  }
  if (validDate(course.last_activity_at)) return `最近学习 · ${formatDateTime(course.last_activity_at)}`
  return '已加入'
}

function isStudentRoute(route) {
  return typeof route === 'string' && (route === '/student' || route.startsWith('/student/'))
}

function go(route) {
  if (isStudentRoute(route)) router.push(route)
}

function selectFocusLessons(lessons, nextLessonId) {
  if (lessons.length <= 3) return lessons
  const currentIndex = lessons.findIndex((lesson) => lesson.id === nextLessonId)
  if (currentIndex < 0) return lessons.slice(0, 3)
  const start = Math.max(0, Math.min(currentIndex - 1, lessons.length - 3))
  return lessons.slice(start, start + 3)
}

async function loadFocusDetails() {
  focusLessons.value = []
  focusProgress.value = null
  const match = focusItem.value?.route?.match(/^\/student\/courses\/(\d+)(?:\/|$)/)
  if (!match) return

  const courseId = Number(match[1])
  const [chaptersResult, progressResult] = await Promise.allSettled([
    coursesAPI.getChapters(courseId),
    progressAPI.getCourse(courseId),
  ])

  if (progressResult.status === 'fulfilled') {
    focusProgress.value = progressResult.value.data || null
  }

  if (chaptersResult.status === 'fulfilled') {
    const payload = chaptersResult.value.data
    const chapters = Array.isArray(payload) ? payload : payload?.items || []
    const lessons = chapters
      .flatMap((chapter) => chapter?.lessons || [])
      .filter((lesson) => lesson?.id != null && lesson?.title)
    focusLessons.value = selectFocusLessons(lessons, focusProgress.value?.next_lesson_id)
  }
}

async function loadDashboard() {
  loading.value = true
  error.value = false
  currentTerm.value = ''
  try {
    const [dashboardResult, termsResult] = await Promise.allSettled([
      dashboardAPI.student(),
      academicsAPI.listTerms({ page: 1, page_size: 100 }),
    ])
    if (termsResult.status === 'fulfilled') {
      const payload = termsResult.value.data
      const terms = Array.isArray(payload) ? payload : payload?.items || []
      currentTerm.value = resolveCurrentTerm(terms)
    }
    if (dashboardResult.status === 'rejected') throw dashboardResult.reason
    dashboard.value = dashboardResult.value.data
    await loadFocusDetails()
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}

async function markRead(notice) {
  if (notice.is_read) return
  try {
    await announcementsAPI.markRead(notice.id)
    if (!dashboard.value) return
    const wasUnread = dashboard.value.announcements.some(
      (item) => item.id === notice.id && !item.is_read,
    )
    dashboard.value = {
      ...dashboard.value,
      announcements: dashboard.value.announcements.map((item) => (
        item.id === notice.id ? { ...item, is_read: true } : item
      )),
      summary: {
        ...dashboard.value.summary,
        unread_announcement_count: Math.max(
          0,
          (dashboard.value.summary?.unread_announcement_count || 0) - (wasUnread ? 1 : 0),
        ),
      },
    }
  } catch {
    // 保持未读状态，允许用户稍后重试。
  }
}

onMounted(loadDashboard)
</script>

<template>
  <AppLayout content-mode="list" :student-context="studentContext">
    <section class="student-dashboard">
      <section class="dashboard-section welcome-section">
        <div>
          <p class="greeting-date">{{ todayText }}</p>
          <h1 class="greeting-title">你好，{{ firstName }}</h1>
          <p class="welcome-copy">先完成今天最重要的一件事，再从容安排这一周。</p>
        </div>
        <div class="term-display" aria-label="当前学期">
          <span class="term-label">当前学期</span>
          <strong class="term-value">{{ currentTerm || '暂无学期信息' }}</strong>
        </div>
      </section>

      <section class="dashboard-section semester-overview" aria-label="本学期学习概览">
        <div class="overview-strip">
          <div class="overview-metric">
            <strong class="summary-num">{{ dashboard?.summary?.course_count ?? '—' }}</strong>
            <span class="summary-label">已加入课程</span>
          </div>
          <div class="overview-metric">
            <strong class="summary-num">{{ dashboard?.summary?.pending_assignment_count ?? '—' }}</strong>
            <span class="summary-label">待交作业</span>
          </div>
          <div class="overview-metric">
            <strong class="summary-num">{{ dashboard?.summary?.upcoming_exam_count ?? '—' }}</strong>
            <span class="summary-label">即将考试</span>
          </div>
          <div class="overview-metric">
            <strong class="summary-num">{{ dashboard?.summary?.unread_announcement_count ?? '—' }}</strong>
            <span class="summary-label">未读公告</span>
          </div>
        </div>
      </section>

      <section class="dashboard-section learning-workspace">
        <div class="work-grid">
          <div class="main-column">
            <article class="workspace-card today-focus-card">
              <DashboardAsyncState
                :loading="loading"
                :error="error"
                :empty="!focusItem"
                empty-title="暂无学习记录"
                empty-body="开始学习后，最近进度会显示在这里"
                @retry="loadDashboard"
              >
                <template v-if="focusItem">
                  <div class="focus-top">
                    <div class="focus-copy">
                      <p class="focus-kicker">今日重点 · 继续学习</p>
                      <h2 class="focus-title">{{ focusTitle }}</h2>
                      <p class="focus-meta">
                        <span v-if="focusRecord">学习记录 · {{ focusRecord }}</span>
                        <span v-if="focusItem.updated_at">最近更新于 {{ formatDateTime(focusItem.updated_at) }}</span>
                        <span v-if="focusProgress?.percent != null">学习进度 {{ focusProgress.percent }}%</span>
                      </p>
                    </div>
                    <button
                      type="button"
                      class="workspace-btn primary-btn continue-btn"
                      :disabled="!isStudentRoute(focusItem.route)"
                      @click="go(focusItem.route)"
                    >
                      继续学习
                      <AppIcon name="arrow-right" :size="17" />
                    </button>
                  </div>

                  <div v-if="focusSteps.length" class="learning-path" aria-label="近期学习路径">
                    <div
                      v-for="step in focusSteps"
                      :key="step.id"
                      class="learning-step"
                      :class="{ 'is-completed': step.isCompleted, 'is-current': step.isCurrent }"
                      :aria-current="step.isCurrent ? 'step' : undefined"
                    >
                      <strong class="step-title">
                        <span>{{ step.title }}</span>
                        <AppIcon v-if="step.isCompleted" name="check" :size="14" />
                      </strong>
                      <span>{{ step.description }}</span>
                    </div>
                  </div>
                </template>
              </DashboardAsyncState>
            </article>

            <article class="workspace-card task-card">
              <header class="workspace-card-head task-card-head">
                <div>
                  <p class="section-eyebrow">近期安排</p>
                  <h2>待办任务</h2>
                </div>
                <div class="task-filters" aria-label="筛选任务">
                  <button
                    v-for="filter in taskFilters"
                    :key="filter.value"
                    type="button"
                    class="task-filter"
                    :data-filter="filter.value"
                    :aria-pressed="activeTaskFilter === filter.value"
                    @click="activeTaskFilter = filter.value"
                  >
                    {{ filter.label }}
                  </button>
                </div>
              </header>
              <div class="task-list-wrap">
                <DashboardAsyncState
                  :loading="loading"
                  :error="error"
                  :empty="!dashboard?.priority_items?.length"
                  empty-title="暂无待办"
                  empty-body="当前没有待完成的作业、考试或实验"
                  @retry="loadDashboard"
                >
                  <div class="task-list">
                    <button
                      v-for="item in filteredTasks"
                      :key="`${item.kind}-${item.id}`"
                      type="button"
                      class="task-row"
                      :disabled="!isStudentRoute(item.route)"
                      @click="go(item.route)"
                    >
                      <span class="task-glyph" aria-hidden="true">
                        <AppIcon :name="kindIcon[item.kind] || 'assignment'" :size="16" />
                      </span>
                      <span class="task-copy">
                        <strong class="task-title">{{ item.title }}</strong>
                        <span class="task-meta">{{ taskMeta(item) }}</span>
                      </span>
                      <span class="status-pill">{{ kindLabel[item.kind] || item.kind }}</span>
                    </button>
                    <p v-if="filteredTasks.length === 0" class="filter-empty">当前筛选下没有任务。</p>
                  </div>
                </DashboardAsyncState>
              </div>
            </article>
          </div>

          <aside class="side-column" aria-label="学习辅助信息">
            <article class="workspace-card schedule-card">
              <header class="workspace-card-head">
                <div>
                  <p class="section-eyebrow">本周节奏</p>
                  <h2>学习日程</h2>
                </div>
              </header>
              <div class="schedule-body">
                <div class="calendar-date">
                  <div><strong>{{ todayDay }}</strong><span>{{ todayMonth }}</span></div>
                  <span>{{ todayWeekday }}</span>
                </div>
                <p v-if="scheduleItems.length" class="schedule-note">{{ scheduleNote }}</p>
                <DashboardAsyncState
                  :loading="loading"
                  :error="error"
                  :empty="scheduleItems.length === 0"
                  empty-title="暂无日程"
                  empty-body="今天没有截止任务，可以专注推进当前课程"
                  @retry="loadDashboard"
                >
                  <div class="schedule-list">
                    <button
                      v-for="item in scheduleItems"
                      :key="`${item.kind}-${item.id}`"
                      type="button"
                      class="schedule-item"
                      :disabled="!isStudentRoute(item.route)"
                      @click="go(item.route)"
                    >
                      <span class="schedule-time">{{ formatScheduleDate(item.time_at) }}</span>
                      <span>
                        <strong>{{ item.title }}</strong>
                        <small>{{ item.course_title || kindLabel[item.kind] }}</small>
                      </span>
                    </button>
                  </div>
                </DashboardAsyncState>
              </div>
            </article>

            <article class="workspace-card announcement-card">
              <header class="workspace-card-head">
                <div>
                  <p class="section-eyebrow">课程动态</p>
                  <h2>通知公告</h2>
                </div>
                <button type="button" class="text-btn" @click="router.push('/student')">查看全部</button>
              </header>
              <DashboardAsyncState
                :loading="loading"
                :error="error"
                :empty="!dashboard?.announcements?.length"
                empty-title="暂无公告"
                empty-body="新的课程通知会显示在这里"
                @retry="loadDashboard"
              >
                <div class="announcement-list">
                  <article
                    v-for="notice in visibleAnnouncements"
                    :key="notice.id"
                    class="announcement-item"
                    :class="{ unread: !notice.is_read }"
                  >
                    <div class="announcement-top">
                      <strong>{{ notice.title }}</strong>
                      <span class="status-pill">{{ announcementPriority(notice) }}</span>
                    </div>
                    <p>{{ notice.content }}</p>
                    <div v-if="!notice.is_read" class="announcement-foot">
                      <button
                        type="button"
                        class="mark-read-btn"
                        @click="markRead(notice)"
                      >
                        标记已读
                      </button>
                    </div>
                  </article>
                </div>
              </DashboardAsyncState>
            </article>
          </aside>
        </div>
      </section>

      <section class="dashboard-section course-section">
        <header class="course-section-head">
          <div>
            <p class="section-eyebrow">本学期</p>
            <h2>我的课程</h2>
            <p>从最近学习过的课程继续。</p>
          </div>
          <button type="button" class="workspace-btn secondary-btn all-courses-btn" @click="router.push('/student/courses')">
            查看全部课程
          </button>
        </header>
        <DashboardAsyncState
          :loading="loading"
          :error="error"
          :empty="!dashboard?.courses?.length"
          empty-title="暂无课程"
          empty-body="加入课程后将在这里展示学习动态"
          @retry="loadDashboard"
        >
          <div v-if="visibleCourses.length" class="course-grid">
            <button
              v-for="(course, index) in visibleCourses"
              :key="course.id"
              type="button"
              class="course-card"
              :disabled="!isStudentRoute(course.route)"
              @click="go(course.route)"
            >
              <span class="course-code">课程 · {{ String(index + 1).padStart(2, '0') }}</span>
              <strong class="course-title">{{ course.title }}</strong>
              <span class="course-meta">{{ courseMeta(course) }}</span>
              <span class="course-foot">
                <span>{{ courseStatus(course) }}</span>
                <span class="course-link">进入课程 <AppIcon name="arrow-right" :size="15" /></span>
              </span>
            </button>
          </div>
          <p v-else class="term-empty">当前学期暂无课程。</p>
        </DashboardAsyncState>
      </section>
    </section>
  </AppLayout>
</template>

<style scoped>
.student-dashboard {
  --workspace-bg: oklch(0.9731 0.0041 91.45);
  --workspace-surface: oklch(0.994 0 89.88);
  --workspace-fg: oklch(0.2586 0.0159 152.78);
  --workspace-muted: oklch(0.4586 0.0139 153.35);
  --workspace-border: oklch(0.8979 0.0095 113.18);
  --workspace-accent: oklch(0.5179 0.0909 158.07);
  width: min(1480px, 100%);
  margin: 0 auto;
  padding: 0 0 24px;
  color: var(--workspace-fg);
  font-family: var(--font-body);
  font-size: var(--text-md);
  line-height: var(--lh-body);
}

.student-dashboard *,
.student-dashboard *::before,
.student-dashboard *::after { box-sizing: border-box; }

.dashboard-section { padding-block: 16px; }
.dashboard-section + .dashboard-section { border-top: 1px solid var(--workspace-border); }

.welcome-section {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 28px;
  padding-top: 4px;
  padding-bottom: 16px;
}

.greeting-date {
  margin: 0 0 7px;
  color: var(--workspace-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
}

.greeting-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 32px;
  font-weight: 700;
  line-height: 1.14;
  letter-spacing: -0.025em;
}

.welcome-copy { margin: 8px 0 0; color: var(--workspace-muted); }
.term-display { display: flex; align-items: baseline; gap: var(--space-2); color: var(--workspace-muted); font-size: var(--text-sm); }
.term-value { color: var(--workspace-fg); font-size: var(--text-md); font-weight: 600; }

.student-dashboard button:focus-visible {
  outline: 2px solid var(--workspace-accent);
  outline-offset: 3px;
}

.overview-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid var(--workspace-border);
  border-radius: var(--radius-lg);
  background: var(--workspace-surface);
}

.overview-metric { min-height: 72px; padding: 12px 16px; }
.overview-metric + .overview-metric { border-left: 1px solid var(--workspace-border); }
.summary-num {
  display: block;
  font-family: var(--font-mono);
  font-size: 23px;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.summary-label { display: block; margin-top: 8px; color: var(--workspace-muted); font-size: 12px; }

.work-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(300px, 0.82fr);
  gap: 18px;
  align-items: stretch;
}
.main-column { min-width: 0; min-height: 0; display: flex; flex-direction: column; gap: 18px; }
.side-column { min-width: 0; display: grid; gap: 18px; align-self: start; }
.workspace-card {
  overflow: hidden;
  border: 1px solid var(--workspace-border);
  border-radius: var(--radius-lg);
  background: var(--workspace-surface);
}
.workspace-card-head {
  min-height: 66px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 15px 18px;
  border-bottom: 1px solid var(--workspace-border);
}
.workspace-card-head h2,
.course-section-head h2 { margin: 0; font-family: var(--font-display); }
.workspace-card-head h2 { font-size: 19px; line-height: 1.25; }
.section-eyebrow {
  margin: 0 0 2px;
  color: var(--workspace-muted);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.11em;
}

.workspace-btn {
  min-height: 46px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 0 18px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  font-weight: 600;
  transition: transform 140ms cubic-bezier(0.23, 1, 0.32, 1), background 200ms ease, border-color 200ms ease;
}
.workspace-btn:active { transform: translateY(1px); }
.workspace-btn:disabled,
.task-row:disabled,
.schedule-item:disabled,
.course-card:disabled { cursor: default; opacity: 0.65; }
.primary-btn { border-color: var(--workspace-accent); background: var(--workspace-accent); color: var(--workspace-surface); }
.primary-btn:hover:not(:disabled) { background: color-mix(in oklch, var(--workspace-accent) 86%, var(--workspace-fg)); }
.secondary-btn { border-color: var(--workspace-border); background: transparent; color: var(--workspace-fg); }
.secondary-btn:hover { border-color: var(--workspace-fg); background: color-mix(in oklch, var(--workspace-fg) 5%, var(--workspace-surface)); }

.today-focus-card { padding: 24px; }
.today-focus-card :deep(.async-state) { padding-block: 44px; }
.focus-top { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 24px; align-items: start; }
.focus-copy { min-width: 0; }
.focus-kicker { margin: 0; color: var(--workspace-muted); font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em; }
.focus-title { margin: 8px 0 0; font-family: var(--font-display); font-size: 24px; line-height: 1.2; }
.focus-meta { display: flex; flex-wrap: wrap; gap: 5px 14px; margin: 8px 0 0; color: var(--workspace-muted); font-size: 13px; }
.focus-meta span + span::before { content: "·"; margin-right: 14px; }
.learning-path { display: grid; grid-template-columns: repeat(3, 1fr); margin-top: 26px; border-top: 1px solid var(--workspace-border); }
.learning-step { position: relative; padding: 20px 16px 0 0; color: var(--workspace-muted); font-size: 12px; }
.learning-step + .learning-step { padding-left: 16px; border-left: 1px solid var(--workspace-border); }
.learning-step strong { display: flex; align-items: center; gap: 4px; margin-bottom: 3px; color: var(--workspace-fg); font-size: 13px; }
.learning-step.is-completed strong :deep(svg) { flex: none; color: var(--workspace-accent); }
.learning-step.is-current strong { font-weight: 700; }
.learning-step.is-current::before {
  content: "";
  position: absolute;
  top: -2px;
  left: 16px;
  width: 32px;
  height: 3px;
  border-radius: var(--radius-full);
  background: var(--workspace-accent);
}

.task-card { min-height: 268px; flex: 1 1 0; display: flex; flex-direction: column; }
.task-filters { display: flex; align-items: center; gap: 3px; padding: 3px; border: 1px solid var(--workspace-border); border-radius: var(--radius-md); background: var(--workspace-bg); }
.task-filter { min-height: 44px; padding: 0 10px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--workspace-muted); font-size: 11px; }
.task-filter:hover { color: var(--workspace-fg); }
.task-filter[aria-pressed="true"] { background: var(--workspace-surface); color: var(--workspace-fg); box-shadow: 0 1px 0 color-mix(in oklch, var(--workspace-fg) 12%, transparent); }
.task-list-wrap { min-height: 0; flex: 1; display: flex; flex-direction: column; }
.task-list-wrap > :deep(*) { min-height: 0; flex: 1; }
.task-list { min-height: 0; display: grid; align-content: start; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--workspace-border) transparent; }
.task-row {
  width: 100%;
  min-height: 88px;
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  padding: 14px 18px;
  border: 0;
  background: transparent;
  color: var(--workspace-fg);
  text-align: left;
}
.task-row + .task-row { border-top: 1px solid var(--workspace-border); }
.task-row:hover:not(:disabled) { background: color-mix(in oklch, var(--workspace-fg) 4%, var(--workspace-surface)); }
.task-glyph { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--workspace-border); border-radius: var(--radius-md); }
.task-copy { min-width: 0; display: block; }
.task-title { display: block; overflow: hidden; color: var(--workspace-fg); font-family: var(--font-body); font-size: 16px; text-overflow: ellipsis; white-space: nowrap; }
.task-meta { display: block; margin-top: 4px; overflow: hidden; color: var(--workspace-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.status-pill { display: inline-flex; align-items: center; min-height: 28px; padding: 0 9px; border: 1px solid var(--workspace-border); border-radius: var(--radius-sm); color: var(--workspace-muted); font-size: 10px; white-space: nowrap; }
.filter-empty,
.term-empty { margin: 0; padding: 24px; color: var(--workspace-muted); text-align: center; }

.schedule-body { padding: 18px; }
.calendar-date { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; padding-bottom: 16px; border-bottom: 1px solid var(--workspace-border); }
.calendar-date > div { display: flex; align-items: flex-end; gap: 6px; }
.calendar-date strong { font-family: var(--font-display); font-size: 32px; line-height: 1; }
.calendar-date span { color: var(--workspace-muted); font-size: 12px; }
.schedule-note { margin: 12px 0 0; padding: 12px; border: 1px dashed var(--workspace-border); border-radius: var(--radius-md); color: var(--workspace-muted); font-size: 12px; }
.schedule-list { display: grid; margin-top: 6px; }
.schedule-item {
  display: grid;
  grid-template-columns: 62px minmax(0, 1fr);
  gap: 12px;
  padding: 14px 0;
  border: 0;
  background: transparent;
  color: var(--workspace-fg);
  text-align: left;
}
.schedule-item + .schedule-item { border-top: 1px solid var(--workspace-border); }
.schedule-time { color: var(--workspace-muted); font-family: var(--font-mono); font-size: 10px; }
.schedule-item strong { display: block; overflow: hidden; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.schedule-item small { display: block; margin-top: 2px; overflow: hidden; color: var(--workspace-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.schedule-body :deep(.async-state) { margin-top: 12px; padding: 12px; border: 1px dashed var(--workspace-border); border-radius: var(--radius-md); }

.text-btn { min-height: 44px; padding: 0 8px; border: 0; background: transparent; color: var(--workspace-fg); font-size: 12px; }
.text-btn:hover { text-decoration: underline; text-underline-offset: 4px; }
.announcement-list { display: grid; }
.announcement-item { padding: 15px 18px; }
.announcement-item + .announcement-item { border-top: 1px solid var(--workspace-border); }
.announcement-item.unread { box-shadow: inset 3px 0 0 var(--workspace-accent); }
.announcement-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.announcement-top strong { min-width: 0; overflow: hidden; font-family: var(--font-body); font-size: 15px; text-overflow: ellipsis; white-space: nowrap; }
.announcement-item p { display: -webkit-box; margin: 5px 0 0; overflow: hidden; color: var(--workspace-muted); font-size: 12px; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.announcement-foot { display: flex; justify-content: flex-end; margin-top: 8px; }
.mark-read-btn { flex: none; min-height: 28px; padding: 0 8px; border: 1px solid var(--workspace-border); border-radius: var(--radius-md); background: transparent; color: var(--workspace-fg); font-size: 10px; }
.mark-read-btn:hover { border-color: var(--workspace-fg); }

.course-section { padding-top: 30px; }
.course-section-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.course-section-head h2 { font-size: 25px; }
.course-section-head > div > p:last-child { margin: 4px 0 0; color: var(--workspace-muted); font-size: 12px; }
.course-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.course-card {
  min-width: 0;
  min-height: 188px;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  justify-content: flex-start;
  padding: 19px;
  border: 1px solid var(--workspace-border);
  border-radius: var(--radius-lg);
  background: var(--workspace-surface);
  color: var(--workspace-fg);
  text-align: left;
  transition: transform 200ms cubic-bezier(0.23, 1, 0.32, 1), border-color 200ms ease;
}
.course-card:hover:not(:disabled) { transform: translateY(-2px); border-color: var(--workspace-fg); }
.course-code { color: var(--workspace-muted); font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.08em; }
.course-title { display: block; margin-top: 12px; overflow: hidden; font-family: var(--font-body); font-size: 20px; line-height: 1.3; text-overflow: ellipsis; white-space: nowrap; }
.course-meta { display: -webkit-box; margin-top: 7px; overflow: hidden; color: var(--workspace-muted); font-size: 12px; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.course-foot { margin-top: auto; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 16px; border-top: 1px solid var(--workspace-border); color: var(--workspace-muted); font-size: 11px; }
.course-foot > span:first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.course-link { flex: none; display: inline-flex; align-items: center; gap: 5px; color: var(--workspace-fg); font-weight: 600; }

@media (max-width: 1120px) {
  .work-grid { grid-template-columns: 1fr; }
  .task-card { min-height: 0; flex-basis: auto; }
  .task-list { max-height: 440px; }
  .side-column { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .course-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 920px) {
  .student-dashboard { padding: 28px 20px 48px; }
  .side-column { grid-template-columns: 1fr; }
}

@media (max-width: 680px) {
  .welcome-section { align-items: flex-start; flex-direction: column; }
  .term-display { align-items: flex-start; flex-direction: column; gap: var(--space-1); }
  .overview-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .overview-metric:nth-child(3) { border-top: 1px solid var(--workspace-border); border-left: 0; }
  .overview-metric:nth-child(4) { border-top: 1px solid var(--workspace-border); }
  .focus-top { grid-template-columns: 1fr; }
  .continue-btn { width: 100%; }
  .learning-path { grid-template-columns: 1fr; }
  .learning-step,
  .learning-step + .learning-step { padding: 14px 0; border-bottom: 1px solid var(--workspace-border); border-left: 0; }
  .learning-step:last-child { padding-bottom: 0; border-bottom: 0; }
  .learning-step.is-current::before { top: auto; bottom: -2px; left: 0; }
  .task-card-head { align-items: flex-start; flex-direction: column; }
  .task-filters { width: 100%; }
  .task-filter { flex: 1; }
  .task-row { grid-template-columns: 34px minmax(0, 1fr); }
  .task-row .status-pill { grid-column: 2; justify-self: start; }
  .course-grid { grid-template-columns: 1fr; }
  .course-section-head { align-items: flex-start; }
}

@media (max-width: 480px) {
  .student-dashboard { padding-inline: 16px; }
  .course-section-head { flex-direction: column; }
  .all-courses-btn { width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .student-dashboard *,
  .student-dashboard *::before,
  .student-dashboard *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
  }
}
</style>
