<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import TeacherPageHeader from '../../components/teacher/TeacherPageHeader.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import ExamCreateDialog from '../../components/teacher/exam/ExamCreateDialog.vue'
import { examsAPI } from '../../api/exams.js'
import { offeringsAPI } from '../../api/offerings.js'
import { coursesAPI } from '../../api/courses.js'
import { useAppStore } from '../../stores/app.js'
import { areaPathForRole } from '../../router/roleHome.js'
import { formatDateTime } from '../../utils/format.js'
import { EXAM_STATUS_MAP, statusBadge } from '../../utils/status.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
// 教师与管理员复用同一页面：链接前缀取自当前已鉴权路由
const area = (suffix) => areaPathForRole((route.path || '').startsWith('/admin') ? 'admin' : 'teacher', suffix)
// 监考视图目前只登记了教师路由（/teacher/exams/:id/proctor）
const teacherArea = computed(() => !(route.path || '').startsWith('/admin'))
const EXAM_KIND_LABELS = { quiz: '测验', midterm: '期中', final: '期末', other: '其他' }
const examKindLabel = (exam) => EXAM_KIND_LABELS[exam.exam_kind] || '测验'
const timingLabel = (exam) => (exam.timing_mode === 'per_student' ? '进入后完整时长' : '统一时间交卷')
const shuffleLabel = (exam) => {
  const parts = []
  if (exam.shuffle_questions) parts.push('题目乱序')
  if (exam.shuffle_options) parts.push('选项乱序')
  return parts.length ? parts.join(' · ') : '顺序固定'
}
const exams = ref([])
const courses = ref([])
const offerings = ref([])
async function fetchOfferings() {
  try { offerings.value = (await offeringsAPI.list()).data.items || [] }
  catch { app.showToast('授课班级加载失败，请刷新后选择班级', 'error') }
}
const loading = ref(true)
const createOpen = ref(false)
const query = ref('')
const statusFilter = ref('all')
const courseFilter = ref('all')
const sortOrder = ref('updated_desc')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)

function displayStatus(exam) {
  if (exam.status === 'draft') return 'draft'
  if (exam.end_at && exam.server_now && new Date(exam.end_at).getTime() < new Date(exam.server_now).getTime()) return 'ended'
  return 'published'
}
const summary = computed(() => ({
  total: total.value,
}))
const courseName = (exam) => exam.course_title || courses.value.find((course) => String(course.id) === String(exam.course_id))?.title || `课程 ${exam.course_id}`
const filteredExams = computed(() => exams.value)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const pagedExams = computed(() => exams.value)

async function fetchExams() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
      sort: sortOrder.value,
    }
    if (query.value.trim()) params.q = query.value.trim()
    if (statusFilter.value !== 'all') params.status = statusFilter.value
    if (courseFilter.value !== 'all') params.course_id = Number(courseFilter.value)
    const res = await examsAPI.list(params)
    exams.value = res.data.items || res.data || []
    total.value = Number(res.data.total ?? exams.value.length)
  } catch { app.showToast('加载失败', 'error') }
  finally { loading.value = false }
}
async function fetchCourses() {
  try {
    const res = await coursesAPI.list()
    courses.value = res.data.items || res.data || []
  } catch { courses.value = [] }
}
function resetPage() { page.value = 1; fetchExams() }
function changePage(nextPage) { page.value = nextPage; fetchExams() }
function openCreateModal() { createOpen.value = true }
function closeCreateModal() { createOpen.value = false }
async function handleSave(payload) {
  const { title, course_id, duration_minutes, start_at, end_at } = payload
  if (!title) return app.showToast('请输入考试名称', 'error')
  if (!Number.isInteger(course_id) || course_id <= 0) return app.showToast('请选择课程', 'error')
  if (!Number.isInteger(duration_minutes) || duration_minutes <= 0) return app.showToast('考试时长必须大于 0 分钟', 'error')
  if (start_at && end_at && new Date(start_at) >= new Date(end_at)) return app.showToast('最晚进入时间必须晚于开始时间', 'error')
  try {
    const res = await examsAPI.create(payload)
    app.showToast('创建成功', 'success')
    createOpen.value = false
    await fetchExams()
    if (res?.data?.id) router.push(area(`/exams/${res.data.id}/edit`))
  } catch (error) { app.showToast(error.response?.data?.detail?.message || '创建失败', 'error') }
}
onMounted(() => { fetchExams(); fetchCourses(); fetchOfferings() })
</script>

