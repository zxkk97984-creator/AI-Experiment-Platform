<script setup>
// QuestionEditView：教师端「编程题目编辑」页（IDE 风格布局重构）
//
// 布局：顶部作业运行设置（环境/导入规则/发布范围） + 题目列表
//       + 下方题目编辑主区（基础信息 → 运行参数 → 代码模板 → 测试用例 → AI 评分配置）
// 题目统一继承作业环境与导入规则，不再提供题目级覆盖。
// 数据契约不变：public_cases 仍为 JSON 数组、hidden_tests 仍为 pytest 代码字符串提交。
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import AIQuestionConfig from '../../components/ai/AIQuestionConfig.vue'
import AiConfigForm from '../../components/ai/AiConfigForm.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import EnvironmentProfilePicker from '../../components/common/EnvironmentProfilePicker.vue'
import TaskAudiencePicker from '../../components/teacher/TaskAudiencePicker.vue'
import QeMarkdownEditor from '../../components/teacher/question-editor/QeMarkdownEditor.vue'
import QeCodeEditor from '../../components/teacher/question-editor/QeCodeEditor.vue'
import QeTestCases from '../../components/teacher/question-editor/QeTestCases.vue'
import GradingVersionPanel from '../../components/grading/GradingVersionPanel.vue'
import { assignmentsAPI } from '../../api/assignments.js'
import { environmentsAPI } from '../../api/environments.js'
import { judgeAPI } from '../../api/judge.js'
import { aiGradingAPI } from '../../api/aiGrading.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime, fromDateTimeLocal, parseApiDateTime, toDateTimeLocal } from '../../utils/format.js'
import { taskEditorReturn } from '../../utils/taskEditorNavigation.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const assignment = ref(null)
const returnNavigation = computed(() => taskEditorReturn({ role: route.meta?.role, kind: 'assignment', task: assignment.value, query: route.query }))
const questions = ref([])
const loading = ref(true)

// ── 编辑状态：null = 未选择题目；'new' = 新建；数字 = 编辑已有题目 ──
const activeId = ref(null)
const editingId = ref(null)  // 编辑中的题目 id（null = 新建）
const formKey = ref(0)       // 强制 QeTestCases 在加载/重置时重挂
const lastSavedAt = ref(null)
const saving = ref(false)
const publishing = ref(false)      // 发布作业进行中（防重复点击）
const confirmPublish = ref(false)  // 发布作业确认弹窗
const runResult = ref(null)
const fullscreenCode = ref(false)
const scheduleDueLocal = ref('')
const scheduleSaving = ref(false)
const scheduleConfirmOpen = ref(false)
const pendingScheduleDue = ref(null)

// ── AI 评分配置草稿（新建题无 id 时使用；随「保存题目」一起提交） ──
// 新建题默认 legacy（显式传值，不依赖后端默认 active，避免未开 AI 却 503）
const aiDraft = ref({ grading_mode: 'legacy', teacher_constraints: {}, reference_solution: '', test_groups: [], score_cap_rules: [] })
const aiDraftDirty = ref(false)      // 草稿有未保存修改（切题/创建成功时据此处理）
const aiFormKey = ref(0)             // 草稿表单重挂 key（重置草稿后按默认值重新初始化）
const aiDraftMsg = ref('')           // 草稿侧 AI 提示（如：先保存题目再生成测试组）

function resetAiDraft() {
 aiDraft.value = { grading_mode: 'legacy', teacher_constraints: {}, reference_solution: '', test_groups: [], score_cap_rules: [] }
 aiDraftDirty.value = false
 aiFormKey.value++
 aiDraftMsg.value = ''
}

// 草稿编辑回调：v-model 数据上抛并置脏标记
function onAiDraftChange(v) { aiDraft.value = v; aiDraftDirty.value = true }

// 草稿模式无 questionId，AI 生成测试组端点无法寻址题目：提示先保存
function onDraftGenerateTestGroups() {
  aiDraftMsg.value = '请先保存题目后再生成测试组'
}

// 切题边界：新建题草稿未保存时切换到其他题目，提示已丢弃并重置（防草稿串题）
function discardAiDraftIfDirty() {
 if (activeId.value === 'new' && aiDraftDirty.value) {
   app.showToast('当前新题的 AI 配置草稿未保存，已丢弃', 'error')
   resetAiDraft()
 }
}

// 发布前 Rubric 门禁提示：当前题 shadow/active 时提前提醒（后端 503 仍兜底）
const currentGradingMode = computed(() => {
 if (!editingId.value) return aiDraft.value.grading_mode  // 新建草稿模式
 const q = questions.value.find((x) => x.id === editingId.value)
 return q?.grading_mode || 'legacy'
})
const aiGateNotice = computed(() => {
 if (currentGradingMode.value === 'legacy') return ''
 return `当前题目为 ${currentGradingMode.value} 模式，若 Rubric 未生成并锁定，发布将被后端拒绝。`
})
const publishDeadlineNotice = computed(() => assignment.value?.due_at
  ? ` 当前截止时间：${formatDateTime(assignment.value.due_at)}。`
  : ' 当前未设置截止时间，学生可持续提交。')

const form = ref({
 title: '', description: '', function_name: '', signature: '',
 starter_code: '', public_cases: '[]', hidden_tests: '',
 time_limit_ms: 10000, memory_limit_mb: 256,
})

// ── 作业默认环境与白名单（draft 可编辑；发布后绑定不可变） ──────────
const envOptions = ref([])
const assignmentEnvId = ref(null)
const historicalEnvLoads = new Map()
const assignmentPolicy = ref('unrestricted')
const assignmentAllowedImports = ref([])
const audienceMode = ref('all_enrolled')
const audienceClassIds = ref([])
const audienceWhitelistIds = ref([])
const audienceExcludedIds = ref([])
const audienceSaving = ref(false)
const assignmentDraft = computed(() => assignment.value?.status === 'draft')
// TASK-020：AI 服务状态（enabled/ready）——禁用时显示治理门提示，AI 动作被服务端拒绝前先行禁用
const aiEnabled = ref(false)
const aiReady = ref(false)
const aiDisabledNotice = computed(() => {
  if (aiEnabled.value) return ''
  return 'AI 智能评分当前未启用（未完成数据治理审批）——题目仍可使用人工评分流程，AI 相关功能暂时不可用。'
})
const aiKeyMissingNotice = computed(() => {
  if (!aiEnabled.value || aiReady.value) return ''
  return 'AI 智能评分已启用但未配置 API Key——AI 相关功能暂时不可用，请联系管理员配置 DAI_AI_API_KEY。'
})

const envById = (id) => envOptions.value.find((o) => o.environment_version_id === id) || null
const envImportCandidates = (envId) => {
 const env = envById(envId)
 if (!env) return []
 const seen = new Set()
 const names = []
 for (const p of env.packages || []) {
   for (const name of p.import_names || []) {
     if (!seen.has(name)) { seen.add(name); names.push(name) }
   }
 }
 return names
}

// 所有题目统一继承作业默认环境与导入规则（不再提供题目级覆盖）
const effectiveEnvId = computed(() => assignmentEnvId.value)
const effectiveEnv = computed(() => envById(effectiveEnvId.value))
const memoryWarning = computed(() => {
 if (!effectiveEnv.value) return ''
 if (form.value.memory_limit_mb < effectiveEnv.value.minimum_memory_mb) {
   return `内存上限 ${form.value.memory_limit_mb} MB 低于环境最低内存 ${effectiveEnv.value.minimum_memory_mb} MB，发布将被阻止`
 }
 return ''
})
const assignmentPolicyLabel = computed(() => {
 if (assignmentPolicy.value !== 'restricted') return '不限制'
 if (!assignmentAllowedImports.value.length) return '限定白名单（空）'
 return `限定白名单：${assignmentAllowedImports.value.join('、')}`
})
const assignmentMismatch = computed(() => {
 if (assignmentPolicy.value !== 'restricted' || assignmentAllowedImports.value.length === 0) return ''
 const installed = new Set(envImportCandidates(assignmentEnvId.value))
 const missing = assignmentAllowedImports.value.filter((name) => !installed.has(name))
 return missing.length ? `注意：${missing.join('、')} 未在作业环境安装` : ''
})

