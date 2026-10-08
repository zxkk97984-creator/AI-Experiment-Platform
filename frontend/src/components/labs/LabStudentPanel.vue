<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { useAppStore } from '../../stores/app.js'
import { useExperimentStore } from '../../stores/experiment.js'
import LabRunCard from './LabRunCard.vue'
import ConfirmDialog from '../ui/ConfirmDialog.vue'
import LabSubmissionFeedback from './LabSubmissionFeedback.vue'
import LabRunComparison from './LabRunComparison.vue'
import LabLeaderboard from './LabLeaderboard.vue'
import { downloadLabFile, errorMessage, isActive, statusLabel, taskLabels, metricLabels } from './labHelpers.js'
import './labs.css'
const emit = defineEmits(['enabled'])
const store = useExperimentStore()
const app = useAppStore()
const overview = ref(null)
const loading = ref(true)
const error = ref('')
const busy = ref(false)
const selectedArtifact = ref(null)
const device = ref('cpu')
const detailId = ref(null)
const deleteTarget = ref(null)
const config = computed(() => overview.value?.assessment?.config || {})
const artifacts = computed(() => overview.value?.artifacts || [])
const activeTraining = computed(() => (overview.value?.runs || []).some(r => ['train', 'training'].includes(r.kind) && isActive(r.status)))
const activeEvaluation = computed(() => (overview.value?.runs || []).some(r => r.kind === 'evaluate' && isActive(r.status)))
const activeValidation = computed(() => (overview.value?.runs || []).some(r => r.kind === 'public_validate' && isActive(r.status)))
const canSubmit = computed(() => !activeEvaluation.value && !busy.value && !store.submitting && overview.value?.attempts_used < overview.value?.max_submissions && (config.value.task_type === 'notebook' || artifacts.value.some(a => a.id === selectedArtifact.value && a.status === 'ready' && a.compatible !== false)))
let timer
let disposed = false
let polls = 0
let requestId = null
let requestKey = null
let lastRecordId = store.recordId
async function load({ silent = false } = {}) {
  if (disposed) return
  if (!silent) error.value = ''
  const recordId = store.recordId
  try {
    const { data } = await labsAPI.getOverview(recordId)
    if (disposed || recordId !== store.recordId) return
    overview.value = data
    store.cellCheckTargets = [...new Set((data.assessment?.config?.cell_checks || []).map(c => c.cell_id))]
    emit('enabled', Boolean(data.enabled))
    if (!data.enabled) return
    if (!selectedArtifact.value || !data.artifacts?.some(a => a.id === selectedArtifact.value && a.compatible !== false)) selectedArtifact.value = data.artifacts?.find(a => a.status === 'ready' && a.compatible !== false)?.id || null
    clearTimeout(timer)
    if ((data.runs?.some(r => isActive(r.status)) || data.artifacts?.some(a => isActive(a.status))) && polls++ < 1440) timer = setTimeout(() => load({ silent: true }), 5000)
  } catch (e) {
    // A legacy installation may not have the lab module enabled yet.
    if (e.response?.status !== 404) error.value = errorMessage(e, '实验任务状态加载失败，请重试')
  } finally { loading.value = false }
}
async function action(fn) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try { await fn() }
  catch (e) { error.value = errorMessage(e); app.showToast(error.value, 'error') }
  finally { busy.value = false }
}
async function saveNotebook() {
  if (store.conflict || !(await store.flushSave()) || store.dirty) throw new Error('请先保存 Notebook 并解决版本冲突')
}
function requestFor(key) {
  if (requestKey !== key || !requestId) { requestKey = key; requestId = crypto.randomUUID() }
  return requestId
}
async function train() {
  await action(async () => {
    await saveNotebook()
    await labsAPI.train(store.recordId, { expected_record_revision: store.recordRevision, device: device.value, client_request_id: requestFor(`train-${store.recordId}-${store.recordRevision}-${device.value}`) })
    requestId = null
    polls = 0
    await load()
    app.showToast('训练已加入队列，离开页面后任务仍会继续', 'success')
  })
}
async function upload(event) {
  const file = event.target.files?.[0]
  if (!file) return
  await action(async () => {
    await saveNotebook()
    const { data } = await labsAPI.uploadArtifact(store.recordId, file, store.recordRevision)
    selectedArtifact.value = data.id
    polls = 0
    await load()
    app.showToast('成果已上传，正在校验', 'success')
  })
  event.target.value = ''
}
async function validate() {
  await action(async () => {
    await labsAPI.validate(store.recordId, { artifact_id: selectedArtifact.value, client_request_id: requestFor(`validate-${store.recordId}-${selectedArtifact.value}`) })
    requestId = null
    polls = 0
    await load()
    app.showToast('公开验证已加入队列，不计入正式提交次数', 'success')
  })
}
async function submit() {
  if (!canSubmit.value) return
  await action(async () => {
    const ok = await store.submitExperiment({ artifact_id: config.value.task_type === 'notebook' ? undefined : selectedArtifact.value })
    if (ok) { polls = 0; await load() }
  })
}
function showSubmission(id) { detailId.value = id }
async function cancel(id) { await action(async () => { await labsAPI.cancelRun(id); await load() }) }
async function deleteArtifact() {
  if (!deleteTarget.value) return
  await action(async () => {
    await labsAPI.deleteArtifact(deleteTarget.value.id)
    deleteTarget.value = null
    await load()
    app.showToast('成果已删除，存储额度已释放', 'success')
  })
}
async function moreFiles(artifact) {
  await action(async () => {
    const page = Math.floor((artifact.files?.length || 0) / 50) + 1
    const { data } = await labsAPI.artifactFiles(artifact.id, page)
    artifact.files = [...artifact.files, ...data.items]
    artifact.files_truncated = artifact.files.length < data.total
  })
}
async function download(file) { await action(() => downloadLabFile(labsAPI, file)) }
onMounted(() => { lastRecordId = store.recordId; load() })
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); if (lastRecordId === store.recordId) emit('enabled', false) })
defineExpose({ submit, canSubmit })
</script>
<template>
  <section v-if="overview?.enabled || error" class="lab-panel" aria-label="实验训练与评测" :aria-busy="loading">
    <div v-if="error" class="lab-error" role="alert">{{ error }} <button class="btn btn-ghost btn-sm" @click="polls = 0; load()">重试加载</button></div>
    <template v-if="overview?.enabled">
      <div class="lab-heading"><h2>训练与评测</h2><span class="lab-pill">{{ taskLabels[config.task_type] }} · 第 {{ overview.assessment.version }} 版</span></div>
      <p v-if="config.requirements">{{ config.requirements }}</p>
      <details><summary>公开评分标准</summary>
        <p v-if="config.task_type !== 'notebook'" class="lab-muted">{{ metricLabels[config.metric] }}：零分基线 {{ config.zero_baseline }}，满分目标 {{ config.full_target }}，区间内线性折算。</p>
        <div v-for="criterion in config.criteria || []" :key="criterion.id" class="lab-record"><strong>{{ criterion.name }} · {{ criterion.max_score }} 分</strong><p>{{ criterion.description }}</p><ul v-if="criterion.levels?.length"><li v-for="level in criterion.levels" :key="level.id">{{ level.points }} 分：{{ level.description }}</li></ul></div>
      </details>
      <details v-if="overview.dataset"><summary>教学数据：{{ overview.dataset.name }}</summary><p class="lab-muted">训练数据位于 /course/train，公开验证数据位于 /course/validation；各目录包含 samples.csv 与 labels.csv，通过 sample_id 关联。</p><ul class="lab-files"><li v-for="file in overview.dataset.files || []" :key="file.id"><button class="btn btn-secondary btn-sm" :disabled="busy" @click="download(file)">{{ file.path }}</button></li></ul></details>
      <div v-if="config.task_type !== 'notebook'">
        <div class="lab-actions">
          <label v-if="config.gpu_environment_version_id" class="lab-field">训练设备<select v-model="device" :disabled="busy"><option value="cpu">CPU</option><option value="gpu">GPU</option></select></label>
          <button class="btn btn-primary" :disabled="busy || activeTraining || store.conflict" @click="train">{{ activeTraining ? '已有训练任务进行中' : '保存并开始训练' }}</button>
          <span class="lab-muted">后台从头运行 Notebook，可关闭页面后回来查看。</span>
        </div>
        <details v-if="config.allow_external && !config.require_platform_training"><summary>上传外部训练成果</summary><p class="lab-muted">上传 ZIP，包含训练代码或 Notebook、predict.py、模型及预处理文件。入口参数为 --input 样本清单 --model-dir 模型目录 --output 预测文件 --device cpu；输出 sample_id,prediction CSV。上传成果须与本次 Notebook 对应，外部日志会标记为学生提供。</p><label class="lab-field">成果 ZIP<input type="file" accept=".zip" :disabled="busy" @change="upload" /></label></details>
        <label class="lab-field">选择用于评测的模型成果<select v-model="selectedArtifact" :disabled="busy"><option :value="null">请先完成训练或上传成果</option><option v-for="a in artifacts" :key="a.id" :value="a.id" :disabled="a.status !== 'ready' || a.compatible === false">成果 #{{ a.id }} · {{ a.source === 'external' ? '外部上传' : '平台训练' }} · {{ a.compatible === false ? '旧评分版本，请重新训练或上传' : statusLabel(a.status) }}</option></select></label>
        <template v-for="a in artifacts.filter(a => a.id === selectedArtifact)" :key="a.id"><p v-if="a.error" class="lab-error">{{ a.error }}</p><p v-if="a.source === 'external'" class="lab-muted">外部训练日志为学生提供；成绩以平台独立评测为准。</p><ul class="lab-files"><li v-for="file in a.files || []" :key="file.id"><button class="btn btn-ghost btn-sm" :disabled="busy" @click="download(file)">{{ file.path }}</button></li></ul><button v-if="a.files_truncated" type="button" class="btn btn-ghost btn-sm" :disabled="busy" @click="moreFiles(a)">加载更多文件（{{ a.files.length }}/{{ a.file_count }}）</button></template>
        <details v-if="artifacts.length"><summary>管理成果文件（{{ artifacts.length }}）</summary><p class="lab-muted">可删除不再使用的成果释放空间。已被正式提交、发布参考或运行任务引用的成果会保留。</p><div v-for="artifact in artifacts" :key="artifact.id" class="lab-record"><div class="lab-heading"><span>成果 #{{ artifact.id }} · {{ artifact.source === 'external' ? '外部上传' : '平台训练' }} · {{ statusLabel(artifact.status) }}</span><button class="btn btn-ghost btn-sm" type="button" :disabled="busy || isActive(artifact.status)" @click="deleteTarget = artifact">删除成果</button></div><p v-if="artifact.error" class="lab-error">{{ artifact.error }}</p></div></details>
        <button class="btn btn-secondary" :disabled="busy || activeValidation || !artifacts.some(a => a.id === selectedArtifact && a.status === 'ready' && a.compatible !== false)" @click="validate">{{ activeValidation ? '公开验证进行中…' : '公开验证（不占提交次数）' }}</button>
      </div>
      <div class="lab-actions"><button class="btn btn-primary" :disabled="!canSubmit" @click="submit">{{ store.submitting ? '提交中…' : activeEvaluation ? '正在评测上次提交…' : '保存并正式提交' }}</button><span>已用 {{ overview.attempts_used }} / {{ overview.max_submissions }} 次 · {{ config.grade_selection === 'latest' ? '取最后一次成绩' : '取同标准最高分' }}</span><strong v-if="overview.score != null">当前成绩 {{ overview.score }} 分</strong></div>
      <LabRunComparison :runs="overview.runs || []" />
      <LabLeaderboard :record-id="store.recordId" />
      <details v-if="overview.runs?.length"><summary>训练与评测记录（{{ overview.runs.length }}）</summary><p class="lab-muted">可比较各次任务的结果、损失曲线与日志。修改代码后请重新训练并选择对应成果。</p><LabRunCard v-for="run in overview.runs" :key="run.id" :run="run" cancellable @cancel="cancel" /></details>
      <details v-if="overview.submissions?.length" open><summary>提交与反馈</summary><div class="lab-actions"><button v-for="s in overview.submissions" :key="s.id" class="btn btn-secondary btn-sm" :disabled="busy" @click="showSubmission(s.id)">第 {{ s.attempt_number }} 次 · {{ (s.grade?.total ?? s.score) == null ? '查看进度' : `${s.grade?.total ?? s.score} 分` }}</button></div>
        <LabSubmissionFeedback v-if="detailId" :key="detailId" :submission-id="detailId" />
      </details>
      <button class="btn btn-ghost btn-sm" :disabled="busy" @click="polls = 0; load()">刷新任务与成绩</button>
    </template>
    <ConfirmDialog v-if="deleteTarget" title="删除实验成果" :message="`删除成果 #${deleteTarget.id} 的模型、代码与日志？删除后无法恢复，已被提交或任务引用的成果无法删除。`" confirm-text="删除成果" danger :busy="busy" @cancel="deleteTarget = null" @confirm="deleteArtifact" />
  </section>
</template>
