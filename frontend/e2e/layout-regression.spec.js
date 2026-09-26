import { test, expect } from '@playwright/test'
import { layoutFixture, longTitle } from './fixtures/layout.js'

const sizes = [[1280, 800], [1440, 900], [1920, 1080]]
async function noOverflow(page) {
  const result = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  expect(result.scroll).toBeLessThanOrEqual(result.client + 1)
  await expect(page.locator('main')).toHaveCount(1)
}
async function capture(page, testInfo) {
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true, timeout: 10000 })
}
for (const [width, height] of sizes) {
  test(`作业首屏包含题干、300px 编辑器和操作 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height })
    await layoutFixture(page, 'student')
    await page.goto('/student/assignments/1')
    await expect(page.locator('#code-editor')).toBeVisible()
    const editor = await page.locator('#code-editor').boundingBox()
    const actions = await page.locator('.action-bar').boundingBox()
    expect(editor.height).toBeGreaterThanOrEqual(300)
    expect(editor.y + editor.height).toBeLessThanOrEqual(height)
    expect(actions.y + actions.height).toBeLessThanOrEqual(height)
    await page.locator('.action-bar').getByRole('button', { name: '自测', exact: true }).click()
    await expect(page.getByText('全部测试通过', { exact: false })).toBeVisible()
    await noOverflow(page)
    await capture(page, testInfo)
  })
  test(`课程长描述与六个班级不挤走操作 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height })
    await layoutFixture(page, 'teacher')
    await page.goto('/teacher/courses')
    await expect(page.getByRole('button', { name: longTitle, exact: true })).toBeVisible()
    const scroll = await page.locator('.course-table').evaluate(el => ({ table: el.getBoundingClientRect().width, container: el.parentElement.clientWidth }))
    expect(scroll.table).toBeLessThanOrEqual(scroll.container + 1)
    await page.locator('.class-summary summary').click()
    await expect(page.locator('.class-summary li')).toHaveCount(6)
    const actions = await page.locator('.course-actions').boundingBox()
    expect(actions.x + actions.width).toBeLessThanOrEqual(width)
    await noOverflow(page)
    await capture(page, testInfo)
  })
  test(`班级侧栏与学员名单宽度 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height })
    await layoutFixture(page, 'teacher')
    await page.goto('/teacher/classes')
    await expect(page.getByText('验收学员 1', { exact: true })).toBeVisible()
    const panels = page.locator('.roster-grid > section')
    const rail = await panels.nth(0).boundingBox()
    const main = await panels.nth(1).boundingBox()
    expect(rail.width).toBeCloseTo(280, 0)
    expect(main.width).toBeGreaterThan(rail.width * 2)
    await noOverflow(page)
    await capture(page, testInfo)
  })
  test(`讲义正文与目录无横向溢出 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height })
    await layoutFixture(page, 'student')
    await page.goto('/student/courses/1/lessons/3')
    await expect(page.getByRole('heading', { name: '认识 Python' })).toBeVisible()
    await page.getByRole('link', { name: '函数与数据', exact: true }).click()
    await noOverflow(page)
    await capture(page, testInfo)
  })
}

test('用户资料优先且输入框填满表单列', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await layoutFixture(page, 'admin')
  await page.goto('/admin/users/7/edit')
  await expect(page.getByLabel('真实姓名')).toHaveValue('布局验收用户')
  const profile = await page.locator('.form-card').boundingBox()
  const reset = await page.getByRole('heading', { name: '独立密码重置' }).boundingBox()
  expect(reset.y).toBeGreaterThan(profile.y + profile.height)
  const input = await page.getByLabel('真实姓名').boundingBox()
  expect(input.width).toBeGreaterThan(350)
  await noOverflow(page)
  await capture(page, testInfo)
})

test('短屏创建课程弹窗焦点循环并返回入口', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1366, height: 768 })
  await layoutFixture(page, 'teacher')
  await page.goto('/teacher/courses')
  const entry = page.getByRole('button', { name: '创建课程', exact: true })
  await entry.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const box = await dialog.boundingBox()
  expect(box.y).toBeGreaterThanOrEqual(0)
  expect(box.y + box.height).toBeLessThanOrEqual(768)
  await page.keyboard.press('Shift+Tab')
  expect(await dialog.evaluate(el => el.contains(document.activeElement))).toBe(true)
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(entry).toBeFocused()
  await capture(page, testInfo)
})

