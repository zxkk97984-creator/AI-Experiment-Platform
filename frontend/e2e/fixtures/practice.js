// 批次 5 自主练习 E2E fixture。
//
// 先调 layoutFixture 的通用桩（认证 / 侧栏 / 课程等），再叠加本批新增的
// /practice/**、/admin/practice/**、/appeals、/offerings 路由：
// Playwright 后注册的路由优先，因此无需修改共享的 e2e/fixtures/layout.js。
import { layoutFixture, longTitle } from './layout.js'

export { longTitle }

const SETTINGS = {
  grading_mode: 'deterministic',
  daily_submission_cap_per_student: 20,
  daily_ai_budget_tokens: 100000,
  daily_ai_budget_amount: 10,
  max_concurrent_practice_jobs: 4,
  updated_at: '2026-10-13T02:00:00Z',
}

export const practiceProblem = {
  id: 1,
  title: longTitle,
  difficulty: 'medium',
  tags: ['列表', '边界条件'],
  prompt: '返回列表中所有正数的和；空列表返回 0，负数忽略。'.repeat(3),
  status: 'published',
  grading_mode: 'deterministic',
  time_limit_ms: 2000,
  memory_limit_kb: 262144,
  total_attempts: 20,
  distinct_solvers: 9,
  current_grading_version_id: 3,
  my_status: 'attempted',
  my_best_score: 60,
  created_at: '2026-10-12T02:00:00Z',
}

export const draftProblem = {
  ...practiceProblem,
  id: 2,
  title: '尚未发布的练习题草稿',
  status: 'draft',
  my_status: 'not_started',
  my_best_score: null,
  distinct_solvers: 0,
  total_attempts: 0,
}

export const deterministicResult = {
  groups: [
    { name: '公开用例', status: 'passed', passed: 3, total: 3 },
    { name: '边界条件', status: 'failed', passed: 1, total: 2, detail: '空列表返回 None' },
  ],
  passed_count: 4,
  total_count: 5,
}

export const aiBreakdown = {
  functional_score: 45,
  algorithm_score: 16,
  robustness_score: 8,
  quality_score: 9,
  final_score_100: 78,
  strengths: ['边界处理完整'],
  issues: ['缺少复杂度说明'],
  suggestions: ['补充 docstring'],
  code_suggestions: [],
  test_groups: [],
  algorithm_items: [],
  quality_items: [],
}

export function practiceSubmission(overrides = {}) {
  return {
    id: 11,
    problem: { id: 1, title: longTitle, difficulty: 'medium', tags: ['列表'], grading_mode: 'ai', current_grading_version_id: 3 },
    code: 'def sum_positive(values):\n    return sum(v for v in values if v > 0)',
    grading_status: 'completed',
    queue_position: null,
    score: 60,
    max_score: 100,
    grading_mode: 'ai',
    grading_version_id: 3,
    grading_version_stale: false,
    created_at: '2026-10-13T02:00:00Z',
    finished_at: '2026-10-13T02:01:00Z',
    deterministic: deterministicResult,
    ai_result: null,
    ai_status: 'pending',
    appeal: null,
    can_appeal: false,
    ...overrides,
  }
}

export const leaderboardRows = [
  { rank: 1, display_name: '勤勉的海豚', solved_count: 12, total_score: 960, avg_score: 80, is_me: false, achieved_at: '2026-10-13T01:00:00Z' },
  { rank: 2, display_name: '专注的海獭', solved_count: 9, total_score: 720, avg_score: 80, is_me: false, achieved_at: '2026-10-12T01:00:00Z' },
  { rank: 3, display_name: '沉稳的岩羊', solved_count: 6, total_score: 480, avg_score: 80, is_me: false, achieved_at: '2026-10-11T01:00:00Z' },
]

export const adminAppeals = [
  {
    id: 5,
    target_kind: 'practice_submission',
    target_id: 11,
    problem_title: longTitle,
    student_name: '验收学员',
    student_no: '2026000009',
    reason: '老师您好，边界条件那一组我认为我的解法是正确的，请求复核并说明理由。',
    status: 'pending',
    score_before: 60,
    score_after: null,
    handler_comment: null,
    created_at: '2026-10-13T02:10:00Z',
    resolved_at: null,
  },
]

/**
 * @param {import('@playwright/test').Page} page
 * @param {'student'|'teacher'|'admin'} role
 * @param {object} options
 *   empty            列表返回空
 *   failed           列表返回 503
 *   forbidden        列表返回 403（页内权限不足提示）
 *   quotaExhausted   meta.quota_exhausted + 提交返回 429
 *   activeSubmissions 练习中心在途提交（排队态）
 *   submission       作答反馈页返回的提交体（默认 AI 未完成态）
 *   submitResult     提交接口返回体（默认 queued）
 *   preflightFailed  切 AI 判题返回 409 + 清单
 *   appeals          练习复核列表
 *   usage            用量页返回体
 *   noOfferings      教师无任课班级
 */
