import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AssignmentDetailView from '../AssignmentDetailView.vue'

const mocks = vi.hoisted(() => ({
  get: vi.fn(), getQuestions: vi.fn(), list: vi.fn(), submit: vi.fn(),
  getResult: vi.fn(), sampleRun: vi.fn(), showToast: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '4' } }),
  useRouter: () => ({ push: vi.fn() }),
  onBeforeRouteLeave: vi.fn(),
}))
vi.mock('../../../api/assignments.js', () => ({ assignmentsAPI: mocks }))
vi.mock('../../../api/judge.js', () => ({ judgeAPI: mocks }))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast: mocks.showToast }) }))

const assignment = { id: 4, course_id: 1, title: '请求隔离作业' }
const questions = [10, 20].map(id => ({ id, title: `题目 ${id}`, description: `说明 ${id}`,
  starter_code: `starter_${id}`, public_cases: [], grading_mode: 'legacy' }))
let wrapper

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function open() {
  wrapper = mount(AssignmentDetailView, { global: { stubs: { AppLayout: { template: '<main><slot /></main>' } } } })
  await flushPromises()
}
async function choose(index) { await wrapper.findAll('.q-dot')[index].trigger('click'); await flushPromises() }
async function submit() { await wrapper.get('.btn-submit-code').trigger('click'); await flushPromises() }
async function sample() { await wrapper.get('.btn-self-test').trigger('click'); await flushPromises() }
function result(id, status = 'wrong_answer', marker = `result_${id}`) {
  return { data: { id, status, code: `saved_${id}`, diagnostic: { message: marker } } }
}
function dirty() {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

describe('作业请求上下文与串行轮询', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.resetAllMocks()
    mocks.get.mockResolvedValue({ data: assignment })
    mocks.getQuestions.mockResolvedValue({ data: questions })
    mocks.list.mockResolvedValue({ data: { items: [] } })
    mocks.submit.mockResolvedValue({ data: { id: 301 } })
    mocks.getResult.mockResolvedValue(result(301, 'running'))
  })
  afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers() })

  it('切题后迟到历史不覆盖当前结果，也不启动旧题轮询', async () => {
    const older = deferred()
    mocks.list.mockResolvedValue({ data: { items: [{ id: 101, question_id: 10 }, { id: 102, question_id: 20 }] } })
    mocks.getResult.mockImplementation(id => id === 101 ? older.promise : Promise.resolve(result(102)))
    await open()
    await choose(1)
    expect(wrapper.text()).toContain('result_102')
    older.resolve(result(101, 'running', 'old_history'))
    await flushPromises()
    expect(wrapper.text()).toContain('result_102')
    expect(wrapper.text()).not.toContain('old_history')
    await vi.advanceTimersByTimeAsync(3000)
    expect(mocks.getResult).toHaveBeenCalledTimes(2)
  })

  it('同题新提交使较早的历史响应失效', async () => {
    const older = deferred()
    mocks.list.mockResolvedValue({ data: { items: [{ id: 101, question_id: 10 }] } })
    mocks.getResult.mockImplementation(id => id === 101 ? older.promise : Promise.resolve(result(301)))
    await open()
    await wrapper.get('#code-editor').setValue('new_answer')
    await submit()
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.text()).toContain('result_301')
    older.resolve(result(101, 'running', 'old_history'))
    await flushPromises()
    expect(wrapper.text()).toContain('result_301')
    expect(wrapper.text()).not.toContain('old_history')
    await vi.advanceTimersByTimeAsync(2000)
    expect(mocks.getResult.mock.calls.map(([id]) => id)).toEqual([101, 301])
  })

  it.each(['success', 'failure'])('旧自测%s不能影响另一题正在进行的自测', async outcome => {
    const older = deferred(), newer = deferred()
    mocks.sampleRun.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    await open()
    await sample()
    await choose(1)
    expect(wrapper.get('.btn-self-test').element.disabled).toBe(false)
    await sample()
    if (outcome === 'success') older.resolve(result(10, 'accepted', 'old_sample'))
    else older.reject(new Error('old failure'))
    await flushPromises()
    expect(wrapper.get('.btn-self-test').element.disabled).toBe(true)
    expect(wrapper.text()).not.toContain('old_sample')
    expect(mocks.showToast).not.toHaveBeenCalled()
    newer.resolve(result(20, 'accepted', 'new_sample'))
    await flushPromises()
    expect(wrapper.text()).toContain('new_sample')
    expect(wrapper.get('.btn-self-test').element.disabled).toBe(false)
  })

  it('截止失败等待刷新期间切题，迟到刷新和toast都被丢弃', async () => {
    const refresh = deferred()
    mocks.sampleRun.mockRejectedValue({ response: { data: { detail: { code: 'ASSIGNMENT_DEADLINE_PASSED', message: 'old_deadline' } } } })
    await open()
    mocks.get.mockReturnValueOnce(refresh.promise)
    await sample()
    await choose(1)
    refresh.resolve({ data: { ...assignment, due_at: '2020-01-01T00:00:00Z' } })
    await flushPromises()
    expect(wrapper.get('.btn-self-test').element.disabled).toBe(false)
    expect(wrapper.text()).not.toContain('作业已截止')
    expect(mocks.showToast).not.toHaveBeenCalled()
  })

  it('切题后的提交确认仅更新原题记录，不开启旧题轮询或替换当前状态', async () => {
    const request = deferred()
    mocks.submit.mockReturnValue(request.promise)
    await open()
    await wrapper.get('#code-editor').setValue('answer_10')
    await submit()
    await choose(1)
    request.resolve({ data: { id: 301 } })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(3000)
    expect(mocks.getResult).not.toHaveBeenCalled()
    expect(wrapper.get('.btn-submit-code').element.disabled).toBe(false)
    await choose(0)
    expect(mocks.getResult).toHaveBeenCalledWith(301)
    expect(dirty()).toBe(false)
  })

  it('同题两次提交倒序确认，旧确认不能回退较新基线和记录', async () => {
    const first = deferred(), second = deferred()
    mocks.submit.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    await open()
    await wrapper.get('#code-editor').setValue('first_code')
    await submit()
    await choose(1)
    await choose(0)
    await wrapper.get('#code-editor').setValue('second_code')
    await submit()
    second.resolve({ data: { id: 302 } })
    await flushPromises()
    first.resolve({ data: { id: 301 } })
    await flushPromises()
    expect(dirty()).toBe(false)
    await vi.advanceTimersByTimeAsync(1000)
    expect(mocks.getResult.mock.calls.map(([id]) => id)).toEqual([302])
    await choose(1)
    await choose(0)
    expect(mocks.getResult).toHaveBeenLastCalledWith(302)
  })

  it('慢轮询只有一个在途请求，返回后等待1秒才请求下一轮', async () => {
    const pending = deferred()
    mocks.getResult.mockReturnValueOnce(pending.promise)
    await open()
    await submit()
    await vi.advanceTimersByTimeAsync(5000)
    expect(mocks.getResult).toHaveBeenCalledTimes(1)
    pending.resolve(result(301, 'running'))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(999)
    expect(mocks.getResult).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(mocks.getResult).toHaveBeenCalledTimes(2)
  })

  it('切题后在途轮询返回终态，不能污染新题或取消新题轮询', async () => {
    const older = deferred()
    mocks.submit.mockResolvedValueOnce({ data: { id: 301 } }).mockResolvedValueOnce({ data: { id: 302 } })
    mocks.getResult.mockImplementation(id => id === 301 ? older.promise : Promise.resolve(result(302, 'wrong_answer', 'new_poll')))
    await open()
    await submit()
    await vi.advanceTimersByTimeAsync(1000)
    await choose(1)
    await submit()
    older.resolve(result(301, 'accepted', 'old_poll'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('old_poll')
    expect(wrapper.get('.btn-submit-code').element.disabled).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(mocks.getResult).toHaveBeenLastCalledWith(302)
    expect(wrapper.text()).toContain('new_poll')
  })

  it.each(['initial', 'history', 'sample', 'submit', 'poll'])('%s挂起时卸载，迟到完成不会再发请求或注册定时器', async phase => {
    const pending = deferred()
    if (phase === 'initial') mocks.getQuestions.mockReturnValueOnce(pending.promise)
    if (phase === 'history' || phase === 'initial') {
      mocks.list.mockResolvedValue({ data: { items: [{ id: 101, question_id: 10 }] } })
      if (phase === 'history') mocks.getResult.mockReturnValueOnce(pending.promise)
    }
    if (phase === 'submit') mocks.submit.mockReturnValueOnce(pending.promise)
    if (phase === 'sample') mocks.sampleRun.mockReturnValueOnce(pending.promise)
    if (phase === 'poll') mocks.getResult.mockReturnValueOnce(pending.promise)
    await open()
    if (phase === 'submit' || phase === 'poll') await submit()
    if (phase === 'sample') await sample()
    if (phase === 'poll') await vi.advanceTimersByTimeAsync(1000)
    wrapper.unmount(); wrapper = null
    const callsBefore = mocks.getResult.mock.calls.length
    pending.resolve(phase === 'initial' ? { data: questions } : phase === 'submit' ? { data: { id: 301 } } : result(101, 'running'))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(3000)
    expect(mocks.getResult).toHaveBeenCalledTimes(callsBefore)
    expect(vi.getTimerCount()).toBe(0)
    expect(mocks.showToast).not.toHaveBeenCalled()
  })

  it.each(['failure', 'no_id'])('旧提交%s不清除新题提交状态或弹出过期错误', async outcome => {
    const older = deferred(), newer = deferred()
    mocks.submit.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    await open()
    await submit()
    await choose(1)
    await submit()
    if (outcome === 'failure') older.reject(new Error('old submission error'))
    else older.resolve({ data: {} })
    await flushPromises()
    expect(wrapper.get('.btn-submit-code').element.disabled).toBe(true)
    expect(mocks.showToast).not.toHaveBeenCalled()
    newer.resolve({ data: { id: 302 } })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    expect(mocks.getResult).toHaveBeenLastCalledWith(302)
  })

  it.each(['running', 'wrong_answer'])('第120次返回%s后停止，最后一次终态不误报超时', async finalStatus => {
    let count = 0
    mocks.getResult.mockImplementation(() => Promise.resolve(result(301, ++count === 120 ? finalStatus : 'running')))
    await open()
    await submit()
    await vi.advanceTimersByTimeAsync(119000)
    expect(count).toBe(119)
    expect(mocks.showToast).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.get('.btn-submit-code').element.disabled).toBe(false)
    if (finalStatus === 'running') expect(mocks.showToast).toHaveBeenCalledExactlyOnceWith('判题超时，请重试', 'error')
    else expect(mocks.showToast).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(5000)
    expect(count).toBe(120)
  })

  it('聚焦刷新和初次加载倒序返回时保留最新作业状态', async () => {
    const initial = deferred(), first = deferred(), second = deferred()
    mocks.get.mockReturnValueOnce(initial.promise).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    await open()
    window.dispatchEvent(new Event('focus'))
    window.dispatchEvent(new Event('focus'))
    second.resolve({ data: { ...assignment, title: '最新作业', due_at: null } })
    await flushPromises()
    first.resolve({ data: { ...assignment, title: '旧聚焦作业', due_at: '2020-01-01T00:00:00Z' } })
    initial.resolve({ data: { ...assignment, title: '初始旧作业' } })
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('最新作业')
    expect(wrapper.get('.btn-self-test').element.disabled).toBe(false)
  })

  it.each(['failed', 'pending'])('初始化期间的聚焦刷新%s，仍采用成功的初始作业响应', async refreshState => {
    const initial = deferred(), refresh = deferred()
    mocks.get.mockReturnValueOnce(initial.promise).mockReturnValueOnce(refresh.promise)
    await open()
    window.dispatchEvent(new Event('focus'))
    if (refreshState === 'failed') refresh.reject(new Error('focus unavailable'))
    await flushPromises()
    initial.resolve({ data: assignment })
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('请求隔离作业')
    expect(wrapper.get('#code-editor').element.value).toBe('starter_10')
    expect(mocks.showToast).not.toHaveBeenCalled()
    if (refreshState === 'pending') {
      refresh.resolve({ data: { ...assignment, title: '聚焦更新的作业' } })
      await flushPromises()
      expect(wrapper.get('h1').text()).toBe('聚焦更新的作业')
    }
  })
})
