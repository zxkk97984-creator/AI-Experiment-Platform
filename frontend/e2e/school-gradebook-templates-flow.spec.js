// 批次 4 界面验收：课程成绩模板弹窗 + 成绩册待分配门禁与「恢复为课程默认」。
// 所有 /api/v1 请求都在浏览器内被本地 fixture 兑现；layoutFixture 先注册，本文件的
// page.route 后注册（Playwright 后注册优先），因此不需要改动共享 fixture。
import { test, expect } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { layoutFixture, longTitle } from './fixtures/layout.js'

const shotDirectory = process.env.GRADEBOOK_TEMPLATE_SCREENSHOT_DIR || '/tmp'
const COURSE = { id: 1, title: longTitle, description: '学习函数、数据处理与可重复实验。', status: 'published', chapter_count: 6, lesson_count: 24, student_count: 120, updated_at: '2026-09-21T08:00:00Z', teaching_classes: [] }
const TEMPLATE = [
  { key: 'regular', title: '平时成绩', weight: '20', weight_mode: 'equal' },
  { key: 'labs', title: '实验成绩', weight: '30', weight_mode: 'equal' },
  { key: 'midterm', title: '期中成绩', weight: '20', weight_mode: 'equal' },
  { key: 'final', title: '期末成绩', weight: '30', weight_mode: 'equal' },
]
const BOOK_CONFIG = [
  { key: 'regular', title: '平时成绩', weight: '100', weight_mode: 'custom', columns: [
    { key: 'manual-a', title: '课堂表现', weight: '60', source_kind: 'manual', source_id: null, selection: 'highest', allocation_status: 'allocated' },
    { key: 'assignment-5', title: '第一次作业', weight: null, source_kind: 'assignment', source_id: 5, selection: 'highest', allocation_status: 'pending' },
  ] },
]

function diagnostics(page) {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (!['error', 'warn'].includes(message.type())) return
    if (message.location().url?.includes('fonts.') || message.text().includes('net::ERR_FAILED')) return
    errors.push(message.text())
  })
  return errors
}

async function capture(page, name) {
  await mkdir(shotDirectory, { recursive: true })
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: join(shotDirectory, name), fullPage: true, animations: 'disabled' })
}

test('教师课程页打开成绩模板弹窗，比例合计校验并保存', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  const errors = diagnostics(page)
  await layoutFixture(page, 'teacher')
  const writes = []
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (path === '/courses') return route.fulfill({ json: { items: [COURSE], total: 1, summary: { total: 1, published: 1, draft: 0, archived: 0 } } })
    if (path === '/academic-terms') return route.fulfill({ json: { items: [{ id: 1, name: '2026-2027 学年第一学期（秋季）' }], total: 1 } })
    if (path === '/gradebooks/courses/1/default-config' && request.method() === 'GET') {
      return route.fulfill({ json: { course_id: 1, customized: false, categories: TEMPLATE } })
    }
    if (path === '/gradebooks/courses/1/default-config' && request.method() === 'PUT') {
      writes.push(request.postDataJSON())
      return route.fulfill({ json: { course_id: 1, customized: true, categories: request.postDataJSON().categories } })
    }
    return route.fallback()
  })
  await page.goto('/teacher/courses')
  await expect(page.getByRole('heading', { name: '课程管理', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '成绩模板', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '课程成绩模板' })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.template-row')).toHaveCount(4)
  const inputs = dialog.locator('.template-row input[type="number"]')
  await inputs.nth(1).fill('40')
  await expect(dialog.getByText('分类占比合计 110%（须等于 100%）')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '保存课程模板' })).toBeDisabled()
  await inputs.nth(1).fill('30')
  await dialog.getByRole('button', { name: '保存课程模板' }).click()
  await expect(page.getByText('课程成绩模板已保存，仅对之后新开的班生效', { exact: true })).toBeVisible()
  expect(writes).toHaveLength(1)
  expect(writes[0].categories.map(row => [row.key, String(row.weight), row.weight_mode])).toEqual([
    ['regular', '20', 'equal'], ['labs', '30', 'equal'], ['midterm', '20', 'equal'], ['final', '30', 'equal']])
  await capture(page, 'batch4-course-template-1440.png')
  expect(errors).toEqual([])
})

