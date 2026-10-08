<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { formatDateTime } from '../../utils/format.js'
import { errorMessage, formatMetric, isActive, metricLabels, statusLabel } from './labHelpers.js'
const props = defineProps({ run: { type: Object, required: true }, cancellable: Boolean })
const emit = defineEmits(['cancel'])
const titles = { train: '模型训练', training: '模型训练', evaluate: '正式评测', evaluation: '正式评测', public_validate: '公开自测', baseline: '参考试跑', validate_dataset: '数据校验', validate_artifact: '成果校验', dataset_validate: '数据校验', artifact_validate: '成果校验' }
const detailResult = ref(null)
const result = computed(() => ({ ...detailResult.value, ...props.run.result }))
const curves = computed(() => (result.value.curves || []).map(p => ({ ...p, loss: p.train_loss ?? p.loss, step: p.epoch ?? p.step })).filter(p => Number.isFinite(p.loss)))
const maxLoss = computed(() => Math.max(1, ...curves.value.flatMap(p => [p.loss, p.val_loss].filter(Number.isFinite))))
const points = computed(() => curves.value.map((p, i) => `${16 + i / Math.max(1, curves.value.length - 1) * 568},${145 - p.loss / maxLoss.value * 125}`).join(' '))
const validationPoints = computed(() => curves.value.map((p, i) => Number.isFinite(p.val_loss) ? `${16 + i / Math.max(1, curves.value.length - 1) * 568},${145 - p.val_loss / maxLoss.value * 125}` : null).filter(Boolean).join(' '))
const fetchedLogs = ref(null)
const logError = ref('')
const logLoading = ref(false)
const logs = computed(() => { const value = fetchedLogs.value ?? props.run.logs; return Array.isArray(value) ? value.join('\n') : (value || '') })
const hasLogs = computed(() => Boolean(props.run.logs) || ['train', 'training', 'public_validate', 'baseline'].includes(props.run.kind))
let logTimer
let disposed = false
let expanded = false
let logPolls = 0
async function loadLogs() {
  if (disposed || logLoading.value) return
  clearTimeout(logTimer)
  logLoading.value = true
  try {
    const { data } = await labsAPI.getRun(props.run.id)
    if (disposed) return
    fetchedLogs.value = data.logs || ''
    detailResult.value = data.result || {}
    logError.value = ''
    if (expanded && isActive(data.status) && logPolls++ < 360) logTimer = setTimeout(loadLogs, 5000)
  } catch (e) { logError.value = errorMessage(e, '日志加载失败') }
  finally { logLoading.value = false }
}
function toggleLogs(event) {
  expanded = event.target.open
  clearTimeout(logTimer)
  if (expanded) { logPolls = 0; loadLogs() }
}
onBeforeUnmount(() => { disposed = true; clearTimeout(logTimer) })
</script>
<template>
  <article class="lab-record">
    <div class="lab-heading"><strong>{{ titles[run.kind] || '实验任务' }} #{{ run.id }}</strong><span class="lab-pill">{{ statusLabel(run.status) }}</span></div>
    <p class="lab-muted">{{ formatDateTime(run.created_at) }} · {{ run.device === 'gpu' ? 'GPU' : 'CPU' }}<span v-if="result.elapsed_seconds != null"> · 用时 {{ Math.round(result.elapsed_seconds) }} 秒</span></p>
    <p v-if="run.error_message" class="lab-error">{{ run.error_message }}</p>
    <dl v-if="Object.keys(result.metrics || {}).length" class="lab-metrics"><div v-for="(value, key) in result.metrics" :key="key"><dt>{{ metricLabels[key] || key }}</dt><dd>{{ formatMetric(value) }}</dd></div></dl>

    <details v-if="hasLogs" @toggle="toggleLogs"><summary>查看日志与训练曲线</summary><figure v-if="curves.length > 1" class="curve"><figcaption>损失曲线：实线为训练，虚线为验证（横轴：记录顺序；纵轴：损失）</figcaption><svg viewBox="0 0 600 165" role="img" aria-label="训练损失随训练过程的变化"><path d="M16 12V145H590" fill="none" stroke="var(--border-strong)" /><polyline :points="points" fill="none" stroke="var(--accent)" stroke-width="2" /><polyline v-if="validationPoints" :points="validationPoints" fill="none" stroke="var(--muted)" stroke-width="2" stroke-dasharray="6 4" /></svg></figure><p v-if="logError" class="lab-error" role="alert">{{ logError }}</p><pre v-if="logs">{{ logs }}</pre><p v-else class="lab-muted">{{ logLoading ? '正在读取日志…' : '暂无运行日志' }}</p><button class="btn btn-ghost btn-sm" type="button" :disabled="logLoading" @click="loadLogs">刷新日志</button></details>
    <button v-if="cancellable && isActive(run.status) && run.status !== 'cancel_requested'" class="btn btn-ghost btn-sm" type="button" @click="emit('cancel', run.id)">取消任务</button>
  </article>
</template>
<style scoped>
.curve { margin: var(--space-3) 0; color: var(--muted); font-size: var(--text-xs); }
.curve svg { width: 100%; max-height: 190px; }
</style>
