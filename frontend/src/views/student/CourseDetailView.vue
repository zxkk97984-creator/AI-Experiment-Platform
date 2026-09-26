<script setup>
// 课程概览（参考图 03）：hero + 七个标签页。
// 概览双栏：左章节路径 + 最近作业考试；右待办任务 + 课程反馈 + 考试与公告。
// 数据全部来自既有 API 与本地真实学习记录；403/404/通用失败保留恢复动作。

import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppLayout from '../../components/layout/AppLayout.vue'
import StudentCourseHero from '../../components/student/StudentCourseHero.vue'
import StudentCourseTabs from '../../components/student/StudentCourseTabs.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiPanel from '../../components/ui/UiPanel.vue'
import { coursesAPI } from '../../api/courses.js'
import { progressAPI } from '../../api/progress.js'
import { assignmentsAPI } from '../../api/assignments.js'
import { examsAPI } from '../../api/exams.js'
import { dashboardAPI } from '../../api/dashboard.js'
import { useAppStore } from '../../stores/app.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const course = ref(null)
const chapters = ref([])
const assignments = ref([])
const exams = ref([])
const dashboard = ref(null)
const loading = ref(true)
const enrolling = ref(false)
const enrolled = ref(false)
const fetchError = ref(false)
const notFound = ref(false)
const forbidden = ref(false)
const tab = ref('overview')

const courseId = computed(() => route.params.id)

const timeFmt = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const kindLabel = { assignment: '作业', exam: '考试', experiment: '实验' }

// TASK-018：学习进度以服务端为事实（跨设备一致），不读取 localStorage 历史
const courseProgress = ref(null)

const completedLessonIds = computed(() => {
  return (courseProgress.value?.items || [])
    .filter((item) => item.status === 'completed')
    .map((item) => item.lesson_id)
})

const totalLessons = computed(() => {
  let count = 0
  for (const ch of chapters.value) count += ch?.lessons?.length || 0
  return count
})

const progressPercent = computed(() => courseProgress.value?.percent ?? 0)

const nextLesson = computed(() => {
  const nextId = courseProgress.value?.next_lesson_id
  if (nextId == null) return null
  for (const ch of chapters.value) {
    for (const l of ch?.lessons || []) {
      if (l.id === nextId) return l
    }
  }
  return null
})

/** 课时状态：completed / current（第一个未完成）/ locked（后续） */
const lessonStates = computed(() => {
  const completed = new Set(completedLessonIds.value)
  const states = new Map()
  let currentFound = false
  for (const ch of chapters.value) {
    for (const l of ch?.lessons || []) {
      if (completed.has(l.id)) { states.set(l.id, 'completed'); continue }
      if (!currentFound) { states.set(l.id, 'current'); currentFound = true }
      else states.set(l.id, 'locked')
    }
  }
  return states
})

/** 概览右列：待办任务（来自首页聚合，按课程名匹配） */
const pendingTasks = computed(() => {
  const title = course.value?.title
  if (!title) return []
  return (dashboard.value?.priority_items || []).filter((i) => i.course_title === title).slice(0, 3)
})

/** 课程反馈（聚合中本课程的最近反馈） */
const courseFeedback = computed(() => {
  const title = course.value?.title
  if (!title) return []
  return (dashboard.value?.recent_feedback || []).filter((i) => i.course_title === title)
})

/** 本课程公告 */
const courseAnnouncements = computed(() =>
  (dashboard.value?.announcements || []).filter((a) => Number(a.course_id) === Number(courseId.value)),
)

/** 下一场考试：最近一场尚未开始的 */
const nextExam = computed(() => {
  const now = Date.now()
  const future = exams.value.filter((e) => e.starts_at && new Date(e.starts_at).getTime() > now)
  return future.sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at))[0] || null
})

/** 实验入口：章节中的 notebook 课时 */
const experimentLessons = computed(() =>
  chapters.value.flatMap((ch) => (ch?.lessons || []).filter((l) => l.content_type === 'notebook')),
)

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : timeFmt.format(date)
}

function stateClass(lesson) {
  return `is-${lessonStates.value.get(lesson.id) || 'locked'}`
}

function stateLabel(lesson, fallback) {
  const s = lessonStates.value.get(lesson.id)
  if (s === 'completed') return `${fallback}，已完成`
  if (s === 'current') return `${fallback}，当前学习`
  return `${fallback}，未解锁`
}