// ── 函数签名 → 函数名自动解析（合并输入框后 function_name 仍正常提交） ─
const fnParseState = ref('')
watch(() => form.value.signature, (sig) => {
 const m = /^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/.exec(sig || '')
 if (m) {
   form.value.function_name = m[1]
   fnParseState.value = `已识别函数名「${m[1]}」`
 } else if (sig && sig.trim()) {
   fnParseState.value = '未识别到 def 签名，请按 def xxx(...) 格式填写'
 } else {
   fnParseState.value = ''
 }
})

// 公开样例数组 ↔ 表单 JSON 字符串（提交时仍按原字段契约）
const publicCasesArr = computed({
 get: () => {
   try { return JSON.parse(form.value.public_cases) } catch { return [] }
 },
 set: (arr) => { form.value.public_cases = JSON.stringify(arr) },
})

async function appendHistoricalEnvironment(versionId) {
 if (!versionId || envOptions.value.some((option) => option.environment_version_id === versionId)) return
 if (typeof environmentsAPI.getVersionSummary !== 'function') return
 if (!historicalEnvLoads.has(versionId)) {
   historicalEnvLoads.set(versionId, environmentsAPI.getVersionSummary(versionId).then((res) => {
     if (res.data?.environment_version_id && !envOptions.value.some((option) => option.environment_version_id === versionId)) {
       envOptions.value = [...envOptions.value, { ...res.data, historical: true }]
     }
   }).catch(() => {}).finally(() => historicalEnvLoads.delete(versionId)))
 }
 await historicalEnvLoads.get(versionId)
}

async function fetchEnv() {
 try {
   const res = await environmentsAPI.listAvailable()
   envOptions.value = res.data || []
   await appendHistoricalEnvironment(assignmentEnvId.value)
 } catch { /* 环境加载失败不阻塞题目编辑 */ }
}

// fetch() and fetchEnv() start together.  If the assignment arrives after
// the available list, append the locked historical version once the list is
// ready instead of allowing the two requests to overwrite each other.
watch(
 () => [assignmentEnvId.value, envOptions.value.length],
 () => {
   if (assignmentEnvId.value && envOptions.value.length) void appendHistoricalEnvironment(assignmentEnvId.value)
 },
)

async function fetch() {
 loading.value = true
 try {
   const [aRes, qRes] = await Promise.all([
     assignmentsAPI.get(route.params.id),
     assignmentsAPI.getQuestions(route.params.id),
   ])
   assignment.value = aRes.data
   scheduleDueLocal.value = toDateTimeLocal(aRes.data.due_at)
   questions.value = qRes.data.items || qRes.data
   assignmentEnvId.value = aRes.data.environment_version_id ?? null
   await appendHistoricalEnvironment(assignmentEnvId.value)
   assignmentPolicy.value = aRes.data.import_policy_mode || 'unrestricted'
   assignmentAllowedImports.value = [...(aRes.data.allowed_imports || [])]
   audienceMode.value = aRes.data.audience_mode || 'all_enrolled'
   audienceClassIds.value = [...(aRes.data.audience_class_ids || [])]
   audienceWhitelistIds.value = [...(aRes.data.whitelist_student_ids || [])]
   audienceExcludedIds.value = [...(aRes.data.excluded_student_ids || [])]
   if (!assignmentEnvId.value && envOptions.value.length) {
     assignmentEnvId.value = envOptions.value[0].environment_version_id
   }
 } catch { app.showToast('加载失败', 'error') }
 finally {
   loading.value = false
   // 默认进入「新建第一道题目」编辑态：作业尚无题目时自动开始新题（避免停在空状态）
   if (!activeId.value && questions.value.length === 0) startNew()
 }
}

function scheduleNeedsConfirmation(nextDue) {
 if (!assignment.value || assignment.value.status !== 'published') return false
 const currentDue = assignment.value.due_at
 if (nextDue === null) return currentDue != null
 const nextTime = parseApiDateTime(nextDue).getTime()
 const currentTime = currentDue ? parseApiDateTime(currentDue).getTime() : null
 return nextTime <= Date.now()
   || (currentTime != null && !Number.isNaN(currentTime) && nextTime < currentTime)
}

const scheduleConfirmMessage = computed(() => {
 if (pendingScheduleDue.value === null) return '清空截止时间后，学生将可以长期运行和提交这份作业。确定继续吗？'
 if (parseApiDateTime(pendingScheduleDue.value).getTime() <= Date.now()) {
   return '新的截止时间已经过去，保存后学生将立即无法运行或提交。确定继续吗？'
 }
 return '新的截止时间早于当前设置，可能缩短学生作答时间。确定继续吗？'
})

async function requestScheduleSave() {
 const nextDue = fromDateTimeLocal(scheduleDueLocal.value)
 if (scheduleNeedsConfirmation(nextDue)) {
   pendingScheduleDue.value = nextDue
   scheduleConfirmOpen.value = true
   return
 }
 await saveSchedule(nextDue)
}

async function saveSchedule(nextDue = pendingScheduleDue.value) {
 if (scheduleSaving.value) return
 scheduleSaving.value = true
 try {
   const res = await assignmentsAPI.update(route.params.id, { due_at: nextDue })
   assignment.value = res.data
   scheduleDueLocal.value = toDateTimeLocal(res.data.due_at)
   scheduleConfirmOpen.value = false
   pendingScheduleDue.value = null
   app.showToast('截止时间已更新', 'success')
 } catch (e) {
   app.showToast(e.response?.data?.detail?.message || '截止时间更新失败', 'error')
 } finally {
   scheduleSaving.value = false
 }
}

async function saveAssignmentAudience() {
  audienceSaving.value = true
  try {
    const res = await assignmentsAPI.update(route.params.id, {
      audience_mode: audienceMode.value,
      audience_class_ids: audienceClassIds.value,
      whitelist_student_ids: audienceWhitelistIds.value,
      excluded_student_ids: audienceExcludedIds.value,
    })
    assignment.value = res.data
    app.showToast('发布范围已保存', 'success')
  } catch (e) {
    app.showToast(e.response?.data?.detail?.message || '发布范围保存失败', 'error')
  } finally {
    audienceSaving.value = false
  }
}

function onAudienceImported() { fetch() }

// 保存作业默认环境与白名单（发布后绑定不可变，后端 409）
async function saveAssignmentEnv() {
 try {
   const res = await assignmentsAPI.update(route.params.id, {
     environment_version_id: assignmentEnvId.value,
     import_policy_mode: assignmentPolicy.value,
     allowed_imports: assignmentPolicy.value === 'restricted' ? [...assignmentAllowedImports.value] : [],
   })
   assignment.value = res.data
   app.showToast('作业环境设置已保存', 'success')
 } catch (e) {
   app.showToast(e.response?.data?.detail?.message || '保存失败（已发布作业的环境设置不可修改）', 'error')
   fetch()
 }
}

function toggleAssignmentImport(name) {
 const idx = assignmentAllowedImports.value.indexOf(name)
 if (idx >= 0) assignmentAllowedImports.value.splice(idx, 1)
 else assignmentAllowedImports.value.push(name)
}

function resetQuestionForm() {
 form.value = {
   title: '', description: '', function_name: '', signature: '',
   starter_code: '', public_cases: '[]', hidden_tests: '',
   time_limit_ms: 10000, memory_limit_mb: 256,
 }
 editingId.value = null
}

// 添加新题目：清空表单进入编辑区
function startNew() {
 discardAiDraftIfDirty()
 resetAiDraft()
 resetQuestionForm()
 activeId.value = 'new'
 formKey.value++
 runResult.value = null
 window.scrollTo({ top: 0, behavior: 'smooth' })
}

// 编辑已有题目 → 回填表单（环境与导入规则始终继承作业，不回填旧覆盖值）
function openEdit(q) {
 discardAiDraftIfDirty()
 resetAiDraft()
 editingId.value = q.id
 activeId.value = q.id
 form.value = {
   title: q.title || '', description: q.description || '', function_name: q.function_name || '',
   signature: q.signature || '', starter_code: q.starter_code || '',
   public_cases: JSON.stringify(q.public_cases || [], null, 2), hidden_tests: q.hidden_tests || '',
   time_limit_ms: q.time_limit_ms ?? 10000, memory_limit_mb: q.memory_limit_mb ?? 256,
 }
 formKey.value++
 runResult.value = null
 window.scrollTo({ top: 0, behavior: 'smooth' })
}

