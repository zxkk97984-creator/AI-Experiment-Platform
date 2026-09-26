<script setup>
// QeTestCases：题目测试用例卡（公开样例表格 + 私有测试双模式）。
// 公开样例：表格化展示（列 = # / 各输入参数 / 期望输出 / 说明 / 操作），
//   支持行内编辑、添加、删除（两段式确认）、批量导入（粘贴 JSON 数组）、分页（10/20/50）。
// 私有测试：可视化测试（同表格模式，保存时自动生成 pytest 代码）与 pytest 高级模式（深色代码编辑器）。
// 数据契约不变：通过 update:publicCases / update:hiddenTests 事件同步给父组件，
//   父组件保存时 public_cases 仍为数组、hidden_tests 仍为 pytest 代码字符串。
import { computed, ref, watch } from 'vue'
import QeCodeEditor from './QeCodeEditor.vue'

const props = defineProps({
  /** 公开样例行（数组，元素 { args, expected, desc }），由父组件在加载/重置时更新 */
  publicCases: { type: Array, default: () => [] },
  /** 私有测试 pytest 代码字符串 */
  hiddenTests: { type: String, default: '' },
  /** 当前题目函数名（生成 pytest 时使用） */
  functionName: { type: String, default: 'solution' },
  /** 题目 id（新建题目为 null，运行测试按钮禁用） */
  questionId: { type: Number, default: null },
  /** 运行测试结果（父组件调用 sample-run 后传入） */
  runResult: { type: Object, default: null },
  /** 是否展示公开样例运行按钮（考试题暂不提供 sample-run 端点） */
  showRun: { type: Boolean, default: true },
})

const emit = defineEmits(['update:publicCases', 'update:hiddenTests', 'run-sample', 'parse-failed'])

// ── 通用工具：单元格值解析 / 格式化 / Python 字面量 ─────────────────
// 行内编辑输入的是 JSON 风格文本：'1' → 1、'"a"' → 'a'、'[1,2]' → [1,2]；
// Python 语义兼容：'None' → null、'True'/'False' → 布尔。
function parseCellValue(str) {
  const s = String(str).trim()
  if (s === '' || s === 'None') return null
  if (s === 'True') return true
  if (s === 'False') return false
  try { return JSON.parse(s) } catch { return s }
}

function formatValue(v) {
  if (v === null || v === undefined) return '—'
  return JSON.stringify(v)
}

// JSON 值 → Python 字面量（生成 pytest 用）
function pyRepr(v) {
  if (v === null || v === undefined) return 'None'
  if (typeof v === 'boolean') return v ? 'True' : 'False'
  if (typeof v === 'string') return JSON.stringify(v)
  if (Array.isArray(v)) return `[${v.map(pyRepr).join(', ')}]`
  if (typeof v === 'object') { try { return JSON.stringify(v) } catch { return '{}' } }
  return String(v)
}

// 行 key 生成（内部编辑定位用，提交时剔除）
let keySeq = 0
function nextKey() { return `k${++keySeq}` }

function stripMeta(row) {
  const { _key, _editingArgs, ...rest } = row
  void _key
  void _editingArgs
  return rest
}

// 可视化行 → pytest 代码（隐藏测试的存储形态）
function generatePytest(rows, fn) {
  const name = fn || 'solution'
  if (!rows.length) return ''
  const lines = ['# 私有测试（由「可视化测试」表格生成，可在 pytest 高级模式中修改）', '']
  rows.forEach((r, i) => {
    if (r.desc) lines.push(`# ${r.desc}`)
    const argsStr = (r.args || []).map(pyRepr).join(', ')
    lines.push(`def test_case_${i + 1}():`)
    lines.push(`    assert ${name}(${argsStr}) == ${pyRepr(r.expected)}`)
    lines.push('')
  })
  return lines.join('\n')
}

