import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import PagedCandidateDialog from '../PagedCandidateDialog.vue'

const wrappers = []
const dataSet = (count, prefix = '候选') => Array.from({ length: count }, (_, i) => ({ id: i + 1, name: `${prefix}${i + 1}` }))
const paginate = rows => vi.fn(async ({ page, page_size, q }) => {
  const filtered = rows.filter(row => !q || row.name.includes(q))
  return { data: { items: filtered.slice((page - 1) * page_size, page * page_size), total: filtered.length } }
})
async function open(props = {}) {
  const wrapper = mount(PagedCandidateDialog, { attachTo: document.body, props: { title: '选择对象', label: row => row.name, fetchPage: paginate(dataSet(101)), ...props }, global: { stubs: { Teleport: true } } })
  wrappers.push(wrapper); await flushPromises(); return wrapper
}
const button = (wrapper, text) => wrapper.findAll('button').find(node => node.text() === text)
async function jump(wrapper, page) {
  const input = wrapper.get('[aria-label="跳转页码"]')
  await input.setValue(String(page)); await input.trigger('keydown.enter'); await flushPromises()
}
async function choose(wrapper, name) {
  const row = wrapper.findAll('.candidate-row').find(node => node.text() === name)
  await row.get('input').setValue(true)
}
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers() })

describe('分页候选确认窗口', () => {
  it.each(['课程', '教学班', '教师'])('%s第101条可选择，取消不覆盖原选项', async prefix => {
    const rows = dataSet(101, prefix), fetchPage = paginate(rows)
    const wrapper = await open({ fetchPage, selected: [rows[0]] })
    await jump(wrapper, 6)
    expect(fetchPage).toHaveBeenLastCalledWith({ page: 6, page_size: 20, q: undefined })
    await choose(wrapper, `${prefix}101`)
    await button(wrapper, '取消').trigger('click')
    expect(wrapper.emitted('confirm')).toBeUndefined()
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(rows[0].id).toBe(1)
    await button(wrapper, '确认选择').trigger('click')
    expect(wrapper.emitted('confirm')[0][0].map(row => row.id)).toEqual([101])
  })

  it('205名学生跨页多选并保留摘要，当前班成员禁选', async () => {
    const fetchPage = paginate(dataSet(205, '学生'))
    const wrapper = await open({ multiple: true, fetchPage, disabledReason: row => row.id === 1 ? '已在本班' : '' })
    expect(wrapper.findAll('.candidate-row')[0].get('input').element.disabled).toBe(true)
    await choose(wrapper, '学生2'); await jump(wrapper, 11); await choose(wrapper, '学生205')
    expect(wrapper.get('[aria-label="已选项目"]').text()).toContain('学生2')
    expect(wrapper.get('[aria-label="已选项目"]').text()).toContain('学生205')
    await jump(wrapper, 1)
    expect(wrapper.findAll('.candidate-row')[1].get('input').element.checked).toBe(true)
    await button(wrapper, '确认选择').trigger('click')
    expect(wrapper.emitted('confirm')[0][0].map(row => row.id)).toEqual([2, 205])
    expect(fetchPage.mock.calls.every(([params]) => params.page_size === 20)).toBe(true)
  })

  it('下一页失败不推进页码、不丢已选，可重试同一页', async () => {
    const fetchPage = paginate(dataSet(101)), wrapper = await open({ fetchPage })
    await choose(wrapper, '候选2')
    fetchPage.mockRejectedValueOnce(new Error('offline'))
    await jump(wrapper, 2)
    expect(wrapper.get('[role="alert"]').text()).toContain('重试')
    expect(wrapper.get('[aria-current="page"]').text()).toBe('1')
    expect(wrapper.get('[aria-label="已选项目"]').text()).toContain('候选2')
    await button(wrapper, '重试').trigger('click'); await flushPromises()
    expect(fetchPage).toHaveBeenLastCalledWith({ page: 2, page_size: 20, q: undefined })
    expect(wrapper.get('[aria-current="page"]').text()).toBe('2')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('搜索250ms去抖重置到第一页，迟到旧响应不会覆盖新结果', async () => {
    vi.useFakeTimers()
    const fetchPage = paginate(dataSet(101)), wrapper = await open({ fetchPage })
    await jump(wrapper, 6)
    let resolveOld
    fetchPage.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const search = wrapper.get('input[type="search"]')
    await search.setValue('候选1'); await vi.advanceTimersByTimeAsync(250)
    await search.setValue('候选101'); await vi.advanceTimersByTimeAsync(249)
    expect(fetchPage).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(fetchPage).toHaveBeenLastCalledWith({ page: 1, page_size: 20, q: '候选101' })
    resolveOld({ data: { items: [{ id: 1, name: '迟到旧结果' }], total: 1 } }); await flushPromises()
    expect(wrapper.get('.candidate-results').text()).toBe('候选101')
  })

  it('输入搜索后立即禁用旧结果，Esc取消不提交', async () => {
    vi.useFakeTimers()
    const wrapper = await open()
    await wrapper.get('input[type="search"]').setValue('另一人')
    expect(wrapper.findAll('.candidate-row').every(row => row.get('input').element.disabled)).toBe(true)
    await wrapper.get('[role="dialog"]').trigger('keydown.esc')
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('移除当前聚焦的已选标签后回到搜索框，仍可按Esc取消', async () => {
    const wrapper = await open({ multiple: true, selected: [dataSet(1)[0]] })
    const remove = wrapper.get('[aria-label="移除候选1"]')
    remove.element.focus()
    await remove.trigger('click'); await flushPromises()
    expect(document.activeElement).toBe(wrapper.get('input[type="search"]').element)
    await wrapper.get('input[type="search"]').trigger('keydown.esc')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
