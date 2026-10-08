<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { computed, ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import AIQuestionConfig from '../../components/ai/AIQuestionConfig.vue'
import ChoiceOptionsEditor from '../../components/teacher/exam/ChoiceOptionsEditor.vue'
import FillBlankEditor from '../../components/teacher/exam/FillBlankEditor.vue'
import QeTestCases from '../../components/teacher/question-editor/QeTestCases.vue'
import TaskAudiencePicker from '../../components/teacher/TaskAudiencePicker.vue'
import { examsAPI } from '../../api/exams.js'
import { useAppStore } from '../../stores/app.js'
import { taskEditorReturn } from '../../utils/taskEditorNavigation.js'
const route = useRoute(); const router = useRouter(); const app = useAppStore()
const examId = route.params.id; const exam = ref(null); const questions = ref([])
const returnNavigation = computed(() => taskEditorReturn({ role: route.meta?.role, kind: 'exam', task: exam.value, query: route.query }))
const loading = ref(true); const showForm = ref(false); const editingQ = ref(null)
const aiConfigQid = ref(null)  // 当前展开 AI 配置的题目 ID
const choiceEditor = ref(null)
const testCasesKey = ref(0)
const modalAiExpanded = ref(true)
const settings = ref({ title: '', duration_minutes: 60, start_at: '', end_at: '', show_score_after_grading: false, show_questions_after_review: false, show_answers_after_review: false })
const settingsReady = ref(false)
const settingsSaving = ref(false)
const audienceMode = ref('all_enrolled')
const audienceClassIds = ref([])
const audienceWhitelistIds = ref([])
const audienceExcludedIds = ref([])

function blankChoice() {
  return { options: [{ key: 'A', text: '', correct: false }, { key: 'B', text: '', correct: false }], scoring_mode: 'all_or_nothing' }
}
function blankForm(type = 'single_choice') {
  return {
    question_type: type, prompt: '', points: 1, choice: blankChoice(), fill_blanks: [],
    starter_code: '', public_cases: [], hidden_tests: '', time_limit_ms: 10000,
    memory_limit_mb: 256, grading_mode: type === 'code' ? 'active' : 'legacy',
  }
}
const form = ref(blankForm())

function toLocalInput(value) {
  if (!value) return ''
  const date = new Date(value)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}
function onAudienceImported() { load() }

async function load() {
  loading.value = true
  try {
    const [eR, qR] = await Promise.all([examsAPI.get(examId), examsAPI.getQuestions(examId)])
    exam.value = eR.data
    questions.value = qR.data.items || []
    if (!settingsReady.value) {
      settings.value = {
        title: exam.value.title || '', duration_minutes: exam.value.duration_minutes || 60,
        start_at: toLocalInput(exam.value.start_at), end_at: toLocalInput(exam.value.end_at),
        show_score_after_grading: Boolean(exam.value.show_score_after_grading),
        show_questions_after_review: Boolean(exam.value.show_questions_after_review),
        show_answers_after_review: Boolean(exam.value.show_answers_after_review),
      }
      audienceMode.value = exam.value.audience_mode || 'all_enrolled'
      audienceClassIds.value = [...(exam.value.audience_class_ids || [])]
      audienceWhitelistIds.value = [...(exam.value.whitelist_student_ids || [])]
      audienceExcludedIds.value = [...(exam.value.excluded_student_ids || [])]
      settingsReady.value = true
    }
  } catch { app.showToast('加载失败', 'error') } finally { loading.value = false }
}
function openAdd() {
  editingQ.value = null
  form.value = blankForm()
  modalAiExpanded.value = true
  testCasesKey.value++
  showForm.value = true
}
function openEdit(q) {
  editingQ.value = q.id
  const correct = new Set(q.correct_answer?.correct || [])
  form.value = {
    ...blankForm(q.question_type), question_type: q.question_type, prompt: q.prompt || '', points: q.points || 1,
    choice: {
      options: Object.entries(q.options || {}).map(([key, text]) => ({ key, text, correct: correct.has(key) })),
      scoring_mode: q.correct_answer?.scoring_mode || 'all_or_nothing',
    },
    fill_blanks: (q.correct_answer?.blanks || []).map(blank => ({ ...blank, accepted_answers: [...(blank.accepted_answers || [])] })),
    starter_code: q.starter_code || '', public_cases: q.public_cases || [], hidden_tests: q.hidden_tests || '',
    time_limit_ms: q.time_limit_ms || 10000, memory_limit_mb: q.memory_limit_mb || 256,
    grading_mode: q.grading_mode || (q.question_type === 'code' ? 'active' : 'legacy'),
  }
  if (['single_choice', 'multi_choice'].includes(q.question_type) && form.value.choice.options.length < 2) form.value.choice = blankChoice()
  modalAiExpanded.value = true
  testCasesKey.value++
  showForm.value = true
}

const functionName = computed(() => {
  const match = /^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/m.exec(form.value.starter_code || '')
  return match?.[1] || 'solution'
})

function configStatus(q) {
  if (q.question_type !== 'code') return { ok: true, text: '' }
  if (q.grading_mode === 'legacy') return { ok: Boolean(q.hidden_tests?.trim()), text: '传统判题' }
  const testsOk = Array.isArray(q.test_groups) && q.test_groups.length > 0
  const hiddenOk = q.grading_mode !== 'shadow' || Boolean(q.hidden_tests?.trim())
  const ok = testsOk && hiddenOk && q.has_locked_rubric
  return { ok, text: q.grading_mode === 'active' ? 'AI 正式评分' : 'AI 影子评分' }
}

async function save() {
  try {
    if (!form.value.prompt.trim()) throw new Error('请输入题目内容')
    if (!(Number(form.value.points) > 0)) throw new Error('分值必须大于 0')
    const p = { question_type: form.value.question_type, prompt: form.value.prompt.trim(), points: form.value.points, order_index: editingQ.value ? undefined : questions.value.length }
    if (['single_choice', 'multi_choice'].includes(form.value.question_type)) {
      const validationError = choiceEditor.value?.validate()
      if (validationError) throw new Error(validationError)
      p.options = Object.fromEntries(form.value.choice.options.map((row) => [row.key.trim(), row.text.trim()]))
      p.correct_answer = {
        correct: form.value.choice.options.filter((row) => row.correct).map((row) => row.key.trim()),
        scoring_mode: form.value.question_type === 'multi_choice' ? form.value.choice.scoring_mode : 'all_or_nothing',
      }
      p.grading_mode = 'legacy'
    } else if (form.value.question_type === 'fill_blank') {
      if (!form.value.fill_blanks.length) throw new Error('请至少插入一个空格')
      if (form.value.fill_blanks.some(blank => !blank.accepted_answers.length || blank.accepted_answers.some(answer => !answer.trim()))) throw new Error('每个空格都必须填写至少一个标准答案')
      p.options = null
      p.correct_answer = {
        blanks: form.value.fill_blanks.map(blank => ({
          id: blank.id,
          accepted_answers: blank.accepted_answers.map(answer => answer.trim()),
          case_sensitive: Boolean(blank.case_sensitive),
        })),
      }
      p.grading_mode = 'legacy'
    } else {
      Object.assign(p, {
        starter_code: form.value.starter_code || '', public_cases: form.value.public_cases,
        hidden_tests: form.value.hidden_tests || '', time_limit_ms: form.value.time_limit_ms,
        memory_limit_mb: form.value.memory_limit_mb, correct_answer: {}, grading_mode: form.value.grading_mode || 'active',
      })
    }
    if (editingQ.value) {
      await examsAPI.updateQuestion(examId, editingQ.value, p)
    } else {
      const response = await examsAPI.createQuestion(examId, p)
      editingQ.value = response.data.id
    }
    await load()
    app.showToast(form.value.question_type === 'code' ? '基础信息已保存，可继续配置测试与 AI 评分' : '保存成功', 'success')
    if (form.value.question_type !== 'code') showForm.value = false
  } catch(e) { app.showToast(e.response?.data?.detail?.message || e.message || '保存失败', 'error') }
}
function finishEditing() { showForm.value = false; load() }
async function remove(qId) { if(confirm('确认删除此题？')) { try { await examsAPI.deleteQuestion(examId, qId); load() } catch { app.showToast('删除失败', 'error') } } }

function questionPublishOk(q) {
  if (!q || !q.prompt?.trim() || !(Number(q.points) > 0)) return false
  if (q.question_type === 'code') return Boolean(configStatus(q).ok)
  if (q.question_type === 'single_choice') {
    return Object.keys(q.options || {}).length >= 2 && (q.correct_answer?.correct || []).length === 1
  }
  if (q.question_type === 'multi_choice') {
    return Object.keys(q.options || {}).length >= 2 && (q.correct_answer?.correct || []).length >= 1
  }
  if (q.question_type === 'fill_blank') return (q.correct_answer?.blanks || []).length > 0
  return false
}

const questionChecks = computed(() => questions.value.map((q, index) => ({
  label: `第 ${String(index + 1).padStart(2, '0')} 题 · ${q.question_type === 'code' ? '编程题' : q.question_type === 'single_choice' ? '单选题' : q.question_type === 'multi_choice' ? '多选题' : '填空题'}`,
  ok: questionPublishOk(q),
  hint: q.question_type === 'code' && !questionPublishOk(q) ? 'AI 配置未完成' : '',
})))

const publishChecks = computed(() => [
  { label: '已设置考试名称', ok: Boolean(settings.value.title.trim()) },
  { label: '开始时间早于最晚进入时间', ok: Boolean(settings.value.start_at && settings.value.end_at && new Date(settings.value.start_at) < new Date(settings.value.end_at)) },
  { label: '考试时长有效', ok: Number(settings.value.duration_minutes) > 0 },
  ...questionChecks.value,
  { label: '已设置有效考生范围', ok: audienceMode.value === 'all_enrolled' || (audienceMode.value === 'selected_classes' && audienceClassIds.value.length > 0) || (audienceMode.value === 'whitelist_only' && audienceWhitelistIds.value.length > 0) },
])
const readyToPublish = computed(() => publishChecks.value.every(check => check.ok))
const totalPoints = computed(() => questions.value.reduce((sum, item) => sum + Number(item.points || 0), 0))
const scoreVisibility = computed({
  get: () => settings.value.show_score_after_grading ? 'after_grading' : 'hidden',
  set: value => { settings.value.show_score_after_grading = value === 'after_grading' },
})
const reviewVisibility = computed({
  get: () => settings.value.show_answers_after_review
    ? 'questions_and_answers'
    : settings.value.show_questions_after_review ? 'questions_only' : 'hidden',
  set: value => {
    settings.value.show_questions_after_review = ['questions_only', 'questions_and_answers'].includes(value)
    settings.value.show_answers_after_review = value === 'questions_and_answers'
  },
})

async function saveSettings({ publish = false, unpublish = false } = {}) {
  settingsSaving.value = true
  try {
    const payload = {
      title: settings.value.title.trim(), duration_minutes: Number(settings.value.duration_minutes),
      start_at: settings.value.start_at ? new Date(settings.value.start_at).toISOString() : null,
      end_at: settings.value.end_at ? new Date(settings.value.end_at).toISOString() : null,
      show_score_after_grading: settings.value.show_score_after_grading,
      show_questions_after_review: settings.value.show_questions_after_review || settings.value.show_answers_after_review,
      show_answers_after_review: settings.value.show_answers_after_review,
      audience_mode: audienceMode.value,
      audience_class_ids: audienceClassIds.value,
      whitelist_student_ids: audienceWhitelistIds.value,
      excluded_student_ids: audienceExcludedIds.value,
    }
    if (publish) payload.status = 'published'
    if (unpublish) payload.status = 'draft'
    const response = await examsAPI.update(examId, payload)
    exam.value = response.data
    settings.value.show_questions_after_review = Boolean(response.data.show_questions_after_review)
    app.showToast(publish ? '考试已发布，学生将按服务器时间看到考试' : unpublish ? '已取消发布，可以继续编辑试题' : '考试设置已保存', 'success')
    await load()
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '保存考试设置失败', 'error')
  } finally { settingsSaving.value = false }
}
onMounted(load)
</script>

