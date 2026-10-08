<script setup>
import { computed, ref, onBeforeUnmount } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { experimentsAPI } from '../../api/experiments.js'
import { useAppStore } from '../../stores/app.js'
import { errorMessage } from './labHelpers.js'
const props = defineProps({ kind: { type: String, required: true }, entryId: { type: [Number, String], required: true }, version: { type: Number, required: true }, offeringId: { type: [Number, String], default: null } })
const app = useAppStore()
const rows = ref([])
const selected = ref([])
const query = ref('')
const reason = ref('')
const busy = ref(false)
const loaded = ref(false)
const error = ref('')
const batch = ref(null)
let timer
let disposed = false
let polls = 0
const records = computed(() => {
  const grouped = new Map()
  for (const row of rows.value) {
    if (row.entry_type !== props.kind || Number(row.entry_id) !== Number(props.entryId)) continue
    if (!grouped.has(row.record_id)) grouped.set(row.record_id, { id: row.record_id, name: row.student_name || row.student_username || '学生', submissions: [] })
    grouped.get(row.record_id).submissions.push(row)
  }
  return [...grouped.values()]
})
const selectedIds = computed(() => records.value.filter(r => selected.value.includes(r.id)).flatMap(r => r.submissions.map(s => s.id)))
async function load() {
  busy.value = true
  error.value = ''
  selected.value = []
  rows.value = []
  try {
    const items = []
    let page = 1
    let total
    do {
      const { data } = await experimentsAPI.listSubmissions({ entry_id: props.entryId, ...(props.offeringId ? { offering_id: props.offeringId } : {}), q: query.value.trim() || undefined, page, page_size: 100 })
      total = data.total
      if (total > 5000) throw new Error('匹配提交超过 5000 条，请输入学生姓名或学号缩小范围后加载')
      items.push(...data.items)
      page++
      if (!data.items.length) break
    } while (items.length < total)
    rows.value = items
    if (props.offeringId) selected.value = records.value.map(r => r.id)
    loaded.value = true
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
async function regrade() {
  if (!reason.value.trim() || !selectedIds.value.length || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const { data } = await labsAPI.regradeBatch(props.kind, props.entryId, { submission_ids: selectedIds.value, reason: reason.value.trim() }, ...(props.offeringId ? [props.offeringId] : []))
    batch.value = data
    polls = 0
    await pollBatch()
    app.showToast('已安排整组重评，完成后统一更新成绩', 'success')
    selected.value = []
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
async function pollBatch() {
  clearTimeout(timer)
  if (!batch.value || disposed) return
  try {
    const { data } = await labsAPI.getRegradeBatch(batch.value.id)
    if (disposed) return
    batch.value = data
    if (data.status === 'running' && polls++ < 1440) timer = setTimeout(pollBatch, 5000)
  } catch (e) { error.value = errorMessage(e) }
}
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <details><summary>使用第 {{ version }} 版标准重评历史提交</summary>
    <p class="lab-muted">{{ offeringId ? '本班该实验全部学生的所有正式提交一起重评；成功后统一采用新标准，失败或撤销授课权限时保留原成绩。' : '每位学生的全部提交一起重评，完成后统一更新最终成绩，避免把不同标准下的分数混合比较。' }}</p>
    <label v-if="!offeringId" class="lab-field">按学生姓名或学号查找<input v-model="query" placeholder="留空加载本实验的历史提交" /></label>
    <button class="btn btn-secondary btn-sm" type="button" :disabled="busy" @click="load">{{ busy ? '处理中…' : offeringId ? '加载本班全部提交' : '加载可选学生' }}</button>
    <p v-if="error" class="lab-error" role="alert">{{ error }}</p>
    <p v-if="batch" role="status">重评进度：{{ batch.completed || 0 }}/{{ batch.total || selectedIds.length }} · {{ batch.status === 'succeeded' ? '已全部完成并发布' : batch.status === 'needs_review' ? '需要教师处理，原成绩保留' : '正在重评，原成绩暂时保留' }} <button type="button" class="btn btn-ghost btn-sm" @click="polls = 0; pollBatch()">刷新</button></p>
    <p v-if="loaded && !records.length" class="lab-muted">没有匹配的历史提交。</p>
    <label v-for="record in records" :key="record.id" class="lab-check"><input v-model="selected" type="checkbox" :value="record.id" :disabled="busy || Boolean(offeringId)" />{{ record.name }} · 全部 {{ record.submissions.length }} 次提交</label>
    <template v-if="records.length">
      <label class="lab-field">批量重评原因<textarea v-model="reason" rows="2" maxlength="2000" placeholder="例如：纠正测试标签后，按新标准重评全部历史结果" /></label>
      <button class="btn btn-secondary" type="button" :disabled="busy || !reason.trim() || !selectedIds.length" @click="regrade">重评已选 {{ selected.length }} 位学生的 {{ selectedIds.length }} 次提交</button>
    </template>
  </details>
</template>
