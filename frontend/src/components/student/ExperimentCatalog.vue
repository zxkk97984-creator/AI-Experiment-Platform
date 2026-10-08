<script setup>
/** 学生实验目录展示组件：状态 tab、搜索排序、表格与分页。数据与请求全部由父组件驱动。 */
import { computed } from 'vue'
import UiStatusPill from '../ui/UiStatusPill.vue'
import AppIcon from '../ui/AppIcon.vue'
import StudentPagination from './StudentPagination.vue'
import { formatDateTime } from '../../utils/format.js'
import { EXPERIMENT_STATUS_MAP, statusBadge } from '../../utils/status.js'

const props = defineProps({
  items: { type: Array, required: true },
  loading: Boolean,
  failed: Boolean,
  total: { type: Number, required: true },
  page: { type: Number, required: true },
  pageCount: { type: Number, required: true },
  activeStatus: { type: String, default: '' },
  // 父组件传入实时搜索输入（父负责防抖后发起请求）
  query: { type: String, default: '' },
  sortBy: { type: String, default: 'default' },
  summary: { type: Object, required: true },
})
const emit = defineEmits(['retry', 'page', 'open', 'update:query', 'update:sort-by', 'select-status'])

const statusTabs = computed(() => [
  { value: '', label: '全部', count: props.summary.total },
  { value: 'started', label: '进行中', count: props.summary.started },
  { value: 'not_started', label: '未开始', count: props.summary.not_started },
  { value: 'submitted', label: '已提交', count: props.summary.submitted },
  { value: 'graded', label: '已评分', count: props.summary.graded },
])
const isFiltered = computed(() => Boolean(props.query || props.activeStatus))

function statusMeta(value) {
  return statusBadge(EXPERIMENT_STATUS_MAP, value)
}

function actionLabel(value) {
  if (value === 'not_started') return '开始实验'
  if (value === 'graded') return '查看实验'
  return '继续实验'
}
</script>

<template>
  <section class="catalog-section">
    <div class="catalog-toolbar">
      <div class="status-tabs" role="tablist" aria-label="按实验状态筛选">
        <button
          v-for="tab in statusTabs"
          :key="tab.value || 'all'"
          type="button"
          role="tab"
          :aria-selected="activeStatus === tab.value"
          :class="{ active: activeStatus === tab.value }"
          @click="emit('select-status', tab.value)"
        >
          {{ tab.label }}
          <span>{{ tab.count }}</span>
        </button>
      </div>

      <div class="catalog-tools">
        <label class="searchbox" :class="{ 'has-value': query }" style="width: 260px;">
          <AppIcon name="search" :size="15" />
          <input :value="query" type="search" class="input" placeholder="搜索实验模块名称" aria-label="搜索实验模块名称" @input="emit('update:query', $event.target.value)" />
          <button v-if="query" type="button" class="clear" aria-label="清空实验搜索" @click="emit('update:query', '')">
            <AppIcon name="close" :size="13" />
          </button>
        </label>
        <label class="sort-select">
          <span class="sr-only">实验排序</span>
          <DsSelect :value="sortBy" @change="emit('update:sort-by', $event.target.value)">
            <option value="default">默认排序</option>
            <option value="recent_desc">最近学习</option>
            <option value="name_asc">名称排序</option>
          </DsSelect>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 10 4 4 4-4"></path></svg>
        </label>
      </div>
    </div>

    <div class="catalog-heading">
      <h2>{{ activeStatus ? statusMeta(activeStatus).label : '全部实验模块' }}</h2>
      <span>共 {{ total }} 个</span>
    </div>

    <div v-if="loading" class="table-shell loading-shell" aria-label="正在加载实验模块">
      <div v-for="index in 6" :key="index" class="skeleton-row">
        <span class="skeleton skeleton-name"></span>
        <span class="skeleton skeleton-status"></span>
        <span class="skeleton skeleton-date"></span>
        <span class="skeleton skeleton-action"></span>
      </div>
    </div>

    <div v-else-if="failed" class="table-shell state-panel" role="alert">
      <strong>实验模块加载失败</strong>
      <p>请检查网络连接后重试。</p>
      <button type="button" class="retry-button" @click="emit('retry')">重新加载</button>
    </div>

    <div v-else-if="items.length === 0" class="table-shell state-panel">
      <strong>{{ isFiltered ? '没有找到匹配的实验模块' : '暂无可学习的实验模块' }}</strong>
      <p>{{ isFiltered ? '请尝试更换关键词或状态筛选。' : '教师发布实验后会显示在这里。' }}</p>
    </div>

    <div v-else class="table-shell">
      <table class="catalog-table">
        <thead>
          <tr>
            <th>实验名称</th>
            <th>状态</th>
            <th>最近学习</th>
            <th class="action-column">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="module in items" :key="`${module.offering_id || 'legacy'}:${module.id}`">
            <td data-label="实验名称">
              <strong class="module-name" :title="module.name">{{ module.name }}</strong>
              <small v-if="module.offering_id" class="offering-context">{{ [module.course_title, module.term_name, module.class_name].filter(Boolean).join(' · ') }}</small>
            </td>
            <td data-label="状态">
              <UiStatusPill
                :tone="statusMeta(module.learning_status).tone"
                :label="statusMeta(module.learning_status).label"
              />
            </td>
            <td data-label="最近学习" class="last-learning">
              {{ module.last_learning_at ? formatDateTime(module.last_learning_at) : '—' }}
            </td>
            <td data-label="操作" class="action-column">
              <button type="button" class="enter-button" @click="emit('open', module)">
                {{ actionLabel(module.learning_status) }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <StudentPagination :current-page="page" :page-count="pageCount" :total="total" :page-size="10" aria-label="实验模块分页" @change="emit('page', $event)" />
  </section>
</template>

<style scoped>
.offering-context { display: block; margin-top: 6px; color: var(--muted); white-space: normal; }
.catalog-section {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.catalog-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 58px;
  padding: 0 14px 0 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xs);
}

