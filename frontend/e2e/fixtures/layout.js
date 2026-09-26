// Deterministic UI data. Every API request is fulfilled locally; no business write
// reaches the running backend, including lesson progress and exam answers.
export const longTitle = 'Python 与人工智能综合实验：数据处理、边界条件验证和结果分析'
export const classes = Array.from({ length: 6 }, (_, i) => ({ id: i + 1, code: `2462160${i + 1}班`, name: `2026-2027 学年第一学期（秋季） 2462160${i + 1}班`, student_count: 20, status: 'active', academic_term_id: 1 }))
export const course = { id: 1, title: longTitle, description: '学习函数、数据处理与可重复实验。'.repeat(10), status: 'published', academic_term: { id: 1, name: '2026-2027 学年第一学期（秋季）' }, teaching_classes: classes, chapter_count: 6, lesson_count: 24, student_count: 120, updated_at: '2026-09-21T08:00:00Z', enrolled: true }
export const lesson = { id: 3, title: 'Python 基础与函数：概念导读', content_type: 'markdown', status: 'published', content: '# 认识 Python\n\n## 函数与数据\n\n使用函数组织程序。\n\n```python\nprint("Hello")\n```', video_source: 'external', video_url: '', template_id: 7 }
export const notebook = { id: 99, record_revision: 1, entry_name: 'Python 基础与函数：动手实验', entry_description: '完成函数并验证边界条件。', cells: [{ id: 'intro', type: 'markdown', source: '# 实验说明\n\n编写函数并记录执行结果。', order: 0 }, { id: 'code', type: 'code', source: 'def average(values):\n    return sum(values) / len(values)\n\nprint(average([1, 2, 3]))', order: 1, student_editable: true }] }

export async function layoutFixture(page, role, options = {}) {
  // External font availability must not stall an isolated browser regression.
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  await page.route('https://fonts.gstatic.com/**', route => route.abort())
  const user = { id: 7, username: 'ui_fixture', real_name: '布局验收用户', role, must_change_password: !!options.mustChange, status: 'active' }
  await page.route('**/api/v1/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname.replace('/api/v1', '')
    const reply = json => route.fulfill({ json })
    if (path === '/auth/refresh') return reply({ access_token: 'ui-fixture', user })
    if (path === '/auth/me' || path === '/users/7') return reply(user)
    if (path === '/users/me/preferences') return reply({ preferences: { sidebar_collapsed: false } })
    if (path === '/environments/editor-options') return reply({ default_python_version: '3.12', default_memory_mb: 256, python_versions: ['3.12'] })
    if (path === '/environments/profiles' || /^\/environments\/profiles\/\d+$/.test(path)) {
      const profiles = Array.from({ length: 12 }, (_, i) => ({ id: i + 1, display_name: `环境 ${i + 1}`, slug: `env-${i + 1}`, description: '包含数据分析、机器学习和科学计算依赖', status: 'active', draft: null, current_version: { version_number: 1, python_version: '3.12' }, recent_build: { status: 'done' }, versions: [] }))
      return reply(path.endsWith('/profiles') ? profiles : profiles[0])
    }
    if (path === '/academic-terms') return reply({ items: [course.academic_term], total: 1 })
    if (path === '/teaching-classes') return reply({ items: classes, total: classes.length })
    if (/\/teaching-classes\/\d+\/students$/.test(path)) return reply({ items: Array.from({ length: 20 }, (_, i) => ({ id: i + 10, username: `student_with_long_account_${i}`, student_no: `202600000${i}`, real_name: `验收学员 ${i + 1}` })), total: 20 })
    if (path === '/courses') {
      if (options.failed) return route.fulfill({ status: 503, json: { detail: { message: '测试服务暂不可用' } } })
      return reply({ items: options.empty ? [] : [course], total: options.empty ? 0 : 1, summary: { total: 1, published: 1, draft: 0, archived: 0 } })
    }
    if (path.endsWith('/chapters')) return reply([{ id: 2, title: 'Python 基础与函数', lessons: [{ ...lesson, content_type: options.lessonType || 'markdown' }] }])
    if (path === '/courses/1') return reply(course)
    if (path === '/lessons/3/start') return reply({ lesson_id: 3, status: 'in_progress' })
    if (path === '/exams/1') return reply({ id: 1, course_id: 1, title: longTitle, status: 'draft', duration_minutes: 60 })
    if (path === '/exams/1/questions') return reply([])
    if (path === '/experiments/submissions/1') return reply({ id: 1, student_name: user.real_name, student_username: user.username, module_title: longTitle, score: 85, reviewed_at: '2026-09-21T08:00:00Z', cells_snapshot: { intro: '# 实验说明', code: 'print(1)' }, cell_metadata: { intro: { type: 'markdown' }, code: { type: 'code' } } })
    if (path === '/assignments/1/questions') return reply([{ id: 2, title: '正数求和', description: '返回列表中所有正数的和；空列表返回 0。', signature: 'def sum_positive(values: list[int]) -> int:', function_name: 'sum_positive', starter_code: 'def sum_positive(values):\n    return 0', public_cases: Array.from({ length: 15 }, (_, i) => ({ args: [[i, -i, 3]], expected: i + 3 })) }])
    if (path === '/assignments/1') return reply({ id: 1, course_id: 1, title: longTitle, status: 'published', due_at: null })
    if (path.endsWith('/sample-run')) return reply({ status: 'accepted', stdout: '1 passed', execution_time_ms: 12 })
    if (path.startsWith('/experiments/records/ensure-for-')) return reply({ id: 99, record_revision: 1 })
    if (path === '/experiments/records/99') return reply(notebook)
    if (path === '/ai-grading/grades/1') return reply({ id: 1, question_title: '给定一个整数数组，验证所有边界条件并返回符合要求的结果。'.repeat(20), student_name: user.real_name, student_username: user.username, course_title: longTitle, status: 'completed', final_score_100: 80, deterministic_details: { groups: [] }, ai_result: null })
    return reply({ items: [], total: 0 })
  })
}
