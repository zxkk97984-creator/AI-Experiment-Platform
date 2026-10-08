import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
vi.mock('../../../api/labs.js', () => ({ labsAPI: { getAssessment: vi.fn(), saveAssessment: vi.fn(), baseline: vi.fn(), publish: vi.fn(), getRun: vi.fn(), generateRubric: vi.fn() } }))
import { labsAPI } from '../../../api/labs.js'
import LabAssessmentPanel from '../LabAssessmentPanel.vue'
import { defaultConfig, rubricErrors } from '../labHelpers.js'
let wrapper
let saveTemplate
const button = text => wrapper.findAll('button').find(b => b.text() === text)
async function setup({ confirmed = true, validated = false } = {}) {
  const config = { ...defaultConfig(), requirements: '用函数实现归一化并解释结果', confirmed }
  const assessment = { id: 4, version: 1, config, status: 'draft', validated }
  labsAPI.getAssessment.mockResolvedValue({ data: { draft: assessment, published: null } })
  labsAPI.saveAssessment.mockImplementation(async (_kind, _id, next) => ({ data: { ...assessment, config: next } }))
  saveTemplate = vi.fn().mockResolvedValue(true)
  wrapper = mount(LabAssessmentPanel, { props: { kind: 'module', entryId: 3, saveTemplate }, global: { stubs: { EnvironmentProfilePicker: true } } })
  await flushPromises()
}
describe('实验备课发布门禁', () => {
  beforeEach(() => { vi.resetAllMocks(); setActivePinia(createPinia()) })
  afterEach(() => wrapper?.unmount())
  it('requires explicit teacher confirmation before reference evaluation', async () => {
    await setup({ confirmed: false })
    expect(button('保存并参考试跑').element.disabled).toBe(true)
    expect(button('发布实验评测').element.disabled).toBe(true)
  })
  it('saves notebook and assessment before enqueuing a baseline', async () => {
    await setup()
    labsAPI.baseline.mockResolvedValue({ data: { id: 19, kind: 'baseline', status: 'queued' } })
    await button('保存并参考试跑').trigger('click')
    await flushPromises()
    expect(saveTemplate).toHaveBeenCalledOnce()
    expect(labsAPI.saveAssessment).toHaveBeenCalledWith('module', 3, expect.objectContaining({ confirmed: true }))
    expect(saveTemplate.mock.invocationCallOrder[0]).toBeLessThan(labsAPI.saveAssessment.mock.invocationCallOrder[0])
    expect(labsAPI.saveAssessment.mock.invocationCallOrder[0]).toBeLessThan(labsAPI.baseline.mock.invocationCallOrder[0])
    expect(labsAPI.publish).not.toHaveBeenCalled()
  })
  it('does not enqueue the reference run when notebook save has failed', async () => {
    await setup()
    saveTemplate.mockResolvedValue(false)
    await button('保存并参考试跑').trigger('click')
    await flushPromises()
    expect(labsAPI.baseline).not.toHaveBeenCalled()
    expect(labsAPI.saveAssessment).not.toHaveBeenCalled()
  })
  it('invalidates publication eligibility when teacher edits a validated rubric', async () => {
    await setup({ validated: true })
    expect(button('发布实验评测').element.disabled).toBe(false)
    await wrapper.find('textarea').setValue('新的算法要求')
    expect(button('发布实验评测').element.disabled).toBe(true)
  })
  it('keeps an altered notebook ineligible even after the ordinary toolbar saves it', async () => {
    await setup({ validated: true })
    await wrapper.setProps({ templateSignature: 'new-notebook-content', templateDirty: false })
    expect(button('发布实验评测').element.disabled).toBe(true)
  })
  it('never exposes a blank replacement form after failing to load an existing draft', async () => {
    labsAPI.getAssessment.mockRejectedValue(new Error('网络断开'))
    wrapper = mount(LabAssessmentPanel, { props: { kind: 'module', entryId: 3, saveTemplate: vi.fn() }, global: { stubs: { EnvironmentProfilePicker: true } } })
    await flushPromises()
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('网络断开')
  })
  it('requires all AI levels to span zero through the criterion maximum', () => {
    const config = defaultConfig()
    config.criteria[0].levels[0].points = 1
    expect(rubricErrors(config)).toContain('实现与训练方法的等级须包含零分与满分')
  })
  it('uses class assessment routes and keeps shared content controls unavailable', async () => {
    const config = { ...defaultConfig(), confirmed: true }
    labsAPI.getAssessment.mockResolvedValue({ data: { draft: { id: 5, version: 1, config, status: 'draft', validated: false }, published: null } })
    wrapper = mount(LabAssessmentPanel, { props: { kind: 'module', entryId: 3, offeringId: 11, saveTemplate: vi.fn().mockResolvedValue(true) }, global: { stubs: { EnvironmentProfilePicker: true } } })
    await flushPromises()
    expect(labsAPI.getAssessment).toHaveBeenCalledWith('module', 3, 11)
    expect(button('AI 生成量表').element.disabled).toBe(true)
    expect(button('使用内置教学模板')).toBeUndefined()
    expect(wrapper.find('select').element.disabled).toBe(true)
  })
})
