import { test, expect } from '@playwright/test'
import { createHash } from 'node:crypto'
import { mkdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { layoutFixture } from './fixtures/layout.js'

// Every /api/v1 request is fulfilled in this browser test, including all writes.
// This proves UI behavior with deterministic data, not real-service authorization.
const config = [['regular', '平时成绩', '30'], ['labs', '实验成绩', '40'], ['exam', '考核成绩', '30']].map(([key, title, weight]) => ({
  key, title, weight, columns: [{ key: `${key}-main`, title, weight: '100', source_kind: 'manual', source_id: null, selection: 'highest' }],
}))
const students = [{ student_id: 7, student_no: '20260001', student_name: '张同学' },
  { student_id: 8, student_no: '20260002', student_name: '李同学（班内私有成绩）' }]
const shotDirectory = process.env.GRADEBOOK_SCREENSHOT_DIR || '/tmp'

async function fixture(page, role) {
  await layoutFixture(page, role)
  const state = { revision: role === 'student' ? 3 : 0, config: structuredClone(config), entries: {}, adjustment: null,
    snapshots: [], latest: null, writes: [], reads: [], conflictNext: false, sourceVersion: 1 }
  const columns = () => state.config.flatMap(category => category.columns)
  const fingerprint = () => createHash('sha256').update(JSON.stringify({ config: state.config, entries: state.entries, adjustment: state.adjustment, sourceVersion: state.sourceVersion })).digest('hex')
  function draft() {
    const rows = students.map(student => {
      const cells = Object.fromEntries(columns().map(col => {
        const entry = state.entries[`${student.student_id}:${col.key}`]
        return [col.key, entry ? { state: entry.state, score: entry.state === 'scored' ? Number(entry.score).toFixed(2) : entry.state === 'missing' ? '0.00' : null,
          source: { kind: 'manual', reason: entry.reason || '' }, message: entry.reason || '' }
          : { state: 'pending', score: null, source: null, message: '人工成绩待录入' }]
      }))
      let total = 0
      const complete = Object.values(cells).every(cell => cell.score !== null)
      if (complete) for (const category of state.config) for (const col of category.columns) total += Number(cells[col.key].score) * Number(col.weight) / 100 * Number(category.weight) / 100
      const adjustment = state.adjustment?.student_id === student.student_id ? state.adjustment : null
      return { ...student, cells, calculated_score: complete ? total.toFixed(2) : null,
        final_score: complete ? Number(adjustment?.score ?? total).toFixed(2) : null,
        adjustment: adjustment ? { score: Number(adjustment.score).toFixed(2), reason: adjustment.reason } : null }
    })
    return { offering_id: 31, book_revision: state.revision, config: structuredClone(state.config), rows,
      blockers: rows.flatMap(row => Object.entries(row.cells).filter(([, cell]) => cell.score === null)
        .map(([column]) => ({ student_id: row.student_id, column, message: '人工成绩待录入' }))), preview_hash: fingerprint() }
  }
  function snapshot(kind, reason, source = null) {
    const payload = source ? structuredClone(source.payload) : draft()
    const row = { id: `grade-${kind}-${state.revision}`, revision: state.revision, kind,
      created_at: '2026-10-03T12:00:00Z', reason, payload,
      source_snapshot_id: source?.id || null, payload_hash: createHash('sha256').update(JSON.stringify(payload)).digest('hex') }
    state.snapshots.unshift(row)
    return row
  }
  function filteredSnapshot(row) {
    if (!row) return null
    const result = structuredClone(row)
    if (role === 'student') { result.payload.rows = result.payload.rows.filter(item => item.student_id === 7); delete result.payload.blockers }
    return result
  }
  function readBook() {
    if (role === 'student') return { offering_id: 31, snapshot: filteredSnapshot(state.latest) }
    return { offering_id: 31, revision: state.revision, read_only: false, config: state.config,
      sources: [], draft: state.revision ? draft() : null, snapshot: filteredSnapshot(state.latest) }
  }
  if (role === 'student') {
    for (const student of students) for (const col of columns()) state.entries[`${student.student_id}:${col.key}`] = {
      state: 'scored', score: student.student_id === 7 ? 81 : 60, reason: null }
    state.latest = snapshot('published', '教师审核发布')
  } else {
    // A second learner already has complete scores; tests edit only the chosen learner.
    for (const col of columns()) state.entries[`8:${col.key}`] = { state: 'scored', score: 60, reason: null }
  }
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/v1', '')
    if (!path.startsWith('/gradebooks')) return route.fallback()
    const reply = (json, status = 200) => route.fulfill({ status, json })
    if (request.method() === 'GET') state.reads.push(path)
    else state.writes.push({ path, body: request.postDataJSON() })
    if (path === '/gradebooks/offerings') return reply({ items: [{ id: 31, course_title: '人工智能通识实践', class_name: '一班', term_name: '2026 秋季学期', status: 'active', read_only: false }] })
    if (path === '/gradebooks/offerings/31' && request.method() === 'GET') return reply(readBook())
    if (path === '/gradebooks/offerings/31/snapshots') return reply({ items: state.snapshots.map(({ id, revision, kind, created_at, reason }) => ({ id, revision, kind, created_at, reason })) })
    if (path.startsWith('/gradebooks/snapshots/')) {
      const id = path.split('/')[3]
      const item = state.snapshots.find(row => row.id === id)
      if (!item) return reply({ detail: { message: '成绩版本不存在' } }, 404)
      if (path.endsWith('/export')) {
        const rows = filteredSnapshot(item).payload.rows
        return route.fulfill({ contentType: 'text/csv; charset=utf-8',
          headers: { 'Content-Disposition': 'attachment; filename="fixture-grades.csv"' },
          body: '\ufeff学号,姓名,最终总分\n' + rows.map(row => `${row.student_no},${row.student_name},${row.final_score}`).join('\n') })
      }
      return reply(filteredSnapshot(item))
    }
    if (role === 'student') return reply({ detail: { message: '没有权限修改成绩' } }, 403)
    const body = request.postDataJSON()
    if (body.expected_revision !== state.revision) return reply({ detail: { message: '成绩簿已被其他操作修改，请刷新后重试' } }, 409)
    if (path.endsWith('/config')) { state.config = structuredClone(body.categories); state.revision++; return reply(readBook()) }
    if (path.endsWith('/entries')) {
      for (const entry of body.entries) state.entries[`${entry.student_id}:${entry.column_key}`] = structuredClone(entry)
      state.revision++; return reply(readBook())
    }
    if (path.endsWith('/adjustment')) { state.adjustment = body.score === null ? null : structuredClone(body); state.revision++; return reply(readBook()) }
    if (path.endsWith('/publish')) {
      if (state.conflictNext) { state.conflictNext = false; state.sourceVersion++; return reply({ detail: { message: '学生名单或来源成绩已变化，请刷新草稿并重新审核' } }, 409) }
      if (body.expected_preview_hash !== fingerprint()) return reply({ detail: { message: '成绩草稿来源摘要已变化' } }, 409)
      if (draft().blockers.length) return reply({ detail: { message: '存在待评分分项，不能发布' } }, 409)
      state.revision++; state.latest = snapshot('published', body.reason); return reply(filteredSnapshot(state.latest), 201)
    }
    if (path.endsWith('/archive')) { state.revision++; return reply(filteredSnapshot(snapshot('archive', body.reason, state.latest)), 201) }
    return reply({ detail: { message: '未知fixture请求' } }, 404)
  })
  return state
}

