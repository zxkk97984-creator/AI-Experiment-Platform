import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
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

describe('成绩簿快照与导出上下文', () => {
  let wrapper
  const otherStudent = { ...student, student_id: 19, student_name: '二班学生' }
  const older = { ...published, id: 'older-1', revision: 1,
    payload: { config, rows: [{ ...student, student_name: '一班旧版本学生' }] } }
  const newer = { ...published, id: 'newer-1', revision: 2,
    payload: { config, rows: [{ ...student, student_name: '一班新版本学生' }] } }
  const otherBook = { ...draft, offering_id: 20, draft: { ...draft.draft, rows: [otherStudent] },
    snapshot: { ...published, id: 'other-20', revision: 8, payload: { config, rows: [otherStudent] } } }
  function deferred() {
    let resolve, reject
    const promise = new Promise((yes, no) => { resolve = yes; reject = no })
    return { promise, resolve, reject }
  }
  async function openBooks() {
    wrapper = mount(GradebookView, { global: { stubs } })
    await flushPromises()
  }
  async function choose(id) { await wrapper.get('.offering-picker select').setValue(String(id)); await flushPromises() }
  async function click(text) {
    await wrapper.findAll('button').find(button => button.text().includes(text)).trigger('click')
    await flushPromises()
  }
  beforeEach(() => {
    vi.resetAllMocks(); mocks.user.role = 'teacher'
    mocks.offerings.mockResolvedValue({ data: { items: [10, 20].map(id => ({ id,
      course_title: '共享课程', class_name: `${id}班`, term_name: '秋季', read_only: false })) } })
    mocks.read.mockImplementation(id => Promise.resolve({ data: String(id) === '10' ? draft : otherBook }))
    mocks.snapshots.mockResolvedValue({ data: { items: [older, newer] } })
    mocks.snapshot.mockImplementation(id => Promise.resolve({ data: id === older.id ? older : newer }))
  })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it.each(['success', 'failure'])('换班后旧快照%s不覆盖新班成绩或错误状态', async outcome => {
    const pending = deferred()
    mocks.snapshot.mockReturnValueOnce(pending.promise)
    await openBooks()
    await click('版本 1 ·')
    await choose(20)
    expect(wrapper.get('.grade-table-scroll').text()).toContain('二班学生')
    if (outcome === 'success') pending.resolve({ data: older })
    else pending.reject({ response: { data: { detail: { message: '一班旧请求失败' } } } })
    await flushPromises()
    expect(wrapper.get('.grade-table-scroll').text()).toContain('二班学生')
    expect(wrapper.text()).not.toContain('一班旧版本学生')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it.each(['success', 'failure'])('两版本请求倒序%s只保留最后选择的版本', async outcome => {
    const first = deferred()
    mocks.snapshot.mockReturnValueOnce(first.promise)
    await openBooks()
    await click('版本 1 ·')
    await click('版本 2 ·')
    if (outcome === 'success') first.resolve({ data: older })
    else first.reject(new Error('old version failure'))
    await flushPromises()
    expect(wrapper.get('.grade-table-scroll').text()).toContain('一班新版本学生')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('历史请求途中返回草稿，旧响应不能重新打开历史版本', async () => {
    const pending = deferred()
    await openBooks()
    await click('版本 1 ·')
    mocks.snapshot.mockReturnValueOnce(pending.promise)
    await click('版本 2 ·')
    await click('返回当前草稿')
    pending.resolve({ data: newer })
    await flushPromises()
    expect(wrapper.text()).toContain('当前成绩草稿')
    expect(wrapper.get('.grade-table-scroll').text()).toContain('陈同学')
    expect(wrapper.text()).not.toContain('历史版本 2')
  })

  it('清空班级选择立即清理数据并作废旧快照', async () => {
    const pending = deferred()
    mocks.snapshot.mockReturnValueOnce(pending.promise)
    await openBooks()
    await click('版本 1 ·')
    await choose('')
    pending.resolve({ data: older })
    await flushPromises()
    expect(wrapper.find('.grade-table-scroll').exists()).toBe(false)
    expect(wrapper.find('.snapshot-list').exists()).toBe(false)
  })

  it.each(['offering', 'snapshot', 'draft', 'unmount'])('导出期间改变%s上下文，丢弃迟到下载', async change => {
    const pending = deferred()
    mocks.export.mockReturnValueOnce(pending.promise)
    await openBooks()
    await click('版本 1 ·')
    await click('下载 CSV')
    expect(mocks.export).toHaveBeenCalledWith('older-1', 'csv')
    if (change === 'offering') await choose(20)
    if (change === 'snapshot') await click('版本 2 ·')
    if (change === 'draft') await click('返回当前草稿')
    if (change === 'unmount') { wrapper.unmount(); wrapper = null }
    pending.resolve({ data: new Blob(['old grades']) })
    await flushPromises()
    expect(mocks.downloadBlob).not.toHaveBeenCalled()
  })

  it('当前快照导出使用点击时版本命名，换班后的旧错误不显示', async () => {
    const pending = deferred()
    const blob = new Blob(['version one'])
    mocks.export.mockResolvedValueOnce({ data: blob }).mockReturnValueOnce(pending.promise)
    await openBooks()
    await click('版本 1 ·')
    await click('下载 CSV')
    expect(mocks.downloadBlob).toHaveBeenCalledWith(blob, '课程成绩-版本1.csv')
    await click('下载 CSV')
    await choose(20)
    pending.reject({ response: { data: { detail: { message: '旧导出失败' } } } })
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('卸载期间候选响应不继续加载成绩簿', async () => {
    const pending = deferred()
    mocks.offerings.mockReturnValueOnce(pending.promise)
    await openBooks()
    wrapper.unmount(); wrapper = null
    pending.resolve({ data: { items: [{ id: 10 }] } })
    await flushPromises()
    expect(mocks.read).not.toHaveBeenCalled()
  })

  it.each(['success', 'failure'])('换班后旧保存%s不刷新新班或写入错误提示', async outcome => {
    const pending = deferred()
    mocks.configure.mockReturnValueOnce(pending.promise)
    await openBooks()
    const form = wrapper.findAll('form').find(item => item.text().includes('保存栏目与比例'))
    await form.trigger('submit'); await flushPromises()
    await choose(20)
    if (outcome === 'success') pending.resolve({})
    else pending.reject({ response: { data: { detail: { message: '一班保存失败' } } } })
    await flushPromises()
    expect(mocks.read).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).not.toContain('栏目与比例已保存')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('旧班发布完成不清空新班原因，旧finally不解除新班忙状态', async () => {
    const older = deferred(), newer = deferred()
    mocks.publish.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    await openBooks()
    let form = wrapper.findAll('form').find(item => item.text().includes('发布新成绩版本'))
    await form.get('input').setValue('一班原因')
    await form.trigger('submit'); await flushPromises()
    await choose(20)
    form = wrapper.findAll('form').find(item => item.text().includes('发布新成绩版本'))
    await form.get('input').setValue('二班原因')
    expect(form.get('.btn-primary').element.disabled).toBe(false)
    await form.trigger('submit'); await flushPromises()
    older.resolve({})
    await flushPromises()
    expect(form.get('input').element.value).toBe('二班原因')
    expect(form.get('.btn-primary').element.disabled).toBe(true)
    newer.resolve({})
    await flushPromises()
    expect(mocks.publish).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('成绩已发布，学生现在可查看本人版本。')
  })
})