// 保存草稿（创建或更新），返回是否成功
async function submitQuestion() {
 if (!form.value.title || !form.value.function_name) {
   app.showToast('请填写标题和函数名', 'error'); return false
 }
 saving.value = true
 try {
   let publicCases = []
   try { publicCases = JSON.parse(form.value.public_cases) }
   catch { app.showToast('公开样例 JSON 格式错误', 'error'); saving.value = false; return false }
   const payload = {
     ...form.value,
     public_cases: publicCases,
     // 所有题目统一继承作业环境与导入规则，不再提交题目级覆盖值
     environment_version_id: null,
     import_policy_mode: 'inherit',
     allowed_imports: [],
   }
   // 新建题：AI 草稿字段随 payload 一起提交（显式传 grading_mode，默认 legacy，
   // 不依赖后端默认 active；其余 AI 字段为 create 链路前瞻，落库仍走独立接口）
   if (!editingId.value) {
     payload.grading_mode = aiDraft.value.grading_mode
     payload.teacher_constraints = aiDraft.value.teacher_constraints || {}
     payload.reference_solution = aiDraft.value.reference_solution || null
     payload.test_groups = aiDraft.value.test_groups
     payload.score_cap_rules = aiDraft.value.score_cap_rules
   }
   let res
   if (editingId.value) {
     await assignmentsAPI.updateQuestion(route.params.id, editingId.value, payload)
     app.showToast('题目已更新', 'success')
   } else {
     res = await assignmentsAPI.createQuestion(route.params.id, payload)
     app.showToast('题目已创建', 'success')
     // 新建成功后获得真实题目 id；修改过的 AI 草稿经独立接口落库到新题目。
     if (res?.data?.id && aiDraftDirty.value) {
       try {
         await aiGradingAPI.updateConfig('assignment', res.data.id, {
           grading_mode: aiDraft.value.grading_mode,
           teacher_constraints: aiDraft.value.teacher_constraints || {},
           reference_solution: aiDraft.value.reference_solution || null,
           test_groups: aiDraft.value.test_groups,
           score_cap_rules: aiDraft.value.score_cap_rules,
         })
       } catch (e) {
         app.showToast(`AI 配置未随题目保存：${e.response?.data?.detail?.message || e.message || '保存失败'}`, 'error')
       }
     }
     resetAiDraft()  // 草稿已随本题落库，清除后由 startNew 进入下一题
   }
   lastSavedAt.value = new Date()
   await fetch()
   // 新题保存成功即视为「配置完一道题」：列表刷新后清空下方编辑区，准备录入下一题。
   if (!editingId.value) startNew()
   return true
 } catch (e) {
   app.showToast(e.response?.data?.detail?.message || '保存失败', 'error')
   return false
 } finally { saving.value = false }
}

// ── 发布作业（作业级接口 POST /assignments/{id}/publish）────────────
// 流程：先保存当前题目（草稿，失败即停）→ 调用发布接口 → 刷新作业状态。
// 发布失败时明确区分「题目已保存」与「发布失败」两段。
async function publishAssignment() {
 if (publishing.value) return
 publishing.value = true
 let saved = false
 try {
   if (activeId.value !== null) {
     saved = await submitQuestion()  // 保存失败已由 submitQuestion toast 错误
     if (!saved) { confirmPublish.value = false; return }
   }
   await assignmentsAPI.publish(route.params.id)
   app.showToast('作业已发布', 'success')
   confirmPublish.value = false
   fetch()  // 刷新作业状态：按钮随之变为「已发布」禁用
 } catch (e) {
   const msg = e.response?.data?.detail?.message || '发布失败，请稍后重试'
   app.showToast(saved ? `题目已保存为草稿，但发布失败：${msg}` : `发布失败：${msg}`, 'error')
 } finally {
   publishing.value = false
 }
}

function openPublishConfirm() {
 if (!assignmentDraft.value || publishing.value) return
 // Rubric 门禁前置提示：当前题 shadow/active 时提前提醒（后端 503 仍兜底）
 if (aiGateNotice.value) app.showToast(aiGateNotice.value, 'error')
 confirmPublish.value = true
}

// 列表行「AI 配置」按钮收敛：选中该题后滚动到题目编辑区下方的 AI 评分配置
function openAiConfig(q) {
 openEdit(q)
 nextTick(() => {
   document.getElementById('ai-config-section')
     ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
 })
}

// AI 配置保存成功：刷新上方题目列表，并清空下方编辑区进入下一题空白表单
async function onQuestionAiSaved() {
 await fetch()
 startNew()
 app.showToast('AI 配置已保存，已进入下一题', 'success')
}

// ── 删除题目（确认弹窗；仅草稿作业可用，后端 409 兜底） ─────────────
const deleteTarget = ref(null)
const deleting = ref(false)

function askDeleteQuestion(q) { deleteTarget.value = q }

async function confirmDeleteQuestion() {
  if (deleting.value) return
  deleting.value = true
  try {
    await assignmentsAPI.deleteQuestion(route.params.id, deleteTarget.value.id)
    if (activeId.value === deleteTarget.value.id) {
      activeId.value = null
      editingId.value = null
      resetQuestionForm()
      runResult.value = null
    }
    deleteTarget.value = null
    app.showToast('题目已删除', 'success')
    fetch()
  } catch (e) {
    app.showToast(e.response?.data?.detail?.message || '删除失败', 'error')
  } finally {
    deleting.value = false
  }
}

// 运行测试：调用后端 sample-run（该接口仅对学生开放，教师调用会返回 403，
// 错误如实展示，不做假成功）
async function runSample() {
 const qid = Number(editingId.value)
 if (!qid) { app.showToast('请先保存题目后再运行测试', 'error'); return }
 runResult.value = { status: 'running' }
 try {
   const res = await judgeAPI.sampleRun(qid, { question_id: qid, code: form.value.starter_code })
   runResult.value = res.data || {}
 } catch (e) {
   runResult.value = {
     status: 'error',
     message: e.response?.data?.detail?.message || e.response?.data?.message || e.message || '运行失败',
   }
 }
}

// 内存警告卡「使用推荐值」：一键设为环境最低内存
function applyRecommendedMemory() {
 if (!effectiveEnv.value) return
 form.value.memory_limit_mb = effectiveEnv.value.minimum_memory_mb
}

function cancel() {
 router.push(returnNavigation.value.destination)
}

// 保存成功时间戳（「今天 14:32 已保存」）
function formatSavedAt(d) {
 const now = new Date()
 const hh = String(d.getHours()).padStart(2, '0')
 const mm = String(d.getMinutes()).padStart(2, '0')
 if (d.toDateString() === now.toDateString()) return `今天 ${hh}:${mm}`
 return `${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`
}

async function fetchAiStatus() {
  try {
    const res = await aiGradingAPI.getStatus()
    aiEnabled.value = res.data.enabled === true
    aiReady.value = res.data.ready === true
  } catch { /* 忽略——AI 调用由服务端兜底门禁 */ }
}

onMounted(() => { fetch(); fetchEnv(); fetchAiStatus() })
</script>

