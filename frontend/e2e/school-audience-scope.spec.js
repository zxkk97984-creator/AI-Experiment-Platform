import { expect, test } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

// Fixture-only browser checks: every request is intercepted by layoutFixture.
for (const kind of ['assignments', 'exams']) {
  test(`${kind} 编辑页仅提供本实例教学班和学生候选`, async ({ page }) => {
    await layoutFixture(page, 'teacher')
    const task = { id: 1, course_id: 1, offering_id: 10, title: '本班原创任务', status: 'draft',
      duration_minutes: 60, audience_mode: 'selected_classes', audience_class_ids: [101],
      whitelist_student_ids: [], excluded_student_ids: [], allowed_imports: [], import_policy_mode: 'unrestricted' }
    const reads = [], errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
      reads.push(path)
      if (path === `/${kind}/1`) return route.fulfill({ json: task })
      if (path === `/${kind}/1/questions`) return route.fulfill({ json: { items: [] } })
      if (path === '/offerings') return route.fulfill({ json: { items: [
        { id: 10, course_id: 1, teaching_class_id: 101, class_name: '本班A' },
        { id: 20, course_id: 1, teaching_class_id: 202, class_name: '其他班B' },
      ] } })
      if (path === '/offerings/10/students') return route.fulfill({ json: { items: [
        { id: 3, real_name: '本班学生', student_no: 'A003', status: 'active' },
        { id: 9, real_name: '已经转班', student_no: 'OLD009', status: 'transferred' },
      ] } })
      return route.fallback()
    })
    await page.goto(`/teacher/${kind}/1/edit`)
    const picker = page.locator('.audience-picker')
    await expect(picker).toContainText('仅可选择本授课班在册学生')
    await picker.getByTestId('audience-class-select').locator('.teaching-class-trigger').click()
    await expect(picker.locator('.teaching-class-option')).toHaveCount(1)
    await expect(picker.locator('.teaching-class-option')).toContainText('本班A')
    await picker.getByTestId('audience-class-select').locator('.teaching-class-trigger').click()
    await picker.getByPlaceholder('搜索姓名、学号或账号').fill('A003')
    await picker.getByPlaceholder('搜索姓名、学号或账号').press('Enter')
    await expect(picker.locator('.student-candidates').first()).toContainText('本班学生')
    await expect(picker).not.toContainText('已经转班')
    await expect(picker).not.toContainText('其他班B')
    await picker.locator('.student-candidates').first().getByRole('button').click()
    await expect(picker.locator('.student-tags').first()).toContainText('本班学生')
    expect(reads).not.toContain('/users/students')
    await expect(page.locator('vite-error-overlay')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}
