<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  questionType: { type: String, required: true },
  modelValue: { type: Object, required: true },
})
const emit = defineEmits(['update:modelValue'])

const importOpen = ref(false)
const importText = ref('')
const importError = ref('')

const rows = computed(() => props.modelValue.options || [])
const scoringMode = computed({
  get: () => props.modelValue.scoring_mode || 'all_or_nothing',
  set: (value) => update({ scoring_mode: value }),
})

function clone(value) { return JSON.parse(JSON.stringify(value)) }
function update(patch = {}) { emit('update:modelValue', { ...clone(props.modelValue), ...patch }) }

function alphaLabel(index) {
  let n = index + 1
  let label = ''
  while (n > 0) {
    n--
    label = String.fromCharCode(65 + (n % 26)) + label
    n = Math.floor(n / 26)
  }
  return label
}

function nextLabel() {
  const used = new Set(rows.value.map((row) => String(row.key).trim()))
  let index = rows.value.length
  while (used.has(alphaLabel(index))) index++
  return alphaLabel(index)
}

function addOption() {
  update({ options: [...clone(rows.value), { key: nextLabel(), text: '', correct: false }] })
}

function removeOption(index) {
  if (rows.value.length <= 2) return
  const options = clone(rows.value)
  options.splice(index, 1)
  update({ options })
}

function updateRow(index, field, value) {
  const options = clone(rows.value)
  options[index][field] = value
  update({ options })
}

function setCorrect(index, checked) {
  const options = clone(rows.value)
  if (props.questionType === 'single_choice') {
    options.forEach((row, i) => { row.correct = i === index })
  } else {
    options[index].correct = checked
  }
  update({ options })
}

watch(() => props.questionType, (type) => {
  if (type === 'single_choice') {
    const options = clone(rows.value)
    let found = false
    options.forEach((row) => {
      row.correct = Boolean(row.correct && !found)
      if (row.correct) found = true
    })
    emit('update:modelValue', { options, scoring_mode: 'all_or_nothing' })
  }
})

function validate(value = props.modelValue) {
  const options = value.options || []
  if (options.length < 2) return '至少需要两个选项'
  const keys = options.map((row) => String(row.key || '').trim())
  if (keys.some((key) => !key)) return '选项标识不能为空'
  if (new Set(keys).size !== keys.length) return '选项标识不能重复'
  if (options.some((row) => !String(row.text || '').trim())) return '选项内容不能为空'
  const correctCount = options.filter((row) => row.correct).length
  if (props.questionType === 'single_choice' && correctCount !== 1) return '单选题必须设置一个正确答案'
  if (props.questionType === 'multi_choice' && correctCount < 1) return '多选题至少需要一个正确答案'
  return ''
}

function doImport() {
  importError.value = ''
  try {
    const parsed = JSON.parse(importText.value)
    const source = parsed?.options
    if (!source || typeof source !== 'object' || Array.isArray(source)) throw new Error('options 必须是对象')
    const correct = parsed.correct ?? parsed.correct_answer?.correct
    if (!Array.isArray(correct)) throw new Error('correct 必须是数组')
    const mode = parsed.scoring_mode ?? parsed.correct_answer?.scoring_mode ?? 'all_or_nothing'
    if (!['all_or_nothing', 'partial_no_wrong'].includes(mode)) throw new Error('scoring_mode 不受支持')
    const options = Object.entries(source).map(([key, text]) => ({ key, text: String(text ?? ''), correct: correct.includes(key) }))
    const candidate = { options, scoring_mode: props.questionType === 'single_choice' ? 'all_or_nothing' : mode }
    const error = validate(candidate)
    if (error) throw new Error(error)
    emit('update:modelValue', candidate)
    importOpen.value = false
    importText.value = ''
  } catch (error) {
    importError.value = error.message || 'JSON 格式不正确'
  }
}

defineExpose({ validate })
</script>