<template>
  <AppLayout content-mode="workspace">
    <div class="qe-page">
      <!-- ── 顶部：面包屑 + 标题 + 保存状态 ─────────────────────── -->
      <header class="qe-topbar">
        <div class="qe-topbar-left">
          <nav class="qe-crumbs" aria-label="面包屑">
            题目管理 <span class="qe-crumbs-sep">/</span>
            <span class="qe-crumbs-name">{{ assignment?.title || '作业' }}</span>
            <span class="qe-crumbs-sep">/</span>
            <span class="qe-crumbs-cur">{{ editingId ? '#' + editingId : (activeId === 'new' ? '新建' : '选择题目') }}</span>
          </nav>
          <h1 class="qe-title">{{ activeId === 'new' ? '添加题目' : '编辑题目' }}</h1>
        </div>
        <div class="qe-topbar-right">
          <span v-if="lastSavedAt" class="qe-saved" role="status">
            <AppIcon name="check" :size="14" />
            {{ formatSavedAt(lastSavedAt) }} 已保存
          </span>
          <button
            type="button"
            class="btn btn-sm btn-primary qe-topbar-publish"
            :disabled="!assignmentDraft || publishing"
            @click="openPublishConfirm"
          >
            <AppIcon name="arrow-right" :size="14" />
            {{ publishing ? '发布中...' : (assignmentDraft ? '发布作业' : '已发布') }}
          </button>
        </div>
      </header>

      <section v-if="assignment" class="card qe-schedule-card" aria-labelledby="assignment-schedule-title">
        <div class="qe-schedule-copy">
          <div class="qe-side-sec-head">
            <h2 id="assignment-schedule-title" class="qe-card-title">发布与截止</h2>
            <span class="status-pill" :class="assignment.status">{{ assignment.status === 'published' ? '已发布' : '草稿' }}</span>
          </div>
          <p>发布时间用于记录首次发布；截止后学生仍可查看题目，但不能自测或提交。</p>
        </div>
        <dl class="qe-schedule-meta">
          <div><dt>首次发布时间</dt><dd>{{ assignment.published_at ? formatDateTime(assignment.published_at) : '尚未发布' }}</dd></div>
          <div class="qe-schedule-due">
            <dt><label for="assignment-due-at">截止时间</label></dt>
            <dd><input id="assignment-due-at" v-model="scheduleDueLocal" type="datetime-local" /></dd>
          </div>
        </dl>
        <button type="button" class="btn btn-sm btn-primary qe-schedule-save" :disabled="scheduleSaving" @click="requestScheduleSave">
          {{ scheduleSaving ? '保存中...' : '保存时间设置' }}
        </button>
      </section>

      <!-- ── 题目列表（保留「添加题目」入口，选中后进入编辑） ─────── -->
      <section class="card qe-list-card">
        <div class="qe-list-head">
          <h2 class="qe-card-title">题目 <span class="qe-count-badge">{{ questions.length }}</span></h2>
          <div v-if="!loading && questions.length === 0" class="qe-empty">
            <AppIcon name="assignment" :size="18" />
            <p>暂无题目，点击「添加题目」创建第一道题目</p>
          </div>
          <button class="btn-primary btn-sm" @click="startNew">
            <AppIcon name="plus" :size="14" />
            添加题目
          </button>
          <p v-if="aiDisabledNotice || aiKeyMissingNotice" class="qe-ai-notice">
            {{ aiDisabledNotice || aiKeyMissingNotice }}
          </p>
        </div>
        <div v-if="loading" class="qe-list-loading">
          <div class="skeleton" style="height:20px;width:260px"></div>
        </div>
        <div v-else-if="questions.length > 0" class="qe-list">
          <div
            v-for="(q, i) in questions"
            :key="q.id"
            class="qe-list-row"
            :class="{ 'qe-list-row--active': activeId === q.id }"
          >
            <div class="qe-list-main" :class="{ 'qe-list-main--readonly': !assignmentDraft }" @click="assignmentDraft && openEdit(q)">
              <span class="qe-list-no">#{{ i + 1 }}</span>
              <div class="qe-list-text">
                <div class="qe-list-title">{{ q.title }}</div>
                <div class="qe-list-meta">
                  函数: {{ q.function_name || '—' }} | 超时: {{ q.time_limit_ms }}ms | 内存: {{ q.memory_limit_mb }}MB
                  <span v-if="q.grading_mode && q.grading_mode !== 'legacy'" class="badge-mode">{{ q.grading_mode }}</span>
                </div>
              </div>
            </div>
            <div class="qe-list-actions">
              <button v-if="assignmentDraft" class="btn-sm btn-outline" @click="openEdit(q)">编辑</button>
              <button v-if="assignmentDraft" class="btn-sm btn-outline qe-delete-btn" @click="askDeleteQuestion(q)">删除</button>
              <!-- 收敛：不再行内展开第二个 AIQuestionConfig 实例，统一打开右侧栏 AI tab -->
              <button class="btn-sm btn-outline" @click="openAiConfig(q)">
                <AppIcon name="settings" :size="14" />
                <span class="qe-sr-only"></span>AI 配置
              </button>
            </div>
          </div>
        </div>
      </section>

      <!-- ── 作业运行设置：固定在题目列表下方，切换题目时保持原位 ─────── -->
      <aside class="qe-side">
        <div class="qe-side-card">
          <div class="qe-side-head">
            <h2 class="qe-card-title">运行设置</h2>
            <span class="qe-side-head-note">作业级配置</span>
          </div>
          <div class="qe-side-body">
            <!-- 作业默认环境（draft 可编辑；发布后绑定不可变） -->
            <div class="qe-side-sec qe-side-sec--assignment">
              <div class="qe-side-sec-head">
                <h3 class="qe-side-title">作业默认环境</h3>
                <span v-if="!assignmentDraft" class="badge badge-neutral">环境已锁定</span>
              </div>
              <p class="qe-side-sub">所有题目默认在此环境运行；发布后绑定不可变</p>
              <div class="form-group qe-side-assignment-env">
                <EnvironmentProfilePicker
                  v-model="assignmentEnvId"
                  :disabled="!assignmentDraft"
                  show-memory
                  label="作业环境"
                />
                <p v-if="assignmentDraft && assignmentEnvId && envById(assignmentEnvId)" class="form-hint">
                  环境最低内存 {{ envById(assignmentEnvId).minimum_memory_mb }} MB——题目内存不得低于该值
                </p>
              </div>
              <div class="form-group qe-side-assignment-policy">
                <label>作业导入规则</label>
                <DsSelect v-model="assignmentPolicy" class="import-policy-select" :disabled="!assignmentDraft">
                  <option value="unrestricted">不限制</option>
                  <option value="restricted">限定白名单</option>
                </DsSelect>
              </div>
              <div v-if="assignmentPolicy === 'restricted'" class="form-group qe-side-imports">
                <label>作业允许导入（白名单）</label>
                <div v-if="envImportCandidates(assignmentEnvId).length" class="import-candidates">
                  <label v-for="name in envImportCandidates(assignmentEnvId)" :key="name" class="import-chip">
                    <input type="checkbox" :disabled="!assignmentDraft" :checked="assignmentAllowedImports.includes(name)" @change="toggleAssignmentImport(name)" />
                    {{ name }}
                  </label>
                </div>
                <p v-if="assignmentMismatch" class="form-hint env-warn">{{ assignmentMismatch }}</p>
              </div>
              <button v-if="assignmentDraft" class="btn-primary btn-sm" @click="saveAssignmentEnv">保存作业环境设置</button>
            </div>

            <div class="qe-side-divider"></div>

            <div class="qe-side-sec qe-side-sec--audience">
              <div class="qe-side-sec-head">
                <h3 class="qe-side-title">发布范围</h3>
                <span v-if="!assignmentDraft" class="badge badge-neutral">已有提交后受限</span>
              </div>
              <p class="qe-side-sub">{{ assignment?.offering_id ? '从本授课班名单中选择发布范围。' : '设置共享模板的发布范围，分配班级后再确认在册学生。' }}</p>
              <TaskAudiencePicker
                task-kind="assignment"
                :task-id="route.params.id"
                :course-id="assignment?.course_id"
                :offering-id="assignment?.offering_id"
                :audience-mode="audienceMode"
                :class-ids="audienceClassIds"
                :whitelist-ids="audienceWhitelistIds"
                :excluded-ids="audienceExcludedIds"
                :disabled="!assignmentDraft"
                @update:audience-mode="audienceMode = $event"
                @update:class-ids="audienceClassIds = $event"
                @update:whitelist-ids="audienceWhitelistIds = $event"
                @update:excluded-ids="audienceExcludedIds = $event"
                @imported="onAudienceImported"
              />
              <button v-if="assignmentDraft" class="btn-primary btn-sm" :disabled="audienceSaving" @click="saveAssignmentAudience">保存发布范围</button>
            </div>
          </div>
        </div>
      </aside>

      <!-- ── 主编辑区：选择题目后显示在作业运行设置下方 ─────────────── -->
      <div v-if="activeId !== null" class="qe-body">
        <!-- 中间主编辑区：基础信息 → 运行参数 → 代码模板 → 测试用例 → AI 评分配置 -->
        <div class="qe-main">
          <!-- ① 基础信息 -->
          <section class="card qe-card">
            <h2 class="qe-card-title">基础信息</h2>
            <div class="qe-grid-2">
              <div class="form-group">
                <label>题目标题</label>
                <input v-model="form.title" placeholder="如: 两数之和" />
              </div>
              <div class="form-group">
                <label>函数签名 <span class="qe-fn-state" :class="{ 'qe-fn-state--err': fnParseState.includes('未识别') }">{{ fnParseState }}</span></label>
                <input v-model="form.signature" placeholder="def add(a: int, b: int) -> int（自动识别函数名）" />
              </div>
            </div>
            <div class="form-group qe-md-group">
              <label>题目描述（Markdown）</label>
              <QeMarkdownEditor v-model="form.description" :height="200" />
            </div>
          </section>

          <!-- ② 本题运行参数（环境与导入规则统一继承作业设置） -->
          <section class="card qe-card qe-run-card">
            <div class="qe-card-head">
              <h2 class="qe-card-title">运行参数</h2>
              <span class="qe-hint">运行环境与导入规则统一继承作业设置，无需单题重复配置</span>
            </div>
            <div class="qe-run-grid">
              <div class="form-group">
                <label>超时 (ms)</label>
                <input v-model.number="form.time_limit_ms" type="number" min="100" />
              </div>
              <div class="form-group">
                <label>内存限制 (MB)</label>
                <input v-model.number="form.memory_limit_mb" type="number" min="64" />
              </div>
            </div>

            <!-- 内存警告卡片（浅橙） -->
            <div v-if="memoryWarning" class="qe-warn-card" role="alert">
              <p class="qe-warn-text"><AppIcon name="warning" :size="14" /><span class="qe-sr-only">⚠ </span>{{ memoryWarning }}</p>
              <p class="qe-warn-sub">当前内存低于环境最低要求，建议自动调整</p>
              <button type="button" class="qe-warn-btn" @click="applyRecommendedMemory">使用推荐值</button>
            </div>

            <!-- 有效环境 info（浅蓝） -->
            <p v-if="effectiveEnv" class="qe-info">
              本题有效环境：{{ effectiveEnv.display_name }} v{{ effectiveEnv.version_number }} 最低内存 {{ effectiveEnv.minimum_memory_mb }} MB；导入规则：{{ assignmentPolicyLabel }}
            </p>
          </section>

          <!-- ③ 学生代码模板 -->
          <section class="card qe-card">
            <div class="qe-card-head">
              <h2 class="qe-card-title">学生代码模板</h2>
              <div class="qe-card-actions">
                <span class="qe-lang-badge">Python</span>
                <button class="btn-sm btn-outline" @click="fullscreenCode = !fullscreenCode">
                  <AppIcon name="move" :size="14" />
                  <span class="qe-sr-only">⛶ </span>{{ fullscreenCode ? '退出全屏' : '全屏' }}
                </button>
              </div>
            </div>
            <QeCodeEditor v-model="form.starter_code" :fullscreen="fullscreenCode" :height="380" placeholder="def add(a, b):&#10;    # TODO&#10;    pass" />
            <p class="qe-hint"><AppIcon name="drag" :size="14" />拖动右下角手柄调整编辑器高度；编辑器内部滚动，长代码不撑高页面</p>
          </section>

          <!-- ④ 测试用例 -->
          <section class="card qe-card">
            <div class="qe-card-head">
              <h2 class="qe-card-title">测试用例</h2>
            </div>
            <QeTestCases
              :key="'cases-' + formKey"
              :public-cases="publicCasesArr"
              :hidden-tests="form.hidden_tests"
              :function-name="form.function_name"
              :question-id="editingId ? Number(editingId) : null"
              :run-result="runResult"
              @update:public-cases="publicCasesArr = $event"
              @update:hidden-tests="form.hidden_tests = $event"
              @run-sample="runSample"
              @parse-failed="app.showToast('当前 pytest 代码无法解析为可视化用例，请使用 pytest 模式', 'error')"
            />
          </section>

          <!-- ④b 可信数据判题（评分版本）：转换预检 → 草稿 → 确认 → 启用 -->
          <section v-if="editingId" id="grading-version-section" class="card qe-card">
            <div class="qe-card-head">
              <h2 class="qe-card-title">可信数据判题</h2>
            </div>
            <GradingVersionPanel :key="`gv-assignment-${editingId}`" :question-id="Number(editingId)" />
          </section>

          <!-- ⑤ AI 评分配置：位于题目编写区下方，先写题再配 AI -->
          <section id="ai-config-section" class="card qe-card qe-ai-main">
            <!-- 已有题目 → 持久化容器（GET/PUT/Rubric），保存后刷新列表并进入下一题 -->
            <AIQuestionConfig
              v-if="editingId"
              :kind="'assignment'"
              :question-id="Number(editingId)"
              :expanded="true"
              :closable="false"
              @saved="onQuestionAiSaved"
            />
            <!-- 新建题目（无 id）→ 草稿模式：随「保存题目」一起提交 -->
            <div v-else class="qe-ai-draft">
              <div class="qe-card-head">
                <div class="qe-side-sec-head">
                  <h2 class="qe-card-title">AI 评分配置</h2>
                  <span class="qe-ai-qid">新建</span>
                </div>
                <p class="qe-side-sub">配置评分模式与测试组，随「保存题目」一起提交；Rubric 需在保存题目后生成。</p>
              </div>
              <!-- Rubric 门禁前置提示：shadow/active 尚无 Rubric（后端 503 仍兜底） -->
              <div v-if="aiDraft.grading_mode !== 'legacy'" class="qe-warn-card" role="alert">
                <p class="qe-warn-text"><AppIcon name="warning" :size="14" /><span class="qe-sr-only">⚠ </span>当前为 {{ aiDraft.grading_mode }} 模式，尚无可发布的 Rubric，请先保存题目后生成并锁定</p>
              </div>
              <AiConfigForm
                :key="'ai-draft-' + aiFormKey"
                :model-value="aiDraft"
                @update:model-value="onAiDraftChange"
                @generate-test-groups="onDraftGenerateTestGroups"
              />
              <p v-if="aiDraftMsg" class="qe-hint" role="status">{{ aiDraftMsg }}</p>
              <p class="qe-hint">保存题目后可生成 Rubric。</p>
            </div>
          </section>
        </div>

      </div>

      <!-- ── 底部固定操作栏 ─────────────────────────────────────── -->
      <footer class="qe-bottom-bar">
        <div class="qe-bottom-inner">
          <div class="qe-bottom-left">
            <span v-if="editingId" class="qe-bottom-id">#{{ editingId }}</span>
            <span v-else-if="activeId === 'new'" class="qe-bottom-id">新题目</span>
          </div>
          <div class="qe-bottom-actions">
            <button type="button" class="btn btn-sm" @click="cancel">{{ returnNavigation.label }}</button>
            <button type="button" class="btn btn-sm btn-primary" :disabled="saving || activeId === null" @click="submitQuestion">
              {{ saving ? '保存中...' : (activeId === 'new' ? '保存题目并下一题' : '保存题目') }}
            </button>
          </div>
        </div>
      </footer>

      <!-- 发布作业确认弹窗（作业级影响，防误点）；AI 门禁提示随当前题模式动态追加 -->
      <ConfirmDialog
        v-if="confirmPublish"
        title="发布整个作业？"
        :message="'将发布整个作业，全部题目对学生可见；发布后作业环境与白名单绑定不可修改。' + publishDeadlineNotice + aiGateNotice"
        confirm-text="确认发布"
        cancel-text="取消"
        :busy="publishing"
        @confirm="publishAssignment()"
        @cancel="confirmPublish = false"
      />
      <ConfirmDialog
        v-if="scheduleConfirmOpen"
        title="确认调整截止时间"
        :message="scheduleConfirmMessage"
        confirm-text="确认保存"
        :danger="true"
        :busy="scheduleSaving"
        @confirm="saveSchedule()"
        @cancel="scheduleConfirmOpen = false"
      />
      <ConfirmDialog
        v-if="deleteTarget"
        title="删除题目"
        :message="`确定删除题目「${deleteTarget.title}」？该题相关的学生提交与评分记录将一并删除，此操作不可恢复。`"
        confirm-text="确认删除"
        :danger="true"
        :busy="deleting"
        @confirm="confirmDeleteQuestion"
        @cancel="deleteTarget = null"
      />
    </div>
  </AppLayout>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════
  Question Edit — IDE 风格（轻量后台管理 + 编辑器）
  白色/浅灰背景 + 蓝色主强调 + 8~10px 圆角 + 轻阴影
  ═══════════════════════════════════════════════════════════════════ */
