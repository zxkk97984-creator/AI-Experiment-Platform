import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
vi.mock('../../../api/labs.js', () => ({ labsAPI: { regradeBatch: vi.fn(), getRegradeBatch: vi.fn() } }))
vi.mock('../../../api/experiments.js', () => ({ experimentsAPI: { listSubmissions: vi.fn() } }))
import { labsAPI } from '../../../api/labs.js'
import { experimentsAPI } from '../../../api/experiments.js'
import LabRegradeBatch from '../LabRegradeBatch.vue'
describe('历史重评范围', () => {
  beforeEach(() => { vi.resetAllMocks(); setActivePinia(createPinia()) })
  it('selects all attempts for a student and filters collisions between lesson/module ids', async () => {
    experimentsAPI.listSubmissions.mockResolvedValue({ data: { total: 3, items: [
      { id: 11, record_id: 1, entry_type: 'module', entry_id: 3, student_name: '学生甲' },
      { id: 12, record_id: 1, entry_type: 'module', entry_id: 3, student_name: '学生甲' },
      { id: 13, record_id: 2, entry_type: 'lesson', entry_id: 3, student_name: '其他实验学生' },
    ] } })
    labsAPI.regradeBatch.mockResolvedValue({ data: { id: 9, status: 'running' } })
    labsAPI.getRegradeBatch.mockResolvedValue({ data: { id: 9, status: 'needs_review', total: 2, completed: 1 } })
    const wrapper = mount(LabRegradeBatch, { props: { kind: 'module', entryId: 3, version: 2 } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(1)
    expect(wrapper.text()).toContain('全部 2 次提交')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.find('textarea').setValue('纠正测试集标签')
    await wrapper.findAll('button').at(-1).trigger('click')
    await flushPromises()
    expect(labsAPI.regradeBatch).toHaveBeenCalledWith('module', 3, { submission_ids: [11, 12], reason: '纠正测试集标签' })
    expect(labsAPI.getRegradeBatch).toHaveBeenCalledWith(9)
    expect(wrapper.text()).toContain('需要教师处理，原成绩保留')
    wrapper.unmount()
  })
})
