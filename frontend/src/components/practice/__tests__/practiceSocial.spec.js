// 批次 6：徽章墙 / 打卡日历 / 成长曲线（窄屏降级）/ 榜单组件的 opt-in 与只读行为
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

const social = vi.hoisted(() => ({
  leaderboard: vi.fn(),
  setOptIn: vi.fn(),
  getOverview: vi.fn(),
  getOptIn: vi.fn(),
  recalculate: vi.fn(),
}))
vi.mock('../../../api/practice.js', () => ({ practiceAPI: {}, practiceSocialAPI: social }))

const offerings = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('../../../api/offerings.js', () => ({ offeringsAPI: offerings }))

import PracticeBadgeWall from '../PracticeBadgeWall.vue'
import PracticeCheckinCalendar from '../PracticeCheckinCalendar.vue'
import PracticeGrowthChart from '../PracticeGrowthChart.vue'
import PracticeScopeBoard from '../PracticeScopeBoard.vue'

const BADGES = [
  { key: 'first_solved', label: '首题通关', description: '通过第一道练习题', threshold: 1, progress: 1, earned: true, awarded_at: '2026-10-13T02:00:00Z' },
  { key: 'solved_10', label: '十题达成', description: '累计通过 10 道练习题', threshold: 10, progress: 3, earned: false, awarded_at: null },
]

function boardPayload(overrides = {}) {
  return {
    board: 'deterministic',
    scope: 'platform',
    scope_id: 0,
    total: 2,
    truncated: false,
    anonymized: true,
    opt_in: false,
    items: [
      { rank: 1, display_name: '同学 AAAA11', solved_count: 3, total_score: 250, avg_score: 83.33, achieved_at: '2026-10-13T02:00:00Z', is_me: false },
      { rank: 1, display_name: '同学 BBBB22', solved_count: 3, total_score: 250, avg_score: 83.33, achieved_at: '2026-10-13T02:00:00Z', is_me: true },
    ],
    me: null,
    window: null,
    generated_at: '2026-10-13T03:00:00Z',
    recalculated_at: '2026-10-13T03:00:00Z',
    ...overrides,
  }
}

describe('PracticeBadgeWall', () => {
  it('已获得显示获得时间，未获得显示条件与进度', () => {
    const wrapper = mount(PracticeBadgeWall, { props: { badges: BADGES } })
    const items = wrapper.findAll('.badge')
    expect(items).toHaveLength(2)
    expect(items[0].classes()).toContain('earned')
    expect(items[0].text()).toContain('已获得')
    expect(items[1].classes()).not.toContain('earned')
    expect(items[1].text()).toContain('累计通过 10 道练习题')
    expect(items[1].text()).toContain('3/10')
  })

  it('没有徽章数据时给出空态而不是报错', () => {
    const wrapper = mount(PracticeBadgeWall, { props: { badges: [] } })
    expect(wrapper.text()).toContain('还没有徽章数据')
  })
})

describe('PracticeCheckinCalendar', () => {
  it('只高亮有新增通关的日期，并带可读的 aria-label', () => {
    const wrapper = mount(PracticeCheckinCalendar, {
      props: {
        activeDates: ['2026-10-12', '2026-10-13'],
        days: 5,
        streakDays: 2,
        today: '2026-10-13',
      },
    })
    const cells = wrapper.findAll('.day')
    expect(cells).toHaveLength(5)
    expect(wrapper.findAll('.day.active')).toHaveLength(2)
    expect(cells[4].attributes('aria-label')).toBe('2026-10-13 已打卡')
    expect(cells[0].attributes('aria-label')).toBe('2026-10-09 未打卡')
    expect(wrapper.text()).toContain('连续 2 天')
  })
})

describe('PracticeGrowthChart', () => {
  const points = [
    { date: '2026-10-12', passed_count: 1, submissions: 2, avg_score: 80 },
    { date: '2026-10-13', passed_count: 2, submissions: 3, avg_score: 90 },
  ]

  it('宽屏渲染折线图（含两条序列）', () => {
    const wrapper = mount(PracticeGrowthChart, { props: { points, narrow: false } })
    expect(wrapper.findAll('polyline')).toHaveLength(2)
    expect(wrapper.find('.growth-list').exists()).toBe(false)
  })

  it('窄屏降级为逐日列表，不渲染 svg', () => {
    const wrapper = mount(PracticeGrowthChart, { props: { points, narrow: true } })
    expect(wrapper.find('svg').exists()).toBe(false)
    const rows = wrapper.findAll('.growth-row')
    expect(rows).toHaveLength(2)
    expect(rows[1].text()).toContain('通过 2 题')
    expect(rows[1].text()).toContain('平均分 90')
  })

  it('没有数据时给出空态', () => {
    const wrapper = mount(PracticeGrowthChart, { props: { points: [] } })
    expect(wrapper.text()).toContain('近 30 天还没有练习记录')
  })
})

describe('PracticeScopeBoard', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    offerings.list.mockResolvedValue({ data: { items: [] } })
    social.leaderboard.mockResolvedValue({ data: boardPayload() })
  })

  function mountBoard(props = {}) {
    return mount(PracticeScopeBoard, {
      props,
      global: { plugins: [createPinia()] },
    })
  }

  it('学生视角：渲染并列名次，未 opt-in 时给出提示与开关', async () => {
    const wrapper = mountBoard({ showOptIn: true })
    await flushPromises()
    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].find('.rank').text()).toBe('1')
    expect(rows[1].text()).toContain('我')
    expect(wrapper.text()).toContain('你尚未加入全平台榜')
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(true)
  })

  it('教师只读视角：不渲染 opt-in 开关与任何写操作', async () => {
    social.leaderboard.mockResolvedValue({
      data: boardPayload({ opt_in: null, items: boardPayload().items }),
    })
    const wrapper = mountBoard({ readonly: true })
    await flushPromises()
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    expect(wrapper.find('form').exists()).toBe(true)
  })

  it('空榜给出「还没有人完成练习」空态', async () => {
    social.leaderboard.mockResolvedValue({
      data: boardPayload({ items: [], total: 0, me: null }),
    })
    const wrapper = mountBoard({ showOptIn: true })
    await flushPromises()
    expect(wrapper.text()).toContain('还没有人完成练习')
  })

  it('切到「只看我的名次」时请求 view=me 并渲染窗口行', async () => {
    social.leaderboard.mockResolvedValue({
      data: boardPayload({
        window: [boardPayload().items[1]],
        me: { ...boardPayload().items[1], is_me: true },
        opt_in: true,
      }),
    })
    const wrapper = mountBoard({ showOptIn: true })
    await flushPromises()
    const buttons = wrapper.findAll('button').filter((btn) => btn.text() === '只看我的名次')
    await buttons[0].trigger('click')
    await flushPromises()
    expect(social.leaderboard).toHaveBeenLastCalledWith(
      expect.objectContaining({ view: 'me', scope: 'platform' }))
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
  })
})
