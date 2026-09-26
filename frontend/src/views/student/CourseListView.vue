<script setup>
// 我的课程（参考图 02）：标题 + 状态标签页 + 搜索 + 横向课程行。
// 选课状态以后端 is_enrolled 为准，只对已选课程抓取章节；
// 进度与下一步全部来自本地真实学习记录，失败降级为 0 而不是隐藏课程。

import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppLayout from '../../components/layout/AppLayout.vue'
import CourseIdentity from '../../components/student/CourseIdentity.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiProgress from '../../components/ui/UiProgress.vue'
import { coursesAPI } from '../../api/courses.js'
import { progressAPI } from '../../api/progress.js'
import { useAppStore } from '../../stores/app.js'
import { getCourseCoverUrl } from '../../utils/courseCover.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const courses = ref([])
const rows = ref([])
const loading = ref(true)
const error = ref(false)
const activeTab = ref('all')
const query = ref(typeof route.query?.q === 'string' ? route.query.q : '')

async function fetchCourses() {
  loading.value = true
  error.value = false
  try {
    const res = await coursesAPI.list({ page: 1, page_size: 100 })
    courses.value = res.data.items || []
  } catch {
    error.value = true
    courses.value = []
  }
  await loadRows()
  loading.value = false
}

async function loadRows() {
  // 选课状态以后端 is_enrolled 为准：只对已选课程抓取章节与服务端进度，
  // 不再用章节 403 猜测是否已选课；进度以服务端为事实（TASK-018）。
  const results = await Promise.allSettled(
    courses.value.map((c) =>
      c.is_enrolled
        ? Promise.all([coursesAPI.getChapters(c.id), progressAPI.getCourse(c.id)])
        : Promise.resolve([{ data: { items: [] } }, { data: null }]),
    ),
  )
  rows.value = courses.value.map((course, i) => {
    const res = results[i]
    const enrolled = course.is_enrolled === true
    const chapters = enrolled ? (res.value?.[0]?.data?.items || []) : []
    const serverProgress = enrolled ? (res.value?.[1]?.data || null) : null
    const nextLessonId = serverProgress?.next_lesson_id
    let nextLesson = null
    for (const ch of chapters) {
      for (const l of ch?.lessons || []) {
        if (l.id === nextLessonId) nextLesson = l
      }
    }
    return {
      course,
      enrolled,
      chapters,
      progress: serverProgress?.percent ?? 0,
      nextLesson,
      chapterTitle: chapters[0]?.title || '',
    }
  })
}

const filteredRows = computed(() => {
  let list = rows.value
  const q = query.value.trim().toLowerCase()
  if (q) {
    list = list.filter((r) =>
      [r.course.title, r.course.description || ''].join(' ').toLowerCase().includes(q),
    )
  }
  if (activeTab.value === 'active') return list.filter((r) => r.progress > 0 && r.progress < 100)
  if (activeTab.value === 'done') return list.filter((r) => r.progress === 100)
  return list
})

async function handleEnroll(course) {
  try {
    await coursesAPI.enroll(course.id)
    app.showToast('选课成功', 'success')
    // 重新拉取列表，以服务端最新 is_enrolled 为准
    await fetchCourses()
  } catch (e) {
    const msg = e.response?.data?.detail?.message || '选课失败'
    app.showToast(msg, 'error')
  }
}

async function handleUnenroll(course) {
  try { await coursesAPI.unenroll(course.id); app.showToast('已退选', 'success'); await fetchCourses() }
  catch (e) { app.showToast(e.response?.data?.detail?.message || '退选失败', 'error') }
}

function goDetail(id) { router.push(`/student/courses/${id}`) }
function goLesson(id, lesson) { router.push(`/student/courses/${id}/lessons/${lesson.id}`) }

function metaText(row) {
  const term = row.course.academic_term?.name || '未设置学期'
  const list = row.course.teaching_classes || []
  const classes = list.length ? `${list[0].code || list[0].name}${list.length > 1 ? ` 等 ${list.length} 个班` : ''}` : '未设置教学班'
  return `${term} · ${classes}`
}

function nextText(row) {
  if (!row.enrolled) return '加入课程后开始学习'
  if (row.nextLesson) return `下一步：${row.nextLesson.title}`
  return '本课程已全部完成'
}

onMounted(fetchCourses)
</script>