for (const role of ['student', 'teacher']) {
  test(`视频课时缺少视频时仍可读取和操作 ${role}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1366, height: 768 })
    await layoutFixture(page, role, { lessonType: 'video' })
    await page.goto(role === 'student' ? '/student/courses/1/lessons/3' : '/teacher/courses/1/lessons/3/edit')
    if (role === 'student') await expect(page.getByText('视频暂不可用')).toBeVisible()
    else await expect(page.getByRole('tablist', { name: '视频来源' })).toBeVisible()
    await noOverflow(page)
    await capture(page, testInfo)
  })
}

test('课程 Notebook 使用隔离记录显示内容', async ({ page }, testInfo) => {
  await layoutFixture(page, 'student')
  await page.goto('/student/courses/1/notebook/3')
  await expect(page.getByRole('heading', { name: 'Python 基础与函数：动手实验' })).toBeVisible()
  await expect(page.getByRole('button', { name: '运行', exact: true })).toBeVisible()
  await noOverflow(page)
  await capture(page, testInfo)
})

test('首次改密短屏显示完整表单且无业务写入', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1366, height: 768 })
  await layoutFixture(page, 'student', { mustChange: true })
  await page.goto('/account/password')
  await expect(page.getByRole('heading', { name: '设置你的个人密码' })).toBeVisible()
  await expect(page.getByRole('button', { name: '退出登录' })).toBeInViewport()
  await noOverflow(page)
  await capture(page, testInfo)
})

for (const state of ['empty', 'failed']) {
  test(`课程列表 ${state} 状态保留筛选入口`, async ({ page }, testInfo) => {
    await layoutFixture(page, 'teacher', { [state]: true })
    await page.goto('/teacher/courses')
    if (state === 'empty') await expect(page.getByText('暂无符合条件的课程')).toBeVisible()
    else await expect(page.getByRole('alert')).toContainText('课程暂时无法加载')
    await expect(page.getByRole('searchbox', { name: '搜索课程名称或编号' })).toBeVisible()
    await noOverflow(page)
    await capture(page, testInfo)
  })
}

test('教务选择班级后名单位于主栏且确认弹窗可取消', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1366, height: 768 })
  await layoutFixture(page, 'admin')
  await page.goto('/admin/academics')
  await page.locator('.class-select').first().click()
  await expect(page.locator('.roster').getByText('验收学员 1', { exact: true })).toBeVisible()
  await expect(page.locator('.class-panel')).toHaveCount(0)
  const roster = await page.locator('.roster').boundingBox()
  expect(roster.y).toBeLessThan(300)
  await page.screenshot({ path: testInfo.outputPath('roster-detail.png'), fullPage: true })
  const edit = page.locator('.term-panel').getByRole('button', { name: '编辑', exact: true })
  await edit.click()
  const dialog = page.getByRole('dialog', { name: '确认教务操作' })
  await expect(dialog).toBeVisible()
  const box = await dialog.boundingBox()
  expect(box.y + box.height).toBeLessThanOrEqual(768)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(edit).toBeFocused()
  await page.getByRole('button', { name: '返回教学班列表' }).click()
  await expect(page.locator('.roster')).toHaveCount(0)
  await expect(page.locator('.class-panel')).toBeVisible()
  await page.locator('select[aria-label="按学期筛选教学班"]').selectOption('1')
  await expect(page.locator('.roster')).toHaveCount(0)
  await noOverflow(page)
  await capture(page, testInfo)
})

test('评分长题干可完整展开但不成为页面大标题', async ({ page }, testInfo) => {
  await layoutFixture(page, 'teacher')
  await page.goto('/teacher/ai-grading/1')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('提交 #1 · 评分详情')
  await page.getByText('查看完整题目', { exact: true }).click()
  await expect(page.locator('.question-context p')).toContainText('验证所有边界条件')
  await noOverflow(page)
  await capture(page, testInfo)
})

test('课程加载失败可重试恢复真实列表', async ({ page }) => {
  const options = { failed: true }
  await layoutFixture(page, 'teacher', options)
  await page.goto('/teacher/courses')
  await expect(page.getByRole('alert')).toContainText('课程暂时无法加载')
  options.failed = false
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await expect(page.getByRole('button', { name: longTitle, exact: true })).toBeVisible()
  await expect(page.locator('.error-panel')).toHaveCount(0)
})

test('环境长列表滚动时不压缩重叠条目', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await layoutFixture(page, 'admin')
  await page.route('**/api/v1/environments/profiles/1', route => route.fulfill({ json: {
    id: 1, display_name: '环境 1', slug: 'env-1', status: 'active', versions: [],
    draft: { revision: 1, state: 'editing', python_version: '3.12', minimum_memory_mb: 256, python_packages: [], system_packages: [] },
  } }))
  await page.goto('/admin/environments')
  await expect(page.locator('.profile-row')).toHaveCount(12)
  const rows = await page.locator('.profile-row').evaluateAll(elements => elements.map(el => {
    const row = el.getBoundingClientRect()
    const content = el.querySelector('.profile-row-main').getBoundingClientRect()
    return { top: row.top, bottom: row.bottom, height: row.height, contentBottom: content.bottom }
  }))
  for (let i = 0; i < rows.length; i++) {
    expect(rows[i].height).toBeGreaterThanOrEqual(84)
    expect(rows[i].contentBottom).toBeLessThanOrEqual(rows[i].bottom)
    if (i) expect(rows[i].top).toBeGreaterThanOrEqual(rows[i - 1].bottom)
  }
  const panel = await page.locator('.profile-list').boundingBox()
  expect(panel.y + panel.height).toBeLessThanOrEqual(800)
  expect(panel.y + panel.height).toBeGreaterThanOrEqual(774)
  await page.evaluate(() => window.scrollTo(0, 400))
  await expect.poll(() => page.locator('.profile-list').evaluate(el => el.getBoundingClientRect().height)).toBeGreaterThan(650)
  await expect.poll(() => page.locator('.profile-list').evaluate(el => Math.abs(el.getBoundingClientRect().bottom - (window.innerHeight - 24)))).toBeLessThanOrEqual(2)
  await page.locator('.profile-row').last().scrollIntoViewIfNeeded()
  await expect(page.locator('.profile-row').last()).toBeInViewport()
  for (const [width, height] of [[1920, 1080], [1366, 768]]) {
    await page.setViewportSize({ width, height })
    await page.evaluate(() => window.scrollTo(0, 400))
    await expect.poll(() => page.locator('.profile-list').evaluate(el => Math.abs(el.getBoundingClientRect().bottom - (window.innerHeight - 24)))).toBeLessThanOrEqual(2)
    await noOverflow(page)
  }
  await noOverflow(page)
  await capture(page, testInfo)
})

test('短屏评分操作和考试配置栏保持在视口内', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1366, height: 768 })
  await layoutFixture(page, 'teacher')
  await page.goto('/teacher/submissions/1')
  await expect(page.getByRole('button', { name: '发布评分', exact: true })).toBeInViewport()
  const scorePanel = await page.locator('.review-panel').boundingBox()
  expect(scorePanel.y + scorePanel.height).toBeLessThanOrEqual(768)
  await capture(page, testInfo)
  await page.goto('/teacher/exams/1/edit')
  await expect(page.locator('.control-rail')).toBeVisible()
  const rail = await page.locator('.control-rail').boundingBox()
  expect(rail.y + rail.height).toBeLessThanOrEqual(768)
  await noOverflow(page)
})

test('考前说明和开始按钮在短屏内完整可见', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1366, height: 768 })
  await layoutFixture(page, 'student')
  await page.route('**/api/v1/exams/1/session', route => route.fulfill({ json: {
    server_now: '2026-09-21T08:00:00Z',
    exam: { id: 1, title: '考前布局验收', duration_minutes: 30, student_status: 'ready', start_at: '2026-09-21T07:00:00Z', end_at: '2026-09-21T09:00:00Z' },
    submission: null, questions: [], saved_answers: [], visibility: {},
  } }))
  await page.goto('/student/exams/1')
  await expect(page.getByRole('heading', { name: '准备开始考试' })).toBeVisible()
  await expect(page.locator('.start-button')).toBeInViewport()
  await noOverflow(page)
  await capture(page, testInfo)
})

for (const [width, height] of [[1280, 800], [1920, 1080]]) {
  test(`系统日志滚动表头贴合容器顶边且遮住正文 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height })
    await layoutFixture(page, 'admin')
    await page.route('**/api/v1/admin/logs?*', route => route.fulfill({ json: {
      file: 'dai-api.log', file_size: 10240, total: 80,
      items: Array.from({ length: 80 }, (_, i) => ({
        ts: '2026-09-21T01:56:11Z', level: i % 2 ? 'ERROR' : 'INFO', logger: 'dai.main',
        message: `日志 ${i}: http_request`, exc: 'Traceback: example\n'.repeat(8),
      })),
    } }))
    await page.goto('/admin/logs')
    await expect(page.locator('.log-table tbody tr')).toHaveCount(80)
    const card = page.locator('.log-card')
    for (const scroll of [0, 100, 580]) {
      await card.evaluate((el, top) => { el.scrollTop = top }, scroll)
      await expect.poll(() => page.locator('.log-table th').first().evaluate(el => {
        const container = el.closest('.log-card')
        return Math.abs(el.getBoundingClientRect().top - container.getBoundingClientRect().top - container.clientTop)
      })).toBeLessThanOrEqual(1)
      const headersCoverContent = await page.locator('.log-table th').evaluateAll(headers => headers.every(el => {
        const r = el.getBoundingClientRect()
        return document.elementFromPoint(r.x + r.width / 2, r.y + 2) === el
      }))
      expect(headersCoverContent).toBe(true)
    }
    await noOverflow(page)
    await capture(page, testInfo)
  })
}

