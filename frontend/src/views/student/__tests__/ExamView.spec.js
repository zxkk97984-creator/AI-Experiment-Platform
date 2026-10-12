import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const routeState = vi.hoisted(() => ({ leaveHook: null }))
const router = { push: vi.fn(), replace: vi.fn() }
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '3' } }),
  useRouter: () => router,
  onBeforeRouteLeave: vi.fn((hook) => { routeState.leaveHook = hook }),
  createRouter: vi.fn(() => ({ beforeEach: vi.fn(), afterEach: vi.fn(), beforeResolve: vi.fn(), push: vi.fn(), replace: vi.fn(), currentRoute: { value: { path: '/student/exams/3' } } })),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../../../api/exams.js', () => ({
  examsAPI: {
    getSession: vi.fn(), saveAnswers: vi.fn(), submit: vi.fn(), start: vi.fn(), sampleRun: vi.fn(),
  },
}))

vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast: vi.fn() }) }))

import { examsAPI } from '../../../api/exams.js'
import ExamView from '../ExamView.vue'

const SERVER_NOW = '2026-08-12T04:00:00Z'
const QUESTIONS = [
  { id: 1, question_type: 'single_choice', prompt: '2 + 2 = ?', options: { A: '4', B: '5' }, points: 10 },
  { id: 2, question_type: 'fill_blank', prompt: '作者是 [[blank:blank1]]', points: 20 },
  { id: 3, question_type: 'code', prompt: '实现 add 函数', starter_code: 'def add(a, b):\n    pass', public_cases: [{ args: [1, 2], expected: 3 }], points: 30 },
]

function sessionFor(status, extra = {}) {
  const active = status === 'started'
  return {
    server_now: SERVER_NOW,
    exam: { id: 3, title: '期中考试', duration_minutes: 60, max_score: 60, student_status: active ? 'in_progress' : 'graded', ...extra.exam },
    submission: {
      id: 7, status, score: null, score_visible: false,
      expires_at: active ? '2026-08-12T04:10:00Z' : '2026-08-12T04:00:00Z',
      submitted_at: active ? null : SERVER_NOW, submission_reason: 'manual', ...extra.submission,
    },
    questions: active || extra.visibility?.questions ? (extra.questions || QUESTIONS) : [],
    saved_answers: extra.saved_answers || [],
    visibility: { score: false, questions: false, answers: false, review_released: false, ...extra.visibility },
  }
}

async function mountExam(status, extra = {}) {
  examsAPI.getSession.mockResolvedValue({ data: sessionFor(status, extra) })
  const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
  await flushPromises()
  return wrapper
}

