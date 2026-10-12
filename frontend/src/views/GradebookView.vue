<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from '../components/layout/AppLayout.vue'
import ConfirmDialog from '../components/ui/ConfirmDialog.vue'
import { useAuthStore } from '../stores/auth.js'
import { gradebookAPI } from '../api/gradebook.js'
import { downloadBlob, errorMessage } from '../api/accountManagement.js'
import { formatDateTime } from '../utils/format.js'

const route = useRoute()
const auth = useAuthStore()
const isStudent = computed(() => auth.user?.role === 'student')
const offerings = ref([])
const offeringId = ref('')
const data = ref(null)
const config = ref([])
const history = ref([])
const viewing = ref(null)
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const message = ref('')
const reason = ref('')
const selectedStudent = ref(null)
const entryForm = ref([])
const adjustmentStudent = ref('')
const adjustmentScore = ref('')
const adjustmentReason = ref('')
let requestVersion = 0
let pageActive = true
let snapshotRequestVersion = 0
let requestedSnapshotId = null
const chosenOffering = computed(() => offerings.value.find(item => String(item.id) === String(offeringId.value)))
const readOnly = computed(() => isStudent.value || chosenOffering.value?.read_only || data.value?.read_only)
const currentSnapshot = computed(() => viewing.value || data.value?.snapshot)
const shownConfig = computed(() => viewing.value ? viewing.value.payload.config : isStudent.value ? currentSnapshot.value?.payload.config || [] : data.value?.config || [])
const columns = computed(() => shownConfig.value.flatMap(group => group.columns))
const rows = computed(() => viewing.value || isStudent.value ? currentSnapshot.value?.payload.rows || [] : data.value?.draft?.rows || [])
const weightTotal = computed(() => config.value.reduce((sum, item) => sum + Number(item.weight || 0), 0))
const blockers = computed(() => data.value?.draft?.blockers || [])
// 自定义比例栏目里新自动关联的分项：未分配比例，暂不参与总评，也不能发布。
const pendingColumns = computed(() => (isStudent.value || viewing.value ? [] : config.value
  .flatMap(category => category.columns).filter(column => column.allocation_status === 'pending')))
const restoreReason = ref('')
const restoreConfirm = ref(false)
const sourceKinds = { manual: '人工录入', assignment: '作业', exam: '考试', experiment: '实验' }
const states = { scored: '已评分', pending: '待评分', missing: '确认缺交', missing_pending: '缺交待确认', exempt: '免修', auto: '来源成绩' }
const createKey = (prefix) => `${prefix}-${crypto.randomUUID()}`