// pytest 代码 → 可视化行（尽力解析；含嵌套参数/复杂断言的行跳过）
function parsePytest(code, fn) {
  if (!code || !code.trim()) return { parsed: true, rows: [] }
  const name = fn || 'solution'
  const lines = code.split('\n')
  const rows = []
  let i = 0
  while (i < lines.length) {
    const m = /^\s*def\s+test_\w+\s*\(\s*\)\s*:/.exec(lines[i])
    if (m) {
      // 收集函数块内行
      const block = []
      let k = i + 1
      while (k < lines.length && /^\s+/.test(lines[k])) { block.push(lines[k].trim()); k++ }
      // 块内每个 `assert fn(args) == expected` 行都可解析为一个用例
      const assertRe = new RegExp(`^assert\\s+${name}\\s*\\((.*)\\)\\s*==\\s*(.+)$`)
      for (const line of block) {
        const am = assertRe.exec(line)
        if (!am) continue
        const argsStr = am[1].trim()
        const expStr = am[2].trim()
        // 简单参数才解析（含嵌套结构则跳过该行，保留给 pytest 模式）
        if (!argsStr || /[[{"']/.test(argsStr)) continue
        const args = argsStr.split(',').map((x) => x.trim()).map(parseCellValue)
        rows.push({ args, expected: parseCellValue(expStr), desc: '', _key: nextKey() })
      }
      i = k
    } else i++
  }
  return { parsed: true, rows }
}

// ── 公开样例 state ──────────────────────────────────────────────────
const tab = ref('public') // public | private（公开样例 / 私有测试）
const rows = ref([])
const page = ref(1)
const pageSize = ref(10)
const editingKey = ref(null)
const editingBuf = ref(null) // 编辑中的临时值 { args: [], expected: '', desc: '' }
const confirmDeleteKey = ref(null)
const importOpen = ref(false)
const importText = ref('')
const importError = ref('')

// 初始化：以父组件数据为唯一真源（父组件用 :key 重挂载来响应加载/重置）
rows.value = (props.publicCases || []).map((c) => ({
  _key: nextKey(),
  args: Array.isArray(c.args) ? [...c.args] : [],
  expected: c.expected ?? null,
  desc: c.desc || '',
}))

const pagedRows = computed(() => rows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const pageCount = computed(() => Math.max(1, Math.ceil(rows.value.length / pageSize.value)))
const argCols = computed(() => Math.max(1, ...pagedRows.value.map((r) => (r.args || []).length)))
const rangeStart = computed(() => (rows.value.length === 0 ? 0 : (page.value - 1) * pageSize.value + 1))
const rangeEnd = computed(() => Math.min(page.value * pageSize.value, rows.value.length))

watch(pageSize, () => { page.value = 1 })
watch(rows, (arr) => {
  if (arr.length === 0) page.value = 1
  else if (page.value > pageCount.value) page.value = pageCount.value
})

function emitPublic() {
  emit('update:publicCases', rows.value.map(stripMeta))
}

function startAdd() {
  const row = { _key: nextKey(), args: [], expected: null, desc: '' }
  rows.value.push(row)
  editingKey.value = row._key
  editingBuf.value = { args: [], expected: '', desc: '' }
  page.value = pageCount.value // 跳到末页，保证新行可见
  importOpen.value = false
}

function startEdit(row) {
  editingKey.value = row._key
  editingBuf.value = {
    args: (row.args || []).map(formatValue),
    expected: row.expected === null || row.expected === undefined ? '' : formatValue(row.expected),
    desc: row.desc || '',
  }
}

function cancelEdit() {
  // 新建的空白行在取消时删除
  const row = rows.value.find((r) => r._key === editingKey.value)
  if (row && row.args.length === 0 && row.expected === null && !row.desc) {
    rows.value = rows.value.filter((r) => r._key !== editingKey.value)
    if (rows.value.length === 0) page.value = 1
    emitPublic()
  }
  editingKey.value = null
  editingBuf.value = null
}

function addArgInput() { editingBuf.value.args.push('') }
function removeArgInput(idx) { editingBuf.value.args.splice(idx, 1) }

function saveEdit() {
  const row = rows.value.find((r) => r._key === editingKey.value)
  if (!row) return
  const buf = editingBuf.value
  // 空输入表示删除该参数；但显式空字符串（'""'）保留
  const kept = []
  buf.args.forEach((raw) => {
    const v = parseCellValue(raw)
    if (raw.trim() !== '' || v !== null) kept.push(v)
  })
  row.args = kept
  row.expected = parseCellValue(buf.expected)
  row.desc = buf.desc || ''
  editingKey.value = null
  editingBuf.value = null
  emitPublic()
}

function askDelete(key) {
  if (confirmDeleteKey.value === key) {
    rows.value = rows.value.filter((r) => r._key !== key)
    confirmDeleteKey.value = null
    if (rows.value.length === 0) page.value = 1
    emitPublic()
  } else {
    confirmDeleteKey.value = key
    setTimeout(() => { if (confirmDeleteKey.value === key) confirmDeleteKey.value = null }, 2500)
  }
}

function doImport() {
  let arr
  try { arr = JSON.parse(importText.value) } catch { importError.value = 'JSON 解析失败，请检查格式'; return }
  if (!Array.isArray(arr)) { importError.value = '必须是 JSON 数组，如 [{"args": [1, 2], "expected": 3}]'; return }
  const skipped = []
  let added = 0
  arr.forEach((item, i) => {
    if (item && typeof item === 'object' && Array.isArray(item.args)) {
      rows.value.push({ _key: nextKey(), args: [...item.args], expected: item.expected ?? null, desc: item.desc || '' })
      added++
    } else skipped.push(i + 1)
  })
  if (skipped.length) importError.value = `第 ${skipped.join('、')} 条缺少 args 数组，已跳过`
  if (added) {
    emitPublic()
    page.value = pageCount.value
    importOpen.value = false
    importText.value = ''
    if (!skipped.length) importError.value = ''
  }
}

// ── 私有测试 state（visual 表格 / pytest 代码双模式） ────────────────
const privateTab = ref('visual') // visual | pytest
const privateRows = ref([])
const privatePage = ref(1)
const privateEditingKey = ref(null)
const privateEditingBuf = ref(null)
const privateConfirmKey = ref(null)
const privatePytestText = ref(props.hiddenTests || '')

const privatePaged = computed(() => privateRows.value.slice((privatePage.value - 1) * pageSize.value, privatePage.value * pageSize.value))
const privatePageCount = computed(() => Math.max(1, Math.ceil(privateRows.value.length / pageSize.value)))
const privateArgCols = computed(() => Math.max(1, ...privatePaged.value.map((r) => (r.args || []).length)))
const privateRangeStart = computed(() => (privateRows.value.length === 0 ? 0 : (privatePage.value - 1) * pageSize.value + 1))
const privateRangeEnd = computed(() => Math.min(privatePage.value * pageSize.value, privateRows.value.length))

function emitHidden(text) {
  emit('update:hiddenTests', text)
}

// 挂载时：尝试把已有 pytest 解析为可视化行，失败则进入 pytest 模式保留原文
{
  const parsed = parsePytest(props.hiddenTests, props.functionName)
  if (parsed.rows.length) {
    privateRows.value = parsed.rows
    privateTab.value = 'visual'
  } else if (props.hiddenTests.trim()) {
    privateTab.value = 'pytest'
  }
}

function privateStartEdit(row) {
  privateEditingKey.value = row._key
  privateEditingBuf.value = {
    args: (row.args || []).map(formatValue),
    expected: row.expected === null || row.expected === undefined ? '' : formatValue(row.expected),
    desc: row.desc || '',
  }
}

function privateCancelEdit() {
  const row = privateRows.value.find((r) => r._key === privateEditingKey.value)
  if (row && row.args.length === 0 && row.expected === null && !row.desc) {
    privateRows.value = privateRows.value.filter((r) => r._key !== privateEditingKey.value)
    emitPrivate()
  }
  privateEditingKey.value = null
  privateEditingBuf.value = null
}

function privateAddArg() { privateEditingBuf.value.args.push('') }
function privateRemoveArg(idx) { privateEditingBuf.value.args.splice(idx, 1) }

function privateSaveEdit() {
  const row = privateRows.value.find((r) => r._key === privateEditingKey.value)
  if (!row) return
  const buf = privateEditingBuf.value
  const kept = []
  buf.args.forEach((raw) => {
    const v = parseCellValue(raw)
    if (raw.trim() !== '' || v !== null) kept.push(v)
  })
  row.args = kept
  row.expected = parseCellValue(buf.expected)
  row.desc = buf.desc || ''
  privateEditingKey.value = null
  privateEditingBuf.value = null
  emitPrivate()
}

function privateAskDelete(key) {
  if (privateConfirmKey.value === key) {
    privateRows.value = privateRows.value.filter((r) => r._key !== key)
    privateConfirmKey.value = null
    if (privateRows.value.length === 0) privatePage.value = 1
    else if (privatePage.value > privatePageCount.value) privatePage.value = privatePageCount.value
    emitPrivate()
  } else {
    privateConfirmKey.value = key
    setTimeout(() => { if (privateConfirmKey.value === key) privateConfirmKey.value = null }, 2500)
  }
}

function privateAddRow() {
  const row = { _key: nextKey(), args: [], expected: null, desc: '' }
  privateRows.value.push(row)
  privateEditingKey.value = row._key
  privateEditingBuf.value = { args: [], expected: '', desc: '' }
  privatePage.value = privatePageCount.value
}

function privateDoImport() {
  let arr
  try { arr = JSON.parse(privateImportText.value) } catch { privateImportError.value = 'JSON 解析失败，请检查格式'; return }
  if (!Array.isArray(arr)) { privateImportError.value = '必须是 JSON 数组，如 [{"args": [1, 2], "expected": 3}]'; return }
  const skipped = []
  let added = 0
  arr.forEach((item, i) => {
    if (item && typeof item === 'object' && Array.isArray(item.args)) {
      privateRows.value.push({ _key: nextKey(), args: [...item.args], expected: item.expected ?? null, desc: item.desc || '' })
      added++
    } else skipped.push(i + 1)
  })
  if (skipped.length) privateImportError.value = `第 ${skipped.join('、')} 条缺少 args 数组，已跳过`
  if (added) {
    emitPrivate()
    privatePage.value = privatePageCount.value
    privateImportOpen.value = false
    privateImportText.value = ''
    if (!skipped.length) privateImportError.value = ''
  }
}

function emitPrivate() {
  emit('update:hiddenTests', generatePytest(privateRows.value, props.functionName))
}

// 模式切换：visual → pytest 时用当前表格生成代码；pytest → visual 时尽力解析
function switchPrivateTab(tab) {
  if (tab === privateTab.value) return
  if (tab === 'pytest') {
    privatePytestText.value = generatePytest(privateRows.value, props.functionName)
    privateTab.value = tab
    emitHidden(privatePytestText.value)
  } else {
    const parsed = parsePytest(privatePytestText.value, props.functionName)
    if (parsed.rows.length || !privatePytestText.value.trim()) {
      privateRows.value = parsed.rows
      privateTab.value = tab
      emitHidden(generatePytest(parsed.rows, props.functionName))
    } else {
      // 无法解析：保留 pytest 模式，数据不动
      emit('parse-failed')
    }
  }
}

// pytest 模式内容变化 → 同步父组件
watch(privatePytestText, (v) => {
  if (privateTab.value === 'pytest') emitHidden(v)
})

// ── 运行测试结果文案 ────────────────────────────────────────────────
const runStatusText = computed(() => {
  const r = props.runResult
  if (!r) return ''
  switch (r.status) {
    case 'running': return '运行中…'
    case 'passed': return `✓ 全部通过${r.execution_time_ms ? `（${r.execution_time_ms}ms）` : ''}`
    case 'failed': return '✗ 测试失败'
    case 'no_public_cases': return '暂无公开样例，无法运行'
    case 'import_not_allowed': return `导入被拦截：${r.diagnostic?.message || '包含未允许的导入'}`
    case 'import_not_installed': return `环境未安装：${r.diagnostic?.message || ''}`
    case 'error': return r.message || '运行失败'
    default: return r.message || r.status || ''
  }
})

const runStatusClass = computed(() => props.runResult?.status || '')

// 私有导入面板 state
const privateImportOpen = ref(false)
const privateImportText = ref('')
const privateImportError = ref('')
</script>

<template>
  <div class="qe-cases">
    <!-- 顶部：标题 + tab -->
    <div class="qe-cases__tabs">
      <button
        type="button"
        class="qe-cases__tab"
        :class="{ active: tab === 'public' }"
        @click="tab = 'public'"
      >公开样例 <span class="qe-cases__count">{{ rows.length }}</span></button>
      <button
        type="button"
        class="qe-cases__tab"
        :class="{ active: tab === 'private' }"
        @click="tab = 'private'"
      >私有测试 🔒</button>
    </div>

    <!-- ═══ 公开样例 ═══ -->
    <div v-if="tab === 'public'" class="qe-cases__body">
      <div class="qe-cases__toolbar">
        <div class="qe-cases__tools">
          <button type="button" class="btn btn-sm btn-outline" @click="startAdd">+ 添加样例</button>
          <button type="button" class="btn btn-sm btn-outline" @click="importOpen = !importOpen">
            {{ importOpen ? '收起导入' : '批量导入' }}
          </button>
          <button
            v-if="showRun"
            type="button"
            class="btn btn-sm qe-cases__run"
            :disabled="!questionId"
            :title="questionId ? '运行全部公开样例' : '请先保存题目后再运行测试'"
            @click="emit('run-sample')"
          >▶ 运行测试</button>
        </div>
        <div class="qe-cases__count-hint">共 {{ rows.length }} 条 / 显示 {{ rangeStart }}–{{ rangeEnd }}</div>
      </div>

      <!-- 运行结果条 -->
      <div v-if="runResult" class="qe-cases__result" :class="'qe-cases__result--' + runStatusClass">
        {{ runStatusText }}
        <pre v-if="runResult.status === 'failed' && runResult.output" class="qe-cases__result-output">{{ runResult.output }}</pre>
      </div>

      <!-- 批量导入面板 -->
      <div v-if="importOpen" class="qe-cases__import">
        <p class="qe-cases__import-tip">粘贴 JSON 数组（追加导入），每条需含 args 数组：</p>
        <textarea
          v-model="importText"
          class="qe-cases__import-ta"
          spellcheck="false"
          placeholder='[{"args": [1, 2], "expected": 3, "desc": "正数相加"}]'
        ></textarea>
        <p v-if="importError" class="qe-cases__import-error">{{ importError }}</p>
        <div class="qe-cases__import-actions">
          <button type="button" class="btn btn-sm btn-outline" @click="importOpen = false; importError = ''">取消</button>
          <button type="button" class="btn btn-sm btn-primary" @click="doImport">导入追加</button>
        </div>
      </div>

      <!-- 表格：内部滚动（纵向 + 横向），不撑高页面 -->
      <div class="qe-cases__table-wrap">
        <table class="qe-cases__table">
          <thead>
            <tr>
              <th class="qe-cases__th-no">#</th>
              <th v-for="c in argCols" :key="'arg-' + c" class="qe-cases__th-arg">参数 {{ c }}</th>
              <th class="qe-cases__th-exp">期望输出</th>
              <th class="qe-cases__th-desc">说明</th>
              <th class="qe-cases__th-ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="pagedRows.length === 0">
              <td :colspan="argCols + 3" class="qe-cases__empty">
                🧩 暂无公开样例，点击「+ 添加样例」添加，或「批量导入」粘贴 JSON
              </td>
            </tr>
            <tr v-for="(row, i) in pagedRows" :key="row._key">
              <!-- 行内编辑态 -->
              <template v-if="editingKey === row._key">
                <td class="qe-cases__no">{{ (page - 1) * pageSize + i + 1 }}</td>
                <td v-for="(a, ai) in editingBuf.args" :key="ai" class="qe-cases__cell">
                  <div class="qe-cases__arg-input">
                    <input v-model="editingBuf.args[ai]" class="qe-cases__input qe-cases__input--arg" spellcheck="false" placeholder="值" />
                    <button type="button" class="qe-cases__arg-x" title="删除该参数" @click="removeArgInput(ai)">×</button>
                  </div>
                </td>
                <td v-for="c in Math.max(0, argCols - editingBuf.args.length)" :key="'pad' + c" class="qe-cases__cell"></td>
                <td class="qe-cases__cell">
                  <input v-model="editingBuf.expected" class="qe-cases__input" spellcheck="false" placeholder="如 3、ok 或 [1,2]" />
                </td>
                <td class="qe-cases__cell">
                  <input v-model="editingBuf.desc" class="qe-cases__input" placeholder="说明（可选）" />
                </td>
                <td class="qe-cases__ops">
                  <button type="button" class="qe-cases__op" title="添加一个参数" @click="addArgInput">+ 参数</button>
                  <button type="button" class="qe-cases__op qe-cases__op--save" @click="saveEdit">保存</button>
                  <button type="button" class="qe-cases__op" @click="cancelEdit">取消</button>
                </td>
              </template>
              <!-- 展示态 -->
              <template v-else>
                <td class="qe-cases__no">{{ (page - 1) * pageSize + i + 1 }}</td>
                <td v-for="c in argCols" :key="'v' + c" class="qe-cases__cell qe-cases__cell--mono">
                  {{ c <= row.args.length ? formatValue(row.args[c - 1]) : '—' }}
                </td>
                <td class="qe-cases__cell qe-cases__cell--mono qe-cases__cell--exp">{{ formatValue(row.expected) }}</td>
                <td class="qe-cases__cell qe-cases__cell--desc">{{ row.desc || '—' }}</td>
                <td class="qe-cases__ops">
                  <button type="button" class="qe-cases__op" @click="startEdit(row)">编辑</button>
                  <button type="button" class="qe-cases__op qe-cases__op--del" @click="askDelete(row._key)">
                    {{ confirmDeleteKey === row._key ? '确认删除？' : '删除' }}
                  </button>
                </td>
              </template>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 分页条 -->
      <div v-if="rows.length > 0" class="qe-cases__pager">
        <span class="qe-cases__pager-info">每页</span>
        <DsSelect v-model.number="pageSize" class="qe-cases__pager-size">
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
        </DsSelect>
        <span class="qe-cases__pager-info">条</span>
        <button type="button" class="qe-cases__pager-btn" :disabled="page <= 1" @click="page--">‹</button>
        <span class="qe-cases__pager-info">{{ page }} / {{ pageCount }}</span>
        <button type="button" class="qe-cases__pager-btn" :disabled="page >= pageCount" @click="page++">›</button>
      </div>
    </div>

    <!-- ═══ 私有测试 ═══ -->
    <div v-else class="qe-cases__body">
      <div class="qe-cases__sub-tabs">
        <button
          type="button"
          class="qe-cases__sub-tab"
          :class="{ active: privateTab === 'visual' }"
          @click="switchPrivateTab('visual')"
        >可视化测试</button>
        <button
          type="button"
          class="qe-cases__sub-tab"
          :class="{ active: privateTab === 'pytest' }"
          @click="switchPrivateTab('pytest')"
        >pytest 高级模式</button>
        <span class="qe-cases__sub-hint">私有测试不展示给学生，保存为 pytest 代码</span>
      </div>

      <!-- 可视化模式：与公开样例同款表格 -->
      <template v-if="privateTab === 'visual'">
        <div class="qe-cases__toolbar">
          <div class="qe-cases__tools">
            <button type="button" class="btn btn-sm btn-outline" @click="privateAddRow">+ 添加用例</button>
            <button type="button" class="btn btn-sm btn-outline" @click="privateImportOpen = !privateImportOpen">
              {{ privateImportOpen ? '收起导入' : '批量导入' }}
            </button>
          </div>
          <div class="qe-cases__count-hint">共 {{ privateRows.length }} 条 / 显示 {{ privateRangeStart }}–{{ privateRangeEnd }}</div>
        </div>

        <div v-if="privateImportOpen" class="qe-cases__import">
          <p class="qe-cases__import-tip">粘贴 JSON 数组（追加导入），每条需含 args 数组：</p>
          <textarea
            v-model="privateImportText"
            class="qe-cases__import-ta"
            spellcheck="false"
            placeholder='[{"args": [1, 2], "expected": 3, "desc": "负数相加"}]'
          ></textarea>
          <p v-if="privateImportError" class="qe-cases__import-error">{{ privateImportError }}</p>
          <div class="qe-cases__import-actions">
            <button type="button" class="btn btn-sm btn-outline" @click="privateImportOpen = false; privateImportError = ''">取消</button>
            <button type="button" class="btn btn-sm btn-primary" @click="privateDoImport">导入追加</button>
          </div>
        </div>

        <div class="qe-cases__table-wrap">
          <table class="qe-cases__table">
            <thead>
              <tr>
                <th class="qe-cases__th-no">#</th>
                <th v-for="c in privateArgCols" :key="'arg-' + c" class="qe-cases__th-arg">参数 {{ c }}</th>
                <th class="qe-cases__th-exp">期望输出</th>
                <th class="qe-cases__th-desc">说明</th>
                <th class="qe-cases__th-ops">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="privatePaged.length === 0">
                <td :colspan="privateArgCols + 3" class="qe-cases__empty">
                  🧩 暂无私有用例，点击「+ 添加用例」添加
                </td>
              </tr>
              <tr v-for="(row, i) in privatePaged" :key="row._key">
                <template v-if="privateEditingKey === row._key">
                  <td class="qe-cases__no">{{ (privatePage - 1) * pageSize + i + 1 }}</td>
                  <td v-for="(a, ai) in privateEditingBuf.args" :key="ai" class="qe-cases__cell">
                    <div class="qe-cases__arg-input">
                      <input v-model="privateEditingBuf.args[ai]" class="qe-cases__input qe-cases__input--arg" spellcheck="false" placeholder="值" />
                      <button type="button" class="qe-cases__arg-x" title="删除该参数" @click="privateRemoveArg(ai)">×</button>
                    </div>
                  </td>
                  <td v-for="c in Math.max(0, privateArgCols - privateEditingBuf.args.length)" :key="'pad' + c" class="qe-cases__cell"></td>
                  <td class="qe-cases__cell">
                    <input v-model="privateEditingBuf.expected" class="qe-cases__input" spellcheck="false" placeholder="如 3、ok 或 [1,2]" />
                  </td>
                  <td class="qe-cases__cell">
                    <input v-model="privateEditingBuf.desc" class="qe-cases__input" placeholder="说明（可选）" />
                  </td>
                  <td class="qe-cases__ops">
                    <button type="button" class="qe-cases__op" title="添加一个参数" @click="privateAddArg">+ 参数</button>
                    <button type="button" class="qe-cases__op qe-cases__op--save" @click="privateSaveEdit">保存</button>
                    <button type="button" class="qe-cases__op" @click="privateCancelEdit">取消</button>
                  </td>
                </template>
                <template v-else>
                  <td class="qe-cases__no">{{ (privatePage - 1) * pageSize + i + 1 }}</td>
                  <td v-for="c in privateArgCols" :key="'v' + c" class="qe-cases__cell qe-cases__cell--mono">
                    {{ c <= row.args.length ? formatValue(row.args[c - 1]) : '—' }}
                  </td>
                  <td class="qe-cases__cell qe-cases__cell--mono qe-cases__cell--exp">{{ formatValue(row.expected) }}</td>
                  <td class="qe-cases__cell qe-cases__cell--desc">{{ row.desc || '—' }}</td>
                  <td class="qe-cases__ops">
                    <button type="button" class="qe-cases__op" @click="privateStartEdit(row)">编辑</button>
                    <button type="button" class="qe-cases__op qe-cases__op--del" @click="privateAskDelete(row._key)">
                      {{ privateConfirmKey === row._key ? '确认删除？' : '删除' }}
                    </button>
                  </td>
                </template>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="privateRows.length > 0" class="qe-cases__pager">
          <span class="qe-cases__pager-info">每页</span>
          <DsSelect v-model.number="pageSize" class="qe-cases__pager-size">
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
          </DsSelect>
          <span class="qe-cases__pager-info">条</span>
          <button type="button" class="qe-cases__pager-btn" :disabled="privatePage <= 1" @click="privatePage--">‹</button>
          <span class="qe-cases__pager-info">{{ privatePage }} / {{ privatePageCount }}</span>
          <button type="button" class="qe-cases__pager-btn" :disabled="privatePage >= privatePageCount" @click="privatePage++">›</button>
        </div>
      </template>

      <!-- pytest 高级模式：深色代码编辑器，固定高度 + 内部滚动 -->
      <template v-else>
        <QeCodeEditor v-model="privatePytestText" :height="320" placeholder="# 私有测试（pytest）&#10;def test_add():&#10;    assert add(1, 2) == 3" />
        <p class="qe-cases__code-hint">保存时按 pytest 代码原样提交；切换回可视化模式会自动转换（复杂断言可能无法解析）</p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.qe-cases { min-width: 0; }

/* ── tab ─────────────────────────────────────────────────────────── */
.qe-cases__tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 14px;
}

.qe-cases__tab {
  padding: 7px 14px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--muted);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  margin-bottom: -1px;
}

.qe-cases__tab:hover { color: var(--fg); }

.qe-cases__tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}

.qe-cases__count {
  display: inline-block;
  min-width: 18px;
  padding: 0 5px;
  border-radius: var(--radius-full);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
  text-align: center;
}

/* ── 工具栏 ──────────────────────────────────────────────────────── */
.qe-cases__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.qe-cases__tools { display: flex; gap: 8px; align-items: center; }

.qe-cases__run {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--surface);
}

.qe-cases__run:hover:not(:disabled) { background: var(--accent-soft); }

.qe-cases__run:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.qe-cases__count-hint {
  font-size: var(--text-xs);
  color: var(--faint);
}

/* ── 运行结果条 ──────────────────────────────────────────────────── */
.qe-cases__result {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: 500;
}

.qe-cases__result--passed { background: var(--success-bg); color: var(--success); }
.qe-cases__result--failed { background: var(--danger-bg); color: var(--danger); }
.qe-cases__result--error { background: var(--warning-bg); color: var(--warning); }
.qe-cases__result--running { background: var(--info-bg); color: var(--info); }

.qe-cases__result-output {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 400;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 140px;
  overflow-y: auto;
}

/* ── 批量导入面板 ────────────────────────────────────────────────── */
.qe-cases__import {
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  margin-bottom: 10px;
  background: var(--surface-sunken);
}

.qe-cases__import-tip { margin: 0 0 6px; font-size: var(--text-xs); color: var(--muted); }

.qe-cases__import-ta {
  width: 100%;
  height: 84px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 12px;
  resize: vertical;
  box-sizing: border-box;
}

.qe-cases__import-error { margin: 6px 0 0; font-size: var(--text-xs); color: var(--danger); }

.qe-cases__import-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }

