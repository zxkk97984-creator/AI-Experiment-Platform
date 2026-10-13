// 教师只读练习排行榜：范围/榜类型切换参数、空态、权限不足与只读约束
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'

const api = vi.hoisted(() => ({ leaderboard: vi.fn(), setOptIn: vi.fn(), getOverview: vi.fn(), getOptIn: vi.fn(), recalculate: vi.fn() }))
const offerings = vi.hoisted(() => ({ list: vi.fn() }))

// 批次 6 起教师页复用学生的榜单组件，走 practiceSocialAPI（快照口径 + opt-in 状态）
vi.mock('../../../api/practice.js', () => ({ practiceAPI: {}, practiceSocialAPI: api, adminPracticeAPI: {} }))
vi.mock('../../../api/offerings.js', () => ({ offeringsAPI: offerings }))

import PracticeLeaderboardView from '../PracticeLeaderboardView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/teacher/practice/leaderboard', component: PracticeLeaderboardView },
  ],
})

const rows = [
  { rank: 1, display_name: '勤勉的海豚', solved_count: 12, total_score: 960, avg_score: 80, is_me: false, achieved_at: '2026-10-13T01:00:00Z' },
  { rank: 2, display_name: '专注的海獭', solved_count: 9, total_score: 720, avg_score: 80, is_me: false, achieved_at: '2026-10-12T01:00:00Z' },
]

async function mountView() {
  router.push('/teacher/practice/leaderboard')
  await router.isReady()
  return mount(PracticeLeaderboardView, {
    global: {
      plugins: [router, createPinia()],
      stubs: { AppLayout: { template: '<div class="layout"><slot /></div>' } },
    },
  })
}

describe('PracticeLeaderboardView', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.leaderboard.mockResolvedValue({ data: { items: rows, total: 2, scope: 'platform', board: 'deterministic' } })
    offerings.list.mockResolvedValue({ data: { items: [{ id: 3, course_title: 'Python 程序设计' }], total: 1 } })
  })

  it('默认展示全平台普通测试榜且无写操作入口', async () => {
    const wrapper = await mountView()
    await flushPromises()
    expect(api.leaderboard).toHaveBeenCalledWith({ scope: 'platform', scope_id: undefined, board: 'deterministic', view: 'top' })
    expect(wrapper.text()).toContain('勤勉的海豚')
    expect(wrapper.text()).toContain('普通测试榜')
    expect(wrapper.text()).not.toContain('发布')
    expect(wrapper.text()).not.toContain('删除')
  })

  it('切换到 AI 综合榜后按 board=ai 重新拉取', async () => {
    api.leaderboard.mockResolvedValue({ data: { items: rows, total: 2, scope: 'platform', board: 'ai' } })
    const wrapper = await mountView()
    await flushPromises()
    const aiBtn = wrapper.findAll('button').find((btn) => btn.text() === 'AI 综合榜')
    await aiBtn.trigger('click')
    await flushPromises()
    expect(api.leaderboard).toHaveBeenLastCalledWith({ scope: 'platform', scope_id: undefined, board: 'ai', view: 'top' })
  })

  it('切到班内榜会带上班级 id', async () => {
    const wrapper = await mountView()
    await flushPromises()
    const offeringBtn = wrapper.findAll('button').find((btn) => btn.text() === '班内榜')
    await offeringBtn.trigger('click')
    await flushPromises()
    // 只有一个教学班时自动选中并请求
    expect(api.leaderboard).toHaveBeenLastCalledWith({ scope: 'offering', scope_id: 3, board: 'deterministic', view: 'top' })
  })

  it('还没有人完成时展示空态', async () => {
    api.leaderboard.mockResolvedValue({ data: { items: [], total: 0, scope: 'platform', board: 'deterministic' } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('还没有人完成练习')
  })

  it('权限不足时只展示提示，不渲染榜单', async () => {
    api.leaderboard.mockRejectedValue({ response: { status: 403, data: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('权限不足')
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('额度耗尽与队列积压各自提示，不冒充完整榜单', async () => {
    api.leaderboard.mockResolvedValue({
      data: { items: rows, total: 2, scope: 'platform', board: 'ai', meta: { quota_exhausted: true, queue: { queued: 6 } } },
    })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('今日额度已用完')
    expect(wrapper.text()).toContain('练习判题队列排队 6 项')
  })
})
