// 作业提交幂等键：结果未知时重放原键原正文；新尝试才换键。
import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
}))

vi.mock('../client.js', () => ({ default: client }))

import { judgeAPI, resetSubmissionRequestKeys } from '../judge.js'

const PAYLOAD = { question_id: 7, code: 'def add(a, b):\n    return a + b' }

function networkError() {
  return Object.assign(new Error('network'), { response: undefined })
}

function httpError(status, code) {
  return Object.assign(new Error('http'), { response: { status, data: { detail: { code } } } })
}

describe('judgeAPI 提交幂等键', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    resetSubmissionRequestKeys()
  })

  it('首次提交生成 UUID 请求键', async () => {
    client.post.mockResolvedValue({ data: { id: 1 } })
    await judgeAPI.submit(PAYLOAD)
    const [path, body] = client.post.mock.calls[0]
    expect(path).toBe('/judge/submissions')
    expect(body.client_request_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(body.question_id).toBe(7)
    expect(body.code).toBe(PAYLOAD.code)
  })

  it('结果未知（网络错误）后重试沿用同一键与同一正文', async () => {
    client.post.mockRejectedValueOnce(networkError())
    await expect(judgeAPI.submit(PAYLOAD)).rejects.toThrow()
    client.post.mockResolvedValue({ data: { id: 2 } })
    await judgeAPI.submit(PAYLOAD)

    const firstKey = client.post.mock.calls[0][1].client_request_id
    const secondKey = client.post.mock.calls[1][1].client_request_id
    expect(secondKey).toBe(firstKey)
    expect(client.post.mock.calls[1][1].code).toBe(PAYLOAD.code)
  })

  it('提交成功后再次提交同代码是新的尝试（换键）', async () => {
    client.post.mockResolvedValue({ data: { id: 3 } })
    await judgeAPI.submit(PAYLOAD)
    await judgeAPI.submit(PAYLOAD)
    expect(client.post.mock.calls[1][1].client_request_id)
      .not.toBe(client.post.mock.calls[0][1].client_request_id)
  })

  it('编辑代码后自动换键', async () => {
    client.post.mockRejectedValueOnce(networkError())
    await expect(judgeAPI.submit(PAYLOAD)).rejects.toThrow()
    client.post.mockResolvedValue({ data: { id: 4 } })
    await judgeAPI.submit({ ...PAYLOAD, code: 'def add(a, b):\n    return a + b + 0' })
    expect(client.post.mock.calls[1][1].client_request_id)
      .not.toBe(client.post.mock.calls[0][1].client_request_id)
  })

  it('显式新尝试（newAttempt）丢弃旧键', async () => {
    client.post.mockRejectedValueOnce(networkError())
    await expect(judgeAPI.submit(PAYLOAD)).rejects.toThrow()
    client.post.mockResolvedValue({ data: { id: 5 } })
    await judgeAPI.submit(PAYLOAD, { newAttempt: true })
    expect(client.post.mock.calls[1][1].client_request_id)
      .not.toBe(client.post.mock.calls[0][1].client_request_id)
  })

  it('确定性 4xx 拒绝后释放键（下一次换新键）', async () => {
    client.post.mockRejectedValueOnce(httpError(400, 'MAX_ATTEMPTS_REACHED'))
    await expect(judgeAPI.submit(PAYLOAD)).rejects.toThrow()
    client.post.mockResolvedValue({ data: { id: 6 } })
    await judgeAPI.submit(PAYLOAD)
    expect(client.post.mock.calls[1][1].client_request_id)
      .not.toBe(client.post.mock.calls[0][1].client_request_id)
  })

  it('5xx 结果未知：保留键以便重放', async () => {
    client.post.mockRejectedValueOnce(httpError(503, 'SERVICE_UNAVAILABLE'))
    await expect(judgeAPI.submit(PAYLOAD)).rejects.toThrow()
    client.post.mockResolvedValue({ data: { id: 7 } })
    await judgeAPI.submit(PAYLOAD)
    expect(client.post.mock.calls[1][1].client_request_id)
      .toBe(client.post.mock.calls[0][1].client_request_id)
  })
})
