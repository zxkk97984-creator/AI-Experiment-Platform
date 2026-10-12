// 练习域展示逻辑：六态判定、额度与权限错误映射、AI 未完成判定
import { describe, expect, it } from 'vitest'
import {
  aiIncomplete, aiStatusText, difficultyMeta, formatScore, gradingModeMeta, gradingStatusMeta,
  isActiveStatus, isPermissionError, isQuotaError, isTerminalStatus, listOf, newClientRequestId,
  practiceErrorMessage, problemStatusMeta, queuePositionText, scoreText, testSummary,
} from '../practiceHelpers.js'

const quotaError = { response: { status: 429, data: { detail: { code: 'QUOTA_EXHAUSTED', message: '今日额度已用完' } } } }

describe('practiceHelpers 状态与文案', () => {
  it('难度 / 题目状态 / 判题方式 / 判题状态都有中英文案与色调', () => {
    expect(difficultyMeta('hard')).toEqual({ label: '困难', tone: 'danger' })
    expect(difficultyMeta('unknown')).toEqual({ label: '未标注', tone: 'neutral' })
    expect(problemStatusMeta('archived').label).toBe('已归档')
    expect(gradingModeMeta('ai').label).toBe('AI 综合判题')
    expect(gradingStatusMeta('review_required').label).toBe('待人工复核')
    expect(gradingStatusMeta('quota_exhausted').label).toBe('今日额度已用完')
  })

  it('排队名次只在排队中展示，且不猜名次', () => {
    expect(queuePositionText({ grading_status: 'queued', queue_position: 3 })).toBe('排队中（第 3 位）')
    expect(queuePositionText({ grading_status: 'queued', queue_position: null })).toBe('排队中，等待判题')
    expect(queuePositionText({ grading_status: 'running', queue_position: 3 })).toBeNull()
    expect(queuePositionText(null)).toBeNull()
  })

  it('进行中 / 终态判定覆盖后端枚举', () => {
    expect(isActiveStatus('queued')).toBe(true)
    expect(isActiveStatus('running')).toBe(true)
    expect(isActiveStatus('completed')).toBe(false)
    expect(isTerminalStatus('completed')).toBe(true)
    expect(isTerminalStatus('quota_exhausted')).toBe(true)
    expect(isTerminalStatus('running')).toBe(false)
  })

  it('列表接口兼容数组与 { items } 两种形状', () => {
    expect(listOf([1, 2])).toEqual([1, 2])
    expect(listOf({ items: [3], total: 1 })).toEqual([3])
    expect(listOf(undefined)).toEqual([])
  })
})

describe('practiceHelpers 错误映射', () => {
  it('额度耗尽优先识别 429 与 QUOTA_EXHAUSTED', () => {
    expect(isQuotaError(quotaError)).toBe(true)
    expect(isQuotaError({ response: { status: 409, data: { detail: { code: 'QUOTA_EXHAUSTED' } } } })).toBe(true)
    expect(isQuotaError({ response: { status: 500 } })).toBe(false)
    expect(practiceErrorMessage(quotaError)).toBe('今日额度已用完')
  })

  it('权限不足与未找到有专门文案', () => {
    expect(isPermissionError({ response: { status: 403 } })).toBe(true)
    expect(practiceErrorMessage({ response: { status: 403 } })).toBe('当前账号没有自主练习的管理权限')
    expect(practiceErrorMessage({ response: { status: 404 } })).toBe('题目或提交记录不存在')
  })

  it('AI 预检失败与网络异常各有说法', () => {
    expect(practiceErrorMessage({ response: { status: 409, data: { detail: { code: 'AI_PREFLIGHT_FAILED' } } } }))
      .toBe('AI 评分标准尚未准备完成，请先补齐未就绪题目')
    expect(practiceErrorMessage(new Error('offline'))).toBe('网络异常，请检查连接后重试')
    expect(practiceErrorMessage({ response: { status: 500, data: {} } }, '自定义兜底')).toBe('自定义兜底')
  })
})

describe('practiceHelpers AI 未完成与得分展示', () => {
  it('AI 模式的未完成状态一律视为「AI 评分未完成」', () => {
    expect(aiIncomplete({ grading_mode: 'ai', ai_status: 'pending' })).toBe(true)
    expect(aiIncomplete({ grading_mode: 'ai', ai_status: 'failed' })).toBe(true)
    expect(aiIncomplete({ grading_mode: 'ai', ai_status: 'completed' })).toBe(false)
    expect(aiIncomplete({ grading_mode: 'deterministic', ai_status: 'skipped' })).toBe(false)
    expect(aiStatusText({ grading_mode: 'ai', ai_status: 'failed' })).toBe('AI 评分未完成')
    expect(aiStatusText({ grading_mode: 'deterministic', ai_status: 'skipped' })).toBe('')
  })

  it('分数缺失时显示破折号，不做 0 分替身', () => {
    expect(scoreText({ score: null, max_score: 100 })).toBe('—')
    expect(scoreText({ score: 82, max_score: 100 })).toBe('82 / 100')
    expect(scoreText({ score: 7.25, max_score: 10 })).toBe('7.3 / 10')
    expect(scoreText(null)).toBe('—')
    expect(formatScore(undefined)).toBe('—')
  })

  it('测试摘要只在后端给出计数时展示', () => {
    expect(testSummary({ deterministic: { passed_count: 3, total_count: 5 } })).toBe('3 / 5 组测试通过')
    expect(testSummary({ deterministic: { groups: [] } })).toBe('')
    expect(testSummary({})).toBe('')
  })

  it('client_request_id 每次生成都不同（幂等键不能复用）', () => {
    const first = newClientRequestId()
    const second = newClientRequestId()
    expect(first).toBeTruthy()
    expect(first).not.toBe(second)
  })
})
