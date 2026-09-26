<script setup>
// 提交与反馈（参考图 01）：以 dashboardAPI.student().recent_feedback 为真实来源
// 的“最近提交与反馈”列表——不做历史档案式声称。
// 状态标签、组合筛选、分页全部由计算属性完成，零伪造数据。

import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { dashboardAPI } from '../../api/dashboard.js'
import { feedbackStatus, filterFeedback } from '../../utils/studentUi.js'

const router = useRouter()

const DAY = 86400000
const items = ref([])
const loading = ref(true)
const error = ref(false)

const filters = reactive({ status: 'all', courseId: '', query: '', time: 'all' })
const page = ref(1)
const pageSize = ref(10)

const timeFmt = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const statusLabel = { pending: '等待评分', needs_revision: '需修改', passed: '已通过' }
const statusIcon = { pending: 'clock', needs_revision: 'close', passed: 'check' }
const statusTone = { pending: 'warning', needs_revision: 'danger', passed: 'success' }
const kindLabel = { assignment: '作业', exam: '考试', experiment: '实验' }

async function loadFeedback() {
  loading.value = true
  error.value = false
  try {
    const { data } = await dashboardAPI.student()
    items.value = data.recent_feedback || []
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}

const courseOptions = computed(() => {
  const seen = new Map()
  for (const item of items.value) {
    if (item.course_id != null) seen.set(item.course_id, item.course_title)
    else if (item.course_title) seen.set(item.course_title, item.course_title)
  }
  return [...seen.entries()].map(([id, title]) => ({ id: String(id), title }))
})

const statusCounts = computed(() => {
  const counts = { all: items.value.length, pending: 0, needs_revision: 0, passed: 0 }
  for (const item of items.value) {
    counts[feedbackStatus(item)]++
  }
  return counts
})

const filteredItems = computed(() => {
  let list = filterFeedback(items.value, {
    status: filters.status,
    courseId: filters.courseId,
    query: filters.query,
  })
  if (filters.time === 'week') {
    const cutoff = Date.now() - 7 * DAY
    list = list.filter((item) => {
      const t = item.graded_at ? new Date(item.graded_at).getTime() : null
      return t != null && !Number.isNaN(t) && t >= cutoff
    })
  }
  return list
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredItems.value.length / pageSize.value)))

const pageItems = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredItems.value.slice(start, start + pageSize.value)
})

function selectStatus(status) {
  filters.status = status
  page.value = 1
}

function onPageSizeChange() {
  page.value = 1
}

function go(route) {
  // 仅允许服务端返回的学生相对路由
  if (route === '/student' || route.startsWith('/student/')) {
    router.push(route)
  }
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : timeFmt.format(date)
}

onMounted(loadFeedback)
</script>

<template>
  <AppLayout content-mode="list">
    <div class="page">
      <!-- 页头 -->
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">学习 / 反馈</p>
          <h1 class="page-title">提交与反馈</h1>
          <p class="lead page-sub">最近提交的作业、考试与实验反馈</p>
        </div>
      </section>

      <!-- 状态标签（白色分段卡） -->
      <div class="tabs status-tabs" role="tablist" aria-label="反馈状态">
        <button
          v-for="tab in [
            { key: 'all', label: '全部' },
            { key: 'needs_revision', label: '需修改' },
            { key: 'passed', label: '已通过' },
            { key: 'pending', label: '等待评分' },
          ]"
          :key="tab.key"
          type="button"
          class="tab status-tab"
          :class="{ active: filters.status === tab.key }"
          role="tab"
          :aria-selected="filters.status === tab.key"
          @click="selectStatus(tab.key)"
        >
          {{ tab.label }}
          <span class="count tab-count">{{ statusCounts[tab.key] }}</span>
        </button>
      </div>

      <!-- 筛选行 -->
      <div class="toolbar filter-row">
        <label class="filter-field">
          <span class="filter-label">课程</span>
          <DsSelect v-model="filters.courseId" class="filter-course" aria-label="按课程筛选" @change="page = 1">
            <option value="">全部课程</option>
            <option v-for="c in courseOptions" :key="c.id" :value="c.id">{{ c.title }}</option>
          </DsSelect>
        </label>
        <label class="filter-field">
          <span class="filter-label">时间</span>
          <DsSelect v-model="filters.time" class="filter-time" aria-label="按时间筛选" @change="page = 1">
            <option value="all">全部时间</option>
            <option value="week">近 7 天</option>
          </DsSelect>
        </label>
        <label class="searchbox search-box" style="width: 280px;">
          <AppIcon name="search" :size="15" />
          <input v-model="filters.query" type="search" class="input search-input" placeholder="搜索标题或课程" aria-label="搜索反馈" />
        </label>
      </div>

      <!-- 计数行 -->
      <p class="result-count" v-if="!loading && !error">共 {{ filteredItems.length }} 条反馈</p>

      <!-- 列表 -->
      <DashboardAsyncState
        :loading="loading"
        :error="error"
        :empty="filteredItems.length === 0"
        empty-title="暂无反馈"
        empty-body="获得批改后，反馈会出现在这里"
        @retry="loadFeedback"
      >
        <div class="feedback-list">
          <article
            v-for="item in pageItems"
            :key="item.kind + '-' + item.id"
            class="feedback-row"
          >
            <span class="feedback-status-icon" :class="'status-' + statusTone[feedbackStatus(item)]" aria-hidden="true">
              <AppIcon :name="statusIcon[feedbackStatus(item)]" :size="18" />
            </span>
            <div class="feedback-main">
              <div class="feedback-head">
                <span class="feedback-title">{{ item.title }}</span>
                <span class="feedback-score" :class="'status-' + statusTone[feedbackStatus(item)]">
                  {{ item.score ?? '—' }}
                </span>
              </div>
              <span class="feedback-course">{{ item.course_title || '—' }} · {{ kindLabel[item.kind] || item.kind }}</span>
              <p class="feedback-summary">{{ item.feedback || '暂无文字反馈' }}</p>
              <span class="feedback-time">{{ formatTime(item.graded_at) }}</span>
            </div>
            <div class="feedback-right">
              <span class="feedback-pill" :class="'status-' + statusTone[feedbackStatus(item)]">
                {{ statusLabel[feedbackStatus(item)] }}
              </span>
              <button type="button" class="detail-link" @click="go(item.route)">
                查看详情
                <AppIcon name="arrow-right" :size="14" />
              </button>
            </div>
          </article>
        </div>

        <!-- 分页页脚：一页时也保持可见与连贯 -->
        <footer class="page-footer" v-if="!loading && !error">
          <span class="page-meta">
            第 <strong>{{ page }}</strong> / {{ totalPages }} 页
          </span>
          <label class="page-size-field">
            每页
            <DsSelect v-model="pageSize" class="page-size" aria-label="每页条数" @change="onPageSizeChange">
              <option :value="10">10</option>
              <option :value="20">20</option>
              <option :value="50">50</option>
            </DsSelect>
            条
          </label>
          <div class="page-nav">
            <button type="button" class="prev-page" :disabled="page <= 1" @click="page--">上一页</button>
            <button type="button" class="next-page" :disabled="page >= totalPages" @click="page++">下一页</button>
          </div>
        </footer>
      </DashboardAsyncState>
    </div>
  </AppLayout>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--space-5); }