function isCurrentBook(version, selectedOffering) {
  return pageActive && version === requestVersion && selectedOffering === offeringId.value
}
function returnToDraft() {
  snapshotRequestVersion++
  requestedSnapshotId = null
  viewing.value = null
  selectedStudent.value = null
  error.value = ''
}
async function load() {
  if (!pageActive) return
  const current = ++requestVersion
  const selectedOffering = offeringId.value
  returnToDraft()
  data.value = null; history.value = []; config.value = []
  busy.value = false; message.value = ''; loading.value = Boolean(selectedOffering)
  if (!selectedOffering) return
  try {
    const [result, snapshots] = await Promise.all([gradebookAPI.read(selectedOffering), gradebookAPI.snapshots(selectedOffering)])
    if (!isCurrentBook(current, selectedOffering)) return
    data.value = result.data
    config.value = JSON.parse(JSON.stringify(result.data.config || []))
    history.value = snapshots.data.items || []
  } catch (e) { if (isCurrentBook(current, selectedOffering)) error.value = errorMessage(e) }
  finally { if (isCurrentBook(current, selectedOffering)) loading.value = false }
}
onMounted(async () => {
  try {
    const result = await gradebookAPI.offerings()
    if (!pageActive) return
    offerings.value = result.data.items || []
    const requested = route.params.offeringId || route.query.offering_id
    offeringId.value = String(offerings.value.find(item => String(item.id) === String(requested))?.id || offerings.value[0]?.id || '')
    await load()
  } catch (e) { if (pageActive) error.value = errorMessage(e) }
})
onUnmounted(() => {
  pageActive = false
  requestVersion++
  snapshotRequestVersion++
})
async function mutate(action, success, allowReadOnly = false) {
  if (busy.value || (readOnly.value && !allowReadOnly)) return false
  const selectedOffering = offeringId.value
  let current = requestVersion
  const isCurrent = () => isCurrentBook(current, selectedOffering)
  busy.value = true; error.value = ''; message.value = ''
  try {
    await action()
    if (!isCurrent()) return false
    current = requestVersion + 1 // load below owns the next book request.
    await load()
    if (!isCurrent()) return false
    message.value = success
    return true
  } catch (e) { if (isCurrent()) error.value = errorMessage(e); return false }
  finally { if (isCurrent()) busy.value = false }
}
function addCategory() {
  const key = createKey('category')
  config.value.push({ key, title: '新栏目', weight: '0', columns: [{ key: createKey('column'), title: '新分项', weight: '100', source_kind: 'manual', source_id: null, selection: 'highest' }] })
}
function addColumn(category) { category.columns.push({ key: createKey('column'), title: '新分项', weight: '0', source_kind: 'manual', source_id: null, selection: 'highest' }) }
function isCustomMode(category) { return category.weight_mode === 'custom' }
function changeMode(category, mode) {
  category.weight_mode = mode
  // 等权栏目由分项数量决定比例：清掉旧的显式比例，避免切换后仍按旧比例计算。
  if (mode !== 'custom') for (const column of category.columns) column.weight = null
}
function saveConfig() {
  // 输入框清空得到 ''，与「未分配」null 同义；统一按 null 提交，避免 422。
  const categories = config.value.map(category => ({
    ...category,
    columns: category.columns.map(column => column.weight === '' ? { ...column, weight: null } : column),
  }))
  return mutate(() => gradebookAPI.configure(offeringId.value, { expected_revision: data.value.revision, categories }), '栏目与比例已保存。成绩仍需审核后发布。')
}
function restoreDefault() {
  return mutate(() => gradebookAPI.restoreDefault(offeringId.value,
    { expected_revision: data.value.revision, reason: restoreReason.value }),
  '已按课程默认模板重建本班栏目与比例，请重新核对分项。').then(saved => {
    restoreConfirm.value = false
    if (saved) restoreReason.value = ''
    return saved
  })
}
function editStudent(student) {
  selectedStudent.value = student
  entryForm.value = columns.value.map(column => {
    const cell = student.cells[column.key]
    let state = cell?.state === 'missing_pending' ? 'auto' : cell?.state || 'pending'
    if (column.source_kind !== 'manual' && state === 'scored' && cell?.source?.kind !== 'manual') state = 'auto'
    if (cell?.source?.missing_confirmation_reason) state = 'missing'
    return { column_key: column.key, title: column.title, source_kind: column.source_kind, state,
      score: state === 'scored' ? cell?.score || '' : '', reason: cell?.source?.reason || cell?.source?.missing_confirmation_reason || '' }
  })
}
function saveEntries() {
  const entries = entryForm.value.map(entry => ({ student_id: selectedStudent.value.student_id, column_key: entry.column_key,
    state: entry.state, score: entry.state === 'scored' ? entry.score : null, reason: entry.reason || null }))
  return mutate(() => gradebookAPI.entries(offeringId.value, { expected_revision: data.value.revision, entries }), '分项草稿已保存，官方成绩版本保持不变。')
}
function adjust(clear = false) {
  return mutate(() => gradebookAPI.adjustment(offeringId.value, { expected_revision: data.value.revision,
    student_id: Number(adjustmentStudent.value), score: clear ? null : adjustmentScore.value, reason: adjustmentReason.value }), '总分调整已保存到草稿，发布后学生可见。')
}
async function publish(archive = false) {
  const selectedOffering = offeringId.value
  const submittedReason = reason.value
  const saved = await mutate(() => gradebookAPI[archive ? 'archive' : 'publish'](offeringId.value,
    { expected_revision: data.value.revision, expected_preview_hash: archive ? undefined : data.value.draft?.preview_hash,
      reason: reason.value }), archive ? '当前官方成绩已生成独立归档。' : '成绩已发布，学生现在可查看本人版本。', archive)
  if (saved && pageActive && offeringId.value === selectedOffering && reason.value === submittedReason) reason.value = ''
}
async function viewSnapshot(id) {
  const selectedOffering = offeringId.value
  const bookRequest = requestVersion
  const request = ++snapshotRequestVersion
  requestedSnapshotId = id
  const isCurrent = () => isCurrentBook(bookRequest, selectedOffering)
    && request === snapshotRequestVersion && requestedSnapshotId === id
  error.value = ''
  try {
    const result = await gradebookAPI.snapshot(id)
    if (!isCurrent()) return
    viewing.value = result.data; selectedStudent.value = null
  } catch (e) { if (isCurrent()) error.value = errorMessage(e) }
}
async function exportGrades(format) {
  const snapshot = currentSnapshot.value
  if (!snapshot || !offeringId.value || loading.value) return
  const { id, revision } = snapshot
  const selectedOffering = offeringId.value
  const bookRequest = requestVersion
  const snapshotRequest = snapshotRequestVersion
  const isCurrent = () => isCurrentBook(bookRequest, selectedOffering)
    && snapshotRequest === snapshotRequestVersion && currentSnapshot.value?.id === id
  error.value = ''
  try {
    const result = await gradebookAPI.export(id, format)
    if (isCurrent()) downloadBlob(result.data, `课程成绩-版本${revision}.${format}`)
  } catch (e) { if (isCurrent()) error.value = errorMessage(e) }
}
function columnWeight(category) { return category.columns.reduce((sum, col) => sum + Number(col.weight || 0), 0) }
function cellLabel(cell) { return cell?.score ?? states[cell?.state] ?? '—' }
</script>

