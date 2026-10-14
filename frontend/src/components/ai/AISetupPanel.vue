<script setup>
// 一键 AI 配置（批次 2）：异步生成草稿 → 可试判 → 保存并启用。
// 生成阶段只产出草稿；教师点「保存并启用」才会锁定标准并启用评分版本。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import AppIcon from '../ui/AppIcon.vue'
import UiProgress from '../ui/UiProgress.vue'
import { aiGradingAPI } from '../../api/aiGrading.js'
import { useAppStore } from '../../stores/app.js'

const props = defineProps({
  kind: { type: String, required: true },
  questionId: { type: Number, required: true },
  referenceSolution: { type: String, default: '' },
  teacherConstraints: { type: Object, default: () => ({}) },
  gradingMode: { type: String, default: 'active' },
  /** 是否同时用 AI 生成 F/R 测试组（关闭时只用评分标准草稿） */
  generateTestGroups: { type: Boolean, default: true },
  /** 弹窗流程里父组件已收集完输入，挂载即开始生成 */
  autoStart: { type: Boolean, default: false },
})
const emit = defineEmits(['enabled'])

const app = useAppStore()
const job = ref(null)
const busy = ref(false)
const trialCode = ref('')
const trial = ref(null)
const enableResult = ref(null)
const stageError = ref('')
const rubricDraft = ref(null)
const groupDraft = ref([])
const entryFunctionDraft = ref('')
const constraintsText = ref('')
const compareRulesDraft = ref({})
const scoreCapRulesDraft = ref([])
const compareRulesJson = ref('{}')
const scoreCapRulesJson = ref('[]')
const savedDraftJson = ref('')
let pollTimer = null

const STAGES = [
  { key: 'generating_rubric', label: '生成评分标准' },
  { key: 'generating_test_groups', label: '生成测试组' },
  { key: 'draft_ready', label: '待确认启用' },
]

const POLL_MS = 2000
const MAX_POLLS = 150

const stageIndex = computed(() => {
  const stage = job.value?.stage
  if (stage === 'enabled') return STAGES.length
  if (stage === 'draft_ready') return 2
  if (stage === 'generating_test_groups') return 1
  if (stage === 'generating_rubric') return 0
  return -1
})
const isRunning = computed(() => ['queued', 'generating_rubric', 'generating_test_groups'].includes(job.value?.stage))
const isReady = computed(() => job.value?.stage === 'draft_ready')
const isFailed = computed(() => job.value?.stage === 'failed')
const progress = computed(() => (job.value?.progress ?? 0))
const rubricStatusText = computed(() => (job.value?.draft_rubric_status === 'locked' ? '已锁定' : '草稿'))
const draftPayload = computed(() => ({
  rubric: rubricDraft.value,
  test_groups: groupDraft.value,
  entry_function: entryFunctionDraft.value,
  teacher_constraints: {
    ...(job.value?.draft_teacher_constraints || props.teacherConstraints || {}),
    requirements_text: constraintsText.value,
  },
  compare_rules: compareRulesDraft.value,
  score_cap_rules: scoreCapRulesDraft.value,
}))
const hasUnsavedDraft = computed(() => Boolean(rubricDraft.value)
  && JSON.stringify({ ...draftPayload.value, compare_rules_json: compareRulesJson.value,
    score_cap_rules_json: scoreCapRulesJson.value }) !== savedDraftJson.value)
function stopPolling() {
  if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null }
}

function copyJson(value) {
  return JSON.parse(JSON.stringify(value ?? {}))
}

function syncDraftFromJob(value) {
  if (!value?.draft_rubric) return
  rubricDraft.value = copyJson(value.draft_rubric)
  groupDraft.value = copyJson(value.draft_test_groups || [])
  entryFunctionDraft.value = value.draft_entry_function || ''
  const constraints = value.draft_teacher_constraints || props.teacherConstraints || {}
  constraintsText.value = String(constraints.requirements_text || '')
  compareRulesDraft.value = copyJson(value.draft_compare_rules || {})
  scoreCapRulesDraft.value = copyJson(value.draft_score_cap_rules || [])
  compareRulesJson.value = JSON.stringify(compareRulesDraft.value, null, 2)
  scoreCapRulesJson.value = JSON.stringify(scoreCapRulesDraft.value, null, 2)
  savedDraftJson.value = JSON.stringify({ ...draftPayload.value,
    compare_rules_json: compareRulesJson.value, score_cap_rules_json: scoreCapRulesJson.value })
}

