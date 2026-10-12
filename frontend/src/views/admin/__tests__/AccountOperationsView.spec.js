import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AccountOperationsView from '../AccountOperationsView.vue'

const mocks = vi.hoisted(() => ({ push: vi.fn(), listTerms: vi.fn(), listClasses: vi.fn(), batches: vi.fn(), previewReset: vi.fn(), previewResetFile: vi.fn(), resetTemplate: vi.fn(), downloadBlob: vi.fn() }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }), useRouter: () => ({ push: mocks.push }) }))
vi.mock('../../../api/academics.js', () => ({ academicsAPI: mocks }))
vi.mock('../../../api/accountManagement.js', async (original) => ({ ...await original(), accountAPI: mocks, downloadBlob: mocks.downloadBlob }))
let wrapper
async function open() {
  wrapper = mount(AccountOperationsView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' }, TeacherPagination: true, RouterLink: true } } })
  await flushPromises()
  return wrapper.findAll('.account-panel--operation')[1]
}
function chooseFile(panel) {
  const file = new File(['synthetic'], 'reset.xlsx')
  Object.defineProperty(panel.get('input[type=file]').element, 'files', { configurable: true, value: [file] })
  return { file, input: panel.get('input[type=file]') }
}
beforeEach(() => {
  vi.resetAllMocks()
  mocks.listTerms.mockResolvedValue({ data: { items: [], total: 0 } })
  mocks.listClasses.mockResolvedValue({ data: { items: [{ id: 1, name: '测试班', code: 'T1', status: 'active' }], total: 1 } })
  mocks.batches.mockResolvedValue({ data: { items: [], total: 0 } })
  mocks.previewResetFile.mockResolvedValue({ data: { id: 'excel-batch' } })
  mocks.previewReset.mockResolvedValue({ data: { id: 'query-batch' } })
})
afterEach(() => wrapper?.unmount())

describe('按 Excel 名单批量重置', () => {
  it('选择匹配范围后默认 Excel，无文件不能误重置全部学生', async () => {
    const panel = await open()
    await panel.get('select').setValue('filter')
    expect(panel.get('input[type=radio][value=file]').element.checked).toBe(true)
    expect(panel.get('button[type=submit], button.operation-submit').element.disabled).toBe(true)
    await panel.get('form').trigger('submit'); await flushPromises()
    expect(mocks.previewReset).not.toHaveBeenCalled()
    expect(mocks.previewResetFile).not.toHaveBeenCalled()
    expect(wrapper.get('[role=alert]').text()).toContain('请选择重置名单文件')
  })
  it('上传仅调用文件预览，跳转逐行确认页面', async () => {
    const panel = await open(); await panel.get('select').setValue('filter')
    const { file, input } = chooseFile(panel); await input.trigger('change')
    await panel.get('form').trigger('submit'); await flushPromises()
    expect(mocks.previewResetFile).toHaveBeenCalledOnce()
    expect(mocks.previewResetFile.mock.calls[0][0].get('file')).toBe(file)
    expect(mocks.previewReset).not.toHaveBeenCalled()
    expect(mocks.push).toHaveBeenCalledWith('/admin/batches/excel-batch')
  })
  it('保留显式条件筛选，与班级选择独立', async () => {
    const panel = await open(); await panel.get('select').setValue('filter')
    await panel.get('input[type=radio][value=query]').setValue(true)
    await panel.get('input:not([type])').setValue('  0001  ')
    await panel.get('form').trigger('submit'); await flushPromises()
    expect(mocks.previewReset).toHaveBeenCalledWith({ all_matching: true, q: '0001' })
    expect(mocks.previewResetFile).not.toHaveBeenCalled()
    await panel.get('select').setValue('class')
    await panel.findAll('select')[1].setValue('1')
    await panel.get('form').trigger('submit'); await flushPromises()
    expect(mocks.previewReset).toHaveBeenLastCalledWith({ class_id: 1 })
  })
  it('提供模板下载并解释代理超限，不跳到确认页', async () => {
    const panel = await open(); await panel.get('select').setValue('filter')
    const blob = new Blob(['fixture'])
    mocks.resetTemplate.mockResolvedValue({ data: blob })
    await panel.get('.reset-template').trigger('click'); await flushPromises()
    expect(mocks.downloadBlob).toHaveBeenCalledWith(blob, '学生密码重置名单.xlsx')
    const { input } = chooseFile(panel); await input.trigger('change')
    mocks.previewResetFile.mockRejectedValue({ response: { status: 413 } })
    await panel.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.get('[role=alert]').text()).toContain('最大 5 MiB')
    expect(mocks.push).not.toHaveBeenCalled()
  })
})
