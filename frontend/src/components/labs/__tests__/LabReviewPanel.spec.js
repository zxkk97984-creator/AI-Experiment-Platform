import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
vi.mock('../../../api/labs.js', () => ({ labsAPI: { getSubmission: vi.fn(), review: vi.fn(), regrade: vi.fn() } }))
import { labsAPI } from '../../../api/labs.js'
import LabReviewPanel from '../LabReviewPanel.vue'
let wrapper
const button = text => wrapper.findAll('button').find(b => b.text() === text)
describe('教师实验人工复核', () => {
  beforeEach(() => { vi.resetAllMocks(); setActivePinia(createPinia()); labsAPI.getSubmission.mockResolvedValue({ data: { enabled: true, assessment: { version: 1 }, grade: { total: 70, items: [], source: 'automatic' }, run: { status: 'needs_review' }, history: [] } }) })
  afterEach(() => wrapper?.unmount())
  it('requires a reason and sends it alongside the revised grade', async () => {
    wrapper = mount(LabReviewPanel, { props: { submissionId: 7, enabled: true } })
    await flushPromises()
    expect(button('发布复核成绩').element.disabled).toBe(true)
    await wrapper.find('input[type="number"]').setValue(92)
    await wrapper.findAll('textarea')[1].setValue('补充检查了算法实现')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(labsAPI.review).toHaveBeenCalledWith(7, { score: 92, feedback: '', reason: '补充检查了算法实现' })
  })
  it('does not fall back to legacy grading when lab result loading fails', async () => {
    labsAPI.getSubmission.mockRejectedValue(new Error('加载失败'))
    wrapper = mount(LabReviewPanel, { props: { submissionId: 7, enabled: true } })
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('加载失败')
    expect(wrapper.emitted('enabled')).toBeUndefined()
    expect(wrapper.find('form').exists()).toBe(false)
  })
})