.qe-page {
 display: flex;
 flex-direction: column;
 gap: 20px;
 padding-bottom: 24px; /* 底部操作栏占位（content-inner 已有 80px 内边距） */
}

/* ── 顶部：面包屑 + 标题 + 保存状态 ─────────────────────────────── */
.qe-topbar {
 display: flex;
 justify-content: space-between;
 align-items: flex-end;
 gap: 16px;
}

.qe-crumbs {
 display: flex;
 align-items: center;
 gap: 6px;
 font-size: 12px;
 color: var(--faint);
 margin-bottom: 4px;
}

.qe-crumbs-sep { color: var(--border-strong); }

.qe-crumbs-name {
 color: var(--muted);
 max-width: 240px;
 overflow: hidden;
 text-overflow: ellipsis;
 white-space: nowrap;
}

.qe-crumbs-cur { color: var(--accent); font-weight: 500; }

.qe-title {
 font-size: 22px;
 font-weight: 700;
 color: var(--fg);
 letter-spacing: -0.01em;
 margin: 0;
 line-height: 1.2;
}

.qe-saved {
 display: inline-flex;
 align-items: center;
 gap: 5px;
 font-size: 12px;
 color: var(--success);
 background: var(--success-bg);
 border-radius: var(--radius-full);
 padding: 4px 12px;
 white-space: nowrap;
}

