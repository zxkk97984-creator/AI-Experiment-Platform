import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ProfileView from '../ProfileView.vue'

const mocks = vi.hoisted(() => ({ profile: vi.fn(), updateProfile: vi.fn(), setUser: vi.fn() }))
vi.mock('../../api/accountManagement.js', async (original) => ({ ...await original(), accountAPI: mocks }))
vi.mock('../../api/users.js', () => ({ usersAPI: { getMyPreferences: vi.fn().mockResolvedValue({ data: { preferences: {} } }), updateMyPreferences: vi.fn().mockResolvedValue({ data: {} }) } }))
vi.mock('../../stores/auth.js', () => ({ useAuthStore: () => ({ setUser: mocks.setUser }) }))

const profile = { id: 9, real_name: '陈同学', username: '20260001', student_no: '20260001', department: '计算机学院', role: 'student', status: 'active', nickname: '小陈', classes: [{ id: 2, name: '计算机一班' }] }

describe('个人中心资料权限', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.profile.mockResolvedValue({ data: profile })
    mocks.updateProfile.mockResolvedValue({ data: { ...profile, nickname: '新昵称' } })
  })
  it('教务信息只读，提交仅包含昵称并同步当前用户', async () => {
    const wrapper = mount(ProfileView, { global: { stubs: { AppLayout: { template: '<div><slot /></div>' }, PasswordForm: true } } })
    await flushPromises()
    for (const identity of ['陈同学', '20260001', '计算机学院', '计算机一班']) expect(wrapper.text()).toContain(identity)
    const identitySection = wrapper.findAll('section').find(section => section.get('h2').text() === '账号资料')
    expect(identitySection.findAll('input, select, textarea')).toHaveLength(1)
    expect(wrapper.get('input').element.value).toBe('小陈')
    await wrapper.get('input').setValue('新昵称')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.updateProfile).toHaveBeenCalledWith({ nickname: '新昵称' })
    expect(mocks.setUser).toHaveBeenCalledWith({ ...profile, nickname: '新昵称' })
    expect(wrapper.get('[role="status"]').text()).toBe('昵称已保存')
  })
})