export async function practiceFixture(page, role, options = {}) {
  await layoutFixture(page, role)
  const state = {
    submitted: [],
    published: [],
    archived: [],
    deleted: [],
    copied: [],
    settingsPatches: [],
    handled: [],
    appealCreates: [],
    problemQueries: [],
    leaderboardQueries: [],
  }
  const reply = (route, json, status = 200) => route.fulfill({ status, json })

  const meta = () => ({
    grading_mode: options.settings?.grading_mode || 'deterministic',
    daily_submission_cap: 20,
    daily_submissions_used: 4,
    quota_exhausted: !!options.quotaExhausted,
    queue: options.queue || { queued: 0, running: 0, max_concurrent: 4 },
    active_submissions: options.activeSubmissions || [],
  })

  const problems = () => {
    if (options.empty) return []
    return options.problems || [practiceProblem]
  }

  await page.route(/\/api\/v1\/practice(\/|$|\?)/, async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname.replace('/api/v1', '')
    if (options.failed) return reply(route, { detail: { message: '测试服务暂不可用' } }, 503)
    if (options.forbidden) return reply(route, { detail: { message: '无权限' } }, 403)

    if (path === '/practice/problems' && req.method() === 'GET') {
      const items = problems()
      const difficulty = url.searchParams.get('difficulty')
      const completion = url.searchParams.get('completion')
      state.problemQueries.push(Object.fromEntries(url.searchParams.entries()))
      const filtered = items.filter((item) => {
        if (difficulty && item.difficulty !== difficulty) return false
        if (completion === 'solved' && item.my_status !== 'solved') return false
        if (completion === 'unsolved' && item.my_status === 'solved') return false
        return true
      })
      return reply(route, { items: filtered, total: filtered.length, meta: meta() })
    }
    if (/^\/practice\/problems\/\d+$/.test(path) && req.method() === 'GET') {
      const id = Number(path.split('/').pop())
      const found = problems().find((item) => item.id === id) || practiceProblem
      return reply(route, {
        ...found,
        starter_code: 'def sum_positive(values):\n    return 0',
        latest_submission_id: options.latestSubmissionId ?? null,
        grading_standard_updated: false,
      })
    }
    if (/^\/practice\/problems\/\d+\/submissions$/.test(path) && req.method() === 'POST') {
      const body = JSON.parse(req.postData() || '{}')
      state.submitted.push(body)
      if (options.quotaExhausted) {
        return reply(route, { detail: { code: 'QUOTA_EXHAUSTED', message: '今日额度已用完', fields: { metric: 'submissions', limit: 20, used: 20 } } }, 429)
      }
      return reply(route, options.submitResult || { id: 11, grading_status: 'queued', grading_mode: 'ai', queue_position: 2, score: null, max_score: 100, created_at: '2026-10-13T02:00:00Z' }, 202)
    }
    if (/^\/practice\/submissions\/\d+$/.test(path) && req.method() === 'GET') {
      return reply(route, options.submission || practiceSubmission())
    }
    if (path === '/practice/me' && req.method() === 'GET') {
      const items = options.empty ? [] : (options.history || [practiceSubmission({
        id: 12,
        problem_id: 1,
        problem_title: practiceProblem.title,
        difficulty: 'medium',
        score: 100,
        max_score: 100,
        grading_status: 'completed',
        grading_mode: 'deterministic',
        created_at: '2026-10-12T02:00:00Z',
      })])
      return reply(route, {
        summary: options.summary || {
          solved_count: 3, total_score: 270, avg_score: 90, accuracy: 75,
          submission_count: 5, streak_days: 2, daily_submission_cap: 20, daily_submissions_used: 4,
        },
        items,
        total: items.length,
        meta: meta(),
      })
    }
    if (path === '/practice/leaderboard' && req.method() === 'GET') {
      state.leaderboardQueries.push(Object.fromEntries(url.searchParams.entries()))
      return reply(route, {
        items: options.empty ? [] : leaderboardRows,
        total: options.empty ? 0 : leaderboardRows.length,
        scope: url.searchParams.get('scope') || 'platform',
        board: url.searchParams.get('board') || 'deterministic',
        meta: meta(),
      })
    }
    return reply(route, { items: [], total: 0 })
  })

  await page.route(/\/api\/v1\/admin\/practice(\/|$|\?)/, async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname.replace('/api/v1', '')
    if (options.failed) return reply(route, { detail: { message: '测试服务不可用' } }, 503)
    if (options.forbidden) return reply(route, { detail: { message: '无权限' } }, 403)

    if (path === '/admin/practice/problems' && req.method() === 'GET') {
      const items = options.empty ? [] : [practiceProblem, draftProblem]
      return reply(route, {
        items,
        total: items.length,
        summary: { total: items.length, published: options.empty ? 0 : 1, draft: options.empty ? 0 : 1, archived: 0 },
        meta: meta(),
      })
    }
    if (path === '/admin/practice/problems/copy-from-assignment' && req.method() === 'POST') {
      const body = JSON.parse(req.postData() || '{}')
      state.copied.push(body)
      return reply(route, { ...draftProblem, id: 77, title: body.title || practiceProblem.title, source_kind: 'from_assignment' }, 201)
    }
    if (/^\/admin\/practice\/problems\/\d+$/.test(path)) {
      const id = Number(path.split('/').pop())
      const found = [practiceProblem, draftProblem].find((item) => item.id === id) || practiceProblem
      if (req.method() === 'GET') {
        return reply(route, { ...found, starter_code: 'def sum_positive(values):\n    return 0' })
      }
      if (req.method() === 'PATCH') {
        return reply(route, { ...found, ...JSON.parse(req.postData() || '{}') })
      }
      if (req.method() === 'DELETE') {
        state.deleted.push(id)
        return reply(route, { ok: true })
      }
    }
    if (/^\/admin\/practice\/problems\/\d+\/(publish|archive)$/.test(path) && req.method() === 'POST') {
      const id = Number(path.split('/')[4])
      const action = path.endsWith('publish') ? 'published' : 'archived'
      if (action === 'published') state.published.push(id)
      else state.archived.push(id)
      return reply(route, { ...practiceProblem, id, status: action })
    }
    if (path === '/admin/practice/problems' && req.method() === 'POST') {
      return reply(route, { ...draftProblem, id: 88, title: JSON.parse(req.postData() || '{}').title }, 201)
    }
    if (path === '/admin/practice/settings') {
      if (req.method() === 'PATCH') {
        const body = JSON.parse(req.postData() || '{}')
        state.settingsPatches.push(body)
        if (body.grading_mode === 'ai' && options.preflightFailed) {
          return reply(route, {
            detail: {
              code: 'AI_PREFLIGHT_FAILED',
              message: '有已发布题目的评分标准尚未准备完成，请补齐或下架后再开启 AI',
              // 全局错误信封只保留 {code, message, fields}（backend/app/main.py _normalize_detail）
              fields: { problems: [{ id: 1, title: longTitle, status: 'published', missing: ['locked_rubric', 'active_version'] }] },
            },
          }, 409)
        }
        return reply(route, { ...SETTINGS, ...(options.settings || {}), ...body })
      }
      return reply(route, { ...SETTINGS, ...(options.settings || {}) })
    }
    if (path === '/admin/practice/ai-preflight') {
      return reply(route, options.preflight || {
        ready: !options.preflightFailed,
        problems: options.preflightFailed
          ? [{ id: 1, title: longTitle, status: 'published', missing: ['locked_rubric', 'active_version'] }]
          : [],
        queue: options.queue || { queued: 0, running: 0, max_concurrent: 4 },
        ai_budget: { exhausted: !!options.quotaExhausted, tokens_used: 1000, tokens_budget: 100000 },
        checked_at: '2026-10-13T02:00:00Z',
      })
    }
    if (path === '/admin/practice/usage') {
      return reply(route, options.usage || {
        date: url.searchParams.get('date') || '2026-10-13',
        submissions_today: 12,
        ai_tokens_used: options.quotaExhausted ? 100000 : 4200,
        ai_budget_tokens: 100000,
        ai_amount_used: 6.5,
        ai_budget_amount: 10,
        queue: options.queue || { queued: 0, running: 2, max_concurrent: 4 },
        daily: [
          { date: '2026-10-13', submissions: 12, ai_tokens: 4200, ai_amount: 6.5 },
          { date: '2026-10-12', submissions: 9, ai_tokens: 3100, ai_amount: 4.2 },
        ],
      })
    }
    if (path === '/admin/practice/appeals' && req.method() === 'GET') {
      const items = options.empty ? [] : (options.appeals || adminAppeals)
      return reply(route, { items, total: items.length, meta: meta() })
    }
    if (/^\/admin\/practice\/appeals\/\d+\/handle$/.test(path) && req.method() === 'POST') {
      const body = JSON.parse(req.postData() || '{}')
      state.handled.push({ id: Number(path.split('/')[4]), ...body })
      return reply(route, { ...adminAppeals[0], status: body.action === 'adjust' ? 'adjusted' : 'upheld', score_after: body.score ?? null })
    }
    return reply(route, { items: [], total: 0 })
  })

  // 学生「我有异议」走批次 2 的 /appeals
  await page.route(/\/api\/v1\/appeals(\/|$|\?)/, async (route) => {
    const req = route.request()
    if (req.method() === 'POST') {
      state.appealCreates.push(JSON.parse(req.postData() || '{}'))
      return reply(route, { id: 9, status: 'pending' }, 201)
    }
    return reply(route, { items: [], total: 0 })
  })

  // 教师班内榜的班级下拉
  await page.route(/\/api\/v1\/offerings(\/|$|\?)/, async (route) => {
    const items = options.noOfferings ? [] : [{ id: 3, course_title: 'Python 程序设计（24621601班）' }]
    return reply(route, { items, total: items.length })
  })

  // 「从作业复制题目」对话框依赖作业与题目列表
  await page.route(/\/api\/v1\/assignments(\/|$|\?)/, async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
    if (/\/questions$/.test(path)) {
      return reply(route, [{ id: 9, title: '正数求和（作业原题）' }])
    }
    return reply(route, { items: [{ id: 2, title: '第一章作业：函数与列表' }], total: 1 })
  })

  return state
}