<template>
  <AppLayout content-mode="list"><div class="gradebook-page">
    <header class="page-head"><div><h1>课程成绩管理</h1><p class="account-muted">按授课班级设置栏目、核对分项并发布成绩。已发布和归档的版本保留当时的分数与来源。</p></div></header>
    <div class="account-actions"><label class="offering-picker">课程 / 班级 / 学期<DsSelect v-model="offeringId" aria-label="选择成绩所属授课班级" @change="load"><option value="">请选择授课班级</option><option v-for="offering in offerings" :key="offering.id" :value="String(offering.id)">{{ offering.course_title }} · {{ offering.class_name }} · {{ offering.term_name }}</option></DsSelect></label><button class="btn-ghost" :disabled="busy || loading || !offeringId" @click="load">刷新成绩状态</button></div>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <p v-if="message" role="status" class="account-notice">{{ message }}</p>
    <p v-if="loading" role="status">正在加载成绩簿…</p>
    <p v-else-if="!offerings.length" class="account-muted">暂无可访问的授课班级。</p>
    <template v-else-if="data">
      <p v-if="readOnly && !isStudent" class="account-notice">该授课班级或学期已归档，成绩只读。</p>
      <p v-if="viewing && !isStudent" class="account-notice">正在查看历史版本，该版本只读。返回当前草稿后才能修改和发布新成绩。</p>
      <p v-if="isStudent && !currentSnapshot" class="account-notice">教师尚未发布本班成绩。发布后将在这里显示你的分项与总成绩。</p>
      <details v-if="!isStudent && !readOnly && !viewing" class="account-panel" :open="data.revision === 0">
        <summary>栏目与比例 · 当前修订 {{ data.revision }}</summary>
        <p class="account-muted">一级栏目比例合计 100%；各栏目内二级分项比例也合计 100%。分项统一按百分制计算。同一来源只能计入一次。</p>
        <form @submit.prevent="saveConfig">
          <section v-for="(category, index) in config" :key="category.key" class="category-editor">
            <div class="category-heading"><label>一级栏目名称<input v-model="category.title" required maxlength="120" /></label><label>总成绩占比 %<input v-model="category.weight" type="number" min="0" max="100" step="0.0001" required /></label><label>同类任务计分<DsSelect :model-value="category.weight_mode || 'equal'" aria-label="同类任务计分方式" @update:model-value="changeMode(category, $event)"><option value="equal">默认等权（按分项数均分）</option><option value="custom">自定义比例</option></DsSelect></label><button type="button" class="btn-ghost" :disabled="config.length === 1" @click="config.splice(index, 1)">删除栏目</button></div>
            <div v-for="(col, colIndex) in category.columns" :key="col.key" class="column-editor">
              <label>二级分项<input v-model="col.title" required maxlength="120" /></label><label v-if="isCustomMode(category)">栏目内占比 %<input v-model="col.weight" type="number" min="0" max="100" step="0.0001" :required="col.allocation_status !== 'pending'" :placeholder="col.allocation_status === 'pending' ? '待分配' : ''" /></label>
              <span v-else class="equal-note" :title="`${col.title} 与该栏目其它分项等权`">等权分项</span>
              <label>成绩来源<DsSelect v-model="col.source_kind" @change="col.source_id = null"><option v-for="(label, kind) in sourceKinds" :key="kind" :value="kind">{{ label }}</option></DsSelect></label>
              <label v-if="col.source_kind !== 'manual'">本班教学活动<DsSelect v-model.number="col.source_id" required><option :value="null">请选择来源</option><option v-for="source in data.sources.filter(source => source.kind === col.source_kind)" :key="`${source.kind}-${source.id}`" :value="source.id">{{ source.title }}</option></DsSelect></label>
              <label v-if="['assignment', 'experiment'].includes(col.source_kind)">取分规则<DsSelect v-model="col.selection"><option value="highest">最高已发布分</option><option value="latest">最新已发布分</option></DsSelect></label>
              <button type="button" class="btn-ghost" :disabled="category.columns.length === 1" @click="category.columns.splice(colIndex, 1)">删除分项</button>
              <small v-if="col.allocation_status === 'pending'" class="pending-note">待分配：该自动关联分项暂不计入总评，发布正式总评前必须为它确认栏目内比例。</small>
              <small v-if="col.source_kind === 'experiment'" class="account-muted">实验平台已有选定成绩时，始终使用平台选定的已发布版本。</small>
            </div>
            <div class="account-actions"><span v-if="isCustomMode(category)">二级比例合计 {{ columnWeight(category) }}%</span><span v-else class="account-muted">该栏目按分项数量等权计分</span><button type="button" class="btn-ghost" @click="addColumn(category)">添加二级分项</button></div>
          </section>
          <p class="account-muted">改变分项的成绩来源或删除分项，会清理该分项的未发布录入；已有官方版本保留。</p>
          <p v-if="pendingColumns.length" class="account-notice" role="status">有 {{ pendingColumns.length }} 个自动关联分项尚未分配栏目内比例（{{ pendingColumns.map(column => column.title).join('、') }}），确认分配前不能发布正式总评。</p>
          <div class="account-actions"><span>一级比例合计 {{ weightTotal }}%</span><button type="button" class="btn-ghost" @click="addCategory">添加一级栏目</button><button class="btn-primary" :disabled="busy">保存栏目与比例</button></div>
          <div class="account-actions restore-actions"><label>恢复为课程默认的原因<input v-model="restoreReason" maxlength="500" placeholder="说明为什么改用课程模板覆盖本班栏目" /></label><button type="button" class="btn-ghost" :disabled="busy || !restoreReason.trim()" @click="restoreConfirm = true">恢复为课程默认</button></div>
          <p class="account-muted">恢复为课程默认会用课程模板重建本班栏目与比例：本班自动关联的任务分项会被移除，未发布的人工分项草稿一并清除。已发布的官方版本与归档不受影响。</p>
        </form>
      </details>
      <section v-if="rows.length || (!isStudent && data.draft)" class="account-panel">
        <div class="account-actions"><h2>{{ viewing ? `历史版本 ${viewing.revision}` : isStudent ? '我的已发布成绩' : '当前成绩草稿' }}</h2><button v-if="viewing && !isStudent" class="btn-ghost" @click="returnToDraft">返回当前草稿</button><span v-if="!isStudent">{{ rows.length }} 名学生</span></div>
        <div v-if="isStudent && rows[0]" class="student-total"><div><span>最终成绩</span><strong>{{ rows[0].final_score ?? '待完成' }}</strong></div><div><span>计算总分</span><strong>{{ rows[0].calculated_score ?? '待完成' }}</strong></div></div>
        <p v-if="!isStudent && blockers.length && !viewing" class="account-notice">{{ blockers.length }} 项尚待处理：待评分、缺交未确认或整个一级栏目免修时，不能发布。确认缺交会将未提交部分记 0 分；免修只在同一一级栏目内重新分配比例。</p>
        <p class="grade-scroll-hint account-muted">分项较多时，可在表格内横向滑动查看。</p>
        <div class="grade-table-scroll"><table class="account-data"><thead><tr><th>学号 / 姓名</th><th v-for="col in columns" :key="col.key">{{ col.title }}</th><th>计算总分</th><th>最终总分</th><th v-if="!isStudent && !viewing && !readOnly">操作</th></tr></thead><tbody><tr v-for="student in rows" :key="student.student_id"><td>{{ student.student_no || '—' }}<br />{{ student.student_name }}</td><td v-for="col in columns" :key="col.key"><span :title="student.cells[col.key]?.message">{{ cellLabel(student.cells[col.key]) }}</span></td><td>{{ student.calculated_score ?? '待完成' }}</td><td>{{ student.final_score ?? '待完成' }}<small v-if="student.adjustment" class="adjustment-reason">调分：{{ student.adjustment.reason }}</small></td><td v-if="!isStudent && !viewing && !readOnly"><button class="btn-ghost" :aria-label="`编辑${student.student_name}的分项成绩`" @click="editStudent(student)">录入 / 确认</button></td></tr></tbody></table></div>
      </section>
      <section v-if="selectedStudent && !readOnly" class="account-panel">
        <h2>{{ selectedStudent.student_name }} · 分项录入</h2>
        <form class="account-form" @submit.prevent="saveEntries">
          <div v-for="entry in entryForm" :key="entry.column_key" class="entry-editor"><strong>{{ entry.title }}</strong><label>状态<DsSelect v-model="entry.state"><option v-if="entry.source_kind !== 'manual'" value="auto">使用已发布来源成绩</option><option value="scored">{{ entry.source_kind === 'manual' ? '人工已评分' : '人工调分' }}</option><option value="pending">待评分</option><option value="missing">确认缺交（未提交部分记0）</option><option value="exempt">免修</option></DsSelect></label><label v-if="entry.state === 'scored'">百分制分数<input v-model="entry.score" type="number" min="0" max="100" step="0.01" required /></label><label>原因<input v-model="entry.reason" maxlength="500" :required="['missing', 'exempt'].includes(entry.state) || (entry.state === 'scored' && entry.source_kind !== 'manual')" placeholder="缺交、免修或人工调分必须填写" /></label></div>
          <div class="account-actions"><button class="btn-primary" :disabled="busy">保存分项草稿</button><button type="button" class="btn-ghost" @click="selectedStudent = null">取消</button></div>
        </form>
      </section>
      <section v-if="!isStudent && !readOnly && !viewing && data.revision > 0" class="account-panel">
        <h2>总成绩调分</h2><p class="account-muted">调整会记录原分、修改分和原因。待评分仍须处理，调整后需重新发布学生才能看到。</p>
        <form class="adjustment-form" @submit.prevent="adjust()"><label>学生<DsSelect v-model="adjustmentStudent" required><option value="">请选择</option><option v-for="student in data.draft?.rows || []" :key="student.student_id" :value="String(student.student_id)">{{ student.student_no }} · {{ student.student_name }}</option></DsSelect></label><label>最终百分制分数<input v-model="adjustmentScore" type="number" min="0" max="100" step="0.01" required /></label><label>调分原因<input v-model="adjustmentReason" required maxlength="500" /></label><div class="account-actions"><button class="btn-primary" :disabled="busy">保存调分</button><button type="button" class="btn-ghost" :disabled="busy || !adjustmentStudent || !adjustmentReason.trim()" @click="adjust(true)">撤销该学生调分</button></div></form>
      </section>
      <section v-if="!isStudent && !viewing && data.revision > 0 && (!readOnly || data.snapshot)" class="account-panel">
        <h2>审核、发布与归档</h2><p class="account-muted">官方版本 {{ data.snapshot?.revision || '尚未发布' }}。来源后来重新评分会出现在草稿中，已有官方分数保持不变。归档保存最近已发布的完整版本。</p>
        <form class="account-form" @submit.prevent="!readOnly && publish()"><label>发布 / 归档原因<input v-model="reason" required maxlength="500" /></label><div class="account-actions"><button v-if="!readOnly" class="btn-primary" :disabled="busy || blockers.length > 0">发布新成绩版本</button><button type="button" class="btn-ghost" :disabled="busy || !data.snapshot || !reason.trim()" @click="publish(true)">归档官方版本</button></div></form>
      </section>
      <section v-if="currentSnapshot || history.length" class="account-panel">
        <div class="account-actions"><h2>成绩版本与导出</h2><template v-if="currentSnapshot"><button class="btn-ghost" @click="exportGrades('xlsx')">下载 Excel</button><button class="btn-ghost" @click="exportGrades('csv')">下载 CSV</button></template></div>
        <p v-if="isStudent" class="account-muted">仅显示和导出你的成绩。</p>
        <ul class="snapshot-list"><li v-for="snapshot in history" :key="snapshot.id"><button class="btn-ghost" @click="viewSnapshot(snapshot.id)">版本 {{ snapshot.revision }} · {{ snapshot.kind === 'archive' ? '归档' : '已发布' }} · {{ formatDateTime(snapshot.created_at) }}</button><span>{{ snapshot.reason }}</span></li></ul>
      </section>
    </template>
    <ConfirmDialog v-if="restoreConfirm" title="恢复为课程默认？"
      message="将用课程模板重建本班栏目与比例：本班自动关联的任务分项会被移除，未发布的人工分项草稿一并清除。已发布的官方版本与归档不受影响。"
      confirm-text="确认恢复" cancel-text="取消" :danger="true" :busy="busy"
      @confirm="restoreDefault()" @cancel="restoreConfirm = false" />
  </div></AppLayout>
