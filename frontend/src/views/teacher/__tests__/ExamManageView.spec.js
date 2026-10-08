/** 考试管理页 ExamManageView 组件测试：列表加载、创建弹窗、课程选择弹窗（点选/手输回填）、提交 payload 携带数字 course_id */
/** 课程选择交互与作业管理页一致：单一课程展示框触发弹窗，弹窗内列表点选或手输 ID，未选择显示「选择课程」 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const pushMock = vi.hoisted(() => vi.fn())
const showToastMock = vi.hoisted(() => vi.fn())

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: pushMock }),
  createRouter: vi.fn(() => ({
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    beforeResolve: vi.fn(),
    push: pushMock,
    replace: vi.fn(),
    currentRoute: { value: { path: '/teacher/exams' } },
  })),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../../../api/exams', () => ({
  examsAPI: { list: vi.fn(), create: vi.fn(), update: vi.fn() },
}))

vi.mock('../../../api/offerings', () => ({ offeringsAPI: { list: vi.fn() } }))

vi.mock('../../../api/courses', () => ({
  coursesAPI: { list: vi.fn() },
}))

vi.mock('../../../stores/app', () => ({
  useAppStore: () => ({ showToast: showToastMock }),
}))

import { examsAPI } from '../../../api/exams.js'
import { offeringsAPI } from '../../../api/offerings.js'
import { coursesAPI } from '../../../api/courses.js'

const courses = [
  { id: 10001, title: '机器学习' },
  { id: 10002, title: '深度学习' },
]

async function mountPage() {
  const mod = await import('../ExamManageView.vue')
  return mount(mod.default, {
    global: {
      stubs: { AppLayout: { template: '<div><slot /></div>' } },
    },
  })
}

/** 点击页头「创建考试」打开创建弹窗 */
async function openCreateForm(wrapper) {
  const btn = wrapper.findAll('button').find((b) => b.text().includes('创建考试'))
  expect(btn, '页面应有「创建考试」按钮').toBeDefined()
  await btn.trigger('click')
}

/** 点击课程展示框打开课程选择弹窗 */
async function openCourseModal(wrapper) {
  const picker = wrapper.find('.course-picker')
  expect(picker, '创建表单内应有课程展示框').toBeDefined()
  await picker.trigger('click')
}

/** 在课程选择弹窗中点击指定 ID 的课程项 */
async function pickCourseItem(wrapper, id) {
  const item = wrapper.findAll('.course-item').find((b) => b.text().includes(`ID: ${id}`))
  expect(item, '课程弹窗应有对应课程项').toBeDefined()
  await item.trigger('click')
}

/** 在课程选择弹窗中手输课程 ID 并点「确定」 */
async function confirmManualCourse(wrapper, id) {
  await wrapper.find('.course-id-input').setValue(id)
  await wrapper.find('.manual-confirm').trigger('click')
}

/** 在创建弹窗内点「确定」提交 */
async function submitCreate(wrapper) {
  const btn = wrapper.findAll('.create-form button').find((b) => b.text().trim() === '确定')
  expect(btn, '创建弹窗应有「确定」按钮').toBeDefined()
  await btn.trigger('click')
  await flushPromises()
}

