// 批次 2：一键 AI 判题对话框（作业题/考试题共用入口）行为验收
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

const api = vi.hoisted(() => ({
  createSetupJob: vi.fn(),
  getSetupJob: vi.fn(),
  retrySetupJob: vi.fn(),
  createTrialRun: vi.fn(),
  getTrialRun: vi.fn(),
  enableSetupJob: vi.fn(),
}))

vi.mock('../../../api/aiGrading.js', () => ({ aiGradingAPI: api }))

import AISetupDialog from '../AISetupDialog.vue'

const READY_JOB = {
  id: 11, question_kind: 'assignment', question_id: 7, stage: 'draft_ready', progress: 100,
  draft_rubric_id: 3, draft_rubric_status: 'draft',
  draft_rubric: {
    rubric_version: 1,
    algorithm_criteria: [{ id: 'A1', name: '正确性', points: 10 },
                         { id: 'A2', name: '完整性', points: 10 }],
    quality_criteria: [{ id: 'Q1', name: '命名', points: 3 }],
  },
  draft_test_groups: [{ id: 'F1', dimension: 'F', name: '功能', max_score: 60 },
                      { id: 'R1', dimension: 'R', name: '边界', max_score: 10 }],
  generate_test_groups: true, error_code: null, error_message: null,
}

const wait = (ms) => new Promise((resolve) => { setTimeout(resolve, ms) })
const POLL_WAIT = 2200

function mountDialog(props = {}) {
  return mount(AISetupDialog, {
    props: {
      kind: 'assignment',
      questionId: 7,
      defaultReferenceSolution: 'def solve(n):\n    return n',
      defaultConstraintsText: '强调边界条件处理',
      defaultGradingMode: 'active',
      ...props,
    },
    global: {
      plugins: [createPinia()],
      stubs: {
        AppIcon: { template: '<span class="icon-stub" />', props: ['name', 'size'] },
        DsSelect: { template: '<select><slot /></select>', props: ['modelValue'] },
      },
    },
  })
}

async function openAndStart(wrapper) {
  await wrapper.find('.ai-setup-trigger').trigger('click')
  await wrapper.find('.dialog-actions .btn-primary').trigger('click')
  await flushPromises()
}

