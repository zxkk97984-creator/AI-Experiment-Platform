// 管理员题库列表：列表渲染、发布确认、权限不足与判题队列积压提示
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'

const api = vi.hoisted(() => ({
  listProblems: vi.fn(),
  getProblem: vi.fn(),
  createProblem: vi.fn(),
  updateProblem: vi.fn(),
  publishProblem: vi.fn(),
  archiveProblem: vi.fn(),
  deleteProblem: vi.fn(),
  copyFromAssignment: vi.fn(),
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  getAiPreflight: vi.fn(),
  getUsage: vi.fn(),
  listAppeals: vi.fn(),
  handleAppeal: vi.fn(),
}))
const assignments = vi.hoisted(() => ({ list: vi.fn(), getQuestions: vi.fn() }))

vi.mock('../../../api/practice.js', () => ({ practiceAPI: {}, adminPracticeAPI: api }))
vi.mock('../../../api/assignments.js', () => ({ assignmentsAPI: assignments }))

import PracticeProblemsView from '../PracticeProblemsView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/admin/practice/problems', component: PracticeProblemsView },
    { path: '/admin/practice/problems/new', component: { template: '<div class="new" />' } },
    { path: '/admin/practice/problems/:id/edit', component: { template: '<div class="edit" />' } },
  ],
})

const problemRow = {
  id: 4,
  title: '统计列表中所有正数的和',
  difficulty: 'easy',
  tags: ['列表'],
  status: 'draft',
  grading_mode: 'deterministic',
  total_attempts: 0,
  distinct_solvers: 0,
}

async function mountView() {
  router.push('/admin/practice/problems')
  await router.isReady()
  return mount(PracticeProblemsView, {
    global: {
      plugins: [router, createPinia()],
      stubs: { AppLayout: { template: '<div class="layout"><slot /></div>' } },
    },
  })
}

describe('PracticeProblemsView', () => {
  beforeEach(() => vi.resetAllMocks())

  it('渲染题库概览与题目行', async () => {
    api.listProblems.mockResolvedValue({
      data: { items: [problemRow], total: 1, summary: { total: 1, draft: 1, published: 0, archived: 0 } },
    })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('统计列表中所有正数的和')
    expect(wrapper.text()).toContain('草稿')
    expect(api.listProblems).toHaveBeenCalledWith(expect.objectContaining({ page: 1, page_size: 10 }))
  })

  it('空题库展示录入引导', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [], total: 0, summary: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('题库还没有题目')
  })

  it('判题队列积压展示排队提示', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [problemRow], total: 1, summary: {}, meta: { queue: { queued: 5, running: 2 } } } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('判题队列积压 5 项')
  })

  it('权限不足时隐藏列表操作', async () => {
    api.listProblems.mockRejectedValue({ response: { status: 403, data: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('权限不足')
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('发布需要在确认弹窗中二次确认后才调用接口', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [problemRow], total: 1, summary: {} } })
    api.publishProblem.mockResolvedValue({ data: { ...problemRow, status: 'published' } })
    const wrapper = await mountView()
    await flushPromises()
    const publishBtn = wrapper.findAll('button').find((btn) => btn.text() === '发布')
    await publishBtn.trigger('click')
    expect(api.publishProblem).not.toHaveBeenCalled()
    const confirmBtn = wrapper.findAll('button').find((btn) => btn.text() === '确认')
    await confirmBtn.trigger('click')
    await flushPromises()
    expect(api.publishProblem).toHaveBeenCalledWith(4)
    expect(api.listProblems).toHaveBeenCalledTimes(2)
  })

  it('额度耗尽时提示 AI 生成任务需要等待额度', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [problemRow], total: 1, summary: {}, meta: { quota_exhausted: true } } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('今日额度已用完')
  })
})