.qe-topbar-right {
 display: flex;
 align-items: center;
 gap: 10px;
}

.qe-topbar-publish {
 white-space: nowrap;
}

/* ── 卡片公共 ───────────────────────────────────────────────────── */
.qe-card {
 border-radius: var(--radius-md);
 box-shadow: none;
}

.qe-card-title {
 font-size: 15px;
 font-weight: 600;
 color: var(--fg);
 margin: 0;
}

.qe-card-head {
 display: flex;
 align-items: center;
 justify-content: space-between;
 gap: 12px;
 margin-bottom: 14px;
}

.qe-card-actions {
 display: flex;
 align-items: center;
 gap: 8px;
}

.qe-hint {
 margin: 8px 0 0;
 font-size: 12px;
 color: var(--faint);
}

/* ── 题目列表卡 ─────────────────────────────────────────────────── */
.qe-list-head {
 display: flex;
 align-items: center;
 justify-content: space-between;
 gap: 12px;
 margin-bottom: 12px;
}

.qe-count-badge {
 display: inline-block;
 min-width: 20px;
 padding: 0 6px;
 border-radius: var(--radius-full);
 background: var(--accent-soft);
 color: var(--accent);
 font-size: 11px;
 font-weight: 600;
 text-align: center;
 vertical-align: 2px;
}

.qe-list-loading { padding: 8px 0; }

.qe-empty {
 padding: 22px 0;
 text-align: center;
 color: var(--faint);
 font-size: var(--text-sm);
}

.qe-empty p { margin: 0; }

/* 题目列表：纵向紧凑列表，超出内部滚动（长内容铁律） */
.qe-list {
 display: flex;
 flex-direction: column;
 max-height: 300px;
 overflow-y: auto;
 margin: 0 -16px;
 padding: 0 16px;
}

.qe-list-row {
 border: 1px solid transparent;
 border-radius: var(--radius-md);
 padding: 10px 12px;
 display: flex;
 align-items: center;
 gap: 12px;
 transition: background var(--duration-fast), border-color var(--duration-fast);
}

.qe-list-row:hover { background: var(--surface-sunken); }

.qe-list-row--active {
 background: var(--accent-soft);
 border-color: oklch(0.52 0.095 158 / 0.25);
}

.qe-list-row + .qe-list-row { margin-top: 2px; }

.qe-list-main {
 display: flex;
 align-items: center;
 gap: 10px;
 flex: 1;
 min-width: 0;
 cursor: pointer;
}

.qe-list-main--readonly { cursor: default; }

.qe-list-no {
 font-family: var(--font-mono);
 font-size: 12px;
 color: var(--faint);
 flex-shrink: 0;
}

.qe-list-text { min-width: 0; }

.qe-list-title {
 font-size: var(--text-sm);
 font-weight: 600;
 color: var(--fg);
 overflow: hidden;
 text-overflow: ellipsis;
 white-space: nowrap;
}

.qe-list-meta {
 font-size: 12px;
 color: var(--faint);
 margin-top: 2px;
 overflow: hidden;
 text-overflow: ellipsis;
 white-space: nowrap;
}

.qe-list-actions {
 display: flex;
 gap: 8px;
 align-items: center;
 flex-shrink: 0;
}

.qe-delete-btn { color: var(--danger, var(--danger)); }
.qe-delete-btn:hover { border-color: var(--danger, var(--danger)); background: var(--danger-bg); }

/* TASK-020：AI 治理门提示横幅（未审批/缺 Key） */
.qe-ai-notice {
  margin: 10px 0 0; padding: 10px 12px; border-radius: var(--radius-md);
  font-size: 13px; line-height: 1.5;
  color: var(--warning); background: var(--warning-bg); border: 1px solid var(--warning-bg);
}

.badge-mode {
 display: inline-block; padding: 1px 8px; border-radius: var(--radius-md);
 font-size: 11px; font-weight: 500; margin-left: 6px;
 background: var(--info-bg); color: var(--accent-hover);
}

/* ── 主体：主编辑区 + 右侧栏 ───────────────────────────────────── */
.qe-body {
 display: flex;
 align-items: flex-start;
 gap: 24px;
}

.qe-main {
 flex: 1;
 min-width: 0;
 display: flex;
 flex-direction: column;
 gap: 20px;
}

.qe-side {
 width: 100%;
 flex-shrink: 0;
 position: static;
}

/* ── 基础信息 ───────────────────────────────────────────────────── */
.qe-grid-2 {
 display: grid;
 grid-template-columns: 1fr 1fr;
 gap: 16px;
}

.qe-md-group { margin-bottom: 0; }

.qe-fn-state {
 font-size: 12px;
 font-weight: 400;
 color: var(--success);
 margin-left: 4px;
}

.qe-fn-state--err { color: var(--warning); }

/* ── 语言徽标 ───────────────────────────────────────────────────── */
.qe-lang-badge {
 display: inline-flex;
 align-items: center;
 gap: 5px;
 padding: 3px 10px;
 border: 1px solid var(--border);
 border-radius: var(--radius-full);
 background: var(--surface-subtle);
 color: var(--muted);
 font-size: 12px;
 font-weight: 600;
}

/* ── 右侧设置栏 ─────────────────────────────────────────────────── */
.qe-side-card {
 border: 1px solid var(--border);
 border-radius: var(--radius-md);
 background: var(--surface);
 box-shadow: none;
 overflow: hidden;
}

.qe-side-tabs {
 display: flex;
 border-bottom: 1px solid var(--border);
 background: var(--surface-subtle);
}

.qe-side-tab {
 flex: 1;
 padding: 10px 0;
 border: none;
 border-bottom: 2px solid transparent;
 background: transparent;
 color: var(--muted);
 font-size: var(--text-sm);
 font-weight: 500;
 cursor: pointer;
 margin-bottom: -1px;
 transition: color var(--duration-fast);
}

.qe-side-tab:hover { color: var(--fg); }

.qe-side-tab.active {
 color: var(--accent);
 border-bottom-color: var(--accent);
 font-weight: 600;
 background: var(--surface);
}

.qe-side-body { padding: 14px 16px 16px; }

.qe-side-sec-head {
 display: flex;
 align-items: center;
 justify-content: space-between;
 gap: 8px;
}

.qe-side-title {
 font-size: var(--text-sm);
 font-weight: 600;
 color: var(--fg);
 margin: 0 0 4px;
}

.qe-side-sub {
 margin: 0 0 12px;
 font-size: 12px;
 color: var(--faint);
 line-height: 1.6;
}

.qe-side-divider {
 height: 1px;
 background: var(--border);
 margin: 14px -16px;
}

.qe-side-grid-2 {
 display: grid;
 grid-template-columns: 1fr 1fr;
 gap: 12px;
}

/* 右侧栏内表单组间距收紧（窄栏） */
.qe-side-body .form-group { margin-bottom: 14px; }

.import-policy-select {
 width: 100%;
 padding: 9px 12px;
 border: 1px solid var(--border);
 border-radius: var(--radius-control, 7px);
 background: var(--surface, var(--surface));
 color: var(--fg);
 font-family: inherit;
 font-size: var(--text-sm, 13px);
}

.import-policy-select:disabled {
 background: var(--surface-raised, var(--surface-subtle));
 color: var(--faint);
}

