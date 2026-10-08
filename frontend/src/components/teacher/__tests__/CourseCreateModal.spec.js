import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const showToastMock = vi.hoisted(() => vi.fn())
const authState = vi.hoisted(() => ({ isAdmin: true }))
vi.mock('../../../stores/auth.js', () => ({ useAuthStore: () => authState }))

vi.mock('../../../api/courses', () => ({
  coursesAPI: {
    create: vi.fn(),
    uploadCourseCover: vi.fn(),
    addWhitelistStudent: vi.fn(),
  },
}))

vi.mock('../../../api/users', () => ({
  usersAPI: {
    listStudents: vi.fn(),
  },
}))

vi.mock('../../../api/academics', () => ({
  academicsAPI: {
    listClasses: vi.fn(),
  },
}))

vi.mock('../../../stores/app', () => ({
  useAppStore: () => ({ showToast: showToastMock }),
}))

import { coursesAPI } from '../../../api/courses.js'
import { academicsAPI } from '../../../api/academics.js'
import { usersAPI } from '../../../api/users.js'
import CourseCreateModal from '../CourseCreateModal.vue'

const terms = [
  { id: 1, name: '2026 秋季学期', status: 'active' },
  { id: 2, name: '2025 春季学期', status: 'closed' },
]

function mountModal() {
  return mount(CourseCreateModal, {
    props: { terms },
    global: {
      stubs: {
        AppIcon: { template: '<span />' },
        Teleport: { template: '<div><slot /></div>' },
      },
    },
  })
}

describe('CourseCreateModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    authState.isAdmin = true
    academicsAPI.listClasses.mockResolvedValue({
      data: { items: [{ id: 10, name: '计算机 2601 班', student_count: 32 }] },
    })
    coursesAPI.create.mockResolvedValue({ data: { id: 99, title: '数据结构' } })
    coursesAPI.uploadCourseCover.mockResolvedValue({ data: { id: 99, title: '数据结构', cover: 'covers/99.png' } })
    coursesAPI.addWhitelistStudent.mockResolvedValue({})
    usersAPI.listStudents.mockResolvedValue({
      data: { items: [{ id: 7, username: 'student_7', real_name: '李同学', status: 'active' }] },
    })
  })

  it('只有课程名称是提交必填项，关闭学期不可选', async () => {
    const wrapper = mountModal()
    const submit = wrapper.get('button[type="submit"]')

    expect(submit.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('option[disabled]').map((option) => option.text())).toContain('2025 春季学期（已关闭）')
  })

  it('选择学期后加载教学班，并按方案提交课程字段', async () => {
    const wrapper = mountModal()
    const selects = wrapper.findAll('select')

    await wrapper.get('input[placeholder="输入课程名称"]').setValue(' 数据结构 ')
    await wrapper.get('input[placeholder="例如：CS101"]').setValue(' CS101 ')
    await wrapper.get('textarea[placeholder="输入课程简介"]').setValue('数据结构与算法')
    await selects[0].setValue('1')
    await flushPromises()
    await wrapper.get('[data-testid="course-create-teaching-classes"] .teaching-class-trigger').trigger('click')
    await wrapper.get('[data-testid="course-create-teaching-classes"] .teaching-class-option').trigger('click')
    await wrapper.get('input[type="datetime-local"]').setValue('2026-09-01T08:00')
    await wrapper.get('input[type="number"]').setValue('90')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(academicsAPI.listClasses).toHaveBeenCalledWith({ academic_term_id: 1, page: 1, page_size: 50 })
    expect(coursesAPI.create).toHaveBeenCalledWith({
      title: '数据结构',
      code: 'CS101',
      description: '数据结构与算法',
      academic_term_id: 1,
      teaching_class_ids: [10],
      start_time: '2026-09-01T08:00',
      visibility: 'class',
      default_score: 90,
    })
    expect(wrapper.emitted('created')).toHaveLength(1)
  })

  it('教学班超过一页时继续加载全部分页供选择', async () => {
    academicsAPI.listClasses
      .mockResolvedValueOnce({ data: { items: [{ id: 10, name: '第一页班级' }], page: 1, page_size: 50, total: 51 } })
      .mockResolvedValueOnce({ data: { items: [{ id: 60, name: '第二页班级' }], page: 2, page_size: 50, total: 51 } })
    const wrapper = mountModal()

    await wrapper.get('select').setValue('1')
    await flushPromises()

    expect(academicsAPI.listClasses).toHaveBeenNthCalledWith(1, { academic_term_id: 1, page: 1, page_size: 50 })
    expect(academicsAPI.listClasses).toHaveBeenNthCalledWith(2, { academic_term_id: 1, page: 2, page_size: 50 })
  })

  it('封面上传失败后可重试且不会重复创建课程', async () => {
    coursesAPI.uploadCourseCover
      .mockRejectedValueOnce({ response: { data: { detail: { message: '上传失败' } } } })
      .mockResolvedValueOnce({ data: { id: 99, title: '数据结构', cover: 'covers/99.png' } })
    const wrapper = mountModal()
    const file = new File(['cover'], 'cover.png', { type: 'image/png' })

    await wrapper.get('input[placeholder="输入课程名称"]').setValue('数据结构')
    const fileInput = wrapper.get('input[type="file"]').element
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true })
    await wrapper.get('input[type="file"]').trigger('change')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="status"]').text()).toContain('封面上传失败')
    await wrapper.findAll('button').find((button) => button.text() === '重试上传').trigger('click')
    await flushPromises()

    expect(coursesAPI.create).toHaveBeenCalledTimes(1)
    expect(coursesAPI.uploadCourseCover).toHaveBeenCalledTimes(2)
    expect(wrapper.emitted('created')).toHaveLength(1)
  })

  it('创建前可选择学生白名单，创建成功后再写入课程白名单', async () => {
    const wrapper = mountModal()

    await wrapper.get('input[placeholder="输入课程名称"]').setValue('数据结构')
    await wrapper.get('[data-testid="course-create-visibility"]').setValue('whitelist')
    await flushPromises()
    await wrapper.get('.wl-add').trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(coursesAPI.create).toHaveBeenCalledTimes(1)
    expect(coursesAPI.addWhitelistStudent).toHaveBeenCalledWith(99, 7)
    expect(wrapper.emitted('created')).toHaveLength(1)
  })

  it('按 Esc 关闭弹窗，标题为空时不会提交', async () => {
    const wrapper = mountModal()

    await wrapper.get('form').trigger('submit')
    expect(coursesAPI.create).not.toHaveBeenCalled()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
