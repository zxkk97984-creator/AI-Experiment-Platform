import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const detailMock = vi.hoisted(() => vi.fn())
const updateScoreMock = vi.hoisted(() => vi.fn())
const updateQuestionScoreMock = vi.hoisted(() => vi.fn())

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '5', submissionId: '7' } }),
  useRouter: () => ({ push: vi.fn() }),
  createRouter: () => ({ beforeEach: vi.fn(), afterEach: vi.fn(), currentRoute: { value: {} }, push: vi.fn() }),
  createWebHistory: () => ({}),
}))
vi.mock('../../../api/exams.js', () => ({
  examsAPI: { getGradeDetail: detailMock, updateGradeAnswerScore: updateScoreMock, updateGradeQuestionScore: updateQuestionScoreMock },
}))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast: vi.fn() }) }))

import GradeDetailView from '../GradeDetailView.vue'

function detailPayload() {
  return {
    exam: { title: 'Python期中测验', course_title: 'Python程序设计' },
    student: { name: '爱丽丝', number: '2023001' },
    submission: { score: 92, status: 'graded', submitted_at: '2026-08-09T10:00:00Z', elapsed_minutes: 45 },
    analysis: { objective_score: 32, objective_total: 40, code_score: 60, code_total: 60, question_count: 1, correct_count: 0 },
    questions: [{
      id: 1, exam_id: 5, question_type: 'single_choice', prompt: 'Python关键字是什么？',
      points: 10, order_index: 0, options: { A: 'def', B: 'class' }, correct_answer: { correct: ['B'] },
    }],
    answers: [{ id: 1, question_id: 1, order_index: 0, question_type: 'single_choice', prompt: 'Python关键字是什么？', points: 10, score: 8, result_revision: 3, selected_options: ['A'] }],
  }
}

async function openDialogFor(wrapper, value = '7.5') {
  const input = wrapper.find('input[type="text"]')
  await input.setValue(value)
  await input.trigger('change')
  await flushPromises()
}

describe('学生成绩详情', () => {
  beforeEach(() => {
    detailMock.mockResolvedValue({ data: detailPayload() })
    updateScoreMock.mockReset()
    updateQuestionScoreMock.mockReset()
  })

  it('按试卷讲评版式展示题目、标准答案与学生作答', async () => {
    const wrapper = mount(GradeDetailView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()
    expect(wrapper.text()).toContain('学生成绩详情')
    expect(wrapper.text()).toContain('爱丽丝')
    expect(wrapper.text()).toContain('客观题得分')
    expect(wrapper.text()).toContain('题目得分明细')
    expect(wrapper.text()).toContain('试卷讲评')
    expect(wrapper.text()).toContain('Python关键字是什么？')
    expect(wrapper.text()).toContain('标准答案')
    expect(wrapper.text()).toContain('学生作答')
    expect(wrapper.text()).toContain('A')
  })

  it('修改分数时必须填写理由，确认后携带理由调用接口', async () => {
    updateScoreMock.mockResolvedValue({
      data: { ...detailPayload(), submission: { ...detailPayload().submission, score: 89.5 }, answers: [{ ...detailPayload().answers[0], score: 7.5, manual_score_reason: '学生部分正确' }] },
    })
    const wrapper = mount(GradeDetailView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()

    await openDialogFor(wrapper)
    expect(wrapper.text()).toContain('确认修改分数')
    expect(wrapper.text()).toContain('改分理由')

    await wrapper.find('.score-dialog__actions .btn-primary').trigger('click')
    expect(updateScoreMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请填写改分理由')

    await wrapper.find('textarea').setValue('学生部分正确')
    await wrapper.find('.score-dialog__actions .btn-primary').trigger('click')
    await flushPromises()

    expect(updateScoreMock).toHaveBeenCalledWith('5', '7', 1, 7.5, '学生部分正确', 3)
    expect(wrapper.find('input[type="text"]').element.value).toBe('7.5')
    expect(wrapper.text()).toContain('得分：7.5 / 10')
  })

  it('取消修改会恢复输入框原分数，不调用接口', async () => {
    const wrapper = mount(GradeDetailView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()

    await openDialogFor(wrapper, '9')
    await wrapper.find('.score-dialog__actions .btn-ghost').trigger('click')
    await flushPromises()

    expect(updateScoreMock).not.toHaveBeenCalled()
    expect(wrapper.find('input[type="text"]').element.value).toBe('8')
  })

  it('没有答题记录时可通过 questionId 直接给分并携带理由', async () => {
    const base = detailPayload()
    detailMock.mockResolvedValue({ data: { ...base, answers: [] } })
    updateQuestionScoreMock.mockResolvedValue({
      data: {
        ...base,
        submission: { ...base.submission, score: 5 },
        answers: [{ id: 22, question_id: 1, order_index: 0, question_type: 'single_choice', prompt: 'Python关键字是什么？', points: 10, score: 5, selected_options: null, manual_score_reason: '整题未作答' }],
      },
    })
    const wrapper = mount(GradeDetailView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()

    await openDialogFor(wrapper, '5')
    await wrapper.find('textarea').setValue('整题未作答')
    await wrapper.find('.score-dialog__actions .btn-primary').trigger('click')
    await flushPromises()
    expect(updateQuestionScoreMock).toHaveBeenCalledWith('5', '7', 1, 5, '整题未作答', 0)
  })

  it('无失效的「导出报告」按钮，打印按钮调用 window.print', async () => {
    const printSpy = vi.spyOn(window, 'print').mockImplementation(() => {})
    const wrapper = mount(GradeDetailView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()

    expect(wrapper.findAll('button').some((button) => button.text().includes('导出报告'))).toBe(false)

    const printButton = wrapper.findAll('button').find((button) => button.text().includes('打印 / 保存 PDF'))
    expect(printButton).toBeDefined()
    await printButton.trigger('click')
    expect(printSpy).toHaveBeenCalledTimes(1)
    printSpy.mockRestore()
  })
})