function lessonIcon(lesson) {
  const s = lessonStates.value.get(lesson.id)
  if (s === 'completed') return 'check'
  if (s === 'current') return 'book'
  return 'lock'
}

async function fetchAll() {
  loading.value = true
  fetchError.value = false
  notFound.value = false
  forbidden.value = false
  // 先取课程元数据：403 = 无权访问或课程不可见；404 = 不存在
  let courseRes
  try {
    courseRes = await coursesAPI.get(courseId.value)
  } catch (e) {
    const status = e.response?.status
    if (status === 403) forbidden.value = true
    else if (status === 404) notFound.value = true
    else {
      fetchError.value = true
      app.showToast('加载课程失败', 'error')
    }
    loading.value = false
    return
  }
  course.value = courseRes.data
  enrolled.value = course.value.is_enrolled === true
  if (enrolled.value) {
    // 已选课：并发加载章节、作业、考试与 dashboard
    const results = await Promise.allSettled([
      coursesAPI.getChapters(courseId.value),
      assignmentsAPI.list({ course_id: courseId.value }),
      examsAPI.list({ course_id: courseId.value }),
      dashboardAPI.student(),
      progressAPI.getCourse(courseId.value),
    ])
    if (results[0].status === 'fulfilled') {
      chapters.value = results[0].value.data?.items || results[0].value.data || []
    }
    if (results[1].status === 'fulfilled') {
      assignments.value = results[1].value.data?.items || results[1].value.data || []
    }
    if (results[2].status === 'fulfilled') {
      exams.value = results[2].value.data?.items || results[2].value.data || []
    }
    if (results[3].status === 'fulfilled') {
      dashboard.value = results[3].value.data
    }
    if (results[4].status === 'fulfilled') {
      courseProgress.value = results[4].value.data
    }
  } else {
    // 未选课：不请求章节/作业/考试/dashboard，避免预期中的 403
    chapters.value = []
    assignments.value = []
    exams.value = []
    dashboard.value = null
  }
  loading.value = false
}

async function handleEnroll() {
  enrolling.value = true
  try {
    await coursesAPI.enroll(courseId.value)
    app.showToast('选课成功', 'success')
    enrolled.value = true
    await fetchAll()
  } catch (e) {
    const msg = e.response?.data?.detail?.message || '选课失败'
    app.showToast(msg, 'error')
  } finally { enrolling.value = false }
}

function goLesson(lesson) {
  router.push(`/student/courses/${courseId.value}/lessons/${lesson.id}`)
}
function goAssignment(id) {
  router.push(`/student/assignments/${id}`)
}
function goExam(id) {
  router.push(`/student/exams/${id}`)
}
function goExperiment(lessonId) {
  router.push(`/student/experiments/${lessonId}`)
}
function goNext() {
  if (nextLesson.value) goLesson(nextLesson.value)
  else tab.value = 'chapters'
}
function goBack() {
  router.push('/student/courses')
}

/** 仅允许服务端返回的学生相对路由 */
function go(route) {
  if (route === '/student' || route.startsWith('/student/')) {
    router.push(route)
  }
}

/** 分数色调：null → 待评分（warning）；<60 → 需修改（danger）；≥60 → 通过（success） */
function scoreTone(score) {
  if (score == null) return ''
  const s = Number(score)
  if (Number.isNaN(s)) return ''
  return s < 60 ? 'is-needs-revision' : 'is-passed'
}

onMounted(fetchAll)
</script>