function addCriterion(dimension) {
  if (!rubricDraft.value) return
  const key = dimension === 'A' ? 'algorithm_criteria' : 'quality_criteria'
  const items = rubricDraft.value[key] || []
  const prefix = dimension
  const used = new Set(items.map((item) => item.id))
  let suffix = items.length + 1
  while (used.has(`${prefix}${suffix}`)) suffix += 1
  items.push({ id: `${prefix}${suffix}`, name: '', description: '', points: 1 })
  rubricDraft.value[key] = items
}

function removeCriterion(dimension, index) {
  const key = dimension === 'A' ? 'algorithm_criteria' : 'quality_criteria'
  rubricDraft.value?.[key]?.splice(index, 1)
}

function addTestGroup() {
  const used = new Set(groupDraft.value.map((group) => group.id))
  let suffix = groupDraft.value.filter((group) => group.dimension === 'F').length + 1
  while (used.has(`F${suffix}`)) suffix += 1
  groupDraft.value.push({ id: `F${suffix}`, name: '', dimension: 'F', max_score: 1, tests: '' })
}

function removeTestGroup(index) {
  groupDraft.value.splice(index, 1)
}

function pollJob(tries = 0) {
  stopPolling()
  pollTimer = window.setTimeout(async () => {
    try {
      const res = await aiGradingAPI.getSetupJob(job.value.id)
      job.value = res.data
      if (res.data.stage === 'draft_ready' && !rubricDraft.value) syncDraftFromJob(res.data)
      if (res.data.stage === 'enabled') { stopPolling(); return }
      if (res.data.stage === 'draft_ready' || res.data.stage === 'failed') { stopPolling(); return }
      if (tries + 1 >= MAX_POLLS) {
        stageError.value = '生成任务长时间没有进展，可稍后重试或点击「重试」继续'
        stopPolling()
        return
      }
      pollJob(tries + 1)
    } catch (e) {
      stageError.value = e.response?.data?.detail?.message || '任务状态查询失败'
      stopPolling()
    }
  }, POLL_MS)
}

async function start() {
  if (busy.value) return
  busy.value = true
  stageError.value = ''
  enableResult.value = null
  trial.value = null
  try {
    const res = await aiGradingAPI.createSetupJob({
      question_kind: props.kind,
      question_id: props.questionId,
      reference_solution: props.referenceSolution || null,
      teacher_constraints: props.teacherConstraints || {},
      grading_mode: props.gradingMode || null,
      generate_test_groups: props.generateTestGroups,
    })
    job.value = res.data
    if (res.data.stage === 'draft_ready') syncDraftFromJob(res.data)
    if (res.data.stage !== 'draft_ready' && res.data.stage !== 'enabled') pollJob()
  } catch (e) {
    stageError.value = e.response?.data?.detail?.message || e.message || '任务创建失败'
  } finally {
    busy.value = false
  }
}

async function retry() {
  if (!job.value || busy.value) return
  busy.value = true
  stageError.value = ''
  try {
    const res = await aiGradingAPI.retrySetupJob(job.value.id)
    job.value = res.data
    pollJob()
  } catch (e) {
    stageError.value = e.response?.data?.detail?.message || '重试失败'
  } finally {
    busy.value = false
  }
}

async function saveDraft() {
  if (!job.value || !rubricDraft.value || busy.value) return false
  busy.value = true
  stageError.value = ''
  try {
    const compareRules = JSON.parse(compareRulesJson.value)
    const scoreCapRules = JSON.parse(scoreCapRulesJson.value)
    if (!compareRules || Array.isArray(compareRules) || typeof compareRules !== 'object'
        || !Array.isArray(scoreCapRules)) {
      throw new Error('比较规则必须是 JSON 对象，封顶规则必须是 JSON 数组')
    }
    compareRulesDraft.value = compareRules
    scoreCapRulesDraft.value = scoreCapRules
    const res = await aiGradingAPI.saveSetupDraft(job.value.id, {
      expected_revision: job.value.draft_revision,
      ...copyJson(draftPayload.value),
      compare_rules: compareRules,
      score_cap_rules: scoreCapRules,
    })
    job.value = res.data
    syncDraftFromJob(res.data)
    app.showToast(`草稿已保存（修订 ${res.data.draft_revision}）`, 'success')
    return true
  } catch (e) {
    const detail = e.response?.data?.detail
    stageError.value = detail?.message || e.message || '草稿保存失败'
    return false
  } finally {
    busy.value = false
  }
}

