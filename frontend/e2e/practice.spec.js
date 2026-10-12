import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import {
  adminAppeals, aiBreakdown, longTitle, practiceFixture, practiceProblem, practiceSubmission,
} from './fixtures/practice.js'

// 批次 5 自主练习：真实浏览器验证管理员 / 教师 / 学生三角色。
// 所有请求由 fixtures/practice.js 本地桩接，不写真实后端。
// 依赖已应用的热文件注册面：/student/practice*、/admin/practice*、/teacher/practice/leaderboard。

// 截图目录按 spec 文件位置解析（仓库根 .runtime/deepseek-evidence/batch5），
// 不依赖 npx 的当前工作目录
const EVIDENCE = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '../../.runtime/deepseek-evidence/batch5')
const DESKTOP = { width: 1542, height: 950 }
const NARROW = { width: 390, height: 844 }

async function shot(page, name, fullPage = true) {
  await page.screenshot({ path: `${EVIDENCE}/${name}.png`, fullPage, animations: 'disabled' })
}

async function assertNoHorizontalOverflow(page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
}

test.describe('学生：练习中心', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP)
  })

  test('练习中心展示长标题、筛选与侧栏入口（桌面截图）', async ({ page }) => {
    const state = await practiceFixture(page, 'student')
    await page.goto('/student/practice')

    await expect(page.getByRole('heading', { name: '自主练习' })).toBeVisible()
    await expect(page.getByRole('button', { name: '自主练习' })).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.crumb .current')).toHaveText('自主练习')
    await expect(page.getByText(longTitle)).toBeVisible()
    await expect(page.locator('.problem-card').first().getByText('做过未通过')).toBeVisible()
    await shot(page, 'student-practice-center-desktop-1542')

    // 难度筛选会带上 difficulty 参数重新查询
    await page.getByRole('button', { name: '按难度筛选' }).click()
    await page.getByRole('option', { name: '困难', exact: true }).click()
    await expect.poll(() => state.problemQueries.at(-1)?.difficulty).toBe('hard')
    await expect(page.getByText('没有符合条件的题目')).toBeVisible()
    await shot(page, 'student-practice-center-empty-filtered')
  })

  test('练习中心六态：空数据 / 失败 / 权限不足 / 额度耗尽 / 排队', async ({ page }) => {
    await practiceFixture(page, 'student', { empty: true })
    await page.goto('/student/practice')
    await expect(page.getByText('题库暂时没有已发布的题目')).toBeVisible()
    await shot(page, 'student-practice-center-empty')

    await practiceFixture(page, 'student', { failed: true })
    await page.goto('/student/practice')
    await expect(page.getByText('加载失败')).toBeVisible()
    await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
    await shot(page, 'student-practice-center-failed')

    await practiceFixture(page, 'student', { forbidden: true })
    await page.goto('/student/practice')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.locator('.problem-grid')).toHaveCount(0)
    await shot(page, 'student-practice-center-forbidden')

    await practiceFixture(page, 'student', {
      quotaExhausted: true,
      activeSubmissions: [
        { id: 21, problem_id: 1, problem_title: practiceProblem.title, grading_status: 'queued', queue_position: 2 },
        { id: 22, problem_id: 2, problem_title: '判题中的练习题', grading_status: 'running', queue_position: null },
      ],
    })
    await page.goto('/student/practice')
    await expect(page.getByText('今日额度已用完')).toBeVisible()
    await expect(page.getByText(`《${practiceProblem.title}》排队中（第 2 位）`)).toBeVisible()
    await expect(page.getByText('《判题中的练习题》判题中')).toBeVisible()
    await shot(page, 'student-practice-center-quota-queued')

    // 在途提交可跳到反馈页
    await page.getByRole('link', { name: '查看进度' }).first().click()
    await expect(page).toHaveURL(/\/student\/practice\/submissions\/21$/)
  })

  test('窄屏 390 下练习中心无水平溢出', async ({ page }) => {
    await page.setViewportSize(NARROW)
    await practiceFixture(page, 'student')
    await page.goto('/student/practice')
    await expect(page.getByRole('heading', { name: '自主练习' })).toBeVisible()
    await assertNoHorizontalOverflow(page)
    await shot(page, 'student-practice-center-narrow-390')
  })

  test('角色守卫：学生访问管理员练习页被静默跳回学生工作台', async ({ page }) => {
    await practiceFixture(page, 'student')
    await page.goto('/admin/practice/problems')
    await expect(page).toHaveURL(/\/student$/)
  })
})

