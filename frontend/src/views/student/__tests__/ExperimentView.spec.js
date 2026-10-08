import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  listStudentCatalog: vi.fn(),
  push: vi.fn(),
}))

vi.mock('../../../api/experiments.js', () => ({
  experimentsAPI: { listStudentCatalog: mocks.listStudentCatalog },
}))

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useRouter: () => ({ push: mocks.push }),
  }
})

import ExperimentView from '../ExperimentView.vue'

const response = (overrides = {}) => ({
  data: {
    items: [
      { id: 1, name: 'Python 基础实验', learning_status: 'started', last_learning_at: '2026-08-09T08:30:00Z' },
      { id: 2, name: '数据分析入门', learning_status: 'not_started', last_learning_at: null },
      { id: 3, name: '模型评估', learning_status: 'submitted', last_learning_at: '2026-08-08T08:30:00Z' },
      { id: 4, name: '特征工程', learning_status: 'graded', last_learning_at: '2026-08-07T08:30:00Z' },
    ],
    total: 4,
    page: 1,
    page_size: 10,
    summary: { total: 4, not_started: 1, started: 1, submitted: 1, graded: 1 },
    ...overrides,
  },
})

function mountView() {
  return mount(ExperimentView, {
    global: {
      stubs: { AppLayout: { template: '<main><slot /></main>' } },
    },
  })
}

describe('学生实验目录页', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.listStudentCatalog.mockResolvedValue(response())
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('合并展示四态目录，且没有旧记录表、模块图标或进度列', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('实验模块')
    expect(wrapper.text()).toContain('Python 基础实验')
    expect(wrapper.text()).toContain('进行中')
    expect(wrapper.text()).toContain('未开始')
    expect(wrapper.text()).toContain('已提交')
    expect(wrapper.text()).toContain('已评分')
    expect(wrapper.text()).not.toContain('我的实验记录')
    expect(wrapper.text()).not.toContain('进度')
    expect(wrapper.find('.module-name svg').exists()).toBe(false)
    expect(wrapper.findAll('thead th').map((node) => node.text())).toEqual([
      '实验名称', '状态', '最近学习', '操作',
    ])
    expect(wrapper.findAll('.enter-button').map((node) => node.text())).toEqual([
      '继续实验', '开始实验', '继续实验', '查看实验',
    ])

    await wrapper.findAll('.enter-button')[1].trigger('click')
    expect(mocks.push).toHaveBeenCalledWith('/student/experiments/2')
  })

  it('状态、搜索和排序条件会查询服务端并回到第一页', async () => {
    vi.useFakeTimers()
    const wrapper = mountView()
    await flushPromises()

    const submittedTab = wrapper.findAll('[role="tab"]').find((node) => node.text().includes('已提交'))
    await submittedTab.trigger('click')
    await flushPromises()
    expect(mocks.listStudentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({
      status: 'submitted', page: 1,
    }))

    await wrapper.find('input[type="search"]').setValue('  NumPy  ')
    await vi.advanceTimersByTimeAsync(350)
    await flushPromises()
    expect(mocks.listStudentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({
      q: 'NumPy', status: 'submitted', page: 1,
    }))

    await wrapper.find('select').setValue('name_asc')
    await flushPromises()
    expect(mocks.listStudentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({
      sort: 'name_asc', page: 1,
    }))
  })

  it('分页按钮请求对应页，并提供加载失败重试', async () => {
    mocks.listStudentCatalog
      .mockResolvedValueOnce(response({ total: 21 }))
      .mockResolvedValueOnce(response({ total: 21, page: 2 }))
    const wrapper = mountView()
    await flushPromises()

    const pageTwo = wrapper.findAll('.page-number').find((node) => node.text() === '2')
    await pageTwo.trigger('click')
    await flushPromises()
    expect(mocks.listStudentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }))

    mocks.listStudentCatalog.mockRejectedValueOnce(new Error('offline'))
    await wrapper.find('.status-tabs button').trigger('click')
    // 第一个 tab 已经选中，不触发请求；切换到“进行中”。
    await wrapper.findAll('.status-tabs button')[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('实验模块加载失败')

    mocks.listStudentCatalog.mockResolvedValueOnce(response())
    await wrapper.find('.retry-button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Python 基础实验')
  })
})

it('opens the selected class instance when one module is released in multiple semesters', async () => {
  mocks.push.mockClear()
  mocks.listStudentCatalog.mockResolvedValue(response({ items: [
    { id: 8, offering_id: 12, name: '同一模块', learning_status: 'graded', term_name: '旧学期', class_name: '旧班' },
    { id: 8, offering_id: 15, name: '同一模块', learning_status: 'not_started', term_name: '新学期', class_name: '新班' },
  ], total: 2 }))
  const wrapper = mountView()
  await flushPromises()
  expect(wrapper.text()).toContain('旧学期 · 旧班')
  expect(wrapper.text()).toContain('新学期 · 新班')
  await wrapper.findAll('.enter-button')[1].trigger('click')
  expect(mocks.push).toHaveBeenCalledWith('/student/experiments/8?offering_id=15')
})