.env-warn { color: var(--warning, var(--warning)); }

.import-candidates {
 display: flex;
 flex-wrap: wrap;
 gap: 8px;
}

.import-chip {
 display: inline-flex;
 align-items: center;
 gap: 6px;
 padding: 5px 10px;
 border: 1px solid var(--border);
 border-radius: var(--radius-full);
 background: var(--surface-raised, var(--surface-subtle));
 font-size: var(--text-sm, 13px);
 cursor: pointer;
}

.import-chip input { margin: 0; }

/* ── 内存警告卡（浅橙，不是一行小字） ───────────────────────────── */
.qe-warn-card {
 margin: 4px 0 12px;
 padding: 10px 12px;
 border: 1px solid oklch(0.66 0.14 75 / 0.35);
 border-radius: var(--radius-md);
 background: var(--warning-bg);
}

.qe-warn-text {
 margin: 0 0 4px;
 font-size: 12.5px;
 font-weight: 600;
 color: var(--warning);
 line-height: 1.5;
}

.qe-warn-sub {
 margin: 0 0 8px;
 font-size: 12px;
 color: var(--warning);
}

.qe-warn-btn {
 padding: 5px 12px;
 border: 1px solid var(--warning);
 border-radius: var(--radius-md);
 background: var(--surface);
 color: var(--warning);
 font-size: 12px;
 font-weight: 500;
 cursor: pointer;
 transition: background var(--duration-fast);
}

.qe-warn-btn:hover { background: var(--warning-bg); }

/* ── 有效环境 info（浅蓝） ──────────────────────────────────────── */
.qe-info {
 margin: 0 0 8px;
 padding: 8px 10px;
 border-radius: var(--radius-md);
 background: var(--info-bg);
 color: oklch(0.52 0.09 235);
 font-size: 12px;
 line-height: 1.6;
}

/* ── AI 评分配置 tab ────────────────────────────────────────────── */
.qe-ai-qid {
 font-family: var(--font-mono);
 font-size: 12px;
 color: var(--faint);
}

.qe-ai-empty {
 padding: 20px 4px;
 text-align: center;
 color: var(--faint);
 font-size: var(--text-sm);
 line-height: 1.6;
}

.qe-ai-empty p { margin: 0; }

/* AI 配置表单较长：该 tab 内内部滚动（长内容铁律），不撑高页面 */
.qe-side-body--scroll {
 max-height: calc(100vh - 150px);
 overflow-y: auto;
}

/* AIQuestionConfig 默认自带 12px 顶部外边距与不可换行的上限行，窄栏内收拢 */
.qe-side-body--scroll :deep(.ai-config) { margin-top: 0; }

.qe-side-body--scroll :deep(.cap-row) { flex-wrap: wrap; }

/* ── 底部固定操作栏 ─────────────────────────────────────────────── */
.qe-bottom-bar {
 position: fixed;
 right: 0;
 bottom: 0;
 z-index: 100;
 background: var(--surface);
 border-top: 1px solid var(--border);
 box-shadow: 0 -2px 8px oklch(0.2 0.01 150 / 0.04);
}

.qe-bottom-inner {
 max-width: var(--content-max, 1440px);
 margin: 0 auto;
 padding: 10px 36px;
 display: flex;
 align-items: center;
 justify-content: space-between;
 gap: 16px;
}

.qe-bottom-left { min-width: 80px; }

.qe-bottom-id {
 font-family: var(--font-mono);
 font-size: 12px;
 color: var(--faint);
}

.qe-bottom-actions {
 display: flex;
 align-items: center;
 gap: 10px;
}

.qe-bottom-actions .btn {
 min-width: 88px;
 justify-content: center;
}

/* 窄屏：底部栏内容收窄 */
@media (max-width: 767.98px) {
 .qe-bottom-inner { padding: 10px 16px; }
 .qe-bottom-left { display: none; }
 .qe-grid-2 { grid-template-columns: 1fr; }
}

@media (max-width: 1199px) {
  /* 侧栏折叠时底部栏跟随（与 AppLayout 的 main-area 一致） */
  .qe-bottom-bar { left: var(--sidebar-collapsed-width); }
}

/* ═══════════════════════════════════════════════════════════════════
   Approved editor canvas refresh
   One calm canvas, a horizontal configuration shelf, and a responsive
   single-column fallback. All selectors below are presentation-only;
   the existing data and event contracts stay unchanged.
   ═══════════════════════════════════════════════════════════════════ */
.qe-page {
  gap: 14px;
  min-width: 0;
  padding-bottom: 20px;
}

.qe-page button:focus-visible,
.qe-page input:focus-visible,
.qe-page .ds-select-trigger:focus-visible,
.qe-page textarea:focus-visible {
  outline: 2px solid oklch(0.52 0.095 158 / 0.45);
  outline-offset: 2px;
}

/* 桥接样式只给了裸 input/textarea 高度和边框，没有给宽度；此处补齐，
   否则题目标题/签名/运行设置里的输入框会以浏览器默认宽度渲染并留白/溢出。 */
.qe-page input:not([type='checkbox']):not([type='radio']):not([type='file']),
.qe-page textarea {
  width: 100%;
  min-width: 0;
}

.qe-topbar {
  align-items: center;
  min-height: 48px;
}

.qe-topbar-right,
.qe-topbar-publish,
.qe-list-head button,
.qe-list-actions button,
.qe-card-actions button,
.qe-bottom-actions .btn,
.qe-hint,
.qe-warn-text {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.qe-topbar-publish,
.btn-outline {
  border: 1px solid var(--border-strong, var(--border-strong));
  border-radius: var(--radius-control, 7px);
  background: var(--surface, var(--surface));
  color: var(--fg);
  font-weight: 600;
}

.qe-topbar-publish:hover:not(:disabled),
.btn-outline:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}

.qe-topbar-publish:disabled,
.btn-outline:disabled {
  opacity: 0.55;
}

.qe-saved {
  background: var(--success-bg);
  border: 1px solid oklch(0.55 0.13 150 / 0.16);
  padding: 5px 10px;
}

.qe-list-card,
.qe-card,
.qe-side-card {
  border-color: var(--border);
  box-shadow: none;
}

.qe-list-card:hover,
.qe-card:hover,
.qe-side-card:hover {
  border-color: var(--border);
  box-shadow: none;
  transform: none;
}

.qe-list-card {
  position: relative;
  min-width: 0;
  padding: 13px 16px;
  border-radius: var(--radius-card, 12px);
  background: var(--surface);
}

.qe-list-head {
  min-height: 30px;
  margin-bottom: 10px;
}

.qe-list-head .qe-card-title {
  font-size: 14px;
  letter-spacing: 0;
}

.qe-count-badge {
  min-width: 21px;
  padding: 2px 7px;
  background: var(--accent-soft);
}

.qe-empty {
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 0 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--faint);
}

.qe-empty .app-icon { color: var(--faint); }

.qe-empty p {
  max-width: 100%;
  margin: 0;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qe-list {
  max-height: 220px;
  margin: 0 -8px;
  padding: 0 8px;
}

.qe-list-row {
  padding: 8px 10px;
  border-radius: var(--radius-md);
}

.qe-list-row--active {
  background: oklch(0.52 0.095 158 / 0.07);
  border-color: oklch(0.52 0.095 158 / 0.24);
}

.qe-list-actions { gap: 6px; }

.qe-list-actions .btn-sm,
.qe-card-actions .btn-sm {
  min-height: 30px;
  padding-inline: 10px;
}

.qe-body {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 14px;
  min-width: 0;
  width: 100%;
}

.qe-main {
  order: 0;
  width: 100%;
  min-width: 0;
  gap: 0;
  overflow: visible;
  border: 1px solid var(--border);
  border-radius: var(--radius-card, 12px);
  background: var(--surface);
}

.qe-main > .qe-card {
  min-width: 0;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  padding: 22px 24px;
}

.qe-main > .qe-card + .qe-card {
  border-top: 1px solid var(--border);
}

.qe-main > .qe-card:hover {
  border-color: transparent;
  box-shadow: none;
  transform: none;
}

.qe-main > .qe-card:first-child { border-radius: var(--radius-card, 12px) var(--radius-card, 12px) 0 0; }
.qe-main > .qe-card:last-child { border-radius: 0 0 var(--radius-card, 12px) var(--radius-card, 12px); }

.qe-card-title {
  font-size: 14px;
  letter-spacing: 0;
}

.qe-card-head { margin-bottom: 14px; }


.qe-side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 40px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-subtle);
}

