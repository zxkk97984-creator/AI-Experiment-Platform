<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { useAppStore } from '../../stores/app.js'
import LabRunCard from './LabRunCard.vue'
import LabGradeResult from './LabGradeResult.vue'
import { errorMessage, isActive } from './labHelpers.js'
import './labs.css'
const props = defineProps({ submissionId: { type: [Number, String], required: true } })
const app = useAppStore()
const detail = ref(null)
const reason = ref('')
const error = ref('')
const busy = ref(false)
let timer
let disposed = false
let polls = 0
async function load() {
  if (disposed) return
  try {
    const { data } = await labsAPI.getSubmission(props.submissionId)
    if (disposed) return
    detail.value = data
    error.value = ''
    clearTimeout(timer)
    if (data.enabled && isActive(data.run?.status) && polls++ < 1440) timer = setTimeout(load, 5000)
  } catch (e) { error.value = errorMessage(e, '评测反馈加载失败') }
}
async function appeal() {
  if (busy.value || !reason.value.trim()) return
  busy.value = true
  try {
    await labsAPI.appeal(props.submissionId, reason.value.trim())
    await load()
    app.showToast('已提交复核请求', 'success')
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <section v-if="detail?.enabled || error" class="lab-panel feedback-panel" aria-label="实验评测反馈">
    <p v-if="error" class="lab-error" role="alert">{{ error }} <button class="btn btn-ghost btn-sm" @click="load">重试</button></p>
    <template v-if="detail?.enabled">
      <LabRunCard v-if="detail.run && !detail.grade" :run="detail.run" />
      <LabGradeResult v-if="detail.grade" :grade="detail.grade" />
      <p v-if="detail.review_requested" class="lab-notice">已申请教师复核：{{ detail.review_reason }}</p>
      <form v-else-if="detail.grade" @submit.prevent="appeal"><label class="lab-field">申请复核的原因<textarea v-model="reason" rows="2" maxlength="2000" placeholder="说明存在疑问的评分项及依据" required /></label><button class="btn btn-secondary btn-sm" :disabled="busy || !reason.trim()">申请教师复核</button></form>
      <details v-if="detail.history?.length"><summary>此前评分记录</summary><div v-for="grade in detail.history" :key="grade.id" class="lab-record"><LabGradeResult :grade="grade" /></div></details>
    </template>
  </section>
</template>
<style scoped>
.feedback-panel { padding: 0; border: 0; margin-bottom: 0; }
</style>
