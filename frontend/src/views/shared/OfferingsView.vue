<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import LabAssessmentPanel from '../../components/labs/LabAssessmentPanel.vue'
import GradebookBindingField from '../../components/gradebook/GradebookBindingField.vue'
import { useAuthStore } from '../../stores/auth.js'
import { offeringsAPI } from '../../api/offerings.js'
import client from '../../api/client.js'
import PagedCandidateDialog from '../../components/common/PagedCandidateDialog.vue'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'

const auth = useAuthStore()
const router = useRouter()
const rows = ref([]), terms = ref([])
const termsLoading = ref(false), termsError = ref(''), membersReady = ref(false)
const choices = ref({}), chosenStudents = ref([]), picker = ref(null)
const termGuard = createLatestRequestGuard(), selectionGuard = createLatestRequestGuard()
const historyReview = ref({ items: [], tasks: [], total: 0 }), historyPage = ref(1)
const selected = ref(null), members = ref([]), templates = ref({ tasks: [], experiments: [] })
const busy = ref(false), error = ref(''), notice = ref('')
const createForm = ref({ course_id: '', academic_term_id: '', teaching_class_id: '', teacher_id: '' })
const canCreate = computed(() => Object.values(createForm.value).every(Boolean))
const assignmentTeacher = ref(''), reason = ref(''), memberIds = ref([]), transferTarget = ref('')
const assessmentEditor = ref('')
const selectedExperiment = computed(() => templates.value.experiments.find(e => `${e.kind}:${e.id}` === assessmentEditor.value))
const saveSharedTemplate = async () => true
const ownerId = ref(''), taskKey = ref(''), experimentKey = ref(''), opensAt = ref(''), deadline = ref(''), assessmentId = ref('')
// 向本班发布实验默认计入课程总评（分类预选「实验」），教师可取消或改分类。
const experimentBinding = ref({ enabled: true, category_key: 'labs' })
const prefix = computed(() => `/${auth.role}`)
const otherClasses = computed(() => rows.value.filter(r => selected.value && r.id !== selected.value.id && r.course_id === selected.value.course_id && r.academic_term_id === selected.value.academic_term_id && r.status === 'active'))

