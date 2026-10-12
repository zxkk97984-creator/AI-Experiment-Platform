// 批次 2 契约：一键 AI 配置（任务/重试/试判/启用）端点路径与超时
import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('../client.js', () => ({ default: client }))

import { aiGradingAPI } from '../aiGrading.js'

describe('aiGradingAPI 一键配置', () => {
  beforeEach(() => vi.resetAllMocks())

  it('createSetupJob 提交题目类型与 id', () => {
    aiGradingAPI.createSetupJob({ question_kind: 'exam', question_id: 12, grading_mode: 'active' })
    const [path, body] = client.post.mock.calls[0]
    expect(path).toBe('/ai-grading/setup-jobs')
    expect(body).toEqual({ question_kind: 'exam', question_id: 12, grading_mode: 'active' })
  })

  it('getSetupJob / retrySetupJob 走任务子路径', () => {
    aiGradingAPI.getSetupJob(5)
    expect(client.get.mock.calls[0][0]).toBe('/ai-grading/setup-jobs/5')
    aiGradingAPI.retrySetupJob(5)
    expect(client.post.mock.calls[0][0]).toBe('/ai-grading/setup-jobs/5/retry')
  })

  it('试判：创建与查询分别走 setup-jobs/{id}/trial-runs 与 trial-runs/{id}', () => {
    aiGradingAPI.createTrialRun(5, { code: 'def solve():\n    return 1', client_request_id: 'r1' })
    expect(client.post.mock.calls[0][0]).toBe('/ai-grading/setup-jobs/5/trial-runs')
    aiGradingAPI.getTrialRun(9)
    expect(client.get.mock.calls[0][0]).toBe('/ai-grading/trial-runs/9')
  })

  it('enableSetupJob 预留 120 秒超时（单事务保存并启用）', () => {
    aiGradingAPI.enableSetupJob(5, {})
    const [path, body, config] = client.post.mock.calls[0]
    expect(path).toBe('/ai-grading/setup-jobs/5/enable')
    expect(body).toEqual({})
    expect(config.timeout).toBe(120000)
  })
})
