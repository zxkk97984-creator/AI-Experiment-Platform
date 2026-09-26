<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { adminLogsAPI } from '../../api/adminLogs.js'
import { useAppStore } from '../../stores/app.js'

const app = useAppStore()
const loading = ref(true)
const items = ref([])
const total = ref(0)
const fileMeta = ref(null)
const source = ref('api')
const level = ref('')
const query = ref('')
const autoRefresh = ref(false)
let searchTimer = null
let refreshTimer = null

const LEVEL_OPTIONS = [
  { value: '', label: '全部级别' },
  { value: 'ERROR', label: 'ERROR 及以上' },
  { value: 'WARNING', label: 'WARNING 及以上' },
  { value: 'INFO', label: 'INFO' },
]

const hasAiFilter = computed(() => query.value.trim().toLowerCase().includes('ai'))

function requestParams() {
  return {
    source: source.value,
    level: level.value || undefined,
    q: query.value.trim() || undefined,
    limit: 300,
  }
}

async function fetch() {
  loading.value = true
  try {
    const res = await adminLogsAPI.listLogs(requestParams())
    items.value = res.data.items
    total.value = res.data.total
    fileMeta.value = res.data
  } catch (error) {
    const message = error.response?.data?.detail?.message
    app.showToast(message || '加载日志失败', 'error')
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function resetAndFetch() { fetch() }

watch(() => query.value, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(resetAndFetch, 300)
})

watch(autoRefresh, (enabled) => {
  clearInterval(refreshTimer)
  if (enabled) refreshTimer = setInterval(fetch, 10000)
})

onBeforeUnmount(() => {
  clearTimeout(searchTimer)
  clearInterval(refreshTimer)
})

function levelTone(lv) {
  return { ERROR: 'error', CRITICAL: 'error', WARNING: 'warn', INFO: 'info', DEBUG: 'muted' }[lv] || 'muted'
}

function fmtTime(ts) {
  if (!ts) return '—'
  return String(ts).replace('T', ' ').replace(/\.\d+.*$/, '').replace(/\+00:00$/, 'Z')
}

function recordDetail(item) {
  const extras = Object.entries(item)
    .filter(([key]) => !['ts', 'level', 'logger', 'rid', 'message', 'exc'].includes(key))
    .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
    .join('  ')
  return [extras, item.exc].filter(Boolean).join('\n')
}

function quickFilterAi() {
  query.value = hasAiFilter.value ? '' : 'ai_'
}

onMounted(fetch)
</script>

<template>
  <AppLayout content-mode="list">
    <div class="page">
      <header class="page-head">
        <div>
          <h1 class="page-title">系统日志</h1>
          <p class="page-sub">API 与判题 Worker 的运行日志（结构化 JSON，自动脱敏），用于快速定位 AI 评分与判题异常</p>
        </div>
        <div class="page-meta">
          <button class="btn-ghost" :class="{ active: hasAiFilter }" @click="quickFilterAi">
            <AppIcon name="brain" :size="14" /> AI 事件
          </button>
          <button class="btn-ghost" @click="fetch"><AppIcon name="refresh" :size="14" /> 刷新</button>
          <label class="auto-refresh">
            <input v-model="autoRefresh" type="checkbox"> 每 10s 自动刷新
          </label>
        </div>
      </header>

      <div class="filter-bar">
        <div class="source-tabs" role="tablist" aria-label="日志来源">
          <button
            v-for="s in ['api', 'worker']"
            :key="s"
            role="tab"
            :aria-selected="source === s"
            :class="{ active: source === s }"
            @click="source = s; resetAndFetch()"
          >{{ s === 'api' ? 'API 服务' : '判题 Worker' }}</button>
        </div>
        <DsSelect v-model="level" host-class="level-select" aria-label="日志级别" @change="resetAndFetch">
          <option v-for="opt in LEVEL_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
        </DsSelect>
        <label class="searchbox" :class="{ 'has-value': query }">
          <AppIcon name="search" :size="15" />
          <input v-model="query" type="search" class="input" placeholder="按关键词、logger 或 request_id 过滤" aria-label="搜索日志">
          <button v-if="query" type="button" class="clear" aria-label="清空搜索" @click="query = ''">
            <AppIcon name="close" :size="13" />
          </button>
        </label>
        <div class="filter-count">
          {{ fileMeta ? `${fileMeta.file} · ${(fileMeta.file_size / 1024).toFixed(0)} KB` : '' }}
          共匹配 {{ total }} 条
        </div>
      </div>

      <div v-if="loading && !items.length" class="card table-card">
        <div class="skeleton-row" v-for="i in 8" :key="i">
          <div class="skeleton skel-cell w-15"></div>
          <div class="skeleton skel-cell w-10"></div>
          <div class="skeleton skel-cell w-15"></div>
          <div class="skeleton skel-cell w-60"></div>
        </div>
      </div>

      <div v-else-if="!items.length" class="empty-state">
        <p>暂无匹配日志{{ source === 'worker' ? '（Worker 未启动或尚未产生日志）' : '' }}</p>
      </div>

      <div v-else class="card table-card log-card">
        <table class="ds-table log-table">
          <thead>
            <tr><th class="col-time">时间 (UTC)</th><th class="col-level">级别</th><th class="col-logger">来源</th><th>内容</th></tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in items" :key="`${item.ts}-${index}`" :class="`row-${levelTone(item.level)}`">
              <td class="text-sm text-secondary col-time">{{ fmtTime(item.ts) }}</td>
              <td><span class="badge" :class="`badge-${levelTone(item.level)}`">{{ item.level }}</span></td>
              <td class="text-sm text-secondary col-logger">
                {{ item.logger || '—' }}
                <small v-if="item.rid && item.rid !== '-'">rid={{ item.rid }}</small>
              </td>
              <td class="log-message">
                <p>{{ item.message }}</p>
                <pre v-if="recordDetail(item)">{{ recordDetail(item) }}</pre>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </AppLayout>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 16px; }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.page-title { margin: 0; color: var(--fg); font-size: 24px; letter-spacing: -0.02em; }
