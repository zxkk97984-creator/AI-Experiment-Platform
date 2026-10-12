// 学生练习中心：加载 / 空 / 失败 / 权限不足 / 额度耗尽 / 排队六态 + 筛选参数
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'

const api = vi.hoisted(() => ({
  listProblems: vi.fn(),
  getProblem: vi.fn(),
  submit: vi.fn(),
  getSubmission: vi.fn(),
  getMine: vi.fn(),
  leaderboard: vi.fn(),
}))

vi.mock('../../../api/practice.js', () => ({ practiceAPI: api, adminPracticeAPI: {} }))

import PracticeCenterView from '../PracticeCenterView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/student/practice', component: PracticeCenterView },
    { path: '/student/practice/mine', component: { template: '<div class="mine" />' } },
    { path: '/student/practice/problems/:id', component: { template: '<div class="problem" />' } },
    { path: '/student/practice/submissions/:id', component: { template: '<div class="submission" />' } },
  ],
})

function problem(overrides = {}) {
  return {
    id: 3,
    title: '统计列表中所有正数的和',
    difficulty: 'easy',
    tags: ['列表'],
    prompt: '返回列表中所有正数的和。',
    status: 'published',
    grading_mode: 'deterministic',
    time_limit_ms: 2000,
    distinct_solvers: 4,
    my_status: 'not_started',
    my_best_score: null,
    ...overrides,
  }
}

async function mountView(path = '/student/practice') {
  router.push(path)
  await router.isReady()
  return mount(PracticeCenterView, {
    global: {
      plugins: [router, createPinia()],
      stubs: { AppLayout: { template: '<div class="layout"><slot /></div>' } },
    },
  })
}

describe('PracticeCenterView', () => {
  beforeEach(() => vi.resetAllMocks())

  it('加载成功后渲染题目卡片与数量', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [problem()], total: 1 } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('共 1 道题')
    expect(wrapper.text()).toContain('统计列表中所有正数的和')
    expect(api.listProblems).toHaveBeenCalledWith(expect.objectContaining({ page: 1, page_size: 12 }))
  })

  it('空数据显示题库空态而不是错误态', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [], total: 0 } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('题库暂时没有已发布的题目')
  })

  it('请求失败展示重试并提示', async () => {
    api.listProblems.mockRejectedValue({ response: { status: 500, data: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('加载失败')
  })

  it('403 展示权限不足而不是加载失败', async () => {
    api.listProblems.mockRejectedValue({ response: { status: 403, data: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('权限不足')
  })

  it('额度耗尽时展示「今日额度已用完」', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [problem()], total: 1, meta: { quota_exhausted: true } } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('今日额度已用完')
  })

  it('在途提交展示排队名次并可跳转反馈页', async () => {
    api.listProblems.mockResolvedValue({
      data: {
        items: [problem()],
        total: 1,
        meta: { active_submissions: [{ id: 21, problem_id: 3, problem_title: '统计列表中所有正数的和', grading_status: 'queued', queue_position: 2 }] },
      },
    })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('排队中（第 2 位）')
    expect(wrapper.find('a[href="/student/practice/submissions/21"]').exists()).toBe(true)
  })

  it('点击卡片进入作答页', async () => {
    api.listProblems.mockResolvedValue({ data: { items: [problem()], total: 1 } })
    const wrapper = await mountView()
    await flushPromises()
    await wrapper.find('.problem-card button').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/student/practice/problems/3')
  })
})