async function runTrial() {
  if (!job.value || !trialCode.value.trim() || busy.value) return
  if (hasUnsavedDraft.value && !(await saveDraft())) return
  busy.value = true
  stageError.value = ''
  try {
    const created = await aiGradingAPI.createTrialRun(job.value.id, {
      expected_revision: job.value.draft_revision,
      code: trialCode.value,
      client_request_id: `trial-${job.value.id}-${Date.now()}`,
    })
    let run = created.data
    for (let i = 0; i < MAX_POLLS && ['queued', 'running'].includes(run.status); i += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, POLL_MS))
      run = (await aiGradingAPI.getTrialRun(run.id)).data
    }
    trial.value = run
    if (run.status === 'failed') app.showToast(run.error_message || '试判失败', 'error')
  } catch (e) {
    stageError.value = e.response?.data?.detail?.message || '试判失败'
  } finally {
    busy.value = false
  }
}

async function enable() {
  if (!job.value || busy.value) return
  if (hasUnsavedDraft.value && !(await saveDraft())) return
  busy.value = true
  stageError.value = ''
  try {
    const res = await aiGradingAPI.enableSetupJob(job.value.id, {})
    job.value = { ...job.value, stage: 'enabled', progress: 100 }
    enableResult.value = res.data
    app.showToast(`评分标准已锁定并启用（版本 v${res.data.version}）`, 'success')
    emit('enabled', res.data)
  } catch (e) {
    const detail = e.response?.data?.detail
    if (e.response?.status === 422 && detail?.fields?.violations) {
      stageError.value = detail.fields.violations.map((v) => v.message).join('；')
    } else {
      stageError.value = detail?.message || '启用失败，请稍后重试'
    }
  } finally {
    busy.value = false
  }
}

onMounted(() => { if (props.autoStart) start() })
onBeforeUnmount(stopPolling)
</script>