async function run(action) {
  busy.value = true; error.value = ''; notice.value = ''
  try { await action() } catch (e) { error.value = e.response?.data?.detail?.message || e.message || '操作失败，请重试' }
  finally { busy.value = false }
}
async function load() {
  rows.value = (await offeringsAPI.list()).data.items
}
async function loadTerms() {
  const ticket = termGuard.begin()
  termsLoading.value = true; termsError.value = ''
  try {
    const found = new Map()
    let page = 1
    while (true) {
      const { data } = await client.get('/academic-terms', { params: { page, page_size: 100 } })
      if (!termGuard.isLatest(ticket)) return
      const items = data.items || []
      const previousCount = found.size
      for (const term of items) found.set(Number(term.id), term)
      if (found.size >= Number(data.total ?? found.size)) break
      if (found.size === previousCount) throw new Error('学期列表未完整加载，请重试')
      page += 1
    }
    terms.value = [...found.values()]
  } catch (e) {
    if (termGuard.isLatest(ticket)) termsError.value = e.response?.data?.detail?.message || e.message || '学期加载失败，请重试'
  } finally {
    if (termGuard.isLatest(ticket)) termsLoading.value = false
  }
}
const courseLabel = row => row.title
const classLabel = row => `${row.name}${row.code ? ` · ${row.code}` : ''}`
const teacherLabel = row => `${row.real_name}${row.username ? ` · ${row.username}` : ''}`
const studentLabel = row => `${row.student_no || row.username || row.id} · ${row.real_name}`
function choiceText(field, fallback = '请选择') {
  const row = choices.value[field]
  return row ? (field === 'course' ? courseLabel(row) : field === 'class' ? classLabel(row) : teacherLabel(row)) : fallback
}
function openPicker(field) {
  if (busy.value || (field === 'class' && !createForm.value.academic_term_id) || (field === 'students' && !membersReady.value)) return
  const teacher = { title: '选择教师', label: teacherLabel, searchPlaceholder: '输入完整姓名或账号精确搜索',
    fetchPage: params => client.get('/users', { params: { ...params, role: 'teacher', status_filter: 'active' } }) }
  const termId = Number(createForm.value.academic_term_id)
  const definitions = {
    course: { title: '选择课程', label: courseLabel, searchPlaceholder: '搜索课程名称或编码', fetchPage: params => client.get('/courses', { params: { ...params, sort_by: 'title' } }) },
    class: { title: '选择教学班', label: classLabel, searchPlaceholder: '搜索当前学期的班级名称或编码', fetchPage: params => client.get('/teaching-classes', { params: { ...params, academic_term_id: termId } }), disabledReason: row => row.status === 'archived' ? '教学班已归档' : '' },
    teacher, assigned: teacher, owner: teacher,
    students: { title: '选择补充学生', label: studentLabel, multiple: true, searchPlaceholder: '搜索姓名、学号或账号', fetchPage: params => client.get('/users/students', { params }), disabledReason: row => members.value.some(member => Number(member.id) === Number(row.id) && member.status === 'active') ? '已在本班' : '' },
  }
  picker.value = { ...definitions[field], field, selected: field === 'students' ? chosenStudents.value : choices.value[field] ? [choices.value[field]] : [] }
}
function confirmPicker(items) {
  const field = picker.value.field
  if (field === 'students') {
    chosenStudents.value = items
    memberIds.value = items.map(row => Number(row.id))
  } else {
    choices.value[field] = items[0]
    const id = items[0]?.id || ''
    if (field === 'course') createForm.value.course_id = id
    if (field === 'class') createForm.value.teaching_class_id = id
    if (field === 'teacher') createForm.value.teacher_id = id
    if (field === 'assigned') assignmentTeacher.value = id
    if (field === 'owner') ownerId.value = id
  }
  picker.value = null
}
watch(() => createForm.value.academic_term_id, () => {
  createForm.value.teaching_class_id = ''
  delete choices.value.class
  if (picker.value?.field === 'class') picker.value = null
})
async function select(row) {
  const ticket = selectionGuard.begin()
  picker.value = null; memberIds.value = []; chosenStudents.value = []; membersReady.value = false
  choices.value.assigned = row.teacher_id ? { id: row.teacher_id, real_name: row.teacher_name || `教师 #${row.teacher_id}` } : null
  choices.value.owner = null
  await run(async () => {
    selected.value = row; assignmentTeacher.value = row.teacher_id || ''; reason.value = ''; ownerId.value = ''; taskKey.value = ''; experimentKey.value = ''; assessmentEditor.value = ''
    members.value = []; templates.value = { tasks: [], experiments: [] }; historyReview.value = { items: [], tasks: [], total: 0 }
    const result = await offeringsAPI.students(row.id)
    if (!selectionGuard.isLatest(ticket)) return
    members.value = result.data.items; membersReady.value = true
    historyPage.value = 1
    if (auth.isAdmin) await loadHistory()
    if (!auth.isStudent) templates.value = (await offeringsAPI.templates(row.course_id)).data
  })
}
async function loadHistory(page = historyPage.value) {
  const response = await offeringsAPI.reviewHistory(selected.value.id, { page, page_size: 20 })
  historyReview.value = response.data
  historyPage.value = page
}
async function changeHistoryPage(offset) {
  await run(() => loadHistory(historyPage.value + offset))
}
async function confirmHistory(item, kind = 'record') {
  await run(async () => {
    const payload = { reason: reason.value }
    if (kind === 'record') {
      if (item.source_offering_id) await offeringsAPI.transferRecord(selected.value.id, item.id, { ...payload, source_offering_id: item.source_offering_id })
      else await offeringsAPI.confirmRecord(selected.value.id, item.id, payload)
    } else await offeringsAPI.confirmTask(selected.value.id, item.kind, item.id, payload)
    await loadHistory(); notice.value = '历史归属已确认，原提交和评分版本保留；已发布成绩档案保持原版本'
  })
}
async function create() {
  if (!createForm.value.course_id || !createForm.value.academic_term_id || !createForm.value.teaching_class_id || !createForm.value.teacher_id) return
  await run(async () => { await offeringsAPI.create(Object.fromEntries(Object.entries(createForm.value).map(([k, v]) => [k, Number(v)]))); notice.value = '授课安排已创建，班级名单已加入'; await load() })
}
async function update(status) {
  await run(async () => {
    const payload = { reason: reason.value, ...(status ? { status } : { teacher_id: Number(assignmentTeacher.value) }) }
    await offeringsAPI.update(selected.value.id, payload); await load(); selected.value = rows.value.find(r => r.id === selected.value.id); notice.value = '授课安排已更新'
  })
}
async function changeOwner() {
  await run(async () => { await offeringsAPI.owner(selected.value.course_id, { teacher_id: Number(ownerId.value), reason: reason.value }); notice.value = '共享课程负责人已设置' })
}
async function addMembers() {
  await run(async () => { await offeringsAPI.addStudents(selected.value.id, { student_ids: memberIds.value.map(Number), reason: reason.value }); members.value = (await offeringsAPI.students(selected.value.id)).data.items; memberIds.value = []; chosenStudents.value = []; notice.value = '实例名单已更新' })
}
async function transfer(student) {
  await run(async () => { await offeringsAPI.transfer(selected.value.id, student.id, { destination_offering_id: Number(transferTarget.value), reason: reason.value }); members.value = (await offeringsAPI.students(selected.value.id)).data.items; notice.value = '转班完成，历史成绩保留原归属' })
}
async function copyTask() {
  await run(async () => { const [kind, id] = taskKey.value.split(':'); const result = await offeringsAPI.copyTask(selected.value.id, { kind, source_id: Number(id) }); notice.value = '班级任务已生成草稿，可继续设置并发布'; router.push(`${prefix.value}/${kind === 'exam' ? 'exams' : 'assignments'}/${result.data.id}/edit`) })
}
async function release() {
  await run(async () => { const [entry_kind, id] = experimentKey.value.split(':'); await offeringsAPI.release(selected.value.id, { entry_kind, entry_id: Number(id), assessment_id: assessmentId.value ? Number(assessmentId.value) : null, opens_at: opensAt.value ? new Date(opensAt.value).toISOString() : null, deadline: deadline.value ? new Date(deadline.value).toISOString() : null, gradebook_binding: experimentBinding.value }); notice.value = '实验已按所选模板与评测版本向本班发布'; assessmentEditor.value = experimentKey.value })
}
onMounted(() => { run(load); if (auth.isAdmin) loadTerms() })
onBeforeUnmount(() => { termGuard.invalidate(); selectionGuard.invalidate() })
</script>

