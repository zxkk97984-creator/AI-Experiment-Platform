<template>
  <div class="gv-panel">
    <div class="gv-head">
      <h4>可信数据判题（评分版本）</h4>
      <span v-if="currentVersionId" class="gv-current" data-testid="gv-current">
        当前启用：#{{ currentVersionId }}
      </span>
      <span v-else class="gv-current gv-current--none" data-testid="gv-current-none">未启用</span>
    </div>

    <p class="gv-hint">
      数据用例在平台侧比较，容器只回传实际值。转换与 AI 结果都只是草稿，
      必须确认后才能启用。新版本每个用例都会启动独立容器；依赖跨用例共享状态的旧测试需转人工，
      或按这条新规则重新确认。启用只影响新提交，已开始的考试与历史提交不受影响。
    </p>

    <div v-if="unconfirmedDraft" class="gv-draft-hint" data-testid="gv-draft-hint" role="status">
      存在未确认草稿 <strong>#{{ unconfirmedDraft.version }}</strong>：
      需先「确认」再「启用」，否则不会影响任何提交。
    </div>

    <div v-if="preflight" class="gv-preflight" data-testid="gv-preflight">
      启用前核查：在途任务 <strong>{{ preflight.in_flight }}</strong> ·
      无基线历史 <strong>{{ preflight.unbound_legacy }}</strong> ·
      进行中考试 <strong>{{ preflight.started_exams }}</strong>
      <span v-if="!preflight.ready" class="gv-block">（有在途任务，启用需显式确认）</span>
    </div>

    <div v-if="error" class="gv-error" role="alert" data-testid="gv-error">{{ error }}</div>
    <div v-if="notice" class="gv-notice" role="status" data-testid="gv-notice">{{ notice }}</div>

    <div class="gv-actions">
      <button class="btn-sm" :disabled="busy" data-testid="gv-preview" @click="preview">
        {{ busy && stage === 'preview' ? '预检中…' : '转换预检（pytest → 数据用例）' }}
      </button>
      <button
        class="btn-sm"
        :disabled="busy || !previewSummary || previewSummary.convertible_count === 0"
        data-testid="gv-apply"
        @click="apply"
      >
        {{ busy && stage === 'apply' ? '生成中…' : '生成草稿版本' }}
      </button>
      <button class="btn-sm" :disabled="busy" data-testid="gv-refresh" @click="load">刷新</button>
      <button
        v-if="kind === 'assignment'"
        class="btn-sm"
        :disabled="busy"
        data-testid="gv-ai-generate"
        @click="aiGenerate"
      >
        {{ busy && stage === 'ai' ? 'AI 生成中…' : 'AI 生成草稿' }}
      </button>
    </div>

    <!-- 预检结果：只报告，不写库 -->
    <div v-if="previewSummary" class="gv-preview" data-testid="gv-preview-result">
      <p>
        可转换 <strong>{{ previewSummary.convertible_count }}</strong> 条 ·
        需人工处理 <strong>{{ previewSummary.unsupported_count }}</strong> 条
        <span v-if="previewSummary.fully_convertible" class="gv-ok">（全部可转换）</span>
      </p>
      <ul v-if="previewSummary.reasons?.length" class="gv-reasons">
        <li v-for="(reason, index) in previewSummary.reasons" :key="index">{{ reason }}</li>
      </ul>
      <p v-for="(note, index) in previewSummary.notes || []" :key="'n' + index" class="gv-note">
        {{ note }}
      </p>
    </div>

    <!-- 逐例表单：手工维护数据用例（保存即生成新草稿） -->
    <section class="gv-form">
      <button class="btn-sm" :disabled="busy" data-testid="gv-form-toggle" @click="formOpen = !formOpen">
        {{ formOpen ? '收起用例表单' : '新建草稿（逐例表单）' }}
      </button>
      <div v-if="formOpen" class="gv-form-body" data-testid="gv-form">
        <div class="gv-form-row">
          <label>入口函数
            <input v-model="formEntry" data-testid="gv-form-entry" placeholder="double_first" />
          </label>
          <button class="btn-sm" :disabled="busy" data-testid="gv-form-add" @click="addCaseRow">添加用例</button>
        </div>
        <table class="gv-table">
          <thead>
            <tr>
              <th>用例 ID</th><th>组</th><th>维度</th><th>组满分</th>
              <th>输入（位置参数元组，如 (1, 2)）</th><th>期望值字面量</th><th>比较规则</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in formRows" :key="index" :data-testid="`gv-form-row-${index}`">
              <td><input v-model="row.id" class="gv-input gv-input--sm" /></td>
              <td><input v-model="row.group" class="gv-input gv-input--sm" /></td>
              <td>
                <select v-model="row.dimension" class="gv-input gv-input--sm">
                  <option value="F">F 功能</option><option value="R">R 边界</option>
                </select>
              </td>
              <td><input v-model.number="row.maxScore" type="number" min="1" class="gv-input gv-input--sm" /></td>
              <td><input v-model="row.input" class="gv-input" placeholder="(1, 2)" /></td>
              <td><input v-model="row.expected" class="gv-input" placeholder="3" /></td>
              <td>
                <select v-model="row.compare" class="gv-input gv-input--sm">
                  <option value="literal">字面量相等</option>
                  <option value="identity">is None/True/False</option>
                  <option value="float">浮点容差</option>
                  <option value="exception">期望异常</option>
                </select>
                <input
                  v-if="row.compare === 'float'"
                  v-model="row.absTol"
                  class="gv-input gv-input--sm"
                  data-testid="`gv-form-tol-${index}`"
                  placeholder="abs_tol 1e-9"
                />
                <input
                  v-if="row.compare === 'exception'"
                  v-model="row.exceptionType"
                  class="gv-input gv-input--sm"
                  placeholder="ValueError"
                />
              </td>
              <td><button class="btn-sm" @click="formRows.splice(index, 1)">删除</button></td>
            </tr>
          </tbody>
        </table>
        <p v-if="formError" class="gv-form-error" data-testid="gv-form-error">{{ formError }}</p>
        <button class="btn-sm" :disabled="busy || !formRows.length" data-testid="gv-form-save" @click="saveForm">
          {{ busy && stage === 'form' ? '保存中…' : '保存为新草稿' }}
        </button>
      </div>
    </section>

    <div v-if="loading" class="gv-loading">加载中…</div>
    <table v-else-if="versions.length" class="gv-table">
      <thead>
        <tr><th>版本</th><th>状态</th><th>来源</th><th>用例</th><th>摘要</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="version in versions" :key="version.id" :data-testid="`gv-row-${version.id}`">
          <td>#{{ version.version }}</td>
          <td>
            <span :class="['gv-status', `gv-status--${version.status}`]">{{ statusLabel(version.status) }}</span>
          </td>
          <td>{{ sourceLabel(version.source_kind) }}</td>
          <td>{{ version.case_count }}</td>
          <td class="gv-digest" :title="version.spec_digest">{{ version.spec_digest.slice(0, 8) }}</td>
          <td class="gv-row-actions">
            <button
              class="btn-sm"
              :disabled="busy"
              :data-testid="`gv-cases-${version.id}`"
              @click="toggleCases(version)"
            >{{ expandedId === version.id ? '收起用例' : '查看用例' }}</button>
            <button
              v-if="version.status === 'draft'"
              class="btn-sm"
              :disabled="busy"
              :data-testid="`gv-confirm-${version.id}`"
              @click="confirm(version)"
            >确认</button>
            <button
              v-if="version.status === 'confirmed'"
              class="btn-sm"
              :disabled="busy"
              :data-testid="`gv-activate-${version.id}`"
              @click="activate(version)"
            >启用</button>
            <button
              v-if="version.status === 'active'"
              class="btn-sm"
              :disabled="busy"
              :data-testid="`gv-retire-${version.id}`"
              @click="retire(version)"
            >退役</button>
            <span v-if="version.status === 'retired'" class="gv-muted">历史版本可读</span>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="caseRows.length" class="gv-cases" data-testid="gv-cases">
      <p class="gv-cases-title">用例明细（版本 #{{ expandedId }}）</p>
      <table class="gv-table">
        <thead><tr><th>用例</th><th>组</th><th>维度</th><th>比较规则</th><th>期望值类型</th></tr></thead>
        <tbody>
          <tr v-for="row in caseRows" :key="row.id" :data-testid="`gv-case-${row.id}`">
            <td>{{ row.id }}</td><td>{{ row.group }}</td><td>{{ row.dimension }}</td>
            <td>{{ row.compare }}</td><td>{{ row.expectedType }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else-if="!versions.length" class="gv-empty" data-testid="gv-empty">暂无评分版本</p>

    <ConfirmDialog
      v-if="pendingAction"
      :title="pendingAction.title"
      :message="pendingAction.message"
      :confirm-text="pendingAction.confirmText"
      :danger="pendingAction.danger === true"
      :busy="busy"
      cancel-text="取消"
      @confirm="runPendingAction"
      @cancel="pendingAction = null"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { gradingVersionsAPI } from '../../api/gradingVersions.js'
import ConfirmDialog from '../ui/ConfirmDialog.vue'

const props = defineProps({
  questionId: { type: Number, required: true },
  // assignment（作业题）| exam（考试题）：端点前缀与权限范围不同
  kind: { type: String, default: 'assignment' },
})

const versions = ref([])
const currentVersionId = ref(null)
const loading = ref(false)
const busy = ref(false)
const stage = ref('')
const error = ref('')
const notice = ref('')
const previewSummary = ref(null)
const preflight = ref(null)
const expandedId = ref(null)
const caseRows = ref([])
const pendingAction = ref(null)

const STATUS_LABELS = { draft: '草稿', confirmed: '已确认', active: '启用中', retired: '已退役' }
const SOURCE_LABELS = { manual: '手工编写', converted_from_pytest: 'pytest 转换', ai_draft: 'AI 草稿' }

function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

function sourceLabel(source) {
  return SOURCE_LABELS[source] || source || '-'
}

const formOpen = ref(false)
const formEntry = ref('')
const formError = ref('')
const formRows = ref([])

function addCaseRow() {
  const index = formRows.value.length + 1
  const dimension = index === 1 ? 'F' : 'F'
  formRows.value.push({
    id: `F1-${index}`, group: 'F1', dimension, maxScore: 60,
    input: '', expected: '', compare: 'literal', absTol: '1e-9', exceptionType: 'ValueError',
  })
}

async function saveForm() {
  formError.value = ''
  if (!formEntry.value.trim()) {
    formError.value = '请填写入口函数'
    return
  }
  if (!formRows.value.length) {
    formError.value = '至少需要一个用例'
    return
  }
  busy.value = true
  stage.value = 'form'
  try {
    const groups = new Map()
    for (const row of formRows.value) {
      if (!row.id || !row.group || !row.input || !row.expected) {
        throw new Error('每行都必须填写用例 ID、组、输入与期望值')
      }
      if (!groups.has(row.group)) {
        groups.set(row.group, { id: row.group, name: row.group, dimension: row.dimension,
                                max_score: Number(row.maxScore) || 0, cases: [] })
      }
      const input = await gradingVersionsAPI.encodeLiteral(row.input)
      const expected = await gradingVersionsAPI.encodeLiteral(row.expected)
      let compare = { kind: row.compare }
      if (row.compare === 'float') compare = { kind: 'float', abs_tol: Number(row.absTol) || 0, rel_tol: 0 }
      if (row.compare === 'exception') compare = { kind: 'exception', types: [row.exceptionType] }
      groups.get(row.group).cases.push({ id: row.id, input: input.data.value,
                                         expected: expected.data.value, compare })
    }
    const payload = {
      schema_version: 1, entry_function: formEntry.value.trim(), execution_model: 'data_v1',
      binary_cases: [], groups: [...groups.values()],
    }
    const created = await gradingVersionsAPI.createDraft(
      props.questionId, { payload, entry_function: formEntry.value.trim() }, props.kind)
    notice.value = `已生成草稿版本 #${created.data.version}，请确认后启用`
    formRows.value = []
    formOpen.value = false
    await load()
  } catch (err) {
    formError.value = messageOf(err)
  } finally {
    busy.value = false
    stage.value = ''
  }
}

const unconfirmedDraft = computed(() => {
  const drafts = versions.value.filter((item) => item.status === 'draft')
  if (!drafts.length) return null
  const activeVersion = versions.value.find((item) => item.status === 'active')
  const newest = drafts[0]
  if (!activeVersion) return newest
  return newest.version > activeVersion.version ? newest : null
})

async function toggleCases(version) {
  if (expandedId.value === version.id) {
    expandedId.value = null
    caseRows.value = []
    return
  }
  busy.value = true
  error.value = ''
  try {
    const { data } = await gradingVersionsAPI.detail(version.id, props.kind)
    expandedId.value = version.id
    caseRows.value = flattenCases(data.payload)
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    busy.value = false
  }
}

function flattenCases(payload) {
  const rows = []
  for (const item of payload?.binary_cases || []) {
    rows.push({
      id: item.id,
      group: '可信二元用例',
      dimension: 'binary',
      compare: item.compare?.kind || 'literal',
      expectedType: item.expected?.t || '-',
    })
  }
  for (const group of payload?.groups || []) {
    for (const item of group.cases || []) {
      rows.push({
        id: item.id,
        group: group.id,
        dimension: group.dimension,
        compare: item.compare?.kind || 'literal',
        expectedType: item.expected?.t || '-',
      })
    }
  }
  return rows
}

function messageOf(err) {
  return err?.response?.data?.detail?.message || err?.message || '操作失败'
}

// 迟到响应防护：切题后旧请求的结果一律丢弃（A05）
let loadGeneration = 0

/** 清空全部本地状态：切题/换 kind 时必须调用，避免残留上一题的版本与对话框。 */
function resetState() {
  versions.value = []
  currentVersionId.value = null
  previewSummary.value = null
  preflight.value = null
  caseRows.value = []
  expandedId.value = null
  pendingAction.value = null
  notice.value = ''
  error.value = ''
  formError.value = ''
  formRows.value = []
}

async function load() {
  const generation = ++loadGeneration
  loading.value = true
  error.value = ''
  try {
    const { data } = await gradingVersionsAPI.list(props.questionId, props.kind)
    if (generation !== loadGeneration) return
    versions.value = data.items || []
    currentVersionId.value = data.current_grading_version_id || null
    await loadPreflight()
  } catch (err) {
    if (generation !== loadGeneration) return
    error.value = messageOf(err)
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

// 题目或类型变化：立即清空并重新加载；在途请求的结果因 generation 不匹配被丢弃
watch(() => [props.questionId, props.kind], () => {
  resetState()
  load()
})

async function loadPreflight() {
  const generation = loadGeneration
  try {
    const { data } = await gradingVersionsAPI.preflight(props.questionId, props.kind)
    if (generation !== loadGeneration) return
    preflight.value = data
  } catch {
    // 预检失败不阻断版本列表（后端仍会在启用时强制门禁）
    preflight.value = null
  }
}

async function preview() {
  busy.value = true
  stage.value = 'preview'
  error.value = ''
  notice.value = ''
  try {
    const { data } = await gradingVersionsAPI.convertPreview(props.questionId, {}, props.kind)
    previewSummary.value = data.summary
    notice.value = data.summary.convertible_count
      ? `预检完成：可转换 ${data.summary.convertible_count} 条（尚未写入任何版本）`
      : '预检完成：没有可自动转换的用例，请人工改写'
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    busy.value = false
    stage.value = ''
  }
}

async function aiGenerate() {
  busy.value = true
  stage.value = 'ai'
  error.value = ''
  notice.value = ''
  try {
    const { data } = await gradingVersionsAPI.aiGenerate(props.questionId, {
      entry_function: formEntry.value.trim() || undefined,
    })
    notice.value = `AI 已生成草稿版本 #${data.version.version}（${data.summary.convertible_count} 条用例，需确认后启用）`
    await load()
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    busy.value = false
    stage.value = ''
  }
}

async function apply() {
  pendingAction.value = {
    title: '按逐例隔离规则生成新版本？',
    message: '每个测试用例会在独立容器中运行，模块状态不会跨用例共享。依赖共享状态的旧测试不能作为等价迁移，请先转人工处理或明确接受新规则。',
    confirmText: '确认新规则并生成草稿',
    run: runConversionApply,
  }
}

async function runConversionApply() {
  busy.value = true
  stage.value = 'apply'
  error.value = ''
  try {
    const { data } = await gradingVersionsAPI.convertApply(
      props.questionId, { confirm_isolated_case_v2: true }, props.kind)
    notice.value = `已生成草稿版本 #${data.version.version}，请确认后启用`
    previewSummary.value = data.summary
    await load()
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    busy.value = false
    stage.value = ''
  }
}

function confirm(version) {
  pendingAction.value = {
    title: `确认评分版本 #${version.version}？`,
    message: '确认后内容将被冻结。该版本采用逐例独立容器语义：依赖跨用例共享状态的旧测试需转人工处理或作为新规则重新创建。',
    confirmText: '确认逐例规则并冻结',
    run: () => gradingVersionsAPI.confirm(version.id, version.spec_digest, props.kind, true),
    success: `版本 #${version.version} 已确认，可启用`,
  }
}

async function activate(version) {
  await loadPreflight()
  const blocked = preflight.value && !preflight.value.ready
  const reasons = blocked ? (preflight.value.blocking || []).join('；') : ''
  pendingAction.value = {
    title: `启用评分版本 #${version.version}？`,
    message: blocked
      ? `影响面核查未通过：${reasons}。确认继续将显式忽略在途任务。`
      : '启用后新提交绑定该版本；同题旧的启用版本将自动退役（历史绑定不受影响）。',
    confirmText: blocked ? '仍然启用' : '启用',
    danger: blocked,
    run: () => gradingVersionsAPI.activate(version.id, { acknowledgeInFlight: blocked, kind: props.kind }),
    success: `版本 #${version.version} 已启用`,
  }
}

function retire(version) {
  pendingAction.value = {
    title: `退役评分版本 #${version.version}？`,
    message: '退役只禁止新绑定；已开始的考试与历史提交仍按原版本判题。',
    confirmText: '退役',
    run: () => gradingVersionsAPI.retire(version.id, props.kind),
    success: `版本 #${version.version} 已退役`,
  }
}

async function runPendingAction() {
  const action = pendingAction.value
  if (!action) return
  pendingAction.value = null
  busy.value = true
  error.value = ''
  try {
    await action.run()
    // Some actions (conversion/AI generation) already write their own notice
    // and refresh the list; avoid overwriting that result or issuing a duplicate load.
    if (action.success) {
      notice.value = action.success
      await load()
    }
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    busy.value = false
  }
}

onMounted(load)

defineExpose({ load, preview, apply, toggleCases, addCaseRow, saveForm, aiGenerate })
</script>

<style scoped>
.gv-panel { padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.gv-head { display: flex; align-items: center; gap: 10px; }
.gv-head h4 { margin: 0; font-size: var(--text-base); color: var(--fg); }
.gv-current { padding: 2px 8px; border-radius: 999px; background: var(--accent-soft, #eef2ff); color: var(--accent); font-size: var(--text-xs); }
.gv-current--none { background: var(--surface-subtle); color: var(--muted); }
.gv-hint { margin: 8px 0 12px; color: var(--muted); font-size: var(--text-sm); line-height: 1.6; }
.gv-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.gv-draft-hint { padding: 8px 10px; margin-bottom: 10px; border: 1px solid #f59e0b; border-radius: var(--radius-sm); background: #fffbeb; color: #92400e; font-size: var(--text-sm); }
.gv-form { margin: 12px 0; }
.gv-form-body { margin-top: 10px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.gv-form-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; font-size: var(--text-sm); }
.gv-input { width: 100%; padding: 4px 6px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); font-size: var(--text-sm); }
.gv-input--sm { max-width: 90px; }
.gv-form-error { color: var(--danger, #dc2626); font-size: var(--text-sm); }
.gv-cases { margin-top: 12px; }
.gv-cases-title { margin: 0 0 6px; font-size: var(--text-sm); color: var(--muted); }
.gv-preflight { padding: 8px 10px; margin-bottom: 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-subtle); font-size: var(--text-sm); }
.gv-block { color: var(--danger, #dc2626); }
.gv-error { padding: 8px 10px; margin-bottom: 10px; border: 1px solid var(--danger, #dc2626); border-radius: var(--radius-sm); color: var(--danger, #dc2626); font-size: var(--text-sm); }
.gv-notice { padding: 8px 10px; margin-bottom: 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-subtle); font-size: var(--text-sm); }
.gv-preview { padding: 10px; margin-bottom: 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-subtle); font-size: var(--text-sm); }
.gv-reasons { margin: 6px 0 0 18px; color: var(--muted); }
.gv-note { margin: 6px 0 0; color: var(--muted); }
.gv-ok { color: var(--success, #16a34a); }
.gv-table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
.gv-table th, .gv-table td { padding: 8px; border-bottom: 1px solid var(--border); text-align: left; }
.gv-status { padding: 2px 8px; border-radius: 999px; font-size: var(--text-xs); }
.gv-status--draft { background: var(--surface-subtle); color: var(--muted); }
.gv-status--confirmed { background: #fef3c7; color: #92400e; }
.gv-status--active { background: #dcfce7; color: #166534; }
.gv-status--retired { background: var(--surface-subtle); color: var(--faint); }
.gv-digest { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); }
.gv-row-actions { display: flex; gap: 6px; align-items: center; }
.gv-muted { color: var(--muted); font-size: var(--text-xs); }
.gv-loading, .gv-empty { color: var(--muted); font-size: var(--text-sm); }
</style>
