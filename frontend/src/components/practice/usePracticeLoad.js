import { ref } from 'vue'
import { useAppStore } from '../../stores/app.js'
import { isPermissionError, isQuotaError, practiceErrorMessage } from './practiceHelpers.js'

/**
 * 练习页统一异步装载：加载 / 失败 / 权限不足 / 额度耗尽 四态 + toast。
 * 空态与排队态由调用方数据驱动（列表为空、存在 queued 提交），不在这里猜测。
 */
export function usePracticeLoad(loader, { fallbackMessage = '数据加载失败，请稍后重试' } = {}) {
  const app = useAppStore()
  const loading = ref(true)
  const error = ref('')
  const forbidden = ref(false)
  const quotaExhausted = ref(false)

  async function load({ silent = false } = {}) {
    if (!silent) loading.value = true
    error.value = ''
    forbidden.value = false
    try {
      const res = await loader()
      const payload = res?.data ?? res ?? null
      if (payload?.meta?.quota_exhausted === true) quotaExhausted.value = true
      return payload
    } catch (e) {
      if (isPermissionError(e)) {
        forbidden.value = true
        error.value = '当前账号没有自主练习的访问权限'
      } else if (isQuotaError(e)) {
        quotaExhausted.value = true
        error.value = '今日额度已用完'
      } else {
        error.value = practiceErrorMessage(e, fallbackMessage)
      }
      app.showToast(error.value, 'error')
      return null
    } finally {
      if (!silent) loading.value = false
    }
  }

  return { loading, error, forbidden, quotaExhausted, load }
}

/** 权限不足时列表页应展示权限提示而不是「加载失败」重试块 */
export function listErrorVisible(error, forbidden) {
  return Boolean(error) && !forbidden
}
