import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { layoutFixture } from './fixtures/layout.js'

const config = [{ key: 'score', title: '总评', weight: '100', columns: [
  { key: 'manual', title: '人工分', weight: '100', source_kind: 'manual' },
] }]
const row = id => ({ student_id: id, student_no: `000${id}`, student_name: `班级${id}学生`,
  cells: { manual: { state: 'scored', score: '80.00', source: { kind: 'manual' } } },
  calculated_score: '80.00', final_score: '80.00' })
const snapshot = id => ({ id: `snapshot-${id}`, revision: id, kind: 'published', reason: 'fixture',
  created_at: '2026-10-09T00:00:00Z', payload: { config, rows: [row(id)] } })

async function prepare(page) {
  await layoutFixture(page, 'teacher')
  await page.route('**/api/v1/gradebooks/**', route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1/gradebooks', '')
    if (path === '/offerings') return route.fulfill({ json: { items: [1, 2].map(id => ({
      id, course_title: '测试课', class_name: `班级${id}`, term_name: '秋季', read_only: false,
    })) } })
    for (const id of [1, 2]) {
      if (path === `/offerings/${id}`) return route.fulfill({ json: {
        offering_id: id, revision: 1, read_only: false, config, sources: [],
        draft: { rows: [row(id)], blockers: [], preview_hash: 'fixture' }, snapshot: snapshot(id),
      } })
      if (path === `/offerings/${id}/snapshots`) return route.fulfill({ json: { items: [snapshot(id)] } })
      if (path === `/snapshots/snapshot-${id}/export`) return route.fulfill({
        contentType: 'text/csv', body: `student,score\nclass-${id},80\n`,
      })
    }
    return route.fulfill({ status: 404, json: { detail: { message: '测试资源不存在' } } })
  })
}

async function selectSecondClass(page) {
  await page.getByRole('button', { name: '选择成绩所属授课班级', exact: true }).click()
  await page.getByRole('option', { name: '测试课 · 班级2 · 秋季', exact: true }).click()
  await expect(page.locator('.grade-table-scroll')).toContainText('班级2学生')
}

for (const width of [1440, 390]) {
  test(`旧班历史快照不会覆盖新班表格 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await prepare(page)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    let release
    const held = new Promise(resolve => { release = resolve })
    await page.route('**/api/v1/gradebooks/snapshots/snapshot-1', route => { release(route) })
    await page.goto('/teacher/gradebook')
    await expect(page.locator('.grade-table-scroll')).toContainText('班级1学生')
    await page.locator('.snapshot-list button').click()
    const delayed = await held
    await selectSecondClass(page)
    const response = page.waitForResponse('**/api/v1/gradebooks/snapshots/snapshot-1')
    await delayed.fulfill({ json: snapshot(1) })
    await (await response).finished()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    await expect(page.getByRole('button', { name: '选择成绩所属授课班级', exact: true })).toContainText('班级2')
    await expect(page.locator('.grade-table-scroll')).toContainText('班级2学生')
    await expect(page.locator('.grade-table-scroll')).not.toContainText('班级1学生')
    await expect(page.getByRole('heading', { name: '当前成绩草稿', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    await page.screenshot({ path: `/tmp/dai-step05-gradebook-context-${width}.png`, fullPage: true })
    expect(errors).toEqual([])
  })

  test(`切班后丢弃旧导出并下载当前版本 ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await prepare(page)
    let release
    const held = new Promise(resolve => { release = resolve })
    const downloads = []
    page.on('download', download => downloads.push(download.suggestedFilename()))
    await page.route('**/api/v1/gradebooks/snapshots/snapshot-1/export?*', route => { release(route) })
    await page.goto('/teacher/gradebook')
    await expect(page.locator('.grade-table-scroll')).toContainText('班级1学生')
    await page.getByRole('button', { name: '下载 CSV', exact: true }).click()
    const delayed = await held
    await selectSecondClass(page)
    const response = page.waitForResponse('**/api/v1/gradebooks/snapshots/snapshot-1/export?*')
    await delayed.fulfill({ contentType: 'text/csv', body: 'student,score\nclass-1,80\n' })
    await (await response).finished()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: '下载 CSV', exact: true }).click()
    const file = await download
    expect(file.suggestedFilename()).toBe('课程成绩-版本2.csv')
    expect(await readFile(await file.path(), 'utf8')).toContain('class-2,80')
    expect(downloads).toEqual(['课程成绩-版本2.csv'])
  })
}