test.describe('学生：作答与反馈', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP)
  })

  test('键盘：Tab 到提交按钮并用 Enter 提交，展示排队名次', async ({ page }) => {
    const state = await practiceFixture(page, 'student', {
      latestSubmissionId: null,
      submitResult: { id: 11, grading_status: 'queued', grading_mode: 'ai', queue_position: 2, score: null, max_score: 100, created_at: '2026-10-13T02:00:00Z' },
      submission: practiceSubmission({ grading_status: 'queued', queue_position: 2, score: null, deterministic: null }),
    })
    await page.goto('/student/practice/problems/1')
    await expect(page.locator('h1')).toHaveText(longTitle)
    await expect(page.getByText('我的最高分')).toBeVisible()

    const editor = page.getByLabel('我的作答')
    await editor.focus()
    await page.keyboard.press('Tab')
    await expect(page.getByRole('button', { name: '提交作答' })).toBeFocused()
    await page.keyboard.press('Enter')

    await expect(page.getByText('排队中（第 2 位）')).toBeVisible()
    await shot(page, 'student-practice-answer-queued')

    expect(state.submitted).toHaveLength(1)
    expect(state.submitted[0].code).toContain('def sum_positive')
    expect(state.submitted[0].client_request_id).toBeTruthy()
  })

  test('额度耗尽：提交被拒并显示「今日额度已用完」', async ({ page }) => {
    await practiceFixture(page, 'student', { quotaExhausted: true })
    await page.goto('/student/practice/problems/1')
    await expect(page.locator('h1')).toHaveText(longTitle)
    await page.getByRole('button', { name: '提交作答' }).click()
    await expect(page.locator('.practice-notice .notice-title').filter({ hasText: '今日额度已用完' })).toBeVisible()
    await shot(page, 'student-practice-answer-quota-exhausted')
  })

  test('AI 未完成：显示「AI 评分未完成」与已完成的确定性分，不显示四维分', async ({ page }) => {
    await practiceFixture(page, 'student', {
      submission: practiceSubmission({ grading_status: 'completed', ai_status: 'pending', ai_result: null, score: 60 }),
    })
    await page.goto('/student/practice/submissions/11')
    await expect(page.getByText('AI 评分未完成').first()).toBeVisible()
    await expect(page.locator('.score-value')).toHaveText('60 / 100')
    await expect(page.getByLabel('测试结果').getByText('4 / 5 组测试通过')).toBeVisible()
    await expect(page.getByText('功能正确性 F')).toHaveCount(0)
    await expect(page.getByLabel('测试结果').getByText('边界条件', { exact: true })).toBeVisible()
    await shot(page, 'student-practice-feedback-ai-incomplete')
  })

  test('AI 完成：四维反馈可见并可就地提交异议', async ({ page }) => {
    const state = await practiceFixture(page, 'student', {
      submission: practiceSubmission({
        grading_status: 'completed',
        ai_status: 'completed',
        ai_result: aiBreakdown,
        score: 78,
        can_appeal: true,
      }),
    })
    await page.goto('/student/practice/submissions/11')
    await expect(page.getByText('功能正确性 F')).toBeVisible()
    await expect(page.getByText('AI 四维评分')).toBeVisible()
    await shot(page, 'student-practice-feedback-ai-completed')

    await page.getByPlaceholder('请说明存在疑问的评分项与依据（20–2000 字）')
      .fill('第三组边界条件我认为我的解法是正确的，请复核该评分项依据。')
    await page.getByRole('button', { name: '我有异议' }).click()
    await expect.poll(() => state.appealCreates.length).toBe(1)
    expect(state.appealCreates[0]).toMatchObject({ target_kind: 'practice_submission', target_id: 11 })
  })

  test('我的练习：统计、历史与窄屏无溢出', async ({ page }) => {
    await practiceFixture(page, 'student')
    await page.goto('/student/practice/mine')
    await expect(page.getByRole('heading', { name: '我的练习' })).toBeVisible()
    await expect(page.getByText('已通过题目')).toBeVisible()
    await expect(page.getByText('今日已提交 4 / 20 次')).toBeVisible()
    await expect(page.getByText('100 / 100')).toBeVisible()
    await shot(page, 'student-practice-mine-desktop-1542')

    await page.setViewportSize(NARROW)
    await page.goto('/student/practice/mine')
    await expect(page.getByRole('heading', { name: '我的练习' })).toBeVisible()
    await assertNoHorizontalOverflow(page)
    await shot(page, 'student-practice-mine-narrow-390')
  })
})

