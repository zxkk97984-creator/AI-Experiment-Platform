<script setup>
// 学生作业列表：只展示作业，不与考试、实验混合为统一任务表格。
// 作业与课程来自既有 API；课程加载失败时仍保留作业列表。
// 状态、筛选、排序走 studentUi 纯函数，零伪造日期。

import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import StudentPagination from '../../components/student/StudentPagination.vue'
import { assignmentsAPI } from '../../api/assignments.js'
import { coursesAPI } from '../../api/courses.js'
import { normalizeAssignmentTask, taskStatus } from '../../utils/studentUi.js'

const router = useRouter()

const DAY = 86400000
const tasks = ref([])
const courseMap = ref({})
const loading = ref(true)
const error = ref(false)
const page = ref(1)
const pageSize = 10

const filters = reactive({ status: 'all', courseId: '', query: '', time: 'all', sort: 'due' })

const timeFmt = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const now = () => new Date()

async function loadAssignments() {
  loading.value = true
  error.value = false
  const [aRes, cRes] = await Promise.allSettled([
    assignmentsAPI.list(),
    coursesAPI.list(),
  ])
  const courses = cRes.status === 'fulfilled' ? (cRes.value.data?.items || cRes.value.data || []) : []
  courseMap.value = Object.fromEntries(courses.map((c) => [c.id, c]))

  const flat = []
  const t = now()
  if (aRes.status === 'fulfilled') {
    for (const item of aRes.value.data?.items || aRes.value.data || []) {
      flat.push(normalizeAssignmentTask(item, courseMap.value, t))
    }
  }
  // 无法生成合法路由的作业直接丢弃，不渲染死链接
  tasks.value = flat.filter((task) => task.route)
  error.value = aRes.status !== 'fulfilled'
  loading.value = false
}

const statusCounts = computed(() => {
  const counts = { all: tasks.value.length, pending: 0, overdue: 0, submitted: 0 }
  const t = now()
  for (const task of tasks.value) {
    const s = taskStatus(task, t)
    if (s === 'overdue') counts.overdue++
    else if (s === 'submitted') counts.submitted++
    else counts.pending++
  }
  return counts
})

/** 未提交作业中最早的未来截止（真实最近截止） */
const nearestDeadline = computed(() => {
  const t = now()
  const ts = tasks.value
    .filter((task) => taskStatus(task, t) !== 'submitted' && task.dueAt != null)
    .map((task) => task.dueAt)
    .sort((a, b) => a - b)
  return ts[0] ?? null
})

const filteredTasks = computed(() => {
  let list = tasks.value
  const t = now()
  if (filters.status !== 'all') list = list.filter((task) => taskStatus(task, t) === filters.status)
  if (filters.courseId) list = list.filter((task) => String(task.courseId) === filters.courseId)
  const query = filters.query.trim().toLowerCase()
  if (query) list = list.filter((task) => task.title.toLowerCase().includes(query))
  if (filters.time === 'today') {
    const start = new Date()
    start.setHours(0, 0, 0, 0)
    list = list.filter((task) => task.dueAt != null && task.dueAt >= start.getTime() && task.dueAt < start.getTime() + DAY)
  } else if (filters.time === 'week') {
    const start = new Date()
    start.setHours(0, 0, 0, 0)
    list = list.filter((task) => task.dueAt != null && task.dueAt < start.getTime() + 7 * DAY)
  }
  if (filters.sort === 'due') {
    list = [...list].sort((a, b) => (a.dueAt ?? Infinity) - (b.dueAt ?? Infinity))
  } else if (filters.sort === 'course') {
    list = [...list].sort((a, b) => (a.courseTitle || '').localeCompare(b.courseTitle || ''))
  } else if (filters.sort === 'title') {
    list = [...list].sort((a, b) => a.title.localeCompare(b.title))
  }
  return list
})

const pageCount = computed(() => Math.max(1, Math.ceil(filteredTasks.value.length / pageSize)))
const paginatedTasks = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredTasks.value.slice(start, start + pageSize)
})
const courseOptions = computed(() => {
  const seen = new Map()
  for (const task of tasks.value) {
    if (task.courseId != null) seen.set(task.courseId, task.courseTitle)
  }
  return [...seen.entries()].map(([id, title]) => ({ id: String(id), title }))
})

function statusText(task) {
  const s = taskStatus(task, now())
  if (s === 'submitted') return '已提交'
  if (s === 'overdue') return '已逾期'
  return '待办'
}

function statusTone(task) {
  const s = taskStatus(task, now())
  if (s === 'submitted') return 'success'
  if (s === 'overdue') return 'danger'
  return 'pending'
}

function resetFilters() {
  filters.status = 'all'
  filters.courseId = ''
  filters.query = ''
  filters.time = 'all'
  filters.sort = 'due'
}

function goToPage(value) { page.value = Math.min(Math.max(value, 1), pageCount.value) }

function go(route) {
  if (route === '/student' || route.startsWith('/student/')) {
    router.push(route)
  }
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : timeFmt.format(date)
}

