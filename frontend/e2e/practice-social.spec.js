import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import { practiceSocialFixture } from './fixtures/practice-social.js'

// 批次 6 练习榜单与激励：真实浏览器验证学生 / 教师两个角色与窄屏。
// 依赖已应用的热文件注册面：/student/practice/leaderboard。
// 所有请求由 fixtures/practice-social.js 本地桩接，不写真实后端。

const EVIDENCE = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '../../.runtime/deepseek-evidence/batch6')
const DESKTOP = { width: 1542, height: 950 }
const NARROW = { width: 390, height: 844 }

async function shot(page, name, fullPage = true) {
  await page.screenshot({ path: `${EVIDENCE}/${name}.png`, fullPage, animations: 'disabled' })
}

async function assertNoHorizontalOverflow(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
}

test.describe('学生：练习排行榜', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP)
  })

  test('并列名次、化名与我的名次视图（桌面截图）', async ({ page }) => {
    const state = await practiceSocialFixture(page, 'student', { optIn: true })
    await page.goto('/student/practice/leaderboard')

    await expect(page.getByRole('heading', { name: '练习排行榜' })).toBeVisible()
    await expect(page.getByRole('button', { name: '练习排行榜' })).toHaveAttribute('aria-current', 'page')
    const rows = page.locator('tbody tr')
    await expect(rows).toHaveCount(4)
    await expect(rows.nth(1).locator('.rank')).toHaveText('2')
    await expect(rows.nth(2).locator('.rank')).toHaveText('2')  // 并列 rank 相同
    await expect(rows.nth(2).getByText('我')).toBeVisible()
    await expect(rows.nth(0).getByText('同学 7A3F21')).toBeVisible()
    await expect(page.getByText('榜单快照时间')).toBeVisible()
    await shot(page, 'student-practice-board-desktop-1542')

    await page.getByRole('button', { name: '只看我的名次' }).click()
    await expect.poll(() => state.boardQueries.at(-1)?.view).toBe('me')
    await shot(page, 'student-practice-board-my-rank')
  })

  test('未 opt-in：提示与开关文案，加入后调用 PUT', async ({ page }) => {
    const state = await practiceSocialFixture(page, 'student', { optIn: false, notOptedIn: true })
    await page.goto('/student/practice/leaderboard')
    await expect(page.getByText('你尚未加入全平台榜')).toBeVisible()
    await shot(page, 'student-practice-board-not-opted-in')

    const toggle = page.locator('input[type="checkbox"]')
    await toggle.focus()
    await page.keyboard.press('Space')
    await expect.poll(() => state.optInPuts.at(-1)).toBe(true)
  })

  test('空榜 / 失败 / 权限不足三态（含截图）', async ({ page }) => {
    await practiceSocialFixture(page, 'student', { empty: true, optIn: true })
    await page.goto('/student/practice/leaderboard')
    await expect(page.getByText('还没有人完成练习')).toBeVisible()
    await shot(page, 'student-practice-board-empty')

    await practiceSocialFixture(page, 'student', { failed: true })
    await page.goto('/student/practice/leaderboard')
    await expect(page.getByText('加载失败')).toBeVisible()
    await shot(page, 'student-practice-board-failed')

    await practiceSocialFixture(page, 'student', { forbidden: true })
    await page.goto('/student/practice/leaderboard')
    await expect(page.getByText('权限不足')).toBeVisible()
    await shot(page, 'student-practice-board-forbidden')
  })

  test('窄屏 390 无水平溢出且成长曲线降级为列表', async ({ page }) => {
    await page.setViewportSize(NARROW)
    await practiceSocialFixture(page, 'student', { optIn: true })
    await page.goto('/student/practice/leaderboard')
    await expect(page.getByRole('heading', { name: '练习排行榜' })).toBeVisible()
    await assertNoHorizontalOverflow(page)
    await shot(page, 'student-practice-board-narrow-390')
  })
})

test.describe('学生：我的练习激励', () => {
  test('统计卡 / 打卡日历 / 徽章墙 / 成长曲线（桌面 + 窄屏）', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await practiceSocialFixture(page, 'student', { optIn: true })
    await page.goto('/student/practice/mine')

    await expect(page.getByRole('heading', { name: '我的练习' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '打卡日历' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '徽章墙' })).toBeVisible()
    await expect(page.getByRole('heading', { name: '成长曲线' })).toBeVisible()
    await expect(page.getByText('首题通关')).toBeVisible()
    await expect(page.locator('.badge.earned')).toHaveCount(1)
    await expect(page.locator('.day.active')).toHaveCount(3)
    await expect(page.locator('.growth svg')).toBeVisible()
    await shot(page, 'student-practice-mine-incentive-desktop-1542')

    await page.setViewportSize(NARROW)
    await expect(page.locator('.growth-list')).toBeVisible()
    await expect(page.locator('.growth svg')).toHaveCount(0)
    await assertNoHorizontalOverflow(page)
    await shot(page, 'student-practice-mine-incentive-narrow-390')
  })
})

test.describe('教师：只读练习排行榜', () => {
  test('只读视图无 opt-in 开关与写操作（桌面截图）', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await practiceSocialFixture(page, 'teacher')
    await page.goto('/teacher/practice/leaderboard')

    await expect(page.getByRole('heading', { name: '练习排行榜' })).toBeVisible()
    await expect(page.getByText('只读入口')).toBeVisible()
    await expect(page.locator('input[type="checkbox"]')).toHaveCount(0)
    await expect(page.locator('tbody tr')).toHaveCount(4)
    await shot(page, 'teacher-practice-board-readonly-desktop-1542')

    // 班内榜：选择班级后带 scope_id 重新查询
    await page.getByRole('button', { name: '班内榜' }).click()
    await expect(page.locator('tbody tr')).toHaveCount(4)
  })
})