test.describe('管理员：题库 / 设置 / 用量 / 复核', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP)
  })

  test('题库列表：概览、发布二次确认、归档入口与长标题', async ({ page }) => {
    const state = await practiceFixture(page, 'admin')
    await page.goto('/admin/practice/problems')
    await expect(page.getByRole('heading', { name: '练习题库' })).toBeVisible()
    await expect(page.locator('.crumb .current')).toHaveText('练习题库')
    await expect(page.getByText(longTitle)).toBeVisible()
    await expect(page.getByText('尚未发布的练习题草稿')).toBeVisible()
    await shot(page, 'admin-practice-problems-desktop-1542')

    await page.getByRole('button', { name: '发布' }).click()
    await expect(page.getByRole('dialog', { name: '发布练习题' })).toBeVisible()
    await page.getByRole('button', { name: '确认' }).click()
    await expect.poll(() => state.published).toEqual([2])
    await expect(page.getByRole('button', { name: '归档' })).toBeVisible()
  })

  test('从已有作业复制题目生成草稿', async ({ page }) => {
    const state = await practiceFixture(page, 'admin')
    await page.goto('/admin/practice/problems')
    await page.getByRole('button', { name: '从作业复制' }).click()
    await expect(page.getByRole('dialog', { name: '从已有作业复制题目' })).toBeVisible()

    await page.getByRole('button', { name: '选择作业' }).click()
    await page.getByRole('option', { name: '第一章作业：函数与列表', exact: true }).click()
    await page.getByRole('button', { name: '选择题目' }).click()
    await page.getByRole('option', { name: '正数求和（作业原题）', exact: true }).click()
    await shot(page, 'admin-practice-copy-from-assignment')

    await page.getByRole('button', { name: '复制为草稿' }).click()
    await expect.poll(() => state.copied.length).toBe(1)
    expect(state.copied[0]).toMatchObject({ assignment_id: 2, question_id: 9 })
    await expect(page).toHaveURL(/\/admin\/practice\/problems\/77\/edit$/)
  })

  test('题库权限不足：403 时只显示提示，不渲染表格', async ({ page }) => {
    await practiceFixture(page, 'admin', { forbidden: true })
    await page.goto('/admin/practice/problems')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.locator('table')).toHaveCount(0)
    await shot(page, 'admin-practice-problems-forbidden')
  })

  test('设置页：预检清单、切换 AI 被 409 拒绝、额度与队列提示', async ({ page }) => {
    const state = await practiceFixture(page, 'admin', { preflightFailed: true, quotaExhausted: true, queue: { queued: 3, running: 4, max_concurrent: 4 } })
    await page.goto('/admin/practice/settings')
    await expect(page.getByRole('heading', { name: '练习设置' })).toBeVisible()
    await expect(page.getByText('AI 预检有 1 道题未就绪')).toBeVisible()
    await expect(page.locator('.practice-notice .notice-title').filter({ hasText: '今日额度已用完' })).toBeVisible()
    await expect(page.getByText('练习判题队列积压 3 项')).toBeVisible()
    await shot(page, 'admin-practice-settings-desktop-1542')

    await page.getByRole('button', { name: 'AI 综合判题' }).click()
    await expect(page.locator('.blocked-box')).toContainText('切换被拒绝')
    await expect(page.locator('.blocked-box')).toContainText('评分标准未锁定')
    await expect(page.locator('.blocked-box')).toContainText('缺少已启用的评分版本')
    await expect.poll(() => state.settingsPatches.at(-1)).toMatchObject({ grading_mode: 'ai' })
    await shot(page, 'admin-practice-settings-preflight-blocked')

    // 额度与并发保存
    await page.getByRole('button', { name: '保存配置' }).click()
    await expect.poll(() => state.settingsPatches.at(-1)).toMatchObject({ max_concurrent_practice_jobs: 4 })
  })

  test('用量页：预算耗尽与队列占用', async ({ page }) => {
    await practiceFixture(page, 'admin', { quotaExhausted: true, queue: { queued: 6, running: 4, max_concurrent: 4 } })
    await page.goto('/admin/practice/usage')
    await expect(page.getByRole('heading', { name: '练习用量' })).toBeVisible()
    await expect(page.locator('.practice-notice .notice-title').filter({ hasText: '今日额度已用完' })).toBeVisible()
    await expect(page.getByText('练习队列排队 6 项')).toBeVisible()
    await expect(page.getByText('100000 / 100000')).toBeVisible()
    await shot(page, 'admin-practice-usage-desktop-1542')

    await page.setViewportSize(NARROW)
    await page.goto('/admin/practice/usage')
    await expect(page.getByRole('heading', { name: '练习用量' })).toBeVisible()
    await assertNoHorizontalOverflow(page)
    await shot(page, 'admin-practice-usage-narrow-390')
  })

  test('练习复核：处理改分异议并透传评分与说明', async ({ page }) => {
    const state = await practiceFixture(page, 'admin', { appeals: adminAppeals })
    await page.goto('/admin/practice/appeals')
    await expect(page.getByRole('heading', { name: '练习复核' })).toBeVisible()
    await expect(page.getByText('验收学员')).toBeVisible()
    await shot(page, 'admin-practice-appeals-desktop-1542')

    await page.getByRole('button', { name: '处理' }).click()
    await expect(page.getByRole('dialog', { name: '处理练习成绩异议' })).toBeVisible()
    await page.getByRole('button', { name: '处理方式' }).click()
    await page.getByRole('option', { name: '人工改分', exact: true }).click()
    await page.getByLabel('调整后的总分（0–100）').fill('88')
    await page.getByPlaceholder('说明维持原因、改分依据或重评理由').fill('边界条件判定有误，按参考解法调整为 88 分')
    await shot(page, 'admin-practice-appeals-handle-dialog')
    await page.getByRole('button', { name: '确认处理' }).click()

    await expect.poll(() => state.handled.length).toBe(1)
    expect(state.handled[0]).toMatchObject({ id: 5, action: 'adjust', score: 88 })
  })

  test('练习复核空态与权限不足', async ({ page }) => {
    await practiceFixture(page, 'admin', { empty: true })
    await page.goto('/admin/practice/appeals')
    await expect(page.getByText('没有待处理的练习异议')).toBeVisible()

    await practiceFixture(page, 'admin', { forbidden: true })
    await page.goto('/admin/practice/appeals')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.locator('table')).toHaveCount(0)
  })
})

