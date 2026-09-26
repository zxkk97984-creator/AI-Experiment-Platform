import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PasswordForm from '../PasswordForm.vue'
import { useAuthStore } from '../../../stores/auth.js'

const mocks = vi.hoisted(() => ({ changePassword: vi.fn(), logout: vi.fn(), replace: vi.fn(), showToast: vi.fn() }))
vi.mock('../../../api/accountManagement.js', async (original) => ({ ...await original(), accountAPI: { changePassword: mocks.changePassword } }))
vi.mock('../../../api/auth.js', () => ({ authAPI: { logout: mocks.logout } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ replace: mocks.replace }) }))
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast: mocks.showToast }) }))

async function fill(wrapper, password = 'private-pass-2026', confirmation = password) {
  const inputs = wrapper.findAll('input')
  await inputs[0].setValue('old-private-pass')
  await inputs[1].setValue(password)
  await inputs[2].setValue(confirmation)
}

describe('本人修改密码', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    setActivePinia(createPinia())
    mocks.logout.mockResolvedValue({})
    mocks.changePassword.mockResolvedValue({})
  })

  it('两次密码不一致时不发送请求', async () => {
    const wrapper = mount(PasswordForm)
    await fill(wrapper, 'private-pass-2026', 'different-pass-2026')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.get('[role="alert"]').text()).toContain('两次新密码不一致')
    expect(mocks.changePassword).not.toHaveBeenCalled()
  })

  it.each(['short', '密'.repeat(25)])('拒绝长度或 UTF-8 字节数越界的密码', async (password) => {
    const wrapper = mount(PasswordForm)
    await fill(wrapper, password)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.get('[role="alert"]').text()).toContain('最多 72 个 UTF-8 字节')
    expect(mocks.changePassword).not.toHaveBeenCalled()
  })

  it('成功后清空凭据、真实登录状态和持久用户信息，再跳转登录', async () => {
    const auth = useAuthStore()
    auth.setAccessToken('old-token')
    auth.setUser({ id: 1, role: 'student', must_change_password: true })
    const wrapper = mount(PasswordForm)
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.changePassword).toHaveBeenCalledWith({ current_password: 'old-private-pass', password: 'private-pass-2026' })
    expect(auth.accessToken).toBe('')
    expect(auth.user).toBeNull()
    expect(localStorage.getItem('user')).toBeNull()
    expect(wrapper.findAll('input').every((input) => input.element.value === '')).toBe(true)
    expect(mocks.replace).toHaveBeenCalledWith('/login')
    expect(mocks.showToast).toHaveBeenCalledWith('密码已修改，请重新登录', 'success')
    wrapper.unmount()
  })

  it('服务端拒绝后显示错误且保留会话、不跳转', async () => {
    mocks.changePassword.mockRejectedValue({ response: { data: { detail: { message: '当前密码错误' } } } })
    const auth = useAuthStore()
    auth.setAccessToken('valid-token')
    const wrapper = mount(PasswordForm)
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('当前密码错误')
    expect(auth.accessToken).toBe('valid-token')
    expect(mocks.logout).not.toHaveBeenCalled()
    expect(mocks.replace).not.toHaveBeenCalled()
    expect(wrapper.get('button').element.disabled).toBe(false)
  })
})
