// 考试验收回归：把上次浏览器验收发现的四类失败固化为可重复的隔离用例。
//
// 全部 /api/v1 请求由本文件本地应答（无后端、无业务库）；未被显式处理的路径记录在
// unmatched 列表里，测试结束断言其仅包含已知良性路径，避免"静默打到真实服务"。
import { expect, test } from '@playwright/test'

const serverNow = '2026-08-14T10:00:00Z'

function questionsOf(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: index + 1,
    question_type: 'single_choice',
    prompt: `第 ${index + 1} 题`,
    options: { A: '选项 A', B: '选项 B' },
    points: 2,
  }))
}

function sessionPayload({ questions, expiresAt, status = 'started', saved = [] }) {
  return {
    server_now: serverNow,
    exam: { id: 41, title: '验收考试', duration_minutes: 60, max_score: 80, student_status: 'in_progress' },
    submission: { id: 77, status, score: null, score_visible: false, expires_at: expiresAt },
    questions,
    saved_answers: saved,
    visibility: {},
  }
}

/** 注册隔离护栏：未显式匹配的 API 记录路径并返回空对象（绝不外呼）。 */
async function isolate(page, matched) {
  const unmatched = []
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (!matched.some(pattern => pattern.test(path))) unmatched.push(path)
    await route.fulfill({ json: {} })
  })
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  await page.route('https://fonts.gstatic.com/**', route => route.abort())
  return unmatched
}

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 })
})

test('教师延时后到点不提前交卷，恢复编辑并刷新计时', async ({ page }) => {
  const submits = []
  const matched = [/\/exams\/41\/session$/, /\/exams\/41\/submit$/, /\/auth\/(refresh|me)$/, /\/users/, /\/notifications/]
  const unmatched = await isolate(page, matched)

  let sessionCalls = 0
  await page.route('**/api/v1/auth/refresh', route => route.fulfill({
    json: { access_token: 'exam-regression-token', user: { id: 9, username: 'student', real_name: '验收学生', role: 'student', must_change_password: false } },
  }))
  await page.route('**/api/v1/exams/41/session', route => {
    sessionCalls += 1
    // 首次返回"本地看到的旧截止已到"，服务器确认后返回延长一小时
    const expiresAt = sessionCalls === 1 ? serverNow : '2026-08-14T11:00:00Z'
    return route.fulfill({ json: sessionPayload({ questions: questionsOf(3), expiresAt }) })
  })
  await page.route('**/api/v1/exams/41/submit', route => {
    submits.push(route.request().postDataJSON() || {})
    return route.fulfill({ json: sessionPayload({ questions: questionsOf(3), expiresAt: serverNow, status: 'submitted' }) })
  })

  await page.goto('/student/exams/41')
  await expect(page.getByRole('heading', { name: '验收考试' })).toBeVisible()
  await expect.poll(() => sessionCalls).toBeGreaterThan(1)

  // 延时后恢复编辑且倒计时刷新（不是 00:00:00）
  await expect(page.locator('.timer strong')).not.toHaveText('00:00:00')
  await expect(page.locator('textarea, .options input').first()).toBeEnabled()
  expect(submits).toHaveLength(0)
  expect(unmatched).toEqual([])
})

test('离线到点：显示确认、保留草稿，不依据旧截止自动交卷', async ({ page }) => {
  const submits = []
  const matched = [/\/exams\/41\/session$/, /\/exams\/41\/submit$/, /\/auth\/(refresh|me)$/, /\/users/, /\/notifications/]
  const unmatched = await isolate(page, matched)

  let sessionCalls = 0
  await page.route('**/api/v1/auth/refresh', route => route.fulfill({
    json: { access_token: 'exam-regression-token', user: { id: 9, username: 'student', real_name: '验收学生', role: 'student', must_change_password: false } },
  }))
  await page.route('**/api/v1/exams/41/session', route => {
    sessionCalls += 1
    if (sessionCalls === 1) {
      return route.fulfill({ json: sessionPayload({ questions: questionsOf(3), expiresAt: serverNow }) })
    }
    return route.abort('failed')     // 确认阶段网络不可用
  })
  await page.route('**/api/v1/exams/41/submit', route => {
    submits.push(route.request().postDataJSON() || {})
    return route.fulfill({ json: {} })
  })

  await page.goto('/student/exams/41')
  await expect(page.getByText('答案草稿已保留在本机')).toBeVisible()
  await expect(page.getByRole('button', { name: '立即尝试交卷' })).toBeVisible()
  expect(submits).toHaveLength(0)
  expect(unmatched).toEqual([])
})