/* ── 表格：固定可视区域，纵向 + 横向内部滚动 ─────────────────────── */
.qe-cases__table-wrap {
  max-height: 340px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
}

.qe-cases__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.qe-cases__table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface-subtle);
  color: var(--muted);
  font-weight: 600;
  font-size: 12px;
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}

.qe-cases__table td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--line-soft);
  vertical-align: middle;
}

.qe-cases__table tr:last-child td { border-bottom: none; }

.qe-cases__table tbody tr:hover { background: var(--accent-soft); }
.qe-cases__table tbody tr:has(.qe-cases__arg-input) { background: var(--surface); }

.qe-cases__th-no { width: 40px; }
.qe-cases__th-arg { min-width: 96px; }
.qe-cases__th-exp { min-width: 130px; }
.qe-cases__th-desc { min-width: 150px; }
.qe-cases__th-ops { min-width: 150px; }

.qe-cases__no {
  color: var(--faint);
  font-family: var(--font-mono);
  font-size: 12px;
}

.qe-cases__cell { max-width: 260px; }

.qe-cases__cell--mono {
  font-family: var(--font-mono);
  font-size: 12.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 220px;
}

.qe-cases__cell--exp { color: var(--accent-hover); font-weight: 500; }
.qe-cases__cell--desc { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.qe-cases__empty {
  text-align: center;
  color: var(--faint);
  padding: 26px 12px !important;
}

/* 行内编辑 */
.qe-cases__arg-input { display: flex; align-items: center; gap: 4px; }

.qe-cases__input {
  width: 100%;
  min-width: 80px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-family: inherit;
  font-size: 12.5px;
  color: var(--fg);
  background: var(--surface);
  box-sizing: border-box;
}

.qe-cases__input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.qe-cases__input--arg { font-family: var(--font-mono); }

.qe-cases__arg-x {
  border: none;
  background: transparent;
  color: var(--faint);
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
}

.qe-cases__arg-x:hover { color: var(--danger); }

.qe-cases__ops {
  display: table-cell;
  vertical-align: middle;
  white-space: nowrap;
}
.qe-cases__ops .qe-cases__op {
  display: inline-flex;
  align-items: center;
  vertical-align: middle;
}
.qe-cases__ops .qe-cases__op + .qe-cases__op {
  margin-left: 6px;
}

.qe-cases__op {
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--muted);
  font-size: 12px;
  cursor: pointer;
}

