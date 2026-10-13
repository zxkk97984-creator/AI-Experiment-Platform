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
const rubricPreview = computed(() => {
  const doc = job.value?.draft_rubric || {}
  return {
    algorithm: (doc.algorithm_criteria || []).slice(0, 8),
    quality: (doc.quality_criteria || []).slice(0, 8),
  }
})
const rubricStatusText = computed(() => (job.value?.draft_rubric_status === 'locked' ? '已锁定' : '草稿'))

function stopPolling() {
  if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null }
}

function pollJob(tries = 0) {
  stopPolling()
  pollTimer = window.setTimeout(async () => {
    try {
      const res = await aiGradingAPI.getSetupJob(job.value.id)
      job.value = res.data
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

async function runTrial() {
  if (!job.value || !trialCode.value.trim() || busy.value) return
  busy.value = true
  stageError.value = ''
  try {
    const created = await aiGradingAPI.createTrialRun(job.value.id, {
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

    <template v-if="isReady">
      <div class="setup-block">
        <h3>预览草稿（尚未锁定）</h3>
        <div class="preview-grid">
          <div>
            <h4 class="preview-title">评分标准</h4>
            <ul class="preview-list">
              <li v-for="item in rubricPreview.algorithm" :key="item.id">A · {{ item.name }}（{{ item.points }} 分）</li>
              <li v-for="item in rubricPreview.quality" :key="item.id">Q · {{ item.name }}（{{ item.points }} 分）</li>
            </ul>
          </div>
          <div>
            <h4 class="preview-title">测试组（F/R）</h4>
            <ul class="preview-list">
              <li v-for="group in job.draft_test_groups" :key="group.id">{{ group.dimension }} · {{ group.name }}（{{ group.max_score }} 分）</li>
              <li v-if="!job.draft_test_groups || !job.draft_test_groups.length">未生成测试组——保存并启用需先用高级面板配置 F/R 测试组</li>
            </ul>
          </div>
        </div>
      </div>

      <div class="setup-block">
        <h3>可选：草稿试判</h3>
        <p class="setup-hint">用当前草稿规则试跑一段样例代码。试判不生成正式提交、不消耗学生提交次数，也不包含 F/R 确定性分。</p>
        <textarea v-model="trialCode" class="input setup-code" rows="4" placeholder="粘贴一段样例代码，例如：&#10;def solve(n):&#10;    return n"></textarea>
        <button type="button" class="btn btn-secondary btn-sm" :disabled="busy || !trialCode.trim()" @click="runTrial">运行试判</button>
        <div v-if="trial" class="trial-result">
          <p v-if="trial.status === 'completed'">
            试判结果：A {{ trial.algorithm_score }} / Q {{ trial.quality_score }}，预览 AI 维度合计 {{ trial.preview_score_100 }} 分。
            试判不包含确定性 F/R 分，正式成绩仍以完整判题结果为准。
          </p>
          <p v-else-if="trial.status === 'failed'" class="setup-error">试判失败：{{ trial.error_message || trial.error_code }}</p>
          <p v-else>试判进行中…</p>
        </div>
      </div>

      <div class="setup-block">
        <h3>保存并启用</h3>
        <p class="setup-hint">锁定当前草稿评分标准、写入题目配置并启用评分版本（单事务，失败整体回滚）。</p>
        <button type="button" class="btn btn-primary btn-sm" :disabled="busy" @click="enable">保存并启用</button>
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
</style>
