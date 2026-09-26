<script setup>
// 提交与评分（V2）：真实实验提交列表。
// 搜索 / 课程 / 实验 / 评分状态 / 排序 / 分页与真实状态保留，仅重构为 V2 视觉。

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { experimentsAPI } from '../../api/experiments.js'
import { useAppStore } from '../../stores/app.js'
import { useAuthStore } from '../../stores/auth.js'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { formatDateTime } from '../../utils/format.js'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const auth = useAuthStore()

const submissions = ref([])
const loading = ref(true)
const refreshing = ref(false)
const loadError = ref(false)
const page = ref(1)
const pageSize = 10
const total = ref(0)
const summary = reactive({ total: 0, pending: 0, graded: 0 })
const filterOptions = reactive({ courses: [], entries: [] })
const filters = reactive({
  q: typeof route.query.q === 'string' ? route.query.q : '',
  courseId: '',
  entryId: '',
  reviewStatus: '',
  sort: 'submitted_desc',
})

let searchTimer = null
const requestGuard = createLatestRequestGuard()

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const hasFilters = computed(() => Boolean(
  filters.q || filters.courseId || filters.entryId || filters.reviewStatus,
))

async function load({ initial = false } = {}) {
  const token = requestGuard.begin()
  if (initial) loading.value = true
  else refreshing.value = true
  loadError.value = false
  try {
    const params = {
      page: page.value,
      page_size: pageSize,
      sort: filters.sort,
    }
    if (route.query.record_id) params.record_id = route.query.record_id
    if (filters.q.trim()) params.q = filters.q.trim()
    if (filters.courseId) params.course_id = Number(filters.courseId)
    if (filters.entryId) params.entry_id = Number(filters.entryId)
    if (filters.reviewStatus) params.review_status = filters.reviewStatus

    const res = await experimentsAPI.listSubmissions(params)
    if (!requestGuard.isLatest(token)) return
    submissions.value = res.data?.items || []
    total.value = res.data?.total || 0
    Object.assign(summary, res.data?.summary || { total: 0, pending: 0, graded: 0 })
    filterOptions.courses = res.data?.filter_options?.courses || []
    filterOptions.entries = res.data?.filter_options?.entries || []
  } catch {
    if (!requestGuard.isLatest(token)) return
    loadError.value = true
    app.showToast('加载提交列表失败', 'error')
  } finally {
    if (requestGuard.isLatest(token)) {
      loading.value = false
      refreshing.value = false
    }
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

function viewDetail(subId) {
  const prefix = auth.isAdmin ? '/admin' : '/teacher'
  router.push(`${prefix}/submissions/${subId}`)
}

function goPage(nextPage) {
  if (nextPage < 1 || nextPage > totalPages.value || nextPage === page.value) return
  page.value = nextPage
  load()
}

function clearFilters() {
  filters.q = ''
  filters.courseId = ''
  filters.entryId = ''
  filters.reviewStatus = ''
  filters.sort = 'submitted_desc'
  window.clearTimeout(searchTimer)
  page.value = 1
  load()
}

watch(() => filters.q, scheduleSearch)
watch(
  () => [filters.courseId, filters.entryId, filters.reviewStatus, filters.sort],
  reloadFromFirstPage,
)

onMounted(() => load({ initial: true }))
onBeforeUnmount(() => { window.clearTimeout(searchTimer); requestGuard.invalidate() })
</script>

<template>
  <AppLayout content-mode="list">
    <div class="submissions-page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">评分 / 实验提交</p>
          <h1>提交与评分</h1>
          <p class="lead">查看学生实验提交情况，完成评分与反馈。</p>
        </div>
      </section>

      <section class="metric-strip submissions-strip" aria-label="提交统计">
        <div class="metric"><span class="m-value">{{ summary.total }}</span><span class="m-label">全部提交</span></div>
        <div class="metric em"><span class="m-value">{{ summary.pending }}</span><span class="m-label">待评分</span></div>
        <div class="metric"><span class="m-value">{{ summary.graded }}</span><span class="m-label">已评分</span></div>
      </section>

      <section class="table-wrap" :class="{ refreshing }" aria-label="提交记录">
        <div class="toolbar">
          <label class="searchbox" style="width: 280px;">
            <AppIcon name="search" :size="15" />
            <input v-model="filters.q" type="search" class="input" placeholder="搜索学生姓名、账号或实验名称" />
            <button v-if="filters.q" type="button" class="clear" aria-label="清空搜索" @click="filters.q = ''">
              <AppIcon name="close" :size="13" />
            </button>
          </label>

          <label class="select" style="width: 150px;">
            <DsSelect v-model="filters.courseId" aria-label="按课程筛选">
              <option value="">全部课程</option>
              <option v-for="course in filterOptions.courses" :key="course.id" :value="String(course.id)">{{ course.name }}</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 170px;">
            <DsSelect v-model="filters.entryId" aria-label="按实验筛选">
              <option value="">全部实验</option>
              <option v-for="entry in filterOptions.entries" :key="entry.id" :value="String(entry.id)">{{ entry.name }}</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 140px;">
            <DsSelect v-model="filters.reviewStatus" aria-label="按评分状态筛选">
              <option value="">全部状态</option>
              <option value="pending">待评分</option>
              <option value="graded">已评分</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 160px;">
            <DsSelect v-model="filters.sort" aria-label="提交记录排序">
              <option value="submitted_desc">提交时间（最新）</option>
              <option value="submitted_asc">提交时间（最早）</option>
            </DsSelect>
          </label>

          <div class="grow"></div>
          <button v-if="hasFilters" type="button" class="btn btn-ghost btn-sm" @click="clearFilters">清除筛选</button>
        </div>

        <div v-if="loading" class="table-scroll" aria-label="正在加载提交记录">
          <div v-for="row in 6" :key="row" class="skeleton-row">
            <span class="skeleton skel-wide"></span><span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
          </div>
        </div>

        <div v-else-if="loadError" class="empty">
          <div class="empty-mark"><AppIcon name="warning" :size="20" /></div>
          <h3>提交记录暂时无法加载</h3>
          <p>请检查网络连接后重试。</p>
          <div class="empty-actions"><button type="button" class="btn btn-secondary btn-sm" @click="load({ initial: true })">重新加载</button></div>
        </div>

        <div v-else-if="submissions.length === 0" class="empty">
          <div class="empty-mark"><AppIcon name="assignment" :size="20" /></div>
          <h3>{{ hasFilters ? '没有符合条件的提交' : '暂无提交记录' }}</h3>
          <p>{{ hasFilters ? '调整筛选条件后再试试。' : '学生完成实验提交后，记录会显示在这里。' }}</p>
          <div v-if="hasFilters" class="empty-actions"><button type="button" class="btn btn-secondary btn-sm" @click="clearFilters">清除筛选</button></div>
        </div>

        <template v-else>
          <div class="table-scroll">
            <table class="ds-table">
              <thead>
                <tr>
                  <th>学生</th>
                  <th>课程</th>
                  <th>实验 / 作业</th>
                  <th>提交情况</th>
                  <th>提交时间</th>
                  <th>状态</th>
                  <th class="cell-num">得分</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="submission in submissions" :key="submission.id">
                  <td>
                    <span class="cell-main">{{ submission.student_name || '未命名学生' }}</span>
                    <div class="cell-sub">{{ submission.student_username || '—' }}</div>
                  </td>
                  <td>{{ submission.course_name || '独立实验' }}</td>
                  <td class="cell-ellipsis">{{ submission.entry_name || '未命名实验' }}</td>
                  <td><span class="badge badge-neutral"><span class="dot"></span>第 {{ submission.attempt_number }} 次提交</span></td>
                  <td class="meta">{{ formatDateTime(submission.submitted_at) }}</td>
                  <td>
                    <span class="badge status-pill" :class="submission.score == null ? 'badge-warning pending' : 'badge-success graded'">
                      <span class="dot"></span>{{ submission.score == null ? '待评分' : '已评分' }}
                    </span>
                  </td>
                  <td class="cell-num">
                    <strong v-if="submission.score != null" class="score-value">{{ submission.score }}</strong>
                    <span v-else>—</span>
                  </td>
                  <td class="col-actions">
                    <button
                      type="button"
                      class="btn row-action"
                      :class="submission.score == null ? 'btn-primary btn-sm' : 'btn-ghost btn-sm'"
                      @click="viewDetail(submission.id)"
                    >
                      {{ submission.score == null ? '去评分' : '查看详情' }}
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
.submissions-strip { grid-template-columns: repeat(3, 1fr); }
.table-wrap.refreshing { opacity: .55; pointer-events: none; }
.skeleton-row { display: grid; grid-template-columns: 1.4fr 1fr 1.4fr 1fr 1fr; gap: 24px; padding: 14px 16px; border-bottom: 1px solid var(--border); }
.skeleton-row .skeleton { height: 15px; }
.skel-wide { width: 85%; }
.score-value { font-weight: 600; }
.row-action { white-space: nowrap; }

@media (max-width: 820px) {
  .submissions-strip { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 560px) {
  .submissions-strip { grid-template-columns: 1fr; }
}
</style>
