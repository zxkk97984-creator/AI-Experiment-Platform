// A04 契约回归：直接导入**真实** API 模块（只替换底层 axios 客户端），
// 验证方法齐全、作业/考试两类资源的 URL 分派正确。
// 面板单测会 mock 本模块，因此必须在这里守住真实实现，避免"mock 虚构方法"。
import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
}))

vi.mock('../client.js', () => ({ default: client }))

import { gradingVersionsAPI } from '../gradingVersions.js'

beforeEach(() => {
  Object.values(client).forEach((fn) => fn.mockReset())
})

describe('gradingVersionsAPI 方法齐全性', () => {
  it('面板用到的全部方法都真实存在（不是 mock 虚构）', () => {
    for (const name of ['list', 'createDraft', 'detail', 'confirm', 'activate', 'retire',
                        'preflight', 'convertPreview', 'convertApply', 'createAiDraft',
                        'aiGenerate', 'encodeLiteral', 'decodeLiteral']) {
      expect(typeof gradingVersionsAPI[name], name).toBe('function')
    }
  })
})

describe('gradingVersionsAPI 按 kind 分派端点前缀', () => {
  it('作业题走 /judge，考试题走 /exams（题目级）', () => {
    gradingVersionsAPI.list(42)
    expect(client.get).toHaveBeenLastCalledWith('/judge/questions/42/grading-versions')
    gradingVersionsAPI.list(42, 'exam')
    expect(client.get).toHaveBeenLastCalledWith('/exams/questions/42/grading-versions')

    gradingVersionsAPI.preflight(42)
    expect(client.get).toHaveBeenLastCalledWith('/judge/questions/42/grading-versions/preflight')
    gradingVersionsAPI.preflight(42, 'exam')
    expect(client.get).toHaveBeenLastCalledWith('/exams/questions/42/grading-versions/preflight')

    gradingVersionsAPI.convertPreview(42, {}, 'exam')
    expect(client.post).toHaveBeenLastCalledWith(
      '/exams/questions/42/grading-versions/convert-preview', {})
    gradingVersionsAPI.convertApply(42, { max_score: 30 }, 'exam')
    expect(client.post).toHaveBeenLastCalledWith(
      '/exams/questions/42/grading-versions/convert-apply', { max_score: 30 })
    gradingVersionsAPI.createAiDraft(42, { payload: {} }, 'exam')
    expect(client.post).toHaveBeenLastCalledWith(
      '/exams/questions/42/grading-versions/ai-draft', { payload: {} })
  })

  it('版本级端点同样按 kind 分派', () => {
    gradingVersionsAPI.detail(7)
    expect(client.get).toHaveBeenLastCalledWith('/judge/grading-versions/7')
    gradingVersionsAPI.detail(7, 'exam')
    expect(client.get).toHaveBeenLastCalledWith('/exams/grading-versions/7')

    gradingVersionsAPI.confirm(7, 'a'.repeat(64))
    expect(client.post).toHaveBeenLastCalledWith('/judge/grading-versions/7/confirm',
                                                 { expected_spec_digest: 'a'.repeat(64), confirm_isolated_case_v2: false })
    gradingVersionsAPI.confirm(7, null, 'exam')
    expect(client.post).toHaveBeenLastCalledWith('/exams/grading-versions/7/confirm',
                                                 { confirm_isolated_case_v2: false })

    gradingVersionsAPI.activate(7, { acknowledgeInFlight: true })
    expect(client.post).toHaveBeenLastCalledWith(
      '/judge/grading-versions/7/activate?acknowledge_in_flight=true')
    gradingVersionsAPI.activate(7, { acknowledgeInFlight: true, kind: 'exam' })
    expect(client.post).toHaveBeenLastCalledWith(
      '/exams/grading-versions/7/activate?acknowledge_in_flight=true')
    gradingVersionsAPI.retire(7, 'exam')
    expect(client.post).toHaveBeenLastCalledWith('/exams/grading-versions/7/retire')
  })

  it('考试题不会打到作业资源（A04 越权/串题防线）', () => {
    gradingVersionsAPI.list(99, 'exam')
    const [url] = client.get.mock.calls.at(-1)
    expect(url.startsWith('/exams/')).toBe(true)
    expect(url).not.toContain('/judge/')
  })
})

describe('gradingVersionsAPI 逐例表单与 AI 生成', () => {
  it('字面量编解码与 AI 生成使用真实端点', () => {
    gradingVersionsAPI.encodeLiteral('[1, 2]')
    expect(client.post).toHaveBeenLastCalledWith('/judge/grading-versions/literal',
                                                 { literal: '[1, 2]' })
    gradingVersionsAPI.decodeLiteral({ t: 'int', v: 1 })
    expect(client.post).toHaveBeenLastCalledWith('/judge/grading-versions/literal/decode',
                                                 { value: { t: 'int', v: 1 } })
    gradingVersionsAPI.aiGenerate(42, { entry_function: 'solve' })
    expect(client.post).toHaveBeenLastCalledWith('/judge/questions/42/grading-versions/ai-generate',
                                                 { entry_function: 'solve' })
  })
})
