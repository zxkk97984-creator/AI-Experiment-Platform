/** 课程级成绩册模板弹窗：只编辑分类与规则，比例必须合计 100%。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CourseGradebookTemplateModal from '../CourseGradebookTemplateModal.vue'

const mocks = vi.hoisted(() => ({ courseTemplate: vi.fn(), saveCourseTemplate: vi.fn(), showToast: vi.fn() }))
vi.mock('../../../api/gradebook.js', () => ({ gradebookAPI: mocks }))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast: mocks.showToast }) }))

const FOUR = [
  { key: 'regular', title: '平时成绩', weight: '20', weight_mode: 'equal' },
  { key: 'labs', title: '实验成绩', weight: '30', weight_mode: 'equal' },
  { key: 'midterm', title: '期中成绩', weight: '20', weight_mode: 'equal' },
  { key: 'final', title: '期末成绩', weight: '30', weight_mode: 'equal' },
]
const course = { id: 7, title: '人工智能通识实践' }

async function open() {
  const wrapper = mount(CourseGradebookTemplateModal, { props: { course } })
  await flushPromises()
  return wrapper
}

describe('课程成绩模板弹窗', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.courseTemplate.mockResolvedValue({ data: { course_id: 7, customized: false, categories: FOUR } })
    mocks.saveCourseTemplate.mockResolvedValue({ data: { course_id: 7 } })
  })

  it('加载课程默认四类模板并显示比例合计', async () => {
    const wrapper = await open()
    expect(mocks.courseTemplate).toHaveBeenCalledWith(7)
    expect(wrapper.findAll('.template-row')).toHaveLength(4)
    const titles = wrapper.findAll('.template-row input').filter((input, index) => index % 3 === 1).map(input => input.element.value)
    expect(titles).toEqual(['平时成绩', '实验成绩', '期中成绩', '期末成绩'])
    expect(wrapper.text()).toContain('分类占比合计 100%（须等于 100%）')
    expect(wrapper.findAll('button').find(item => item.text() === '保存课程模板').element.disabled).toBe(false)
    wrapper.unmount()
  })

  it('比例不等于 100% 时不允许保存，调整后可以保存并通知父级', async () => {
    const wrapper = await open()
    const weightInputs = wrapper.findAll('.template-row input[type="number"]')
    await weightInputs[1].setValue('40')
    const save = wrapper.findAll('button').find(item => item.text() === '保存课程模板')
    expect(wrapper.text()).toContain('分类占比合计 110%')
    expect(save.element.disabled).toBe(true)
    await weightInputs[1].setValue('30')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const [courseId, payload] = mocks.saveCourseTemplate.mock.calls[0]
    expect(courseId).toBe(7)
    expect(payload.categories.map(item => [item.key, String(item.weight), item.weight_mode])).toEqual([
      ['regular', '20', 'equal'], ['labs', '30', 'equal'], ['midterm', '20', 'equal'], ['final', '30', 'equal']])
    expect(mocks.showToast).toHaveBeenCalledWith('课程成绩模板已保存，仅对之后新开的班生效', 'success')
    expect(wrapper.emitted('saved')).toBeTruthy()
    wrapper.unmount()
  })

  it('加载失败时显示错误且不能保存', async () => {
    mocks.courseTemplate.mockRejectedValue({ response: { data: { detail: { message: '没有权限管理该课程' } } } })
    const wrapper = await open()
    expect(wrapper.get('[role="alert"]').text()).toContain('没有权限管理该课程')
    expect(wrapper.findAll('button').find(item => item.text() === '保存课程模板').element.disabled).toBe(true)
    wrapper.unmount()
  })
})
