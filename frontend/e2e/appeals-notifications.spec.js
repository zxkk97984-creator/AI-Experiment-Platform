import { expect, test } from '@playwright/test'

import { appealsFixture } from './fixtures/appeals.js'

// 批次 2 学生通知中心与教师待处理异议：全部请求本地桩接，不写真实后端。
// 依赖未 commit 的热文件 diff：/student/notifications 路由 + AppSidebar 入口 +
// AppHeader 未读角标指向 /student/notifications（见 .runtime/batch2-frontend-hotdiff.md）。

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 })
})

test('学生通知中心展示评分与异议通知，并可全部标记已读', async ({ page }) => {
  await appealsFixture(page, { role: 'student' })
  await page.goto('/student/notifications')

  await expect(page.getByRole('heading', { name: '通知中心' })).toBeVisible()
  await expect(page.getByText('AI 评分已完成')).toBeVisible()
  await expect(page.getByText('成绩异议已改分')).toBeVisible()
  await expect(page.getByText('2 条未读')).toBeVisible()

  await page.getByRole('button', { name: '全部已读' }).click()
  await expect(page.getByText('0 条未读')).toBeVisible()
})

test('仅未读筛选可切换到空态', async ({ page }) => {
  await appealsFixture(page, {
    role: 'student',
    notifications: [{
      id: 3, type: 'grading', title: 'AI 评分已完成', content: '已评分',
      entity_kind: 'code_grade', entity_id: 9, route: '', priority: 'normal',
      created_at: '2026-10-13T01:00:00Z', is_read: true,
    }],
  })
  await page.goto('/student/notifications')

  await page.getByRole('button', { name: '仅未读' }).click()
  await expect(page.getByText('没有未读通知')).toBeVisible()
})

test('教师可在 AI 评分复核页处理待处理异议', async ({ page }) => {
  const state = await appealsFixture(page, { role: 'teacher' })
  await page.goto('/teacher/ai-grading')

  await page.getByRole('button', { name: /待处理异议/ }).click()
  await expect(page.getByText('验收学员')).toBeVisible()
  await expect(page.getByText('作业提交 #3')).toBeVisible()

  await page.getByRole('button', { name: '维持' }).click()
  await expect(page.getByRole('dialog', { name: '处理成绩异议' })).toBeVisible()
  await page.getByPlaceholder('说明维持原因、改分依据或重评理由').fill('已复核，原分无误')
  await page.getByRole('button', { name: '确认处理' }).click()

  await expect.poll(() => state.handled.length).toBe(1)
  expect(state.handled[0]).toMatchObject({ action: 'uphold', comment: '已复核，原分无误' })
})

test('窄屏下通知中心无水平溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await appealsFixture(page, { role: 'student' })
  await page.goto('/student/notifications')

  await expect(page.getByRole('heading', { name: '通知中心' })).toBeVisible()
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement
    return doc.scrollWidth - doc.clientWidth
  })
  expect(overflow).toBeLessThanOrEqual(1)
})
