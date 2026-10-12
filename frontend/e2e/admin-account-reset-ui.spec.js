import { test, expect } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

for (const width of [1542, 390]) {
  test(`Excel 重置预览与两侧面板排版 ${width}px`, async ({ page }) => {
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    await page.setViewportSize({ width, height: 900 })
    await layoutFixture(page, 'admin')
    await page.route('**/api/v1/admin/batches?*', route => route.fulfill({ json: { items: [], total: 0 } }))
    const batch = { id: 'ui-reset', actor_id: 7, kind: 'password_reset', status: 'preview', preview_digest: 'fixture', total: 2, scope: { selection: 'excel' },
      summary: { selected: 2, eligible: 1, skipped: 0, errors: 1 },
      items: [{ row_number: 2, data: { student_no: '0001', real_name: '测试学生' }, status: 'pending', result: { message: '重置后旧登录失效，必须改密' } },
        { row_number: 3, data: { student_no: '9999', real_name: '未知学生' }, status: 'pending', result: { errors: ['学号不存在，请先核对学生账号'] } }] }
    let uploads = 0
    await page.route('**/api/v1/admin/password-resets/preview-file', async route => {
      uploads += 1
      expect(route.request().headers()['content-type']).toContain('multipart/form-data; boundary=')
      expect(route.request().postDataBuffer().toString()).toContain('reset.xlsx')
      await route.fulfill({ json: batch })
    })
    await page.route('**/api/v1/admin/batches/ui-reset?*', route => route.fulfill({ json: batch }))
    await page.goto('/admin/accounts')
    const panel = page.locator('.account-panel--operation').nth(1)
    await panel.locator('select').first().selectOption('filter')
    await expect(panel.getByRole('button', { name: '预览重置名单' })).toBeDisabled()
    await panel.locator('input[type=file]').setInputFiles({ name: 'reset.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('synthetic fixture') })
    if (width > 900) {
      const boxes = await page.locator('.account-panel--operation').evaluateAll(nodes => nodes.map(n => ({ top: n.getBoundingClientRect().top, height: n.getBoundingClientRect().height })))
      expect(boxes[0].top).toBe(boxes[1].top)
      expect(boxes[0].height).toBe(boxes[1].height)
      const actions = await page.locator('.operation-submit').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().bottom))
      expect(actions[0]).toBe(actions[1])
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: `/tmp/dai-accounts-excel-${width}.png`, fullPage: true })
    await panel.getByRole('button', { name: '预览重置名单' }).click()
    await expect(page).toHaveURL(/\/admin\/batches\/ui-reset$/)
    await expect(page.getByText('错误行：1', { exact: true })).toBeVisible()
    await expect(page.getByText('名单行：2', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '确认并后台执行' })).toBeDisabled()
    await expect(page.getByText('学号不存在，请先核对学生账号', { exact: true })).toBeVisible()
    await expect(page.getByLabel('管理员当前密码')).toHaveValue('')
    expect(uploads).toBe(1)
    expect(errors).toEqual([])
  })

  test(`授课安排表单布局与选择状态 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await layoutFixture(page, 'admin')
    await page.route('**/api/v1/offerings', route => route.fulfill({ json: { items: [] } }))
    await page.route('**/api/v1/users?*', route => route.fulfill({ json: { items: [{ id: 3, real_name: '授课教师', username: 'teacher-demo' }], total: 1 } }))
    await page.goto('/admin/offerings')
    const form = page.locator('.offering-create')
    await expect(form.getByRole('button', { name: '创建并加入班级名单' })).toBeDisabled()
    await expect(form.getByRole('button', { name: '选择教学班' })).toBeDisabled()
    await form.locator('select').selectOption('1')
    await form.getByRole('button', { name: '选择授课教师' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByText('授课教师 · teacher-demo', { exact: true }).click()
    await dialog.getByRole('button', { name: '确认选择' }).click()
    await expect(form.getByRole('button', { name: '选择授课教师' })).toContainText('teacher-demo')
    await expect(form.getByRole('button', { name: '创建并加入班级名单' })).toBeDisabled()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: `/tmp/dai-offering-create-${width}.png`, fullPage: true })
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  })
}
