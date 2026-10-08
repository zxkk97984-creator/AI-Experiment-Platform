<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { useAppStore } from '../../stores/app.js'
import EnvironmentProfilePicker from '../common/EnvironmentProfilePicker.vue'
import ConfirmDialog from '../ui/ConfirmDialog.vue'
import LabDatasetPicker from './LabDatasetPicker.vue'
import LabRubricEditor from './LabRubricEditor.vue'
import LabCellCheckEditor from './LabCellCheckEditor.vue'
import LabRunCard from './LabRunCard.vue'
import LabRegradeBatch from './LabRegradeBatch.vue'
import { builtinCriteria, clone, defaultConfig, errorMessage, isActive, rubricErrors, taskLabels } from './labHelpers.js'
import './labs.css'
const props = defineProps({
  kind: { type: String, required: true }, entryId: { type: [Number, String], required: true },
  offeringId: { type: [Number, String], default: null },
  environmentId: { type: Number, default: null }, cells: { type: Array, default: () => [] },
  saveTemplate: { type: Function, required: true }, templateDirty: Boolean,
  templateSignature: { type: String, default: '' }, focusPublishing: { type: Number, default: 0 },
})
const entryArgs = () => props.offeringId ? [props.kind, props.entryId, props.offeringId] : [props.kind, props.entryId]
const emit = defineEmits(['dirty', 'import-starter', 'configured', 'environment'])
const app = useAppStore()
const config = ref(defaultConfig())
const assessment = ref(null)
const published = ref(null)
const baseline = ref(null)
const referenceRun = ref(null)
let referenceTimer
let referencePolls = 0
const loading = ref(true)
const loaded = ref(false)
const busy = ref(false)
const error = ref('')
const savedConfig = ref('')
const savedTemplateSignature = ref('')
const showImport = ref(false)
const publishingSection = ref(null)
watch(() => [props.focusPublishing, loading.value], async ([signal, loading]) => {
  if (!signal || loading) return
  await nextTick()
  if (publishingSection.value) { publishingSection.value.open = true; publishingSection.value.scrollIntoView?.({ block: 'start', behavior: 'smooth' }) }
})
const dirty = computed(() => savedConfig.value !== JSON.stringify(config.value))
const errors = computed(() => rubricErrors(config.value))
const validated = computed(() => assessment.value?.validated && !dirty.value && !props.templateDirty && props.templateSignature === savedTemplateSignature.value)
let timer
let disposed = false
let polls = 0
watch(dirty, value => emit('dirty', value))
async function load() {
  loading.value = true
  loaded.value = false
  error.value = ''
  try {
    const { data } = await labsAPI.getAssessment(...entryArgs())
    if (disposed) return
    published.value = data.published
    assessment.value = data.draft || data.published
    emit('configured', Boolean(assessment.value))
    config.value = assessment.value ? { ...defaultConfig(), ...clone(assessment.value.config) } : { ...defaultConfig(), environment_version_id: props.environmentId }
    savedConfig.value = JSON.stringify(config.value)
    savedTemplateSignature.value = props.templateSignature
    loaded.value = true
    if (assessment.value?.validation_run_id) {
      baseline.value = (await labsAPI.getRun(assessment.value.validation_run_id)).data
      if (isActive(baseline.value.status)) { polls = 0; schedulePoll() }
    }
  } catch (e) { error.value = errorMessage(e, '实验评测设置加载失败') }
  finally { loading.value = false }
}
function schedulePoll() {
  clearTimeout(timer)
  if (!disposed && polls++ < 720) timer = setTimeout(pollBaseline, 5000)
}
async function pollBaseline() {
  if (disposed || !baseline.value) return
  try {
    baseline.value = (await labsAPI.getRun(baseline.value.id)).data
    if (disposed) return
    if (isActive(baseline.value.status)) schedulePoll()
    else {
      const { data } = await labsAPI.getAssessment(...entryArgs())
      assessment.value = data.draft || data.published
      published.value = data.published
    }
  } catch (e) { error.value = errorMessage(e, '参考试跑状态加载失败，请手动刷新') }
}
async function action(fn) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try { await fn() }
  catch (e) { error.value = errorMessage(e); app.showToast(error.value, 'error') }
  finally { busy.value = false }
}
async function save() {
  if (errors.value.length) throw new Error(errors.value[0])
  const ok = await props.saveTemplate()
  if (ok === false) throw new Error('请先解决 Notebook 保存冲突')
  assessment.value = (await labsAPI.saveAssessment(props.kind, props.entryId, config.value, ...(props.offeringId ? [props.offeringId] : []))).data
  config.value = clone(assessment.value.config)
  emit('configured', true)
  savedConfig.value = JSON.stringify(config.value)
  savedTemplateSignature.value = props.templateSignature
}
function changeTask() {
  config.value.criteria = builtinCriteria(config.value.task_type)
  config.value.dataset_id = null
  config.value.reference_artifact_id = null
  config.value.reference_cells = []
  config.value.metric = config.value.task_type === 'tabular_regression' ? 'rmse' : 'macro_f1'
  config.value.zero_baseline = config.value.task_type === 'tabular_regression' ? 1 : 0
  config.value.full_target = config.value.task_type === 'tabular_regression' ? 0 : 1
  config.value.confirmed = false
}
async function generate() {
  await action(async () => {
    const { data } = await labsAPI.generateRubric(props.kind, props.entryId, { ...config.value, criteria: [] })
    config.value.criteria = data.criteria
    config.value.confirmed = false
    app.showToast('量表已生成，请逐项检查并编辑判据', 'success')
  })
}
async function runBaseline() {
  await action(async () => {
    await save()
    baseline.value = (await labsAPI.baseline(...entryArgs())).data
    polls = 0
    schedulePoll()
    app.showToast('参考试跑已加入队列', 'success')
  })
}
async function publish() {
  await action(async () => {
    if (dirty.value) await save()
    assessment.value = (await labsAPI.publish(...entryArgs())).data
    published.value = assessment.value
    savedConfig.value = JSON.stringify(config.value)
    app.showToast(`实验评测第 ${assessment.value.version} 版已发布`, 'success')
  })
}
async function pollReference() {
  clearTimeout(referenceTimer)
  if (disposed || !referenceRun.value) return
  try {
    referenceRun.value = (await labsAPI.getRun(referenceRun.value.id)).data
    if (!disposed && isActive(referenceRun.value.status) && referencePolls++ < 360) referenceTimer = setTimeout(pollReference, 5000)
  } catch (e) { error.value = errorMessage(e, '参考成果校验状态加载失败') }
}

