<script setup>
import { formatDateTime } from '../../utils/format.js'
import { formatMetric, metricLabels } from './labHelpers.js'
defineProps({ grade: { type: Object, required: true } })
</script>
<template>
  <div>
    <div class="lab-heading"><strong>成绩：{{ grade.total }} / 100</strong><span class="lab-muted">{{ grade.source === 'manual' ? '教师复核' : '自动评测' }} · {{ formatDateTime(grade.published_at) }}</span></div>
    <p v-if="grade.feedback">{{ grade.feedback }}</p>
    <dl v-if="Object.keys(grade.metrics || {}).length" class="lab-metrics"><div v-for="(value, key) in grade.metrics" :key="key"><dt>{{ metricLabels[key] || key }}</dt><dd>{{ formatMetric(value) }}</dd></div></dl>
    <div v-for="item in grade.items || []" :key="item.criterion_id" class="lab-record">
      <div class="lab-heading"><strong>{{ item.name }}</strong><span>{{ item.score }} / {{ item.max_score }}</span></div>
      <p v-if="item.reason">{{ item.reason }}</p>
      <details v-if="item.evidence?.length"><summary>评分依据</summary><blockquote v-for="(e, i) in item.evidence" :key="i"><span class="lab-muted">{{ e.cell_id ? `内容块 ${e.cell_id}` : '评测指标' }}<template v-if="e.line_start"> · 第 {{ e.line_start }}<template v-if="e.line_end && e.line_end !== e.line_start">–{{ e.line_end }}</template> 行</template></span><p>{{ e.quote || e.description }}</p></blockquote></details>
    </div>
  </div>
</template>
<style scoped>
blockquote { border-left: 2px solid var(--border-strong); margin: var(--space-2) 0; padding-left: var(--space-3); overflow-wrap: anywhere; }
</style>