<template>
  <AppLayout content-mode="list">
    <main class="offering-page">
      <header><h1>{{ auth.isAdmin ? '授课安排' : '我的教学班' }}</h1><p>共享课程内容，按教学班管理任务、学生和成绩。</p></header>
      <p v-if="error" role="alert" class="error">{{ error }}</p><p v-if="notice" role="status" class="notice">{{ notice }}</p>
      <form v-if="auth.isAdmin" class="panel grid offering-create" @submit.prevent="create">
        <header class="wide create-heading"><div class="create-mark" aria-hidden="true">+</div><div><h2>新增授课安排</h2><p>选择课程、学期、教学班和授课教师，创建后自动加入班级名单。</p></div></header>
        <div class="candidate-field"><span>课程</span><button class="create-picker" type="button" aria-label="选择课程" :disabled="busy" @click="openPicker('course')"><span>{{ choiceText('course') }}</span><svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m5 3 5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" /></svg></button><small>可为不同教学班安排同一门课程</small></div>
        <label class="create-field">学期<select v-model="createForm.academic_term_id" required :disabled="busy || termsLoading"><option value="">{{ termsLoading ? '正在加载学期…' : '请选择' }}</option><option v-for="t in terms" :key="t.id" :value="t.id" :disabled="t.status === 'closed'">{{ t.name }}{{ t.status === 'closed' ? '（已关闭）' : '' }}</option></select><small>已关闭的学期不接受新增安排</small></label>
        <p v-if="termsError" class="error wide" role="alert">{{ termsError }} <button type="button" @click="loadTerms">重试学期加载</button></p>
        <div class="candidate-field"><span>教学班</span><button class="create-picker" type="button" aria-label="选择教学班" :disabled="busy || !createForm.academic_term_id" @click="openPicker('class')"><span>{{ choiceText('class', createForm.academic_term_id ? '请选择' : '请先选择学期') }}</span><svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m5 3 5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" /></svg></button><small>仅显示所选学期的教学班</small></div>
        <div class="candidate-field"><span>授课教师</span><button class="create-picker" type="button" aria-label="选择授课教师" :disabled="busy" @click="openPicker('teacher')"><span>{{ choiceText('teacher') }}</span><svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m5 3 5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" /></svg></button><small>负责本班任务、学生与成绩管理</small></div>
        <footer class="wide create-footer"><span>{{ canCreate ? '已选齐，可创建授课安排' : '请选择全部四项后创建' }}</span><button class="create-submit" :disabled="busy || !canCreate">{{ busy ? '正在处理…' : '创建并加入班级名单' }}</button></footer>
      </form>
      <section class="cards" aria-label="教学班列表">
        <button v-for="row in rows" :key="row.id" class="class-card" :class="{ selected: selected?.id === row.id }" :disabled="busy" @click="select(row)"><strong>{{ row.course_title }}</strong><span>{{ row.term_name }} · {{ row.class_name }}</span><span>{{ row.teacher_name || '待分配教师' }} · {{ row.status === 'active' ? '教学中' : row.status === 'archived' ? '已归档' : '待确认' }}</span></button>
        <p v-if="!busy && !rows.length">暂无授课安排，请联系管理员分配。</p>
      </section>
      <section v-if="selected" class="panel">
        <h2>{{ selected.class_name }} · {{ selected.course_title }}</h2>
        <div class="actions"><RouterLink :to="`${prefix}/materials?course_id=${selected.course_id}`">课程资料</RouterLink><RouterLink :to="`${prefix}/gradebook?offering_id=${selected.id}`">课程成绩簿</RouterLink><RouterLink v-if="auth.isStudent" :to="`/student/courses/${selected.course_id}?offering_id=${selected.id}`">进入课程</RouterLink></div>
        <div v-if="auth.isAdmin" class="grid">
          <label>变更原因<input v-model="reason" placeholder="授权、转班或负责人变更原因" required maxlength="500"></label>
          <div class="candidate-field"><span>负责教师</span><button type="button" aria-label="选择负责教师" :disabled="busy" @click="openPicker('assigned')">{{ choiceText('assigned') }}</button></div>
          <div class="actions"><button :disabled="busy || !reason" @click="update()">移交授课</button><button :disabled="busy || !reason" @click="update(selected.status === 'active' ? 'archived' : 'active')">{{ selected.status === 'active' ? '归档班级授课' : '确认并激活' }}</button></div>
          <div class="candidate-field"><span>共享内容负责人</span><button type="button" aria-label="选择共享内容负责人" :disabled="busy" @click="openPicker('owner')">{{ choiceText('owner') }}</button></div><button :disabled="busy || !ownerId || !reason" @click="changeOwner">设置课程负责人</button>
          <div class="candidate-field"><span>补充实例学生</span><button type="button" aria-label="选择补充学生" :disabled="busy || !membersReady" @click="openPicker('students')">{{ memberIds.length ? `已选 ${memberIds.length} 位学生，点击修改` : '搜索并选择学生' }}</button><ul v-if="chosenStudents.length" class="chosen-students"><li v-for="s in chosenStudents" :key="s.id">{{ studentLabel(s) }}</li></ul></div><button :disabled="busy || !membersReady || !memberIds.length || !reason" @click="addMembers">补充名单</button>
          <label>转入教学班<select v-model="transferTarget"><option value="">请选择同课程同学期班级</option><option v-for="o in otherClasses" :key="o.id" :value="o.id">{{ o.class_name }}</option></select></label>
        </div>
        <section v-if="auth.isAdmin" class="history-review">
          <h3>历史归属核对</h3><p>核对名单后填写变更原因，再确认历史记录。跨班历史任务保留原记录，可在成绩簿人工核对录分并记录来源。成绩转入要求两班实验模板和评测版本一致，已归档实例只读。</p>
          <ul><li v-for="item in historyReview.items" :key="`record-${item.id}`"><span>{{ item.title }} · {{ item.student_no }} · {{ item.student_name }} · 模板 v{{ item.template_version_id }}</span><button :disabled="busy || !reason.trim() || !item.can_confirm || selected.status !== 'active'" @click="confirmHistory(item)">{{ item.source_offering_id ? '明确转入历史实验成绩' : '确认实验归属' }}</button><small v-if="!item.can_confirm">需先确认学生名单归属</small></li></ul>
          <ul><li v-for="item in historyReview.tasks" :key="`${item.kind}-${item.id}`"><span>{{ item.kind === 'exam' ? '考试' : '作业' }} · {{ item.title }} · {{ item.participants }} 位历史参与者</span><button :disabled="busy || !reason.trim() || !item.can_confirm || selected.status !== 'active'" @click="confirmHistory(item, 'task')">确认整项任务归属</button><small v-if="!item.can_confirm">{{ item.unmatched }} 位参与者未归属本班，不可整项转入</small></li></ul>
          <p v-if="!historyReview.items.length && !historyReview.tasks.length">当前页没有待核对的历史数据。</p>
          <div class="actions"><button :disabled="busy || historyPage <= 1" @click="changeHistoryPage(-1)">上一页</button><span>第 {{ historyPage }} 页</span><button :disabled="busy || (historyPage * 20 >= Math.max(historyReview.total || 0, ...Object.values(historyReview.task_totals || {})))" @click="changeHistoryPage(1)">下一页</button></div>
        </section>
        <h3>{{ auth.isStudent ? '我的归属' : '实例名单' }}</h3>
        <div class="table-wrap"><table><thead><tr><th>学号</th><th>姓名</th><th>状态</th><th v-if="auth.isAdmin">操作</th></tr></thead><tbody><tr v-for="m in members" :key="m.id"><td>{{ m.student_no || '—' }}</td><td>{{ m.real_name }}</td><td>{{ m.status === 'active' ? '在班' : '已转出' }}</td><td v-if="auth.isAdmin"><button :disabled="busy || !reason || !transferTarget || m.status !== 'active'" @click="transfer(m)">转班</button></td></tr></tbody></table></div>
        <div v-if="!auth.isStudent && selected.status === 'active'" class="grid">
          <h3 class="wide">安排本班教学任务</h3>
          <label>共享任务模板<select v-model="taskKey"><option value="">请选择</option><option v-for="t in templates.tasks" :key="`${t.kind}:${t.id}`" :value="`${t.kind}:${t.id}`">{{ t.kind === 'exam' ? '考试' : '作业' }} · {{ t.title }}</option></select></label><button :disabled="busy || !taskKey" @click="copyTask">复制为本班任务</button>
          <label>实验模板<select v-model="experimentKey" @change="assessmentId = ''"><option value="">请选择</option><option v-for="e in templates.experiments" :key="`${e.kind}:${e.id}`" :value="`${e.kind}:${e.id}`">{{ e.title }}</option></select></label>
          <label>评测版本<select v-model="assessmentId"><option value="">人工评分</option><option v-for="a in templates.experiments.find(e => `${e.kind}:${e.id}` === experimentKey)?.assessments || []" :key="a.id" :value="a.id">评测 v{{ a.version }}</option></select></label>
          <label>开放时间<input v-model="opensAt" type="datetime-local"></label><label>截止时间<input v-model="deadline" type="datetime-local"></label><GradebookBindingField v-model="experimentBinding" class="wide" :offering-id="selected.id" default-key="labs" :disabled="busy" /><button :disabled="busy || !experimentKey" @click="release">发布本班实验</button>
          <label class="wide">本班实验评分配置<select v-model="assessmentEditor"><option value="">请选择已向本班发布的实验</option><option v-for="e in templates.experiments" :key="`${e.kind}:${e.id}`" :value="`${e.kind}:${e.id}`">{{ e.title }}</option></select></label>
        </div>
        <LabAssessmentPanel v-if="selectedExperiment && !auth.isStudent" :key="`${selected.id}:${assessmentEditor}`" :offering-id="selected.id" :kind="selectedExperiment.kind" :entry-id="selectedExperiment.id" :cells="selectedExperiment.cells" :environment-id="selectedExperiment.environment_version_id" :save-template="saveSharedTemplate" :template-signature="String(selectedExperiment.template_version_id)" />
      </section>
    </main>
    <PagedCandidateDialog v-if="picker" :key="picker.field" :title="picker.title" :fetch-page="picker.fetchPage" :selected="picker.selected" :multiple="picker.multiple" :label="picker.label" :disabled-reason="picker.disabledReason" :search-placeholder="picker.searchPlaceholder" @confirm="confirmPicker" @close="picker = null" />
  </AppLayout>