async function uploadReference(event) {
  const file = event.target.files?.[0]
  if (!file) return
  await action(async () => {
    await save()
    const { data } = await labsAPI.uploadReference(props.kind, props.entryId, file)
    config.value.reference_artifact_id = data.id
    if (data.validation_run_id) {
      referenceRun.value = { id: data.validation_run_id, kind: 'artifact_validate', status: 'queued' }
      referencePolls = 0
      await pollReference()
    }
    app.showToast('参考成果已上传，校验通过后可试跑', 'success')
  })
  event.target.value = ''
}
async function importStarter() {
  await action(async () => {
    const { data } = await labsAPI.starter(config.value.task_type)
    const environment = config.value.environment_version_id
    config.value = { ...defaultConfig(config.value.task_type), ...data.config, environment_version_id: environment }
    emit('import-starter', data.cells)
    showImport.value = false
    app.showToast('教学模板已载入，请完善任务标准并保存', 'success')
  })
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); clearTimeout(referenceTimer) })
</script>
<template>
  <section class="lab-panel" aria-label="实验备课与评测设置">
    <div class="lab-heading"><h2>实验备课与评测</h2><span v-if="published" class="lab-pill">已发布第 {{ published.version }} 版</span></div>
    <p v-if="offeringId" class="lab-notice">本班评分配置；Notebook、软件环境及数据沿用已发布的共享版本。调整评分后请试评并发布，历史成绩通过整组重评更新。</p>
    <p class="lab-muted">准备教学数据与评分标准，参考试跑通过后启用自动评测。未启用的实验继续使用人工评分。</p>
    <p v-if="loading" role="status">正在加载实验设置…</p>
    <div v-if="error" role="alert" class="lab-error">{{ error }} <button v-if="!assessment" class="btn btn-ghost btn-sm" @click="load">重新加载</button></div>
    <form v-if="!loading && loaded" @submit.prevent="action(async () => { await save(); app.showToast('实验配置已保存', 'success') })">
      <fieldset :disabled="busy" class="lab-form">
        <details open><summary>1. 教学任务与数据</summary>
          <div class="lab-grid">
            <label class="lab-field">实验类型<select v-model="config.task_type" :disabled="Boolean(offeringId)" @change="changeTask"><option v-for="(label, key) in taskLabels" :key="key" :value="key">{{ label }}</option></select></label>
            <label class="lab-field">学习方式<select v-model="config.teaching_mode"><option value="guided">引导式：提供待完成代码</option><option value="open">开放式：学生自行设计</option></select></label>
          </div>
          <label class="lab-field">任务与预期结果<textarea v-model="config.requirements" rows="4" maxlength="12000" placeholder="例如：实现 CNN，对比两种学习率，解释训练集与验证集表现的差异。说明允许使用的算法、禁止事项及要回答的问题。" /></label>
          <LabDatasetPicker v-if="!offeringId && config.task_type !== 'notebook'" v-model="config.dataset_id" :task-type="config.task_type" />
          <button v-if="!offeringId" class="btn btn-secondary btn-sm" type="button" @click="showImport = true">使用内置教学模板</button>
          <p class="lab-muted">在 Notebook 的 Markdown 内容块开启「学生填写」，即可作为实验报告区域。</p>
        </details>
        <details><summary>2. 评分标准</summary>
          <div class="lab-actions"><button class="btn btn-secondary btn-sm" type="button" :disabled="Boolean(offeringId) || !config.requirements.trim()" @click="generate">AI 生成量表</button><button class="btn btn-ghost btn-sm" type="button" @click="config.criteria = builtinCriteria(config.task_type); config.confirmed = false">使用内置量表</button></div>
          <div v-if="config.task_type !== 'notebook'" class="lab-grid">
            <label class="lab-field">客观指标<select v-model="config.metric"><template v-if="config.task_type === 'tabular_regression'"><option value="rmse">RMSE</option><option value="mae">MAE</option></template><template v-else><option value="macro_f1">Macro-F1</option><option value="accuracy">准确率</option></template></select></label>
            <label class="lab-field">零分基线<input v-model.number="config.zero_baseline" type="number" step="any" required /></label>
            <label class="lab-field">满分目标<input v-model.number="config.full_target" type="number" step="any" required /></label>
          </div>
          <LabRubricEditor v-model="config.criteria" />
          <LabCellCheckEditor v-model="config" :cells="cells" :kind="kind" :entry-id="entryId" :offering-id="offeringId" />
          <div v-if="config.criteria.some(c => c.source === 'efficiency')" class="lab-record"><h3>程序运行效率</h3><p class="lab-muted">统一 CPU 环境，三次干净重放或整批推理取中位数；排队、容器准备和文件复制不计时。排行榜只展示，名次不参与评分。</p><div class="lab-grid"><label class="lab-field">满分耗时（秒）<input v-model.number="config.efficiency_full_seconds" type="number" min="0.001" step="any" /></label><label class="lab-field">零分耗时（秒）<input v-model.number="config.efficiency_zero_seconds" type="number" min="0.001" step="any" /></label></div></div>
          <ul v-if="errors.length" class="lab-error"><li v-for="message in errors" :key="message">{{ message }}</li></ul>
          <label class="lab-check"><input v-model="config.confirmed" type="checkbox" />我已确认评分要求、等级和分值，可向学生公开</label>
        </details>
        <details><summary>3. 环境、资源与提交规则</summary>
          <EnvironmentProfilePicker v-if="!offeringId" v-model="config.environment_version_id" label="Notebook、CPU 训练与评测环境" show-memory @update:model-value="emit('environment', $event)" />
          <div class="lab-grid">
            <label class="lab-field">正式提交次数<input v-model.number="config.max_submissions" type="number" min="1" max="100" required /></label>
            <label class="lab-field">最终成绩<select v-model="config.grade_selection"><option value="highest">取同一标准下的最高分</option><option value="latest">取最后一次成绩</option></select></label>
          </div>
          <label class="lab-check"><input v-model="config.require_platform_training" type="checkbox" @change="config.require_platform_training && (config.allow_external = false)" />必须在平台完成训练</label>
          <label class="lab-check"><input v-model="config.allow_external" type="checkbox" :disabled="config.require_platform_training" />允许上传外部训练成果</label>
          <h3>后台训练预算</h3>
          <div class="lab-grid">
            <label class="lab-field">CPU 核数<input v-model.number="config.resources.cpus" type="number" min="0.25" max="64" step="0.25" /></label>
            <label class="lab-field">内存（MiB）<input v-model.number="config.resources.memory_mb" type="number" min="256" max="262144" /></label>
            <label class="lab-field">时间上限（秒）<input v-model.number="config.resources.timeout_seconds" type="number" min="10" max="86400" /></label>
            <label class="lab-field">成果空间（MiB）<input v-model.number="config.resources.disk_mb" type="number" min="64" max="102400" /></label>
          </div>
          <h3>独立评测预算</h3>
          <div class="lab-grid">
            <label class="lab-field">CPU 核数<input v-model.number="config.evaluation_resources.cpus" type="number" min="0.25" max="64" step="0.25" /></label>
            <label class="lab-field">内存（MiB）<input v-model.number="config.evaluation_resources.memory_mb" type="number" min="256" max="262144" /></label>
            <label class="lab-field">时间上限（秒）<input v-model.number="config.evaluation_resources.timeout_seconds" type="number" min="10" max="86400" /></label>
            <label class="lab-field">空间（MiB）<input v-model.number="config.evaluation_resources.disk_mb" type="number" min="64" max="102400" /></label>
          </div>
          <details v-if="!offeringId"><summary>GPU 训练（选配）</summary><p class="lab-muted">需管理员启用 GPU 并验证环境；正式评分统一在 CPU 上进行。</p><EnvironmentProfilePicker v-model="config.gpu_environment_version_id" label="GPU 训练环境" show-memory /><button v-if="config.gpu_environment_version_id" class="btn btn-ghost btn-sm" type="button" @click="config.gpu_environment_version_id = null">停用本实验 GPU 训练</button></details>
        </details>
        <details ref="publishingSection"><summary>4. 参考试跑与发布</summary>
          <p class="lab-muted">提供完整参考实现，在 CPU 上检查数据读取、模型预测与评分。发布后冻结本版教学内容与标准。</p>
          <div class="lab-actions"><button class="btn btn-secondary btn-sm" type="button" @click="config.reference_cells = clone(cells)">以当前 Notebook 作为参考实现</button><span v-if="config.reference_cells.length">已设置 {{ config.reference_cells.length }} 个参考内容块</span></div>
          <label v-if="!offeringId && config.task_type !== 'notebook'" class="lab-field">上传参考成果 ZIP<input type="file" accept=".zip" @change="uploadReference" /></label>
          <div v-if="config.reference_artifact_id"><p class="lab-muted">参考成果 #{{ config.reference_artifact_id }}</p><LabRunCard v-if="referenceRun" :run="referenceRun" /><div class="lab-actions"><button v-if="referenceRun" class="btn btn-ghost btn-sm" type="button" @click="referencePolls = 0; pollReference()">刷新成果校验</button><button v-if="!offeringId" class="btn btn-ghost btn-sm" type="button" @click="config.reference_artifact_id = null; referenceRun = null">取消使用上传成果</button></div></div>
          <details><summary>高级：自定义检查脚本</summary><p class="lab-muted">脚本接收 --evidence-dir 和 --output-dir；从证据目录的 evidence.json 读取 cells 与 metrics。在输出目录生成 checks.json，内容为每个检查项编号对应 {"passed": true/false, "message": "检查说明"}，所有检查项必须有结果。</p><label class="lab-field">Python 检查脚本<textarea v-model="config.check_script" rows="8" maxlength="20000" placeholder="仅用于配置检查脚本评分项的教师" /></label></details>
          <div class="lab-actions"><button class="btn btn-secondary" type="button" :disabled="errors.length > 0 || !config.confirmed || isActive(baseline?.status) || (config.reference_artifact_id && referenceRun && referenceRun.status !== 'succeeded')" @click="runBaseline">保存并参考试跑</button><button class="btn btn-primary" type="button" :disabled="!validated || !config.confirmed" @click="publish">发布实验评测</button></div>
          <p v-if="!validated && assessment?.validated" class="lab-muted">配置有修改，保存后需要重新试跑。</p>
          <p v-if="validated" class="lab-notice">参考试跑通过，可发布本版标准。</p>
          <LabRunCard v-if="baseline" :run="baseline" />
          <button v-if="baseline" class="btn btn-ghost btn-sm" type="button" @click="pollBaseline">刷新试跑结果</button>
        </details>
        <div class="lab-actions"><button class="btn btn-primary" type="submit" :disabled="errors.length > 0">{{ busy ? '处理中…' : '保存实验配置' }}</button><span class="lab-muted">{{ dirty ? '有未保存的修改' : '配置已保存' }}</span></div>
      </fieldset>
    </form>
    <LabRegradeBatch v-if="published" :kind="kind" :entry-id="entryId" :version="published.version" :offering-id="offeringId" />
    <ConfirmDialog v-if="showImport" title="载入教学模板" message="将替换当前 Notebook 内容和评分草稿。已有内容可先导出备份。" confirm-text="载入模板" :busy="busy" @cancel="showImport = false" @confirm="importStarter" />
  </section>
</template>
<style scoped>
.lab-form { border: 0; padding: 0; margin: 0; min-width: 0; }
.lab-form:disabled { opacity: .7; }
</style>
