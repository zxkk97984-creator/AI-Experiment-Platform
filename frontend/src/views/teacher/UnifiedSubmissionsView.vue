<script setup>
// 统一提交中心（V2）：实验 / 作业 / 考试提交合并列表。
// 数据来自 GET /submissions/unified，筛选、排序、分页全部服务端完成。

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { submissionsAPI } from '../../api/submissions.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const items = ref([])
const loading = ref(true)
const loadError = ref(false)
const page = ref(1)
const pageSize = 10
const total = ref(0)
const summary = reactive({ total: 0, pending: 0, graded: 0, review: 0, failed: 0 })
const filterOptions = reactive({ courses: [], entries: [] })
const filters = reactive({
  q: typeof route.query.q === 'string' ? route.query.q : '',
  courseId: typeof route.query.course_id === 'string' ? route.query.course_id : '',
  kind: typeof route.query.kind === 'string' ? route.query.kind : 'all',
  status: typeof route.query.status === 'string' ? route.query.status : typeof route.query.review_status === 'string' ? 'pending_grading' : 'all',
  entryId: typeof route.query.entry_id === 'string' ? route.query.entry_id : '',
  sort: 'submitted_desc',
})

const statusMap = {
  pending_grading: { label: '待评分', tone: 'badge-warning' },
  graded: { label: '已评分', tone: 'badge-success' },
  review_required: { label: '待复核', tone: 'badge-info' },
  failed: { label: '失败', tone: 'badge-danger' },
}
const kindMap = { experiment: '实验', assignment: '作业', exam: '考试' }

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const hasFilters = computed(() => Boolean(
  filters.q.trim() || filters.courseId || filters.entryId || filters.kind !== 'all' || filters.status !== 'all',
))

let searchTimer = null
const requestGuard = createLatestRequestGuard()

function statusBadge(status, tone) {
  const entry = statusMap[status] || { label: status || '—', tone: 'badge-neutral' }
  return { label: entry.label, tone: tone || entry.tone }
}

async function load() {
  const token = requestGuard.begin()
  loading.value = true
  loadError.value = false
  try {
    const params = {
      page: page.value,
      page_size: pageSize,
      kind: filters.kind,
      status: filters.status,
      sort: filters.sort,
    }
    if (filters.q.trim()) params.q = filters.q.trim()
    if (filters.courseId) params.course_id = Number(filters.courseId)
    if (filters.entryId) params.entry_id = Number(filters.entryId)

    const res = await submissionsAPI.unified(params)
    if (!requestGuard.isLatest(token)) return
    items.value = res.data?.items || []
    total.value = res.data?.total || 0
    Object.assign(summary, res.data?.summary || {})
    filterOptions.courses = res.data?.filter_options?.courses || []
    filterOptions.entries = res.data?.filter_options?.entries || []
  } catch {
    if (!requestGuard.isLatest(token)) return
    loadError.value = true
    app.showToast('加载统一提交列表失败', 'error')
  } finally {
    if (requestGuard.isLatest(token)) loading.value = false
  }
}

function reloadFromFirstPage() {
  page.value = 1
  load()
}

function scheduleSearch() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(reloadFromFirstPage, 300)
}

function goPage(nextPage) {
  if (nextPage < 1 || nextPage > totalPages.value || nextPage === page.value) return
  page.value = nextPage
  load()
}

function openRow(row) {
  if (row.route) router.push(row.route)
}

function clearFilters() {
  filters.q = ''
  filters.courseId = ''
  filters.entryId = ''
  filters.kind = 'all'
  filters.status = 'all'
  window.clearTimeout(searchTimer)
  reloadFromFirstPage()
}

watch(() => filters.q, scheduleSearch)
watch(() => [filters.courseId, filters.entryId, filters.kind, filters.status, filters.sort], reloadFromFirstPage)
onMounted(load)
onBeforeUnmount(() => { window.clearTimeout(searchTimer); requestGuard.invalidate() })
</script>

