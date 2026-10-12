<script setup>
// 课程管理（V2）：Page Head + Metric Strip + Toolbar + Dense Table + Modal。
// 业务逻辑与 API 不变：真实课程列表、搜索/筛选/排序/分页、发布与创建。

import { ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import ClassSummary from '../../components/common/ClassSummary.vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import CourseCreateModal from '../../components/teacher/CourseCreateModal.vue'
import CourseGradebookTemplateModal from '../../components/teacher/CourseGradebookTemplateModal.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { coursesAPI } from '../../api/courses.js'
import { academicsAPI } from '../../api/academics.js'
import { useAppStore } from '../../stores/app.js'
import { coursePublishMissingMessage } from '../../utils/coursePublish.js'
import { formatDateTime } from '../../utils/format.js'

const router = useRouter()
const app = useAppStore()
const courses = ref([])
const terms = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 10
const serverSummary = ref({ total: 0, published: 0, draft: 0, archived: 0 })
const loading = ref(true)
const loadError = ref('')
const showCreate = ref(false)
const templateCourse = ref(null)
const query = ref('')
const statusFilter = ref('all')
const termFilter = ref('all')
const sortOrder = ref('updated')

const courseStatus = (course) => course.status || 'draft'
const courseLabel = (status) => ({ published: '已发布', draft: '草稿', archived: '已归档' })[status] || status
const badgeTone = (status) => ({ published: 'success', draft: 'neutral', archived: 'neutral' })[status] || 'neutral'
const courseUpdated = (course) => course.updated_at || course.created_at || course.updatedAt || ''
const summary = computed(() => serverSummary.value)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const managePath = (course) => `${router.currentRoute?.value?.path?.startsWith('/admin') ? '/admin' : '/teacher'}/courses/${course.id}/manage`

function resetPage() { page.value = 1; fetchCourses() }
function goToPage(next) { page.value = next; fetchCourses() }

async function fetchCourses() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await coursesAPI.list({
      page: page.value, page_size: pageSize,
      q: query.value.trim() || undefined,
      status_filter: statusFilter.value === 'all' ? undefined : statusFilter.value,
      academic_term_id: termFilter.value === 'all' ? undefined : Number(termFilter.value),
      sort_by: sortOrder.value,
    })
    courses.value = res.data.items || []
    total.value = res.data.total || 0
    serverSummary.value = res.data.summary || serverSummary.value
  } catch {
    loadError.value = '课程暂时无法加载，请稍后重试。'
    app.showToast('加载失败', 'error')
  } finally {
    loading.value = false
  }
}

function openTemplate(course) { templateCourse.value = course }

function handleCourseCreated(course) {
  showCreate.value = false
  if (course?.id) {
    router.push(managePath(course))
    return
  }
  fetchCourses()
}

async function handlePublish(course) {
  const missingMessage = coursePublishMissingMessage(course)
  if (missingMessage) {
    app.showToast(missingMessage, 'error')
    return
  }
  try {
    await coursesAPI.update(course.id, { status: 'published' })
    app.showToast('已发布', 'success')
    fetchCourses()
  } catch (err) {
    app.showToast(err.response?.data?.detail?.message || '操作失败', 'error')
  }
}

let queryTimer
watch(query, () => { clearTimeout(queryTimer); queryTimer = setTimeout(resetPage, 250) })
onMounted(async () => {
  try {
    const res = await academicsAPI.listTerms({ page_size: 100 })
    terms.value = res.data.items || []
  } catch { terms.value = [] }
  fetchCourses()
})
</script>

