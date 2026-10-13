<script setup>
// 管理员练习题编辑页：录入题目、判题用例与参考解法，配置 AI 评分标准（复用批次 2 组件），发布 / 归档。
// 六态：加载 / 空数据（新建未保存或题目不存在）/ 失败 / 排队（AI 生成或判题队列）/ 额度耗尽 / 权限不足。
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import DsSelect from '../../components/ui/DsSelect.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import AISetupPanel from '../../components/ai/AISetupPanel.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { isPermissionError, isQuotaError, missingRequirementsText, practiceErrorMessage, problemStatusMeta } from '../../components/practice/practiceHelpers.js'
import { adminPracticeAPI } from '../../api/practice.js'
import { useAppStore } from '../../stores/app.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const isNew = computed(() => route.params.id === 'new' || route.params.id === undefined)
const problemId = computed(() => (isNew.value ? null : Number(route.params.id)))

const loading = ref(!isNew.value)
const error = ref('')
const forbidden = ref(false)
const notFound = ref(false)
const saving = ref(false)
const actionBusy = ref(false)
const pendingAction = ref('')
const preflight = ref(null)
const problem = ref(null)
const jsonErrors = ref({})

const form = ref({
  title: '',
  difficulty: 'easy',
  tags: '',
  prompt: '',
  starter_code: '',
  public_cases: '[]',
  hidden_tests: '[]',
  time_limit_ms: 2000,
  memory_limit_kb: 262144,
  reference_solution: '',
  teacher_constraints: '',
})

const hasId = computed(() => Boolean(problemId.value))
const status = computed(() => problemStatusMeta(problem.value?.status || 'draft'))
const aiReady = computed(() => {
  const entry = (preflight.value?.problems || []).find((item) => item.id === problemId.value)
  if (preflight.value?.ready === true && !entry) return true
  return !entry
})
const aiMissing = computed(() => {
  const entry = (preflight.value?.problems || []).find((item) => item.id === problemId.value)
  return entry?.missing || []
})
const queue = computed(() => preflight.value?.queue || {})
const aiBudget = computed(() => preflight.value?.ai_budget || {})
const quotaExhausted = computed(() => aiBudget.value.exhausted === true)

function applyProblem(data) {
  problem.value = data
  form.value = {
    title: data.title || '',
    difficulty: data.difficulty || 'easy',
    tags: (data.tags || []).join(', '),
    prompt: data.prompt || '',
    starter_code: data.starter_code || '',
    public_cases: JSON.stringify(data.public_cases || [], null, 2),
    hidden_tests: JSON.stringify(data.hidden_tests || [], null, 2),
    time_limit_ms: data.time_limit_ms ?? 2000,
    memory_limit_kb: data.memory_limit_kb ?? 262144,
    reference_solution: data.reference_solution || '',
    teacher_constraints: data.teacher_constraints ? JSON.stringify(data.teacher_constraints, null, 2) : '',
  }
}

async function loadPreflight() {
  try {
    const res = await adminPracticeAPI.getAiPreflight()
    preflight.value = res.data
    if (res.data?.ai_budget?.exhausted === true) app.showToast('今日额度已用完', 'warning')
  } catch (e) {
    if (isPermissionError(e)) forbidden.value = true
    // 预检失败不阻塞录题，用页面提示条告知即可
    if (!forbidden.value) app.showToast(practiceErrorMessage(e, 'AI 预检状态加载失败'), 'warning')
  }
}

async function load() {
  if (isNew.value) {
    loading.value = false
    await loadPreflight()
    return
  }
  loading.value = true
  error.value = ''
  forbidden.value = false
  notFound.value = false
  try {
    const detail = await adminPracticeAPI.getProblem(problemId.value)
    applyProblem(detail.data)
    await loadPreflight()
  } catch (e) {
    if (isPermissionError(e)) forbidden.value = true
    else if (e.response?.status === 404) notFound.value = true
    else error.value = practiceErrorMessage(e, '题目加载失败，请稍后重试')
    if (error.value) app.showToast(error.value, 'error')
  } finally {
    loading.value = false
  }
}

