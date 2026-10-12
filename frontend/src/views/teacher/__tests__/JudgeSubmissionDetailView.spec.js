// A06 前端：教师作业提交详情的人工评分表单（分数 + 理由 + 乐观并发修订号）。
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const routerPush = vi.fn()
const routerBack = vi.fn()
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '11' }, path: '/teacher/judge/11' }),
  useRouter: () => ({ push: routerPush, back: routerBack }),
}))

vi.mock('../../../api/submissions.js', () => ({
  submissionsAPI: { getTeacherJudgeDetail: vi.fn(), unified: vi.fn() },
}))
vi.mock('../../../api/judge.js', () => ({
  judgeAPI: { manualScore: vi.fn() },
}))

const showToast = vi.fn()
vi.mock('../../../stores/app.js', () => ({ useAppStore: () => ({ showToast }) }))

import { submissionsAPI } from '../../../api/submissions.js'
import { judgeAPI } from '../../../api/judge.js'
import JudgeSubmissionDetailView from '../JudgeSubmissionDetailView.vue'

const DETAIL = {
  id: 11, question_id: 3, student_id: 9, student_name: '张三', student_no: '0001',
  code: 'def add(a, b):\n    return a + b', status: 'succeeded', grading_status: 'completed',
  score: 60, tests_passed: 2, tests_total: 2, result_revision: 3,
  stdout: 'ok', stderr: '', created_at: '2026-08-12T04:00:00Z',
  question_title: '两数之和', assignment_title: '第一次作业', course_title: '程序设计',
  ai_grade_id: null, ai_score: null, ai_needs_review: false, ai_review_reason: null,
}

async function mountDetail(detail = DETAIL) {
  submissionsAPI.getTeacherJudgeDetail.mockResolvedValue({ data: detail })
  const wrapper = mount(JudgeSubmissionDetailView, {
    global: { stubs: { AppLayout: { template: '<main><slot /></main>' } } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('JudgeSubmissionDetailView 人工评分', () => {
  it('打开表单并校验必填项（理由缺失不发请求）', async () => {
    const wrapper = await mountDetail()
    await wrapper.get('[data-testid="manual-score-open"]').trigger('click')
    expect(wrapper.get('[data-testid="manual-score-form"]').exists()).toBe(true)

    await wrapper.get('[data-testid="manual-score-input"]').setValue(85)
    await wrapper.get('[data-testid="manual-score-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="manual-score-error"]').text()).toContain('理由')
    expect(judgeAPI.manualScore).not.toHaveBeenCalled()
  })

  it('提交人工分：带修订号、刷新详情并提示成功', async () => {
    judgeAPI.manualScore.mockResolvedValue({ data: { ...DETAIL, score: 85, result_revision: 4,
      manual_review: { score: 85, reason: '人工复核' } } })
    const wrapper = await mountDetail()
    await wrapper.get('[data-testid="manual-score-open"]').trigger('click')
    await wrapper.get('[data-testid="manual-score-input"]').setValue(85)
    await wrapper.get('[data-testid="manual-score-reason"]').setValue('算法正确，未处理空输入')
    await wrapper.get('[data-testid="manual-score-submit"]').trigger('click')
    await flushPromises()

    expect(judgeAPI.manualScore).toHaveBeenCalledWith('11', {
      score: 85, reason: '算法正确，未处理空输入', expectedResultRevision: 3,
    })
    expect(wrapper.find('[data-testid="manual-score-form"]').exists()).toBe(false)
    expect(showToast).toHaveBeenCalledWith('人工评分已保存', 'success')
    expect(wrapper.text()).toContain('修订号 4')
  })

  it('判题或 AI 在途（409）提示稍后重试且不关闭表单', async () => {
    judgeAPI.manualScore.mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'AI_GRADING_IN_PROGRESS' } } },
    })
    const wrapper = await mountDetail()
    await wrapper.get('[data-testid="manual-score-open"]').trigger('click')
    await wrapper.get('[data-testid="manual-score-input"]').setValue(70)
    await wrapper.get('[data-testid="manual-score-reason"]').setValue('理由')
    await wrapper.get('[data-testid="manual-score-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="manual-score-error"]').text()).toContain('仍在进行')
    expect(wrapper.get('[data-testid="manual-score-form"]').exists()).toBe(true)
  })

  it('结果修订号冲突（409）提示刷新后重试', async () => {
    judgeAPI.manualScore.mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'RESULT_REVISION_CONFLICT' } } },
    })
    const wrapper = await mountDetail()
    await wrapper.get('[data-testid="manual-score-open"]').trigger('click')
    await wrapper.get('[data-testid="manual-score-input"]').setValue(70)
    await wrapper.get('[data-testid="manual-score-reason"]').setValue('理由')
    await wrapper.get('[data-testid="manual-score-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="manual-score-error"]').text()).toContain('其他人更新')
  })

  it('已定分时按钮文案为"修改最终分"', async () => {
    const wrapper = await mountDetail({ ...DETAIL, score: 60 })
    expect(wrapper.get('[data-testid="manual-score-open"]').text()).toContain('修改最终分')
    const wrapper2 = await mountDetail({ ...DETAIL, score: null })
    expect(wrapper2.get('[data-testid="manual-score-open"]').text()).toContain('人工给分')
  })
})