test('日志级别菜单向下展开并支持选择、键盘和外部关闭', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await layoutFixture(page, 'admin')
  const levels = []
  await page.route('**/api/v1/admin/logs?*', route => {
    levels.push(new URL(route.request().url()).searchParams.get('level'))
    return route.fulfill({ json: { items: [], total: 0, file: 'dai-api.log', file_size: 0 } })
  })
  await page.goto('/admin/logs')
  const trigger = page.getByRole('button', { name: '日志级别', exact: true })
  const menu = page.getByRole('listbox', { name: '日志级别', exact: true })
  await trigger.click()
  await expect(menu).toBeVisible()
  const anchor = await trigger.boundingBox()
  const popup = await menu.boundingBox()
  expect(popup.y).toBeGreaterThanOrEqual(anchor.y + anchor.height)
  expect(popup.x).toBeCloseTo(anchor.x, 0)
  expect(popup.y + popup.height).toBeLessThan(800)
  await page.getByRole('option', { name: 'WARNING 及以上', exact: true }).click()
  await expect(trigger).toContainText('WARNING 及以上')
  await expect(trigger).toBeFocused()
  await expect(menu).toHaveCount(0)
  await expect.poll(() => levels.at(-1)).toBe('WARNING')
  await trigger.press('ArrowDown')
  await expect(page.getByRole('option', { name: 'WARNING 及以上', exact: true })).toBeFocused()
  await page.keyboard.press('Home')
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await expect.poll(() => levels.at(-1)).toBe('ERROR')
  await trigger.click()
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  await expect(trigger).toBeFocused()
  await trigger.click()
  await page.getByRole('heading', { name: '系统日志', exact: true }).click()
  await expect(menu).toHaveCount(0)
  await trigger.click()
  await page.keyboard.press('Tab')
  await expect(menu).toHaveCount(0)
  await trigger.click()
  await noOverflow(page)
  await capture(page, testInfo)
})