test('成绩册：待分配分项拦住发布，恢复课程默认需要原因与确认', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  const errors = diagnostics(page)
  await layoutFixture(page, 'teacher')
  const writes = []
  const student = { student_id: 9, student_no: '20260001', student_name: '张同学',
    cells: { 'manual-a': { state: 'scored', score: '80.00', source: { kind: 'manual' }, message: '' },
             'assignment-5': { state: 'pending', score: null, source: null, message: '待分配' } },
    calculated_score: null, final_score: null, adjustment: null }
  const book = () => ({ offering_id: 31, revision: 3, read_only: false, config: BOOK_CONFIG, sources: [],
    draft: { rows: [student], config: BOOK_CONFIG, book_revision: 3,
      blockers: [{ student_id: 9, column: 'assignment-5', code: 'UNALLOCATED_WEIGHTS', message: '自动关联分项「第一次作业」尚未分配栏目内比例' }],
      warnings: [], preview_hash: 'a'.repeat(64) },
    snapshot: null })
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (!path.startsWith('/gradebooks')) return route.fallback()
    if (request.method() === 'GET') {
      if (path === '/gradebooks/offerings') return route.fulfill({ json: { items: [{ id: 31, course_title: '人工智能通识实践', class_name: '一班', term_name: '2026 秋季学期', status: 'active', read_only: false }] } })
      if (path === '/gradebooks/offerings/31') return route.fulfill({ json: book() })
      if (path === '/gradebooks/offerings/31/snapshots') return route.fulfill({ json: { items: [] } })
      return route.fulfill({ json: { detail: { message: '未知 fixture 请求' } } })
    }
    writes.push({ path, body: request.postDataJSON() })
    if (path === '/gradebooks/offerings/31/config/restore-default') {
      const restored = { ...book(), revision: 4, config: TEMPLATE.map(row => ({ ...row, columns: [{ key: `${row.key}-main`, title: row.title, weight: null, source_kind: 'manual', source_id: null, selection: 'highest', allocation_status: 'allocated' }] })) }
      return route.fulfill({ json: restored })
    }
    return route.fulfill({ json: { detail: { message: '未知 fixture 请求' } } })
  })
  await page.goto('/teacher/gradebook')
  // 草稿已在修订 3：栏目面板默认折叠，先展开再核对。
  await page.locator('details.account-panel summary').first().click()
  await expect(page.getByText('一级比例合计 100%', { exact: true })).toBeVisible()
  await expect(page.getByText(/有 1 个自动关联分项尚未分配栏目内比例/)).toBeVisible()
  await expect(page.getByRole('button', { name: '发布新成绩版本', exact: true })).toBeDisabled()
  await capture(page, 'batch4-gradebook-pending-1440.png')
  await page.getByLabel('恢复为课程默认的原因').fill('本学期改用课程统一模板')
  await page.getByRole('button', { name: '恢复为课程默认', exact: true }).click()
  await expect(page.getByText(/将用课程模板重建本班栏目与比例/)).toBeVisible()
  await page.getByRole('button', { name: '确认恢复', exact: true }).click()
  await expect(page.getByText('已按课程默认模板重建本班栏目与比例，请重新核对分项。', { exact: true })).toBeVisible()
  expect(writes).toEqual([{ path: '/gradebooks/offerings/31/config/restore-default',
    body: { expected_revision: 3, reason: '本学期改用课程统一模板' } }])
  await capture(page, 'batch4-gradebook-restored-1440.png')
  expect(errors).toEqual([])
})