function parseJsonField(key, label) {
  const raw = String(form.value[key] || '').trim()
  if (!raw) return undefined
  try {
    return JSON.parse(raw)
  } catch {
    jsonErrors.value = { ...jsonErrors.value, [key]: `${label}不是合法的 JSON` }
    return null
  }
}

function buildPayload() {
  jsonErrors.value = {}
  const publicCases = parseJsonField('public_cases', '公开用例')
  const hiddenTests = parseJsonField('hidden_tests', '隐藏测试')
  let constraints
  if (String(form.value.teacher_constraints || '').trim()) {
    try {
      constraints = JSON.parse(form.value.teacher_constraints)
    } catch {
      jsonErrors.value = { ...jsonErrors.value, teacher_constraints: '评分侧重不是合法的 JSON' }
    }
  }
  if (Object.keys(jsonErrors.value).length) return null
  return {
    title: form.value.title.trim(),
    difficulty: form.value.difficulty,
    tags: form.value.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
    prompt: form.value.prompt,
    starter_code: form.value.starter_code,
    public_cases: publicCases ?? [],
    hidden_tests: hiddenTests ?? [],
    time_limit_ms: Number(form.value.time_limit_ms) || 2000,
    memory_limit_kb: Number(form.value.memory_limit_kb) || 262144,
    reference_solution: form.value.reference_solution,
    teacher_constraints: constraints ?? null,
  }
}

async function save({ silent = false } = {}) {
  const payload = buildPayload()
  if (!payload) return null
  if (!payload.title) {
    app.showToast('请填写题目名称', 'error')
    return null
  }
  saving.value = true
  try {
    const res = hasId.value
      ? await adminPracticeAPI.updateProblem(problemId.value, payload)
      : await adminPracticeAPI.createProblem(payload)
    applyProblem(res.data)
    if (!silent) app.showToast('已保存', 'success')
    if (!hasId.value && res.data?.id) await router.replace(`/admin/practice/problems/${res.data.id}/edit`)
    await loadPreflight()
    return res.data
  } catch (e) {
    if (isQuotaError(e)) app.showToast('今日额度已用完', 'error')
    else app.showToast(practiceErrorMessage(e, '保存失败，请稍后重试'), 'error')
    return null
  } finally {
    saving.value = false
  }
}

async function publish() {
  if (!hasId.value) {
    const saved = await save({ silent: true })
    if (!saved) return
  }
  pendingAction.value = 'publish'
}

async function archive() {
  pendingAction.value = 'archive'
}

