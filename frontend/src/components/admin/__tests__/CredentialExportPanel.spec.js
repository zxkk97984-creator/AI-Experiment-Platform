import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CredentialExportPanel from '../CredentialExportPanel.vue'

const mocks = vi.hoisted(() => ({ credentialDeliveries: vi.fn(), exportCredential: vi.fn(), exportBatchCredentials: vi.fn(), downloadBlob: vi.fn() }))
vi.mock('../../../api/accountManagement.js', async (original) => ({ ...await original(), accountAPI: mocks, downloadBlob: mocks.downloadBlob }))
const delivery = { id: 'delivery-1', available: true, export_expires_at: '2030-01-02T00:00:00Z' }

describe('临时凭据交付', () => {
  beforeEach(() => { vi.resetAllMocks(); mocks.credentialDeliveries.mockResolvedValue({ data: { items: [delivery] } }); mocks.exportCredential.mockResolvedValue({ data: new Blob(['xlsx']) }); mocks.exportBatchCredentials.mockResolvedValue({ data: new Blob(['xlsx']) }) })

  it('只显示交付元数据，管理员再次认证后下载单人凭据并清除输入', async () => {
    const wrapper = mount(CredentialExportPanel, { props: { userId: 9 } })
    await flushPromises()
    expect(wrapper.text()).toContain('24 小时')
    expect(wrapper.find('input').attributes('required')).toBeDefined()
    await wrapper.get('input').setValue('private-admin-password')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.exportCredential).toHaveBeenCalledWith('delivery-1', { current_password: 'private-admin-password' })
    expect(mocks.downloadBlob).toHaveBeenCalledOnce()
    expect(wrapper.get('input').element.value).toBe('')
    expect(wrapper.text()).toContain('只向对应学生发放')
    wrapper.unmount()
  })

  it('批次入口独立验证管理员密码，失败后不保留认证输入', async () => {
    mocks.exportBatchCredentials.mockRejectedValue({ response: { data: { detail: { message: '当前密码不正确' } } } })
    const wrapper = mount(CredentialExportPanel, { props: { batchId: 'batch-1', deliveries: [delivery] } })
    await wrapper.get('input').setValue('wrong-password')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(mocks.exportBatchCredentials).toHaveBeenCalledWith('batch-1', { current_password: 'wrong-password' })
    expect(wrapper.get('[role="alert"]').text()).toContain('当前密码不正确')
    expect(wrapper.get('input').element.value).toBe('')
    expect(mocks.downloadBlob).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('过期或已改密交付没有下载入口', () => {
    const wrapper = mount(CredentialExportPanel, { props: { batchId: 'batch-1', deliveries: [{ ...delivery, available: false }] } })
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无可下载凭据')
    wrapper.unmount()
  })
})
