<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import ExperimentCatalog from '../../components/student/ExperimentCatalog.vue'
import { experimentsAPI } from '../../api/experiments.js'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'

const router = useRouter()

const items = ref([])
const summary = ref({ total: 0, not_started: 0, started: 0, submitted: 0, graded: 0 })
const loading = ref(true)
const failed = ref(false)
const searchInput = ref('')
const query = ref('')
const activeStatus = ref('')
const sortBy = ref('default')
const page = ref(1)
const pageSize = 10
const total = ref(0)
let searchTimer = null
const requestGuard = createLatestRequestGuard()

const summaryItems = computed(() => [
  { key: 'started', label: '进行中', value: summary.value.started },
  { key: 'not_started', label: '未开始', value: summary.value.not_started },
  { key: 'submitted', label: '已提交', value: summary.value.submitted },
  { key: 'graded', label: '已评分', value: summary.value.graded },
  { key: 'total', label: '实验总数', value: summary.value.total },
])

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

async function loadCatalog() {
  const sequence = requestGuard.begin()
  loading.value = true
  failed.value = false
  try {
    const response = await experimentsAPI.listStudentCatalog({
      q: query.value || undefined,
      status: activeStatus.value || undefined,
      sort: sortBy.value,
      page: page.value,
      page_size: pageSize,
    })
    if (!requestGuard.isLatest(sequence)) return
    const data = response.data
    items.value = data.items || []
    summary.value = data.summary || summary.value
    total.value = data.total || 0
    if (page.value > Math.max(1, Math.ceil(total.value / pageSize))) {
      page.value = Math.max(1, Math.ceil(total.value / pageSize))
    }
  } catch {
    if (!requestGuard.isLatest(sequence)) return
    items.value = []
    failed.value = true
  } finally {
    if (requestGuard.isLatest(sequence)) loading.value = false
  }
}

function selectStatus(value) {
  if (activeStatus.value === value) return
  activeStatus.value = value
  page.value = 1
}

function goToPage(value) {
  const nextPage = Math.min(Math.max(value, 1), pageCount.value)
  if (nextPage !== page.value) page.value = nextPage
}

function enterExperiment(module) {
  router.push(`/student/experiments/${module.id}${module.offering_id ? `?offering_id=${module.offering_id}` : ''}`)
}

watch(searchInput, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    const nextQuery = value.trim()
    if (query.value === nextQuery) return
    query.value = nextQuery
    page.value = 1
    loadCatalog()
  }, 350)
})

watch([activeStatus, sortBy, page], loadCatalog)

onMounted(loadCatalog)
onBeforeUnmount(() => { clearTimeout(searchTimer); requestGuard.invalidate() })
</script>

<template>
  <AppLayout content-mode="list">
    <div class="experiment-page">
      <header class="page-head">
        <div>
          <h1>实验模块</h1>
          <p>进入在线实验环境，动手实践编程与数据分析</p>
        </div>
      </header>

      <section class="metric-strip summary-panel" aria-label="实验状态汇总">
        <div
          v-for="stat in summaryItems"
          :key="stat.key"
          class="metric summary-item"
          :class="`summary-item--${stat.key}`"
        >
          <span class="summary-marker" aria-hidden="true"></span>
          <span class="summary-copy">
            <span class="m-label summary-label">{{ stat.label }}</span>
            <span class="m-value"><strong>{{ stat.value }}</strong> <small>个模块</small></span>
          </span>
        </div>
      </section>

      <ExperimentCatalog
        :items="items"
        :loading="loading"
        :failed="failed"
        :total="total"
        :page="page"
        :page-count="pageCount"
        :active-status="activeStatus"
        :query="searchInput"
        :sort-by="sortBy"
        :summary="summary"
        @retry="loadCatalog"
        @page="goToPage"
        @open="enterExperiment"
        @update:query="searchInput = $event"
        @update:sort-by="sortBy = $event"
        @select-status="selectStatus"
      />
    </div>
  </AppLayout>
</template>

<style scoped>
.experiment-page { display: flex; flex-direction: column; gap: var(--space-5); color: var(--fg); }
.page-head h1 { margin: 0 0 6px; font-family: var(--font-display); font-size: var(--text-3xl); font-weight: 600; line-height: var(--lh-tight); letter-spacing: -0.01em; }
.page-head p { margin: 0; color: var(--muted); font-size: var(--text-md); }
.summary-panel { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.summary-item { display: flex; align-items: center; justify-content: flex-start; gap: 12px; min-width: 0; padding: 14px 16px; }
.summary-marker { width: 10px; height: 30px; flex: 0 0 10px; border-radius: var(--radius-sm); background: var(--accent); }
.summary-item--not_started .summary-marker { background: var(--warning); }
.summary-item--submitted .summary-marker { background: var(--info); }
.summary-item--graded .summary-marker { background: var(--success); }
.summary-item--total .summary-marker { background: var(--accent); }
.summary-copy { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.summary-label { color: var(--muted); font-size: var(--text-sm); white-space: nowrap; }
.summary-copy .m-value { font-size: 22px; line-height: 1; }
.summary-copy small { color: var(--faint); font-size: var(--text-sm); font-weight: 400; }

@media (max-width: 1024px) {
  .summary-panel { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 560px) {
  .experiment-page { gap: var(--space-4); }
  .summary-panel { grid-template-columns: 1fr; }
}
</style>