<template>
  <section class="ai-setup" aria-label="一键 AI 配置">
    <header class="setup-head">
      <div>
        <p class="section-eyebrow">一键 AI 配置</p>
        <h2>生成 → 预览 → 保存并启用</h2>
      </div>
      <button v-if="!job" type="button" class="btn btn-primary btn-sm" :disabled="busy" @click="start">
        <AppIcon name="brain" :size="14" />一键生成草稿
      </button>
      <button v-else-if="isFailed" type="button" class="btn btn-secondary btn-sm" :disabled="busy" @click="retry">重试（保留草稿）</button>
    </header>

    <ol v-if="job" class="stage-list">
      <li v-for="(stage, index) in STAGES" :key="stage.key" class="stage-item" :class="{ done: stageIndex > index, active: stageIndex === index }">
        <span class="stage-dot" aria-hidden="true"></span>
        <span class="stage-label">{{ stage.label }}</span>
        <span v-if="stageIndex === index && isRunning" class="stage-note">进行中</span>
        <span v-else-if="stageIndex > index" class="stage-note">完成</span>
      </li>
    </ol>

    <UiProgress v-if="job && isRunning" :value="progress" />

    <p v-if="job" class="setup-meta">
      任务 #{{ job.id }} · 当前阶段 {{ job.stage }} · 评分标准状态：{{ rubricStatusText }}
      <template v-if="job.draft_test_groups?.length">· 已生成 {{ job.draft_test_groups.length }} 组测试</template>
    </p>
    <p v-if="isFailed && job" class="setup-error" role="alert">
      生成失败：{{ job.error_message || job.error_code }}（已生成的草稿会保留）
    </p>
    <p v-if="stageError" class="setup-error" role="alert">{{ stageError }}</p>

    <template v-if="isReady && rubricDraft">
      <div class="setup-block">
        <div class="draft-heading">
          <div>
            <h3>编辑评分草稿（尚未锁定）</h3>
            <p class="setup-hint">A 合计需 20 分，Q 合计需 10 分；F/R 满分上限为 60/10，启用时会校验完整配置。</p>
          </div>
          <span v-if="hasUnsavedDraft" class="unsaved-badge">未保存修改</span>
        </div>
        <label class="field"><span>入口函数</span><input v-model="entryFunctionDraft" class="input" /></label>
        <label class="field"><span>教师约束</span><textarea v-model="constraintsText" class="input" rows="2" placeholder="描述评分时必须遵守的要求"></textarea></label>
        <details class="advanced-draft">
          <summary>高级评分配置（现有 JSON 格式）</summary>
          <label class="field"><span>比较规则</span><textarea v-model="compareRulesJson" class="input setup-code" rows="4" spellcheck="false"></textarea></label>
          <label class="field"><span>封顶规则</span><textarea v-model="scoreCapRulesJson" class="input setup-code" rows="5" spellcheck="false"></textarea></label>
        </details>

        <div class="criterion-section">
          <div class="draft-heading">
            <h4 class="preview-title">A · 算法评分项（{{ rubricDraft.algorithm_criteria.reduce((n, item) => n + Number(item.points || 0), 0) }} / 20）</h4>
            <button type="button" class="btn btn-secondary btn-sm" @click="addCriterion('A')">添加 A 项</button>
          </div>
          <div v-for="(item, index) in rubricDraft.algorithm_criteria" :key="`a-${index}`" class="draft-row">
            <label class="field"><span>标识</span><input v-model="item.id" class="input" maxlength="20" /></label>
            <label class="field"><span>名称</span><input v-model="item.name" class="input" maxlength="200" /></label>
            <label class="field points-field"><span>分值</span><input v-model.number="item.points" class="input" type="number" min="0.01" max="20" step="0.1" /></label>
            <button type="button" class="btn btn-ghost btn-sm" @click="removeCriterion('A', index)">删除</button>
            <label class="field description-field"><span>说明</span><textarea v-model="item.description" class="input" rows="2" maxlength="500" /></label>
          </div>
        </div>

        <div class="criterion-section">
          <div class="draft-heading">
            <h4 class="preview-title">Q · 代码质量评分项（{{ rubricDraft.quality_criteria.reduce((n, item) => n + Number(item.points || 0), 0) }} / 10）</h4>
            <button type="button" class="btn btn-secondary btn-sm" @click="addCriterion('Q')">添加 Q 项</button>
          </div>
          <div v-for="(item, index) in rubricDraft.quality_criteria" :key="`q-${index}`" class="draft-row">
            <label class="field"><span>标识</span><input v-model="item.id" class="input" maxlength="20" /></label>
            <label class="field"><span>名称</span><input v-model="item.name" class="input" maxlength="200" /></label>
            <label class="field points-field"><span>分值</span><input v-model.number="item.points" class="input" type="number" min="0.01" max="20" step="0.1" /></label>
            <button type="button" class="btn btn-ghost btn-sm" @click="removeCriterion('Q', index)">删除</button>
            <label class="field description-field"><span>说明</span><textarea v-model="item.description" class="input" rows="2" maxlength="500" /></label>
          </div>
        </div>

        <div class="criterion-section">
          <div class="draft-heading">
            <h4 class="preview-title">F/R 确定性测试组</h4>
            <button type="button" class="btn btn-secondary btn-sm" @click="addTestGroup">添加测试组</button>
          </div>
          <p class="setup-hint">每组使用现有隔离 pytest runner。在测试代码中可以增删用例；样例代码不会写入正式提交。</p>
          <div v-for="(group, index) in groupDraft" :key="`g-${index}`" class="group-editor">
            <div class="draft-row">
              <label class="field"><span>标识</span><input v-model="group.id" class="input" maxlength="40" /></label>
              <label class="field"><span>名称</span><input v-model="group.name" class="input" maxlength="120" /></label>
              <label class="field"><span>维度</span><select v-model="group.dimension" class="input"><option value="F">F 功能</option><option value="R">R 边界</option></select></label>
              <label class="field points-field"><span>满分</span><input v-model.number="group.max_score" class="input" type="number" min="0.01" max="60" step="0.1" /></label>
              <button type="button" class="btn btn-ghost btn-sm" @click="removeTestGroup(index)">删除</button>
            </div>
            <label class="field"><span>测试代码</span><textarea v-model="group.tests" class="input setup-code" rows="7" placeholder="def test_example():\n    assert solve(2) == 2"></textarea></label>
          </div>
        </div>
        <button type="button" class="btn btn-secondary btn-sm draft-save" :disabled="busy || !hasUnsavedDraft" @click="saveDraft">{{ busy ? '保存中…' : `保存草稿（修订 ${job.draft_revision}）` }}</button>
      </div>

      <div class="setup-block">
        <h3>可选：完整草稿试判</h3>
        <p class="setup-hint">先用隔离判题容器运行 F/R，再由无 Docker 权限的 AI worker 评分 A/Q。完整成功才显示总分；试判不生成正式成绩、也不计入提交次数、成绩册或排行榜。</p>
        <textarea v-model="trialCode" class="input setup-code trial-code" rows="4" placeholder="粘贴一段样例代码，例如：&#10;def solve(n):&#10;    return n"></textarea>
        <button type="button" class="btn btn-secondary btn-sm" :disabled="busy || !trialCode.trim()" @click="runTrial">运行试判</button>
        <div v-if="trial" class="trial-result">
          <p v-if="trial.stale_draft" class="setup-hint">此结果对应旧草稿修订 {{ trial.draft_revision }}，当前修订为 {{ trial.current_draft_revision }}。</p>
          <p v-if="trial.trial_type === 'ai_only'" class="setup-hint">旧版 A/Q 预览：不含 F/R，不能作为完整试判结果。</p>
          <p v-if="trial.status === 'completed' && trial.complete">完整试判：F {{ trial.functional_score }} / R {{ trial.robustness_score }} / A {{ trial.algorithm_score }} / Q {{ trial.quality_score }}；总分 {{ trial.total_score }} / {{ trial.score_max }}。</p>
          <p v-else-if="trial.status === 'completed' && trial.trial_type === 'ai_only'">A {{ trial.algorithm_score }} / Q {{ trial.quality_score }}，旧版 AI 维度预览 {{ trial.preview_score_100 }} 分。</p>
          <p v-else-if="trial.status === 'failed'" class="setup-error">试判未完成：{{ trial.error_message || trial.error_code }}<template v-if="trial.functional_score != null || trial.robustness_score != null"> · 已保留 F/R 阶段结果；没有完整总分。</template></p>
          <p v-else>试判{{ trial.stage || trial.status }}…</p>
        </div>
      </div>

      <div class="setup-block">
        <h3>保存并启用</h3>
        <p class="setup-hint">锁定服务端当前保存的草稿、写入题目配置并启用评分版本。试判是可选的。</p>
        <button type="button" class="btn btn-primary btn-sm" :disabled="busy" @click="enable">{{ hasUnsavedDraft ? '保存修改并启用' : '保存并启用' }}</button>
      </div>
    </template>

    <p v-if="enableResult" class="setup-ok" role="status">
      已启用评分版本 v{{ enableResult.version }}（Rubric #{{ enableResult.rubric_id }}）
    </p>
  </section>
