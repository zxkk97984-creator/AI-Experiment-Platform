<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { useAppStore } from '../../stores/app.js'
import LabGradeResult from './LabGradeResult.vue'
import LabRunCard from './LabRunCard.vue'
import LabLeaderboard from './LabLeaderboard.vue'
import { errorMessage, isActive } from './labHelpers.js'
import './labs.css'
const props = defineProps({ submissionId: { type: [Number, String], required: true }, enabled: Boolean })
const emit = defineEmits(['enabled', 'reviewed'])
const app = useAppStore()
const data = ref(null)
const error = ref('')
const busy = ref(false)
const score = ref('')
const feedback = ref('')
const reason = ref('')
let timer
let disposed = false
let polls = 0
async function load(initial = false) {
  try {
    const result = (await labsAPI.getSubmission(props.submissionId)).data
    if (disposed) return
    data.value = result
    emit('enabled', Boolean(result.enabled))
    if (initial && result.grade) { score.value = result.grade.total; feedback.value = result.grade.feedback || '' }
    clearTimeout(timer)
    if (isActive(result.run?.status) && polls++ < 1440) timer = setTimeout(() => load(), 5000)
    error.value = ''
  } catch (e) { if (props.enabled || data.value?.enabled) error.value = errorMessage(e, '评测详情加载失败') }
}
async function action(fn) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try { await fn(); await load() }
  catch (e) { error.value = errorMessage(e); app.showToast(error.value, 'error') }
  finally { busy.value = false }
}
async function review() {
  if (!reason.value.trim() || score.value === '' || !Number.isFinite(Number(score.value))) return
  await action(async () => {
    await labsAPI.review(props.submissionId, { score: Number(score.value), feedback: feedback.value, reason: reason.value.trim() })
    app.showToast('复核成绩已发布，改分原因已记录', 'success')
    emit('reviewed')
  })
}
async function regrade() {
  if (!reason.value.trim()) return
  await action(async () => {
    await labsAPI.regrade(props.submissionId, reason.value.trim())
    polls = 0
    app.showToast('已安排重新评测，将保留原评分记录', 'success')
  })
}
onMounted(() => load(true))
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <aside v-if="enabled || data?.enabled" class="lab-panel lab-review" aria-label="实验自动评测与人工复核">
    <h2>评测与复核</h2>
    <div v-if="error" role="alert" class="lab-error">{{ error }} <button class="btn btn-ghost btn-sm" @click="load">重新加载</button></div>
    <p v-if="!data && !error" role="status">正在加载评测详情…</p>
    <template v-if="data?.enabled">
      <p class="lab-muted">按第 {{ data.assessment?.version }} 版实验标准评分</p>
      <p v-if="data.review_requested" class="lab-notice">学生申请复核：{{ data.review_reason }}</p>
      <LabRunCard v-if="data.run" :run="data.run" />
      <LabGradeResult v-if="data.grade" :grade="data.grade" />
      <LabLeaderboard v-if="data.record_id" :record-id="data.record_id" />
      <form @submit.prevent="review">
        <label class="lab-field">最终成绩<input v-model="score" type="number" min="0" max="100" step="0.1" required /></label>
        <label class="lab-field">给学生的反馈<textarea v-model="feedback" rows="4" maxlength="20000" /></label>
        <label class="lab-field">复核或重评原因（必填）<textarea v-model="reason" rows="2" maxlength="2000" placeholder="说明纠正内容、依据或重新评测原因" required /></label>
        <div class="lab-actions"><button class="btn btn-primary" :disabled="busy || !reason.trim() || score === ''">发布复核成绩</button><button class="btn btn-secondary" type="button" :disabled="busy || !reason.trim() || isActive(data.run?.status)" @click="regrade">重新自动评测</button></div>
      </form>
      <details v-if="data.history?.length"><summary>成绩修订历史（{{ data.history.length }}）</summary><div v-for="grade in data.history" :key="grade.id" class="lab-record"><LabGradeResult :grade="grade" /><p v-if="grade.reason" class="lab-muted">原因：{{ grade.reason }}</p></div></details>
    </template>
  </aside>
</template>
<style scoped>
.lab-review { margin-bottom: 0; }
</style>
