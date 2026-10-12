// 练习作答反馈：排队态、AI 未完成态（不得显示 0 分或完整 AI 成绩）、完成态与异议入口
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

const appeals = vi.hoisted(() => ({ create: vi.fn(), withdraw: vi.fn(), reply: vi.fn() }))
vi.mock('../../../api/appeals.js', () => ({ appealsAPI: appeals }))

import PracticeSubmissionFeedback from '../PracticeSubmissionFeedback.vue'

function mountFeedback(submission) {
  return mount(PracticeSubmissionFeedback, {
    props: { submission },
    global: { plugins: [createPinia()] },
  })
}

const baseSubmission = {
  id: 11,
  problem: { id: 3, title: '统计列表中所有正数的和' },
  code: 'print(1)',
  grading_mode: 'deterministic',
  grading_status: 'completed',
  score: 100,
  max_score: 100,
  created_at: '2026-10-13T02:00:00Z',
  deterministic: { groups: [{ name: '公开用例', status: 'passed', passed: 3, total: 3 }], passed_count: 3, total_count: 3 },
  ai_result: null,
  ai_status: 'skipped',
  appeal: null,
  can_appeal: false,
}

describe('PracticeSubmissionFeedback', () => {
  beforeEach(() => vi.resetAllMocks())

  it('排队中展示名次，且不展示任何分数', () => {
    const wrapper = mountFeedback({
      ...baseSubmission,
      grading_status: 'queued',
      queue_position: 3,
      score: null,
      deterministic: null,
    })
    expect(wrapper.text()).toContain('排队中（第 3 位）')
    expect(wrapper.text()).toContain('判题完成后才会给出分数')
    expect(wrapper.text()).not.toContain('本次得分')
  })

  it('AI 未完成时展示「AI 评分未完成」与已完成的确定性分，不展示 0 分或 AI 四维', () => {
    const wrapper = mountFeedback({
      ...baseSubmission,
      grading_mode: 'ai',
      ai_status: 'pending',
      score: 60,
      ai_result: null,
    })
    expect(wrapper.text()).toContain('AI 评分未完成')
    expect(wrapper.text()).toContain('60 / 100')
    expect(wrapper.text()).toContain('3 / 3 组测试通过')
    expect(wrapper.text()).not.toContain('功能正确性 F')
    // 未完成评分绝不以 0 分顶替：得分区块只出现确定性分
    expect(wrapper.find('.score-value').text()).toBe('60 / 100')
  })

  it('AI 评分完成后渲染四维分与异议入口', () => {
    const wrapper = mountFeedback({
      ...baseSubmission,
      grading_mode: 'ai',
      ai_status: 'completed',
      can_appeal: true,
      appeal: null,
      ai_result: {
        functional_score: 55, algorithm_score: 18, robustness_score: 8, quality_score: 9,
        final_score_100: 90, strengths: ['边界处理完整'], issues: [], suggestions: [], code_suggestions: [],
        test_groups: [], algorithm_items: [], quality_items: [],
      },
    })
    expect(wrapper.text()).toContain('功能正确性 F')
    expect(wrapper.text()).toContain('算法关键步骤 A')
    expect(wrapper.find('button').exists()).toBe(true)
    expect(wrapper.text()).toContain('我有异议')
  })

  it('判题失败时明确不计分，不折算为 0 分', () => {
    const wrapper = mountFeedback({
      ...baseSubmission,
      grading_status: 'failed',
      score: 0,
      ai_status: 'failed',
      grading_mode: 'ai',
    })
    expect(wrapper.text()).toContain('判题失败，本次不计分')
    expect(wrapper.text()).not.toContain('本次得分')
  })

  it('评分标准更新时提示历史成绩不参与当前榜单', () => {
    const wrapper = mountFeedback({ ...baseSubmission, grading_version_stale: true })
    expect(wrapper.text()).toContain('本题评分标准已更新')
  })
})