test('并发编辑串行保存，旧响应不得降低版本', async ({ page }) => {
  const matched = [/\/exams\/41\/session$/, /\/exams\/41\/answers$/, /\/auth\/(refresh|me)$/, /\/users/, /\/notifications/]
  const unmatched = await isolate(page, matched)

  await page.route('**/api/v1/auth/refresh', route => route.fulfill({
    json: { access_token: 'exam-regression-token', user: { id: 9, username: 'student', real_name: '验收学生', role: 'student', must_change_password: false } },
  }))
  await page.route('**/api/v1/exams/41/session', route => route.fulfill({
    json: sessionPayload({ questions: questionsOf(2), expiresAt: '2026-08-14T11:00:00Z' }),
  }))

  let inFlight = 0
  let maxInFlight = 0
  const payloads = []
  await page.route('**/api/v1/exams/41/answers', async route => {
    const body = route.request().postDataJSON()
    payloads.push(body.answers)
    inFlight += 1
    maxInFlight = Math.max(maxInFlight, inFlight)
    await new Promise(resolve => setTimeout(resolve, 250))
    const attempt = payloads.length
    await route.fulfill({
      json: {
        server_now: serverNow,
        // 第 1 次返回版本 3；第 2 次故意返回更旧的版本 1（模拟乱序/陈旧响应）
        results: body.answers.map(answer => ({ question_id: answer.question_id, ok: true, version: attempt === 1 ? 3 : 1 })),
      },
    })
    inFlight -= 1
  })

  await page.goto('/student/exams/41')
  await expect(page.getByRole('heading', { name: '验收考试' })).toBeVisible()

  const first = page.locator('article.question-card').first()
  await first.locator('input[type="radio"]').first().check()
  // 等第一个请求真正在途，再产生新编辑：它必须排队而不是并发
  await expect.poll(() => payloads.length).toBe(1)
  await first.locator('input[type="radio"]').nth(1).check()
  await expect.poll(() => payloads.length).toBeGreaterThanOrEqual(2)
  await expect.poll(() => maxInFlight).toBe(1)                // 串行：任何时刻最多一个在途
  expect(payloads[1][0].selected_options).toEqual(['B'])      // 第二批携带最新编辑

  // 旧响应（版本 1）不得把版本降到 1：下一次保存必须以版本 3 提交
  await first.locator('input[type="radio"]').first().check()
  await expect.poll(() => payloads.length).toBeGreaterThanOrEqual(3)
  expect(payloads[2][0].expected_version).toBe(3)
  expect(unmatched).toEqual([])
})

test('40 题短屏：题号区内部滚动，交卷入口始终可见', async ({ page }) => {
  const matched = [/\/exams\/41\/session$/, /\/auth\/(refresh|me)$/, /\/users/, /\/notifications/]
  const unmatched = await isolate(page, matched)

  await page.route('**/api/v1/auth/refresh', route => route.fulfill({
    json: { access_token: 'exam-regression-token', user: { id: 9, username: 'student', real_name: '验收学生', role: 'student', must_change_password: false } },
  }))
  await page.route('**/api/v1/exams/41/session', route => route.fulfill({
    json: sessionPayload({ questions: questionsOf(40), expiresAt: '2026-08-14T11:00:00Z' }),
  }))

  await page.goto('/student/exams/41')
  await expect(page.getByRole('heading', { name: '验收考试' })).toBeVisible()

  const submitBox = await page.locator('.submit-button').boundingBox()
  const viewport = page.viewportSize()
  expect(submitBox).not.toBeNull()
  expect(submitBox.y).toBeGreaterThanOrEqual(0)
  expect(submitBox.y + submitBox.height).toBeLessThanOrEqual(viewport.height)

  const navMetrics = await page.locator('nav.question-nav').evaluate(element => {
    const style = getComputedStyle(element)
    return { overflowY: style.overflowY, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight }
  })
  expect(navMetrics.overflowY).toBe('auto')
  expect(navMetrics.scrollHeight).toBeGreaterThan(navMetrics.clientHeight)

  // 滚动题号区到底部后，交卷入口仍在视口内
  await page.locator('nav.question-nav').evaluate(element => { element.scrollTop = element.scrollHeight })
  const afterScroll = await page.locator('.submit-button').boundingBox()
  expect(afterScroll.y + afterScroll.height).toBeLessThanOrEqual(viewport.height)
  expect(unmatched).toEqual([])
})

test('管理员直达考试成绩详情：路由可解析且链接使用 admin 前缀', async ({ page }) => {
  const matched = [/\/exams\/41\/grades(\/19)?$/, /\/exams\/41$/, /\/auth\/(refresh|me)$/, /\/users/, /\/notifications/]
  const unmatched = await isolate(page, matched)

  await page.route('**/api/v1/auth/refresh', route => route.fulfill({
    json: { access_token: 'admin-regression-token', user: { id: 1, username: 'admin', real_name: '教务管理员', role: 'admin', must_change_password: false } },
  }))
  await page.route('**/api/v1/exams/41/grades/19', route => route.fulfill({
    json: {
      exam: { title: '验收考试', course_title: 'Python 程序设计' },
      student: { name: '验收学生', number: '2026001' },
      submission: { score: 66, status: 'graded', submitted_at: serverNow, elapsed_minutes: 42 },
      analysis: { objective_score: 16, objective_total: 20, code_score: 50, code_total: 60, question_count: 1, correct_count: 1 },
      questions: [{ id: 1, exam_id: 41, question_type: 'single_choice', prompt: '第 1 题', points: 2, order_index: 0, options: { A: '选项 A', B: '选项 B' }, correct_answer: { correct: ['A'] } }],
      answers: [{ id: 1, question_id: 1, order_index: 0, question_type: 'single_choice', prompt: '第 1 题', points: 2, score: 2, selected_options: ['A'] }],
    },
  }))

  await page.goto('/admin/exams/41/grades/19')
  await expect.poll(() => new URL(page.url()).pathname).toBe('/admin/exams/41/grades/19')
  await expect(page.getByText('学生成绩详情')).toBeVisible()

  // 返回链接必须指向 admin 前缀（而不是 /teacher）
  await page.getByRole('button', { name: /返回成绩总览/ }).first().click()
  await expect.poll(() => new URL(page.url()).pathname).toBe('/admin/exams/41/grades')
  expect(unmatched).toEqual([])
})