describe('考试管理页 ExamManageView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    offeringsAPI.list.mockResolvedValue({ data: { items: [] } })
    pushMock.mockClear()
    examsAPI.list.mockResolvedValue({ data: { items: [] } })
  })

  it('加载并展示考试列表', async () => {
    examsAPI.list.mockResolvedValue({
      data: { items: [{ id: 1, title: '期中考试', status: 'draft', duration_minutes: 60 }] },
    })
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('期中考试')
  })

  it('把筛选和排序交给服务端，并按 total 分页', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    examsAPI.list.mockResolvedValue({
      data: {
        items: [{ id: 1, title: '目标考试', status: 'draft', course_id: 10001, duration_minutes: 60 }],
        page: 1,
        page_size: 10,
        total: 25,
      },
    })
    const wrapper = await mountPage()
    await flushPromises()

    expect(examsAPI.list).toHaveBeenCalledWith({
      page: 1,
      page_size: 10,
      sort: 'updated_desc',
    })
    const next = wrapper.find('[aria-label="考试列表分页"] button[aria-label="下一页"]')
    expect(next.exists()).toBe(true)
    await next.trigger('click')
    await flushPromises()
    expect(examsAPI.list).toHaveBeenLastCalledWith({
      page: 2,
      page_size: 10,
      sort: 'updated_desc',
    })

    await wrapper.find('input[aria-label="搜索考试名称或课程"]').setValue('目标')
    await wrapper.find('input[aria-label="搜索考试名称或课程"]').trigger('input')
    await wrapper.find('select[aria-label="状态筛选"]').setValue('draft')
    await wrapper.find('select[aria-label="课程筛选"]').setValue('10001')
    await wrapper.find('select[aria-label="排序"]').setValue('title_asc')
    await flushPromises()

    expect(examsAPI.list).toHaveBeenLastCalledWith({
      page: 1,
      page_size: 10,
      q: '目标',
      status: 'draft',
      course_id: 10001,
      sort: 'title_asc',
    })
  })

  it('点击「创建考试」打开信息弹窗，确认后跳转题目编辑页', async () => {
    examsAPI.create.mockResolvedValue({ data: { id: 23 } })
    const wrapper = await mountPage()
    await flushPromises()

    await openCreateForm(wrapper)
    expect(wrapper.find('[role="dialog"][aria-label="创建考试"]').exists()).toBe(true)
    await wrapper.find('input[placeholder="输入考试名称"]').setValue('期中考试')
    await openCourseModal(wrapper)
    await confirmManualCourse(wrapper, '10001')
    await submitCreate(wrapper)

    expect(pushMock).toHaveBeenCalledWith('/teacher/exams/23/edit')
  })

  it('创建表单中课程选择器未选择时显示「选择课程」占位', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)

    const picker = wrapper.find('.course-picker')
    expect(picker.exists()).toBe(true)
    expect(picker.text()).toContain('选择课程')
  })

  it('点击课程展示框打开课程选择弹窗，弹窗展示课程名与 ID', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)

    expect(wrapper.find('.course-picker-panel').exists()).toBe(false)
    await openCourseModal(wrapper)
    expect(wrapper.find('.course-picker-panel').exists()).toBe(true)
    expect(wrapper.find('.course-picker-panel').text()).toContain('选择课程')
    // 手输输入框存在
    expect(wrapper.find('.course-id-input').exists()).toBe(true)
    // 课程列表展示「课程名（ID: xxx）」
    const items = wrapper.findAll('.course-item').map((b) => b.text())
    expect(items).toEqual(['机器学习（ID: 10001）', '深度学习（ID: 10002）'])
  })

  it('点击课程项选中后关闭课程弹窗，展示框回填课程名与 ID', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)
    await openCourseModal(wrapper)

    await pickCourseItem(wrapper, 10001)
    expect(wrapper.find('.course-picker-panel').exists()).toBe(false)
    const pickerText = wrapper.find('.course-picker').text()
    expect(pickerText).toContain('机器学习')
    expect(pickerText).toContain('ID: 10001')
  })

  it('课程弹窗内手输课程 ID 并确认后关闭课程弹窗，展示框显示「课程 ID: n」', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)
    await openCourseModal(wrapper)

    await confirmManualCourse(wrapper, '20002')
    expect(wrapper.find('.course-picker-panel').exists()).toBe(false)
    expect(wrapper.find('.course-picker').text()).toContain('课程 ID: 20002')
  })

  it('点选课程后提交，payload 携带数字 course_id', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    examsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)

    await wrapper.find('input[placeholder="输入考试名称"]').setValue('期中考试')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10001)
    await submitCreate(wrapper)

    expect(examsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 10001 }))
  })

  it('手输课程 ID 后提交，payload 携带数字 course_id', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: [] } })
    examsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)

    await wrapper.find('input[placeholder="输入考试名称"]').setValue('期末考')
    await openCourseModal(wrapper)
    await confirmManualCourse(wrapper, '30003')
    await submitCreate(wrapper)

    expect(examsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 30003 }))
  })

  it('课程列表为空时弹窗显示「暂无课程」提示，仍可手输 ID 提交', async () => {
    coursesAPI.list.mockResolvedValue({ data: [] })
    examsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)

    await openCourseModal(wrapper)
    expect(wrapper.find('.course-picker-panel').text()).toContain('暂无课程')
    await wrapper.find('input[placeholder="输入考试名称"]').setValue('空课程考试')
    await confirmManualCourse(wrapper, '40004')
    await submitCreate(wrapper)

    expect(examsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 40004 }))
  })

  it('课程列表接口失败时不阻塞创建，仍可手输 ID 提交', async () => {
    coursesAPI.list.mockRejectedValue(new Error('network'))
    examsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)

    await wrapper.find('input[placeholder="输入考试名称"]').setValue('容错考试')
    await openCourseModal(wrapper)
    await confirmManualCourse(wrapper, '50005')
    await submitCreate(wrapper)

    expect(examsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 50005 }))
  })

  it('未选课程时确定按钮禁用，无法提交', async () => {
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    examsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateForm(wrapper)

    await wrapper.find('input[placeholder="输入考试名称"]').setValue('无课程考试')
    const confirmButton = wrapper.find('.create-form button.btn-primary')
    expect(confirmButton.attributes('disabled')).toBeDefined()
    await confirmButton.trigger('click')
    expect(examsAPI.create).not.toHaveBeenCalled()
  })
})
