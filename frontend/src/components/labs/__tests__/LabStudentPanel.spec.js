import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
vi.mock('../../../api/labs.js', () => ({ labsAPI: { getOverview: vi.fn(), artifactFiles: vi.fn(), train: vi.fn(), validate: vi.fn(), uploadArtifact: vi.fn(), cancelRun: vi.fn(), deleteArtifact: vi.fn(), getSubmission: vi.fn(), appeal: vi.fn() } }))
import { labsAPI } from '../../../api/labs.js'
import { useExperimentStore } from '../../../stores/experiment.js'
import LabStudentPanel from '../LabStudentPanel.vue'
import { defaultConfig } from '../labHelpers.js'
const overview = (overrides = {}) => ({ enabled: true, assessment: { id: 4, version: 1, config: defaultConfig('tabular_classification') }, attempts_used: 0, max_submissions: 5, runs: [], artifacts: [{ id: 8, status: 'ready', source: 'platform', files: [] }], submissions: [], ...overrides })
let wrappers = []
async function setup(data = overview()) {
  setActivePinia(createPinia())
  labsAPI.getOverview.mockResolvedValue({ data })
  const store = useExperimentStore()
  store.recordId = 5
  store.recordRevision = 3
  vi.spyOn(store, 'flushSave').mockImplementation(async () => { store.recordRevision = 4; return true })
  const wrapper = mount(LabStudentPanel)
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, store }
}
const button = (wrapper, text) => wrapper.findAll('button').find(b => b.text().includes(text))
describe('学生实验训练与提交', () => {
  beforeEach(() => vi.resetAllMocks())
  afterEach(() => { wrappers.forEach(w => w.unmount()); wrappers = []; vi.useRealTimers() })
  it('loads more artifact files only on request', async () => {
    const files = Array.from({ length: 50 }, (_, i) => ({ id: i + 1, path: `output-${i}` }))
    const { wrapper } = await setup(overview({ artifacts: [{ id: 8, status: 'ready', source: 'platform', files, files_truncated: true, file_count: 51 }] }))
    expect(labsAPI.artifactFiles).not.toHaveBeenCalled()
    labsAPI.artifactFiles.mockResolvedValue({ data: { items: [{ id: 51, path: 'model-final.pt' }], total: 51 } })
    await button(wrapper, '加载更多文件').trigger('click')
    await flushPromises()
    expect(labsAPI.artifactFiles).toHaveBeenCalledWith(8, 2)
    expect(wrapper.text()).toContain('model-final.pt')
    expect(button(wrapper, '加载更多文件')).toBeUndefined()
  })
  it('does not select an artifact from an incompatible assessment version', async () => {
    const { wrapper, store } = await setup(overview({ artifacts: [{ id: 8, status: 'ready', compatible: false, files: [] }] }))
    expect(button(wrapper, '保存并正式提交').element.disabled).toBe(true)
    const submit = vi.spyOn(store, 'submitExperiment')
    await wrapper.vm.submit()
    expect(submit).not.toHaveBeenCalled()
  })
  it('flushes edits before training and uses the resulting revision', async () => {
    const { wrapper, store } = await setup()
    labsAPI.train.mockResolvedValue({ data: { id: 9 } })
    await button(wrapper, '保存并开始训练').trigger('click')
    await flushPromises()
    expect(store.flushSave).toHaveBeenCalledOnce()
    expect(labsAPI.train).toHaveBeenCalledWith(5, expect.objectContaining({ expected_record_revision: 4, device: 'cpu' }))
    expect(labsAPI.train.mock.calls[0][1].client_request_id).toMatch(/^[0-9a-f-]{36}$/)
  })
  it('does not train after a failed save', async () => {
    const { wrapper, store } = await setup()
    store.flushSave.mockResolvedValue(false)
    await button(wrapper, '保存并开始训练').trigger('click')
    await flushPromises()
    expect(labsAPI.train).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请先保存 Notebook')
  })
  it('reuses a training request id after an uncertain network failure', async () => {
    const { wrapper } = await setup()
    labsAPI.train.mockRejectedValueOnce(new Error('网络断开')).mockResolvedValue({ data: {} })
    await button(wrapper, '保存并开始训练').trigger('click')
    await flushPromises()
    await button(wrapper, '保存并开始训练').trigger('click')
    await flushPromises()
    expect(labsAPI.train.mock.calls[0][1].client_request_id).toBe(labsAPI.train.mock.calls[1][1].client_request_id)
  })
  it('public validation does not call formal submission', async () => {
    const { wrapper, store } = await setup()
    const submit = vi.spyOn(store, 'submitExperiment')
    labsAPI.validate.mockResolvedValue({ data: {} })
    await button(wrapper, '公开验证').trigger('click')
    await flushPromises()
    expect(labsAPI.validate).toHaveBeenCalledWith(5, expect.objectContaining({ artifact_id: 8 }))
    expect(submit).not.toHaveBeenCalled()
  })
  it('prevents submitting from either the panel or exposed menu action when attempts are exhausted', async () => {
    const { wrapper, store } = await setup(overview({ attempts_used: 5 }))
    const submit = vi.spyOn(store, 'submitExperiment')
    expect(button(wrapper, '保存并正式提交').element.disabled).toBe(true)
    await wrapper.vm.submit()
    expect(submit).not.toHaveBeenCalled()
  })
  it('does not enqueue another formal submission while evaluation is running', async () => {
    const { wrapper, store } = await setup(overview({ runs: [{ id: 4, kind: 'evaluate', status: 'queued' }] }))
    const submit = vi.spyOn(store, 'submitExperiment')
    expect(button(wrapper, '正在评测上次提交').element.disabled).toBe(true)
    await wrapper.vm.submit()
    expect(submit).not.toHaveBeenCalled()
  })
  it('keeps legacy experiments hidden without submitting or training', async () => {
    const { wrapper } = await setup({ enabled: false })
    expect(wrapper.find('section').exists()).toBe(false)
    expect(labsAPI.train).not.toHaveBeenCalled()
  })
  it('requires confirmation before deleting a model and keeps referenced-model errors visible', async () => {
    const { wrapper } = await setup()
    labsAPI.deleteArtifact.mockRejectedValue({ response: { data: { detail: { message: '成果已被正式提交引用' } } } })
    await button(wrapper, '删除成果').trigger('click')
    expect(labsAPI.deleteArtifact).not.toHaveBeenCalled()
    const dialog = wrapper.get('[role="dialog"]')
    await dialog.findAll('button').at(-1).trigger('click')
    await flushPromises()
    expect(labsAPI.deleteArtifact).toHaveBeenCalledWith(8)
    expect(wrapper.text()).toContain('成果已被正式提交引用')
  })
  it('stops polling queued jobs when the component leaves the page', async () => {
    vi.useFakeTimers()
    const { wrapper } = await setup(overview({ runs: [{ id: 1, kind: 'train', status: 'queued' }] }))
    expect(labsAPI.getOverview).toHaveBeenCalledOnce()
    wrapper.unmount()
    wrappers = []
    await vi.advanceTimersByTimeAsync(15000)
    expect(labsAPI.getOverview).toHaveBeenCalledOnce()
  })
})