<template>
  <AppLayout content-mode="list">
    <section class="exam-page teacher-management-page">
      <TeacherPageHeader title="考试管理" subtitle="创建考试、配置题目、查看成绩与统计" action-label="创建考试" @action="openCreateModal" />

      <p class="exam-count" role="status">共 {{ summary.total }} 场考试符合筛选</p>

      <section class="table-wrap data-panel">
        <div class="toolbar filter-bar">
          <label class="searchbox" :class="{ 'has-value': query }" style="width: 260px;"><AppIcon name="search" :size="15" /><input v-model="query" type="search" class="input" placeholder="搜索考试名称或课程" aria-label="搜索考试名称或课程" @input="resetPage" /><button v-if="query" type="button" class="clear" aria-label="清空搜索" @click="query = ''; resetPage()"><AppIcon name="close" :size="13" /></button></label>
          <DsSelect v-model="statusFilter" aria-label="状态筛选" @change="resetPage"><option value="all">状态：全部</option><option value="published">已发布</option><option value="draft">草稿</option><option value="ended">已结束</option></DsSelect>
          <DsSelect v-model="courseFilter" aria-label="课程筛选" @change="resetPage"><option value="all">课程：全部课程</option><option v-for="course in courses" :key="course.id" :value="String(course.id)">{{ course.title }}</option></DsSelect>
          <DsSelect v-model="sortOrder" aria-label="排序" @change="resetPage"><option value="updated_desc">排序：最近更新</option><option value="title_asc">排序：考试名称</option></DsSelect>
        </div>

        <div v-if="loading" class="loading-list"><span v-for="i in 6" :key="i" class="skeleton"></span></div>
        <div v-else-if="filteredExams.length === 0" class="empty-state"><AppIcon name="exam" :size="34" /><strong>暂无符合条件的考试</strong><p>调整筛选条件，或创建一场新考试。</p></div>
        <div v-else class="table-scroll">
          <table class="exam-table">
            <thead><tr><th>考试名称</th><th>所属课程</th><th>状态</th><th>题目数</th><th>时长</th><th>参与人数</th><th>最近更新</th><th>操作</th></tr></thead>
            <tbody><tr v-for="exam in pagedExams" :key="exam.id">
              <td data-label="考试名称"><strong>{{ exam.title }}</strong><small>{{ exam.start_at ? formatDateTime(exam.start_at) + ' 开始' : '未设置考试时间' }}</small><small>{{ examKindLabel(exam) }} · {{ timingLabel(exam) }}</small></td>
              <td data-label="所属课程"><span>{{ courseName(exam) }}</span><small>课程 ID {{ exam.course_id }}</small></td>
              <td data-label="状态"><span class="status-pill" :class="displayStatus(exam)">{{ statusBadge(EXAM_STATUS_MAP, displayStatus(exam)).label }}</span></td>
              <td data-label="题目数">{{ exam.question_count ?? '—' }}</td>
              <td data-label="时长">{{ exam.duration_minutes }} 分钟<small>{{ shuffleLabel(exam) }}</small></td>
              <td data-label="参与人数">{{ exam.participant_count ?? 0 }}<span v-if="exam.expected_count"> / {{ exam.expected_count }}</span></td>
              <td data-label="最近更新" class="muted-cell">{{ formatDateTime(exam.updated_at || exam.created_at) }}</td>
              <td data-label="操作" class="row-actions">
                <button class="text-action" @click="router.push(area(`/exams/${exam.id}/edit`))">编辑试卷</button>
                <button class="text-action" @click="router.push(area(`/exams/${exam.id}/grades`))">成绩分析</button>
                <button v-if="teacherArea" class="text-action" @click="router.push(`/teacher/exams/${exam.id}/proctor`)">监考</button>
                <button v-if="displayStatus(exam) === 'draft'" class="publish-action" @click="router.push(area(`/exams/${exam.id}/edit`))">发布检查</button>
              </td>
            </tr></tbody>
          </table>
        </div>

        <TeacherPagination v-if="!loading" :current-page="page" :page-count="pageCount" :total="total" :page-size="pageSize" aria-label="考试列表分页" @change="changePage" />
      </section>
    </section>

    <ExamCreateDialog :open="createOpen" :courses="courses" :offerings="offerings" @save="handleSave" @close="closeCreateModal" />
  </AppLayout>
