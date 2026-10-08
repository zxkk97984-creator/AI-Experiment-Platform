import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const navigation = vi.hoisted(() => ({ route: { params: { id: '8' }, query: {}, meta: { role: 'teacher' } }, push: vi.fn() }))

vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal()),
  useRoute: () => navigation.route,
  useRouter: () => ({ push: navigation.push }),
}))

vi.mock('../../../api/exams.js', () => ({
  examsAPI: {
    get: vi.fn(), getQuestions: vi.fn(), createQuestion: vi.fn(), updateQuestion: vi.fn(), deleteQuestion: vi.fn(), update: vi.fn(),
  },
}))

import { examsAPI } from '../../../api/exams.js'
import ExamQuestionEditView from '../ExamQuestionEditView.vue'

describe('ExamQuestionEditView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    navigation.route = { params: { id: '8' }, query: {}, meta: { role: 'teacher' } }
    examsAPI.get.mockResolvedValue({ data: { id: 8, title: '期中考试', status: 'draft' } })
    examsAPI.getQuestions.mockResolvedValue({ data: { items: [] } })
    examsAPI.createQuestion.mockResolvedValue({ data: { id: 91 } })
    examsAPI.update.mockResolvedValue({ data: { id: 8, title: '期中考试', status: 'draft' } })
  })

  it('管理员从课程资料进入后，按真实任务归属返回资料', async () => {
    navigation.route = { params: { id: '8' }, query: { from: 'materials', course_id: '999', offering_id: '999' }, meta: { role: 'admin' } }
    examsAPI.get.mockResolvedValue({ data: { id: 8, title: '期中考试', status: 'draft', course_id: 101, offering_id: 109 } })
    const wrapper = mount(ExamQuestionEditView, { global: { stubs: { AppLayout: { template: '<main><slot /></main>' } } } })
    await flushPromises()
    expect(wrapper.find('.back-btn').text()).toContain('返回课程资料')
    await wrapper.find('.back-btn').trigger('click')
    expect(navigation.push).toHaveBeenCalledWith({ path: '/admin/materials', query: { course_id: '101', offering_id: '109' } })
  })

  it.each([
    ['admin', {}, '/admin/offerings', '返回授课安排'],
    ['teacher', {}, '/teacher/exams', '返回考试列表'],
    ['admin', { from: 'materials', course_id: '101', offering_id: '109' }, { path: '/admin/materials', query: { course_id: '101', offering_id: '109' } }, '返回课程资料'],
  ])('考试加载失败仍保留正确返回入口：%s %j', async (role, query, destination, label) => {
    navigation.route = { params: { id: '8' }, query, meta: { role } }
    examsAPI.get.mockRejectedValueOnce(new Error('unavailable'))
    const wrapper = mount(ExamQuestionEditView, { global: { stubs: { AppLayout: { template: '<main><slot /></main>' } } } })
    await flushPromises()
    expect(wrapper.find('.back-btn').text()).toContain(label)
    await wrapper.find('.back-btn').trigger('click')
    expect(navigation.push).toHaveBeenCalledWith(destination)
  })

  it('使用双栏布局，并通过两个下拉框配置公开策略', async () => {
    const wrapper = mount(ExamQuestionEditView, {
      global: { stubs: { AppLayout: { template: '<main><slot /></main>' } } },
    })
    await flushPromises()

    expect(wrapper.find('.editor-shell').exists()).toBe(true)
    expect(wrapper.find('.question-workspace').exists()).toBe(true)
    expect(wrapper.find('.control-rail').exists()).toBe(true)
    expect(wrapper.findAll('.visibility-block input[type="checkbox"]')).toHaveLength(0)

    await wrapper.find('#score-visibility').setValue('after_grading')
    await wrapper.find('#review-visibility').setValue('questions_and_answers')
    await wrapper.find('.save-settings').trigger('click')
    await flushPromises()

    expect(examsAPI.update).toHaveBeenCalledWith('8', expect.objectContaining({
      show_score_after_grading: true,
      show_questions_after_review: true,
      show_answers_after_review: true,
    }))
  })

  it('新建编程题默认 active，首次保存后原地解锁 AI 配置', async () => {
    const wrapper = mount(ExamQuestionEditView, {
      global: {
        stubs: {
          AppLayout: { template: '<main><slot /></main>' },
          QeTestCases: { template: '<div data-testid="test-cases">测试用例编辑器</div>' },
          AIQuestionConfig: { props: ['questionId'], template: '<div data-testid="ai-config">AI {{ questionId }}</div>' },
        },
      },
    })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('添加题目')).trigger('click')
    await wrapper.find('.modal-body select').setValue('code')
    await wrapper.find('textarea[placeholder="输入题目内容"]').setValue('编写求和函数')

    expect(wrapper.text()).toContain('先保存基础信息')
    await wrapper.findAll('button').find((button) => button.text() === '保存并继续配置').trigger('click')
    await flushPromises()

    expect(examsAPI.createQuestion).toHaveBeenCalledWith('8', expect.objectContaining({
      question_type: 'code', grading_mode: 'active', prompt: '编写求和函数',
    }))
    expect(wrapper.find('[data-testid="ai-config"]').text()).toContain('91')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
  })
})
