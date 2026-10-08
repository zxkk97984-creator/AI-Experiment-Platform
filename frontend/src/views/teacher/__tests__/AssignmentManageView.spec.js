/** 作业管理页 AssignmentManageView 组件测试：列表加载、创建弹窗（基本信息 + 环境配置）、课程选择弹窗（点选/手输回填）、提交 payload */
/** Phase 4：环境选择——无可用环境禁止创建；payload 携带 environment_version_id 与 import 策略 */
/** 弹窗化改造：点「布置作业」直接弹出完整创建弹窗，确定后创建并跳转题目编辑页，取消不创建，失败保持弹窗打开 */
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
    currentRoute: { value: { path: '/teacher/assignments' } },
  })),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../../../api/assignments', () => ({
  assignmentsAPI: { list: vi.fn(), create: vi.fn(), update: vi.fn(), publish: vi.fn(), deleteAssignment: vi.fn(), unpublishAssignment: vi.fn() },
}))

vi.mock('../../../api/offerings', () => ({ offeringsAPI: { list: vi.fn() } }))

vi.mock('../../../api/courses', () => ({
  coursesAPI: { list: vi.fn() },
}))

vi.mock('../../../api/environments', () => ({
  environmentsAPI: { listAvailable: vi.fn() },
}))

vi.mock('../../../stores/app', () => ({
  useAppStore: () => ({ showToast: showToastMock }),
}))

import { assignmentsAPI } from '../../../api/assignments.js'
import { offeringsAPI } from '../../../api/offerings.js'
import { coursesAPI } from '../../../api/courses.js'
import { environmentsAPI } from '../../../api/environments.js'

const courses = [
  { id: 10001, title: '机器学习' },
  { id: 10002, title: '深度学习' },
]

// Phase 4：默认提供 basic 可用环境，供创建弹窗选择
const envOptions = [
  {
    profile_id: 1, environment_version_id: 11, slug: 'basic', display_name: 'Python 基础',
    version_number: 1, packages: [{ pip_name: 'pytest', locked_version: '8.3.4', import_names: ['pytest'] }],
    minimum_memory_mb: 256,
  },
]

async function mountPage() {
  const mod = await import('../AssignmentManageView.vue')
  return mount(mod.default, {
    global: {
      stubs: { AppLayout: { template: '<div><slot /></div>' } },
    },
  })
}

/** 点击「布置作业」打开创建弹窗（等待环境列表加载完成） */
async function openCreateModal(wrapper) {
  const btn = wrapper.findAll('button').find((b) => b.text().includes('布置作业'))
  expect(btn, '页面应有「布置作业」按钮').toBeDefined()
  await btn.trigger('click')
  await flushPromises()
}

