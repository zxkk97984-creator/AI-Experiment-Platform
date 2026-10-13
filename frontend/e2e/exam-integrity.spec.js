import { expect, test } from '@playwright/test'

import { examIntegrityFixture, proctorFixture } from './fixtures/exam-integrity.js'
import { longTitle } from './fixtures/layout.js'

// 批次 3 考试秩序：隐私告知、判断题作答、换机后旧设备被拒、教师只读监考页。
// 全部请求本地桩接，不写真实后端。
// 定位器一律收敛到具体容器：同一句话在标题与正文里都会出现，全局 getByText 会命中多个元素。

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 })
})

test('考试页展示监考与隐私告知，判断题按 T/F 作答', async ({ page }) => {
  const requests = await examIntegrityFixture(page)
  await page.goto('/student/exams/4')

  const notice = page.locator('.integrity-note')
  await expect(notice).toBeVisible()
  // 「统一时间交卷」既出现在模式标题（strong）也出现在说明句（p），必须限定到标题元素
  await expect(notice.locator('strong').first()).toHaveText('统一时间交卷')
  await expect(notice).toContainText('统一收卷') // unified 说明句（per_student 下换成「完整…分钟」）
  await expect(notice).toContainText('不会自动认定作弊，也不会自动扣分')
  await expect(notice).toContainText('监考记录说明')
  await expect(notice).toContainText('IP / 浏览器标识仅保存不可逆摘要')
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

  // 横幅提示（页面内）而不是 toast：toast 里有同义文案，必须限定到 .integrity-banner
  const banner = page.locator('.integrity-banner')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('本设备已失去答题写入权限')
  await expect(banner).toContainText('请在新设备继续作答')

  // 失去权限后页面转为只读：选项禁用，且不再产生任何写入
  const writes = requests.saves.length
  await expect(page.getByRole('radio').nth(1)).toBeDisabled()
  await page.waitForTimeout(1200)
  expect(requests.saves.length).toBe(writes)
})

test('教师监考页只读展示切屏、离开时长与换机时间线', async ({ page }) => {
  const { calls } = await proctorFixture(page, { examId: 9 })
  await page.goto('/teacher/exams/9/proctor')

  // 标题是考试名；页面标识用 eyebrow，避免与「监考视图」占位文案混淆
  await expect(page.locator('.page-heading h1')).toHaveText(longTitle)
  await expect(page.locator('.eyebrow')).toHaveText('PROCTOR')

  const note = page.locator('.readonly-note')
  await expect(note).toContainText('只读观测')
  await expect(note).toContainText('不自动认定作弊、也不自动扣分')
  await expect(note).toContainText('不会自动交卷')

  const totals = page.locator('.totals')
  await expect(totals).toContainText('参与学生')
  await expect(totals).toContainText('切屏次数')
  await expect(totals).toContainText('换机次数')
  await expect(totals).toContainText('12 秒') // 累计离开 12000ms

  const timeline = page.locator('.timeline')
  await expect(timeline).toContainText('换机接管')
  await expect(timeline).toContainText('回到页面')
  await expect(timeline).toContainText('离开页面（切屏）')

  const table = page.locator('table')
  await expect(table).toContainText('验收学员 1')
  await expect(table).toContainText('作答中')
  expect(calls()).toBeGreaterThan(0)

  // 只读观测页只有刷新按钮，没有任何写操作入口
  await expect(page.locator('.page-heading').getByRole('button', { name: '刷新' })).toBeVisible()
  expect(page.locator('button:has-text("保存")')).toHaveCount(0)
})