</template>

<style scoped>
.candidate-field{display:grid;gap:8px;min-width:0}.candidate-field button{text-align:left;overflow-wrap:anywhere}.chosen-students{list-style:none;padding:0;margin:0;display:flex;gap:8px;flex-wrap:wrap;max-height:120px;overflow:auto}.chosen-students li{border:1px solid var(--border);padding:4px 8px;overflow-wrap:anywhere;max-width:100%}
.offering-page{max-width:none;margin:0;padding:0;display:grid;gap:24px}header p{color:var(--text-secondary,#64748b);margin-top:8px}.panel{background:var(--surface,#fff);border:1px solid var(--border,#e2e8f0);border-radius:14px;padding:24px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:end}.wide{grid-column:1/-1}.grid h2,.grid h3{margin:0}.grid label{display:grid;gap:8px;color:var(--text-secondary,#475569)}input,select{width:100%;min-width:0;padding:10px;border:1px solid var(--border,#cbd5e1);border-radius:8px;background:var(--surface,#fff)}button{padding:10px 16px;border:1px solid var(--border,#cbd5e1);border-radius:8px;background:var(--surface,#fff);cursor:pointer;color:var(--text-primary,#0f172a)}button:disabled{opacity:.55;cursor:default}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}.class-card{display:grid;text-align:left;gap:10px;padding:20px}.class-card span{font-size:13px;color:var(--text-secondary,#64748b)}.selected{border-color:var(--accent,#2563eb);box-shadow:0 0 0 1px var(--accent,#2563eb)}.actions{display:flex;flex-wrap:wrap;gap:16px;margin:16px 0}.history-review{margin:24px 0}.history-review p,.history-review small{color:var(--text-secondary,#64748b)}.history-review ul{padding:0;list-style:none}.history-review li{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--border)}.history-review li span{flex:1;min-width:180px}.error{color:#b91c1c}.notice{color:#047857}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;margin-bottom:24px}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--border,#e2e8f0)}@media(max-width:640px){.offering-page{padding:0}.grid{grid-template-columns:1fr}.panel{padding:16px}}

.offering-create { gap: 24px; align-items: start; border-radius: var(--radius-lg); }
.create-heading { display: flex; align-items: flex-start; gap: 12px; }
.create-mark { display: grid; place-items: center; flex: 0 0 40px; height: 40px; border-radius: var(--radius-md); background: var(--accent-soft); color: var(--accent); font-size: 24px; }
.create-heading h2 { font-size: 20px; line-height: 1.4; }
.create-heading p { margin: 6px 0 0; color: var(--muted); font-size: var(--text-sm); }
.offering-create .candidate-field, .offering-create .create-field { gap: 8px; color: var(--fg); font-weight: 500; }
.offering-create .create-picker, .offering-create select { min-height: 44px; padding: 10px 12px; font-family: var(--font-body); font-size: var(--text-base); line-height: 1.5; border-radius: var(--radius-md); }
.offering-create select { height: 44px; }
.offering-create .create-picker { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.create-picker span { min-width: 0; overflow-wrap: anywhere; }
.create-picker svg { flex-shrink: 0; color: var(--muted); }
.offering-create .create-picker:disabled, .offering-create select:disabled { background: var(--surface-sunken); }
.offering-create .create-picker:focus-visible, .offering-create select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.offering-create small { color: var(--muted); font-size: var(--text-sm); font-weight: 400; line-height: 1.5; }
.create-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; border-top: 1px solid var(--border); padding-top: 20px; }
.create-footer > span { color: var(--muted); font-size: var(--text-sm); }
.offering-create .create-submit { min-height: 44px; background: var(--accent); border-color: var(--accent); color: var(--surface); font-family: var(--font-body); font-size: var(--text-base); font-weight: 600; }
.offering-create .create-submit:disabled { background: var(--surface-subtle); border-color: var(--border); color: var(--muted); opacity: 1; }
@media (max-width: 640px) { .create-footer { align-items: stretch; flex-direction: column; } .create-submit { width: 100%; } }
</style>