/** 点击课程展示框打开课程选择弹窗 */
async function openCourseModal(wrapper) {
  const picker = wrapper.find('.course-picker')
  expect(picker, '创建弹窗内应有课程展示框').toBeDefined()
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

/** 在创建弹窗内点「确定」完成创建 */
async function submitCreate(wrapper) {
  const btn = wrapper.findAll('.create-modal button').find((b) => b.text().includes('确定'))
  expect(btn, '创建弹窗应有「确定」按钮').toBeDefined()
  await btn.trigger('click')
  await flushPromises()
}

describe('作业管理页 AssignmentManageView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    offeringsAPI.list.mockResolvedValue({ data: { items: [] } })
    pushMock.mockClear()
    environmentsAPI.listAvailable.mockResolvedValue({ data: envOptions })
  })

  it('加载并展示作业列表', async () => {
    assignmentsAPI.list.mockResolvedValue({
      data: { items: [{ id: 1, title: '作业一', status: 'draft', due_at: null }] },
    })
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('作业一')
  })

  it('页面加载时请求课程列表，打开创建弹窗后课程弹窗渲染「课程名（ID: xxx）」课程项', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await openCourseModal(wrapper)

    expect(coursesAPI.list).toHaveBeenCalled()
    const items = wrapper.findAll('.course-item').map((b) => b.text())
    expect(items).toEqual(['机器学习（ID: 10001）', '深度学习（ID: 10002）'])
  })

  it('点「布置作业」直接弹出创建弹窗（标题 + 全部表单字段 + 确定/取消按钮），未点确定不创建', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()

    expect(wrapper.find('.create-modal').exists()).toBe(false)
    await openCreateModal(wrapper)

    const modal = wrapper.find('.create-modal')
    expect(modal.exists()).toBe(true)
    expect(modal.text()).toContain('创建作业')
    // 全部表单字段：名称、描述、课程展示框、截止时间、运行环境、导入规则
    expect(modal.find('input[placeholder="输入作业名称"]').exists()).toBe(true)
    expect(modal.find('textarea[placeholder="作业描述（可选）"]').exists()).toBe(true)
    expect(modal.find('.course-picker').exists()).toBe(true)
    expect(modal.find('input[type="datetime-local"]').exists()).toBe(true)
    expect(modal.find('.env-picker-select').exists()).toBe(true)
    expect(modal.find('.import-policy-select').exists()).toBe(true)
    const btns = wrapper.findAll('.create-modal button').map((b) => b.text().trim())
    expect(btns).toContain('确定')
    expect(btns).toContain('取消')
    // 未点确定前不创建
    expect(assignmentsAPI.create).not.toHaveBeenCalled()
  })

  it('点击课程展示框弹出课程选择弹窗（标题 + 手输输入框）', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    expect(wrapper.find('.course-picker-panel').exists()).toBe(false)
    await openCourseModal(wrapper)
    expect(wrapper.find('.course-picker-panel').exists()).toBe(true)
    expect(wrapper.find('.course-picker-panel').text()).toContain('选择课程')
    expect(wrapper.find('.course-id-input').exists()).toBe(true)
  })

  it('点击课程项选中后关闭课程弹窗，展示框回填课程名与 ID', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await openCourseModal(wrapper)

    await pickCourseItem(wrapper, 10001)
    expect(wrapper.find('.course-picker-panel').exists()).toBe(false)
    const pickerText = wrapper.find('.course-picker').text()
    expect(pickerText).toContain('机器学习')
    expect(pickerText).toContain('ID: 10001')
  })

  it('课程弹窗内手输课程 ID 并确认后关闭课程弹窗，展示框显示「课程 ID: n」', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await openCourseModal(wrapper)

    await confirmManualCourse(wrapper, '20002')
    expect(wrapper.find('.course-picker-panel').exists()).toBe(false)
    expect(wrapper.find('.course-picker').text()).toContain('课程 ID: 20002')
  })

  it('选中课程后点「确定」提交，payload 携带数字 course_id', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    await wrapper.find('input[placeholder="输入作业名称"]').setValue('课程作业')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10001)
    await submitCreate(wrapper)

    expect(assignmentsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 10001 }))
  })

  it('课程弹窗手输 ID 后提交，payload 携带数字 course_id', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: [] } })
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    await wrapper.find('input[placeholder="输入作业名称"]').setValue('手输作业')
    await openCourseModal(wrapper)
    await confirmManualCourse(wrapper, '30003')
    await submitCreate(wrapper)

    expect(assignmentsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 30003 }))
  })

  it('课程列表为空时课程弹窗显示「暂无课程」提示，仍可手输 ID 提交', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: [] })
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    await openCourseModal(wrapper)
    expect(wrapper.find('.course-picker-panel').text()).toContain('暂无课程')
    await wrapper.find('input[placeholder="输入作业名称"]').setValue('空课程作业')
    await confirmManualCourse(wrapper, '40004')
    await submitCreate(wrapper)

    expect(assignmentsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 40004 }))
  })

  it('课程列表接口失败时不影响手输提交', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockRejectedValue(new Error('network'))
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    await wrapper.find('input[placeholder="输入作业名称"]').setValue('容错作业')
    await openCourseModal(wrapper)
    await confirmManualCourse(wrapper, '50005')
    await submitCreate(wrapper)

    expect(assignmentsAPI.create).toHaveBeenCalledWith(expect.objectContaining({ course_id: 50005 }))
  })

  // ── Phase 4：环境选择 ────────────────────────────────────────────

  it('提交 payload 携带默认环境版本与 import 策略', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    await wrapper.find('input[placeholder="输入作业名称"]').setValue('环境作业')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10001)
    await submitCreate(wrapper)

    // 无可用环境时默认选中第一项（basic）；unrestricted 时白名单为空数组
    expect(assignmentsAPI.create).toHaveBeenCalledWith(
      expect.objectContaining({
        course_id: 10001,
        environment_version_id: 11,
        import_policy_mode: 'unrestricted',
        allowed_imports: [],
      }),
    )
  })

  it('无可用环境时「确定」按钮禁用且弹窗内提示联系管理员', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    environmentsAPI.listAvailable.mockResolvedValue({ data: [] })
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    expect(wrapper.find('.create-modal').text()).toContain('暂无可用环境，请联系管理员')
    const okBtn = wrapper.findAll('.create-modal button').find((b) => b.text().includes('确定'))
    expect(okBtn?.attributes('disabled')).toBeDefined()
  })

  it('restricted 模式勾选白名单后 payload 携带白名单', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)

    // 切到限定白名单并勾选 pytest
    const policySelect = wrapper.find('.import-policy-select')
    await policySelect.setValue('restricted')
    await wrapper.find('.import-chip input').setValue(true)
    await wrapper.find('input[placeholder="输入作业名称"]').setValue('白名单作业')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10001)
    await submitCreate(wrapper)

    expect(assignmentsAPI.create).toHaveBeenCalledWith(
      expect.objectContaining({ import_policy_mode: 'restricted', allowed_imports: ['pytest'] }),
    )
  })

  // ── 创建弹窗（确定后创建并跳转题目编辑页） ─────────────────────────

  it('点「取消」关闭创建弹窗且不创建', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await wrapper.find('input[placeholder="输入作业名称"]').setValue('取消作业')

    await wrapper.findAll('.create-modal button').find((b) => b.text().includes('取消')).trigger('click')
    await flushPromises()

    expect(wrapper.find('.create-modal').exists()).toBe(false)
    expect(assignmentsAPI.create).not.toHaveBeenCalled()
  })

  it('点「确定」后创建成功并跳转题目编辑页，弹窗关闭', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockResolvedValue({ data: { id: 88 } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await wrapper.find('input[placeholder="输入作业名称"]').setValue('跳转作业')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10001)
    await submitCreate(wrapper)

    expect(assignmentsAPI.create).toHaveBeenCalledWith(
      expect.objectContaining({ title: '跳转作业', course_id: 10001, environment_version_id: 11 }),
    )
    expect(pushMock).toHaveBeenCalledWith('/teacher/assignments/88/edit')
    // 创建成功后弹窗关闭
    expect(wrapper.find('.create-modal').exists()).toBe(false)
  })

  it('在创建弹窗内切换环境后提交，payload 使用所选环境', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockResolvedValue({ data: { id: 89 } })
    // 提供两个环境档位供切换
    environmentsAPI.listAvailable.mockResolvedValue({
      data: [
        ...envOptions,
        {
          profile_id: 2, environment_version_id: 22, slug: 'data', display_name: '数据分析',
          version_number: 1, packages: [], minimum_memory_mb: 768,
        },
      ],
    })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await wrapper.find('input[placeholder="输入作业名称"]').setValue('切换环境作业')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10002)

    // 弹窗内直接切换环境（EnvironmentProfilePicker 下拉）
    await wrapper.find('.env-picker-select').setValue(22)
    await submitCreate(wrapper)

    expect(assignmentsAPI.create).toHaveBeenCalledWith(
      expect.objectContaining({ course_id: 10002, environment_version_id: 22 }),
    )
    expect(pushMock).toHaveBeenCalledWith('/teacher/assignments/89/edit')
  })

  it('未填截止时间时 payload 的 due_at 归一化为 null（避免空字符串 422）', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockResolvedValue({ data: { id: 90 } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await wrapper.find('input[placeholder="输入作业名称"]').setValue('无截止作业')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10001)
    await submitCreate(wrapper)

    // 表单 due_at 默认为空字符串，提交时应为 null 而不是 ''
    expect(assignmentsAPI.create).toHaveBeenCalledWith(
      expect.objectContaining({ due_at: null }),
    )
    expect(assignmentsAPI.create.mock.calls[0][0].due_at).not.toBe('')
    expect(pushMock).toHaveBeenCalledWith('/teacher/assignments/90/edit')
  })

  it('创建失败时提示错误且弹窗保持打开', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
    coursesAPI.list.mockResolvedValue({ data: { items: courses } })
    assignmentsAPI.create.mockRejectedValue({ response: { data: { detail: { message: '后端拒绝' } } } })
    const wrapper = await mountPage()
    await flushPromises()
    await openCreateModal(wrapper)
    await wrapper.find('input[placeholder="输入作业名称"]').setValue('失败作业')
    await openCourseModal(wrapper)
    await pickCourseItem(wrapper, 10001)
    await submitCreate(wrapper)

    expect(showToastMock).toHaveBeenCalledWith('后端拒绝', 'error')
    expect(wrapper.find('.create-modal').exists()).toBe(true)
  })

  // ── 统一管理：删除草稿 + 取消发布 ───────────────────────────────

  const mixed = [
    { id: 1, title: '草稿作业', status: 'draft', due_at: null },
    {
      id: 2,
      title: '已发布作业',
      status: 'published',
      published_at: '2026-08-01T01:00:00Z',
      due_at: '2099-12-01T12:00:00Z',
    },
  ]

  function rowButtons(wrapper, rowIndex) {
    return wrapper.findAll('tbody tr')[rowIndex].findAll('button')
  }

  function clickButton(buttons, text) {
    const btn = buttons.find((b) => b.text() === text)
    expect(btn, `行内应有「${text}」按钮`).toBeDefined()
    return btn.trigger('click')
  }

  it('draft 行显示删除按钮、published 行显示取消发布按钮', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    const wrapper = await mountPage()
    await flushPromises()

    const draftText = wrapper.findAll('tbody tr')[0].text()
    expect(draftText).toContain('删除')
    expect(draftText).not.toContain('取消发布')
    const publishedText = wrapper.findAll('tbody tr')[1].text()
    expect(publishedText).toContain('取消发布')
    expect(publishedText).not.toContain('删除')
  })

  it('列表同时展示首次发布时间与截止时间，并为每行提供调整入口', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    const wrapper = await mountPage()
    await flushPromises()

    const draftText = wrapper.findAll('tbody tr')[0].text()
    expect(draftText).toContain('未发布')
    expect(draftText).toContain('长期开放')
    const publishedText = wrapper.findAll('tbody tr')[1].text()
    expect(publishedText).toContain('发布')
    expect(publishedText).toContain('截止')
    expect(publishedText).toContain('调整截止')
  })

  it('延后已发布作业截止时间可直接保存，并转换为带时区 ISO 时间', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    assignmentsAPI.update.mockResolvedValue({ data: mixed[1] })
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 1), '调整截止')
    expect(wrapper.find('.schedule-modal').text()).toContain('首次发布时间')
    await wrapper.find('#schedule-due-at').setValue('2099-12-20T20:30')
    await clickButton(wrapper.findAll('.schedule-modal button'), '保存时间设置')
    await flushPromises()

    expect(assignmentsAPI.update).toHaveBeenCalledWith(2, {
      due_at: new Date('2099-12-20T20:30').toISOString(),
    })
    expect(showToastMock).toHaveBeenCalledWith('截止时间已更新', 'success')
  })

  it('提前已发布作业截止时间时先展示影响确认，再执行保存', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    assignmentsAPI.update.mockResolvedValue({ data: mixed[1] })
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 1), '调整截止')
    await wrapper.find('#schedule-due-at').setValue('2099-11-01T20:30')
    await clickButton(wrapper.findAll('.schedule-modal button'), '保存时间设置')
    await flushPromises()

    expect(assignmentsAPI.update).not.toHaveBeenCalled()
    expect(wrapper.find('.confirm-panel').text()).toContain('缩短学生作答时间')
    await clickButton(wrapper.findAll('.confirm-panel button'), '确认保存')
    await flushPromises()
    expect(assignmentsAPI.update).toHaveBeenCalledWith(2, {
      due_at: new Date('2099-11-01T20:30').toISOString(),
    })
  })

  it('发布状态与时限状态独立筛选，已发布数量包含已截止作业', async () => {
    const rows = [
      { id: 1, title: '草稿作业', status: 'draft', due_at: null },
      { id: 2, title: '已截止作业', status: 'published', due_at: '2020-01-01T00:00:00Z' },
      { id: 3, title: '进行中作业', status: 'published', due_at: '2099-01-01T00:00:00Z' },
    ]
    assignmentsAPI.list.mockResolvedValue({ data: { items: rows } })
    const wrapper = await mountPage()
    await flushPromises()

    expect(wrapper.findAll('.teacher-metric-card')[1].text()).toContain('2')
    expect(wrapper.findAll('.teacher-metric-card')[3].text()).toContain('1')
    await wrapper.find('select[aria-label="时限状态筛选"]').setValue('ended')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(wrapper.find('tbody').text()).toContain('已截止作业')
  })

  it('点击「删除」弹出确认框，取消不调用删除接口', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 0), '删除')
    expect(wrapper.find('.confirm-panel').text()).toContain('确定删除作业「草稿作业」？')
    await clickButton(wrapper.findAll('.confirm-panel button'), '取消')
    await flushPromises()

    expect(assignmentsAPI.deleteAssignment).not.toHaveBeenCalled()
    expect(wrapper.find('.confirm-panel').exists()).toBe(false)
  })

  it('确认删除后调用 deleteAssignment 并刷新列表', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    assignmentsAPI.deleteAssignment.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 0), '删除')
    await clickButton(wrapper.findAll('.confirm-panel button'), '确认删除')
    await flushPromises()

    expect(assignmentsAPI.deleteAssignment).toHaveBeenCalledWith(1)
    expect(showToastMock).toHaveBeenCalledWith('作业已删除', 'success')
    expect(assignmentsAPI.list).toHaveBeenCalledTimes(2) // 初始加载 + 删除后刷新
    expect(wrapper.find('.confirm-panel').exists()).toBe(false)
  })

  it('删除失败时 toast 后端 message 且确认框保持打开', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    assignmentsAPI.deleteAssignment.mockRejectedValue({
      response: { data: { detail: { message: '仅草稿作业可删除' } } },
    })
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 0), '删除')
    await clickButton(wrapper.findAll('.confirm-panel button'), '确认删除')
    await flushPromises()

    expect(showToastMock).toHaveBeenCalledWith('仅草稿作业可删除', 'error')
    expect(wrapper.find('.confirm-panel').exists()).toBe(true)
  })

  it('点击「取消发布」弹出确认框，取消不请求', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 1), '取消发布')
    expect(wrapper.find('.confirm-panel').text()).toContain('确定取消发布？')
    await clickButton(wrapper.findAll('.confirm-panel button'), '取消')
    await flushPromises()

    expect(assignmentsAPI.unpublishAssignment).not.toHaveBeenCalled()
    expect(wrapper.find('.confirm-panel').exists()).toBe(false)
  })

  it('确认取消发布后调用 unpublishAssignment 并刷新列表', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    assignmentsAPI.unpublishAssignment.mockResolvedValue({ data: { id: 2, status: 'draft' } })
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 1), '取消发布')
    await clickButton(wrapper.findAll('.confirm-panel button'), '确认取消发布')
    await flushPromises()

    expect(assignmentsAPI.unpublishAssignment).toHaveBeenCalledWith(2)
    expect(showToastMock).toHaveBeenCalledWith('已取消发布', 'success')
    expect(assignmentsAPI.list).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.confirm-panel').exists()).toBe(false)
  })

  it('取消发布失败时 toast 后端 message 且确认框保持打开', async () => {
    assignmentsAPI.list.mockResolvedValue({ data: { items: mixed } })
    assignmentsAPI.unpublishAssignment.mockRejectedValue({
      response: { data: { detail: { message: '仅已发布的作业可取消发布' } } },
    })
    const wrapper = await mountPage()
    await flushPromises()

    await clickButton(rowButtons(wrapper, 1), '取消发布')
    await clickButton(wrapper.findAll('.confirm-panel button'), '确认取消发布')
    await flushPromises()

    expect(showToastMock).toHaveBeenCalledWith('仅已发布的作业可取消发布', 'error')
    expect(wrapper.find('.confirm-panel').exists()).toBe(true)
  })
})


