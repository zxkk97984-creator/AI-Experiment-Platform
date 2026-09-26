<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { computed, onMounted, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { academicsAPI } from '../../api/academics.js'
import { usersAPI } from '../../api/users.js'
import { useAppStore } from '../../stores/app.js'

const app = useAppStore()
const terms = ref([])
const termOptions = ref([])
const selectedSnapshot = ref(null)
const action = ref(null)
const actionBusy = ref(false)
const selectedStudents = ref([])
const transferTargets = ref([])
const frozen = computed(() => selectedClass.value?.status === 'archived' || termOptions.value.find(t => t.id === selectedClass.value?.academic_term_id)?.status === 'closed')
const classes = ref([])
const selectedClassId = ref(null)
const classStudents = ref([])
const termPage = ref(1)
const classPage = ref(1)
const termFilter = ref('')
const rosterPage = ref(1)
const termTotal = ref(0)
const classTotal = ref(0)
const rosterTotal = ref(0)
const candidates = ref([])
const studentQuery = ref('')
const termForm = ref({ code: '', name: '', start_date: '', end_date: '', status: 'planned' })
const classForm = ref({ academic_term_id: null, code: '', name: '' })
const selectedClass = computed(() => classes.value.find((item) => item.id === Number(selectedClassId.value)) || selectedSnapshot.value)
const termName = (id) => termOptions.value.find((item) => item.id === Number(id))?.name || '—'
const pageSize = 20
const termPageCount = computed(() => Math.max(1, Math.ceil(termTotal.value / pageSize)))
const classPageCount = computed(() => Math.max(1, Math.ceil(classTotal.value / pageSize)))
const rosterPageCount = computed(() => Math.max(1, Math.ceil(rosterTotal.value / pageSize)))

async function loadTerms() {
  const res = await academicsAPI.listTerms({ page: termPage.value, page_size: pageSize })
  terms.value = res.data.items || []
  termTotal.value = Number(res.data.total ?? terms.value.length)
}
async function loadClasses() {
  const res = await academicsAPI.listClasses({ page: classPage.value, page_size: pageSize, academic_term_id: termFilter.value || undefined })
  classes.value = res.data.items || []
  classTotal.value = Number(res.data.total ?? classes.value.length)
}
async function filterClasses() {
  classPage.value = 1
  selectedClassId.value = null
  selectedSnapshot.value = null
  selectedStudents.value = []
  classStudents.value = []
  candidates.value = []
  rosterTotal.value = 0
  await loadClasses()
}
async function loadOptions() {
  const output = []; let page = 1
  while (true) { const { data } = await academicsAPI.listTerms({ page: page++, page_size: 100 }); output.push(...(data.items || [])); if (!data.items?.length || output.length >= (data.total ?? output.length)) break }
  termOptions.value = output
}
async function load() {
  await loadOptions()
  await Promise.all([loadTerms(), loadClasses()])
  if (selectedClassId.value) await loadRoster()
}
async function changeTermPage(nextPage) {
  termPage.value = nextPage
  await loadTerms()
}
async function changeClassPage(nextPage) {
  classPage.value = nextPage
  await loadClasses()
}
async function createTerm() {
  try { await academicsAPI.createTerm(termForm.value); termForm.value = { code: '', name: '', start_date: '', end_date: '', status: 'planned' }; await load(); app.showToast('学期已创建', 'success') }
  catch (e) { app.showToast(e.response?.data?.detail?.message || '创建失败', 'error') }
}
function closeTerm(term) { action.value = { type: 'close', row: term, reason: '' } }
async function createClass() {
  try { await academicsAPI.createClass(classForm.value); classForm.value = { academic_term_id: null, code: '', name: '' }; await load(); app.showToast('教学班已创建', 'success') }
  catch (e) { app.showToast(e.response?.data?.detail?.message || '创建失败', 'error') }
}
function archiveClass(item) { action.value = { type: 'archive', row: item, reason: '' } }
async function selectClass(item) { selectedSnapshot.value = item; selectedStudents.value = []; selectedClassId.value = item.id; rosterPage.value = 1; candidates.value = []; await loadRoster() }
function backToClasses() {
  selectedClassId.value = null
  selectedSnapshot.value = null
  selectedStudents.value = []
  classStudents.value = []
  candidates.value = []
  studentQuery.value = ''
  rosterTotal.value = 0
  rosterPage.value = 1
}
async function loadRoster() {
  const res = await academicsAPI.listClassStudents(selectedClassId.value, { page: rosterPage.value, page_size: pageSize })
  classStudents.value = res.data.items || []
  rosterTotal.value = Number(res.data.total ?? classStudents.value.length)
}
async function changeRosterPage(nextPage) {
  rosterPage.value = nextPage
  await loadRoster()
}
async function searchStudents() {
  const res = await usersAPI.listStudents({ q: studentQuery.value || undefined, page_size: 30, exclude_class_id: selectedClassId.value })
  const existing = new Set(classStudents.value.map((item) => item.id))
  candidates.value = (res.data.items || []).filter((item) => !existing.has(item.id))
}
async function addStudent(student) { await academicsAPI.addClassStudents(selectedClassId.value, [student.id]); await load(); await searchStudents() }
function removeStudent(student) { action.value = { type: 'remove', row: student, reason: '' } }
async function beginTransfer() {
  const rows = []; let page = 1
  while (true) { const { data } = await academicsAPI.listClasses({ academic_term_id: selectedClass.value.academic_term_id, page: page++, page_size: 100 }); rows.push(...data.items); if (!data.items.length || rows.length >= data.total) break }
  transferTargets.value = rows.filter(c => c.id !== selectedClassId.value && c.status === 'active')
  action.value = { type: 'transfer', row: selectedClass.value, reason: '', target: '' }
}
async function applyAction() {
  actionBusy.value = true
  const a = action.value
  try {
    if (a.type === 'close') await academicsAPI.closeTerm(a.row.id)
    if (a.type === 'archive') await academicsAPI.archiveClass(a.row.id)
    if (a.type === 'reopen') await academicsAPI.reopenTerm(a.row.id, { reason: a.reason })
    if (a.type === 'restore') await academicsAPI.restoreClass(a.row.id, { reason: a.reason })
    if (a.type === 'edit_term') await academicsAPI.updateTerm(a.row.id, { name: a.name, code: a.code })
    if (a.type === 'edit_class') await academicsAPI.updateClass(a.row.id, { name: a.name, code: a.code })
    if (a.type === 'remove') await academicsAPI.removeClassStudent(selectedClassId.value, a.row.id)
    if (a.type === 'transfer') await academicsAPI.transferStudents(a.row.id, { target_class_id: Number(a.target), student_ids: selectedStudents.value, reason: a.reason })
    action.value = null; selectedStudents.value = []; await load()
  } catch (e) { app.showToast(e.response?.data?.detail?.message || '操作失败', 'error') }
  finally { actionBusy.value = false }
}
onMounted(async () => { try { await load() } catch { app.showToast('加载失败', 'error') } })
</script>

<template>
  <AppLayout content-mode="list"><section class="academic-page">
    <header><div><p>ACADEMIC OPERATIONS</p><h1>教务管理</h1><span>维护学期、教学班和学生名单，课程人数会自动同步。</span></div><RouterLink to="/admin/accounts">Excel 导入与批次</RouterLink><AppIcon name="course" :size="32" /></header>
    <section class="grid">
      <article class="panel term-panel"><h2>学期</h2><details class="create-disclosure"><summary>新增学期</summary><form class="form-grid" @submit.prevent="createTerm"><label class="academic-field">学期编码<input v-model="termForm.code" required placeholder="编码，如 2026-FALL"/></label><label class="academic-field">学期名称<input v-model="termForm.name" required placeholder="名称，如 2026 秋季学期"/></label><label class="academic-field">开始日期<input v-model="termForm.start_date" required type="date"/></label><label class="academic-field">结束日期<input v-model="termForm.end_date" required type="date"/></label><DsSelect v-model="termForm.status"><option value="planned">计划中</option><option value="active">进行中</option></DsSelect><button class="btn-primary">创建学期</button></form></details>
        <div class="rows"><div v-for="term in terms" :key="term.id"><span><strong>{{ term.name }}</strong><small>{{ term.code }} · {{ term.start_date }} — {{ term.end_date }}</small></span><em :class="term.status">{{ term.status }}</em><button v-if="term.status !== 'closed'" @click="action = { type: 'edit_term', row: term, name: term.name, code: term.code, reason: '' }">编辑</button><button v-if="term.status !== 'closed'" @click="closeTerm(term)">关闭</button><button v-else @click="action = { type: 'reopen', row: term, reason: '' }">重新开放</button></div></div>
        <TeacherPagination v-if="termTotal > 0" :current-page="termPage" :page-count="termPageCount" :total="termTotal" :page-size="pageSize" aria-label="学期列表分页" @change="changeTermPage" />
      </article>
      <article v-if="!selectedClass" class="panel class-panel"><div class="class-panel-head"><h2>教学班</h2><label>学期 <DsSelect v-model="termFilter" aria-label="按学期筛选教学班" @change="filterClasses"><option value="">全部学期</option><option v-for="term in termOptions" :key="term.id" :value="term.id">{{ term.name }}</option></DsSelect></label></div><details class="create-disclosure"><summary>新增教学班</summary><form class="form-grid" @submit.prevent="createClass"><DsSelect v-model="classForm.academic_term_id" required><option :value="null">选择学期</option><option v-for="term in termOptions.filter(t => t.status !== 'closed')" :key="term.id" :value="term.id">{{ term.name }}</option></DsSelect><label class="academic-field">班级编码<input v-model="classForm.code" required placeholder="班级编码"/></label><label class="academic-field">班级名称<input v-model="classForm.name" required placeholder="班级名称"/></label><button class="btn-primary">创建教学班</button></form></details>
        <div class="rows"><div v-for="item in classes" :key="item.id" :class="{ selected: item.id === selectedClassId }" @click="selectClass(item)"><button type="button" class="class-select" @click.stop="selectClass(item)"><strong>{{ item.name }}</strong><small>{{ termName(item.academic_term_id) }} · {{ item.student_count }} 人</small></button><em :class="item.status">{{ item.status }}</em><button v-if="item.status !== 'archived'" @click.stop="action = { type: 'edit_class', row: item, name: item.name, code: item.code, reason: '' }">编辑</button><button v-if="item.status !== 'archived'" @click.stop="archiveClass(item)">归档</button><button v-else @click.stop="action = { type: 'restore', row: item, reason: '' }">恢复</button></div></div>
        <TeacherPagination v-if="classTotal > 0" :current-page="classPage" :page-count="classPageCount" :total="classTotal" :page-size="pageSize" aria-label="教学班列表分页" @change="changeClassPage" />
      </article>
    <section v-if="selectedClass" class="panel roster"><div class="roster-title"><div class="roster-heading"><button type="button" class="back-to-classes" @click="backToClasses"><AppIcon name="back" :size="16" />返回教学班列表</button><h2>{{ selectedClass.name }} · 班级名单</h2><p>{{ termName(selectedClass.academic_term_id) }}</p></div><span>{{ rosterTotal }} 人</span></div>
      <p v-if="frozen" class="account-notice">名单已冻结，已有教学活动仍按各自期限进行。</p>
      <div v-else class="account-actions"><RouterLink :to="`/admin/accounts?class_id=${selectedClassId}`">导入班内学生</RouterLink><button :disabled="!selectedStudents.length" @click="beginTransfer">转移已选 {{ selectedStudents.length }} 人</button></div>
      <div v-if="!frozen" class="search"><input v-model="studentQuery" placeholder="按姓名、学号或账号搜索学生" @keyup.enter="searchStudents"/><button class="btn-primary" @click="searchStudents">搜索并添加</button></div>
      <div v-if="candidates.length" class="candidates"><button v-for="student in candidates" :key="student.id" @click="addStudent(student)">{{ student.real_name }} · {{ student.student_no || student.username }} <b>加入</b></button></div>
      <table class="ds-table"><thead><tr><th>选择</th><th>姓名</th><th>学号</th><th>账号</th><th></th></tr></thead><tbody><tr v-for="student in classStudents" :key="student.id"><td><input v-if="!frozen" v-model="selectedStudents" type="checkbox" :value="student.id" :aria-label="`选择 ${student.real_name}`" /></td><td>{{ student.real_name }}</td><td>{{ student.student_no }}</td><td>{{ student.username }}</td><td><button v-if="!frozen" @click="removeStudent(student)">移出</button></td></tr></tbody></table>
      <TeacherPagination v-if="rosterTotal > 0" :current-page="rosterPage" :page-count="rosterPageCount" :total="rosterTotal" :page-size="pageSize" aria-label="班级名单分页" @change="changeRosterPage" />
    </section>
    </section>
    <div v-if="action" class="modal-backdrop" @click.self="!actionBusy && (action = null)"><section v-dialog-focus class="panel account-panel academic-action-dialog" role="dialog" aria-modal="true" aria-label="确认教务操作" @keydown.esc="!actionBusy && (action = null)">
      <h2>确认教务操作：{{ action.row.name || action.row.real_name }}</h2><p>关闭或归档会冻结教务名单，不删除提交、成绩或现有权限；移出只调整当前班级关系。</p>
      <form class="account-form" @submit.prevent="applyAction">
        <template v-if="action.type.startsWith('edit')"><label>编码<input v-model="action.code" required /></label><label>名称<input v-model="action.name" required /></label></template>
        <label v-if="action.type === 'transfer'">目标班级<DsSelect v-model="action.target" required><option value="">请选择</option><option v-for="c in transferTargets" :key="c.id" :value="c.id">{{ c.name }}</option></DsSelect></label>
        <label>操作原因<input v-model="action.reason" required maxlength="500" /></label>
        <div class="account-actions"><button class="btn-primary" :disabled="actionBusy">确认</button><button type="button" :disabled="actionBusy" @click="action = null">取消</button></div>
      </form>
    </section></div>
  </section></AppLayout>
</template>

<style scoped>
.academic-page{display:grid;gap:20px}header,.panel{border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:none}header{display:flex;justify-content:space-between;align-items:center;padding:24px 28px;background:linear-gradient(135deg,var(--accent-soft),var(--surface))}header p{margin:0;color:var(--accent);font-size:11px;font-weight:700;letter-spacing:.12em}header h1{margin:4px 0;font-size:30px}header span,small{color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.panel{padding:20px}.panel h2{margin:0 0 14px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.form-grid button{min-height:42px}.rows{display:grid;gap:8px;margin-top:16px}.rows>div{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:10px;padding:12px;border:1px solid var(--border);border-radius: var(--radius-md);cursor:pointer}.rows>div.selected{border-color:var(--accent);background:var(--accent-soft)}.rows span{display:grid;gap:3px}.rows em{padding:3px 8px;border-radius: var(--radius-full);background:var(--surface-subtle);font-size:11px;font-style:normal}.rows em.active{color:var(--success);background:var(--success-bg)}.rows button,table button{border:0;background:transparent;color:var(--danger)}.roster-title,.search{display:flex;align-items:center;justify-content:space-between;gap:12px}.roster-title h2,.roster-title p{margin:0}.roster-title>span{padding:5px 12px;border-radius: var(--radius-full);background:var(--accent-soft);color:var(--accent)}.search{margin:18px 0}.search input{flex:1}.candidates{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}.candidates button{padding:8px 10px;border:1px solid var(--border);border-radius: var(--radius-md);background:var(--surface-subtle)}.candidates b{margin-left:8px;color:var(--accent)}table{width:100%;border-collapse:collapse}th,td{padding:11px;border-bottom:1px solid var(--border);text-align:left}@media(max-width:900px){.grid{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}}

/* Navigation uses a narrow rail; class and roster content own the wide column. */
header { padding: 16px 20px; }
header h1 { font-size: var(--text-3xl); }
.grid { grid-template-columns: 300px minmax(0, 1fr); align-items: start; }
.term-panel { grid-row: span 2; }
.roster { grid-column: 2; }
.rows > div { grid-template-columns: minmax(0, 1fr) auto auto auto; }
.rows span { min-width: 0; overflow-wrap: anywhere; }
.term-panel .rows > div { grid-template-columns: 1fr auto auto; }
.term-panel .rows > div > span { grid-column: 1 / -1; }
.class-panel-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.class-panel-head h2 { margin: 0; }
.class-panel-head label { display: flex; align-items: center; gap: 8px; min-width: 0; }
.class-panel-head .ds-select { max-width: 280px; }
.create-disclosure { border-block: 1px solid var(--border); padding-block: 12px; }
.create-disclosure summary { cursor: pointer; color: var(--accent); }
.create-disclosure[open] summary { margin-bottom: 12px; }
.term-panel .form-grid { grid-template-columns: minmax(0, 1fr); }
.form-grid input, .form-grid .ds-select { width: 100%; min-width: 0; }
.rows button { color: var(--fg); }
.roster table { table-layout: fixed; }
.roster th:first-child { width: 60px; }
.roster th:nth-child(2) { width: 22%; }
.roster th:nth-child(3) { width: 24%; }
.roster th:last-child { width: 70px; }
.roster td:last-child { text-align: right; }
.roster td { overflow-wrap: anywhere; }

.academic-field { display: grid; gap: 6px; color: var(--muted); font-size: var(--text-sm); }
.rows .class-select { display: grid; justify-content: start; justify-items: start; gap: 3px; text-align: left; min-width: 0; padding: 0; color: var(--fg); }
.class-select strong, .class-select small { white-space: normal; overflow-wrap: anywhere; }
.grid:has(.roster) .term-panel { grid-column: 1; grid-row: 1; }
.grid:has(.roster) .roster { grid-column: 2; grid-row: 1; }
.rows em { justify-self: start; }

.roster-heading { display: grid; gap: 5px; }
.back-to-classes {
  display: inline-flex;
  align-items: center;
  justify-self: start;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--accent);
  font-size: var(--text-sm);
}
.back-to-classes:hover { border-color: transparent; color: var(--accent-hover); }

.academic-action-dialog { width: min(600px, calc(100vw - 48px)); overflow: auto; }
</style>
