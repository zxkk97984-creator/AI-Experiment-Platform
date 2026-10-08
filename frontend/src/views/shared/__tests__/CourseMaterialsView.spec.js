import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import CourseMaterialsView from '../CourseMaterialsView.vue'

const state = vi.hoisted(() => ({ role: 'student', route: { params: {}, query: {} }, push: vi.fn() }))
vi.mock('vue-router', () => ({ useRoute: () => state.route, useRouter: () => ({ push: state.push }) }))
vi.mock('../../../stores/auth.js', () => ({ useAuthStore: () => state }))
vi.mock('../../../api/courses.js', () => ({ coursesAPI: { list: vi.fn(), get: vi.fn(), getChapters: vi.fn() } }))
vi.mock('../../../api/courseMaterials.js', () => ({ courseMaterialsAPI: {
  list: vi.fn(), chapters: vi.fn(), offerings: vi.fn(), activities: vi.fn(), activityOptions: vi.fn(),
  access: vi.fn(), create: vi.fn(), update: vi.fn(), upload: vi.fn(), archive: vi.fn(), link: vi.fn(), unlink: vi.fn(),
} }))
import { coursesAPI } from '../../../api/courses.js'
import { courseMaterialsAPI as api } from '../../../api/courseMaterials.js'
const pdf = { id: 4, title: '第一章讲义', description: '课程参考资料', chapter_id: 2, category: 'lecture', status: 'published',
  filename: '讲义.pdf', content_type: 'application/pdf', size_bytes: 1024, previewable: true, source: 'file', version: 3 }
