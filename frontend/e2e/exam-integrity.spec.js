import { expect, test } from '@playwright/test'

import { examIntegrityFixture, proctorFixture } from './fixtures/exam-integrity.js'

// 批次 3 考试秩序：隐私告知、判断题作答、换机后旧设备被拒、教师只读监考页。
// 全部请求本地桩接，不写真实后端。

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 })
})

test('考试页展示监考与隐私告知，判断题按 T/F 作答', async ({ page }) => {
  const requests = await examIntegrityFixture(page)
  await page.goto('/student/exams/4')

  await expect(page.getByText('监考记录说明')).toBeVisible()
  await expect(page.getByText('不会自动认定作弊，也不会自动扣分')).toBeVisible()
  await expect(page.getByText('统一时间交卷')).toBeVisible()
  await expect(page.getByText('Python 的 list 是可变的。')).toBeVisible()

  await page.getByRole('radio').first().check()
  await expect.poll(() => requests.saves.length).toBeGreaterThan(0)
  const save = requests.saves.at(-1)
  expect(save.expected_write_generation).toBe(1)
  expect(save.answers[0]).toMatchObject({ question_id: 11, selected_options: ['T'] })
})

test('换机后旧设备写入被 409 拒绝时停止保存并提示', async ({ page }) => {
  const requests = await examIntegrityFixture(page, {
    onAnswerSave: async (_body, route) => {
      await route.fulfill({
        status: 409,
        json: { detail: { code: 'DEVICE_SUPERSEDED', message: '本设备已失去答题写入权限，请在新设备继续作答' } },
      })
      return true
    },
  })
  await page.goto('/student/exams/4')
  await page.getByRole('radio').first().check()

  await expect(page.getByText('本设备已失去答题写入权限')).toBeVisible()
  const writes = requests.saves.length
  await page.getByRole('radio').nth(1).check()
  await page.waitForTimeout(1200)
  expect(requests.saves.length).toBe(writes) // 失去权限后不再自动保存
})

test('教师监考页只读展示切屏、离开时长与换机时间线', async ({ page }) => {
  await proctorFixture(page, { examId: 9 })
  await page.goto('/teacher/exams/9/proctor')

  await expect(page.getByText('监考视图')).toBeVisible()
  await expect(page.getByText('不自动认定作弊、也不自动扣分')).toBeVisible()
  await expect(page.getByText('切屏次数')).toBeVisible()
  await expect(page.getByText('换机次数')).toBeVisible()
  await expect(page.getByText('换机接管')).toBeVisible()
  await expect(page.getByText('回到页面')).toBeVisible()
  await expect(page.getByText('验收学员 1')).toBeVisible()
})
