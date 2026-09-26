import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const showToast = vi.hoisted(() => vi.fn())

vi.mock('../../../api/academics.js', () => ({
  academicsAPI: {
    listTerms: vi.fn(), listClasses: vi.fn(), createTerm: vi.fn(), closeTerm: vi.fn(),
    createClass: vi.fn(), archiveClass: vi.fn(), listClassStudents: vi.fn(),
    addClassStudents: vi.fn(), removeClassStudent: vi.fn(),
  },
}))
vi.mock('../../../api/users.js', () => ({ usersAPI: { listStudents: vi.fn() } }))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast }) }))

import { academicsAPI } from '../../../api/academics.js'
import { usersAPI } from '../../../api/users.js'
import AcademicManageView from '../AcademicManageView.vue'

const term = { id: 1, code: '2026-FALL', name: '2026 秋季学期', start_date: '2026-09-01', end_date: '2027-01-20', status: 'active' }
const teachingClass = { id: 2, academic_term_id: 1, code: 'CS-01', name: '计算机一班', status: 'active', student_count: 1 }

async function mountPage() {
  const wrapper = mount(AcademicManageView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' } } } })
  await flushPromises()
  return wrapper
}

describe('管理员教务管理页', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    academicsAPI.listTerms.mockResolvedValue({ data: { items: [term], total: 1 } })
    academicsAPI.listClasses.mockResolvedValue({ data: { items: [teachingClass], total: 101 } })
    academicsAPI.listClassStudents.mockResolvedValue({ data: { items: [{ id: 3, username: 'student_a', student_no: '20260001', real_name: '陈同学' }], total: 1 } })
    usersAPI.listStudents.mockResolvedValue({ data: { items: [] } })
  })

  it('展示学期、班级与独立学号名单', async () => {
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('2026 秋季学期')
    await wrapper.findAll('.rows')[1].find('div').trigger('click')
    await flushPromises()
    expect(academicsAPI.listClasses).toHaveBeenCalledWith({ page: 1, page_size: 20 })
    expect(academicsAPI.listClassStudents).toHaveBeenCalledWith(2, { page: 1, page_size: 20 })
    expect(wrapper.text()).toContain('20260001')
    expect(wrapper.text()).toContain('陈同学')
    expect(wrapper.find('.class-panel').exists()).toBe(false)
    expect(wrapper.find('.back-to-classes').text()).toContain('返回教学班列表')

    await wrapper.find('.back-to-classes').trigger('click')
    expect(wrapper.find('.roster').exists()).toBe(false)
    expect(wrapper.find('.class-panel').exists()).toBe(true)
    expect(wrapper.text()).toContain('计算机一班')
  })

  it('使用后端 total 分页加载超过一页的教学班', async () => {
    const wrapper = await mountPage()
    const next = wrapper.find('[aria-label="教学班列表分页"] button[aria-label="下一页"]')

    expect(next.exists()).toBe(true)
    await next.trigger('click')
    await flushPromises()

    expect(academicsAPI.listClasses).toHaveBeenLastCalledWith({ page: 2, page_size: 20 })
  })

  it('创建学期后刷新教务数据', async () => {
    academicsAPI.createTerm.mockResolvedValue({ data: term })
    const wrapper = await mountPage()
    academicsAPI.listTerms.mockClear()
    academicsAPI.listTerms.mockResolvedValue({ data: { items: [term, { ...term, id: 4, code: '2027-SPRING', name: '2027 春季学期' }], total: 2 } })
    const inputs = wrapper.findAll('.grid article').at(0).findAll('input')
    await inputs[0].setValue('2027-SPRING')
    await inputs[1].setValue('2027 春季学期')
    await inputs[2].setValue('2027-02-20')
    await inputs[3].setValue('2027-07-10')
    await wrapper.findAll('.grid article').at(0).find('form').trigger('submit')
    await flushPromises()
    expect(academicsAPI.createTerm).toHaveBeenCalledWith(expect.objectContaining({ code: '2027-SPRING' }))
    expect(academicsAPI.listTerms).toHaveBeenCalledWith({ page: 1, page_size: 20 })
    expect(academicsAPI.listTerms).toHaveBeenCalledWith({ page: 1, page_size: 100 })
    expect(wrapper.findAll('.rows')[0].text()).toContain('2027 春季学期')
    expect(wrapper.findAll('option').some((option) => option.text().includes('2027 春季学期'))).toBe(true)
  })
})
