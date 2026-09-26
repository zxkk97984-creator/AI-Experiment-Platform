import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import BatchDetailView from '../BatchDetailView.vue'

const mocks = vi.hoisted(() => ({ batch: vi.fn(), confirm: vi.fn() }))
vi.mock('../../../api/accountManagement.js', async (original) => ({ ...await original(), accountAPI: mocks }))
vi.mock('../../../stores/auth.js', () => ({ useAuthStore: () => ({ user: { id: 7 } }) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { id: 'batch-9' } }) }))

const preview = { id: 'batch-9', actor_id: 7, kind: 'password_reset', status: 'preview', total: 45, preview_digest: 'fixed-preview', summary: { selected: 45, eligible: 43, skipped: 2, errors: 0 }, items: [{ row_number: 1, data: { student_no: '001', real_name: '陈同学' }, result: { message: '可重置' }, status: 'pending' }] }
let wrapper
async function mountBatch(data = preview) {
  mocks.batch.mockResolvedValue({ data })
  wrapper = mount(BatchDetailView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' }, RouterLink: { template: '<a><slot /></a>' } } } })
  await flushPromises()
  return wrapper
}

describe('管理批次确认', () => {
  beforeEach(() => { vi.resetAllMocks(); mocks.confirm.mockResolvedValue({}) })
  afterEach(() => wrapper?.unmount())

  it('显示固定目标总数与可执行数，必须核对影响后才能确认', async () => {
    await mountBatch()
    expect(wrapper.text()).toContain('已选择：45')
    expect(wrapper.text()).toContain('可重置：43')
    expect(wrapper.text()).toContain('跳过：2')
    expect(wrapper.text()).toContain('本批次 45 行')
    const confirmButton = wrapper.get('form button')
    expect(confirmButton.element.disabled).toBe(true)
    expect(wrapper.get('textarea').attributes('required')).toBeDefined()
    expect(wrapper.get('input[type="password"]').attributes('required')).toBeDefined()
    await wrapper.get('textarea').setValue('新学期开课前重置')
    await wrapper.get('input[type="password"]').setValue('admin-private-password')
    await wrapper.get('input[type="checkbox"]').setValue(true)
    expect(confirmButton.element.disabled).toBe(false)
    mocks.batch.mockResolvedValue({ data: { ...preview, status: 'succeeded' } })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.confirm).toHaveBeenCalledWith('batch-9', { preview_digest: 'fixed-preview', current_password: 'admin-private-password', reason: '新学期开课前重置' })
    expect(wrapper.find('form').exists()).toBe(false)
  })

  it('存在预检错误即使勾选确认也无法执行', async () => {
    await mountBatch({ ...preview, kind: 'students', summary: { errors: 1 }, items: [{ ...preview.items[0], result: { errors: ['学号对应账号已禁用'] } }] })
    await wrapper.get('input[type="checkbox"]').setValue(true)
    expect(wrapper.get('form button').element.disabled).toBe(true)
    await wrapper.get('form button').trigger('click')
    expect(mocks.confirm).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('学号对应账号已禁用')
  })

  it.each(['queued', 'preparing', 'applying', 'succeeded', 'partially_succeeded', 'failed', 'conflict', 'expired'])('状态 %s 不再提供确认入口', async (status) => {
    await mountBatch({ ...preview, status })
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('确认并后台执行')
  })

  it('其他管理员仅查看批次，不能确认创建者的操作', async () => {
    await mountBatch({ ...preview, actor_id: 8 })
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).toContain('陈同学')
  })
})