<template>
  <AppLayout content-mode="list">
    <section class="course-page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">教学 / 课程</p>
          <h1>课程管理</h1>
          <p class="lead">创建与维护课程，管理章节、课时与教学安排。</p>
        </div>
        <div class="ph-actions">
          <button class="btn btn-primary btn-lg" type="button" @click="showCreate = true">
            <AppIcon name="plus" :size="15" />
            创建课程
          </button>
        </div>
      </section>

      <section class="metric-strip" aria-label="课程统计">
        <div class="metric"><span class="m-value">{{ summary.total }}</span><span class="m-label">全部课程</span></div>
        <div class="metric"><span class="m-value">{{ summary.published }}</span><span class="m-label">已发布</span></div>
        <div class="metric"><span class="m-value">{{ summary.draft }}</span><span class="m-label">草稿</span></div>
        <div class="metric"><span class="m-value">{{ summary.archived }}</span><span class="m-label">已归档</span></div>
      </section>

      <section class="table-wrap" aria-label="课程列表">
        <div class="toolbar">
          <label class="searchbox" :class="{ 'has-value': query }" style="width: 260px;">
            <AppIcon name="search" :size="15" />
            <input v-model="query" type="search" class="input" placeholder="搜索课程名称或编号" aria-label="搜索课程名称或编号" />
            <button v-if="query" type="button" class="clear" aria-label="清空搜索" @click="query = ''">
              <AppIcon name="close" :size="13" />
            </button>
          </label>
          <label class="select" style="width: 150px;">
            <DsSelect v-model="statusFilter" aria-label="状态筛选" @change="resetPage">
              <option value="all">全部状态</option>
              <option value="published">已发布</option>
              <option value="draft">草稿</option>
              <option value="archived">已归档</option>
            </DsSelect>
          </label>
          <label class="select" style="width: 160px;">
            <DsSelect v-model="termFilter" aria-label="学期筛选" @change="resetPage">
              <option value="all">全部学期</option>
              <option v-for="term in terms" :key="term.id" :value="String(term.id)">{{ term.name }}</option>
            </DsSelect>
          </label>
          <label class="select" style="width: 160px;">
            <DsSelect v-model="sortOrder" aria-label="排序" @change="resetPage">
              <option value="updated">按最近更新排序</option>
              <option value="title">按课程名称排序</option>
            </DsSelect>
          </label>
          <div class="grow"></div>
          <button v-if="query || statusFilter !== 'all' || termFilter !== 'all'" type="button" class="btn btn-ghost btn-sm" @click="query = ''; statusFilter = 'all'; termFilter = 'all'; resetPage()">清除筛选</button>
        </div>

        <div v-if="loading" class="table-scroll" aria-label="正在加载课程">
          <div v-for="i in 6" :key="i" class="course-skeleton"><span class="skeleton"></span></div>
        </div>

        <div v-else-if="loadError" class="error-panel" role="alert"><div class="grow">{{ loadError }}</div><button type="button" class="btn btn-secondary" @click="fetchCourses">重试</button></div>
        <div v-else-if="courses.length === 0" class="empty">
          <div class="empty-mark"><AppIcon name="course" :size="20" /></div>
          <h3>暂无符合条件的课程</h3>
          <p>调整筛选条件，或创建一门新课程。</p>
          <div class="empty-actions"><button type="button" class="btn btn-secondary btn-sm" @click="showCreate = true">创建课程</button></div>
        </div>

        <template v-else>
          <div class="table-scroll">
            <table class="ds-table course-table">
              <colgroup><col style="width:27%" /><col style="width:15%" /><col style="width:12%" /><col style="width:8%" /><col style="width:6%" /><col style="width:8%" /><col style="width:12%" /><col style="width:12%" /></colgroup>
              <thead>
                <tr>
                  <th>课程</th>
                  <th>学期</th>
                  <th>班级</th>
                  <th class="cell-num">章节 / 课时</th>
                  <th class="cell-num">学生</th>
                  <th>状态</th>
                  <th>最近更新</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="course in courses" :key="course.id">
                  <td>
                    <button type="button" class="cell-main course-link" @click="router.push(managePath(course))">{{ course.title }}</button>
                    <div class="cell-sub" :title="course.description">{{ course.description || '课程教学内容与安排' }}</div>
                  </td>
                  <td class="term-cell">{{ course.academic_term?.name || '未设置学期' }}</td>
                  <td><ClassSummary :classes="course.teaching_classes || []" /></td>
                  <td class="cell-num">{{ course.chapter_count }} / {{ course.lesson_count }}</td>
                  <td class="cell-num">{{ course.student_count }}</td>
                  <td><span class="badge" :class="`badge-${badgeTone(courseStatus(course))}`"><span class="dot"></span>{{ courseLabel(courseStatus(course)) }}</span></td>
                  <td class="meta">{{ formatDateTime(courseUpdated(course)) }}</td>
                  <td class="col-actions course-actions">
                    <button type="button" class="btn btn-ghost btn-sm" @click="router.push(managePath(course))">设置</button>
                    <button type="button" class="btn btn-ghost btn-sm" @click="openTemplate(course)">成绩模板</button>
                    <button v-if="course.status === 'draft'" type="button" class="btn btn-secondary btn-sm" @click="handlePublish(course)">发布</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <TeacherPagination
            :current-page="page"
            :page-count="pageCount"
            :total="total"
            :page-size="pageSize"
            aria-label="课程列表分页"
            @change="goToPage"
          />
        </template>
      </section>

      <CourseCreateModal v-if="showCreate" :terms="terms" @close="showCreate = false" @created="handleCourseCreated" />
      <CourseGradebookTemplateModal v-if="templateCourse" :course="templateCourse" @close="templateCourse = null"
                                    @saved="templateCourse = null" />
    </section>
  </AppLayout>
</template>

<style scoped>
.course-page { display: flex; flex-direction: column; gap: var(--space-5); }
.course-link {
  display: block;
  max-width: 280px;
  padding: 0;
  border: 0;
  background: transparent;
  font-weight: 600;
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.course-link:hover { color: var(--accent); background: transparent; }
.course-skeleton { padding: 14px 16px; border-bottom: 1px solid var(--border); }
.course-skeleton .skeleton { display: block; width: 55%; height: 15px; }
.course-actions { width: auto; white-space: nowrap; }

.course-table { table-layout: fixed; min-width: 0; }
.course-table td { white-space: normal; vertical-align: middle; padding-block: 10px; overflow-wrap: anywhere; }
.course-table .cell-sub { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; white-space: normal; }
.course-table .course-link { max-width: 100%; width: 100%; white-space: normal; text-align: left; }
.course-table .term-cell { font-size: var(--text-sm); }
.course-table .course-actions { white-space: normal; }
.course-table .course-actions button { padding-inline: 8px; }

.course-table th { white-space: normal; overflow-wrap: normal; line-height: 1.4; padding-block: 8px; }
</style>