</template>

<style scoped>
.exam-page{display:flex;min-width:0;container-type:inline-size;flex-direction:column;gap:22px}.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.page-head h1{margin:0 0 5px;color:var(--fg);font-size:30px;line-height:1.15;letter-spacing:-.025em}.page-head p{margin:0;color:var(--muted);font-size:14px}.create-button{min-height:48px;padding:0 20px;font-size:15px}
.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}.metric-card{display:flex;align-items:center;gap:18px;min-height:106px;padding:20px;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:none}.metric-icon{display:grid;place-items:center;width:54px;height:54px;border-radius:15px}.metric-icon.blue{color:var(--accent);background:var(--accent-soft)}.metric-icon.green{color:var(--success);background:var(--success-bg)}.metric-icon.orange{color:var(--warning);background:var(--warning-bg)}.metric-icon.purple{color:var(--info);background:var(--info-bg)}.metric-card span:last-child{display:grid;gap:3px}.metric-card small{color:var(--muted);font-size:14px}.metric-card strong{color:var(--fg);font-size:26px;line-height:1}
body .teacher-management-page .filter-bar{display:grid}.data-panel{min-width:0;overflow:hidden;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:none}.filter-bar{display:grid;min-width:0;grid-template-columns:minmax(240px,1.25fr) repeat(3,minmax(150px,.7fr));gap:14px;padding:18px;border-bottom:1px solid var(--border)}.filter-bar .ds-select,.search-control{height:44px;min-width:0}.search-control{display:flex;align-items:center;gap:9px;min-width:0;padding:0 13px;border:1px solid var(--border);border-radius: var(--radius-md);color:var(--faint);background:var(--surface)}.search-control:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}.search-control input{flex:1 1 0;width:100%;height:auto;min-width:0;padding:0;border:0;box-shadow:none!important}.filter-bar .ds-select{width:100%;cursor:pointer;color:var(--muted)}
.table-scroll{min-width:0;overflow-x:hidden}.exam-table{width:100%;min-width:0;table-layout:fixed}.exam-table th{height:44px;padding:0 16px;overflow:hidden;text-transform:none;letter-spacing:0;text-overflow:ellipsis;white-space:nowrap;background:var(--surface-subtle)}.exam-table th:nth-child(1){width:22%}.exam-table th:nth-child(2){width:19%}.exam-table th:nth-child(3){width:10%}.exam-table th:nth-child(4){width:9%}.exam-table th:nth-child(5){width:11%}.exam-table th:nth-child(6){width:12%}.exam-table th:nth-child(7){width:15%}.exam-table th:nth-child(8){width:22%}.exam-table td{height:68px;min-width:0;padding:10px 16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.exam-table td:first-child{min-width:0}.exam-table td strong,.exam-table td small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.exam-table td strong{color:var(--fg);font-size:14px}.exam-table td small{margin-top:3px;color:var(--faint);font-size:12px}.muted-cell{color:var(--muted);font-size:13px}.status-pill{display:inline-flex;padding:4px 11px;border-radius: var(--radius-full);font-size:12px;font-weight:600}.status-pill.published{color:var(--success);background:var(--success-bg)}.status-pill.draft{color:var(--muted);background:var(--surface-subtle)}.status-pill.ended{color:var(--info);background:var(--info-bg)}.row-actions{display:table-cell;vertical-align:middle;white-space:nowrap}.row-actions button{display:inline-flex;align-items:center;padding:5px 7px;border:0;background:transparent;font-size:13px;white-space:nowrap}.row-actions button+button{margin-left:2px}.text-action{color:var(--accent)}.publish-action{color:var(--warning)}.row-actions button:hover{background:var(--accent-soft)}
.pagination-bar{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-top:1px solid var(--border);color:var(--muted);font-size:13px}.pagination{display:flex;gap:0}.pagination button{display:grid;place-items:center;width:62px;height:58px;padding:0;border:1px solid var(--border);color:var(--muted);background:var(--surface);font-size:18px}.pagination button:first-child{border-radius: var(--radius-lg) 0 0 12px}.pagination button:last-child{border-radius:0 12px 12px 0}.pagination button+button{border-left:0}.pagination button.active{border-color:var(--border);color:var(--fg);background:var(--surface)}.pagination button:disabled{color:var(--faint);background:var(--surface)}.pagination-bar>span:last-child{justify-self:end}.loading-list{display:grid;gap:1px;background:var(--border)}.loading-list .skeleton{height:68px;border-radius:0}.empty-state{display:grid;place-items:center;gap:8px}.empty-state strong{color:var(--fg)}.empty-state p{margin:0}
@media(max-width:1200px){.metric-grid{grid-template-columns:repeat(2,1fr)}.filter-bar{grid-template-columns:1fr 1fr}}@media(max-width:720px){.exam-page{gap:16px}.page-head{align-items:stretch;flex-direction:column}.page-head h1{font-size:26px}.metric-grid{grid-template-columns:1fr 1fr;gap:10px}.metric-card{min-height:88px;padding:14px;gap:12px}.metric-icon{width:44px;height:44px}.metric-card strong{font-size:22px}.filter-bar{grid-template-columns:1fr;padding:12px}.pagination-bar{grid-template-columns:1fr auto}.pagination{grid-column:1/-1;grid-row:1;justify-content:center}.pagination-bar>span:last-child{justify-self:end}}
@container (max-width:1050px){.filter-bar{grid-template-columns:minmax(0,1.3fr) repeat(3,minmax(0,.7fr))}.exam-table th:nth-child(1){width:27%}.exam-table th:nth-child(2){width:23%}.exam-table th:nth-child(3){width:14%}.exam-table th:nth-child(5){width:12%}.exam-table th:nth-child(8){width:24%}.exam-table th:nth-child(4),.exam-table td:nth-child(4),.exam-table th:nth-child(6),.exam-table td:nth-child(6),.exam-table th:nth-child(7),.exam-table td:nth-child(7){display:none}.exam-table td:last-child{padding-left:8px;padding-right:8px}.row-actions{gap:0}.row-actions button{padding:4px;font-size:12px}}
@container (max-width:760px){.filter-bar{grid-template-columns:1fr;padding:12px}.table-scroll{overflow:visible;padding:12px;background:var(--surface-subtle)}.exam-table,.exam-table tbody{display:block;width:100%}.exam-table thead{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.exam-table tbody{display:grid;gap:12px}.exam-table tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));overflow:hidden;border:1px solid var(--border);border-radius: var(--radius-md);background:var(--surface)}.exam-table td,.exam-table td:nth-child(4),.exam-table td:nth-child(6),.exam-table td:nth-child(7){display:flex;width:auto;height:auto;min-height:58px;padding:10px 12px;flex-direction:column;justify-content:center;gap:5px;border-bottom:1px solid var(--border);overflow:visible;white-space:normal}.exam-table td::before{content:attr(data-label);color:var(--faint);font-size:11px;font-weight:500}.exam-table td:nth-child(1),.exam-table td:nth-child(2),.exam-table td:nth-child(8){grid-column:1/-1}.exam-table td:nth-child(7),.exam-table td:nth-last-child(-n+2){border-bottom:0}.exam-table td strong,.exam-table td small{overflow:visible;white-space:normal}.exam-table .row-actions{display:flex;flex-direction:row;align-items:center;justify-content:flex-end;gap:6px}.exam-table .row-actions::before{margin-right:auto}.row-actions button{padding:6px 8px}.exam-table .row-actions button+button{margin-left:0}}

.exam-count { margin: 0; color: var(--muted); font-size: var(--text-sm); }
</style>