<template>
  <AppLayout content-mode="list">
    <div class="page">
      <!-- 页头 -->
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">学习 / 课程</p>
          <h1 class="page-title">我的课程</h1>
          <p class="lead page-sub">查看课程进度，继续你的学习旅程</p>
        </div>
        <div class="ph-actions"><span class="page-count">{{ rows.length }} 门课程</span></div>
      </section>

      <!-- 标签页 + 搜索 -->
      <div class="course-toolbar">
        <div class="tabs course-tabs" role="tablist" aria-label="课程状态">
          <button
            v-for="tab in [
              { key: 'all', label: '全部课程' },
              { key: 'active', label: '进行中' },
              { key: 'done', label: '已完成' },
            ]"
            :key="tab.key"
            type="button"
            class="tab tab-btn"
            :class="{ active: activeTab === tab.key }"
            role="tab"
            :aria-selected="activeTab === tab.key"
            @click="activeTab = tab.key"
          >
            {{ tab.label }}
          </button>
        </div>
        <label class="searchbox search-box" :class="{ 'has-value': query }" style="width: 260px;">
          <AppIcon name="search" :size="15" />
          <input
            v-model="query"
            type="search"
            class="input search-input"
            placeholder="搜索课程"
            aria-label="搜索课程"
          />
          <button v-if="query" type="button" class="clear" aria-label="清空课程搜索" @click="query = ''">
            <AppIcon name="close" :size="13" />
          </button>
        </label>
      </div>

      <!-- 状态区 -->
      <DashboardAsyncState
        :loading="loading"
        :error="error"
        :empty="filteredRows.length === 0"
        empty-title="暂无课程"
        empty-body="没有符合条件的课程"
        @retry="fetchCourses"
      >
        <!-- 课程行列表 -->
        <div class="course-list">
          <article v-for="row in filteredRows" :key="row.course.id" class="course-row">
            <!-- 身份（约 34%） -->
            <button type="button" class="course-row-link" @click="goDetail(row.course.id)">
              <CourseIdentity
                :title="row.course.title"
                :meta="metaText(row)"
                :cover-url="getCourseCoverUrl(row.course)"
              />
            </button>

            <!-- 进度（约 22%） -->
            <div class="course-row-progress">
              <span class="progress-text">{{ row.enrolled ? `已学 ${row.progress}%` : '尚未加入' }}</span>
              <UiProgress :value="row.progress" />
            </div>

            <!-- 下一步 + 动作 -->
            <div class="course-row-action">
              <span class="action-next">{{ nextText(row) }}</span>
              <div class="action-buttons">
                <button
                  v-if="!row.enrolled"
                  type="button"
                  class="btn-primary enroll-btn"
                  @click="handleEnroll(row.course)"
                >
                  选课
                </button>
                <button
                  v-else
                  type="button"
                  class="btn-primary continue-btn"
                  @click="row.nextLesson ? goLesson(row.course.id, row.nextLesson) : goDetail(row.course.id)"
                >
                  {{ row.nextLesson ? '继续学习' : '查看课程' }}
                </button>
                <button v-if="row.enrolled && row.course.enrollment_origin !== 'class'" type="button" class="btn-outline" @click="handleUnenroll(row.course)">退选</button>
                <span v-else-if="row.course.enrollment_origin === 'class'" class="class-enrolled">班级统一加入</span>
                <button type="button" class="btn-outline detail-btn" @click="goDetail(row.course.id)">
                  课程详情
                </button>
              </div>
            </div>
          </article>
        </div>
      </DashboardAsyncState>
    </div>
  </AppLayout>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--space-5); }
.page-title { margin: 0; font-size: var(--text-3xl); font-weight: 600; color: var(--fg); letter-spacing: -0.01em; line-height: var(--lh-tight); font-family: var(--font-display); }
.page-sub { margin-top: 6px; }
.page-count { padding: 4px 10px; background: var(--surface-sunken); border: 1px solid var(--border); border-radius: var(--radius-sm); font-family: var(--font-mono); font-size: var(--text-sm); color: var(--muted); font-weight: 500; }

.course-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
.course-tabs { flex: 1; }
.search-box { flex: none; }
.search-input { width: 100%; }

.course-list { display: flex; flex-direction: column; gap: 12px; }
.course-row {
  display: flex; align-items: center; gap: 24px; min-height: 132px; padding: 20px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
}
.course-row:hover { background: var(--surface); border-color: var(--border-strong); }
.course-row-link { flex: 0 0 34%; justify-content: flex-start; background: none; border: 0; padding: 0; cursor: pointer; text-align: left; }
.course-row-progress { flex: 0 0 22%; display: flex; flex-direction: column; gap: 8px; }
.progress-text { font-size: var(--text-sm); font-weight: 600; color: var(--fg); }
.course-row-action { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }
.action-next { font-size: var(--text-sm); color: var(--muted); text-align: right; }
.action-buttons { display: flex; gap: 8px; }
.btn-outline { height: 32px; padding: 0 12px; background: var(--surface); border: 1px solid var(--border-strong); border-radius: var(--radius-md); font-size: var(--text-md); font-weight: 500; color: var(--fg); cursor: pointer; }
.btn-outline:hover { border-color: var(--fg); }
.class-enrolled { padding: 4px 8px; border-radius: var(--radius-sm); background: var(--accent-soft); color: var(--accent); font-size: var(--text-sm); white-space: nowrap; }

@media (max-width: 1024px) {
  .course-row { flex-wrap: wrap; }
  .course-row-link { flex: 1 1 100%; }
  .course-row-progress { flex: 1 1 40%; }
  .course-row-action { flex: 1 1 50%; }
}
@media (max-width: 820px) {
  .course-toolbar { flex-direction: column; align-items: stretch; }
  .search-box { width: 100% !important; }
  .course-row { padding: 16px; gap: 16px; }
  .course-row-progress, .course-row-action { flex: 1 1 100%; align-items: flex-start; }
  .action-next { text-align: left; }
}
</style>
