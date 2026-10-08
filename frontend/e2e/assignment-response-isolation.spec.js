import { test, expect } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

const pageErrors = new WeakMap()
test.beforeEach(async ({ page }) => {
  pageErrors.set(page, [])
  page.on('pageerror', error => pageErrors.get(page).push(error.message))
})
test.afterEach(async ({ page }) => {
  expect(pageErrors.get(page)).toEqual([])
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
})

// All API traffic is local fixture data, including the deliberately delayed requests.
async function prepare(page, submissions = []) {
  await layoutFixture(page, 'student')
  await page.route('**/api/v1/assignments/1/questions', route => route.fulfill({ json: [10, 20].map(id => ({
    id, title: `题目 ${id}`, description: `完成题目 ${id}`, starter_code: `starter_${id}`,
    public_cases: [], grading_mode: 'legacy',
  })) }))
  await page.route('**/api/v1/assignments/1/my-progress', route => route.fulfill({ json: { items: submissions.map(s => ({ ...s, latest_submission_id: s.id, completed: ['accepted', 'graded'].includes(s.status) })) } }))
}

async function deferredRoute(page, pattern) {
  let release
  const received = new Promise(resolve => { release = resolve })
  await page.route(pattern, route => { release(route) })
  return { received }
}

for (const width of [1440, 390]) {
  test(`切题后迟到历史不显示在当前题 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await prepare(page, [{ id: 200, question_id: 20, status: 'wrong_answer' }])
    const { received: held } = await deferredRoute(page, '**/api/v1/judge/submissions/200/result')
    await page.goto('/student/assignments/1')
    await expect(page.locator('#code-editor')).toHaveValue('starter_10')
    await page.locator('.q-dot').nth(1).click()
    const delayed = await held
    await page.locator('.q-dot').nth(0).click()
    const response = page.waitForResponse('**/api/v1/judge/submissions/200/result')
    await delayed.fulfill({ json: { id: 200, question_id: 20, code: 'saved_20', status: 'wrong_answer' } })
    await (await response).finished()
    await page.locator('.bottom-tab').filter({ hasText: '提交' }).click()
    await expect(page.locator('.q-dot.active')).toHaveText('1')
    await expect(page.locator('#code-editor')).toHaveValue('starter_10')
    await expect(page.locator('.submit-result-card')).toHaveCount(0)
    await expect(page.locator('.btn-submit-code')).toBeEnabled()
  })

  test(`切题后迟到自测不污染当前题 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await prepare(page)
    const { received: held } = await deferredRoute(page, '**/api/v1/judge/questions/10/sample-run')
    await page.goto('/student/assignments/1')
    await expect(page.locator('#code-editor')).toHaveValue('starter_10')
    await page.locator('.btn-self-test').click()
    const delayed = await held
    await page.locator('.q-dot').nth(1).click()
    const response = page.waitForResponse('**/api/v1/judge/questions/10/sample-run')
    await delayed.fulfill({ json: { status: 'runtime_error', diagnostic: { message: '旧题诊断不得出现在新题' } } })
    await (await response).finished()
    await expect(page.locator('.q-dot.active')).toHaveText('2')
    await expect(page.locator('.btn-self-test')).toBeEnabled()
    await expect(page.locator('.terminal-panel')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    await page.screenshot({ path: `/tmp/dai-step04-response-isolation-${width}.png`, fullPage: true })
  })
}

test('离开后迟到的运行中历史不会重新开始轮询', async ({ page }) => {
  await page.clock.install()
  await prepare(page, [{ id: 100, question_id: 10, status: 'running' }])
  let requests = 0
  let release
  const held = new Promise(resolve => { release = resolve })
  await page.route('**/api/v1/judge/submissions/100/result', async route => {
    requests += 1
    if (requests === 1) release(route)
    else await route.fulfill({ json: { id: 100, status: 'running' } })
  })
  await page.goto('/student/assignments/1')
  const delayed = await held
  await expect(page.locator('#code-editor')).toHaveValue('starter_10')
  await page.locator('.back-btn').click()
  await expect(page).toHaveURL(/\/student\/courses\/1$/)
  const response = page.waitForResponse('**/api/v1/judge/submissions/100/result')
  await delayed.fulfill({ json: { id: 100, question_id: 10, code: 'saved_10', status: 'running' } })
  await (await response).finished()
  await page.clock.runFor(2500)
  expect(requests).toBe(1)
})
