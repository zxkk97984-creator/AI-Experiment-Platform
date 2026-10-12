// 我的练习：统计卡、提交历史、空态与排队中的提交
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'

const api = vi.hoisted(() => ({ getMine: vi.fn() }))
vi.mock('../../../api/practice.js', () => ({ practiceAPI: api, adminPracticeAPI: {} }))

import PracticeMineView from '../PracticeMineView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/student/practice/mine', component: PracticeMineView },
    { path: '/student/practice', component: { template: '<div class="center" />' } },
    { path: '/student/practice/submissions/:id', component: { template: '<div class="submission" />' } },
  ],
})

const historyRow = {
  id: 21,
  problem_id: 3,
  problem_title: '统计列表中所有正数的和',
  difficulty: 'easy',
  score: 100,
  max_score: 100,
  grading_status: 'completed',
  grading_mode: 'deterministic',
  created_at: '2026-10-13T02:00:00Z',
}

async function mountView() {
  router.push('/student/practice/mine')
  await router.isReady()
  return mount(PracticeMineView, {
    global: {
      plugins: [router, createPinia()],
      stubs: { AppLayout: { template: '<div class="layout"><slot /></div>' } },
    },
  })
}

describe('PracticeMineView', () => {
  beforeEach(() => vi.resetAllMocks())

  it('渲染统计与提交历史', async () => {
    api.getMine.mockResolvedValue({
      data: {
        summary: { solved_count: 3, total_score: 270, avg_score: 90, accuracy: 75, submission_count: 5, daily_submission_cap: 20, daily_submissions_used: 4 },
        items: [historyRow],
        total: 1,
      },
    })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('已通过题目')
    expect(wrapper.text()).toContain('统计列表中所有正数的和')
    expect(wrapper.text()).toContain('100 / 100')
    expect(wrapper.text()).toContain('今日已提交 4 / 20 次')
  })

  it('没有记录时展示空态引导', async () => {
    api.getMine.mockResolvedValue({ data: { summary: {}, items: [], total: 0 } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('还没有练习记录')
  })

  it('排队中的提交展示名次并可跳转反馈页', async () => {
    api.getMine.mockResolvedValue({
      data: {
        summary: { solved_count: 0 },
        items: [{ ...historyRow, id: 22, grading_status: 'queued', queue_position: 3, score: null }],
        total: 1,
      },
    })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('排队中（第 3 位）')
    expect(wrapper.find('a[href="/student/practice/submissions/22"]').exists()).toBe(true)
  })

  it('额度耗尽时提示今日额度已用完', async () => {
    api.getMine.mockResolvedValue({ data: { summary: {}, items: [historyRow], total: 1, meta: { quota_exhausted: true } } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('今日额度已用完')
  })
})
