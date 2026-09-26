import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'


const mocks = vi.hoisted(() => ({
  logout: vi.fn(),
  login: vi.fn(),
  refresh: vi.fn(),
  me: vi.fn(),
}))

vi.mock('../../api/auth.js', () => ({
  authAPI: mocks,
}))

import { useAuthStore } from '../auth.js'
import { waitForAuthTransitions } from '../../api/authRefreshCoordinator.js'


describe('useAuthStore logout', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('刷新会话用服务端强制改密状态覆盖本地旧资料', async () => {
    const auth = useAuthStore()
    auth.setUser({ id: 1, role: 'student', must_change_password: false })
    const refreshed = { id: 1, role: 'student', must_change_password: true, temporary_password_expires_at: '2026-10-01T00:00:00' }
    mocks.refresh.mockResolvedValue({ data: { access_token: 'refreshed-token', user: refreshed } })

    await expect(auth.tryRestoreSession()).resolves.toBe(true)
    expect(auth.user).toEqual(refreshed)
    expect(JSON.parse(localStorage.getItem('user'))).toEqual(refreshed)
    expect(auth.accessToken).toBe('refreshed-token')
  })

  it('用 access token 快照发起服务端退出，并立即清除本地会话', async () => {
    let resolveLogout
    mocks.logout.mockImplementation(
      () => new Promise((resolve) => {
        resolveLogout = resolve
      }),
    )
    const auth = useAuthStore()
    auth.setAccessToken('access-token')
    auth.setUser({ id: 1, username: 'teacher', role: 'teacher' })
    const generationBeforeLogout = auth.sessionGeneration

    const logoutPromise = auth.logout()

    expect(logoutPromise).toBeInstanceOf(Promise)
    expect(mocks.logout).toHaveBeenCalledWith('access-token')
    expect(auth.accessToken).toBe('')
    expect(auth.user).toBeNull()
    expect(auth.sessionGeneration).toBe(generationBeforeLogout + 1)
    expect(localStorage.getItem('user')).toBeNull()

    resolveLogout({})
    await logoutPromise
  })

  it('退出时清除考试暂存答案和编程题运行状态', () => {
    mocks.logout.mockResolvedValue({})
    localStorage.setItem('exam-answer-queue:4:9', '{}')
    localStorage.setItem('exam-code-runs:4:9', '{}')
    localStorage.setItem('unrelated-setting', 'keep')
    const auth = useAuthStore()

    auth.logout()

    expect(localStorage.getItem('exam-answer-queue:4:9')).toBeNull()
    expect(localStorage.getItem('exam-code-runs:4:9')).toBeNull()
    expect(localStorage.getItem('unrelated-setting')).toBe('keep')
  })

  it('页面会话恢复期间发生退出时不接受迟到的刷新结果', async () => {
    let resolveRefresh
    mocks.refresh.mockImplementation(
      () => new Promise((resolve) => {
        resolveRefresh = resolve
      }),
    )
    mocks.logout.mockResolvedValue({})
    const auth = useAuthStore()

    const restoring = auth.tryRestoreSession()
    auth.logout()
    resolveRefresh({
      data: {
        access_token: 'late-access-token',
        user: { id: 1, username: 'teacher', role: 'teacher' },
      },
    })

    await expect(restoring).resolves.toBe(false)
    expect(auth.accessToken).toBe('')
    expect(auth.user).toBeNull()
    expect(mocks.logout).toHaveBeenLastCalledWith('late-access-token')
  })

  it('会话恢复的并发闸门覆盖迟到响应清理的完整过程', async () => {
    let resolveRefresh
    let resolveManualLogout
    let resolveLateCleanup
    mocks.refresh.mockImplementation(
      () => new Promise((resolve) => {
        resolveRefresh = resolve
      }),
    )
    mocks.logout
      .mockImplementationOnce(
        () => new Promise((resolve) => {
          resolveManualLogout = resolve
        }),
      )
      .mockImplementationOnce(
        () => new Promise((resolve) => {
          resolveLateCleanup = resolve
        }),
      )
    const auth = useAuthStore()

    const restoring = auth.tryRestoreSession()
    auth.logout()
    const transitionsSettled = waitForAuthTransitions()
    let didSettle = false
    transitionsSettled.then(() => {
      didSettle = true
    })

    resolveRefresh({
      data: {
        access_token: 'late-access-token',
        user: { id: 1, username: 'teacher', role: 'teacher' },
      },
    })
    await vi.waitFor(() => expect(mocks.logout).toHaveBeenCalledTimes(2))
    await Promise.resolve()
    expect(didSettle).toBe(false)

    resolveLateCleanup({})
    resolveManualLogout({})
    await expect(restoring).resolves.toBe(false)
    await transitionsSettled
  })
})
