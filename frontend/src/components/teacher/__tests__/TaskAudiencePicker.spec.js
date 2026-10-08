import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import TaskAudiencePicker from '../TaskAudiencePicker.vue'
import TeachingClassMultiSelect from '../TeachingClassMultiSelect.vue'

vi.mock('../../../api/courses.js', () => ({ coursesAPI: { get: vi.fn() } }))
vi.mock('../../../api/users.js', () => ({ usersAPI: { listStudents: vi.fn() } }))
vi.mock('../../../api/offerings.js', () => ({ offeringsAPI: { list: vi.fn(), students: vi.fn() } }))
vi.mock('../../../api/assignments.js', () => ({ assignmentsAPI: { importAudienceStudents: vi.fn() } }))
vi.mock('../../../api/exams.js', () => ({ examsAPI: { importAudienceStudents: vi.fn() } }))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast: vi.fn() }) }))

import { coursesAPI } from '../../../api/courses.js'
import { usersAPI } from '../../../api/users.js'
import { offeringsAPI } from '../../../api/offerings.js'

const offerings = [
  { id: 10, course_id: 1, teaching_class_id: 101, class_name: 'A班' },
  { id: 20, course_id: 1, teaching_class_id: 202, class_name: 'B班' },
]
const studentA = { id: 3, real_name: '本班学生', student_no: 'A003', status: 'active' }
const studentB = { id: 4, real_name: '另班学生', student_no: 'B004', status: 'active' }

function mountPicker(props = {}) {
  return mount(TaskAudiencePicker, {
    props: { taskKind: 'assignment', taskId: 7, courseId: 1, offeringId: 10, ...props },
    global: { stubs: { AppIcon: true, DsSelect: { props: ['modelValue', 'disabled'], template: '<select :disabled="disabled"><slot /></select>' } } },
  })
}

describe('TaskAudiencePicker class authority', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    offeringsAPI.list.mockResolvedValue({ data: { items: offerings } })
    offeringsAPI.students.mockImplementation(id => Promise.resolve({ data: { items: Number(id) === 10 ? [studentA, { id: 9, real_name: '已转班学生', status: 'transferred' }] : [studentB] } }))
    coursesAPI.get.mockResolvedValue({ data: { teaching_classes: [{ id: 101, name: 'A班' }, { id: 202, name: 'B班' }] } })
    usersAPI.listStudents.mockResolvedValue({ data: { items: [studentA, studentB] } })
  })

  it('班级任务只展示所属教学班和在册学生，不调用全局候选接口', async () => {
    const wrapper = mountPicker({ audienceMode: 'selected_classes' })
    await flushPromises()
    expect(wrapper.findComponent(TeachingClassMultiSelect).props('options')).toEqual([{ id: 101, name: 'A班', status: 'active' }])
    await wrapper.findAll('input[type="search"]')[0].trigger('keyup', { key: 'Enter' })
    expect(wrapper.findAll('.student-candidates button')).toHaveLength(2)
    expect(wrapper.text()).toContain('本班学生')
    expect(wrapper.text()).not.toContain('另班学生')
    expect(wrapper.text()).not.toContain('已转班学生')
    expect(usersAPI.listStudents).not.toHaveBeenCalled()
    expect(coursesAPI.get).not.toHaveBeenCalled()
    await wrapper.find('.student-candidates button').trigger('click')
    expect(wrapper.emitted('update:whitelistIds').at(-1)).toEqual([[3]])
    wrapper.unmount()
  })

  it('仅在本班搜索姓名和学号', async () => {
    const wrapper = mountPicker()
    await flushPromises()
    const input = wrapper.findAll('input[type="search"]')[0]
    await input.setValue('A003')
    await input.trigger('keyup', { key: 'Enter' })
    expect(wrapper.findAll('.student-candidates button')).toHaveLength(2)
    await input.setValue('B004')
    await input.trigger('keyup', { key: 'Enter' })
    expect(wrapper.find('.student-candidates').exists()).toBe(false)
    expect(usersAPI.listStudents).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('加载失败时禁止选择并可重试，不回退到全局学生名单', async () => {
    offeringsAPI.students.mockRejectedValueOnce(new Error('forbidden'))
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
    expect(wrapper.findAll('input[type="search"]')[0].attributes('disabled')).toBeDefined()
    expect(usersAPI.listStudents).not.toHaveBeenCalled()
    await wrapper.get('[role="alert"] button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.findAll('input[type="search"]')[0].attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('切换授课实例后丢弃旧响应与旧学生名称', async () => {
    let finishOld
    offeringsAPI.students.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
    const wrapper = mountPicker({ whitelistIds: [3] })
    await wrapper.setProps({ offeringId: 20, taskId: 8, whitelistIds: [] })
    await flushPromises()
    finishOld({ data: { items: [studentA] } })
    await flushPromises()
    await wrapper.findAll('input[type="search"]')[0].trigger('keyup', { key: 'Enter' })
    expect(wrapper.text()).toContain('另班学生')
    expect(wrapper.text()).not.toContain('本班学生')
    expect(wrapper.findComponent(TeachingClassMultiSelect).exists()).toBe(false)
    expect(usersAPI.listStudents).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('共享模板保留原课程班级与学生候选行为', async () => {
    const wrapper = mountPicker({ offeringId: null, audienceMode: 'selected_classes' })
    await flushPromises()
    expect(wrapper.findComponent(TeachingClassMultiSelect).props('options')).toHaveLength(2)
    expect(coursesAPI.get).toHaveBeenCalledWith(1)
    expect(usersAPI.listStudents).toHaveBeenCalledWith({ page_size: 100 })
    expect(offeringsAPI.students).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('仅可选择本授课班')
    wrapper.unmount()
  })
})
