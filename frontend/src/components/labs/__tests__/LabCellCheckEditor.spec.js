import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import LabCellCheckEditor from '../LabCellCheckEditor.vue'
import LabLeaderboard from '../LabLeaderboard.vue'
import { defaultConfig } from '../labHelpers.js'
vi.mock('../../../api/labs.js', () => ({ labsAPI: { uploadCheckScript: vi.fn(), leaderboard: vi.fn() } }))
import { labsAPI } from '../../../api/labs.js'

describe('版本化 Cell 检查配置', () => {
  beforeEach(() => vi.clearAllMocks())
  it('adds presets using the stable cell ID and removes a rule with its criterion', async () => {
    const config = defaultConfig()
    const wrapper = mount(LabCellCheckEditor, { props: { modelValue: config, cells: [{ id: 'stable-cell', type: 'code' }], kind: 'module', entryId: 1 } })
    await wrapper.findAll('button').find(b => b.text() === '添加数值近似相等').trigger('click')
    expect(config.cell_checks).toHaveLength(1)
    expect(config.cell_checks[0].cell_id).toBe('stable-cell')
    expect(config.criteria.at(-1).id).toBe(config.cell_checks[0].id)
    config.criteria = config.criteria.filter(c => c.source !== 'cell')
    await wrapper.setProps({ modelValue: { ...config } })
    await flushPromises()
    expect(wrapper.props('modelValue').cell_checks).toHaveLength(0)
    wrapper.unmount()
  })

  it('requires a visible code cell before adding a preset', () => {
    const wrapper = mount(LabCellCheckEditor, { props: { modelValue: defaultConfig(), cells: [{ id: 'hidden', type: 'code', source_hidden: true }], kind: 'module', entryId: 1 } })
    expect(wrapper.findAll('button').every(button => button.element.disabled)).toBe(true)
    wrapper.unmount()
  })

  function pythonEditor(offeringId = 11) {
    const config = reactive(defaultConfig())
    config.criteria = [{ id: 'custom', source: 'cell', name: '自定义检查', max_score: 100 }]
    config.cell_checks = [{ id: 'custom', cell_id: 'stable-cell', kind: 'python', script: '# previous\n' }]
    const wrapper = mount(LabCellCheckEditor, { props: { modelValue: config,
      cells: [{ id: 'stable-cell', type: 'code' }], kind: 'module', entryId: 1, offeringId } })
    return { wrapper, config }
  }
  async function selectScript(wrapper) {
    const file = new File(['# uploaded\n'], 'check.py', { type: 'text/plain' })
    const input = wrapper.find('input[type="file"]')
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    return file
  }
  function pendingUpload() {
    let resolve, reject
    labsAPI.uploadCheckScript.mockReturnValue(new Promise((res, rej) => { resolve = res; reject = rej }))
    return { resolve, reject }
  }

  it.each([11, null])('uploads into the current draft without saving or publishing (offering %s)', async offeringId => {
    labsAPI.uploadCheckScript.mockResolvedValue({ data: { script: '# uploaded\n' } })
    const { wrapper, config } = pythonEditor(offeringId)
    const file = await selectScript(wrapper)
    await flushPromises()
    expect(labsAPI.uploadCheckScript).toHaveBeenCalledWith('module', 1, file, offeringId)
    expect(config.cell_checks[0].script).toBe('# uploaded\n')
    expect(wrapper.text()).toContain('上传仅填入当前草稿')
    wrapper.unmount()
  })

  it('preserves the current script and makes the upload retryable after an error', async () => {
    labsAPI.uploadCheckScript.mockRejectedValue({ response: { data: { detail: { message: 'Python 语法无效' } } } })
    const { wrapper, config } = pythonEditor()
    await selectScript(wrapper)
    await flushPromises()
    expect(config.cell_checks[0].script).toBe('# previous\n')
    expect(wrapper.find('[role="alert"]').text()).toBe('Python 语法无效')
    expect(wrapper.find('input[type="file"]').element.disabled).toBe(false)
    wrapper.unmount()
  })

  it.each(['offering', 'entry', 'kind', 'config', 'remove', 'unmount', 'edit'])('ignores a late upload after %s changes', async change => {
    const request = pendingUpload()
    const { wrapper, config } = pythonEditor()
    const rule = config.cell_checks[0]
    await selectScript(wrapper)
    if (change === 'offering') await wrapper.setProps({ offeringId: 12 })
    if (change === 'entry') await wrapper.setProps({ entryId: 2 })
    if (change === 'kind') await wrapper.setProps({ kind: 'lesson' })
    if (change === 'config') await wrapper.setProps({ modelValue: JSON.parse(JSON.stringify(config)) })
    if (change === 'remove') {
      config.criteria = []
      await flushPromises()
    }
    if (change === 'unmount') wrapper.unmount()
    if (change === 'edit') await wrapper.find('textarea').setValue('# typed while uploading\n')
    request.resolve({ data: { script: '# stale upload\n' } })
    await flushPromises()
    expect(rule.script).toBe(change === 'edit' ? '# typed while uploading\n' : '# previous\n')
    if (change !== 'unmount') wrapper.unmount()
  })

  it('does not let the previous class response clear a newer pending upload or overwrite its script', async () => {
    const first = pendingUpload()
    const { wrapper, config } = pythonEditor()
    await selectScript(wrapper)
    await wrapper.setProps({ offeringId: 12 })
    const second = pendingUpload()
    await selectScript(wrapper)
    first.resolve({ data: { script: '# old class\n' } })
    await flushPromises()
    expect(wrapper.find('input[type="file"]').element.disabled).toBe(true)
    expect(config.cell_checks[0].script).toBe('# previous\n')
    second.resolve({ data: { script: '# new class\n' } })
    await flushPromises()
    expect(config.cell_checks[0].script).toBe('# new class\n')
    expect(wrapper.find('input[type="file"]').element.disabled).toBe(false)
    wrapper.unmount()
  })

  it('keeps failures from a previous class out of the current editor', async () => {
    const request = pendingUpload()
    const { wrapper } = pythonEditor()
    await selectScript(wrapper)
    await wrapper.setProps({ offeringId: 12 })
    request.reject(new Error('old class request failed'))
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('排行榜公开投影', () => {
  it('loads only on request and renders anonymous ties without changing a score', async () => {
    labsAPI.leaderboard.mockResolvedValue({ data: { named: false, assessment_version: 2, grade_selection: 'highest', metric: null, items: [
      { rank: 1, display_name: '同学 ABC123', is_me: true, score: 90, program_elapsed_seconds: 2 },
      { rank: 1, display_name: '同学 DEF456', is_me: false, score: 90, program_elapsed_seconds: null },
    ] } })
    const wrapper = mount(LabLeaderboard, { props: { recordId: 4 } })
    expect(labsAPI.leaderboard).not.toHaveBeenCalled()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(labsAPI.leaderboard).toHaveBeenCalledWith(4)
    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    expect(wrapper.text()).toContain('同学 ABC123（我）')
    expect(wrapper.text()).toContain('名次不参与总分')
    wrapper.unmount()
  })
})
