import client from './client.js'

// 作业提交的幂等请求键：只在**作业提交**这一条路径上生成/复用 UUID，
// 不放到 axios 全局（其他接口有各自的幂等/重试策略，例如实验提交用 record 级幂等）。
//
// 语义（与后端约定一致）：
// - 键绑定"学生 + 题目 + 原样代码"：结果未知（网络中断/超时）时用同一键重放原请求；
// - 提交成功或服务器确定性拒绝后释放键：下一次"新尝试"会换新键；
// - 代码被编辑 → 内容不同 → 自动换新键。
const requestKeys = new Map()

function digestOf(data) {
  return `${data?.question_id ?? ''}\u0000${data?.code ?? ''}`
}

function newRequestId() {
  const cryptoObj = globalThis.crypto
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID()
  // 非安全上下文回退：仍返回 36 字符 UUID v4 形态
  const rand = () => Math.floor((1 + Math.random()) * 0x10000).toString(16).slice(1)
  return `${rand()}${rand()}-${rand()}-4${rand().slice(1)}-a${rand().slice(1)}-${rand()}${rand()}${rand()}`
}

function keyFor(data, { newAttempt = false } = {}) {
  const digest = digestOf(data)
  if (newAttempt) requestKeys.delete(digest)
  let requestId = requestKeys.get(digest)
  if (!requestId) {
    requestId = newRequestId()
    requestKeys.set(digest, requestId)
  }
  return requestId
}

function settle(digest, outcome) {
  // unknown：结果未知（无响应/网络错误/5xx）→ 保留键，重试即重放原请求
  if (outcome === 'unknown') return
  requestKeys.delete(digest)
}

export function resetSubmissionRequestKeys() {
  requestKeys.clear()
}

export const judgeAPI = {
  /**
   * 人工评分（教师/管理员）：分数 + 理由 + 必需的期望结果修订号（乐观并发）。
   * 判题或 AI 仍在途时后端返回 409，界面应提示稍后重试。
   */
  manualScore(submissionId, { score, reason, expectedResultRevision }) {
    return client.patch(`/judge/submissions/${submissionId}/manual-score`, {
      score, reason, expected_result_revision: expectedResultRevision,
    })
  },
  async submit(data, options = {}) {
    const digest = digestOf(data)
    const client_request_id = keyFor(data, options)
    try {
      const response = await client.post('/judge/submissions', { ...data, client_request_id })
      settle(digest, 'done')
      return response
    } catch (error) {
      const status = error?.response?.status
      if (status === undefined) {
        settle(digest, 'unknown')          // 网络/超时：保留键，重试即重放
      } else if (status === 409
        && error?.response?.data?.detail?.code === 'IDEMPOTENCY_KEY_REUSED') {
        settle(digest, 'done')             // 键被复用为不同内容：释放，下次换新键
      } else if (status >= 400 && status < 500) {
        settle(digest, 'done')             // 确定性拒绝（截止/次数/权限）
      } else {
        settle(digest, 'unknown')          // 5xx：结果未知，保留键
      }
      throw error
    }
  },
  list(params) { return client.get('/judge/submissions', { params }) },
  get(id) { return client.get(`/judge/submissions/${id}`) },
  getResult(id) { return client.get(`/judge/submissions/${id}/result`) },
  sampleRun(questionId, data) { return client.post(`/judge/questions/${questionId}/sample-run`, data) },
}
