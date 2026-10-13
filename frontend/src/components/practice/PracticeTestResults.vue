<script setup>
// 确定性（普通判题）结果：测试组逐项通过情况。
// 未完成的判题没有 deterministic 数据时整个区块不渲染，避免出现「0 / 0」的假成绩。
import { computed } from 'vue'
import UiStatusPill from '../ui/UiStatusPill.vue'

const props = defineProps({
  deterministic: { type: Object, default: null },
})

const GROUP_STATUS = {
  passed: { label: '通过', tone: 'success' },
  failed: { label: '未通过', tone: 'danger' },
  error: { label: '运行错误', tone: 'warning' },
  skipped: { label: '未运行', tone: 'neutral' },
}

const groups = computed(() => props.deterministic?.groups || [])
const passedCount = computed(() => Number(props.deterministic?.passed_count ?? 0))
const totalCount = computed(() => Number(props.deterministic?.total_count ?? groups.value.length))
const summary = computed(() => `${passedCount.value} / ${totalCount.value} 组测试通过`)
const allPassed = computed(() => totalCount.value > 0 && passedCount.value === totalCount.value)

function groupMeta(status) {
  return GROUP_STATUS[status] || GROUP_STATUS.skipped
}
</script>

<template>
  <section v-if="deterministic" class="test-results" aria-label="测试结果">
    <header class="results-head">
      <h3>测试结果</h3>
      <UiStatusPill :tone="allPassed ? 'success' : 'warning'" :label="summary" />
    </header>
    <ul v-if="groups.length" class="group-list">
      <li v-for="(group, index) in groups" :key="group.name || index" class="group-row">
        <span class="group-name" :title="group.name">{{ group.name || `测试组 ${index + 1}` }}</span>
        <span class="group-count mono">{{ group.passed ?? 0 }} / {{ group.total ?? 0 }}</span>
        <UiStatusPill :tone="groupMeta(group.status).tone" :label="groupMeta(group.status).label" />
      </li>
    </ul>
    <p v-else class="group-empty">本次判题没有可展示的测试组明细</p>
    <p v-if="deterministic.detail" class="group-detail">{{ deterministic.detail }}</p>
  </section>
</template>

<style scoped>
.test-results { display: flex; flex-direction: column; gap: var(--space-3); }
.results-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
.results-head h3 { margin: 0; font-size: var(--text-md); }
.group-list { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; }
.group-row { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-2) var(--space-3); font-size: var(--text-md); }
.group-row + .group-row { border-top: 1px solid var(--border); }
.group-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.group-count { color: var(--muted); font-variant-numeric: tabular-nums; }
.group-empty, .group-detail { margin: 0; color: var(--muted); font-size: var(--text-md); }
</style>