<template>
  <section class="choice-editor">
    <div class="choice-head">
      <div>
        <h4>选项与正确答案</h4>
        <p>选项数量不限，标识可自定义；点击答案控件即可设置正确项。</p>
      </div>
      <button type="button" class="btn-ghost btn-sm" @click="addOption">＋ 添加选项</button>
    </div>

    <div class="option-list">
      <div v-for="(row, index) in rows" :key="index" class="option-row">
        <input
          :type="questionType === 'single_choice' ? 'radio' : 'checkbox'"
          :name="questionType === 'single_choice' ? 'exam-correct-option' : undefined"
          :checked="row.correct"
          :aria-label="`设为正确答案 ${row.key || index + 1}`"
          @change="setCorrect(index, $event.target.checked)"
        />
        <input class="option-key" :value="row.key" maxlength="20" aria-label="选项标识" @input="updateRow(index, 'key', $event.target.value)" />
        <input class="option-text" :value="row.text" placeholder="输入选项内容" aria-label="选项内容" @input="updateRow(index, 'text', $event.target.value)" />
        <span v-if="row.correct" class="correct-tag">正确答案</span>
        <button type="button" class="remove-option" :disabled="rows.length <= 2" @click="removeOption(index)">删除</button>
      </div>
    </div>

    <div v-if="questionType === 'multi_choice'" class="scoring-box">
      <label>多选题计分方式</label>
      <DsSelect v-model="scoringMode">
        <option value="all_or_nothing">完全匹配才得分</option>
        <option value="partial_no_wrong">无错选时按正确项比例得分</option>
      </DsSelect>
      <p v-if="scoringMode === 'partial_no_wrong'">学生只要选择任一错误项，本题计 0 分；没有错选时按选对比例计分。</p>
    </div>

    <div class="json-import">
      <button type="button" class="json-toggle" @click="importOpen = !importOpen">
        {{ importOpen ? '收起 JSON 导入' : '高级：从 JSON 导入' }}
      </button>
      <div v-if="importOpen" class="json-panel">
        <p>支持新格式，也兼容旧版 <code>correct_answer.correct</code>：</p>
        <pre>{
  "options": { "A": "选项一", "B": "选项二", "C": "选项三" },
  "correct": ["A", "C"],
  "scoring_mode": "partial_no_wrong"
}</pre>
        <textarea v-model="importText" rows="7" spellcheck="false" placeholder="在此粘贴 JSON"></textarea>
        <p v-if="importError" class="import-error" role="alert">{{ importError }}</p>
        <div class="import-actions">
          <button type="button" class="btn-ghost btn-sm" @click="importOpen = false; importError = ''">取消</button>
          <button type="button" class="btn-accent btn-sm" @click="doImport">识别并导入</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.choice-editor { margin-top: 20px; }
.choice-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:12px; }
.choice-head h4 { margin:0 0 4px; font-size:15px; color:var(--fg); }
.choice-head p, .scoring-box p, .json-panel p { margin:0; color:var(--muted); font-size:12px; line-height:1.5; }
.option-list { display:flex; flex-direction:column; gap:8px; }
.option-row { display:grid; grid-template-columns:24px 76px minmax(180px,1fr) auto auto; gap:8px; align-items:center; padding:10px 12px; border:1px solid var(--border); border-radius: var(--radius-md); background:var(--surface); }
.option-row input[type="radio"], .option-row input[type="checkbox"] { width:17px; height:17px; accent-color:var(--accent); }
.option-key, .option-text { width:100%; padding:9px 10px; border:1px solid var(--border); border-radius: var(--radius-md); background:var(--surface); color:var(--fg); }
.option-key { text-align:center; font-weight:700; }
.correct-tag { color:var(--success); background:var(--success-bg); border-radius: var(--radius-full); padding:3px 8px; font-size:11px; white-space:nowrap; }
.remove-option { border:0; background:none; color:var(--danger); cursor:pointer; }
.remove-option:disabled { color:var(--faint); cursor:not-allowed; }
.scoring-box { margin-top:14px; padding:14px; border-radius: var(--radius-md); background:var(--surface-sunken); }
.scoring-box label { display:block; font-size:13px; font-weight:600; margin-bottom:6px; }
.scoring-box .ds-select { width:100%; margin-bottom:6px; }
.json-import { margin-top:22px; padding-top:16px; border-top:1px dashed var(--border); }
.json-toggle { border:0; background:none; color:var(--accent); padding:0; cursor:pointer; font-weight:600; }
.json-panel { margin-top:10px; padding:14px; border:1px solid var(--border); border-radius: var(--radius-md); background:var(--surface-sunken); }
.json-panel pre { white-space:pre-wrap; margin:8px 0; padding:10px; border-radius: var(--radius-md); background:var(--fg); color:var(--border); font-size:12px; }
.json-panel textarea { width:100%; font-family:var(--font-mono); font-size:12px; }
.import-error { color:var(--danger)!important; margin-top:6px!important; }
.import-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:8px; }
@media (max-width: 720px) {
  .option-row { grid-template-columns:24px 62px 1fr auto; }
  .correct-tag { display:none; }
  .choice-head { flex-direction:column; }
}
</style>
