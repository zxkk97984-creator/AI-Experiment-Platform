// 学生练习作答页：幂等提交、排队反馈、额度耗尽与权限不足
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

import PracticeProblemView from '../PracticeProblemView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/student/practice', component: { template: '<div class="center" />' } },
    { path: '/student/practice/problems/:id', component: PracticeProblemView },
  ],
})

const problemDetail = {
  id: 3,
  title: '统计列表中所有正数的和',
  difficulty: 'medium',
  tags: ['列表'],
  prompt: '返回列表中所有正数的和，空列表返回 0。',
  starter_code: 'def sum_positive(values):\n    return 0',
  status: 'published',
  grading_mode: 'deterministic',
  time_limit_ms: 2000,
  distinct_solvers: 4,
  my_best_score: 60,
  latest_submission_id: null,
  grading_standard_updated: false,
}

async function mountView() {
  router.push('/student/practice/problems/3')
  await router.isReady()
  return mount(PracticeProblemView, {
    global: {
      plugins: [router, createPinia()],
      stubs: { AppLayout: { template: '<div class="layout"><slot /></div>' } },
    },
  })
}

describe('PracticeProblemView', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.getProblem.mockResolvedValue({ data: problemDetail })
  })

  it('渲染题目、初始代码与我的最高分', async () => {
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('统计列表中所有正数的和')
    expect(wrapper.text()).toContain('中等')
    expect(wrapper.find('textarea').element.value).toContain('def sum_positive')
    expect(wrapper.text()).toContain('60')
  })

  it('提交携带 client_request_id 并展示排队名次', async () => {
    api.submit.mockResolvedValue({ data: { id: 21, grading_status: 'queued', queue_position: 2 } })
    api.getSubmission.mockResolvedValue({
      data: {
        id: 21,
        problem: { id: 3, title: problemDetail.title },
        code: 'def sum_positive(values):\n    return 0',
        grading_status: 'queued',
        grading_mode: 'deterministic',
        queue_position: 2,
        score: null,
        max_score: 100,
        deterministic: null,
        ai_result: null,
        ai_status: 'skipped',
        appeal: null,
        can_appeal: false,
      },
    })
    const wrapper = await mountView()
    await flushPromises()
    await wrapper.find('form.answer-form').trigger('submit')
    await flushPromises()

    expect(api.submit).toHaveBeenCalledTimes(1)
    const [problemId, code, requestId] = api.submit.mock.calls[0]
    expect(problemId).toBe('3')
    expect(code).toContain('def sum_positive')
    expect(typeof requestId).toBe('string')
    expect(requestId.length).toBeGreaterThan(8)
    expect(wrapper.text()).toContain('排队中（第 2 位）')
  })

  it('额度耗尽时展示「今日额度已用完」', async () => {
    api.submit.mockRejectedValue({ response: { status: 429, data: { detail: { code: 'QUOTA_EXHAUSTED', message: '今日额度已用完' } } } })
    const wrapper = await mountView()
    await flushPromises()
    await wrapper.find('form.answer-form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('今日额度已用完')
  })

  it('权限不足时不展示作答表单', async () => {
    api.getProblem.mockRejectedValue({ response: { status: 403, data: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('权限不足')
    expect(wrapper.find('form.answer-form').exists()).toBe(false)
  })

  it('题目不存在时展示空态并保留返回入口', async () => {
    api.getProblem.mockRejectedValue({ response: { status: 404, data: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('题目不存在或已下架')
  })
})