.qe-side-head-note {
  font-size: 11px;
  color: var(--faint);
}

.qe-side-body {
  display: grid;
  /* 顶部配置架只保留作业级两区：作业默认环境 | 发布范围。
     一条 .qe-side-divider 自动落入中间 1px 轨道。 */
  grid-template-columns: minmax(0, 0.95fr) 1px minmax(0, 1.3fr);
  gap: 14px;
  align-items: stretch;
  min-width: 0;
  padding: 12px 16px 14px;
}

.qe-side-body .qe-side-sec {
  min-width: 0;
}

.qe-side-sec--assignment {
  grid-column: 1;
  grid-row: 1;
}

.qe-side-sec--audience {
  grid-column: 3;
  grid-row: 1;
}

.qe-side-sec--assignment,
.qe-side-sec--audience {
  align-content: start;
  min-width: 0;
}

/* 作业环境：整列纵向排布，避免窄列内 select 选项文字被裁切 */
.qe-side-sec--assignment {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 10px;
}

.qe-side-sec--assignment > .form-group { margin-bottom: 0; }
.qe-side-sec--assignment > button { align-self: flex-end; }

/* 发布范围：整列纵向排布，搜索行允许换行 */
.qe-side-sec--audience {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 10px;
}

.qe-side-sec--audience > .qe-side-sub { margin-bottom: 0; }
.qe-side-sec--audience > button { align-self: flex-end; }
.qe-side-sec--audience :deep(.audience-picker) { min-width: 0; }

.qe-side-divider {
  width: 1px;
  height: auto;
  margin: 0;
  background: var(--border);
}

.qe-side-body .form-group { min-width: 0; }

/* 题目编辑区内的运行参数（原「本题运行设置」收敛为超时/内存两字段） */
.qe-run-grid {
  display: grid;
  grid-template-columns: minmax(180px, 240px) minmax(180px, 240px);
  gap: 16px;
  align-items: start;
}

.qe-run-grid .form-group { margin-bottom: 0; }

.qe-run-card > .qe-warn-card,
.qe-run-card > .qe-info {
  margin-top: 12px;
  max-width: 760px;
}

/* AI 评分配置内嵌到题目编辑区下方：去掉组件自带卡片壳，与父卡片融合 */
.qe-ai-main :deep(.ai-config) {
  margin-top: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}

.qe-ai-main :deep(.cap-row) { flex-wrap: wrap; }

.qe-ai-main .qe-ai-draft {
  min-width: 0;
}

.qe-ai-main .qe-card-head {
  margin-bottom: 0;
}

.qe-ai-main .qe-ai-draft .qe-side-sub {
  margin-top: 4px;
  margin-bottom: 14px;
}
.qe-side-sub {
  max-width: 780px;
  margin-bottom: 0;
  font-size: 11px;
  line-height: 1.4;
}

.qe-side-sec--assignment .qe-side-sub {
  margin-bottom: 0;
}

.qe-side-sec--assignment .form-hint {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qe-side-grid-2 {
  gap: 10px;
}

.qe-side-body .import-policy-select {
  min-height: 34px;
  padding-block: 8px;
}

.qe-side-body .form-hint,
.qe-side-body .env-warn {
  font-size: 11px;
  line-height: 1.45;
}

.qe-warn-card {
  margin: 0;
  padding: 10px 12px;
}

.qe-warn-text {
  margin-bottom: 4px;
}

.qe-warn-text .app-icon { flex: 0 0 auto; }

.qe-info {
  margin: 0;
  padding: 6px 8px;
  font-size: 11px;
}

.qe-hint {
  line-height: 1.5;
}

.qe-hint .app-icon { flex: 0 0 auto; color: var(--faint); }

.qe-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.qe-bottom-bar {
  left: var(--modal-left, var(--sidebar-width, 264px));
  right: 0;
  z-index: 50;
  box-shadow: 0 -4px 14px oklch(0.2 0.01 150 / 0.06);
}

.qe-bottom-inner {
  padding-top: 11px;
  padding-bottom: 11px;
}

.qe-bottom-actions .btn-primary {
  min-width: 116px;
}

@media (max-width: 1180px) {
  .qe-side-body {
    grid-template-columns: 1fr;
    gap: 14px;
  }

  .qe-side-divider {
    width: auto;
    height: 1px;
    margin: 0;
  }

  .qe-side-sec--assignment,
  .qe-side-sec--audience {
    grid-column: 1;
    grid-row: auto;
  }
}

@media (max-width: 767.98px) {
  .qe-page { gap: 12px; }

  .qe-topbar {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 10px;
  }

  .qe-topbar-right {
    width: 100%;
    justify-content: space-between;
  }

  .qe-title { font-size: 20px; }

  .qe-list-card { padding-inline: 12px; }

  .qe-list-head { align-items: flex-start; }

  .qe-list-head { flex-wrap: wrap; }

  .qe-list-head .qe-empty {
    order: 3;
    flex-basis: 100%;
    padding: 2px 0 0;
  }

  .qe-list-row { flex-wrap: wrap; }

  .qe-list-actions {
    width: 100%;
    justify-content: flex-end;
  }

  .qe-main > .qe-card { padding: 18px 16px; }

  .qe-side-body {
    padding: 16px;
  }

  .qe-side { order: 1; }

  .qe-run-grid {
    grid-template-columns: 1fr;
    gap: 14px;
  }

  .qe-bottom-inner { padding-inline: 12px; }
  .qe-bottom-left { display: none; }
  .qe-bottom-actions { width: 100%; }
  .qe-bottom-actions .btn { flex: 1 1 0; min-width: 0; }
}

/* 作业级时间安排与题目/环境编辑分层，已发布后仍可单独调整截止时间。 */
.qe-schedule-card {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(420px, 1.4fr) auto;
  align-items: center;
  gap: 20px;
  padding: 16px 18px;
  border-radius: var(--radius-card, 12px);
  box-shadow: none;
}
.qe-schedule-copy .qe-side-sec-head { justify-content: flex-start; margin-bottom: 4px; }
.qe-schedule-copy p { margin: 0; color: var(--faint); font-size: 12px; line-height: 1.55; }
.qe-schedule-card .status-pill { display: inline-flex; padding: 3px 9px; border-radius: var(--radius-full); font-size: 11px; font-weight: 600; }
.qe-schedule-card .status-pill.published { color: var(--success); background: var(--success-bg); }
.qe-schedule-card .status-pill.draft { color: var(--warning); background: var(--warning-bg); }
.qe-schedule-meta { display: grid; grid-template-columns: minmax(170px,.8fr) minmax(220px,1fr); gap: 12px; margin: 0; }
.qe-schedule-meta>div { min-width: 0; }
.qe-schedule-meta dt { margin-bottom: 5px; color: var(--faint); font-size: 11px; }
.qe-schedule-meta dd { margin: 0; color: var(--fg); font-size: 13px; font-weight: 600; }
.qe-schedule-due input { width: 100%; height: 36px; min-width: 0; padding: 6px 9px; }
.qe-schedule-save { min-width: 112px; justify-content: center; white-space: nowrap; }
@media (max-width: 1050px) {
  .qe-schedule-card { grid-template-columns: 1fr auto; }
  .qe-schedule-copy { grid-column: 1 / -1; }
}
@media (max-width: 767.98px) {
  .qe-schedule-card { grid-template-columns: 1fr; gap: 14px; padding: 16px; }
  .qe-schedule-copy { grid-column: auto; }
  .qe-schedule-meta { grid-template-columns: 1fr; }
  .qe-schedule-save { width: 100%; }
}

@media (min-width: 1100px) {
  .qe-page { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; align-items: start; }
  .qe-topbar, .qe-schedule-card { grid-column: 1 / -1; }
  .qe-list-card, .qe-body { grid-column: 1; min-width: 0; }
  .qe-side { grid-column: 2; grid-row: 3 / span 2; margin: 0; position: sticky; top: 72px; max-height: calc(100dvh - 340px); overflow: auto; overscroll-behavior: contain; }
  .qe-side-body { display: flex; flex-direction: column; }
  .qe-side-sec { width: 100%; min-width: 0; }
  .qe-side-sec + .qe-side-sec { border-left: 0; border-top: 1px solid var(--border); }
}
</style>