async function confirmAction() {
  const action = pendingAction.value
  if (!action) return
  actionBusy.value = true
  try {
    const res = action === 'publish'
      ? await adminPracticeAPI.publishProblem(problemId.value)
      : await adminPracticeAPI.archiveProblem(problemId.value)
    applyProblem(res.data)
    app.showToast(action === 'publish' ? '已发布，学生现在可以看到该题' : '已归档，历史提交与成绩保留', 'success')
    pendingAction.value = ''
  } catch (e) {
    app.showToast(practiceErrorMessage(e, '操作失败，请稍后重试'), 'error')
  } finally {
    actionBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="detail">
    <section class="problem-edit">
      <nav class="crumb-row" aria-label="面包屑">
        <button type="button" class="btn btn-ghost btn-sm" @click="router.push('/admin/practice/problems')">
          <AppIcon name="back" :size="14" />返回题库
        </button>
      </nav>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        练习题库仅管理员可维护；教师账号只能查看只读排行榜。
      </PracticeNotice>

      <template v-else>
        <PracticeNotice v-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
          AI 生成任务需要等预算恢复后才能提交；题目内容仍可编辑与发布。
        </PracticeNotice>
        <PracticeNotice
          v-if="Number(queue.queued) > 0"
          tone="warning"
          :title="`练习判题队列积压 ${queue.queued} 项`"
        >
          练习队列与正式作业、考试队列隔离，超额提交会明确排队。
        </PracticeNotice>

        <DashboardAsyncState
          :loading="loading"
          :error="Boolean(error) && !forbidden && !notFound"
          :empty="notFound"
          empty-title="题目不存在"
          empty-body="该题可能已被删除，返回题库查看其它题目。"
          @retry="load"
        >
          <header class="edit-head">
            <div class="ph-title">
              <p class="eyebrow">{{ isNew ? 'NEW PRACTICE PROBLEM' : 'EDIT PRACTICE PROBLEM' }}</p>
              <h1>{{ isNew ? '新建练习题' : form.title || '未命名练习题' }}</h1>
              <p class="head-meta">
                <UiStatusPill :tone="status.tone" :label="status.label" />
                <UiStatusPill
                  v-if="hasId"
                  :tone="aiReady ? 'success' : 'warning'"
                  :label="aiReady ? 'AI 评分标准已就绪' : 'AI 评分标准未就绪'"
                />
                <span v-if="hasId" class="meta mono">题目 ID {{ problemId }}</span>
              </p>
            </div>
            <div class="head-actions">
              <button type="button" class="btn btn-secondary" :disabled="saving" @click="save()">
                {{ saving ? '保存中…' : '保存' }}
              </button>
              <button
                v-if="problem?.status !== 'published'"
                type="button"
                class="btn btn-primary"
                :disabled="saving || actionBusy"
                @click="publish"
              >发布</button>
              <button
                v-else
                type="button"
                class="btn btn-danger"
                :disabled="actionBusy"
                @click="archive"
              >归档</button>
            </div>
          </header>

          <PracticeNotice v-if="isNew" tone="info" title="尚未保存的草稿">
            保存后才会生成题目记录，AI 评分标准配置在学生可见之前必须准备完成。
          </PracticeNotice>

          <PracticeNotice
            v-if="hasId && !aiReady"
            tone="warning"
            title="AI 评分标准未就绪"
            assertive
          >
            缺少：{{ missingRequirementsText(aiMissing) || '评分标准或评分版本' }}。切换到 AI 综合判题前必须补齐，否则全局切换会被拒绝。
          </PracticeNotice>

          <div class="edit-grid">
            <section class="form-panel">
              <h2 class="panel-title">基础信息</h2>
              <label class="field">
                <span class="field-label">题目名称</span>
                <input v-model="form.title" class="input" type="text" placeholder="例如：统计列表中所有正数的和" />
              </label>
              <div class="two-col">
                <label class="field">
                  <span class="field-label">难度</span>
                  <DsSelect v-model="form.difficulty" aria-label="难度">
                    <option value="easy">简单</option>
                    <option value="medium">中等</option>
                    <option value="hard">困难</option>
                  </DsSelect>
                </label>
                <label class="field">
                  <span class="field-label">标签（逗号分隔）</span>
                  <input v-model="form.tags" class="input" type="text" placeholder="列表, 字符串, 循环" />
                </label>
              </div>
              <label class="field">
                <span class="field-label">题目描述</span>
                <textarea v-model="form.prompt" class="textarea" rows="5" placeholder="描述输入、输出与边界条件"></textarea>
              </label>
              <label class="field">
                <span class="field-label">初始代码</span>
                <textarea v-model="form.starter_code" class="textarea mono" rows="4" placeholder="def solve(values):&#10;    ..."></textarea>
              </label>
              <div class="two-col">
                <label class="field">
                  <span class="field-label">时间限制（毫秒）</span>
                  <input v-model.number="form.time_limit_ms" class="input" type="number" min="100" max="60000" />
                </label>
                <label class="field">
                  <span class="field-label">内存限制（KB）</span>
                  <input v-model.number="form.memory_limit_kb" class="input" type="number" min="16384" max="2097152" />
                </label>
              </div>
            </section>

            <section class="form-panel">
              <h2 class="panel-title">判题与评分</h2>
              <label class="field">
                <span class="field-label">公开用例（JSON 数组）</span>
                <textarea v-model="form.public_cases" class="textarea mono" rows="5" placeholder='[{"args": [[1, -2, 3]], "expected": 4}]'></textarea>
                <span v-if="jsonErrors.public_cases" class="field-error">{{ jsonErrors.public_cases }}</span>
              </label>
              <label class="field">
                <span class="field-label">隐藏测试（JSON 数组）</span>
                <textarea v-model="form.hidden_tests" class="textarea mono" rows="5" placeholder='[{"args": [[]], "expected": 0}]'></textarea>
                <span v-if="jsonErrors.hidden_tests" class="field-error">{{ jsonErrors.hidden_tests }}</span>
              </label>
              <label class="field">
                <span class="field-label">参考解法</span>
                <textarea v-model="form.reference_solution" class="textarea mono" rows="5" placeholder="用于生成 AI 评分标准的参考实现"></textarea>
              </label>
              <label class="field">
                <span class="field-label">评分侧重（JSON，可选）</span>
                <textarea v-model="form.teacher_constraints" class="textarea mono" rows="3" placeholder='{"focus": ["边界条件", "复杂度"]}'></textarea>
                <span v-if="jsonErrors.teacher_constraints" class="field-error">{{ jsonErrors.teacher_constraints }}</span>
              </label>
            </section>
          </div>

          <section class="ai-panel">
            <h2 class="panel-title">AI 评分标准</h2>
            <p class="panel-sub">复用批次 2 的一键配置：生成草稿 → 微调 / 试判 → 保存并启用。只有启用后 AI 综合判题才会使用该题。</p>
            <AISetupPanel
              v-if="hasId && form.reference_solution"
              kind="practice_problem"
              :question-id="problemId"
              :reference-solution="form.reference_solution"
              :grading-mode="problem?.grading_mode || 'deterministic'"
              @enabled="loadPreflight"
            />
            <PracticeNotice v-else-if="!hasId" tone="info" title="保存草稿后可配置 AI 评分标准">
              先保存题目，再填写参考解法并生成评分标准。
            </PracticeNotice>
            <PracticeNotice v-else tone="info" title="请先填写参考解法">
              参考解法是生成评分标准的必要输入。
            </PracticeNotice>
          </section>
        </DashboardAsyncState>
      </template>

      <ConfirmDialog
        v-if="pendingAction"
        :title="pendingAction === 'publish' ? '发布练习题' : '归档练习题'"
        :message="pendingAction === 'publish' ? `发布后学生可在练习中心看到《${form.title}》。` : `归档后学生不再看到《${form.title}》，历史提交与成绩保留。`"
        :confirm-text="pendingAction === 'publish' ? '发布' : '归档'"
        :danger="pendingAction === 'archive'"
        :busy="actionBusy"
        @confirm="confirmAction"
        @cancel="pendingAction = ''"
      />
    </section>
  </AppLayout>
</template>

<style scoped>
.problem-edit { display: flex; flex-direction: column; gap: var(--space-4); }
.crumb-row { display: flex; }
.edit-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; }
.edit-head h1 { margin: 0; font-family: var(--font-display); font-size: var(--text-3xl); overflow-wrap: anywhere; }
.head-meta { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; margin: var(--space-3) 0 0; }
.head-actions { display: flex; gap: var(--space-2); }
.meta { color: var(--muted); font-size: var(--text-sm); }
.edit-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: var(--space-4); align-items: start; }
.form-panel, .ai-panel { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.panel-title { margin: 0; font-size: var(--text-md); }
.panel-sub { margin: 0; color: var(--muted); font-size: var(--text-md); }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-3); }
.mono { font-family: var(--font-mono); }
@media (max-width: 640px) {
  .two-col { grid-template-columns: 1fr; }
}
</style>
