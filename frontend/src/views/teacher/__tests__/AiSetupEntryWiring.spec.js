// 批次 2 接线验收：作业题/考试题编辑页的【AI 判题】一键入口真的挂在页面上
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

const routeState = vi.hoisted(() => ({ params: { id: '3' }, query: {}, meta: { role: 'teacher' } }))
const routerState = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), resolve: vi.fn(() => ({ href: '/' })) }))
const appState = vi.hoisted(() => ({ showToast: vi.fn() }))
const aiApi = vi.hoisted(() => ({
  createSetupJob: vi.fn(),
  getSetupJob: vi.fn(),
  retrySetupJob: vi.fn(),
  createTrialRun: vi.fn(),
  getTrialRun: vi.fn(),
  enableSetupJob: vi.fn(),
  getStatus: vi.fn(),
  getConfig: vi.fn(),
  updateConfig: vi.fn(),
  listRubrics: vi.fn(),
  generateRubric: vi.fn(),
  generateTestGroups: vi.fn(),
  updateRubric: vi.fn(),
  lockRubric: vi.fn(),
}))
const assignmentsApi = vi.hoisted(() => ({
  get: vi.fn(),
  getQuestions: vi.fn(),
  updateQuestion: vi.fn(),
  createQuestion: vi.fn(),
  deleteQuestion: vi.fn(),
}))
const environmentsApi = vi.hoisted(() => ({
  listAvailable: vi.fn(),
  getEditorOptions: vi.fn(),
  listProfiles: vi.fn(),
}))
const judgeApi = vi.hoisted(() => ({ sampleRun: vi.fn() }))
const examsApi = vi.hoisted(() => ({ get: vi.fn(), getQuestions: vi.fn() }))

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => routerState,
  RouterLink: { template: '<a><slot /></a>' },
}))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => appState }))
vi.mock('../../../api/aiGrading.js', () => ({ aiGradingAPI: aiApi }))
vi.mock('../../../api/assignments.js', () => ({ assignmentsAPI: assignmentsApi }))
vi.mock('../../../api/environments.js', () => ({ environmentsAPI: environmentsApi }))
vi.mock('../../../api/judge.js', () => ({ judgeAPI: judgeApi }))
vi.mock('../../../api/exams.js', () => ({ examsAPI: examsApi }))

import QuestionEditView from '../QuestionEditView.vue'
import ExamQuestionEditView from '../ExamQuestionEditView.vue'

const READY_JOB = {
  id: 11, question_kind: 'assignment', question_id: 7, stage: 'draft_ready', progress: 100,
  draft_rubric_id: 3, draft_rubric_status: 'draft',
  draft_rubric: { rubric_version: 1, algorithm_criteria: [{ id: 'A1', name: '正确性', points: 10 }], quality_criteria: [] },
  draft_test_groups: [{ id: 'F1', dimension: 'F', name: '功能', max_score: 60 }],
  generate_test_groups: true, error_code: null, error_message: null,
}

const CODE_QUESTION = {
  id: 11, title: '正数求和', prompt: '返回正数之和', function_name: 'sum_positive',
  question_type: 'code', points: 10, grading_mode: 'active', time_limit_ms: 5000,
  memory_limit_mb: 256, reference_solution: 'def solve(n):\n    return n',
  teacher_constraints: { requirements_text: '强调边界条件' }, test_groups: [],
}

const STUBS = {
  AppLayout: { template: '<div class="layout-stub"><slot /></div>' },
  AIQuestionConfig: true,
  AiConfigForm: true,
  ConfirmDialog: true,
  GradebookBindingField: true,
  EnvironmentProfilePicker: true,
  TaskAudiencePicker: true,
  QeMarkdownEditor: true,
  QeCodeEditor: true,
  QeTestCases: true,
  GradingVersionPanel: true,
  ChoiceOptionsEditor: true,
  FillBlankEditor: true,
}

function mountView(component) {
  return mount(component, {
    global: { plugins: [createPinia()], stubs: STUBS },
  })
}

async function submitSetupDialog(wrapper) {
  await wrapper.find('.ai-setup-trigger').trigger('click')
  await flushPromises()
  await wrapper.find('.dialog-actions .btn-primary').trigger('click')
  await flushPromises()
}

describe('作业题/考试题的【AI 判题】一键入口', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    aiApi.getStatus.mockResolvedValue({ data: { enabled: true, ready: true } })
    environmentsApi.listAvailable.mockResolvedValue({ data: [] })
    environmentsApi.getEditorOptions.mockResolvedValue({ data: { python_versions: ['3.12'] } })
    environmentsApi.listProfiles.mockResolvedValue({ data: [] })
    judgeApi.sampleRun.mockResolvedValue({ data: {} })
    aiApi.createSetupJob.mockResolvedValue({ data: READY_JOB })
  })

  it('作业题页：点【AI 判题】→ 开始生成 → setup-jobs 携带参考解法与评分侧重', async () => {
    assignmentsApi.get.mockResolvedValue({
      data: { id: 3, title: '作业', status: 'draft', environment_version_id: null,
              due_at: null, import_policy_mode: 'unrestricted', allowed_imports: [],
              audience_mode: 'all_enrolled', audience_class_ids: [], whitelist_student_ids: [],
              excluded_student_ids: [] },
    })
    assignmentsApi.getQuestions.mockResolvedValue({ data: { items: [CODE_QUESTION] } })
    const wrapper = mountView(QuestionEditView)
    await flushPromises()

    // 选中题目进入编辑态后，AI 评分配置区顶部出现一键入口
    const editButton = wrapper.findAll('button').find((node) => node.text() === '编辑')
    expect(editButton).toBeTruthy()
    await editButton.trigger('click')
    await flushPromises()
    expect(wrapper.find('.ai-setup-trigger').exists()).toBe(true)
    // 高级入口保持不变
    expect(wrapper.findComponent({ name: 'AIQuestionConfig' }).exists()).toBe(true)

    await submitSetupDialog(wrapper)
    expect(aiApi.createSetupJob).toHaveBeenCalledTimes(1)
    expect(aiApi.createSetupJob.mock.calls[0][0]).toMatchObject({
      question_kind: 'assignment',
      question_id: 11,
      reference_solution: 'def solve(n):\n    return n',
      teacher_constraints: { requirements_text: '强调边界条件' },
      grading_mode: 'active',
    })
  })

  it('考试题页：编程题行内出现【AI 判题】并按 exam 类型提交', async () => {
    examsApi.get.mockResolvedValue({
      data: { id: 4, title: '考试', status: 'draft', duration_minutes: 60, exam_kind: 'quiz',
              timing_mode: 'unified', audience_mode: 'all_enrolled', audience_class_ids: [] },
    })
    examsApi.getQuestions.mockResolvedValue({ data: { items: [CODE_QUESTION] } })
    const wrapper = mountView(ExamQuestionEditView)
    await flushPromises()

    expect(wrapper.find('.ai-setup-trigger').exists()).toBe(true)
    // 高级入口保持不变：行内「AI 配置」按钮仍在（点击才展开 AIQuestionConfig）
    expect(wrapper.findAll('button').some((node) => node.text().includes('AI 配置'))).toBe(true)

    await submitSetupDialog(wrapper)
    expect(aiApi.createSetupJob.mock.calls[0][0]).toMatchObject({
      question_kind: 'exam',
      question_id: 11,
      reference_solution: 'def solve(n):\n    return n',
      teacher_constraints: { requirements_text: '强调边界条件' },
    })
  })
})