it('binds a new assignment to the selected class offering', async () => {
  setActivePinia(createPinia())
  assignmentsAPI.list.mockResolvedValue({ data: { items: [] } })
  assignmentsAPI.create.mockResolvedValue({ data: { id: 9 } })
  coursesAPI.list.mockResolvedValue({ data: { items: courses } })
  environmentsAPI.listAvailable.mockResolvedValue({ data: envOptions })
  offeringsAPI.list.mockResolvedValue({ data: { items: [
    { id: 5, course_id: 10001, status: 'active', term_name: '秋季', class_name: '一班' },
    { id: 6, course_id: 10002, status: 'active', term_name: '秋季', class_name: '另一个课程' },
  ] } })
  const wrapper = await mountPage()
  await flushPromises()
  await openCreateModal(wrapper)
  await wrapper.find('input[placeholder="输入作业名称"]').setValue('本班作业')
  await openCourseModal(wrapper)
  await pickCourseItem(wrapper, 10001)
  expect(wrapper.get('#assignment-offering').text()).not.toContain('另一个课程')
  await wrapper.get('#assignment-offering').setValue('5')
  await submitCreate(wrapper)
  expect(assignmentsAPI.create).toHaveBeenLastCalledWith(expect.objectContaining({ course_id: 10001, offering_id: 5 }))
})