<template>
  <AppLayout content-mode="list">
    <div class="submissions-page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">评分 / 统一提交</p>
          <h1>提交与评分</h1>
          <p class="lead">统一查看实验、作业与考试提交，快速进入评分或复核。</p>
        </div>
      </section>

      <section class="metric-strip submissions-strip" aria-label="提交统计">
        <div class="metric"><span class="m-value">{{ summary.total }}</span><span class="m-label">全部提交</span></div>
        <div class="metric em"><span class="m-value">{{ summary.pending }}</span><span class="m-label">待评分</span></div>
        <div class="metric"><span class="m-value">{{ summary.graded }}</span><span class="m-label">已评分</span></div>
        <div class="metric"><span class="m-value">{{ summary.review }}</span><span class="m-label">待复核</span></div>
        <div class="metric warn"><span class="m-value">{{ summary.failed }}</span><span class="m-label">失败</span></div>
      </section>

      <section class="table-wrap" aria-label="统一提交记录">
        <div class="toolbar">
          <label class="searchbox" style="width: 280px;">
            <AppIcon name="search" :size="15" />
            <input v-model="filters.q" type="search" class="input" placeholder="搜索学生、学号或任务名称" />
            <button v-if="filters.q" type="button" class="clear" aria-label="清空搜索" @click="filters.q = ''">
              <AppIcon name="close" :size="13" />
            </button>
          </label>

          <label class="select" style="width: 150px;">
            <DsSelect v-model="filters.courseId" aria-label="按课程筛选">
              <option value="">全部课程</option>
              <option v-for="course in filterOptions.courses" :key="'c' + course.id" :value="String(course.id)">{{ course.name }}</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 160px;">
            <DsSelect v-model="filters.kind" aria-label="按提交类型筛选">
              <option value="all">全部类型</option>
              <option value="experiment">实验</option>
              <option value="assignment">作业</option>
              <option value="exam">考试</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 190px;">
            <DsSelect v-model="filters.entryId" aria-label="按任务筛选">
              <option value="">全部实验 / 作业 / 考试</option>
              <option v-for="entry in filterOptions.entries" :key="entry.kind + '-' + entry.id" :value="String(entry.id)">{{ entry.name }}</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 140px;">
            <DsSelect v-model="filters.status" aria-label="按状态筛选">
              <option value="all">全部状态</option>
              <option value="pending_grading">待评分</option>
              <option value="graded">已评分</option>
              <option value="review_required">待复核</option>
              <option value="failed">失败</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 170px;">
            <DsSelect v-model="filters.sort" aria-label="排序">
              <option value="submitted_desc">提交时间（最新）</option>
              <option value="submitted_asc">提交时间（最早）</option>
            </DsSelect>
          </label>

          <div class="grow"></div>
          <button v-if="hasFilters" type="button" class="btn btn-ghost btn-sm" @click="clearFilters">清除筛选</button>
        </div>

        <div v-if="loading" class="table-scroll" aria-label="正在加载提交记录">
          <div v-for="row in 6" :key="row" class="skeleton-row">
            <span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
          </div>
        </div>

        <div v-else-if="loadError" class="empty">
          <div class="empty-mark"><AppIcon name="warning" :size="20" /></div>
          <h3>提交记录暂时无法加载</h3>
          <p>请稍后重试。</p>
          <div class="empty-actions"><button type="button" class="btn btn-secondary btn-sm" @click="load">重新加载</button></div>
        </div>

        <div v-else-if="items.length === 0" class="empty">
          <div class="empty-mark"><AppIcon name="assignment" :size="20" /></div>
          <h3>{{ hasFilters ? '没有符合条件的提交' : '暂无提交记录' }}</h3>
          <p>{{ hasFilters ? '调整筛选条件后再试试。' : '学生提交后，记录会显示在这里。' }}</p>
          <div v-if="hasFilters" class="empty-actions"><button type="button" class="btn btn-secondary btn-sm" @click="clearFilters">清除筛选</button></div>
        </div>

        <template v-else>
          <div class="table-scroll">
            <table class="ds-table unified-table">
              <thead>
                <tr>
                  <th>学生</th>
                  <th>类型</th>
                  <th>实验 / 作业</th>
                  <th>课程</th>
                  <th>状态</th>
                  <th class="cell-num">测试</th>
                  <th class="cell-num">AI 得分</th>
                  <th>提交时间</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in items" :key="row.kind + '-' + row.id" @click="openRow(row)">
                  <td>
                    <span class="cell-main">{{ row.student_name || '未命名学生' }}</span>
                    <div class="cell-sub">{{ row.student_no || '—' }}</div>
                  </td>
                  <td><span class="badge badge-neutral"><span class="dot"></span>{{ kindMap[row.kind] || row.kind }}</span></td>
                  <td class="cell-ellipsis">{{ row.entry_title || '未命名任务' }}</td>
                  <td>{{ row.course_title || '—' }}</td>
                  <td>
                    <span class="badge" :class="statusBadge(row.status, row.status_tone).tone">
                      <span class="dot"></span>{{ statusBadge(row.status, row.status_tone).label }}
                    </span>
                  </td>
                  <td class="cell-num">{{ row.tests_total != null ? `${row.tests_passed ?? 0} / ${row.tests_total}` : '—' }}</td>
                  <td class="cell-num score-cell">{{ row.ai_score != null ? Number(row.ai_score).toFixed(1) : row.score != null ? Number(row.score).toFixed(1) : '—' }}</td>
                  <td class="meta">{{ formatDateTime(row.submitted_at) }}</td>
                  <td class="col-actions">
                    <button type="button" class="btn row-action" :class="row.status === 'pending_grading' ? 'btn-primary btn-sm' : 'btn-ghost btn-sm'" @click.stop="openRow(row)">
                      {{ row.status === 'pending_grading' ? '去评分' : '查看' }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <TeacherPagination :current-page="page" :page-count="totalPages" :total="total" :page-size="pageSize" aria-label="提交记录分页" total-suffix="条记录" @change="goPage" />
        </template>
      </section>
    </div>
  </AppLayout>
</template>

<style scoped>
.submissions-page { display: flex; flex-direction: column; gap: var(--space-5); }
.submissions-strip { grid-template-columns: repeat(5, 1fr); }
.skeleton-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 24px; padding: 14px 16px; border-bottom: 1px solid var(--border); }
.skeleton-row .skeleton { height: 15px; }
.score-cell { font-weight: 600; }
.row-action { white-space: nowrap; }
.unified-table tbody tr { cursor: pointer; }
.unified-table tbody tr:hover { background: var(--surface-sunken); }

@media (max-width: 1024px) { .submissions-strip { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 640px) { .submissions-strip { grid-template-columns: 1fr 1fr; } }
</style>