.status-tabs {
  display: flex;
  align-self: stretch;
  gap: 28px;
}

.status-tabs button {
  position: relative;
  flex: 0 0 auto;
  min-width: auto;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  box-shadow: none;
}

.status-tabs button:hover,
.status-tabs button.active {
  color: var(--accent);
  background: transparent;
  transform: none;
}

.status-tabs button.active::after {
  content: '';
  position: absolute;
  right: 0;
  bottom: -1px;
  left: 0;
  height: 3px;
  border-radius: var(--radius-sm) 3px 0 0;
  background: var(--accent);
}

.status-tabs button span {
  margin-left: 3px;
  color: var(--faint);
  font-size: 11px;
}

.catalog-tools {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sort-select {
  display: flex;
  align-items: center;
  height: 36px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  transition: border-color var(--duration-fast), box-shadow var(--duration-fast);
}

.sort-select:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.sort-select svg {
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  fill: none;
  stroke: var(--faint);
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.sort-select .ds-select {
  width: 100%;
  height: 100%;
  min-width: 0;
  padding: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--fg);
  font-family: var(--font-body);
  font-size: 13px;
  box-shadow: none;
}

.sort-select .ds-select:focus { box-shadow: none; }

.sort-select {
  position: relative;
  width: 120px;
  padding: 0 10px 0 12px;
}

.sort-select .ds-select {
  appearance: none;
  cursor: pointer;
  padding-right: 18px;
}

.sort-select svg {
  position: absolute;
  right: 9px;
  pointer-events: none;
}

.catalog-heading {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.catalog-heading h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 650;
}

.catalog-heading span {
  color: var(--muted);
  font-size: 13px;
}

.table-shell {
  overflow: hidden;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xs);
}

.catalog-table {
  margin: 0;
  table-layout: fixed;
}

.catalog-table th {
  height: 46px;
  padding: 0 20px;
  background: var(--surface-subtle);
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
}

.catalog-table th:first-child { width: 48%; }
.catalog-table th:nth-child(2) { width: 16%; }
.catalog-table th:nth-child(3) { width: 22%; }
.catalog-table th:last-child { width: 14%; }

