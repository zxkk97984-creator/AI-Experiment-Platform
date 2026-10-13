// 批次 6 练习榜单/激励 E2E fixture。
//
// 先调 layoutFixture 的通用桩（认证 / 侧栏 / 课程等），再叠加本批新增的
// /practice/social/** 与 /practice/me、/offerings 路由：Playwright 后注册的路由优先，
// 因此无需修改共享的 e2e/fixtures/layout.js。
import { layoutFixture } from './layout.js'

export const boardRows = [
  { rank: 1, display_name: '同学 7A3F21', solved_count: 4, total_score: 400, avg_score: 100, achieved_at: '2026-10-13T02:00:00Z', is_me: false },
  { rank: 2, display_name: '同学 B91C04', solved_count: 4, total_score: 350, avg_score: 87.5, achieved_at: '2026-10-13T02:30:00Z', is_me: false },
  // 并列名次：竞争式 rank 下两行同为 2
  { rank: 2, display_name: '同学 C0D5E7', solved_count: 4, total_score: 350, avg_score: 87.5, achieved_at: '2026-10-13T03:00:00Z', is_me: true },
  { rank: 4, display_name: '同学 E2F8AA', solved_count: 2, total_score: 200, avg_score: 66.67, achieved_at: '2026-10-13T04:00:00Z', is_me: false },
]

export const overviewPayload = (overrides = {}) => ({
  summary: {
    solved_count: 4,
    total_score: 350,
    avg_score: 87.5,
    accuracy: 0.8,
    submission_count: 9,
    streak_days: 3,
    last_active_date: '2026-10-13',
    daily_submissions_used: 4,
    daily_submission_cap: 20,
  },
  boards: {
    deterministic: { solved_count: 4, total_score: 350, avg_score: 87.5, accuracy: 0.8 },
    ai: { solved_count: 1, total_score: 72, avg_score: 72, accuracy: 1 },
  },
  badges: [
    { key: 'first_solved', label: '首题通关', description: '通过第一道练习题', threshold: 1, metric: 'solved_count', progress: 1, earned: true, awarded_at: '2026-10-01T02:00:00Z' },
    { key: 'solved_10', label: '十题达成', description: '累计通过 10 道练习题', threshold: 10, metric: 'solved_count', progress: 4, earned: false, awarded_at: null },
    { key: 'solved_50', label: '五十题达成', description: '累计通过 50 道练习题', threshold: 50, metric: 'solved_count', progress: 4, earned: false, awarded_at: null },
    { key: 'streak_7', label: '连续打卡 7 天', description: '连续 7 天每天都有新增通关', threshold: 7, metric: 'streak_days', progress: 3, earned: false, awarded_at: null },
  ],
  checkin: {
    days: 30,
    active_dates: ['2026-10-11', '2026-10-12', '2026-10-13'],
    streak_days: 3,
    last_active_date: '2026-10-13',
  },
  growth: Array.from({ length: 30 }, (_, index) => {
    const day = new Date(Date.UTC(2026, 8, 14 + index)).toISOString().slice(0, 10)
    return {
      date: day,
      passed_count: index % 4,
      submissions: index % 3,
      avg_score: index % 4 ? 60 + (index % 5) * 8 : null,
    }
  }),
  opt_in: true,
  recalculated_at: '2026-10-13T03:00:00Z',
  ...overrides,
})

export async function practiceSocialFixture(page, role, options = {}) {
  await layoutFixture(page, role)
  const state = { boardQueries: [], optInPuts: [], recalcCalls: 0 }
  const reply = (route, json, status = 200) => route.fulfill({ status, json })

  await page.route(/\/api\/v1\/practice\/social\/leaderboard/, async (route) => {
    const url = new URL(route.request().url())
    const query = Object.fromEntries(url.searchParams.entries())
    state.boardQueries.push(query)
    if (options.failed) return reply(route, { detail: { message: '榜单服务暂不可用' } }, 503)
    if (options.forbidden) return reply(route, { detail: { message: '无权限' } }, 403)
    const optIn = query.scope === 'offering' ? null : (options.optIn ?? true)
    const items = options.empty ? [] : boardRows
    return reply(route, {
      board: query.board || 'deterministic',
      scope: query.scope || 'platform',
      scope_id: query.scope === 'offering' ? Number(query.scope_id || 1) : 0,
      total: items.length,
      truncated: false,
      anonymized: query.scope !== 'offering',
      opt_in: optIn,
      items,
      me: options.notOptedIn ? null : (items.find((row) => row.is_me) || null),
      window: query.view === 'me' ? items.slice(0, 3) : null,
      generated_at: '2026-10-13T03:00:00Z',
      recalculated_at: '2026-10-13T03:00:00Z',
    })
  })

  await page.route(/\/api\/v1\/practice\/social\/me\/opt-in/, async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() || {}
      state.optInPuts.push(body.opt_in)
      return reply(route, { opt_in: !!body.opt_in, decided_at: '2026-10-13T03:10:00Z' })
    }
    return reply(route, { opt_in: options.optIn ?? true, decided_at: '2026-10-13T03:10:00Z' })
  })

  await page.route(/\/api\/v1\/practice\/social\/me\/overview/, async (route) => {
    if (options.failed) return reply(route, { detail: { message: '统计服务暂不可用' } }, 503)
    if (options.forbidden) return reply(route, { detail: { message: '无权限' } }, 403)
    return reply(route, options.emptyOverview ? overviewPayload({
      badges: [], checkin: { days: 30, active_dates: [], streak_days: 0, last_active_date: null },
      growth: [], summary: { ...overviewPayload().summary, solved_count: 0, total_score: 0, streak_days: 0 },
    }) : overviewPayload())
  })

  await page.route(/\/api\/v1\/practice\/me(\?|$)/, async (route) => reply(route, {
    summary: {
      solved_count: 4, total_score: 350, avg_score: 87.5, accuracy: 0.8,
      submission_count: 9, streak_days: 3, daily_submissions_used: 4,
      daily_submission_cap: 20,
    },
    items: options.empty ? [] : [{
      id: 11, problem_id: 3, problem_title: '统计列表中所有正数的和', difficulty: 'medium',
      score: 100, max_score: 100, grading_status: 'completed', grading_mode: 'deterministic',
      created_at: '2026-10-13T02:00:00Z',
    }],
    total: options.empty ? 0 : 1,
    page: 1,
    page_size: 10,
    meta: { quota_exhausted: false, queue: { queued: 0, running: 0, max_concurrent: 4 } },
  }))

  await page.route(/\/api\/v1\/offerings(\?|$)/, async (route) => reply(route, {
    items: [
      { id: 1, course_title: 'Python 与人工智能综合实验', title: '24621601班' },
    ],
    total: 1,
  }))

  return state
}
