import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AssignmentDetailView from '../AssignmentDetailView.vue'

const mocks = vi.hoisted(() => ({
  leave: null, push: vi.fn(), get: vi.fn(), getQuestions: vi.fn(),
  list: vi.fn(), submit: vi.fn(), getResult: vi.fn(), sampleRun: vi.fn(), showToast: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '4' } }),
  useRouter: () => ({ push: mocks.push }),
  onBeforeRouteLeave: (hook) => { mocks.leave = hook },
}))
vi.mock('../../../api/assignments.js', () => ({ assignmentsAPI: mocks }))
vi.mock('../../../api/judge.js', () => ({ judgeAPI: mocks }))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast: mocks.showToast }) }))

const questions = [10, 20].map(id => ({
  id, title: `题目 ${id}`, description: '完成代码', starter_code: `starter_${id}`,
  public_cases: [], grading_mode: 'legacy',
}))
let wrapper

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function open() {
  wrapper = mount(AssignmentDetailView, {
    attachTo: document.body,
    global: { stubs: { AppLayout: { template: '<main><slot /></main>' } } },
  })
  await flushPromises()
  return wrapper
}

function attemptLeave() {
  const next = vi.fn()
  expect(mocks.leave).toBeTypeOf('function')
  mocks.leave({ fullPath: '/student/assignments' }, {}, next)
  return next
}

