import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import GradebookView from '../GradebookView.vue'

const mocks = vi.hoisted(() => ({ offerings: vi.fn(), read: vi.fn(), configure: vi.fn(), entries: vi.fn(), adjustment: vi.fn(), publish: vi.fn(), archive: vi.fn(), snapshots: vi.fn(), snapshot: vi.fn(), export: vi.fn(), downloadBlob: vi.fn(), user: { role: 'teacher' } }))
vi.mock('../../api/gradebook.js', () => ({ gradebookAPI: mocks }))
vi.mock('../../stores/auth.js', () => ({ useAuthStore: () => ({ user: mocks.user }) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ params: {}, query: {} }) }))
vi.mock('../../api/accountManagement.js', async (original) => ({ ...await original(), downloadBlob: mocks.downloadBlob }))

const config = [['regular', '平时成绩', '30'], ['labs', '实验成绩', '40'], ['exam', '考核成绩', '30']].map(([key, title, weight]) => ({ key, title, weight, columns: [{ key: `${key}-main`, title, weight: '100', source_kind: 'manual', source_id: null, selection: 'highest' }] }))
const student = { student_id: 9, student_no: '0009', student_name: '陈同学', cells: Object.fromEntries(config.map(category => [category.columns[0].key, { state: 'scored', score: '80.00', source: { kind: 'manual' } }])), calculated_score: '80.00', final_score: '80.00', adjustment: null }
const published = { id: 'snapshot-1', revision: 3, kind: 'published', payload: { config, rows: [student], book_revision: 3 } }
const draft = { offering_id: 10, revision: 2, read_only: false, config, sources: [], draft: { rows: [student], blockers: [], preview_hash: 'a'.repeat(64) }, snapshot: published }
const stubs = { AppLayout: { template: '<div><slot /></div>' } }

async function open(data = draft) {
  mocks.read.mockResolvedValue({ data })
  const wrapper = mount(GradebookView, { global: { stubs } })
  await flushPromises()
  return wrapper
}

describe('班级课程成绩簿', () => {
  beforeEach(() => {
    vi.resetAllMocks(); mocks.user.role = 'teacher'
    mocks.offerings.mockResolvedValue({ data: { items: [{ id: 10, course_title: '共享课程', class_name: '授课一班', term_name: '秋季', read_only: false }] } })
    mocks.snapshots.mockResolvedValue({ data: { items: [] } })
    mocks.configure.mockResolvedValue({}); mocks.entries.mockResolvedValue({})
  })

  it('默认两级30/40/30配置保存时携带当前修订，出现冲突不会覆盖表单', async () => {
    const wrapper = await open({ ...draft, revision: 0, draft: null, snapshot: null })
    expect(wrapper.text()).toContain('一级比例合计 100%')
    const form = wrapper.findAll('form').find(item => item.text().includes('保存栏目与比例'))
    mocks.configure.mockRejectedValue({ response: { data: { detail: { message: '成绩簿已被其他操作修改，请刷新后重试' } } } })
    await form.trigger('submit'); await flushPromises()
    expect(mocks.configure).toHaveBeenCalledWith('10', { expected_revision: 0, categories: config })
    expect(wrapper.get('[role="alert"]').text()).toContain('已被其他操作修改')
    expect(mocks.read).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('分项录入使用当前班学生和栏目，携带修订后保存草稿', async () => {
    const wrapper = await open()
    await wrapper.get('button[aria-label="编辑陈同学的分项成绩"]').trigger('click')
    const form = wrapper.findAll('form').find(item => item.text().includes('保存分项草稿'))
    await form.find('input[type="number"]').setValue('75.5')
    await form.trigger('submit'); await flushPromises()
    const request = mocks.entries.mock.calls[0]
    expect(request[0]).toBe('10')
    expect(request[1].expected_revision).toBe(2)
    expect(request[1].entries[0]).toMatchObject({ student_id: 9, column_key: 'regular-main', state: 'scored', score: 75.5 })
    expect(wrapper.text()).toContain('官方成绩版本保持不变')
    wrapper.unmount()
  })

  it('有缺交待确认或待评分时禁用发布，仍显示具体处理说明', async () => {
    const wrapper = await open({ ...draft, draft: { rows: [student], blockers: [{ student_id: 9, column: 'regular-main', message: '待评分' }] } })
    const button = wrapper.findAll('button').find(item => item.text() === '发布新成绩版本')
    expect(button.element.disabled).toBe(true)
    expect(wrapper.text()).toContain('确认缺交会将未提交部分记 0 分')
    expect(mocks.publish).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('学生仅看到已发布本人快照和导出，没有权重、录入或调分入口', async () => {
    mocks.user.role = 'student'
    const wrapper = await open({ offering_id: 10, snapshot: published })
    expect(wrapper.text()).toContain('我的已发布成绩')
    expect(wrapper.text()).toContain('仅显示和导出你的成绩')
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('总成绩调分')
    mocks.export.mockResolvedValue({ data: new Blob(['csv']) })
    await wrapper.findAll('button').find(item => item.text() === '下载 CSV').trigger('click')
    await flushPromises()
    expect(mocks.export).toHaveBeenCalledWith('snapshot-1', 'csv')
    expect(mocks.downloadBlob).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('发布提交审核过的来源摘要，来源变化后保留原因并允许刷新重新审核', async () => {
    const wrapper = await open()
    const form = wrapper.findAll('form').find(item => item.text().includes('发布新成绩版本'))
    await form.get('input').setValue('Reviewed semester result')
    mocks.publish.mockRejectedValue({ response: { data: { detail: { message: '学生名单或来源成绩已变化，请刷新草稿并重新审核' } } } })
    await form.trigger('submit'); await flushPromises()
    expect(mocks.publish).toHaveBeenCalledWith('10', { expected_revision: 2, expected_preview_hash: 'a'.repeat(64), reason: 'Reviewed semester result' })
    expect(form.get('input').element.value).toBe('Reviewed semester result')
    expect(wrapper.get('[role="alert"]').text()).toContain('来源成绩已变化')
    expect(wrapper.findAll('button').some(item => item.text() === '刷新成绩状态')).toBe(true)
    wrapper.unmount()
  })
})