<template>
  <AppLayout content-mode="workspace">
    <div class="page">
      <!-- ── Page Head ─────────────────────────────────────────────────── -->
      <header class="page-head">
        <div>
          <button class="btn-ghost btn-sm back-btn" @click="router.push(returnNavigation.destination)">
            <AppIcon name="back" :size="15" />
            {{ returnNavigation.label }}
          </button>
          <div class="title-line">
            <h1 class="page-title">{{ exam?.title || '考试题目管理' }}</h1>
            <span v-if="exam" class="status-badge" :class="exam.status === 'draft' ? 'is-draft' : 'is-published'">
              {{ exam.status === 'draft' ? '草稿' : '已发布' }}
            </span>
          </div>
          <p class="page-sub">集中编辑试题，右侧完成考试时间、公开策略与发布检查</p>
        </div>
      </header>

      <!-- ── Loading ────────────────────────────────────────────────────── -->
      <div v-if="loading" class="card" style="padding:48px;text-align:center">
        <div class="skeleton" style="height:18px;width:240px;margin:0 auto 12px"></div>
        <div class="skeleton" style="height:14px;width:360px;margin:0 auto"></div>
      </div>

      <div v-else class="editor-shell">
        <section class="question-workspace">
          <section class="question-section" aria-labelledby="question-section-title">
            <div class="section-toolbar">
              <div>
                <p class="section-kicker">试题内容</p>
                <h2 id="question-section-title">题目列表</h2>
                <p>共 {{ questions.length }} 道题，满分 {{ totalPoints }} 分</p>
              </div>
              <button class="btn-accent add-question" @click="openAdd" :disabled="exam?.status !== 'draft'" :title="exam?.status !== 'draft' ? '已发布考试不可修改题目' : ''">
                <AppIcon name="plus" :size="17" />
                添加题目
              </button>
            </div>

            <div v-if="questions.length === 0" class="question-empty">
              <AppIcon name="exam" :size="30" />
              <strong>还没有试题</strong>
              <p>添加第一道题目后，可在右侧完成发布前检查。</p>
              <button class="btn-accent" :disabled="exam?.status !== 'draft'" @click="openAdd">
                <AppIcon name="plus" :size="16" />
                添加题目
              </button>
            </div>

            <div v-else class="question-list">
              <article v-for="(q,i) in questions" :key="q.id" class="question-row">
                <div class="question-row-main">
                  <div class="question-meta">
                    <strong class="question-index">{{ String(i + 1).padStart(2, '0') }}</strong>
                    <span class="badge" :class="q.question_type === 'single_choice' ? 'badge-primary' : q.question_type === 'multi_choice' ? 'badge-info' : 'badge-neutral'">
                      {{ { single_choice:'单选题', multi_choice:'多选题', fill_blank:'填空题', code:'编程题' }[q.question_type] }}
                    </span>
                    <span class="qp">{{ q.points }} 分</span>
                    <span v-if="q.question_type === 'code'" class="badge-mode" :class="{ incomplete: !configStatus(q).ok }">
                      {{ configStatus(q).text }}{{ configStatus(q).ok ? '' : ' · 配置未完成' }}
                    </span>
                  </div>
                  <p class="qd">{{ q.prompt }}</p>
                </div>
                <div class="question-actions">
                  <button v-if="q.question_type === 'code'" class="text-action btn-ai" @click="aiConfigQid = aiConfigQid === q.id ? null : q.id">
                    <AppIcon name="brain" :size="15" />
                    {{ aiConfigQid === q.id ? '收起配置' : 'AI 配置' }}
                  </button>
                  <button class="icon-action" :disabled="exam?.status !== 'draft'" aria-label="编辑题目" title="编辑题目" @click="openEdit(q)">
                    <AppIcon name="edit" :size="16" />
                  </button>
                  <button class="icon-action is-danger" :disabled="exam?.status !== 'draft'" aria-label="删除题目" title="删除题目" @click="remove(q.id)">
                    <AppIcon name="trash" :size="16" />
                  </button>
                </div>
                <AIQuestionConfig v-if="aiConfigQid === q.id && q.question_type === 'code'" class="question-ai-config" :kind="'exam'" :question-id="q.id" :expanded="true" @close="aiConfigQid = null" />
              </article>
            </div>
          </section>
        </section>

        <aside class="control-rail" aria-label="考试设置与发布检查">
          <section class="rail-section settings-panel" aria-labelledby="exam-settings-title">
            <div class="rail-heading">
              <div>
                <p class="section-kicker">考试配置</p>
                <h2 id="exam-settings-title">考试设置</h2>
              </div>
              <AppIcon name="settings" :size="19" />
            </div>

            <div class="rail-form">
              <div class="form-group">
                <label for="exam-title">考试名称</label>
                <input id="exam-title" v-model="settings.title" :disabled="exam?.status !== 'draft'">
              </div>
              <div class="form-group">
                <label for="exam-duration">考试时长（分钟）</label>
                <input id="exam-duration" v-model.number="settings.duration_minutes" type="number" min="1" :disabled="exam?.status !== 'draft'">
              </div>
              <div class="form-group">
                <label for="exam-start">开始时间</label>
                <input id="exam-start" v-model="settings.start_at" type="datetime-local" :disabled="exam?.status !== 'draft'">
              </div>
              <div class="form-group">
                <label for="exam-end">最晚进入时间</label>
                <input id="exam-end" v-model="settings.end_at" type="datetime-local" :disabled="exam?.status !== 'draft'">
              </div>
            </div>

            <div class="timing-note">
              <AppIcon name="info" :size="16" />
              <span>学生在最晚进入时间前开始，即可获得完整的 {{ settings.duration_minutes || 0 }} 分钟。</span>
            </div>

            <div class="audience-section">
              <div class="strategy-heading">
                <h3>考生范围</h3>
                <p>班级 + 白名单，白名单优先于排除名单。</p>
              </div>
              <TaskAudiencePicker
                task-kind="exam"
                :task-id="examId"
                :course-id="exam?.course_id"
                :offering-id="exam?.offering_id"
                :audience-mode="audienceMode"
                :class-ids="audienceClassIds"
                :whitelist-ids="audienceWhitelistIds"
                :excluded-ids="audienceExcludedIds"
                :disabled="exam?.status !== 'draft'"
                @update:audience-mode="audienceMode = $event"
                @update:class-ids="audienceClassIds = $event"
                @update:whitelist-ids="audienceWhitelistIds = $event"
                @update:excluded-ids="audienceExcludedIds = $event"
                @imported="onAudienceImported"
              />
            </div>

            <div class="strategy-section">
              <div class="strategy-heading">
                <h3>公开策略</h3>
                <p>默认隐藏，避免成绩和答案提前泄露。</p>
              </div>
              <div class="form-group">
                <label for="score-visibility">成绩公开方式</label>
                <DsSelect id="score-visibility" v-model="scoreVisibility">
                  <option value="hidden">暂不公开</option>
                  <option value="after_grading">评分完成后公开总成绩</option>
                </DsSelect>
              </div>
              <div class="form-group">
                <label for="review-visibility">讲评公开范围</label>
                <DsSelect id="review-visibility" v-model="reviewVisibility">
                  <option value="hidden">暂不公开题目与答案</option>
                  <option value="questions_only">仅公开题目</option>
                  <option value="questions_and_answers">公开题目与标准答案</option>
                </DsSelect>
              </div>
              <p class="strategy-note">讲评范围仅在教师手动发布讲评后生效。</p>
            </div>

            <div class="rail-actions">
              <button class="btn-accent save-settings" :disabled="settingsSaving" @click="saveSettings()">
                <AppIcon name="save" :size="16" />
                {{ settingsSaving ? '正在保存…' : '保存设置' }}
              </button>
              <button v-if="exam?.status === 'published'" class="unpublish-button" :disabled="settingsSaving" @click="saveSettings({ unpublish: true })">取消发布后编辑</button>
            </div>
          </section>

          <section class="rail-section publish-panel" aria-labelledby="publish-check-title">
            <div class="rail-heading compact">
              <div>
                <p class="section-kicker">发布前确认</p>
                <h2 id="publish-check-title">发布检查</h2>
              </div>
            </div>
            <div class="checklist">
              <div v-for="check in publishChecks" :key="check.label" class="check-row" :class="{ ok: check.ok }">
                <span class="check-icon" aria-hidden="true"><AppIcon :name="check.ok ? 'check' : 'warning'" :size="14" /></span>
                <span>{{ check.label }}<template v-if="check.hint"> · {{ check.hint }}</template></span>
                <small>{{ check.ok ? '通过' : '待完善' }}</small>
              </div>
            </div>
            <p class="server-check-note">发布时，服务端还会校验标准答案、填空占位符、隐藏测试和 AI 评分规则。</p>
            <button v-if="exam?.status === 'draft'" class="btn-accent publish-button" :disabled="!readyToPublish || settingsSaving" @click="saveSettings({ publish: true })">检查通过并发布考试</button>
            <p v-else class="published-note">考试已发布；如有学生开始作答，关键考试内容将由服务端锁定。</p>
          </section>
        </aside>
      </div>

      <!-- ── Modal Form ─────────────────────────────────────────────────── -->
      <div v-if="showForm" class="modal-overlay" @click.self="showForm = false">
        <div class="modal-card" v-dialog-focus role="dialog" aria-modal="true" aria-labelledby="exam-question-title">
          <div class="modal-header">
            <div>
              <h3 id="exam-question-title" class="modal-title">{{ editingQ ? '编辑题目' : '新建题目' }}</h3>
              <p>按题型填写内容，所有高级配置都可在保存前检查。</p>
            </div>
            <button class="modal-close" aria-label="关闭" @click="showForm = false">×</button>
          </div>

          <div class="modal-body">
          <section class="editor-section">
            <div class="section-heading"><span>1</span><div><h4>基础信息</h4><p>设置题型、题目内容和本题分值</p></div></div>

          <div class="form-group">
            <label>题目类型</label>
            <DsSelect v-model="form.question_type" class="modal-field" @change="form.grading_mode = form.question_type === 'code' ? 'active' : 'legacy'">
              <option value="single_choice">单选题</option>
              <option value="multi_choice">多选题</option>
              <option value="fill_blank">填空题</option>
              <option value="code">编程题</option>
            </DsSelect>
          </div>
          <div v-if="form.question_type !== 'fill_blank'" class="form-group">
            <label>题目内容</label>
            <textarea v-model="form.prompt" rows="3" class="modal-field" placeholder="输入题目内容"></textarea>
          </div>
          <div class="form-group">
            <label>分值</label>
            <input v-model.number="form.points" type="number" class="modal-field" min="0.1" step="0.1" />
          </div>
          </section>

          <!-- 选择题字段 -->
          <template v-if="['single_choice', 'multi_choice'].includes(form.question_type)">
            <ChoiceOptionsEditor ref="choiceEditor" v-model="form.choice" :question-type="form.question_type" />
          </template>

          <template v-else-if="form.question_type === 'fill_blank'">
            <FillBlankEditor v-model="form.fill_blanks" :prompt="form.prompt" @update:prompt="form.prompt = $event" />
          </template>

          <!-- 编程题字段 -->
          <template v-else>
            <section class="editor-section">
              <div class="section-heading"><span>2</span><div><h4>代码模板</h4><p>为学生提供起始代码并设置运行限制</p></div></div>
            <div class="form-group">
              <label>初始代码</label>
              <textarea v-model="form.starter_code" rows="7" class="modal-field code-input" placeholder="def solution():&#10;    pass"></textarea>
            </div>
            <div class="limit-grid">
              <div class="form-group"><label>时间限制（毫秒）</label><input v-model.number="form.time_limit_ms" type="number" class="modal-field" min="1" /></div>
              <div class="form-group"><label>内存限制（MB）</label><input v-model.number="form.memory_limit_mb" type="number" class="modal-field" min="1" /></div>
            </div>
            </section>

            <section class="editor-section">
              <div class="section-heading"><span>3</span><div><h4>样例与测试</h4><p>公开样例展示给学生，私有测试用于传统或影子判题</p></div></div>
              <div v-if="form.grading_mode === 'active'" class="active-optional-note">
                <AppIcon name="info" :size="14" />
                <span>当前为 <strong>AI 正式评分（active）</strong>：私有测试不参与判题，本区块均可选填——公开样例仅用于向学生展示输入输出示例，实际判题使用下方「AI 评分」中的功能/鲁棒性测试组。</span>
              </div>
              <QeTestCases
                :key="testCasesKey"
                :public-cases="form.public_cases"
                :hidden-tests="form.hidden_tests"
                :function-name="functionName"
                :question-id="editingQ"
                :show-run="false"
                @update:public-cases="form.public_cases = $event"
                @update:hidden-tests="form.hidden_tests = $event"
                @parse-failed="app.showToast('复杂 pytest 无法转为可视化用例，已保留高级模式内容', 'error')"
              />
            </section>

            <section class="editor-section ai-section">
              <div class="section-heading"><span>4</span><div><h4>AI 评分</h4><p>默认按百分制 AI 得分折算到本题分值，例如 10 分题获得 80 分即计 8.0 分</p></div></div>
              <div v-if="!editingQ" class="save-first-note">先保存基础信息，即可生成测试组、配置评分规则并锁定 Rubric。</div>
              <template v-else>
                <button v-if="!modalAiExpanded" class="btn-ghost" @click="modalAiExpanded = true">展开 AI 配置</button>
                <AIQuestionConfig v-if="modalAiExpanded" kind="exam" :question-id="editingQ" :expanded="true" @close="modalAiExpanded = false" />
              </template>
            </section>
          </template>
          </div>

          <div class="modal-actions">
            <button class="btn-ghost" @click="showForm = false">取消</button>
            <button v-if="form.question_type === 'code' && editingQ" class="btn-ghost" @click="finishEditing">完成</button>
            <button class="btn-accent" @click="save">
              {{ form.question_type === 'code' ? (editingQ ? '保存基础信息' : '保存并继续配置') : '保存题目' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </AppLayout>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════════
   Exam Question Edit — Code Studio
   page-head + question cards + modal form
   ═══════════════════════════════════════════════════════════════════════ */
.page {
  display: flex;
  flex-direction: column;
  gap: 20px;
  /* Keep this editor aligned with the platform's blue primary action color. */
  --exam-action: var(--accent);
  --exam-action-hover: var(--accent-hover);
}

/* The shared .btn-accent token is orange for legacy pages.  The exam editor
   uses blue primary actions so its controls match the rest of the teacher UI. */
.page :deep(.btn-accent) {
  background: var(--exam-action);
  border-color: var(--exam-action);
  color: var(--surface);
}
.page :deep(.btn-accent:hover:not(:disabled)) {
  background: var(--exam-action-hover);
  border-color: var(--exam-action-hover);
  box-shadow: var(--shadow-md);
}
.page :deep(.btn-accent:focus-visible) {
  outline: none;
  box-shadow: 0 0 0 3px oklch(0.52 0.095 158 / 0.2);
}

/* ── Page Head ─────────────────────────────────────────────────────── */
.page-head {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 16px;
}
.back-btn { display:inline-flex; align-items:center; gap:6px; margin-bottom: 9px; color: var(--muted); }
.title-line { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
.page-title {
  font-size: 27px; font-weight: 700;
  color: var(--fg); letter-spacing: -0.02em; line-height: 1.15;
  margin: 0;
}
.page-sub {
  font-size: var(--text-sm); color: var(--muted); margin: 7px 0 0;
}
.status-badge { display:inline-flex; align-items:center; min-height:24px; padding:2px 9px; border-radius: var(--radius-full); font-size:11px; font-weight:650; }
.status-badge.is-draft { background:var(--surface-subtle); color:var(--muted); }
.status-badge.is-published { background:var(--success-bg); color:var(--success); }

/* ── Two-column editor workspace ───────────────────────────────────── */
.editor-shell { display:grid; grid-template-columns:minmax(0, 1fr) minmax(330px, 370px); gap:20px; align-items:start; }
.question-workspace { min-width:0; }
.question-section,.control-rail { border:1px solid var(--border); border-radius: var(--radius-lg); background:var(--surface); box-shadow:0 8px 24px oklch(0.2 0.01 150 / 0.04); }
.question-section { overflow:hidden; }
.section-toolbar { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; padding:22px 24px; border-bottom:1px solid var(--border); }
.section-kicker { margin:0 0 5px; color:var(--accent); font-size:10px; font-weight:750; letter-spacing:.11em; text-transform:uppercase; }
.section-toolbar h2,.rail-heading h2 { margin:0; color:var(--fg); font-size:17px; line-height:1.3; }
.section-toolbar > div > p:last-child { margin:5px 0 0; color:var(--muted); font-size:12px; }
.add-question { display:inline-flex; align-items:center; gap:6px; flex:none; }
.question-empty { display:flex; min-height:300px; flex-direction:column; align-items:center; justify-content:center; padding:40px 24px; text-align:center; color:var(--faint); }
.question-empty strong { margin-top:12px; color:var(--fg); font-size:15px; }
.question-empty p { margin:6px 0 18px; color:var(--muted); font-size:12px; }
.question-empty .btn-accent { display:inline-flex; align-items:center; gap:6px; }
.question-list { display:flex; flex-direction:column; }
.question-row { display:grid; grid-template-columns:minmax(0,1fr) auto; column-gap:18px; padding:20px 24px; border-bottom:1px solid var(--border); }
.question-row:last-child { border-bottom:0; }
.question-row:hover { background:var(--surface); }
.question-meta { display:flex; align-items:center; flex-wrap:wrap; gap:9px; }
.question-index { color:var(--faint); font-size:12px; font-variant-numeric:tabular-nums; letter-spacing:.06em; }
.qp { font-size: var(--text-xs); color: var(--muted); }
.qd { color:var(--muted); font-size:var(--text-sm); margin:10px 0 0; line-height:1.65; overflow-wrap:anywhere; }
.question-actions { display:flex; align-items:flex-start; gap:4px; }
.text-action,.icon-action { border:0; background:transparent; cursor:pointer; transition:background .16s ease,color .16s ease; }
.text-action { display:inline-flex; align-items:center; gap:5px; min-height:32px; padding:0 8px; border-radius: var(--radius-md); color:var(--accent); font-size:12px; }
.text-action:hover { background:var(--accent-soft); }
.icon-action { display:grid; place-items:center; width:32px; height:32px; border-radius: var(--radius-md); color:var(--muted); }
.icon-action:hover { background:var(--surface-subtle); color:oklch(0.32 0.02 155); }
.icon-action.is-danger { color:var(--danger); }
.icon-action.is-danger:hover { background:var(--danger-bg); }
.icon-action:disabled,.text-action:disabled { opacity:.38; cursor:not-allowed; }
.question-ai-config { grid-column:1 / -1; margin-top:18px; }
.badge-mode {
  display: inline-block; padding: 1px 8px; border-radius: var(--radius-md);
  font-size: 11px; font-weight: 500;
  background: var(--info-bg); color: var(--accent-hover);
}
.badge-mode.incomplete { background:var(--warning-bg); color:var(--danger); }

/* ── Settings rail ─────────────────────────────────────────────────── */
.control-rail { position:sticky; top:calc(var(--header-height) + 16px); max-height:calc(100dvh - 230px); overflow:auto; overscroll-behavior:contain; }
.rail-section { padding:20px; }
.rail-section + .rail-section { border-top:1px solid var(--border); }
.rail-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:18px; }
.rail-heading > .app-icon { color:var(--muted); }
.rail-heading.compact { margin-bottom:14px; }
.rail-form { display:grid; gap:13px; }
.rail-form .form-group,.strategy-section .form-group { margin:0; }
.rail-form label,.strategy-section label { display:block; margin-bottom:6px; color:var(--muted); font-size:11px; font-weight:600; }
.rail-form input,.strategy-section .ds-select { width:100%; min-height:38px; box-sizing:border-box; font-size:12px; }
.rail-form input:disabled { background:var(--surface-subtle); color:var(--muted); }
.timing-note { display:flex; align-items:flex-start; gap:8px; margin:15px 0 0; padding:10px 11px; border-radius: var(--radius-md); background:var(--warning-bg); color:var(--danger); font-size:10px; line-height:1.55; }
.timing-note .app-icon { flex:none; margin-top:1px; }
.strategy-section,.audience-section { margin-top:18px; padding-top:18px; border-top:1px solid var(--border); }
.strategy-heading { margin-bottom:13px; }
.strategy-heading h3 { margin:0; color:var(--fg); font-size:13px; }
.strategy-heading p { margin:4px 0 0; color:var(--muted); font-size:10px; line-height:1.5; }
.strategy-section .form-group + .form-group { margin-top:12px; }
.strategy-note { margin:9px 0 0; color:var(--muted); font-size:10px; line-height:1.5; }
.rail-actions { display:grid; gap:8px; margin-top:18px; }
.save-settings { display:flex; width:100%; align-items:center; justify-content:center; gap:6px; min-height:40px; }
.unpublish-button { min-height:34px; border:0; background:transparent; color:var(--muted); font-size:12px; cursor:pointer; }
.unpublish-button:hover { color:var(--accent-hover); }
.unpublish-button:disabled { opacity:.45; cursor:not-allowed; }
.checklist { display:grid; gap:8px; }
.check-row { display:grid; grid-template-columns:24px minmax(0,1fr) auto; align-items:center; gap:8px; min-height:38px; padding:7px 9px; border:1px solid var(--danger-bg); border-radius: var(--radius-md); background:var(--danger-bg); color:var(--danger); font-size:11px; }
.check-row.ok { border-color:var(--success-bg); background:var(--success-bg); color:var(--success); }
.check-icon { display:grid; place-items:center; width:24px; height:24px; border-radius: var(--radius-md); background:var(--danger-bg); color:var(--danger); }
.check-row.ok .check-icon { background:var(--success-bg); color:var(--success); }
.check-row small { color:inherit; font-size:9px; font-weight:650; }
.server-check-note { margin:12px 0 0; color:var(--muted); font-size:10px; line-height:1.55; }
.publish-button { width:100%; min-height:40px; margin-top:15px; }
.published-note { margin:13px 0 0; padding:10px 11px; border-radius: var(--radius-md); background:var(--success-bg); color:var(--success); font-size:10px; line-height:1.55; }

/* ── Modal ─────────────────────────────────────────────────────────── */
.modal-overlay {
  position: fixed; inset: 0; z-index: 100;
  background: oklch(0.2 0.01 150 / 0.4);
  backdrop-filter: blur(2px);
  display: flex; align-items: flex-start; justify-content: center;
  padding: 4vh 16px;
}
.modal-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: min(1040px, 96vw); max-height: 92vh; overflow: hidden;
  box-sizing: border-box;
  box-shadow: var(--shadow-xl);
  display:flex; flex-direction:column;
}
.modal-header {
  display:flex; align-items:flex-start; justify-content:space-between; gap:16px;
  padding:24px 28px 18px; border-bottom:1px solid var(--border); background:var(--surface);
}
.modal-header p { margin:5px 0 0; color:var(--muted); font-size:13px; }
.modal-close { border:0; background:none; color:var(--muted); font-size:26px; line-height:1; cursor:pointer; }
.modal-body { padding:24px 28px; overflow-y:auto; }
.modal-field { width:100%; box-sizing:border-box; }
.editor-section { padding:0 0 22px; margin-bottom:22px; border-bottom:1px solid var(--border); }
.editor-section:last-child { border-bottom:0; margin-bottom:0; }
.section-heading { display:flex; align-items:center; gap:10px; margin-bottom:16px; }
.section-heading > span { display:grid; place-items:center; width:28px; height:28px; border-radius:50%; background:var(--accent-soft); color:var(--accent); font-weight:700; font-size:13px; }
.section-heading h4 { margin:0; font-size:16px; color:var(--fg); }
.section-heading p { margin:3px 0 0; color:var(--muted); font-size:12px; }
.limit-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.save-first-note { padding:14px 16px; border:1px solid var(--warning-bg); border-radius: var(--radius-md); background:var(--warning-bg); color:var(--danger); font-size:13px; }
.active-optional-note { display:flex; align-items:flex-start; gap:7px; margin-bottom:14px; padding:9px 12px; border-radius: var(--radius-md); background:var(--info-bg); color:var(--accent-hover); font-size:12px; line-height:1.6; }
.active-optional-note .app-icon { flex:none; margin-top:2px; }
.active-optional-note strong { font-weight:650; }
.ai-section :deep(.ai-config) { margin-top:0; }
.ai-section :deep(.ai-config-header h4) { display:none; }
.ai-section :deep(.ai-config-header) { justify-content:flex-end; }
.modal-title {
  font-size: 20px; font-weight: 650; color: var(--fg);
  margin: 0; letter-spacing: -0.01em;
}
.modal-actions {
  display: flex; gap: 8px; justify-content: flex-end;
  padding:16px 28px; border-top:1px solid var(--border); background:var(--surface);
  box-shadow:0 -8px 22px oklch(0.2 0.01 150 / 0.04);
}

/* ── Code input (monospace textarea) ────────────────────────────────── */
.code-input {
  font-family: var(--font-mono); font-size: var(--text-xs);
  background: var(--surface-sunken); line-height: 1.6;
}

@media (max-width: 1080px) {
  .editor-shell { grid-template-columns:1fr; }
  .control-rail { position:static; max-height:none; overflow:visible; }
  .rail-form { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .strategy-section { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); column-gap:14px; }
  .strategy-heading,.strategy-note { grid-column:1 / -1; }
  .strategy-section .form-group + .form-group { margin-top:0; }
}

@media (max-width: 768px) {
  .page-head { flex-direction: column; }
  .page-title { font-size: 24px; }
  .section-toolbar { padding:18px; }
  .question-row { grid-template-columns:1fr; padding:18px; }
  .question-actions { margin-top:12px; }
  .rail-form,.strategy-section { grid-template-columns:1fr; }
  .strategy-heading,.strategy-note { grid-column:auto; }
  .strategy-section .form-group + .form-group { margin-top:12px; }
  .modal-overlay { padding:0; align-items:stretch; }
  .modal-card { width:100vw; max-height:100vh; border-radius:0; }
  .modal-header, .modal-body, .modal-actions { padding-left:16px; padding-right:16px; }
  .limit-grid { grid-template-columns:1fr; gap:0; }
}

@media (max-width: 480px) {
  .section-toolbar { align-items:stretch; flex-direction:column; }
  .add-question { justify-content:center; width:100%; }
  .question-meta { gap:7px; }
}
</style>
