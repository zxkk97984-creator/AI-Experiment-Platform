import { test, expect } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

// Every business request is intercepted by layoutFixture or the overrides below.
// No credentials or writes reach the business backend.
for (const width of [1440, 390]) {
  test(`作业切题保留草稿并确认离开 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await layoutFixture(page, 'student')
    await page.route('**/api/v1/assignments/1/questions', route => route.fulfill({ json: [10, 20].map(id => ({
      id, title: `题目 ${id}`, description: '完成代码后提交', starter_code: `starter_${id}`,
      public_cases: [], grading_mode: 'legacy',
    })) }))
    const pageErrors = []
    const consoleErrors = []
    page.on('pageerror', error => pageErrors.push(error.message))
    page.on('console', message => {
      if (message.type() === 'error' && !message.text().includes('net::ERR_FAILED')) consoleErrors.push(message.text())
    })

    await page.goto('/student/assignments/1')
    await expect(page).toHaveURL(/\/student\/assignments\/1$/)
    await expect(page.locator('h1')).toContainText('Python')
    await expect(page.locator('#code-editor')).toHaveValue('starter_10')
    await page.locator('#code-editor').fill('draft_10')
    await page.locator('.q-dot').nth(1).click()
    await page.locator('#code-editor').fill('draft_20')
    await page.locator('.q-dot').nth(0).click()
    await expect(page.locator('#code-editor')).toHaveValue('draft_10')
    await page.locator('.back-btn').click()
    const dialog = page.getByRole('dialog', { name: '有尚未提交的代码' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('未提交的修改仅保留在当前页面')
    await expect(page).toHaveURL(/\/student\/assignments\/1$/)
    const bounds = await dialog.boundingBox()
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
    await page.screenshot({ path: `/tmp/dai-step03-draft-dialog-${width}.png`, fullPage: true })
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(page.locator('#code-editor')).toHaveValue('draft_10')
    await page.locator('.q-dot').nth(1).click()
    await expect(page.locator('#code-editor')).toHaveValue('draft_20')
    await page.locator('.back-btn').click()
    await dialog.getByRole('button', { name: '放弃草稿并离开' }).click()
    await expect(page).toHaveURL(/\/student\/courses\/1$/)
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(pageErrors).toEqual([])
    expect(consoleErrors).toEqual([])
  })
}

for (const width of [1440, 390]) {
  test(`当前作业恢复旧提交及历史完成标记 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await layoutFixture(page, 'student')
    await page.route('**/api/v1/assignments/1/questions', route => route.fulfill({ json: [2, 3].map(id => ({
      id, title: `题目 ${id}`, description: '恢复进度', starter_code: '', public_cases: [], grading_mode: 'legacy',
    })) }))
    await page.route('**/api/v1/assignments/1/my-progress', route => route.fulfill({ json: { items: [
      { question_id: 2, latest_submission_id: 7, status: 'wrong_answer', completed: true },
    ] } }))
    await page.route('**/api/v1/judge/submissions/7/result', route => route.fulfill({ json: {
      id: 7, question_id: 2, status: 'wrong_answer', code: 'old_saved_code',
    } }))
    await page.goto('/student/assignments/1')
    await expect(page.locator('#code-editor')).toHaveValue('old_saved_code')
    await expect(page.locator('.q-dot.done')).toHaveCount(1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  })
}