<template>
  <AppLayout content-mode="list">
    <!-- Loading -->
    <div v-if="loading" class="detail-loading">
      <div class="skeleton" style="height:166px;width:100%;margin-bottom:16px"></div>
      <div class="skeleton" style="height:52px;width:100%;margin-bottom:20px"></div>
      <div class="skeleton" style="height:120px;width:100%"></div>
    </div>

    <!-- 通用失败 -->
    <div v-else-if="fetchError" class="empty-state">
      <p>加载课程失败</p>
      <button type="button" class="btn-primary retry-btn" @click="fetchAll" style="margin-top:12px">重试</button>
    </div>

    <!-- 无权访问或课程不可见 -->
    <div v-else-if="forbidden" class="empty-state">
      <p>无权访问或课程不可见</p>
      <button type="button" class="btn-primary back-list-btn" @click="goBack" style="margin-top:12px">返回课程列表</button>
    </div>

    <!-- 课程不存在 -->
    <div v-else-if="notFound" class="empty-state">
      <p>课程不存在</p>
      <button type="button" class="btn-primary back-list-btn" @click="goBack" style="margin-top:12px">返回课程列表</button>
    </div>

    <!-- 未选课：仅展示 hero 与选课 CTA，不加载内容 -->
    <div v-else-if="course && !enrolled" class="course-detail">
      <StudentCourseHero
        :course="course"
        :progress="0"
        :total-lessons="0"
        :completed-lessons="0"
        :total-chapters="0"
        :enrolled="false"
        :enrolling="enrolling"
        @enroll="handleEnroll"
        @back="goBack"
      />
      <div class="empty-state">
        <p>你还未选这门课，加入后即可查看课程内容</p>
        <button
          type="button"
          class="btn-primary hero-enroll-btn"
          :disabled="enrolling"
          @click="handleEnroll"
          style="margin-top:12px"
        >
          {{ enrolling ? '选课中...' : '立即选课' }}
        </button>
      </div>
    </div>

    <!-- 防御：课程缺失 -->
    <div v-else-if="!course" class="empty-state">
      <p>课程不存在</p>
      <button type="button" class="btn-primary back-list-btn" @click="goBack" style="margin-top:12px">返回课程列表</button>
    </div>

    <template v-else>
      <div class="course-detail">
        <StudentCourseHero
          :course="course"
          :progress="progressPercent"
          :total-lessons="totalLessons"
          :completed-lessons="completedLessonIds.length"
          :total-chapters="chapters.length"
          :enrolled="enrolled"
          @continue="goNext"
          @enroll="handleEnroll"
          @back="goBack"
        />

        <StudentCourseTabs :active="tab" @change="tab = $event" />

        <!-- ── 概览 ─────────────────────────────────────────────── -->
        <div v-if="tab === 'overview'" class="overview-grid">
          <div class="overview-left">
            <UiPanel compact class="chapters-panel">
              <template #header><h2 class="panel-title">章节路径</h2></template>
              <ul v-if="chapters.length" class="chapter-list">
                <li v-for="ch in chapters" :key="ch.id">
                  <div class="chapter-group-title">第{{ ch.order_index + 1 }}章 {{ ch.title }}</div>
                  <button
                    v-for="l in ch.lessons"
                    :key="l.id"
                    type="button"
                    class="chapter-row"
                    :class="stateClass(l)"
                    :aria-label="stateLabel(l, l.title)"
                    @click="goLesson(l)"
                  >
                    <span class="chapter-row-icon" aria-hidden="true">
                      <AppIcon :name="lessonIcon(l)" :size="16" />
                    </span>
                    <span class="chapter-row-title">{{ l.title }}</span>
                    <span v-if="lessonStates.get(l.id) === 'completed'" class="chapter-check" aria-hidden="true">
                      <AppIcon name="check" :size="14" />
                    </span>
                  </button>
                </li>
              </ul>
              <p v-else class="empty-inline">暂无章节内容</p>
            </UiPanel>

            <UiPanel compact class="recent-panel">
              <template #header><h2 class="panel-title">最近作业与考试</h2></template>
              <div v-if="assignments.length || exams.length" class="recent-list">
                <button
                  v-for="a in assignments.slice(0, 3)"
                  :key="'a' + a.id"
                  type="button"
                  class="recent-row"
                  @click="goAssignment(a.id)"
                >
                  <span class="recent-row-icon" aria-hidden="true"><AppIcon name="assignment" :size="16" /></span>
                  <span class="recent-row-text">
                    <span class="recent-row-title">{{ a.title }}</span>
                    <span class="recent-row-meta">作业 · 截止 {{ formatTime(a.due_at) }}</span>
                  </span>
                  <AppIcon name="chevron-right" :size="14" />
                </button>
                <button
                  v-for="e in exams.slice(0, 3)"
                  :key="'e' + e.id"
                  type="button"
                  class="recent-row"
                  @click="goExam(e.id)"
                >
                  <span class="recent-row-icon" aria-hidden="true"><AppIcon name="exam" :size="16" /></span>
                  <span class="recent-row-text">
                    <span class="recent-row-title">{{ e.title }}</span>
                    <span class="recent-row-meta">考试 · {{ formatTime(e.starts_at) }}</span>
                  </span>
                  <AppIcon name="chevron-right" :size="14" />
                </button>
              </div>
              <p v-else class="empty-inline">暂无作业与考试</p>
            </UiPanel>
          </div>

          <div class="overview-right">
            <UiPanel compact class="pending-panel">
              <template #header><h2 class="panel-title">待办任务</h2></template>
              <ul v-if="pendingTasks.length" class="side-list">
                <li v-for="item in pendingTasks" :key="item.kind + '-' + item.id" class="side-row">
                  <span class="side-row-dot" :class="'urgency-' + item.urgency" aria-hidden="true"></span>
                  <button type="button" class="side-row-main" @click="go(item.route)">
                    <span class="side-row-title">{{ item.title }}</span>
                    <span class="side-row-meta">{{ kindLabel[item.kind] || item.kind }}<template v-if="item.time_at"> · {{ formatTime(item.time_at) }}</template></span>
                  </button>
                </li>
              </ul>
              <p v-else class="empty-inline">暂无待办任务</p>
            </UiPanel>

            <UiPanel compact class="feedback-panel">
              <template #header><h2 class="panel-title">课程反馈</h2></template>
              <ul v-if="courseFeedback.length" class="side-list">
                <li v-for="item in courseFeedback.slice(0, 3)" :key="item.kind + '-' + item.id" class="side-row">
                  <span class="side-score" :class="scoreTone(item.score)">{{ item.score ?? '—' }}</span>
                  <button type="button" class="side-row-main" @click="go(item.route)">
                    <span class="side-row-title">{{ item.title }}</span>
                    <span class="side-row-meta">{{ item.feedback || '暂无文字反馈' }}</span>
                  </button>
                </li>
              </ul>
              <p v-else class="empty-inline">暂无反馈</p>
            </UiPanel>

            <UiPanel compact class="upcoming-panel">
              <template #header><h2 class="panel-title">考试与公告</h2></template>
              <ul v-if="nextExam || courseAnnouncements.length" class="side-list">
                <li v-if="nextExam" class="side-row">
                  <span class="side-row-icon" aria-hidden="true"><AppIcon name="exam" :size="16" /></span>
                  <button type="button" class="side-row-main" @click="goExam(nextExam.id)">
                    <span class="side-row-title">下一场考试：{{ nextExam.title }}</span>
                    <span class="side-row-meta">{{ formatTime(nextExam.starts_at) }} · {{ nextExam.duration_minutes }} 分钟</span>
                  </button>
                </li>
                <li v-for="a in courseAnnouncements.slice(0, 3)" :key="a.id" class="side-row">
                  <span class="side-row-icon" aria-hidden="true"><AppIcon name="notification" :size="16" /></span>
                  <div class="side-row-main">
                    <span class="side-row-title">{{ a.title }}</span>
                    <span class="side-row-meta">{{ a.content }}</span>
                  </div>
                </li>
              </ul>
              <p v-else class="empty-inline">暂无考试与公告</p>
            </UiPanel>
          </div>
        </div>

        <!-- ── 章节内容 ─────────────────────────────────────────── -->
        <UiPanel v-else-if="tab === 'chapters'" compact>
          <template #header><h2 class="panel-title">章节内容</h2></template>
          <ul v-if="chapters.length" class="chapter-list">
            <li v-for="ch in chapters" :key="ch.id">
              <div class="chapter-group-title">第{{ ch.order_index + 1 }}章 {{ ch.title }}</div>
              <button
                v-for="l in ch.lessons"
                :key="l.id"
                type="button"
                class="chapter-row"
                :class="stateClass(l)"
                :aria-label="stateLabel(l, l.title)"
                @click="goLesson(l)"
              >
                <span class="chapter-row-icon" aria-hidden="true"><AppIcon :name="lessonIcon(l)" :size="16" /></span>
                <span class="chapter-row-title">{{ l.title }}</span>
                <span v-if="lessonStates.get(l.id) === 'completed'" class="chapter-check" aria-hidden="true">
                  <AppIcon name="check" :size="14" />
                </span>
              </button>
            </li>
          </ul>
          <p v-else class="empty-inline">暂无章节内容</p>
        </UiPanel>

        <!-- ── 作业 ─────────────────────────────────────────────── -->
        <UiPanel v-else-if="tab === 'assignments'" compact>
          <template #header><h2 class="panel-title">作业</h2></template>
          <div v-if="assignments.length" class="recent-list">
            <button v-for="a in assignments" :key="a.id" type="button" class="recent-row" @click="goAssignment(a.id)">
              <span class="recent-row-icon" aria-hidden="true"><AppIcon name="assignment" :size="16" /></span>
              <span class="recent-row-text">
                <span class="recent-row-title">{{ a.title }}</span>
                <span class="recent-row-meta">截止 {{ formatTime(a.due_at) }}</span>
              </span>
              <AppIcon name="chevron-right" :size="14" />
            </button>
          </div>
          <p v-else class="empty-inline">暂无作业</p>
        </UiPanel>

        <!-- ── 实验 ─────────────────────────────────────────────── -->
        <UiPanel v-else-if="tab === 'experiments'" compact>
          <template #header><h2 class="panel-title">实验</h2></template>
          <div v-if="experimentLessons.length" class="recent-list">
            <button v-for="l in experimentLessons" :key="l.id" type="button" class="recent-row" @click="goExperiment(l.id)">
              <span class="recent-row-icon" aria-hidden="true"><AppIcon name="experiment" :size="16" /></span>
              <span class="recent-row-text">
                <span class="recent-row-title">{{ l.title }}</span>
                <span class="recent-row-meta">实验课时</span>
              </span>
              <AppIcon name="chevron-right" :size="14" />
            </button>
          </div>
          <p v-else class="empty-inline">暂无实验</p>
        </UiPanel>

        <!-- ── 考试 ─────────────────────────────────────────────── -->
        <UiPanel v-else-if="tab === 'exams'" compact>
          <template #header><h2 class="panel-title">考试</h2></template>
          <div v-if="exams.length" class="recent-list">
            <button v-for="e in exams" :key="e.id" type="button" class="recent-row exam-row-link" @click="goExam(e.id)">
              <span class="recent-row-icon" aria-hidden="true"><AppIcon name="exam" :size="16" /></span>
              <span class="recent-row-text">
                <span class="recent-row-title">{{ e.title }}</span>
                <span class="recent-row-meta">{{ formatTime(e.starts_at) }} · {{ e.duration_minutes }} 分钟</span>
              </span>
              <AppIcon name="chevron-right" :size="14" />
            </button>
          </div>
          <p v-else class="empty-inline">暂无考试</p>
        </UiPanel>

        <!-- ── 公告 ─────────────────────────────────────────────── -->
        <UiPanel v-else-if="tab === 'announcements'" compact>
          <template #header><h2 class="panel-title">公告</h2></template>
          <div v-if="courseAnnouncements.length" class="recent-list">
            <div v-for="a in courseAnnouncements" :key="a.id" class="recent-row static">
              <span class="recent-row-icon" aria-hidden="true"><AppIcon name="notification" :size="16" /></span>
              <span class="recent-row-text">
                <span class="recent-row-title">{{ a.title }}</span>
                <span class="recent-row-meta">{{ a.content }} · {{ a.author_name }} · {{ formatTime(a.published_at) }}</span>
              </span>
            </div>
          </div>
          <p v-else class="empty-inline">暂无公告</p>
        </UiPanel>

        <!-- ── 成绩 ─────────────────────────────────────────────── -->
        <UiPanel v-else-if="tab === 'grades'" compact>
          <template #header><h2 class="panel-title">成绩</h2></template>
          <div v-if="courseFeedback.length" class="recent-list">
            <div v-for="item in courseFeedback" :key="item.kind + '-' + item.id" class="recent-row static">
              <span class="recent-row-icon" aria-hidden="true"><AppIcon name="chart" :size="16" /></span>
              <span class="recent-row-text">
                <span class="recent-row-title">{{ item.title }} · <span class="score-text" :class="scoreTone(item.score)">{{ item.score ?? '待评分' }}</span></span>
                <span class="recent-row-meta">{{ item.feedback || '暂无文字反馈' }} · {{ formatTime(item.graded_at) }}</span>
              </span>
            </div>
          </div>
          <p v-else class="empty-inline">暂无成绩</p>
        </UiPanel>
      </div>
    </template>
  </AppLayout>