function diagnostics(page) {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (!['error', 'warn'].includes(message.type())) return
    // Font-CDN requests are deliberately aborted by layoutFixture.
    if (message.location().url?.includes('fonts.') || message.text().includes('net::ERR_FAILED')) return
    errors.push(message.text())
  })
  return errors
}
async function checkPage(page, role, width) {
  await expect(page).toHaveURL(new RegExp(`/${role}/gradebook`))
  expect(await page.title()).toBeTruthy()
  await expect(page.getByRole('heading', { name: '课程成绩管理', exact: true })).toBeVisible()
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  const layout = await page.evaluate(() => ({ width: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }))
  expect(layout.width).toBe(width)
  expect(layout.document).toBeLessThanOrEqual(width + 1)
  expect(layout.body).toBeLessThanOrEqual(width + 1)
}
async function capture(page, name) {
  await mkdir(shotDirectory, { recursive: true })
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: join(shotDirectory, name), fullPage: true, animations: 'disabled' })
}
async function editFirstStudent(page) {
  await page.getByRole('button', { name: '编辑张同学的分项成绩' }).click()
  const editors = page.locator('.entry-editor')
  for (const [index, value] of [80, 90, 70].entries()) {
    await editors.nth(index).locator('.ds-select-trigger').click()
    await page.getByRole('option', { name: '人工已评分', exact: true }).click()
    await editors.nth(index).getByLabel('百分制分数').fill(String(value))
  }
  await page.getByRole('button', { name: '保存分项草稿', exact: true }).click()
  await expect(page.getByText('分项草稿已保存，官方成绩版本保持不变。', { exact: true })).toBeVisible()
}
async function publishReason(page, reason) {
  await page.getByLabel('发布 / 归档原因').fill(reason)
  await page.getByRole('button', { name: '发布新成绩版本', exact: true }).click()
  await expect(page.getByText('成绩已发布，学生现在可查看本人版本。', { exact: true })).toBeVisible()
}