</template>

<style scoped>
.ai-setup { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.setup-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.setup-head h2 { margin: 2px 0 0; font-size: var(--text-lg); }
.stage-list { display: flex; gap: var(--space-4); margin: 0; padding: 0; list-style: none; flex-wrap: wrap; }
.stage-item { display: flex; align-items: center; gap: var(--space-2); color: var(--faint); font-size: var(--text-sm); }
.stage-item.active { color: var(--fg); font-weight: 600; }
.stage-item.done { color: var(--muted); }
.stage-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--border-strong); }
.stage-item.active .stage-dot { background: var(--accent); }
.stage-item.done .stage-dot { background: var(--success, var(--accent)); }
.setup-meta { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.setup-error { margin: 0; color: var(--danger); font-size: var(--text-sm); }
.setup-ok { margin: 0; color: var(--success); font-size: var(--text-sm); }
.setup-block { display: flex; flex-direction: column; gap: var(--space-2); padding-top: var(--space-3); border-top: 1px solid var(--border); }
.setup-block h3 { margin: 0; font-size: var(--text-base); }
.setup-hint { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.setup-code { width: 100%; font-family: var(--font-mono); font-size: var(--text-sm); resize: vertical; }
.trial-result { color: var(--muted); font-size: var(--text-sm); }
.preview-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: var(--space-3); }
.preview-title { margin: 0 0 var(--space-1); font-size: var(--text-sm); color: var(--muted); }
.preview-list { margin: 0; padding-left: var(--space-4); color: var(--fg); font-size: var(--text-sm); }
.trial-result p { margin: 0; }
.draft-heading { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.draft-heading h3, .draft-heading h4 { margin: 0; }
.unsaved-badge { padding: 3px 8px; border-radius: 999px; background: var(--warning-soft, var(--accent-soft)); color: var(--fg); font-size: var(--text-xs); white-space: nowrap; }
.criterion-section { display: flex; flex-direction: column; gap: var(--space-2); padding-top: var(--space-3); border-top: 1px solid var(--border); }
.draft-row { display: grid; grid-template-columns: minmax(70px, 0.7fr) minmax(130px, 1.4fr) minmax(85px, 0.6fr) auto; align-items: end; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-md); }
.draft-row .field, .field { display: flex; flex-direction: column; gap: var(--space-1); color: var(--muted); font-size: var(--text-sm); }
.points-field { max-width: 120px; }
.description-field { grid-column: 1 / -1; }
.group-editor { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-md); }
.draft-save { align-self: flex-start; }
.advanced-draft { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-md); }
.advanced-draft summary { color: var(--muted); font-size: var(--text-sm); cursor: pointer; }
@media (max-width: 640px) {
  .draft-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .description-field { grid-column: 1 / -1; }
}
</style>
