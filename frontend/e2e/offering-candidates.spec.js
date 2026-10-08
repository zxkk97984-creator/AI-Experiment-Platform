import { expect, test } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

// Every API request is intercepted, including writes; this never changes the business database.
async function fixture(page) {
  await layoutFixture(page, 'admin')
  const offerings = [1, 2].map(id => ({ id, course_id: 1, academic_term_id: 1, teaching_class_id: id, teacher_id: 101, teacher_name: '教师101', course_title: '共享课程', class_name: `${id}班`, term_name: '当前学期', status: 'active' }))
  const datasets = {
    '/courses': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, title: `课程${i + 1}` })),
    '/academic-terms': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, name: `学期${i + 1}`, status: 'active' })),
    '/teaching-classes': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, name: `班级${i + 1}`, academic_term_id: 1, status: 'active' })),
    '/users': Array.from({ length: 101 }, (_, i) => ({ id: i + 1, real_name: `教师${i + 1}`, username: `teacher${i + 1}`, status: 'active' })),
    '/users/students': Array.from({ length: 205 }, (_, i) => ({ id: i + 1, real_name: `学生${i + 1}`, student_no: `S${i + 1}` })),
  }
  const state = { reads: [], writes: [], failStudents: false }
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/v1', '')
    const reply = json => route.fulfill({ json })
    if (request.method() === 'POST' && (path === '/offerings' || /^\/offerings\/\d+\/students$/.test(path))) {
      state.writes.push({ path, body: request.postDataJSON() }); return reply({ id: 3 })
    }
    if (path === '/offerings') return reply({ items: offerings, total: offerings.length })
    if (/^\/offerings\/\d+\/students$/.test(path)) return reply({ items: [{ id: 1, real_name: '学生1', student_no: 'S1', status: 'active' }] })
    if (path.endsWith('/legacy-review')) return reply({ items: [], tasks: [], total: 0 })
    if (path.endsWith('/teaching-templates')) return reply({ tasks: [], experiments: [] })
    if (datasets[path]) {
      const params = Object.fromEntries(url.searchParams); state.reads.push({ path, params })
      if (path === '/users/students' && state.failStudents) {
        state.failStudents = false
        return route.fulfill({ status: 503, json: { detail: { message: '候选名单暂不可用' } } })
      }
      const list = datasets[path].filter(row => (!params.q || (path === '/users' ? [row.real_name, row.username].includes(params.q) : JSON.stringify(row).includes(params.q))) && (path !== '/teaching-classes' || Number(params.academic_term_id) === row.academic_term_id))
      const pageNumber = Number(params.page || 1), size = Number(params.page_size || 20)
      return reply({ items: list.slice((pageNumber - 1) * size, pageNumber * size), total: list.length })
    }
    return route.fallback()
  })
  return state
}
async function jump(dialog, pageNumber) {
  await dialog.getByLabel('跳转页码').fill(String(pageNumber))
  await dialog.getByLabel('跳转页码').press('Enter')
  await expect(dialog.getByRole('button', { name: `第 ${pageNumber} 页`, exact: true })).toHaveAttribute('aria-current', 'page')
}
async function pick(page, opener, name) {
  await page.getByRole('button', { name: opener, exact: true }).click()
  const dialog = page.getByRole('dialog')
  await jump(dialog, 6)
  await dialog.getByRole('radio', { name, exact: true }).check()
  await dialog.getByRole('button', { name: '确认选择', exact: true }).click()
}
for (const width of [390, 1440]) {
  test(`101条候选与205名学生跨页确认、取消、焦点恢复 ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    const errors = []; page.on('pageerror', e => errors.push(e.message))
    const state = await fixture(page)
    await page.goto('/admin/offerings')
    await expect(page.getByLabel('学期')).toContainText('学期101')
    await page.getByLabel('学期').selectOption('1')
    await pick(page, '选择课程', '课程101')
    await pick(page, '选择教学班', '班级101')
    await pick(page, '选择授课教师', '教师101 · teacher101')
    await page.getByRole('button', { name: '创建并加入班级名单' }).click()
    await expect.poll(() => state.writes.length).toBe(1)
    expect(state.writes[0]).toEqual({ path: '/offerings', body: { course_id: 101, academic_term_id: 1, teaching_class_id: 101, teacher_id: 101 } })
    expect(state.reads.filter(row => row.path === '/courses').every(row => row.params.page_size === '20' && !row.params.academic_term_id)).toBe(true)
    expect(state.reads.filter(row => row.path === '/users').every(row => row.params.role === 'teacher' && row.params.status_filter === 'active')).toBe(true)
    await page.locator('.class-card').first().click()
    await page.getByRole('button', { name: '选择补充学生', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.locator('.candidate-row').first().getByRole('checkbox')).toBeDisabled()
    await dialog.getByRole('checkbox', { name: 'S2 · 学生2', exact: true }).check()
    await jump(dialog, 11)
    await dialog.getByRole('checkbox', { name: 'S205 · 学生205', exact: true }).check()
    await expect(dialog.getByLabel('已选项目')).toContainText('学生205')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    await page.screenshot({ path: test.info().outputPath(`student-candidates-${width}.png`), fullPage: true })
    await dialog.getByRole('button', { name: '确认选择', exact: true }).click()
    await expect(page.getByRole('button', { name: '选择补充学生', exact: true })).toBeFocused()
    await page.getByRole('button', { name: '选择补充学生', exact: true }).click()
    await dialog.getByRole('button', { name: '移除S205 · 学生205' }).click()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('button', { name: '选择补充学生', exact: true })).toBeFocused()
    await expect(page.locator('.chosen-students')).toContainText('学生205')
    await page.getByLabel('变更原因').fill('测试跨页名单确认')
    await page.getByRole('button', { name: '补充名单', exact: true }).click()
    await expect.poll(() => state.writes.length).toBe(2)
    expect(state.writes[1]).toEqual({ path: '/offerings/1/students', body: { student_ids: [2, 205], reason: '测试跨页名单确认' } })
    expect(state.reads.filter(row => row.path === '/users/students').every(row => row.params.page_size === '20')).toBe(true)
    expect(errors).toEqual([])
  })
}

test('候选局部失败重试、学期联动和切换实例清空待添名单', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('/admin/offerings')
  await page.getByLabel('学期').selectOption('1')
  await pick(page, '选择教学班', '班级101')
  await page.getByLabel('学期').selectOption('2')
  await expect(page.getByRole('button', { name: '选择教学班', exact: true })).toHaveText('请选择')
  await page.locator('.class-card').first().click()
  state.failStudents = true
  await page.getByRole('button', { name: '选择补充学生', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('alert')).toContainText('候选名单暂不可用')
  await dialog.getByRole('button', { name: '重试', exact: true }).click()
  await expect(dialog.getByRole('checkbox', { name: 'S2 · 学生2', exact: true })).toBeEnabled()
  await dialog.getByRole('checkbox', { name: 'S2 · 学生2', exact: true }).check()
  await dialog.getByRole('button', { name: '确认选择', exact: true }).click()
  await page.locator('.class-card').nth(1).click()
  await expect(page.locator('.chosen-students')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '补充名单', exact: true })).toBeDisabled()
})