watch(filters, () => {
  page.value = 1
})

watch(pageCount, (count) => {
  if (page.value > count) page.value = count
})

onMounted(loadAssignments)
</script>

<template>
  <AppLayout content-mode="list">
    <div class="page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">学习 / 作业</p>
          <h1>作业</h1>
          <p class="lead">
            <span class="assignment-count">{{ statusCounts.all }}</span> 项作业
            <template v-if="nearestDeadline"> · 最近截止 <span class="nearest-deadline">{{ formatTime(nearestDeadline) }}</span></template>
          </p>
        </div>
      </section>

      <div class="tabs" role="tablist" aria-label="作业状态">
        <button
          v-for="tab in [
            { key: 'all', label: '全部作业' },
            { key: 'pending', label: '待办' },
            { key: 'overdue', label: '逾期' },
            { key: 'submitted', label: '已完成' },
          ]"
          :key="tab.key"
          type="button"
          class="tab status-tab"
          :class="{ active: filters.status === tab.key }"
          role="tab"
          :aria-selected="filters.status === tab.key"
          @click="filters.status = tab.key"
        >
          {{ tab.label }}
          <span class="count">{{ statusCounts[tab.key] }}</span>
        </button>
      </div>

      <section class="table-wrap" aria-label="作业列表">
        <div class="toolbar">
          <label class="searchbox assignment-search" :class="{ 'has-value': filters.query }">
            <AppIcon name="search" :size="15" />
            <input
              v-model="filters.query"
              type="search"
              class="input assignment-search-input"
              placeholder="搜索作业名称"
              aria-label="搜索作业名称"
            />
            <button v-if="filters.query" type="button" class="clear" aria-label="清空作业搜索" @click="filters.query = ''">
              <AppIcon name="close" :size="13" />
            </button>
          </label>
          <label class="select" style="width: 150px;">
            <DsSelect v-model="filters.courseId" class="filter-course" aria-label="按课程筛选">
              <option value="">全部课程</option>
              <option v-for="c in courseOptions" :key="c.id" :value="c.id">{{ c.title }}</option>
            </DsSelect>
          </label>
          <label class="select" style="width: 140px;">
            <DsSelect v-model="filters.time" class="filter-time" aria-label="按时间筛选">
              <option value="all">全部时间</option>
              <option value="today">今天截止</option>
              <option value="week">未来 7 天</option>
            </DsSelect>
          </label>
          <label class="select" style="width: 140px;">
            <DsSelect v-model="filters.sort" class="filter-sort" aria-label="排序方式">
              <option value="due">按截止时间</option>
              <option value="course">按课程</option>
              <option value="title">按标题</option>
            </DsSelect>
          </label>
          <div class="grow"></div>
          <button type="button" class="btn btn-ghost btn-sm reset-btn" @click="resetFilters">重置</button>
        </div>

        <div class="task-catalog-heading">
          <h2>全部作业</h2>
          <span>共 {{ filteredTasks.length }} 个</span>
        </div>

        <DashboardAsyncState
          :loading="loading"
          :error="error"
          :empty="filteredTasks.length === 0"
          empty-title="暂无作业"
          empty-body="没有符合条件的作业"
          @retry="loadAssignments"
        >
          <div class="table-scroll">
            <table class="ds-table task-table">
              <thead>
                <tr>
                  <th>作业名称</th>
                  <th>所属课程</th>
                  <th>状态</th>
                  <th>截止时间</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="task in paginatedTasks" :key="task.kind + '-' + task.id">
                  <td data-label="作业名称"><span class="cell-main task-name" :title="task.title">{{ task.title }}</span></td>
                  <td data-label="所属课程" class="cell-ellipsis">{{ task.courseTitle || '—' }}</td>
                  <td data-label="状态"><UiStatusPill :tone="statusTone(task)" :label="statusText(task)" /></td>
                  <td data-label="截止时间" class="meta">{{ task.dueAt ? formatTime(task.dueAt) : '—' }}</td>
                  <td data-label="操作" class="col-actions"><button type="button" class="btn btn-ghost btn-sm task-action" @click="go(task.route)">进入</button></td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="task-pagination">
            <StudentPagination :current-page="page" :page-count="pageCount" :total="filteredTasks.length" :page-size="pageSize" aria-label="作业分页" @change="goToPage" />
          </div>
        </DashboardAsyncState>
      </section>
    </div>
  </AppLayout>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.task-catalog-heading { display: flex; align-items: baseline; gap: 10px; margin: 12px 0 4px; }
.task-catalog-heading h2 { margin: 0; font-size: var(--text-lg); font-weight: 600; color: var(--fg); }
.task-catalog-heading span { color: var(--muted); font-size: var(--text-base); }
.assignment-search { flex: 0 1 280px; max-width: 100%; }
.task-name { max-width: 320px; }
.task-action { color: var(--accent); }
.task-action:hover { color: var(--accent); background: var(--accent-soft); }
.task-pagination { border-top: 1px solid var(--border); }

@media (max-width: 560px) {
  .assignment-search { flex-basis: 100%; }
}
</style>
