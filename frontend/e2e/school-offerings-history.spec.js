import { expect, test } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

// Deterministic UI workflow. Every API read/write is intercepted; no live DB is changed.
async function historyFixture(page, role) {
  await layoutFixture(page, role)
  const rows = [
    { id: 10, course_id: 1, academic_term_id: 1, teaching_class_id: 101, teacher_id: 7,
      course_title: '共享课程', term_name: '秋季', class_name: 'A班', teacher_name: '授课教师', status: 'active' },
    { id: 20, course_id: 1, academic_term_id: 1, teaching_class_id: 202, teacher_id: 8,
      course_title: '共享课程', term_name: '秋季', class_name: '已归档B班', teacher_name: '另一教师', status: 'archived' },
  ]
  const state = {
    reads: [], writes: [], failNext: false, failMembers: false,
    records: Array.from({ length: 21 }, (_, i) => ({ id: i + 1, title: `历史实验${i + 1}`,
      student_no: '000001', student_name: '本班学生', template_version_id: 1, can_confirm: true,
      source_offering_id: i === 20 ? 30 : null })),
    tasks: [{ id: 5, kind: 'assignment', title: '历史整班作业', participants: 1, unmatched: 0, can_confirm: true }],
  }
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/v1', '')
    const reply = json => route.fulfill({ json })
    state.reads.push(path)
    if (path === '/offerings') return reply({ items: role === 'admin' ? rows : rows.slice(0, 1) })
    if (/^\/offerings\/(10|20)\/students$/.test(path)) {
      if (path.includes('/20/') && state.failMembers) return route.fulfill({ status: 503, json: { detail: { message: '名单暂不可用' } } })
      return reply({ items: [{ id: 3, student_no: '000001', real_name: '本班学生', status: 'active' }] })
    }
    if (path === '/courses/1/teaching-templates') return reply({ tasks: [], experiments: [] })
    if (/^\/offerings\/(10|20)\/legacy-review$/.test(path)) {
      const pageNumber = Number(url.searchParams.get('page')), start = (pageNumber - 1) * 20
      if (pageNumber === 2 && state.failNext) {
        state.failNext = false
        return route.fulfill({ status: 503, json: { detail: { message: '历史数据暂不可用' } } })
      }
      return reply({ items: state.records.slice(start, start + 20), total: state.records.length,
        page: pageNumber, page_size: 20, tasks: state.tasks.slice(start, start + 20),
        task_totals: { assignment: state.tasks.length, exam: 0 } })
    }
    if (request.method() === 'POST' && path.endsWith('/transfer')) {
      const body = request.postDataJSON(); state.writes.push({ path, body })
      const id = Number(path.split('/')[4]); state.records = state.records.filter(item => item.id !== id)
      return reply({ ok: true })
    }
    if (request.method() === 'POST' && path.endsWith('/assign')) {
      state.writes.push({ path, body: request.postDataJSON() })
      if (path.includes('/legacy-tasks/')) state.tasks = []
      else state.records = state.records.filter(item => item.id !== Number(path.split('/')[4]))
      return reply({ ok: true })
    }
    return route.fallback()
  })
  return state
}

async function assertHealthy(page, errors, role, width) {
  await expect(page).toHaveURL(new RegExp(`/${role}/offerings`))
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  expect(errors).toEqual([])
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: `../.runtime/school-verification/offerings-${role}-${width}.png`, fullPage: true })
}

for (const role of ['admin', 'teacher', 'student']) {
  for (const width of [390, 1440]) {
    test(`授课安排历史操作与角色边界 ${role} ${width}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 })
      const errors = []; page.on('pageerror', error => errors.push(error.message))
      page.on('console', message => {
        // layoutFixture deliberately blocks remote fonts for deterministic offline checks.
        if (message.type() === 'error' && !message.location().url.startsWith('https://fonts.')) errors.push(message.text())
      })
      const state = await historyFixture(page, role)
      await page.goto(`/${role}/offerings`)
      await expect(page.getByRole('heading', { name: role === 'admin' ? '授课安排' : '我的教学班', exact: true })).toBeVisible()
      await page.getByRole('button', { name: /共享课程.*A班/ }).click()
      await expect(page.getByRole('link', { name: '课程成绩簿', exact: true })).toHaveAttribute('href', `/${role}/gradebook?offering_id=10`)
      if (role !== 'admin') {
        await expect(page.getByRole('heading', { name: '历史归属核对' })).toHaveCount(0)
        await expect(page.getByLabel('变更原因')).toHaveCount(0)
        expect(state.reads.some(path => path.includes('legacy-review'))).toBe(false)
        expect(state.writes).toEqual([])
      } else {
        await expect(page.getByRole('button', { name: '确认实验归属', exact: true }).first()).toBeDisabled()
        await page.getByLabel('变更原因').fill('核对原始名册和评分版本')
        await page.getByRole('button', { name: '下一页', exact: true }).click()
        await expect(page.getByText('第 2 页')).toBeVisible()
        await page.getByRole('button', { name: '明确转入历史实验成绩', exact: true }).click()
        await expect(page.getByRole('status')).toContainText('原提交和评分版本保留')
        expect(state.writes[0]).toEqual({ path: '/offerings/10/records/21/transfer',
          body: { reason: '核对原始名册和评分版本', source_offering_id: 30 } })
        await page.getByRole('button', { name: '上一页', exact: true }).click()
        await page.getByRole('button', { name: '确认整项任务归属', exact: true }).click()
        expect(state.writes[1].path).toBe('/offerings/10/legacy-tasks/assignment/5/assign')
        await expect(page.getByText('历史整班作业')).toHaveCount(0)
        await page.getByRole('button', { name: /共享课程.*已归档B班/ }).click()
        await page.getByLabel('变更原因').fill('归档保持只读')
        await expect(page.getByRole('button', { name: '确认实验归属', exact: true }).first()).toBeDisabled()
      }
      await assertHealthy(page, errors, role, width)
    })
  }
}

test('历史翻页失败保留已加载页，切换班级失败清除旧名单和历史', async ({ page }) => {
  const state = await historyFixture(page, 'admin')
  await page.goto('/admin/offerings')
  await page.getByRole('button', { name: /共享课程.*A班/ }).click()
  state.failNext = true
  await page.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('历史数据暂不可用')
  await expect(page.getByText('第 1 页')).toBeVisible()
  await expect(page.getByText('历史实验1 ·', { exact: false }).first()).toBeVisible()
  await page.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(page.getByText('第 2 页')).toBeVisible()
  state.failMembers = true
  await page.getByRole('button', { name: /共享课程.*已归档B班/ }).click()
  await expect(page.getByRole('alert')).toContainText('名单暂不可用')
  await expect(page.locator('tbody tr')).toHaveCount(0)
  await expect(page.getByText('历史实验21 ·', { exact: false })).toHaveCount(0)
})
