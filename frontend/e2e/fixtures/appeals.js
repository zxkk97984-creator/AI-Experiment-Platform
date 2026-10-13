// 批次 2 成绩异议与学生通知中心 E2E fixture。
//
// 复用 layoutFixture 的通用桩（认证 / 侧栏 / 课程等），再叠加本批新增的
// /notifications 与 /appeals 路由：Playwright 后注册的 route 优先，
// 因此无需修改共享的 e2e/fixtures/layout.js。
import { layoutFixture } from './layout.js'

export const notificationItems = [
  {
    id: 1, type: 'grading', title: 'AI 评分已完成',
    content: '你的作业提交已评分：80 分', entity_kind: 'code_grade', entity_id: 3,
    route: '/student/submissions/3', priority: 'normal',
    created_at: '2026-10-13T01:00:00Z', is_read: false,
  },
  {
    id: 2, type: 'appeal', title: '成绩异议已改分',
    content: '原分 60 → 82；复核后调整', entity_kind: 'grade_appeal', entity_id: 5,
    route: '/student/submissions/3', priority: 'important',
    created_at: '2026-10-13T02:00:00Z', is_read: false,
  },
]

export const teacherAppeals = [
  {
    id: 5, target_kind: 'submission', target_id: 3, status: 'pending',
    reason: '老师您好，这道题的第三组测试我认为我的解法是正确的，请求复核。',
    student_reply: null, handler_comment: null, score_before: 60, score_after: null,
    result_revision_snapshot: 1, title: '正数求和', route: '/student/submissions/3',
    student_id: 9, student_name: '验收学员', student_no: '2026000009',
    code_grade_id: 3, resolved_by_id: null,
    created_at: '2026-10-13T02:10:00Z', visible_at: '2026-10-12T02:10:00Z',
    window_expires_at: '2026-10-19T02:10:00Z', resolved_at: null, withdrawn_at: null,
    can_withdraw: true,
  },
]

export const myAppealItems = [
  {
    id: 5, target_kind: 'submission', target_id: 3, status: 'pending',
    reason: '老师您好，这道题的第三组测试我认为我的解法是正确的，请求复核。',
    student_reply: null, handler_comment: null, score_before: 60, score_after: null,
    result_revision_snapshot: 1, title: '正数求和', route: '/student/submissions/3',
    created_at: '2026-10-13T02:10:00Z', visible_at: '2026-10-12T02:10:00Z',
    window_expires_at: '2026-10-19T02:10:00Z', resolved_at: null, withdrawn_at: null,
    can_withdraw: true,
  },
]

/**
 * 学生视角：通知中心 + 我的异议。readAll 会切换为全部已读。
 * 教师视角：/appeals 列表与处理接口，处理成功后列表变为 upheld。
 */
export async function appealsFixture(page, { role = 'student', notifications = notificationItems } = {}) {
  await layoutFixture(page, role)
  const state = { notifications: notifications.map((item) => ({ ...item })), handled: [], listed: 0 }

  await page.route('**/api/v1/notifications**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/read-all')) {
      state.notifications.forEach((item) => { item.is_read = true })
      return route.fulfill({ status: 204, body: '' })
    }
    if (/\/notifications\/\d+\/read$/.test(path)) {
      const id = Number(path.split('/').at(-2))
      const hit = state.notifications.find((item) => item.id === id)
      if (hit) hit.is_read = true
      return route.fulfill({ status: 204, body: '' })
    }
    const unreadOnly = new URL(request.url()).searchParams.get('unread_only') === 'true'
    const items = state.notifications.filter((item) => !unreadOnly || !item.is_read)
    return route.fulfill({
      json: {
        items,
        unread_count: state.notifications.filter((item) => !item.is_read).length,
        total: items.length,
      },
    })
  })

  await page.route('**/api/v1/appeals**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/handle')) {
      const body = request.postDataJSON() || {}
      state.handled.push(body)
      const id = Number(path.split('/').at(-2))
      const hit = (role === 'student' ? myAppealItems : teacherAppeals).find((item) => item.id === id)
      return route.fulfill({
        json: {
          ok: true, appeal_id: id,
          status: body.action === 'adjust' ? 'adjusted' : body.action === 'regrade' ? 'regrading' : 'upheld',
          score_before: hit?.score_before ?? 60,
          score_after: body.action === 'adjust' ? body.final_score_100 : hit?.score_before ?? 60,
        },
      })
    }
    if (path.endsWith('/mine')) return route.fulfill({ json: { items: myAppealItems } })
    if (path.endsWith('/appeals')) {
      state.listed += 1
      return route.fulfill({ json: { items: teacherAppeals, total: teacherAppeals.length, page: 1, page_size: 20 } })
    }
    return route.fulfill({ status: 404, json: { detail: { message: 'fixture 未覆盖的异议路径' } } })
  })

  return state
}