.page-sub { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
.page-meta { display: flex; align-items: center; gap: 9px; flex: none; }
.page-meta .btn-ghost.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.auto-refresh { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; white-space: nowrap; cursor: pointer; }
.auto-refresh input { accent-color: var(--accent); }

.filter-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.source-tabs { display: inline-flex; padding: 3px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.source-tabs button { padding: 6px 14px; border: 0; border-radius: calc(var(--radius-md) - 2px); background: transparent; color: var(--muted); font-size: 12px; font-weight: 600; cursor: pointer; }
.source-tabs button.active { background: var(--fg); color: var(--surface); }
.level-select { flex: 0 0 176px; }
.searchbox { flex: 1; min-width: 220px; }
.filter-count { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }

/* This card is the scrollport: card padding would leave a strip above sticky headers. */
.log-card { padding: 0; max-height: calc(100dvh - 230px); overflow: auto; }
.log-table th { position: sticky; top: 0; z-index: 1; background: var(--surface-subtle); }
.log-table { table-layout: fixed; }
.log-table th { vertical-align: middle; }
.log-table td { overflow-wrap: anywhere; vertical-align: top; }
.col-time { width: 170px; }
.col-level { width: 80px; }
.col-logger { width: 180px; }
.col-logger small { display: block; margin-top: 3px; color: var(--faint); font-size: 10px; }
.log-message p { margin: 0; color: var(--fg); font-size: 12.5px; line-height: 1.6; word-break: break-word; white-space: pre-wrap; }
.log-message pre {
  max-height: 220px;
  margin: 7px 0 0;
  overflow: auto;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  color: var(--muted);
  font: 11px/1.6 var(--font-mono);
  white-space: pre-wrap;
  word-break: break-word;
}
.row-error td { background: color-mix(in oklch, var(--danger-bg) 45%, transparent); }
.row-warn td { background: color-mix(in oklch, var(--warning-bg) 35%, transparent); }
.badge-error { color: var(--danger); background: var(--danger-bg); }
.badge-warn { color: var(--warning); background: var(--warning-bg); }
.badge-info { color: var(--accent-hover); background: var(--accent-soft); }
.badge-muted { color: var(--muted); background: var(--surface-subtle); }

</style>
