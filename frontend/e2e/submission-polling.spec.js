import { test, expect } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

for (const width of [1440, 390]) {
  test(`不存在的提交停止轮询并支持重试 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.clock.install()
    await layoutFixture(page, 'student')
    let calls = 0
    let available = false
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/v1/judge/submissions/1/result', route => {
      calls++
      return available ? route.fulfill({ json: { id: 1, status: 'accepted', score: 80 } })
        : route.fulfill({ status: 404, json: { detail: { message: '提交不存在' } } })
    })
    await page.goto('/student/submissions/1')
    await expect(page.getByRole('alert')).toContainText('提交不存在')
    await page.clock.runFor(10000)
    expect(calls).toBe(1)
    await expect(page.getByText('正在读取提交结果')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    await page.screenshot({ path: `/tmp/dai-step10-poll-error-${width}.png`, fullPage: true })
    available = true
    await page.getByRole('button', { name: '重试', exact: true }).click()
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.locator('.submission-panel')).toContainText('80')
    await page.clock.runFor(10000)
    expect(calls).toBe(2)
    expect(errors).toEqual([])
  })
}