</template>

<style scoped>
.gradebook-page { display: grid; gap: var(--space-5, 20px); }
.offering-picker { display: grid; gap: 8px; max-width: 680px; }
.gradebook-page summary { cursor: pointer; font-weight: 600; padding: 8px 0; }
.category-editor { border-top: 1px solid var(--border); padding: 18px 0; display: grid; gap: 12px; }
.category-heading, .column-editor, .adjustment-form, .entry-editor { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.gradebook-page label { display: grid; gap: 6px; min-width: 150px; flex: 1; }
.gradebook-page input, .gradebook-page .ds-select { width: 100%; min-width: 0; }
.column-editor { padding: 12px; background: var(--surface-alt, #f5f6f5); border-radius: 8px; }
.column-editor small { flex-basis: 100%; }
.entry-editor { border-bottom: 1px solid var(--border); padding-bottom: 14px; }
.entry-editor strong { min-width: 110px; }
.grade-table-scroll { max-width: 100%; overflow-x: auto; }
.grade-table-scroll table { min-width: 650px; }
.student-total { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
.student-total > div { display: grid; gap: 6px; background: var(--surface-alt, #f5f6f5); border-radius: 8px; padding: 12px; }
.student-total span { color: var(--muted); font-size: 13px; }
.student-total strong { font-size: 30px; line-height: 1.2; color: var(--accent); }
.grade-scroll-hint { margin-bottom: 10px; font-size: 12px; }
.adjustment-reason { display: block; font-size: 12px; color: var(--muted); max-width: 180px; white-space: normal; }
.snapshot-list { padding: 0; list-style: none; display: grid; gap: 8px; }
.snapshot-list li { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.snapshot-list span { color: var(--muted); }
.equal-note { align-self: end; padding: 8px 10px; border-radius: 6px; background: var(--surface-subtle, #eef1ee); color: var(--muted); font-size: 13px; }
.pending-note { flex-basis: 100%; color: var(--warning, #8a5b00); }
.restore-actions { border-top: 1px dashed var(--border); padding-top: 12px; }
.restore-actions label { max-width: 420px; }
@media (max-width: 700px) { .gradebook-page label { flex-basis: 100%; } .category-heading, .column-editor { align-items: stretch; } .offering-picker { max-width: 100%; } }
</style>
