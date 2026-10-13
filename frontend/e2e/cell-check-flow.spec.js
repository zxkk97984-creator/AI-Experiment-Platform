import { test, expect } from '@playwright/test'
import { layoutFixture, notebook } from './fixtures/layout.js'

// Browser interaction acceptance with every API intercepted; no real accounts,
// grades or classroom data are changed by this fixture.
for (const width of [320, 1440]) {
  test(`Cell feedback, version invalidation and anonymous offering board ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    await layoutFixture(page, 'student')
    const errors = []
    const writes = []
    let scoped = false
    page.on('pageerror', error => errors.push(error.message))
    const config = { task_type: 'notebook', grade_selection: 'highest', max_submissions: 5,
      criteria: [{ id: 'point', source: 'cell', name: '计算结果', description: '完成数值计算', max_score: 100, levels: [] }],
      cell_checks: [{ id: 'point', cell_id: 'code' }] }
    await page.route('**/api/v1/**', async route => {
      const req = route.request()
      const url = new URL(req.url())
      const path = url.pathname.replace('/api/v1', '')
      const reply = json => route.fulfill({ json })
      if (req.method() !== 'GET') writes.push(path)
      if (path === '/experiments/records/ensure-for-module/3') {
        scoped = url.searchParams.get('offering_id') === '11'
        return reply({ id: 99, record_revision: 1, offering_id: 11 })
      }
      if (path === '/experiments/records/99') return reply({ ...notebook, offering_id: 11, lab_enabled: true })
      if (path === '/experiments/records/99/cells') return reply({ record_revision: 2 })
      // 异步执行契约：POST 只受理（202 + execution_id），结果由 GET executions/{id} 轮询取得；
      // 刷新恢复会先问 executions/active。
      if (path === '/experiments/records/99/executions/active') return reply({ execution: null })
      if (path === '/experiments/records/99/cells/code/execute') return reply({ execution_id: 77, status: 'queued', cell_id: 'code', timeout_seconds: 120, queue_position: 1 })
      if (path === '/experiments/records/99/executions/77') return reply({
        id: 77, record_id: 99, cell_id: 'code', status: 'succeeded', timeout_seconds: 120,
        outputs: [{ msg_type: 'stream', content: { text: '7\n' } }], execution_time_ms: 12, execution_count: 1,
        error_code: null, error_message: null, state_lost: false, timed_out: false, attempts: 1,
      })
      if (path === '/labs/records/99/overview') return reply({ enabled: true, assessment: { id: 1, version: 1, config }, attempts_used: 0, max_submissions: 5, artifacts: [], submissions: [], runs: [] })
      if (path === '/labs/records/99/cells/code/check') return reply({ id: 50, kind: 'cell_check', status: 'queued' })
      if (path === '/labs/runs/50') return reply({ id: 50, status: 'succeeded', stale: false, result: { cell_checks: { point: { cell_id: 'code', passed: true, message: '检查通过' } } } })
      if (path === '/labs/records/99/leaderboard') return reply({ named: false, assessment_version: 1, grade_selection: 'highest', metric: null, items: [
        { rank: 1, display_name: '同学 ABC123', is_me: true, score: 90, program_elapsed_seconds: 2 },
        { rank: 1, display_name: '同学 DEF456', is_me: false, score: 90, program_elapsed_seconds: 3 },
      ] })
      return route.fallback()
    })
    await page.goto('/student/experiments/3?offering_id=11')
    await expect(page.getByRole('heading', { name: '训练与评测' })).toBeVisible()
    expect(scoped).toBe(true)
    await page.getByRole('button', { name: '运行', exact: true }).click()
    await expect(page.getByText('单元格检查通过', { exact: false })).toBeVisible()
    expect(writes.some(path => path.endsWith('/submit'))).toBe(false)
    await page.locator('.cm-content').fill('result=8')
    await expect(page.getByText('代码已修改，请重新运行检查', { exact: false })).toBeVisible()
    await page.getByText('本班实验排行榜', { exact: true }).click()
    await page.getByRole('button', { name: '查看排行榜', exact: true }).click()
    await expect(page.locator('.lab-leaderboard tbody tr')).toHaveCount(2)
    await expect(page.getByText('同学 ABC123（我）', { exact: true })).toBeVisible()
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    const sizes = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
    expect(sizes.scroll).toBeLessThanOrEqual(sizes.width + 1)
    expect(errors).toEqual([])
    await page.screenshot({ path: `/tmp/cell-check-flow-${width}.png`, fullPage: true })
  })
}
