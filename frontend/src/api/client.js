import axios from 'axios'
import { useAuthStore } from '../stores/auth.js'
let routerPromise = null

async function getRouter() {
  if (!routerPromise) routerPromise = import('../router/index.js').then((m) => m.default)
  return routerPromise
}
import { beginAuthRefresh } from './authRefreshCoordinator.js'

const client = axios.create({
  baseURL: '/api/v1',
  headers: { 'Content-Type': 'application/json' },
  timeout: 30000,
  withCredentials: true,  // 发送 HttpOnly refresh cookie
})

// Request interceptor: attach access token from memory
client.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.accessToken) {
    config.headers.Authorization = `Bearer ${auth.accessToken}`
  }
  return config
})

// Response interceptor: handle 401 → cookie refresh → retry
let isRefreshing = false
let refreshQueue = []

function resolveQueue(token) {
  refreshQueue.forEach(({ resolve }) => resolve(token))
  refreshQueue = []
}

function rejectQueue(error) {
  refreshQueue.forEach(({ reject }) => reject(error))
  refreshQueue = []
}

client.interceptors.response.use(
  (res) => res,
  async (error) => {
    const { config, response } = error
    const code = response?.data?.detail?.code
    if (code === 'PASSWORD_CHANGE_REQUIRED') {
      const auth = useAuthStore()
      auth.setUser({ ...auth.user, must_change_password: true })
      const router = await getRouter()
      router.replace('/account/password')
      return Promise.reject(error)
    }
    if (code === 'TEMPORARY_PASSWORD_EXPIRED') {
      const auth = useAuthStore()
      await auth.logout()
      const router = await getRouter()
      router.replace('/login')
      return Promise.reject(error)
    }
    if (
      !response ||
      response.status !== 401 ||
      config?._retry ||
      config?.skipAuthRefresh
    ) {
      return Promise.reject(error)
    }

    const auth = useAuthStore()
    if (!auth.isAuthenticated) {
      const router = await getRouter()
      router.push('/login')
      return Promise.reject(error)
    }

    if (isRefreshing) {
      return new Promise((resolve, reject) => {
        refreshQueue.push({ resolve, reject, config })
      }).then((token) => {
        config.headers.Authorization = `Bearer ${token}`
        config._retry = true
        return client(config)
      })
    }

    isRefreshing = true
    config._retry = true
    const refreshGeneration = auth.sessionGeneration
    const finishAuthRefresh = beginAuthRefresh()

    try {
      // Refresh via cookie（不需要手动传 refresh_token）
      const res = await axios.post('/api/v1/auth/refresh', {},
        { timeout: 10000, withCredentials: true },
      )

      const newToken = res.data.access_token
      if (
        auth.sessionGeneration !== refreshGeneration ||
        !auth.isAuthenticated
      ) {
        const newSessionIsActive = (
          auth.sessionGeneration !== refreshGeneration &&
          auth.isAuthenticated
        )
        if (!newSessionIsActive) {
          // 手动退出与 refresh 并发时，响应 Cookie 可能已被轮换。
          // 没有新会话时，用迟到 token 幂等清理旋转后的旧 Cookie。
          await axios.post('/api/v1/auth/logout', {}, {
            timeout: 10000,
            withCredentials: true,
            headers: { Authorization: `Bearer ${newToken}` },
          }).catch(() => {})
        }
        const sessionChangedError = new Error('会话已改变，已丢弃迟到的刷新结果')
        rejectQueue(sessionChangedError)
        return Promise.reject(sessionChangedError)
      }
      // 更新 Pinia 内存中的 token
      auth.setAccessToken(newToken)
      if (res.data.user) auth.setUser(res.data.user)

      resolveQueue(newToken)
      config.headers.Authorization = `Bearer ${newToken}`
      return client(config)
    } catch (refreshError) {
      if (refreshError.response?.status === 401 || refreshError.response?.data?.detail?.code === 'TEMPORARY_PASSWORD_EXPIRED') {
        rejectQueue(refreshError)
        if (
          auth.sessionGeneration === refreshGeneration &&
          auth.isAuthenticated
        ) {
          auth.logout()
        }
        const router = await getRouter()
        router.push('/login')
      } else {
        rejectQueue(refreshError)
        isRefreshing = false
      }
      return Promise.reject(error)
    } finally {
      isRefreshing = false
      finishAuthRefresh()
    }
  }
)

export default client