function beforeUnload() {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

async function submit() {
  await wrapper.get('.btn-submit-code').trigger('click')
  await flushPromises()
}

describe('作业页面内草稿与离开保护', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    mocks.leave = null
    mocks.push.mockResolvedValue(undefined)
    mocks.get.mockResolvedValue({ data: { id: 4, course_id: 1, title: '多题作业' } })
    mocks.getQuestions.mockResolvedValue({ data: questions })
    mocks.list.mockResolvedValue({ data: { items: [] } })
    mocks.submit.mockResolvedValue({ data: { id: 301 } })
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    vi.useRealTimers()
  })

  it('两道题分别编辑后往返切题保留草稿，未编辑的初始代码不阻止离开', async () => {
    await open()
    expect(beforeUnload()).toBe(false)
    expect(attemptLeave()).toHaveBeenCalledWith()
    await wrapper.get('#code-editor').setValue('draft_10')
    await wrapper.findAll('.q-dot')[1].trigger('click')
    expect(wrapper.get('#code-editor').element.value).toBe('starter_20')
    await wrapper.get('#code-editor').setValue('draft_20')
    await wrapper.findAll('.q-dot')[0].trigger('click')
    expect(wrapper.get('#code-editor').element.value).toBe('draft_10')
    await wrapper.findAll('.q-dot')[1].trigger('click')
    expect(wrapper.get('#code-editor').element.value).toBe('draft_20')
    expect(beforeUnload()).toBe(true)
  })

  it('历史代码建立干净基线，恢复后没有虚假的离开警告', async () => {
    mocks.list.mockResolvedValue({ data: { items: [{ id: 201, question_id: 10, status: 'wrong_answer' }] } })
    mocks.getResult.mockResolvedValue({ data: { id: 201, status: 'wrong_answer', code: 'historical_10' } })
    await open()
    expect(wrapper.get('#code-editor').element.value).toBe('historical_10')
    expect(beforeUnload()).toBe(false)
    expect(attemptLeave()).toHaveBeenCalledWith()
  })

  it('历史请求尚未完成时开始编辑，迟到历史不能覆盖草稿', async () => {
    const history = deferred()
    mocks.list.mockResolvedValue({ data: { items: [{ id: 201, question_id: 10, status: 'wrong_answer' }] } })
    mocks.getResult.mockReturnValue(history.promise)
    await open()
    await wrapper.get('#code-editor').setValue('new_draft_10')
    expect(beforeUnload()).toBe(true)
    history.resolve({ data: { id: 201, status: 'wrong_answer', code: 'older_10' } })
    await flushPromises()
    expect(wrapper.get('#code-editor').element.value).toBe('new_draft_10')
    expect(beforeUnload()).toBe(true)
  })

  it('已编辑题目的草稿不会被再次切回时回载的历史覆盖', async () => {
    mocks.list.mockResolvedValue({ data: { items: [{ id: 201, question_id: 10, status: 'wrong_answer' }] } })
    mocks.getResult.mockResolvedValue({ data: { id: 201, status: 'wrong_answer', code: 'historical_10' } })
    await open()
    await wrapper.get('#code-editor').setValue('edited_10')
    await wrapper.findAll('.q-dot')[1].trigger('click')
    await wrapper.findAll('.q-dot')[0].trigger('click')
    await flushPromises()
    expect(wrapper.get('#code-editor').element.value).toBe('edited_10')
    expect(beforeUnload()).toBe(true)
  })

  it('有效提交成功后当前内容成为基线，失败和无ID响应均保留草稿', async () => {
    await open()
    await wrapper.get('#code-editor').setValue('answer_10')
    mocks.submit.mockRejectedValueOnce(new Error('offline'))
    await submit()
    expect(beforeUnload()).toBe(true)
    mocks.submit.mockResolvedValueOnce({ data: {} })
    await submit()
    expect(beforeUnload()).toBe(true)
    await submit()
    expect(mocks.submit).toHaveBeenLastCalledWith({ question_id: 10, code: 'answer_10' })
    expect(beforeUnload()).toBe(false)
    expect(attemptLeave()).toHaveBeenCalledWith()
  })

  it('提交途中继续编辑时，成功响应不能清除更新的草稿', async () => {
    const request = deferred()
    mocks.submit.mockReturnValue(request.promise)
    await open()
    await wrapper.get('#code-editor').setValue('submitted_10')
    await submit()
    await wrapper.get('#code-editor').setValue('newer_10')
    request.resolve({ data: { id: 301 } })
    await flushPromises()
    expect(wrapper.get('#code-editor').element.value).toBe('newer_10')
    expect(beforeUnload()).toBe(true)
    await wrapper.get('#code-editor').setValue('submitted_10')
    expect(beforeUnload()).toBe(false)
  })

  it('提交一题不会清除另一题未提交的修改', async () => {
    await open()
    await wrapper.get('#code-editor').setValue('draft_10')
    await wrapper.findAll('.q-dot')[1].trigger('click')
    await wrapper.get('#code-editor').setValue('answer_20')
    await submit()
    expect(beforeUnload()).toBe(true)
    await wrapper.findAll('.q-dot')[0].trigger('click')
    expect(wrapper.get('#code-editor').element.value).toBe('draft_10')
  })

  it('离开弹窗取消保留草稿，确认后继续原目标导航', async () => {
    const navigation = deferred()
    mocks.push.mockReturnValue(navigation.promise)
    await open()
    await wrapper.get('#code-editor').setValue('draft_10')
    expect(attemptLeave()).toHaveBeenCalledWith(false)
    await flushPromises()
    expect(wrapper.get('[role="dialog"]').text()).toContain('未提交')
    await wrapper.get('[role="dialog"] .btn-ghost').trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(wrapper.get('#code-editor').element.value).toBe('draft_10')
    expect(mocks.push).not.toHaveBeenCalled()
    attemptLeave()
    await flushPromises()
    await wrapper.get('[role="dialog"] .btn-primary').trigger('click')
    expect(mocks.push).toHaveBeenCalledWith('/student/assignments')
    expect(attemptLeave()).toHaveBeenCalledWith()
    navigation.resolve()
    await flushPromises()
  })

  it('确认离开被其他导航守卫取消后，再次离开仍保护草稿', async () => {
    mocks.push.mockResolvedValue({ type: 4 })
    await open()
    await wrapper.get('#code-editor').setValue('draft_10')
    attemptLeave()
    await flushPromises()
    await wrapper.get('[role="dialog"] .btn-primary').trigger('click')
    await flushPromises()
    expect(attemptLeave()).toHaveBeenCalledWith(false)
    expect(wrapper.get('#code-editor').element.value).toBe('draft_10')
  })

  it('历史请求期间卸载，迟到响应不会重新注册页面定时器或离开监听', async () => {
    const history = deferred()
    mocks.list.mockResolvedValue({ data: { items: [{ id: 201, question_id: 10, status: 'wrong_answer' }] } })
    mocks.getResult.mockReturnValue(history.promise)
    await open()
    await wrapper.get('#code-editor').setValue('draft_10')
    wrapper.unmount()
    wrapper = null
    history.resolve({ data: { id: 201, status: 'wrong_answer', code: 'historical_10' } })
    await flushPromises()
    expect(beforeUnload()).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('撤销到原代码后可以离开，卸载后移除刷新保护', async () => {
    await open()
    await wrapper.get('#code-editor').setValue('draft_10')
    expect(beforeUnload()).toBe(true)
    await wrapper.get('#code-editor').setValue('starter_10')
    expect(beforeUnload()).toBe(false)
    await wrapper.get('#code-editor').setValue('draft_10')
    wrapper.unmount()
    wrapper = null
    expect(beforeUnload()).toBe(false)
  })
})
