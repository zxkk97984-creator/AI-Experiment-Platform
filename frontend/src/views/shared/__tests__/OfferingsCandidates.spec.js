import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OfferingsView from '../OfferingsView.vue'

const mocks = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn(), students: vi.fn(), templates: vi.fn(), reviewHistory: vi.fn(), create: vi.fn(), addStudents: vi.fn(), role: 'admin' }))
vi.mock('../../../api/client.js', () => ({ default: { get: mocks.get } }))
vi.mock('../../../api/offerings.js', () => ({ offeringsAPI: mocks }))
vi.mock('../../../stores/auth.js', () => ({ useAuthStore: () => ({ get role() { return mocks.role }, get isAdmin() { return mocks.role === 'admin' }, get isStudent() { return mocks.role === 'student' } }) }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
const datasets = {
  '/courses': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, title: `课程${i + 1}` })),
  '/academic-terms': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, name: `学期${i + 1}`, status: 'active' })),
  '/teaching-classes': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, name: `班级${i + 1}`, academic_term_id: 1, status: i === 0 ? 'archived' : 'active' })),
  '/users': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, real_name: `教师${i + 1}`, username: `teacher${i + 1}`, status: 'active' })),
  '/users/students': Array.from({ length: 205 }, (_, i) => ({ id: i + 1, real_name: `学生${i + 1}`, student_no: `S${i + 1}` })),
}
const offerings = [1, 2].map(id => ({ id, course_id: 1, academic_term_id: 1, teaching_class_id: id, teacher_id: 101, teacher_name: '教师101', course_title: '共享课程', class_name: `${id}班`, term_name: '当前学期', status: 'active' }))
const wrappers = []
const getList = async (path, { params }) => {
  const rows = (datasets[path] || []).filter(row => path !== '/teaching-classes' || row.academic_term_id === params.academic_term_id)
  const filtered = rows.filter(row => !params.q || (path === '/users' ? row.real_name === params.q || row.username === params.q : JSON.stringify(row).includes(params.q)))
  return { data: { items: filtered.slice((params.page - 1) * params.page_size, params.page * params.page_size), total: filtered.length } }
}
async function open() {
  const wrapper = mount(OfferingsView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' }, LabAssessmentPanel: true, RouterLink: true, Teleport: true } } })
  wrappers.push(wrapper); await flushPromises(); return wrapper
}
const button = (wrapper, name) => wrapper.findAll('button').find(node => node.text() === name)
async function pick(wrapper, opener, page, option) {
  await wrapper.get(`[aria-label="${opener}"]`).trigger('click'); await flushPromises()
  if (page > 1) { const jump = wrapper.get('[aria-label="跳转页码"]'); await jump.setValue(String(page)); await jump.trigger('keydown.enter'); await flushPromises() }
  await wrapper.findAll('.candidate-row').find(row => row.text() === option).get('input').setValue(true)
  await button(wrapper, '确认选择').trigger('click'); await flushPromises()
}
beforeEach(() => {
  vi.resetAllMocks(); mocks.role = 'admin'; mocks.get.mockImplementation(getList)
  mocks.list.mockResolvedValue({ data: { items: offerings } })
  mocks.students.mockResolvedValue({ data: { items: [{ id: 1, real_name: '学生1', student_no: 'S1', status: 'active' }] } })
  mocks.templates.mockResolvedValue({ data: { tasks: [], experiments: [] } })
  mocks.reviewHistory.mockResolvedValue({ data: { items: [], tasks: [], total: 0 } })
  mocks.create.mockResolvedValue({ data: { id: 3 } }); mocks.addStudents.mockResolvedValue({})
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers() })

describe('授课安排完整候选', () => {
  it('101条学期完整加载，课程、班级、教师第101条可提交，列表按需读取', async () => {
    const wrapper = await open()
    expect(mocks.get.mock.calls.map(([path]) => path)).toEqual(['/academic-terms', '/academic-terms'])
    expect(wrapper.get('form select').findAll('option')).toHaveLength(102)
    await wrapper.get('form select').setValue('1')
    await pick(wrapper, '选择课程', 6, '课程101')
    await pick(wrapper, '选择教学班', 6, '班级101')
    await pick(wrapper, '选择授课教师', 6, '教师101 · teacher101')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(mocks.create).toHaveBeenCalledWith({ course_id: 101, academic_term_id: 1, teaching_class_id: 101, teacher_id: 101 })
    const courseCalls = mocks.get.mock.calls.filter(([path]) => path === '/courses')
    expect(courseCalls.every(([, { params }]) => params.page_size === 20 && params.sort_by === 'title' && !('academic_term_id' in params))).toBe(true)
    expect(mocks.get.mock.calls.filter(([path]) => path === '/users').every(([, { params }]) => params.role === 'teacher' && params.status_filter === 'active')).toBe(true)
    expect(mocks.get.mock.calls.filter(([path]) => path === '/academic-terms')).toHaveLength(2)
  })

  it('学期第二页失败不发布半份选项，仍可选择课程；重试后去重并完整替换', async () => {
    let fail = true
    mocks.get.mockImplementation(async (path, args) => {
      if (path === '/academic-terms' && args.params.page === 2 && fail) throw new Error('学期暂不可用')
      const result = await getList(path, args)
      if (path === '/academic-terms' && args.params.page === 2) result.data.items.unshift(datasets[path][0])
      return result
    })
    const wrapper = await open()
    expect(wrapper.get('form select').findAll('option')).toHaveLength(1)
    await pick(wrapper, '选择课程', 1, '课程1')
    expect(wrapper.get('[aria-label="选择课程"]').text()).toBe('课程1')
    fail = false; await button(wrapper, '重试学期加载').trigger('click'); await flushPromises()
    expect(wrapper.get('form select').findAll('option')).toHaveLength(102)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('学期切换清除所选班级，迟到旧学期候选不显示', async () => {
    const wrapper = await open()
    await wrapper.get('form select').setValue('1'); await pick(wrapper, '选择教学班', 1, '班级2')
    await wrapper.get('form select').setValue('2')
    expect(wrapper.get('[aria-label="选择教学班"]').text()).toBe('请选择')
    let resolveOld
    await wrapper.get('form select').setValue('1')
    mocks.get.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    await wrapper.get('[aria-label="选择教学班"]').trigger('click'); await flushPromises()
    await wrapper.get('form select').setValue('2')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    await wrapper.get('[aria-label="选择教学班"]').trigger('click'); await flushPromises()
    resolveOld({ data: { items: [{ id: 9, name: '旧学期班级' }], total: 1 } }); await flushPromises()
    expect(wrapper.get('[role="dialog"]').text()).not.toContain('旧学期班级')
    expect(mocks.get).toHaveBeenLastCalledWith('/teaching-classes', { params: { page: 1, page_size: 20, q: undefined, academic_term_id: 2 } })
  })

  it('205名学生跨页选择，本班禁选，取消保留确认值，切换实例清空待添加名单', async () => {
    const wrapper = await open()
    await wrapper.findAll('.class-card')[0].trigger('click'); await flushPromises()
    expect(wrapper.get('[aria-label="选择负责教师"]').text()).toBe('教师101')
    await wrapper.get('[aria-label="选择补充学生"]').trigger('click'); await flushPromises()
    expect(wrapper.findAll('.candidate-row')[0].get('input').element.disabled).toBe(true)
    await wrapper.findAll('.candidate-row')[1].get('input').setValue(true)
    const jump = wrapper.get('[aria-label="跳转页码"]'); await jump.setValue('11'); await jump.trigger('keydown.enter'); await flushPromises()
    await wrapper.findAll('.candidate-row').find(row => row.text() === 'S205 · 学生205').get('input').setValue(true)
    await button(wrapper, '确认选择').trigger('click'); await flushPromises()
    expect(wrapper.get('.chosen-students').text()).toContain('学生205')
    await wrapper.get('[aria-label="选择补充学生"]').trigger('click'); await flushPromises()
    await wrapper.get('[aria-label="移除S205 · 学生205"]').trigger('click')
    await button(wrapper, '取消').trigger('click')
    expect(wrapper.get('.chosen-students').text()).toContain('学生205')
    await wrapper.findAll('.class-card')[1].trigger('click'); await flushPromises()
    expect(wrapper.find('.chosen-students').exists()).toBe(false)
    expect(button(wrapper, '补充名单').element.disabled).toBe(true)
    expect(mocks.get.mock.calls.filter(([path]) => path === '/users/students').every(([, { params }]) => params.page_size === 20 && !('exclude_class_id' in params))).toBe(true)
  })

  it('教师搜索提示精确匹配且保留所选教师，局部失败可重试', async () => {
    vi.useFakeTimers()
    const wrapper = await open()
    await wrapper.get('[aria-label="选择授课教师"]').trigger('click'); await flushPromises()
    expect(wrapper.get('[role="dialog"]').text()).toContain('完整姓名或账号精确搜索')
    mocks.get.mockRejectedValueOnce(new Error('offline'))
    await wrapper.get('input[type="search"]').setValue('教师101'); await vi.advanceTimersByTimeAsync(250); await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('重试')
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(mocks.get).toHaveBeenLastCalledWith('/users', { params: { page: 1, page_size: 20, q: '教师101', role: 'teacher', status_filter: 'active' } })
    await wrapper.get('.candidate-row input').setValue(true); await button(wrapper, '确认选择').trigger('click')
    expect(wrapper.get('[aria-label="选择授课教师"]').text()).toContain('教师101')
  })

  it.each(['teacher', 'student'])('%s不请求管理员候选API', async role => {
    mocks.role = role
    const wrapper = await open()
    expect(mocks.get).not.toHaveBeenCalled()
    expect(wrapper.find('[aria-label="选择课程"]').exists()).toBe(false)
  })
})