for (const role of ['teacher', 'admin']) for (const width of [390, 1440]) {
  test(`${role} 配置录分发布、归档与有原因调分 ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    const errors = diagnostics(page), state = await fixture(page, role)
    await page.goto(`/${role}/gradebook`)
    await expect(page.getByText('一级比例合计 100%', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '保存栏目与比例', exact: true }).click()
    await expect(page.getByRole('button', { name: '发布新成绩版本' })).toBeDisabled()
    expect(state.writes[0].body).toMatchObject({ expected_revision: 0 })
    expect(state.writes[0].body.categories.map(row => row.weight)).toEqual(['30', '40', '30'])
    await editFirstStudent(page)
    const row = page.locator('.grade-table-scroll tbody tr').first()
    await expect(row).toContainText('81.00')
    await publishReason(page, '逐项核对后发布')
    const originalId = state.latest.id
    await page.getByLabel('发布 / 归档原因').fill('学期原始成绩归档')
    await page.getByRole('button', { name: '归档官方版本', exact: true }).click()
    await expect(page.getByText('当前官方成绩已生成独立归档。', { exact: true })).toBeVisible()
    const archive = state.snapshots.find(item => item.kind === 'archive')
    const adjustment = page.locator('.adjustment-form')
    await adjustment.locator('.ds-select-trigger').click()
    await page.getByRole('option', { name: '20260001 · 张同学', exact: true }).click()
    await adjustment.getByLabel('最终百分制分数').fill('88')
    await adjustment.getByLabel('调分原因').fill('核对课堂表现后调整')
    await adjustment.getByRole('button', { name: '保存调分', exact: true }).click()
    await expect(row).toContainText('88.00')
    expect(state.latest.id).toBe(originalId)
    await publishReason(page, '确认调分后重新发布')
    expect(state.latest.payload.rows[0].final_score).toBe('88.00')
    expect(archive.payload.rows[0].final_score).toBe('81.00')
    await page.getByRole('button', { name: /版本 4 · 归档/ }).click()
    await expect(page.getByRole('heading', { name: '历史版本 4', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '保存调分', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '发布新成绩版本', exact: true })).toHaveCount(0)
    await expect(row).toContainText('81.00')
    const archiveRead = state.reads.find(path => path === `/gradebooks/snapshots/${archive.id}`)
    expect(archiveRead).toBeTruthy()
    await checkPage(page, role, width)
    await capture(page, `school-gradebook-${role}-${width}.png`)
    expect(errors).toEqual([])
  })
}

for (const width of [390, 1440]) {
  test(`学生仅本人官方成绩及CSV导出 ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    const errors = diagnostics(page), state = await fixture(page, 'student')
    await page.goto('/student/gradebook')
    await expect(page.getByRole('heading', { name: '我的已发布成绩', exact: true })).toBeVisible()
    await expect(page.locator('.student-total')).toContainText('最终成绩81.00')
    await expect(page.locator('.grade-table-scroll tbody tr')).toHaveCount(1)
    await expect(page.getByText('李同学（班内私有成绩）')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '保存栏目与比例' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '保存调分' })).toHaveCount(0)
    const downloadReady = page.waitForEvent('download')
    await page.getByRole('button', { name: '下载 CSV', exact: true }).click()
    const download = await downloadReady
    const exported = await readFile(await download.path(), 'utf-8')
    expect(exported).toContain('20260001,张同学,81.00')
    expect(exported).not.toContain('李同学')
    expect(state.writes).toEqual([])
    await checkPage(page, 'student', width)
    await capture(page, `school-gradebook-student-${width}.png`)
    expect(errors).toEqual([])
  })
}

test('来源变化409保留发布原因，刷新重审后发布', async ({ page }) => {
  const state = await fixture(page, 'teacher')
  await page.goto('/teacher/gradebook')
  await page.getByRole('button', { name: '保存栏目与比例', exact: true }).click()
  await editFirstStudent(page)
  state.conflictNext = true
  await page.getByLabel('发布 / 归档原因').fill('审核当前来源版本')
  await page.getByRole('button', { name: '发布新成绩版本', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('来源成绩已变化')
  await expect(page.getByLabel('发布 / 归档原因')).toHaveValue('审核当前来源版本')
  expect(state.latest).toBeNull()
  await page.getByRole('button', { name: '刷新成绩状态', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await publishReason(page, '刷新重新审核后发布')
  expect(state.latest).not.toBeNull()
})