async function render() {
  const wrapper = mount(CourseMaterialsView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
  await flushPromises(); return wrapper
}

describe('course material page', () => {
  beforeEach(() => {
    vi.clearAllMocks(); state.role = 'student'; state.route = { params: {}, query: {} }
    coursesAPI.list.mockResolvedValue({ data: { items: [{ id: 1, title: '人工智能基础' }] } })
    api.chapters.mockResolvedValue({ data: { items: [{ id: 2, title: '第一章' }] } })
    api.list.mockResolvedValue({ data: { items: [pdf], can_edit: false, total: 1 } })
    api.offerings.mockResolvedValue({ data: { items: [{ id: 9, course_id: 1, class_name: '一班', term_name: '秋季', status: 'active' }] } })
    api.activities.mockResolvedValue({ data: [{ id: 8, title: '本班练习', chapter_id: 2, status: 'published', route: '/student/assignments/5?offering_id=9' }] })
    api.activityOptions.mockResolvedValue({ data: [] })
  })
  it('shows chapter materials and authorized activities without edit controls', async () => {
    const wrapper = await render()
    expect(wrapper.text()).toContain('第一章讲义'); expect(wrapper.text()).toContain('本班练习')
    expect(wrapper.text()).not.toContain('添加资料'); expect(wrapper.text()).not.toContain('关联本班学习活动')
    await wrapper.find('.activities button').trigger('click')
    expect(state.push).toHaveBeenCalledWith('/student/assignments/5?offering_id=9')
  })
  it('restores an explicitly requested course beyond the first 100 courses', async () => {
    state.role = 'admin'; state.route.query = { course_id: '101', offering_id: '109' }
    coursesAPI.list.mockResolvedValue({ data: { items: Array.from({ length: 100 }, (_, i) => ({ id: i + 1, title: `课程${i + 1}` })) } })
    coursesAPI.get.mockResolvedValue({ data: { id: 101, title: '第101门课程' } })
    api.offerings.mockResolvedValue({ data: { items: [{ id: 109, course_id: 101, class_name: '返回班级', term_name: '秋季', status: 'active' }] } })
    const wrapper = await render()
    expect(wrapper.find('.filters select').element.value).toBe('101')
    expect(wrapper.findAll('.filters select')[1].element.value).toBe('109')
    expect(wrapper.text()).toContain('第101门课程')
    expect(coursesAPI.get).toHaveBeenCalledWith('101')
    expect(api.list).toHaveBeenCalledWith('101', expect.objectContaining({ offering_id: '109' }))
  })
  it.each([403, 404])('keeps an inaccessible requested course explicit (%s), without selecting the first course', async (status) => {
    state.route.query = { course_id: '101', offering_id: '109' }
    coursesAPI.get.mockRejectedValue({ response: { status, data: { detail: { message: '指定课程不可访问' } } } })
    const wrapper = await render()
    expect(wrapper.find('[role="alert"]').text()).toContain('指定课程不可访问')
    expect(wrapper.find('.filters select').element.value).toBe('')
    expect(api.list).not.toHaveBeenCalled()
  })
  it('retries the requested course lookup instead of silently loading the first course', async () => {
    state.route.query = { course_id: '101', offering_id: '109' }
    coursesAPI.get.mockRejectedValueOnce({ response: { data: { detail: { message: '课程服务暂不可用' } } } })
    const wrapper = await render()
    coursesAPI.get.mockResolvedValue({ data: { id: 101, title: '恢复的课程' } })
    await wrapper.find('.state button').trigger('click'); await flushPromises()
    expect(wrapper.find('.filters select').element.value).toBe('101')
    expect(api.list).toHaveBeenCalledWith('101', expect.objectContaining({ offering_id: '109' }))
  })
  it('uses the admin activity editor URL with its material return context', async () => {
    state.role = 'admin'
    const route = '/admin/assignments/5/edit?offering_id=9&from=materials&course_id=1'
    api.activities.mockResolvedValue({ data: [{ id: 8, title: '本班练习', chapter_id: 2, status: 'published', route }] })
    const wrapper = await render()
    await wrapper.find('.activities button').trigger('click')
    expect(state.push).toHaveBeenCalledWith(route)
  })
  it('requests offering-bound PDF preview and closes it', async () => {
    api.access.mockResolvedValue({ data: { url: '/api/v1/media/course-materials/4?sig=test', previewable: true } })
    const wrapper = await render()
    await wrapper.findAll('.actions button').find(button => button.text() === '预览').trigger('click'); await flushPromises()
    expect(api.access).toHaveBeenCalledWith(4, { offering_id: '9', preview: true })
    expect(wrapper.find('iframe').attributes('title')).toBe('第一章讲义 PDF 预览')
    await wrapper.find('.preview-modal button').trigger('click')
    expect(wrapper.find('iframe').exists()).toBe(false)
  })
  it('opens official URL in browser with noopener without fetching access URL', async () => {
    api.list.mockResolvedValue({ data: { items: [{ ...pdf, source: 'external', external_url: 'https://www.python.org/downloads/' }], can_edit: false, total: 1 } })
    const wrapper = await render(), anchor = wrapper.find('.actions a')
    expect(anchor.attributes('href')).toBe('https://www.python.org/downloads/')
    expect(anchor.attributes('rel')).toBe('noopener noreferrer'); expect(anchor.attributes('target')).toBe('_blank')
    expect(api.access).not.toHaveBeenCalled()
  })
  it('assigned teacher reads shared content and selects only offered activity options', async () => {
    state.role = 'teacher'
    api.activityOptions.mockResolvedValue({ data: [{ target_type: 'assignment', target_id: 20, title: '本班作业', status: 'published' }] })
    const wrapper = await render()
    expect(wrapper.text()).toContain('共享资料由课程负责人维护')
    expect(wrapper.text()).not.toContain('添加资料'); expect(wrapper.find('.link-form').text()).toContain('本班作业')
    api.link.mockResolvedValue({ data: {} })
    await wrapper.findAll('.link-form select')[0].setValue('2'); await wrapper.findAll('.link-form select')[2].setValue('20')
    await wrapper.find('.link-form').trigger('submit'); await flushPromises()
    expect(api.link).toHaveBeenCalledWith('9', { chapter_id: 2, target_type: 'assignment', target_id: 20 })
  })
  it('owner creates draft metadata, uploads file and publishes after success', async () => {
    state.role = 'teacher'; api.list.mockResolvedValue({ data: { items: [pdf], can_edit: true, total: 1 } })
    api.create.mockResolvedValue({ data: { ...pdf, id: 12, version: 1, status: 'draft' } })
    api.upload.mockResolvedValue({ data: { ...pdf, id: 12, version: 2, status: 'draft' } })
    api.update.mockResolvedValue({ data: { ...pdf, id: 12, version: 3 } })
    const wrapper = await render()
    await wrapper.find('.page-heading button').trigger('click')
    const editor = wrapper.findComponent({ name: 'MaterialEditor' })
    const file = new File(['%PDF-1.7'], 'new.pdf', { type: 'application/pdf' })
    editor.vm.$emit('save', { title: '新讲义', description: '', chapter_id: 2, category: 'lecture', source: 'file', external_url: '', order_index: 0, status: 'published', file })
    await flushPromises()
    expect(api.upload.mock.calls[0].slice(0, 3)).toEqual([12, file, 1])
    expect(api.update).toHaveBeenCalledWith(12, { version: 2, status: 'published' })
    expect(wrapper.findComponent({ name: 'MaterialEditor' }).exists()).toBe(false)
  })
  it('preserves explicit unavailable and empty states', async () => {
    api.list.mockRejectedValue({ response: { data: { detail: { message: '无权访问课程资料' } } } })
    const wrapper = await render()
    expect(wrapper.find('[role="alert"]').text()).toContain('无权访问课程资料')
    api.list.mockResolvedValue({ data: { items: [], total: 0, can_edit: false } }); api.activities.mockResolvedValue({ data: [] })
    await wrapper.find('.state button').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('本章节暂无资料或活动')
  })
  it('uses configured list response limits in the editor before any metadata write', async () => {
    state.role = 'teacher'
    api.list.mockResolvedValue({ data: { items: [pdf], can_edit: true, total: 1,
      ordinary_max_bytes: 3 * 1048576, large_max_bytes: 7 * 1048576 } })
    const wrapper = await render()
    await wrapper.find('.page-heading button').trigger('click')
    expect(wrapper.find('[role="dialog"]').text()).toContain('IPYNB 最大 3 MiB')
    expect(wrapper.find('[role="dialog"]').text()).toContain('视频、ZIP 最大 7 MiB')
    await wrapper.find('[role="dialog"] input[maxlength="200"]').setValue('讲义')
    const input = wrapper.find('input[type="file"]'), oversized = new File(['x'], 'lecture.pdf')
    Object.defineProperty(oversized, 'size', { value: 3 * 1048576 + 1 })
    Object.defineProperty(input.element, 'files', { value: [oversized] })
    await input.trigger('change')
    await wrapper.find('[role="dialog"] form').trigger('submit')
    expect(wrapper.find('[role="dialog"] [role="alert"]').text()).toContain('超过 3 MiB')
    expect(api.create).not.toHaveBeenCalled()
    expect(api.upload).not.toHaveBeenCalled()
  })
})
