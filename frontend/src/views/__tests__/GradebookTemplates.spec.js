/** 批次 4：成绩册四类模板 / 待分配门禁 / 恢复课程默认 视图行为。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import GradebookView from '../GradebookView.vue'

const mocks = vi.hoisted(() => ({
  offerings: vi.fn(), read: vi.fn(), configure: vi.fn(), restoreDefault: vi.fn(), entries: vi.fn(),
  adjustment: vi.fn(), publish: vi.fn(), archive: vi.fn(), snapshots: vi.fn(), snapshot: vi.fn(),
  export: vi.fn(), downloadBlob: vi.fn(), user: { role: 'teacher' },
}))
vi.mock('../../api/gradebook.js', () => ({ gradebookAPI: mocks }))
vi.mock('../../stores/auth.js', () => ({ useAuthStore: () => ({ user: mocks.user }) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ params: {}, query: {} }) }))
vi.mock('../../api/accountManagement.js', async (original) => ({ ...await original(), downloadBlob: mocks.downloadBlob }))

const config = [
  { key: 'regular', title: '平时', weight: '100', weight_mode: 'custom', columns: [
    { key: 'manual-a', title: '人工一', weight: '60', source_kind: 'manual', source_id: null, selection: 'highest', allocation_status: 'allocated' },
    { key: 'assignment-5', title: '第一次作业', weight: null, source_kind: 'assignment', source_id: 5, selection: 'highest', allocation_status: 'pending' },
  ] },
  { key: 'labs', title: '实验', weight: '0', weight_mode: 'equal', columns: [
    { key: 'labs-main', title: '实验', weight: null, source_kind: 'manual', source_id: null, selection: 'highest', allocation_status: 'allocated' },
  ] },
]
const student = { student_id: 9, student_no: '0009', student_name: '陈同学', cells: {
  'manual-a': { state: 'scored', score: '80.00', source: { kind: 'manual' } },
  'assignment-5': { state: 'pending', score: null, source: null, message: '自动关联待分配' },
  'labs-main': { state: 'pending', score: null, source: null, message: '人工成绩待录入' },
}, calculated_score: null, final_score: null, adjustment: null }
const draftBook = {
  offering_id: 10, revision: 3, read_only: false, config, sources: [],
  draft: { rows: [student], blockers: [{ column: 'assignment-5', code: 'UNALLOCATED_WEIGHTS', message: '尚未分配' }], preview_hash: 'a'.repeat(64) },
  snapshot: null,
}
const stubs = { AppLayout: { template: '<div><slot /></div>' } }

describe('成绩册模板与待分配分项', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.user.role = 'teacher'
    mocks.offerings.mockResolvedValue({ data: { items: [{ id: 10, course_title: '课程', class_name: '一班', term_name: '秋季', read_only: false }] } })
    mocks.snapshots.mockResolvedValue({ data: { items: [] } })
    mocks.read.mockResolvedValue({ data: draftBook })
    mocks.configure.mockResolvedValue({})
    mocks.restoreDefault.mockResolvedValue({ data: draftBook })
  })

  async function open() {
    const wrapper = mount(GradebookView, { global: { stubs } })
    await flushPromises()
    return wrapper
  }

  it('待分配的自动关联分项显式提示，并阻止发布正式总评', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('第一次作业')
    expect(wrapper.text()).toContain('待分配')
    expect(wrapper.text()).toContain('尚未分配栏目内比例')
    const publishButton = wrapper.findAll('button').find(item => item.text() === '发布新成绩版本')
    expect(publishButton.element.disabled).toBe(true)
    expect(mocks.publish).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('自定义比例栏目可逐项填比例，等权栏目按分项数均分', async () => {
    const wrapper = await open()
    const rows = wrapper.findAll('.category-editor')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('二级比例合计 60%')
    expect(rows[0].findAll('input[type="number"]')).toHaveLength(3)  // 总占比 + 两个分项比例
    expect(rows[1].text()).toContain('该栏目按分项数量等权计分')
    expect(rows[1].findAll('input[type="number"]')).toHaveLength(1)  // 只有总占比
    expect(rows[1].text()).toContain('等权分项')
    wrapper.unmount()
  })

  it('切到自定义比例时清掉旧的显式比例，保存时按 null 提交空输入', async () => {
    const wrapper = await open()
    const rows = wrapper.findAll('.category-editor')
    // 等权栏目切到自定义：分项比例清空为 null，避免旧的显式比例继续生效。
    await rows[1].findAll('.ds-select-native')[0].setValue('custom')
    const form = wrapper.findAll('form').find(item => item.text().includes('保存栏目与比例'))
    await form.trigger('submit')
    await flushPromises()
    const saved = mocks.configure.mock.calls[0][1]
    expect(saved.categories[1].weight_mode).toBe('custom')
    expect(saved.categories[1].columns[0].weight).toBeNull()
    expect(saved.categories[0].columns[0].weight).toBe('60')
    wrapper.unmount()
  })

  it('恢复为课程默认需要原因并在确认后携带修订号提交', async () => {
    const wrapper = await open()
    const restoreButton = wrapper.findAll('button').find(item => item.text() === '恢复为课程默认')
    expect(restoreButton.element.disabled).toBe(true)
    const reasonInput = wrapper.get('input[placeholder="说明为什么改用课程模板覆盖本班栏目"]')
    await reasonInput.setValue('本学期改用课程统一模板')
    await restoreButton.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('恢复为课程默认？')
    const confirm = wrapper.findAll('button').find(item => item.text() === '确认恢复')
    await confirm.trigger('click')
    await flushPromises()
    expect(mocks.restoreDefault).toHaveBeenCalledWith('10', { expected_revision: 3, reason: '本学期改用课程统一模板' })
    expect(wrapper.text()).toContain('已按课程默认模板重建本班栏目与比例')
    wrapper.unmount()
  })

  it('学生看不到模板、待分配与恢复入口', async () => {
    mocks.user.role = 'student'
    mocks.read.mockResolvedValue({ data: { offering_id: 10, snapshot: { id: 's1', revision: 1, kind: 'published', payload: { config, rows: [student] } } } })
    const wrapper = await open()
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('恢复为课程默认')
    expect(wrapper.text()).not.toContain('待分配：')
    wrapper.unmount()
  })
})