const selectRouteGroups = {
  admin: [
    ['/admin/users', 2], ['/admin/academics', 1], ['/admin/accounts', 3], ['/admin/environments', 0],
  ],
  teacher: [
    ['/teacher/courses', 3], ['/teacher/assignments', 4], ['/teacher/exams', 3],
    ['/teacher/experiments', 2], ['/teacher/submissions', 4], ['/teacher/submissions/unified', 5],
    ['/teacher/ai-grading', 2],
  ],
  student: [
    ['/student/assignments', 3], ['/student/feedback', 2], ['/student/history', 2], ['/student/experiments', 1],
  ],
}

for (const [role, routes] of Object.entries(selectRouteGroups)) {
  test(`${role} 端下拉栏逐页展开位置与视口边界`, async ({ page }) => {
    await page.setViewportSize({ width: 1366, height: 768 })
    await layoutFixture(page, role)
    for (const [path, expectedMinimum] of routes) {
      await test.step(path, async () => {
        await page.goto(path)
        const triggers = page.locator('.ds-select-trigger:visible:not(:disabled)')
        await expect.poll(() => triggers.count()).toBeGreaterThanOrEqual(expectedMinimum)
        expect(await page.locator('select:not(.ds-select-native)').count()).toBe(0)
        const count = await triggers.count()
        for (let index = 0; index < count; index += 1) {
          const current = triggers.nth(index)
          await current.click()
          const menu = page.locator('.ds-select-menu')
          await expect(menu).toBeVisible()
          const [anchor, popup] = await Promise.all([current.boundingBox(), menu.boundingBox()])
          expect(popup.x).toBeGreaterThanOrEqual(11)
          expect(popup.x + popup.width).toBeLessThanOrEqual(1355)
          expect(popup.y).toBeGreaterThanOrEqual(11)
          expect(popup.y + popup.height).toBeLessThanOrEqual(757)
          const canAlignLeft = anchor.x + popup.width <= 1355
          if (canAlignLeft) expect(Math.abs(popup.x - anchor.x)).toBeLessThanOrEqual(1)
          else expect(Math.abs((popup.x + popup.width) - 1354)).toBeLessThanOrEqual(1)
          const availableBelow = 768 - anchor.y - anchor.height - 12
          if (availableBelow >= Math.min(popup.height + 6, 180)) {
            expect(popup.y).toBeGreaterThanOrEqual(anchor.y + anchor.height)
          }
          await page.keyboard.press('Escape')
          await expect(menu).toHaveCount(0)
          await expect(current).toBeFocused()
        }
        await noOverflow(page)
      })
    }
  })
}
