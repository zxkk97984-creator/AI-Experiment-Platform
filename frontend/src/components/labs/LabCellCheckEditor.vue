<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { errorMessage } from './labHelpers.js'
const config = defineModel({ type: Object, required: true })
const props = defineProps({ cells: { type: Array, default: () => [] }, kind: { type: String, required: true }, entryId: { type: [Number, String], required: true }, offeringId: { type: [Number, String], default: null } })
const error = ref('')
const uploading = ref(false)
let uploadGeneration = 0
let disposed = false
watch(() => [props.kind, props.entryId, props.offeringId, config.value], () => {
  uploadGeneration += 1
  uploading.value = false
  error.value = ''
}, { flush: 'sync' })
onBeforeUnmount(() => { disposed = true; uploadGeneration += 1 })
const codeCells = computed(() => props.cells.filter(c => (c.type || c.cell_type) === 'code' && !c.source_hidden))
const labels = { numeric_range: '数值区间', numeric_close: '数值近似相等', stdout: '输出字符串', shape: '数组 / 表格形状', python: '自定义 Python' }
watch(() => config.value.criteria.filter(c => c.source === 'cell').map(c => c.id), ids => {
  const retained = (config.value.cell_checks || []).filter(c => ids.includes(c.id))
  for (const id of ids) if (!retained.some(c => c.id === id)) retained.push({ id, cell_id: codeCells.value[0]?.id || '', kind: 'stdout', variable: 'result', minimum: 0, maximum: 1, expected_value: 0, tolerance: 0.000001, expected_output: '', stdout_mode: 'exact', shape: [1], script: '' })
  config.value.cell_checks = retained
  if (ids.length && config.value.task_type !== 'notebook') { config.value.require_platform_training = true; config.value.allow_external = false }
}, { immediate: true })
function addPreset(kind) {
  const id = `cell_${crypto.randomUUID().slice(0, 8)}`
  config.value.criteria.push({ id, name: labels[kind], description: '完成指定单元格的计算与输出要求', source: 'cell', max_score: 10, levels: [] })
  config.value.cell_checks.push({ id, cell_id: codeCells.value[0]?.id || '', kind, variable: 'result', minimum: 0, maximum: 1, expected_value: 0, tolerance: 0.000001, expected_output: '', stdout_mode: 'exact', shape: [1], script: '' })
}
function setShape(rule, text) { rule.shape = text.split(',').map(v => Number(v.trim())) }
async function upload(event, rule) {
  const file = event.target.files?.[0]
  if (!file) return
  event.target.value = ''
  const generation = ++uploadGeneration
  const originalConfig = config.value
  const originalScript = rule.script
  const isCurrent = () => !disposed && generation === uploadGeneration && config.value === originalConfig
    && config.value.cell_checks.includes(rule) && rule.kind === 'python' && rule.script === originalScript
  uploading.value = true
  error.value = ''
  try {
    const { data } = await labsAPI.uploadCheckScript(props.kind, props.entryId, file, props.offeringId)
    if (isCurrent()) rule.script = data.script
  } catch (e) {
    if (isCurrent()) error.value = errorMessage(e, '检查脚本上传失败')
  } finally {
    if (!disposed && generation === uploadGeneration) uploading.value = false
  }
}
</script>
<template>
  <section aria-label="单元格检查设置">
    <h3>单元格评分点</h3>
    <p class="lab-muted">选择稳定单元格编号；模板发布后规则冻结。期望值和 Python 脚本仅教师可见。添加后请调整评分项，使合计为 100 分。</p>
    <p v-if="config.task_type !== 'notebook' && config.cell_checks.length" class="lab-notice">模型实验包含单元格评分时，需在平台训练并提交完整 Notebook。外部模型上传将停用。</p>
    <div class="lab-actions"><button v-for="(label, kindKey) in labels" :key="kindKey" type="button" class="btn btn-secondary btn-sm" :disabled="!codeCells.length" @click="addPreset(kindKey)">添加{{ label }}</button></div>
    <p v-if="error" role="alert" class="lab-error">{{ error }}</p>
    <fieldset v-for="rule in config.cell_checks" :key="rule.id" class="lab-record">
      <legend>{{ config.criteria.find(c => c.id === rule.id)?.name || rule.id }}</legend>
      <div class="lab-grid">
        <label class="lab-field">评分单元格<select v-model="rule.cell_id"><option value="">选择代码单元格</option><option v-for="(cell, index) in codeCells" :key="cell.id" :value="cell.id">{{ index + 1 }} · {{ cell.id }}</option></select></label>
        <label class="lab-field">检查模板<select v-model="rule.kind"><option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option></select></label>
      </div>
      <label v-if="['numeric_range', 'numeric_close', 'shape'].includes(rule.kind)" class="lab-field">变量名称<input v-model="rule.variable" pattern="[a-zA-Z_][a-zA-Z0-9_]*" maxlength="100" placeholder="例如 result" /></label>
      <div v-if="rule.kind === 'numeric_range'" class="lab-grid"><label class="lab-field">最小值<input v-model.number="rule.minimum" type="number" step="any" /></label><label class="lab-field">最大值<input v-model.number="rule.maximum" type="number" step="any" /></label></div>
      <div v-if="rule.kind === 'numeric_close'" class="lab-grid"><label class="lab-field">期望数值<input v-model.number="rule.expected_value" type="number" step="any" /></label><label class="lab-field">绝对容差<input v-model.number="rule.tolerance" type="number" min="0" step="any" /></label></div>
      <template v-if="rule.kind === 'stdout'"><label class="lab-field">匹配方式<select v-model="rule.stdout_mode"><option value="exact">完整输出一致（忽略末尾换行）</option><option value="contains">包含指定文本</option></select></label><label class="lab-field">期望输出<textarea v-model="rule.expected_output" rows="3" maxlength="10000" /></label></template>
      <label v-if="rule.kind === 'shape'" class="lab-field">期望形状（逗号分隔）<input :value="rule.shape.join(',')" placeholder="例如 100,4" @input="setShape(rule, $event.target.value)" /></label>
      <template v-if="rule.kind === 'python'"><p class="lab-muted">脚本接收 --evidence-dir 与 --output-dir，从 evidence.json 读取 cells、cell_id、observations。须在输出目录生成 checks.json：{"{{ rule.id }}":{"passed":true,"message":"说明"}}。脚本在独立隔离环境执行。</p><label class="lab-field">上传 .py<input type="file" accept=".py" :disabled="uploading" @change="upload($event, rule)" /></label><p class="lab-muted">上传仅填入当前草稿，请保存配置后试评并发布。</p><label class="lab-field">Python 脚本<textarea v-model="rule.script" rows="7" maxlength="20000" /></label></template>
    </fieldset>
  </section>
</template>