describe('AISetupDialog 一键 AI 判题', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('点【AI 判题】填写参考解法与评分侧重后提交 setup-jobs', async () => {
    api.createSetupJob.mockResolvedValue({ data: READY_JOB })
    const wrapper = mountDialog()

    expect(wrapper.find('.ai-setup-mask').exists()).toBe(false)
    await openAndStart(wrapper)

    expect(api.createSetupJob).toHaveBeenCalledTimes(1)
    expect(api.createSetupJob.mock.calls[0][0]).toMatchObject({
      question_kind: 'assignment',
      question_id: 7,
      reference_solution: 'def solve(n):\n    return n',
      teacher_constraints: { requirements_text: '强调边界条件处理' },
      grading_mode: 'active',
      generate_test_groups: true,
    })
  })

  it('轮询到 draft_ready 后展示分阶段进度与草稿预览', async () => {
    api.createSetupJob.mockResolvedValue({ data: { ...READY_JOB, stage: 'queued', progress: 5, draft_rubric: null, draft_test_groups: [] } })
    api.getSetupJob
      .mockResolvedValueOnce({ data: { ...READY_JOB, stage: 'generating_rubric', progress: 30, draft_rubric: null, draft_test_groups: [] } })
      .mockResolvedValueOnce({ data: READY_JOB })
    const wrapper = mountDialog()
    await openAndStart(wrapper)

    expect(wrapper.text()).toContain('生成评分标准')

    await wait(POLL_WAIT)
    await flushPromises()
    expect(wrapper.text()).toContain('进行中')

    await wait(POLL_WAIT)
    await flushPromises()

    expect(api.getSetupJob).toHaveBeenCalledWith(11)
    expect(wrapper.text()).toContain('待确认启用')
    expect(wrapper.text()).toContain('预览草稿')
    expect(wrapper.text()).toContain('A · 正确性（10 分）')
    expect(wrapper.text()).toContain('F · 功能（60 分）')
    expect(wrapper.text()).toContain('评分标准状态：草稿')
  })

  it('保存并启用调用 enable 端点并回传启用结果', async () => {
    api.createSetupJob.mockResolvedValue({ data: READY_JOB })
    api.enableSetupJob.mockResolvedValue({
      data: { ok: true, job_id: 11, rubric_id: 3, version_id: 9, version: 2, status: 'active' },
    })
    const wrapper = mountDialog()
    await openAndStart(wrapper)

    const enableButton = wrapper.findAll('button').find((node) => node.text() === '保存并启用')
    expect(enableButton).toBeTruthy()
    await enableButton.trigger('click')
    await flushPromises()

    expect(api.enableSetupJob).toHaveBeenCalledWith(11, {})
    const emitted = wrapper.emitted('enabled')
    expect(emitted).toBeTruthy()
    expect(emitted[0][0]).toMatchObject({ version: 2, rubric_id: 3, status: 'active' })
    // 成功后关闭对话框（入口按钮回到可见状态）
    expect(wrapper.find('.ai-setup-mask').exists()).toBe(false)
  })

  it('生成失败保留草稿并给出重试入口', async () => {
    api.createSetupJob.mockResolvedValue({
      data: { ...READY_JOB, stage: 'failed', progress: 0, draft_test_groups: [],
              error_code: 'AI_GENERATION_INVALID', error_message: 'AI 生成失败，草稿已保留' },
    })
    api.retrySetupJob.mockResolvedValue({ data: { ...READY_JOB, stage: 'generating_test_groups', progress: 70 } })
    api.getSetupJob.mockResolvedValue({ data: READY_JOB })
    const wrapper = mountDialog()
    await openAndStart(wrapper)

    expect(wrapper.text()).toContain('生成失败')
    expect(wrapper.text()).toContain('已生成的草稿会保留')

    const retryButton = wrapper.findAll('button').find((node) => node.text().includes('重试（保留草稿）'))
    expect(retryButton).toBeTruthy()
    await retryButton.trigger('click')
    await flushPromises()
    expect(api.retrySetupJob).toHaveBeenCalledWith(11)

    await wait(POLL_WAIT)
    await flushPromises()
    expect(wrapper.text()).toContain('预览草稿')
  })

  it('试判走 trial-runs 且明确标注不含确定性 F/R 分', async () => {
    api.createSetupJob.mockResolvedValue({ data: READY_JOB })
    api.createTrialRun.mockResolvedValue({ data: { id: 21, status: 'running' } })
    api.getTrialRun.mockResolvedValue({
      data: { id: 21, status: 'completed', algorithm_score: 20, quality_score: 10,
              preview_score_100: 30, includes_deterministic: false, error_message: null },
    })
    const wrapper = mountDialog()
    await openAndStart(wrapper)

    expect(wrapper.text()).toContain('不包含 F/R 确定性分')

    await wrapper.find('.setup-code').setValue('def solve(n):\n    return n')
    const trialButton = wrapper.findAll('button').find((node) => node.text() === '运行试判')
    await trialButton.trigger('click')
    await flushPromises()
    await wait(POLL_WAIT)
    await flushPromises()

    expect(api.createTrialRun).toHaveBeenCalledTimes(1)
    const [jobId, payload] = api.createTrialRun.mock.calls[0]
    expect(jobId).toBe(11)
    expect(payload.code).toBe('def solve(n):\n    return n')
    expect(typeof payload.client_request_id).toBe('string')
    expect(api.getTrialRun).toHaveBeenCalledWith(21)
    expect(wrapper.text()).toContain('试判不包含确定性 F/R 分')
    expect(wrapper.text()).toContain('预览 AI 维度合计 30 分')
  })

  it('考试题入口同样可用（kind=exam）', async () => {
    api.createSetupJob.mockResolvedValue({ data: { ...READY_JOB, question_kind: 'exam' } })
    const wrapper = mountDialog({ kind: 'exam', questionId: 42, defaultConstraintsText: '' })
    await openAndStart(wrapper)

    expect(api.createSetupJob.mock.calls[0][0]).toMatchObject({
      question_kind: 'exam', question_id: 42, teacher_constraints: {},
    })
  })
})
