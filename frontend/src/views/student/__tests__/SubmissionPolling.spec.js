import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

vi.mock('vue-router', () => ({ useRoute: () => ({ params: { id: '1' } }), useRouter: () => ({}) }))
vi.mock('../../../api/judge.js', () => ({ judgeAPI: { getResult: vi.fn() } }))
import { judgeAPI } from '../../../api/judge.js'
import SubmissionView from '../SubmissionView.vue'

let wrapper
const open = async () => {
  wrapper = mount(SubmissionView, { global: { stubs: { AppLayout: { template: '<main><slot /></main>' }, StudentAIGradingResult: true } } })
  await flushPromises()
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
})
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks(); vi.useRealTimers() })

it.each([403, 404])('stops permanent %s responses with an actionable error', async status => {
  judgeAPI.getResult.mockRejectedValue({ response: { status } })
  await open()
  await vi.advanceTimersByTimeAsync(60000)
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(1)
  expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('正在读取提交结果')
})

it('backs off five failures then allows an explicit retry to completion', async () => {
  judgeAPI.getResult.mockRejectedValue({ response: { status: 503 } })
  await open()
  await vi.advanceTimersByTimeAsync(60000)
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(5)
  judgeAPI.getResult.mockResolvedValue({ data: { id: 1, status: 'accepted' } })
  await wrapper.get('[role="alert"] button').trigger('click')
  await flushPromises()
  await vi.advanceTimersByTimeAsync(10000)
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(6)
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
})

it('never overlaps a slow request and stops after unmount', async () => {
  let resolve
  judgeAPI.getResult.mockImplementation(() => new Promise(done => { resolve = done }))
  await open()
  await vi.advanceTimersByTimeAsync(10000)
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(1)
  wrapper.unmount()
  resolve({ data: { id: 1, status: 'queued' } })
  await flushPromises()
  await vi.advanceTimersByTimeAsync(10000)
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(1)
})

it('pauses hidden pages and resumes on visibility', async () => {
  judgeAPI.getResult.mockResolvedValue({ data: { id: 1, status: 'queued' } })
  await open()
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  document.dispatchEvent(new Event('visibilitychange'))
  await vi.advanceTimersByTimeAsync(10000)
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(1)
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  document.dispatchEvent(new Event('visibilitychange'))
  await flushPromises()
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(2)
})

it('waits for AI details on graded results', async () => {
  judgeAPI.getResult.mockResolvedValueOnce({ data: { id: 1, status: 'graded' } })
    .mockResolvedValue({ data: { id: 1, status: 'graded', grading_breakdown: {} } })
  await open()
  await vi.advanceTimersByTimeAsync(1500)
  await vi.advanceTimersByTimeAsync(6000)
  expect(judgeAPI.getResult).toHaveBeenCalledTimes(2)
})