.catalog-table td {
  height: 60px;
  padding: 0 20px;
  border-top: 1px solid var(--border);
  vertical-align: middle;
}

.catalog-table tbody tr {
  transition: background var(--duration-fast);
}

.catalog-table tbody tr:hover { background: var(--surface-sunken); }

.module-name {
  display: block;
  overflow: hidden;
  color: var(--fg);
  font-size: 14px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.last-learning {
  color: var(--muted);
  font-size: 13px;
}

.action-column { text-align: right; }

.enter-button,
.retry-button {
  min-height: 32px;
  padding: 6px 14px;
  border: 1px solid var(--accent-soft);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}

.enter-button:hover,
.retry-button:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.state-panel {
  display: flex;
  min-height: 240px;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  padding: 40px 20px;
  text-align: center;
}

.state-panel strong { font-size: 16px; }
.state-panel p {
  margin: 8px 0 18px;
  color: var(--muted);
  font-size: 13px;
}

.loading-shell { padding: 0 20px; }
.skeleton-row {
  display: grid;
  grid-template-columns: 48% 16% 22% 14%;
  align-items: center;
  height: 60px;
  border-bottom: 1px solid var(--border);
}
.skeleton-row:last-child { border-bottom: 0; }
.skeleton { height: 12px; border-radius: var(--radius-sm); }
.skeleton-name { width: 58%; }
.skeleton-status { width: 64px; }
.skeleton-date { width: 120px; }
.skeleton-action { width: 72px; justify-self: end; }

.pagination {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  min-height: 40px;
  color: var(--muted);
  font-size: 12px;
}

.pagination-controls {
  display: flex;
  gap: 8px;
}

.pagination button {
  width: 34px;
  height: 34px;
  min-width: 34px;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--muted);
  font-size: 12px;
}

.pagination button:hover:not(:disabled) {
  border-color: var(--accent-soft);
  color: var(--accent);
}

.pagination button.active {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--surface);
}

.pagination button:disabled { cursor: not-allowed; opacity: .45; }
.pagination svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.page-size { justify-self: end; }

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 1100px) {
  .catalog-toolbar { align-items: stretch; flex-direction: column; padding: 0 16px 14px; }
  .status-tabs { min-height: 54px; overflow-x: auto; }
  .catalog-tools { justify-content: flex-end; }
}

@media (max-width: 767.98px) {
  .catalog-toolbar { padding-inline: 12px; }
  .status-tabs { gap: 22px; }
  .catalog-tools { align-items: stretch; flex-direction: column; }
  .catalog-toolbar .searchbox, .sort-select { width: 100% !important; }
  .table-shell { overflow: visible; border: 0; background: transparent; box-shadow: none; }
  .catalog-table, .catalog-table tbody { display: block; }
  .catalog-table thead { display: none; }
  .catalog-table tr {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 14px 18px;
    margin-bottom: 10px;
    padding: 18px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-xs);
  }
  .catalog-table td {
    display: block;
    width: auto;
    height: auto;
    padding: 0;
    border: 0;
    text-align: left;
  }
  .catalog-table td:first-child { grid-column: 1 / -1; }
  .catalog-table td:nth-child(3) { align-self: center; }
  .catalog-table td:nth-child(3)::before {
    content: '最近学习  ';
    color: var(--faint);
  }
  .catalog-table .action-column { grid-column: 1 / -1; justify-self: end; }
  .module-name { white-space: normal; }
  .loading-shell { padding: 0; }
  .skeleton-row {
    grid-template-columns: 1fr 80px;
    height: 126px;
    margin-bottom: 10px;
    padding: 18px;
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    background: var(--surface);
  }
  .skeleton-date { display: none; }
  .pagination { grid-template-columns: 1fr auto; }
  .pagination-total { display: none; }
  .pagination-controls { justify-self: start; }
}

@media (prefers-reduced-motion: reduce) {
  .catalog-table tbody tr,
  .searchbox,
  .sort-select { transition: none; }
}
</style>