</template>

<style scoped>
.course-detail {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* 课程详情页修复：教学班 chip 拼接长串会撑出卡片覆盖「继续学习」按钮。
   组件保持原样，仅在此页 scoped 内用 :deep() 约束其 hero-meta 行换行且不溢出。
   注意：scoped 内 :deep() 选择器以本页元素为作用域根，不影响其他页面。 */
.course-detail :deep(.hero-meta) {
  flex-wrap: wrap;
  max-width: 100%;
}
.course-detail :deep(.hero-chip) {
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 0 1 auto;
}

.panel-title {
  margin: 0;
  font-size: var(--text-base);
  font-weight: 700;
  color: var(--fg);
}

.empty-inline {
  margin: 0;
  padding: 16px 0;
  text-align: center;
  font-size: var(--text-sm);
  color: var(--faint);
}

.detail-loading { display: flex; flex-direction: column; gap: 0; }

/* ── 概览双栏（左 1.15fr / 右 0.85fr） ───────────────────────── */
.overview-grid {
  display: grid;
  grid-template-columns: 1.15fr 0.85fr;
  gap: 20px;
  align-items: start;
}
.overview-left, .overview-right {
  display: flex;
  flex-direction: column;
  gap: 20px;
  min-width: 0;
}

/* ── 章节路径 ───────────────────────────────────────────────── */
.chapter-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 14px; }
.chapter-group-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
  margin-bottom: 6px;
}
.chapter-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 44px;
  padding: 6px 10px;
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  text-align: left;
  font-family: var(--font-body);
}
.chapter-row:hover { background: var(--bg); }
.chapter-row-icon {
  flex-shrink: 0;
  display: inline-flex;
  color: var(--faint);
}
.chapter-row-title {
  flex: 1;
  font-size: var(--text-sm);
  color: var(--fg);
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chapter-check {
  flex-shrink: 0;
  display: inline-flex;
  color: var(--success);
}

/* 状态样式：当前为蓝色、锁定为灰、已完成含勾 */
.chapter-row.is-current .chapter-row-icon { color: var(--accent); }
.chapter-row.is-current .chapter-row-title { font-weight: 600; color: var(--accent); }
.chapter-row.is-locked { cursor: default; opacity: 0.6; }
.chapter-row.is-locked:hover { background: transparent; }
.chapter-row.is-completed .chapter-row-title { color: var(--muted); }

/* ── 最近作业考试 / 通用行 ─────────────────────────────────── */
.recent-list { display: flex; flex-direction: column; }
.recent-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 52px;
  padding: 8px 4px;
  background: none;
  border: none;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  text-align: left;
  font-family: var(--font-body);
}
.recent-row:last-child { border-bottom: none; }
.recent-row.static { cursor: default; }
.recent-row-icon {
  flex-shrink: 0;
  display: inline-flex;
  color: var(--faint);
}
.recent-row-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.recent-row-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
}
.recent-row:hover .recent-row-title { color: var(--accent); }
.recent-row.static:hover .recent-row-title { color: var(--fg); }
.recent-row-meta {
  font-size: var(--text-xs);
  color: var(--faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.score-text { font-weight: 700; }

/* ── 侧栏列表 ───────────────────────────────────────────────── */
.side-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.side-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 52px;
  padding: 8px 4px;
  border-bottom: 1px solid var(--border);
}
.side-row:last-child { border-bottom: none; }
.side-row-dot {
  flex-shrink: 0;
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--faint);
}
.side-row-dot.urgency-urgent { background: var(--danger); }
.side-row-dot.urgency-soon { background: var(--warning); }
.side-row-icon {
  flex-shrink: 0;
  display: inline-flex;
  color: var(--faint);
}
.side-score {
  flex-shrink: 0;
  font-size: var(--text-sm);
  font-weight: 700;
  min-width: 34px;
  text-align: center;
  color: var(--warning);
}
.side-score.is-passed { color: var(--success); }
.side-score.is-needs-revision { color: var(--danger); }
.side-row-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  text-align: left;
  font-family: var(--font-body);
}
.side-row-title {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
}
.side-row-main:hover .side-row-title { color: var(--accent); }
.side-row-meta {
  font-size: var(--text-xs);
  color: var(--faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 响应式 ─────────────────────────────────────────────────── */
@media (max-width: 1199px) {
  .overview-grid { grid-template-columns: 1fr; }
}
</style>