.qe-cases__op:hover { border-color: var(--border-strong); color: var(--fg); }

.qe-cases__op--save { border-color: var(--accent); color: var(--accent); }
.qe-cases__op--save:hover { background: var(--accent-soft); }

.qe-cases__op--del:hover { border-color: var(--danger); color: var(--danger); background: var(--danger-bg); }

/* ── 分页条 ──────────────────────────────────────────────────────── */
.qe-cases__pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}

.qe-cases__pager-info { font-size: var(--text-xs); color: var(--faint); }

.qe-cases__pager-size {
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: 12px;
  background: var(--surface);
  color: var(--fg);
}

.qe-cases__pager-btn {
  min-width: 26px;
  height: 26px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--fg);
  font-size: 13px;
  cursor: pointer;
}

.qe-cases__pager-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.qe-cases__pager-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* ── 私有测试子 tab ──────────────────────────────────────────────── */
.qe-cases__sub-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 12px;
}

.qe-cases__sub-tab {
  padding: 5px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--surface);
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}

.qe-cases__sub-tab:hover { color: var(--fg); border-color: var(--border-strong); }

.qe-cases__sub-tab.active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--surface);
}

.qe-cases__sub-hint {
  margin-left: auto;
  font-size: var(--text-xs);
  color: var(--faint);
}

.qe-cases__code-hint {
  margin: 8px 0 0;
  font-size: var(--text-xs);
  color: var(--faint);
}
</style>