.page-title { margin: 0; font-family: var(--font-display); font-size: var(--text-3xl); font-weight: 600; letter-spacing: -0.01em; line-height: var(--lh-tight); color: var(--fg); }
.page-sub { margin-top: 6px; }
.status-tabs { height: 42px; background: transparent; border: 0; border-radius: 0; box-shadow: none; overflow-x: auto; }
.filter-row { padding: 12px 16px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.filter-field { display: flex; flex-direction: column; gap: 5px; color: var(--muted); font-size: var(--text-sm); font-weight: 500; }
.filter-field .ds-select { height: var(--h-input); border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface); color: var(--fg); }
.search-box { flex: none; }
.search-input { height: 30px; border: 0; box-shadow: none !important; }
.result-count { margin: -8px 0 0; font-size: var(--text-sm); color: var(--muted); font-variant-numeric: tabular-nums; }

.feedback-list { border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); overflow: hidden; }
.feedback-row {
  display: flex; align-items: flex-start; gap: 14px; padding: 16px;
  border-bottom: 1px solid var(--border);
}
.feedback-row:last-child { border-bottom: 0; }
.feedback-status-icon {
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 32px; height: 32px; border-radius: var(--radius-md);
  color: var(--faint); background: var(--surface-subtle);
}
.feedback-status-icon.status-warning { color: var(--warning); background: var(--warning-bg); }
.feedback-status-icon.status-danger { color: var(--danger); background: var(--danger-bg); }
.feedback-status-icon.status-success { color: var(--success); background: var(--success-bg); }
.feedback-main { flex: 1; min-width: 0; }
.feedback-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.feedback-title { font-size: var(--text-md); font-weight: 600; color: var(--fg); }
.feedback-score { font-family: var(--font-mono); font-weight: 600; font-variant-numeric: tabular-nums; color: var(--muted); }
.feedback-score.status-warning { color: var(--warning); }
.feedback-score.status-danger { color: var(--danger); }
.feedback-score.status-success { color: var(--success); }
.feedback-course { display: block; margin-top: 2px; color: var(--muted); font-size: var(--text-sm); }
.feedback-summary { margin: 6px 0 0; color: var(--muted); font-size: var(--text-base); line-height: var(--lh-body); }
.feedback-time { display: block; margin-top: 4px; color: var(--faint); font-family: var(--font-mono); font-size: var(--text-sm); }
.feedback-right { display: flex; flex: none; align-items: center; gap: 10px; }
.feedback-pill { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 8px; border-radius: var(--radius-sm); font-size: var(--text-sm); font-weight: 500; color: var(--muted); background: var(--surface-sunken); }
.feedback-pill.status-warning { color: var(--warning); background: var(--warning-bg); }
.feedback-pill.status-danger { color: var(--danger); background: var(--danger-bg); }
.feedback-pill.status-success { color: var(--success); background: var(--success-bg); }
.detail-link { display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface); color: var(--fg); font-size: var(--text-md); font-weight: 500; }
.detail-link:hover { border-color: var(--fg); }

.page-footer { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 12px; padding: 12px 16px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); color: var(--muted); font-size: var(--text-sm); }
.page-meta { grid-column: 1; }
.page-size-field { grid-column: 2; display: inline-flex; align-items: center; gap: 6px; }
.page-size { height: 30px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface); color: var(--fg); }
.page-nav { grid-column: 3; justify-self: end; display: flex; gap: 8px; }
.page-nav button { height: 30px; padding: 0 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface); color: var(--fg); }
.page-nav button:hover:not(:disabled) { border-color: var(--fg); }
.page-nav button:disabled { opacity: .45; }

@media (max-width: 820px) {
  .feedback-row { flex-wrap: wrap; }
  .feedback-right { width: 100%; justify-content: flex-end; }
  .page-footer { grid-template-columns: 1fr auto; }
  .page-meta { grid-column: 1; }
  .page-size-field { grid-column: 1 / -1; justify-self: end; }
  .page-nav { grid-column: 1 / -1; justify-self: stretch; }
}
</style>