test.describe('教师：只读排行榜', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP)
  })

  test('全平台榜 / 班内榜 × 普通榜 / AI 榜切换，只读无写操作', async ({ page }) => {
    const state = await practiceFixture(page, 'teacher')
    await page.goto('/teacher/practice/leaderboard')
    await expect(page.getByRole('heading', { name: '练习排行榜' })).toBeVisible()
    await expect(page.locator('.crumb .current')).toHaveText('练习排行榜')
    await expect(page.getByText('勤勉的海豚')).toBeVisible()
    await expect(page.getByText('全平台榜 · 普通测试榜 · 3 人')).toBeVisible()
    await shot(page, 'teacher-practice-leaderboard-desktop-1542')

    await page.getByRole('button', { name: 'AI 综合榜' }).click()
    await expect.poll(() => state.leaderboardQueries.at(-1)?.board).toBe('ai')

    await page.getByRole('button', { name: '班内榜' }).click()
    await expect.poll(() => state.leaderboardQueries.at(-1)?.scope).toBe('offering')
    await expect.poll(() => String(state.leaderboardQueries.at(-1)?.scope_id)).toBe('3')

    // 只读：没有任何题库写操作入口
    await expect(page.getByRole('button', { name: '发布' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '归档' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '编辑' })).toHaveCount(0)
  })

  test('排队 / 额度耗尽 / 空态 / 权限不足', async ({ page }) => {
    await practiceFixture(page, 'teacher', { quotaExhausted: true, queue: { queued: 6, running: 2, max_concurrent: 4 } })
    await page.goto('/teacher/practice/leaderboard')
    await expect(page.getByText('今日额度已用完')).toBeVisible()
    await expect(page.getByText('练习判题队列排队 6 项')).toBeVisible()
    await shot(page, 'teacher-practice-leaderboard-quota-queued')

    await practiceFixture(page, 'teacher', { empty: true })
    await page.goto('/teacher/practice/leaderboard')
    await expect(page.getByText('还没有人完成练习')).toBeVisible()
    await shot(page, 'teacher-practice-leaderboard-empty')

    await practiceFixture(page, 'teacher', { forbidden: true })
    await page.goto('/teacher/practice/leaderboard')
    await expect(page.getByText('权限不足')).toBeVisible()
    await expect(page.locator('table')).toHaveCount(0)
    await shot(page, 'teacher-practice-leaderboard-forbidden')
  })

  test('窄屏 390 下排行榜无水平溢出', async ({ page }) => {
    await page.setViewportSize(NARROW)
    await practiceFixture(page, 'teacher')
    await page.goto('/teacher/practice/leaderboard')
    await expect(page.getByRole('heading', { name: '练习排行榜' })).toBeVisible()
    await assertNoHorizontalOverflow(page)
    await shot(page, 'teacher-practice-leaderboard-narrow-390')
  })
})

test.describe('首页快捷入口（热文件注册面）', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP)
  })

  test('学生工作台卡片、教师最近提交入口、管理员管理入口都能进入练习模块', async ({ page }) => {
    await practiceFixture(page, 'student')
    await page.goto('/student')
    await expect(page.getByText('练习中心')).toBeVisible()
    await shot(page, 'student-dashboard-practice-card')
    await page.getByRole('button', { name: '开始练习' }).click()
    await expect(page).toHaveURL(/\/student\/practice$/)

    await practiceFixture(page, 'teacher')
    await page.goto('/teacher')
    await page.getByRole('button', { name: '练习排行榜 →' }).click()
    await expect(page).toHaveURL(/\/teacher\/practice\/leaderboard$/)

    await practiceFixture(page, 'admin')
    await page.goto('/admin')
    await page.locator('.section-row').filter({ hasText: '练习题库' }).click()
    await expect(page).toHaveURL(/\/admin\/practice\/problems$/)
    await expect(page.getByRole('heading', { name: '练习题库' })).toBeVisible()
  })
})
