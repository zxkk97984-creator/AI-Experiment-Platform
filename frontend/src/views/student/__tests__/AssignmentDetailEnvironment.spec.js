import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const push = vi.fn()
const showToast = vi.fn()

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '4' }, path: '/student/assignments/4' }),
  useRouter: () => ({ push }),
  onBeforeRouteLeave: vi.fn(),
  createRouter: vi.fn(() => ({
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    beforeResolve: vi.fn(),
    push: vi.fn(),
    replace: vi.fn(),
    currentRoute: { value: { path: '/student/assignments/4' } },
  })),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../../../api/assignments.js', () => ({
  assignmentsAPI: {
    get: vi.fn(),
    getQuestions: vi.fn(),
    myProgress: vi.fn(),
  },
}))

vi.mock('../../../api/judge.js', () => ({
  judgeAPI: {
    list: vi.fn(),
    submit: vi.fn(),
    getResult: vi.fn(),
    sampleRun: vi.fn(),
  },
}))

vi.mock('../../../stores/app.js', () => ({
  useAppStore: () => ({ showToast }),
}))

import { assignmentsAPI } from '../../../api/assignments.js'
import { judgeAPI } from '../../../api/judge.js'
import AssignmentDetailView from '../AssignmentDetailView.vue'

const basicSummary = {
  display_name: 'Python 基础',
  version_label: 'v1',
  python_version: '3.12',
  imports: ['pytest'],
  import_policy_mode: 'unrestricted',
  allowed_imports: [],
}

const dataSummary = {
  display_name: '数据分析',
  version_label: 'v2',
  python_version: '3.12',
  imports: ['numpy', 'pandas', 'scipy', 'sklearn', 'matplotlib'],
  import_policy_mode: 'restricted',
  allowed_imports: ['numpy', 'pandas'],
}

let wrapper

function question(overrides = {}) {
  return {
    id: 10,
    title: '数据分析题',
    description: '计算分类指标',
    function_name: 'confusion_metrics',
    starter_code: 'def confusion_metrics():\n    pass',
    public_cases: [],
    grading_mode: 'legacy',
    ...overrides,
  }
}

async function mountPage({ assignmentSummary = basicSummary, assignmentOverrides = {}, questions = [question()], submissions = [] } = {}) {
  assignmentsAPI.get.mockResolvedValue({
    data: {
      id: 4,
      course_id: 1,
      title: '数据处理综合练习',
      environment_summary: assignmentSummary,
      ...assignmentOverrides,
    },
  })
  assignmentsAPI.getQuestions.mockResolvedValue({
    data: { items: questions },
  })
  assignmentsAPI.myProgress.mockResolvedValue({ data: { items: submissions.map(s => ({ ...s, latest_submission_id: s.id, completed: ['accepted', 'graded'].includes(s.status) })) } })
  wrapper = mount(AssignmentDetailView, {
    global: {
      plugins: [createPinia()],
      stubs: {
        AppLayout: { template: '<main><slot /></main>' },
      },
    },
  })
  await flushPromises()
  return wrapper
}

afterEach(() => {
  wrapper?.unmount()
  vi.clearAllMocks()
})

describe('AssignmentDetailView Phase 5: 学生端环境提示与诊断', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('作业默认环境：显示「本作业环境」与可用库，学生无需操作', async () => {
    await mountPage()
    const envBanner = wrapper.find('.env-banner')
    expect(envBanner.exists()).toBe(true)
    expect(envBanner.text()).toContain('本作业环境：Python 基础 v1')
    expect(envBanner.text()).toContain('可用库：pytest')
  })

  it('题目覆盖环境：显示「本题环境」而非作业默认', async () => {
    const overrideQuestion = question({ environment_summary: dataSummary })
    await mountPage({ questions: [overrideQuestion] })
    const envBanner = wrapper.find('.env-banner')
    expect(envBanner.text()).toContain('本题环境：数据分析 v2')
    expect(envBanner.text()).toContain('可用库：numpy · pandas · scipy · sklearn · matplotlib')
    expect(envBanner.text()).toContain('本题允许导入：numpy · pandas')
  })

  it('restricted 作业显示允许导入清单', async () => {
    const restrictedQuestion = question({ environment_summary: dataSummary })
    await mountPage({ questions: [restrictedQuestion] })
    expect(wrapper.find('.env-banner').text()).toContain('本题允许导入：numpy · pandas')
  })

  it('无环境摘要时不显示环境提示', async () => {
    await mountPage({ assignmentSummary: null, questions: [question({ environment_summary: null })] })
    expect(wrapper.find('.env-banner').exists()).toBe(false)
  })

  it('截止后仍显示题目但禁用自测与提交，并给出明确提示', async () => {
    await mountPage({ assignmentOverrides: { due_at: new Date(Date.now() - 60_000).toISOString() } })

    expect(wrapper.find('.deadline-banner').text()).toContain('作业已截止')
    expect(wrapper.find('.btn-self-test').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.btn-submit-code').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('数据分析题')
  })

  it('页面重新获得焦点后刷新时间，教师延长截止即可恢复操作', async () => {
    const pastDue = new Date(Date.now() - 60_000).toISOString()
    const futureDue = new Date(Date.now() + 3_600_000).toISOString()
    await mountPage({ assignmentOverrides: { due_at: pastDue } })
    expect(wrapper.find('.btn-submit-code').attributes('disabled')).toBeDefined()

    assignmentsAPI.get.mockResolvedValueOnce({
      data: {
        id: 4,
        course_id: 1,
        title: '数据处理综合练习',
        environment_summary: basicSummary,
        due_at: futureDue,
      },
    })
    window.dispatchEvent(new Event('focus'))
    await flushPromises()

    expect(assignmentsAPI.get).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.deadline-banner').text()).toContain('作业进行中')
    expect(wrapper.find('.btn-self-test').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('.btn-submit-code').attributes('disabled')).toBeUndefined()
  })

  it('判题结果优先显示结构化中文诊断，不含裸 traceback', async () => {
    judgeAPI.submit.mockResolvedValue({ data: { id: 201 } })
    await mountPage()
    wrapper.find('.btn-submit-code').trigger('click')
    await flushPromises()

    judgeAPI.getResult.mockResolvedValue({
      data: {
        id: 201,
        question_id: 10,
        status: 'runtime_error',
        score: 0,
        diagnostic: {
          code: 'IMPORT_NOT_ALLOWED',
          module: 'numpy',
          message: 'numpy 未在本作业允许范围内',
        },
      },
    })
    // 轮询 1 秒间隔：先直接触发一次轮询以完成首轮
    await new Promise(r => setTimeout(r, 1100))
    await flushPromises()

    const card = wrapper.find('.submit-result-card')
    expect(card.exists()).toBe(true)
    expect(wrapper.text()).toContain('numpy 未在本作业允许范围内')
    expect(wrapper.text()).not.toContain('Traceback')
    expect(wrapper.text()).not.toContain('ModuleNotFoundError')
  })
})
