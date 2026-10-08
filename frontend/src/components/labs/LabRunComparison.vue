<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { errorMessage, formatMetric, metricLabels } from './labHelpers.js'
const props = defineProps({ runs: { type: Array, required: true } })
const completed = computed(() => props.runs.filter(run => ['succeeded', 'completed'].includes(run.status)).slice(0, 5))
const loaded = ref([])
const loading = ref(false)
const error = ref('')
let disposed = false
const results = computed(() => loaded.value.map(run => ({ ...run, last: run.result?.curves?.at(-1) })))
async function load(event) {
  if (event && !event.target.open) return
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const responses = await Promise.all(completed.value.map(run => labsAPI.getRun(run.id)))
    if (!disposed) loaded.value = responses.map(response => response.data)
  } catch (e) { error.value = errorMessage(e, '实验比较结果加载失败') }
  finally { loading.value = false }
}
onBeforeUnmount(() => { disposed = true })

</script>
<template>
  <details v-if="completed.length > 1" @toggle="load"><summary>比较实验结果</summary><p class="lab-muted">比较最近五次已完成任务。</p><p v-if="loading" role="status">正在加载结果…</p><p v-if="error" class="lab-error" role="alert">{{ error }}</p><button class="btn btn-ghost btn-sm" type="button" :disabled="loading" @click="load()">刷新比较</button><div class="comparison-grid"><article v-for="run in results" :key="run.id"><strong>任务 #{{ run.id }}</strong><dl><template v-if="run.last"><dt>训练轮次</dt><dd>{{ run.last.epoch ?? run.last.step ?? '—' }}</dd><dt>学习率</dt><dd>{{ formatMetric(run.last.learning_rate) }}</dd><dt>训练 / 验证损失</dt><dd>{{ formatMetric(run.last.train_loss ?? run.last.loss) }} / {{ formatMetric(run.last.val_loss) }}</dd><dt>训练 / 验证准确率</dt><dd>{{ formatMetric(run.last.train_accuracy ?? run.last.accuracy) }} / {{ formatMetric(run.last.val_accuracy) }}</dd></template><template v-for="(value, key) in run.result?.metrics || {}" :key="key"><dt>{{ metricLabels[key] || key }}</dt><dd>{{ formatMetric(value) }}</dd></template></dl></article></div></details>
</template>
<style scoped>
.comparison-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: var(--space-3); }
article { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space-3); }
dt { color: var(--muted); margin-top: var(--space-2); }
dd { margin: var(--space-1) 0; font-variant-numeric: tabular-nums; }
</style>
