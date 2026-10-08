import { test, expect } from '@playwright/test'
import { layoutFixture, notebook } from './fixtures/layout.js'
import { defaultConfig } from '../src/components/labs/labHelpers.js'

// Isolated UI acceptance: all requests are fulfilled locally, including writes.
const config = { ...defaultConfig('tabular_classification'), requirements: '训练分类模型，对比学习率并解释误差。', confirmed: true, dataset_id: 4, environment_version_id: 2 }
const assessment = { id: 6, version: 1, config, status: 'draft', validated: false }
const run = { id: 8, kind: 'train', device: 'cpu', status: 'succeeded', created_at: '2026-09-26T08:00:00Z', result: { curves: [{ epoch: 1, train_loss: 0.9, val_loss: 1.1 }, { epoch: 2, train_loss: 0.4, val_loss: 0.6 }], metrics: { macro_f1: 0.87, accuracy: 0.9 } }, logs: 'Epoch 1 loss=0.9\nEpoch 2 loss=0.4' }
const grade = { id: 2, total: 87, source: 'auto', published_at: '2026-09-26T09:00:00Z', feedback: '实现完整，请继续分析误分类样本。', metrics: run.result.metrics, items: [{ criterion_id: 'implementation', name: '实现与训练方法', score: 40, max_score: 40, reason: '训练与验证分离，算法符合要求。', evidence: [{ cell_id: 'code', line_start: 1, line_end: 2, quote: 'model.fit(train_x, train_y)' }] }] }
async function fixture(page, role) {
  await layoutFixture(page, role)
  const writes = []
  const current = structuredClone(assessment)
  let published = null
  let attempts = 0
  let training = false
  let reviewed = false
  let draftCells = structuredClone(notebook.cells)
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    if (request.method() !== 'GET') writes.push({ path, body: request.postDataJSON() })
    const reply = json => route.fulfill({ json })
    if (path === '/environments/available') return reply([{ environment_version_id: 2, display_name: '机器学习 CPU', version_number: 1, minimum_memory_mb: 4096, packages: [{ pip_name: 'torch', import_names: ['torch'] }] }])
    const template = { id: 7, name: '图像与表格分类实验', module_id: 3, lesson_id: null, status: 'draft', draft_revision: 1, draft_environment_version_id: 2, draft_cells: draftCells, current_version_id: null }
    if (path === '/studio/templates/7') return reply(template)
    if (path === '/studio/templates/7/draft') {
      const next = request.postDataJSON().cells
      if (JSON.stringify(next) !== JSON.stringify(draftCells)) current.validated = false
      draftCells = next
      return reply({ ...template, draft_revision: 2, draft_cells: draftCells })
    }
    if (path === '/labs/entries/module/3/assessment') {
      if (request.method() === 'PUT') {
        const next = request.postDataJSON()
        if (JSON.stringify(next) !== JSON.stringify(current.config)) current.validated = false
        current.config = next
        return reply(current)
      }
      return reply({ draft: current, published })
    }
    if (path === '/labs/entries/module/3/assessment/baseline') { current.validation_run_id = 8; return reply({ ...run, kind: 'baseline', status: 'queued' }) }
    if (path === '/labs/runs/8') { current.validated = true; return reply({ ...run, kind: 'baseline' }) }
    if (path === '/labs/entries/module/3/assessment/publish') { published = { ...current, status: 'published' }; return reply(published) }
    if (path === '/labs/datasets') return reply({ items: [{ id: 4, status: 'ready', task_type: 'tabular_classification', name: '花卉教学数据' }], total: 1 })
    if (path === '/experiments/records/99') return reply({ ...notebook, lab_enabled: true, cells: [...notebook.cells, { id: 'report', type: 'markdown', source: '## 结果分析', order: 2, student_editable: true }] })
    if (path === '/experiments/records/99/cells') return reply({ record_revision: 2 })
    if (path === '/experiments/records/99/submit') { attempts++; return reply({ id: 21, attempt_number: attempts, submitted_at: '2026-09-26T09:00:00Z' }) }
    if (path === '/labs/records/99/overview') return reply({ enabled: true, assessment: current, attempts_used: attempts, max_submissions: 5, score: attempts ? 87 : null, runs: training ? [{ ...run, id: 9, status: 'queued' }, run] : [run], artifacts: [{ id: 5, status: 'ready', source: 'platform', files: [] }], submissions: attempts ? [{ id: 21, attempt_number: attempts, score: 87 }] : [] })
    if (path === '/labs/records/99/train') { training = true; return reply({ ...run, id: 9, status: 'queued' }) }
    if (path === '/labs/records/99/validate') return reply({ ...run, kind: 'public_validate' })
    if (path === '/experiments/submissions/21') return reply({ id: 21, lab_enabled: true, attempt_number: 1, student_name: '张同学', student_username: '20260001', entry_name: '分类实验', score: reviewed ? 92 : 87, cells_snapshot: { code: 'model.fit(train_x, train_y)' }, cell_metadata: { code: { type: 'code' } } })
    if (path === '/labs/submissions/21') return reply({ enabled: true, assessment: current, grade: reviewed ? { ...grade, total: 92, source: 'manual' } : grade, run: { ...run, kind: 'evaluate' }, history: [], review_requested: false })
    if (path === '/labs/submissions/21/review') { reviewed = true; return reply({ ...grade, total: 92 }) }
    if (path === '/labs/submissions/21/appeal') return reply({ ok: true })
    if (path === '/labs/runtime') return reply({ enabled: true, training_concurrency: 1, total_cpus: 8, total_memory_mb: 16384, interactive_reserved_memory_mb: 4096, max_kernels: 8, gpu_enabled: false, gpu_devices: [], queued: 2, running: 1, leases: [] })
    return route.fallback()
  })
  return writes
}
async function checkPage(page) {
  await expect(page.locator('main')).toBeVisible()
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  const sizes = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  expect(sizes.scroll).toBeLessThanOrEqual(sizes.width + 1)
}
for (const width of [320, 768, 1024, 1440]) {
  test(`学生训练、报告与正式提交 ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    const writes = await fixture(page, 'student')
    await page.goto('/student/experiments/3')
    await expect(page.getByRole('heading', { name: '训练与评测' })).toBeVisible()
    await page.getByRole('button', { name: '保存并开始训练', exact: true }).click()
    await expect(page.getByRole('button', { name: '已有训练任务进行中' })).toBeDisabled()
    await page.getByLabel('报告内容 report').fill('验证集误差较高，需要分析过拟合。')
    await page.getByRole('button', { name: '保存并正式提交', exact: true }).click()
    await expect(page.getByText('当前成绩 87 分')).toBeVisible()
    const submission = writes.filter(w => w.path.endsWith('/submit'))
    expect(submission).toHaveLength(1)
    expect(submission[0].body).toMatchObject({ artifact_id: 5, expected_record_revision: 2 })
    await page.getByRole('button', { name: '第 1 次 · 87 分' }).click()
    await expect(page.getByText('实现完整，请继续分析误分类样本。')).toBeVisible()
    await checkPage(page)
    expect(errors).toEqual([])
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: `/tmp/lab-student-${width}.png`, fullPage: true })
  })
}
for (const width of [320, 1440]) {
test(`教师试跑与发布门禁 ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  const writes = await fixture(page, 'teacher')
  await page.goto('/teacher/experiments/3/studio/7')
  await page.getByRole('button', { name: '实验评测', exact: true }).click()
  await page.getByLabel('任务与预期结果').fill('实现分类算法，并讨论验证结果与过拟合。')
  await page.getByRole('button', { name: '实验评测 · 未保存' }).click()
  await page.getByRole('button', { name: '实验评测 · 未保存' }).click()
  await expect(page.getByLabel('任务与预期结果')).toHaveValue('实现分类算法，并讨论验证结果与过拟合。')
  await page.getByText('4. 参考试跑与发布', { exact: true }).click()
  await expect(page.getByRole('button', { name: '发布实验评测', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '保存并参考试跑', exact: true }).click()
  await page.getByRole('button', { name: '刷新试跑结果' }).click()
  await expect(page.getByText('参考试跑通过，可发布本版标准。')).toBeVisible()
  await page.getByRole('button', { name: '学生填写', exact: true }).click()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('button', { name: '发布实验评测', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '保存并参考试跑', exact: true }).click()
  await page.getByRole('button', { name: '刷新试跑结果' }).click()
  await expect(page.getByRole('button', { name: '发布实验评测', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '发布实验评测', exact: true }).click()
  await expect(page.getByText('已发布第 1 版', { exact: true })).toBeVisible()
  expect(writes.filter(w => w.path.endsWith('/assessment/publish'))).toHaveLength(1)
  await checkPage(page)
  expect(errors).toEqual([])
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.getByText('2. 评分标准', { exact: true }).click()
  await checkPage(page)
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: `/tmp/lab-teacher-${width}.png`, fullPage: true })
})
}
test('教师复核记录原因并发布分数', async ({ page }) => {
  await fixture(page, 'teacher')
  await page.goto('/teacher/submissions/21')
  await expect(page.getByRole('heading', { name: '评测与复核' })).toBeVisible()
  await expect(page.getByRole('button', { name: '发布复核成绩' })).toBeDisabled()
  await page.getByLabel('最终成绩', { exact: true }).fill('92')
  await page.getByLabel('复核或重评原因（必填）').fill('已检查提交代码，参数比较符合要求。')
  await page.getByRole('button', { name: '发布复核成绩' }).click()
  await expect(page.getByText('成绩：92 / 100')).toBeVisible()
  await checkPage(page)
})
test('管理员查看生效资源预算', async ({ page }) => {
  await fixture(page, 'admin')
  await page.goto('/admin/environments')
  await page.getByText('实验训练资源与队列', { exact: true }).click()
  await expect(page.getByText('16384 MiB', { exact: true })).toBeVisible()
  await expect(page.getByText('2 / 1', { exact: true })).toBeVisible()
  await checkPage(page)
})