describe('ExamView 安全状态与结果展示', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    routeState.leaveHook = null
    class IsolatedBroadcastChannel { postMessage() {} close() {} }
    vi.stubGlobal('BroadcastChannel', IsolatedBroadcastChannel)
    examsAPI.saveAnswers.mockImplementation((_examId, items) => Promise.resolve({
      data: { server_now: SERVER_NOW, results: items.map(item => ({ question_id: item.question_id, ok: true, version: 1 })) },
    }))
    examsAPI.sampleRun.mockResolvedValue({ data: { status: 'accepted', output: '1 passed', execution_time_ms: 12 } })
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('待复核显示人工处理提示，不泄露系统细节', async () => {
    const wrapper = await mountExam('review_required')
    expect(wrapper.text()).toContain('已交卷，等待教师复核')
    expect(wrapper.text()).toContain('不会按 0 分计入')
    expect(wrapper.text()).not.toContain('system_error')
    wrapper.unmount()
  })

  it('讲评开放后展示教师改分说明', async () => {
    const wrapper = await mountExam('graded', {
      visibility: { score: true, questions: true, answers: true, review_released: true },
      submission: { score: 55, score_visible: true },
      saved_answers: [
        { question_id: 1, score: 8, manual_score_reason: '学生部分正确', selected_options: ['A'] },
        { question_id: 2, score: 18, text_answers: { blank1: '函数名' } },
      ],
    })
    expect(wrapper.text()).toContain('教师改分说明')
    expect(wrapper.text()).toContain('学生部分正确')
    expect(wrapper.text()).toContain('教师改分后得分：8 / 10 分')
    expect(wrapper.findAll('.question-score').map(node => node.text())).toEqual([
      '本题得分 8 / 10 分',
      '本题得分 18 / 20 分',
    ])
    wrapper.unmount()
  })

  it('讲评开放后按题型展示选择题选项和编程题参考代码，而不是原始答案 JSON', async () => {
    const reviewQuestions = [
      {
        ...QUESTIONS[0],
        correct_answer: { correct: ['B'] },
      },
      {
        ...QUESTIONS[1],
        correct_answer: {
          blanks: [{ id: 'blank1', accepted_answers: ['函数名'], case_sensitive: false }],
        },
      },
      {
        ...QUESTIONS[2],
        correct_answer: {},
        reference_solution: 'def add(a, b):\n    return a + b',
      },
    ]
    const wrapper = await mountExam('graded', {
      visibility: { score: true, questions: true, answers: true, review_released: true },
      submission: { score: 55, score_visible: true },
      questions: reviewQuestions,
    })

    expect(wrapper.find('.review-options').text()).toContain('4')
    expect(wrapper.find('.review-options').text()).toContain('5')
    expect(wrapper.get('pre.review-code').text()).toContain('def add(a, b):')
    expect(wrapper.get('pre.review-code').text()).toContain('return a + b')
    expect(wrapper.findAll('.standard-answer')[1].text()).toContain('函数名')
    expect(wrapper.findAll('.standard-answer')[1].text()).not.toContain('blank1')
    expect(wrapper.text()).not.toContain('"correct"')
    expect(wrapper.text()).not.toContain('"blanks"')
    wrapper.unmount()
  })

  it('仅开放题目时仍展示选择题选项，但不标记正确答案', async () => {
    const wrapper = await mountExam('graded', {
      visibility: { score: true, questions: true, answers: false, review_released: true },
      questions: [{ ...QUESTIONS[0], correct_answer: { correct: ['B'] } }],
    })

    expect(wrapper.find('.review-options').text()).toContain('4')
    expect(wrapper.find('.review-options').text()).toContain('5')
    expect(wrapper.find('.review-option.correct').exists()).toBe(false)
    expect(wrapper.find('.standard-answer').exists()).toBe(false)
    wrapper.unmount()
  })

  it('成绩未公开时不把 null 渲染成 0 分', async () => {
    const wrapper = await mountExam('graded', { submission: { score: null, score_visible: false } })
    expect(wrapper.text()).toContain('成绩暂未开放')
    expect(wrapper.text()).not.toContain('0 / 30')
    wrapper.unmount()
  })

  it('总成绩未公开时不渲染逐题分数或教师改分理由', async () => {
    const wrapper = await mountExam('graded', {
      visibility: { score: false, questions: true, answers: true, review_released: true },
      submission: { score: null, score_visible: false },
      saved_answers: [{ question_id: 1, score: 8, manual_score_reason: '不应提前显示' }],
    })

    expect(wrapper.find('.question-score').exists()).toBe(false)
    expect(wrapper.find('.teacher-score-note').exists()).toBe(false)
    wrapper.unmount()
  })

  it('成绩公开时使用服务端 max_score', async () => {
    const wrapper = await mountExam('graded', { submission: { score: 25, score_visible: true }, visibility: { score: true } })
    expect(wrapper.text()).toContain('25 / 60 分')
    wrapper.unmount()
  })

  it('进行中恢复题目并安全渲染填空输入框', async () => {
    const wrapper = await mountExam('started')
    expect(wrapper.find('.workspace').exists()).toBe(true)
    expect(wrapper.find('input[aria-label="填空 blank1"]').exists()).toBe(true)
    expect(wrapper.html()).not.toContain('v-html')
    wrapper.unmount()
  })

  it('答案变更在 800ms 防抖后走批量保存并携带版本', async () => {
    vi.useFakeTimers()
    const wrapper = await mountExam('started')
    await wrapper.get('input[type="radio"]').trigger('change')
    await vi.advanceTimersByTimeAsync(801)
    await flushPromises()
    expect(examsAPI.saveAnswers).toHaveBeenCalledWith(3, [expect.objectContaining({ question_id: 1, selected_options: ['A'], expected_version: 0 })])
    wrapper.unmount()
  })

  it('刷新后若剩余不足一分钟立即提醒，并在 3 秒后关闭', async () => {
    vi.useFakeTimers()
    const wrapper = await mountExam('started', { submission: { expires_at: '2026-08-12T04:00:50Z' } })
    expect(wrapper.find('.minute-warning').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(3001)
    expect(wrapper.find('.minute-warning').exists()).toBe(false)
    wrapper.unmount()
  })

  it('倒计时兼容顶层 expires_at，并随服务器时钟递减', async () => {
    vi.useFakeTimers()
    const payload = sessionFor('started', { submission: { expires_at: null } })
    payload.expires_at = '2026-08-12T04:10:00Z'
    examsAPI.getSession.mockResolvedValue({ data: payload })
    const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()
    expect(wrapper.get('.timer strong').text()).toBe('00:10:00')
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.get('.timer strong').text()).toBe('00:09:59')
    wrapper.unmount()
  })

  it('把后端无时区的考试截止时间按 UTC 解析', async () => {
    vi.useFakeTimers()
    const wrapper = await mountExam('started', {
      submission: { expires_at: '2026-08-12T04:10:00' },
    })
    expect(wrapper.get('.timer strong').text()).toBe('00:10:00')
    wrapper.unmount()
  })

  it('未来 started 的异常数据不展示答题界面和倒计时', async () => {
    const wrapper = await mountExam('started', {
      exam: { student_status: 'scheduled' },
      submission: { expires_at: '2026-09-19T10:22:00Z' },
    })
    expect(wrapper.find('.timer').exists()).toBe(false)
    expect(wrapper.find('.workspace').exists()).toBe(false)
    expect(wrapper.text()).toContain('考试尚未开始')
    wrapper.unmount()
  })

  it('退出考试界面后重进按服务器时间继续倒计时，不暂停', async () => {
    vi.useFakeTimers()
    const firstPayload = sessionFor('started')
    examsAPI.getSession.mockResolvedValue({ data: firstPayload })
    const first = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()
    expect(first.get('.timer strong').text()).toBe('00:10:00')
    first.unmount()

    const secondPayload = sessionFor('started')
    secondPayload.server_now = '2026-08-12T04:05:00Z'
    secondPayload.exam.server_now = secondPayload.server_now
    secondPayload.submission.expires_at = '2026-08-12T04:10:00Z'
    examsAPI.getSession.mockResolvedValue({ data: secondPayload })
    const second = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()
    expect(second.get('.timer strong').text()).toBe('00:05:00')
    second.unmount()
  })

  it('starter code 不算已完成，运行当前版本后才标记编程题完成', async () => {
    const wrapper = await mountExam('started')
    const codeNav = wrapper.get('.exam-sidebar nav button:nth-child(3)')
    expect(codeNav.classes()).not.toContain('answered')
    expect(wrapper.get('[data-action="run-code-3"]').text()).toContain('运行自测')
    await wrapper.get('[data-action="run-code-3"]').trigger('click')
    await flushPromises()
    expect(codeNav.classes()).toContain('answered')
    wrapper.unmount()
  })

  it('未完成题目时退出，显示暂存提示并仅在确认后离开', async () => {
    const wrapper = await mountExam('started')
    const next = vi.fn()
    routeState.leaveHook({ fullPath: '/student/exams' }, {}, next)
    await wrapper.vm.$nextTick()
    expect(next).toHaveBeenCalledWith(false)
    expect(wrapper.text()).toContain('当前尚有未完成的题目，退出将暂存题目')
    expect(wrapper.get('.confirm-actions .btn-primary').text()).toBe('确定')
    await wrapper.get('.confirm-actions .btn-ghost').trigger('click')
    expect(router.push).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('未完成题目时交卷，显示明确风险提示', async () => {
    const wrapper = await mountExam('started')
    await wrapper.get('.submit-button').trigger('click')
    expect(wrapper.text()).toContain('当前尚有未完成的题目，确定交卷吗')
    expect(wrapper.get('.confirm-actions .btn-danger-solid').text()).toBe('确定')
    expect(wrapper.get('.confirm-actions .btn-ghost').text()).toBe('取消')
    wrapper.unmount()
  })
})

describe('ExamView 考试交互回归（批次二）', () => {
  function deferred() {
    let resolve
    const promise = new Promise((res) => { resolve = res })
    return { promise, resolve }
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    routeState.leaveHook = null
    class IsolatedBroadcastChannel { postMessage() {} close() {} }
    vi.stubGlobal('BroadcastChannel', IsolatedBroadcastChannel)
    examsAPI.saveAnswers.mockImplementation((_examId, items) => Promise.resolve({
      data: { server_now: SERVER_NOW, results: items.map(item => ({ question_id: item.question_id, ok: true, version: 1 })) },
    }))
    examsAPI.sampleRun.mockResolvedValue({ data: { status: 'accepted', output: '1 passed', execution_time_ms: 12 } })
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('活动期间会话同步不覆盖本地答案、版本与当前题', async () => {
    examsAPI.getSession.mockImplementation(() => Promise.resolve({ data: sessionFor('started') }))
    const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()

    const codeArea = wrapper.get('textarea.code-editor')
    await codeArea.setValue('def add(a, b):\n    return a + b  # 本地草稿')
    expect(wrapper.vm.answers[3]).toContain('本地草稿')

    // 服务器返回的是另一份答案与更高版本号：同步只更新元数据
    examsAPI.getSession.mockImplementation(() => Promise.resolve({
      data: sessionFor('started', {
        saved_answers: [{ question_id: 3, version: 9, code_answer: '服务器旧答案' }],
      }),
    }))
    window.dispatchEvent(new Event('focus'))
    await flushPromises()
    await flushPromises()

    expect(wrapper.vm.answers[3]).toContain('本地草稿')
    expect(wrapper.vm.answers[3]).not.toContain('服务器旧答案')
    expect(wrapper.vm.versions[3] ?? 0).toBeLessThan(9)
    expect(wrapper.vm.currentQuestion).toBe(1)
    wrapper.unmount()
  })

  it('保存串行化：同一时刻最多一个在途请求，保存期间的新编辑进入下一批', async () => {
    vi.useFakeTimers()
    const first = deferred()
    examsAPI.saveAnswers.mockImplementationOnce(() => first.promise)
    examsAPI.getSession.mockImplementation(() => Promise.resolve({ data: sessionFor('started') }))
    const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()

    await wrapper.get('textarea.code-editor').setValue('第一次编辑')
    await vi.advanceTimersByTimeAsync(900)          // 触发 debounce → 第一个请求在途
    expect(examsAPI.saveAnswers).toHaveBeenCalledTimes(1)

    await wrapper.get('textarea.code-editor').setValue('第二次编辑')
    await vi.advanceTimersByTimeAsync(900)          // 新编辑继续排队，不得并发
    expect(examsAPI.saveAnswers).toHaveBeenCalledTimes(1)
    await wrapper.vm.$nextTick()
    expect(wrapper.vm.saveState).toBe('saving')
    expect(wrapper.text()).toContain('保存中')

    first.resolve({ data: { server_now: SERVER_NOW, results: [{ question_id: 3, ok: true, version: 1 }] } })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(900)
    await flushPromises()

    expect(examsAPI.saveAnswers).toHaveBeenCalledTimes(2)
    const secondPayload = examsAPI.saveAnswers.mock.calls[1][1]
    expect(secondPayload[0].code_answer).toContain('第二次编辑')
    wrapper.unmount()
  })

  it('批量保存中部分失败：保留草稿、停止本次 drain，不影响其他可保存题', async () => {
    examsAPI.getSession.mockImplementation(() => Promise.resolve({ data: sessionFor('started') }))
    examsAPI.saveAnswers.mockImplementation((_examId, items) => Promise.resolve({
      data: {
        server_now: SERVER_NOW,
        results: items.map(item => item.question_id === 1
          ? { question_id: 1, ok: false, code: 'SAVE_FAILED', message: '保存失败' }
          : { question_id: item.question_id, ok: true, version: 1 }),
      },
    }))
    const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()

    await wrapper.get('input[name="q-1"]').setValue()
    const radio = wrapper.get('input[name="q-1"]')
    await radio.trigger('change')
    await flushPromises()
    await wrapper.get('textarea.code-editor').setValue('代码答案')
    await new Promise(resolve => setTimeout(resolve, 900))
    await flushPromises()

    // 失败题保留草稿（仍处于待保存状态），成功题已清除
    expect(wrapper.vm.pending[1]).toBeDefined()
    expect(wrapper.vm.pending[3]).toBeUndefined()
    expect(wrapper.text()).toContain('保存失败')
    wrapper.unmount()
  })

  it('版本冲突展示本地与服务器答案，由学生明确选择', async () => {
    examsAPI.getSession.mockImplementation(() => Promise.resolve({
      data: sessionFor('started', {
        saved_answers: [{ question_id: 1, version: 5, selected_options: ['B'] }],
      }),
    }))
    examsAPI.saveAnswers.mockImplementation((_examId, items) => Promise.resolve({
      data: {
        server_now: SERVER_NOW,
        results: items.map(item => item.question_id === 1
          ? { question_id: 1, ok: false, code: 'ANSWER_VERSION_CONFLICT', message: '答案已在其他页面更新' }
          : { question_id: item.question_id, ok: true, version: 1 }),
      },
    }))
    const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()

    const radios = wrapper.findAll('input[name="q-1"]')
    await radios[0].trigger('change')                 // 本地选择 A
    await new Promise(resolve => setTimeout(resolve, 900))
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="conflict-local"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="conflict-server"]').text()).toContain('B')
    expect(wrapper.get('[data-testid="conflict-local"]').text()).toContain('A')

    await wrapper.get('[data-testid="conflict-use-server"]').trigger('click')
    await flushPromises()
    expect(wrapper.vm.answers[1]).toEqual(['B'])
    expect(wrapper.vm.versions[1]).toBe(5)
    expect(wrapper.find('[data-testid="conflict-local"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('倒计时到零且服务器已延时：恢复编辑且不交卷', async () => {
    let calls = 0
    examsAPI.getSession.mockImplementation(() => {
      calls += 1
      if (calls === 1) {
        return Promise.resolve({ data: sessionFor('started', {
          submission: { expires_at: SERVER_NOW },   // 本地看到的旧截止已到
        }) })
      }
      return Promise.resolve({ data: sessionFor('started', {
        submission: { expires_at: '2026-08-12T05:00:00Z' },   // 教师已延时
      }) })
    })
    const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()
    await flushPromises()

    expect(examsAPI.submit).not.toHaveBeenCalled()
    expect(wrapper.get('textarea.code-editor').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('离线到点：锁定并保留草稿，不依据旧截止自动交卷', async () => {
    vi.stubGlobal('navigator', { onLine: false })
    let calls = 0
    examsAPI.getSession.mockImplementation(() => {
      calls += 1
      if (calls === 1) {
        return Promise.resolve({ data: sessionFor('started', { submission: { expires_at: SERVER_NOW } }) })
      }
      return Promise.reject(new Error('offline'))
    })
    const wrapper = mount(ExamView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
    await flushPromises()
    await flushPromises()

    expect(examsAPI.submit).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('答案草稿已保留在本机')
    expect(wrapper.text()).toContain('立即尝试交卷')
    wrapper.unmount()
  })

  it('题号导航使用内部滚动容器，交卷入口保持可见', async () => {
    const wrapper = await mountExam('started', { questions: QUESTIONS })
    expect(wrapper.get('nav.question-nav').exists()).toBe(true)
    expect(wrapper.find('.submit-button').exists()).toBe(true)
    // 容器类名固定，布局由 CSS（overflow-y:auto + min-height:0）保证
    expect(wrapper.get('aside.exam-sidebar').exists()).toBe(true)
    wrapper.unmount()
  })
})


// ─────────────────────────────────────────────────────────────
// A07 / A18 回归：批量响应先校验、冲突逐题排队、联网后延时解锁
// ─────────────────────────────────────────────────────────────

describe('ExamView 批量响应校验与冲突排队', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
  })

  async function mountActive(extra = {}) {
    return mountExam('started', extra)
  }

  async function stageChoice(wrapper, questionId, option = 'A') {
    // 选择题：勾选第 option 个单选（真实模板是 radio + @change）
    const radios = wrapper.findAll(`input[name="q-${questionId}"]`)
    await radios[option.charCodeAt(0) - 65].setValue(true)
    await flushPromises()
  }


  it('重复结果整批不应用：草稿与版本保持不变', async () => {
    const wrapper = await mountActive()
    examsAPI.saveAnswers.mockResolvedValue({
      data: { server_now: SERVER_NOW, results: [
        { question_id: 1, ok: true, version: 1 },
        { question_id: 1, ok: true, version: 2 },
      ] },
    })
    await stageChoice(wrapper, 1)
    await wrapper.vm.flushPending()
    await flushPromises()

    // 重复响应 → 视为整批失败，不清草稿、不推进版本
    expect(wrapper.vm.saveState).toBe('error')
    expect(Object.keys(wrapper.vm.pending).length).toBeGreaterThan(0)
    expect(wrapper.vm.versions[1] ?? 0).toBe(0)
  })

  it('批量响应校验：重复/缺失/未知题目/畸形一律整批拒绝', async () => {
    const wrapper = await mountActive()
    const entries = [[1, ['A']], [3, 'code']]
    expect(wrapper.vm.validateBatchResults([
      { question_id: 1, ok: true, version: 1 },
      { question_id: 1, ok: true, version: 2 },
    ], entries)).toEqual({ ok: false, reason: 'duplicate' })
    expect(wrapper.vm.validateBatchResults([
      { question_id: 1, ok: true, version: 1 },
    ], entries).reason).toBe('missing-results')
    expect(wrapper.vm.validateBatchResults([
      { question_id: 1, ok: true, version: 1 },
      { question_id: 99, ok: true, version: 1 },
    ], entries).reason).toBe('unknown-question')
    expect(wrapper.vm.validateBatchResults([
      { question_id: 1, ok: true, version: 1 },
      { question_id: 3, version: 1 },
    ], entries).reason).toBe('malformed')
    expect(wrapper.vm.validateBatchResults(null, entries).reason).toBe('missing-results')
    const ok = wrapper.vm.validateBatchResults([
      { question_id: 1, ok: true, version: 2 },
      { question_id: 3, ok: false, code: 'ANSWER_VERSION_CONFLICT' },
    ], entries)
    expect(ok.ok).toBe(true)
    expect(ok.results.map(item => item.question_id)).toEqual([1, 3])
  })

  it('同批两题冲突时逐题排队展示，处理完第一题出现第二题', async () => {
    const wrapper = await mountActive()
    examsAPI.getSession.mockResolvedValue({
      data: sessionFor('started', { saved_answers: [
        { question_id: 1, version: 3, selected_options: ['B'] },
        { question_id: 3, version: 4, code_answer: '服务器旧代码' },
      ] }),
    })
    await wrapper.vm.openConflict(1)
    await wrapper.vm.openConflict(3)
    await flushPromises()

    expect(wrapper.vm.conflict?.questionId).toBe(1)
    expect(wrapper.vm.conflictQueue.length).toBe(2)
    expect(wrapper.vm.conflicted.size).toBe(2)

    wrapper.vm.resolveConflict('server')
    await flushPromises()
    expect(wrapper.vm.conflict?.questionId).toBe(3), '处理完第一题必须出现第二题（不再永久 blocked）'
    expect(wrapper.vm.conflicted.has(3)).toBe(true)

    wrapper.vm.resolveConflict('server')
    await flushPromises()
    expect(wrapper.vm.conflict).toBe(null)
    expect(wrapper.vm.conflicted.size).toBe(0)
  })

  it('离线到点后联网同步确认已延时 → 解锁并恢复编辑', async () => {
    const wrapper = await mountActive()
    // 模拟离线到点：本地时间归零
    wrapper.vm.locked = true
    wrapper.vm.expiredHandled = true
    examsAPI.getSession.mockResolvedValue({
      data: sessionFor('started', { submission: { expires_at: '2026-08-12T04:30:00Z' } }),
    })
    window.dispatchEvent(new Event('online'))
    await flushPromises()
    await flushPromises()
    expect(wrapper.vm.locked).toBe(false), '教师延时后必须解除锁定'
    expect(wrapper.vm.expiredHandled).toBe(false)
  })

  it('联网同步若服务器已收卷则保持锁定', async () => {
    const wrapper = await mountActive()
    wrapper.vm.locked = true
    wrapper.vm.expiredHandled = true
    examsAPI.getSession.mockResolvedValue({ data: sessionFor('submitted') })
    window.dispatchEvent(new Event('online'))
    await flushPromises()
    await flushPromises()
    expect(wrapper.vm.locked).toBe(true)
  })
})
