// 批次 3 考试秩序 E2E fixture。
//
// 复用 layoutFixture 的通用桩（认证 / 侧栏 / 课程等），再叠加本批新增的考试路由：
// Playwright 后注册的 route 优先，因此无需修改共享的 e2e/fixtures/layout.js。
import { layoutFixture, longTitle } from './layout.js'

export const examServerNow = '2026-10-13T02:00:00Z'

export const trueFalseQuestion = {
  id: 11, question_type: 'true_false', prompt: 'Python 的 list 是可变的。', options: null, points: 10,
}
export const choiceQuestion = {
  id: 12, question_type: 'single_choice', prompt: '2 + 2 = ?', options: { A: '4', B: '5' }, points: 10,
}

export function examSessionPayload({
  examId = 4, submissionId = 19, status = 'started', writeGeneration = 1,
  questions = [trueFalseQuestion, choiceQuestion], timingMode = 'unified',
} = {}) {
  return {
    server_now: examServerNow,
    exam: {
      id: examId, title: '考试秩序验收', duration_minutes: 60, max_score: 20,
      student_status: status === 'started' ? 'in_progress' : 'graded',
      timing_mode: timingMode, exam_kind: 'quiz',
      shuffle_questions: false, shuffle_options: false,
      end_at: '2026-10-13T03:00:00Z',
    },
    submission: {
      id: submissionId, status, score: null, score_visible: false,
      started_at: examServerNow, expires_at: '2026-10-13T02:30:00Z',
      last_saved_at: null, write_generation: writeGeneration,
    },
    can_write: status === 'started',
    takeover_required: false,
    questions: status === 'started' ? questions : [],
    saved_answers: [],
    visibility: { score: false, questions: false, answers: false, review_released: false },
  }
}

/**
 * 学生考试页 fixture：会话、批量保存、交卷与换机接管全部本地桩接，不发真实写请求。
 * onAnswerSave 可返回 { status, json } 覆盖默认成功响应（例如 409 DEVICE_SUPERSEDED）。
 */
export async function examIntegrityFixture(page, {
  examId = 4, session = examSessionPayload({ examId }), onAnswerSave, onTakeover,
} = {}) {
  await layoutFixture(page, 'student')
  const requests = { saves: [], submits: [], takeovers: [] }

  await page.route(`**/api/v1/exams/${examId}/session`, route => route.fulfill({ json: session }))
  await page.route(`**/api/v1/exams/${examId}/answers`, async route => {
    const body = route.request().postDataJSON() || {}
    requests.saves.push(body)
    if (onAnswerSave) {
      const override = await onAnswerSave(body, route)
      if (override) return
    }
    return route.fulfill({
      json: {
        server_now: examServerNow,
        write_generation: session.submission.write_generation,
        events_recorded: (body.events || []).length,
        results: (body.answers || []).map(answer => ({
          question_id: answer.question_id, ok: true, version: 1,
        })),
      },
    })
  })
  await page.route(`**/api/v1/exams/${examId}/submit`, async route => {
    requests.submits.push(route.request().postDataJSON() || {})
    return route.fulfill({ json: { ...session, submission: { ...session.submission, status: 'graded' } } })
  })
  await page.route(`**/api/v1/exams/${examId}/takeover`, async route => {
    const body = route.request().postDataJSON() || {}
    requests.takeovers.push(body)
    if (onTakeover) return onTakeover(body, route, session)
    return route.fulfill({
      json: {
        submission: {
          ...session.submission, write_generation: session.submission.write_generation + 1,
          device_token: 'fixture-device-token',
        },
        saved_answers: [],
        server_now: examServerNow,
      },
    })
  })
  return requests
}

/** 教师只读监考页 fixture：先走 layoutFixture（教师角色），再桩接 proctor 端点。 */
export async function proctorFixture(page, { examId = 9, overview } = {}) {
  await layoutFixture(page, 'teacher')
  let calls = 0
  await page.route(`**/api/v1/exams/${examId}/proctor*`, route => {
    calls += 1
    return route.fulfill({ json: overview || {
      exam: {
        id: examId, title: longTitle, status: 'published', exam_kind: 'midterm',
        timing_mode: 'unified', duration_minutes: 60, shuffle_questions: true, shuffle_options: false,
      },
      students: [{
        submission_id: 51, student_id: 21, username: 'student_1', display_name: '验收学员 1',
        status: 'started', started_at: examServerNow, expires_at: '2026-10-13T02:30:00Z',
        submitted_at: null, last_saved_at: examServerNow, blur_count: 3, away_total_ms: 12000,
        device_switch_count: 1, write_generation: 2, score: null,
      }],
      events: [
        { id: 3, submission_id: 51, student_id: 21, kind: 'device_switch', payload: { generation: 2 }, client_ts: null, server_ts: examServerNow, duration_ms: null },
        { id: 2, submission_id: 51, student_id: 21, kind: 'focus', payload: {}, client_ts: examServerNow, server_ts: examServerNow, duration_ms: 12000 },
        { id: 1, submission_id: 51, student_id: 21, kind: 'blur', payload: {}, client_ts: examServerNow, server_ts: examServerNow, duration_ms: null },
      ],
      totals: { students: 1, in_progress: 1, blur_count: 3, away_total_ms: 12000, device_switch_count: 1 },
      server_now: examServerNow,
    } })
  })
  return { calls: () => calls }
}
