import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// vi.mock 的工厂会被提升到 import 之前，因此用 vi.hoisted 声明 mock 对象
const api = vi.hoisted(() => ({
  list: vi.fn(),
  confirm: vi.fn(),
  activate: vi.fn(),
  retire: vi.fn(),
  convertPreview: vi.fn(),
  convertApply: vi.fn(),
  createDraft: vi.fn(),
  detail: vi.fn(),
  createAiDraft: vi.fn(),
  preflight: vi.fn(),
  encodeLiteral: vi.fn(),
  decodeLiteral: vi.fn(),
  aiGenerate: vi.fn(),
}))

vi.mock('../../../api/gradingVersions.js', () => ({
  gradingVersionsAPI: api,
  default: api,
}))

import GradingVersionPanel from '../GradingVersionPanel.vue'

function version(overrides = {}) {
  return {
    id: 7, version: 1, status: 'draft', source_kind: 'converted_from_pytest',
    case_count: 3, spec_digest: 'a'.repeat(64), rubric_binding_ok: true, ...overrides,
  }
}

function mountPanel(props = {}) {
  return mount(GradingVersionPanel, { props: { questionId: 42, ...props } })
}

const READY_PREFLIGHT = {
  in_flight: 0, unbound_legacy: 0, started_exams: 0, ready: true, blocking: [], warnings: [],
}

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset())
  api.list.mockResolvedValue({ data: { items: [], current_grading_version_id: null } })
  api.preflight.mockResolvedValue({ data: { ...READY_PREFLIGHT } })
})

describe('GradingVersionPanel', () => {
  it('加载并展示版本列表与当前启用版本', async () => {
    api.list.mockResolvedValue({
      data: {
        items: [
          version({ id: 9, version: 2, status: 'active' }),
          version({ id: 8, version: 1, status: 'retired', source_kind: 'manual' }),
        ],
        current_grading_version_id: 9,
      },
    })
    const wrapper = mountPanel()
    await flushPromises()

    expect(api.list).toHaveBeenCalledWith(42, 'assignment')
    expect(wrapper.get('[data-testid="gv-current"]').text()).toContain('#9')
    expect(wrapper.get('[data-testid="gv-row-9"]').text()).toContain('启用中')
    expect(wrapper.get('[data-testid="gv-row-8"]').text()).toContain('已退役')
    expect(wrapper.get('[data-testid="gv-row-8"]').text()).toContain('历史版本可读')
    // 只有 active 行提供退役；confirmed 行提供启用；draft 行提供确认
    expect(wrapper.find('[data-testid="gv-retire-9"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="gv-activate-9"]').exists()).toBe(false)
  })

  it('无版本时给出空态与未启用标记', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-empty"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="gv-current-none"]').exists()).toBe(true)
  })

  it('转换预检只报告、不生成版本', async () => {
    api.convertPreview.mockResolvedValue({
      data: {
        summary: {
          convertible_count: 2, unsupported_count: 1, fully_convertible: false,
          reasons: ['包含控制流/赋值/导入等语句，无法静态证明等价'], notes: ['转换后需人工补齐'],
        },
      },
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-preview"]').trigger('click')
    await flushPromises()

    expect(api.convertPreview).toHaveBeenCalledWith(42, {}, 'assignment')
    expect(api.convertApply).not.toHaveBeenCalled()
    const result = wrapper.get('[data-testid="gv-preview-result"]').text()
    expect(result).toContain('可转换 2')
    expect(result).toContain('需人工处理 1')
    expect(result).toContain('控制流')
    expect(wrapper.get('[data-testid="gv-notice"]').text()).toContain('尚未写入任何版本')
  })

  it('无可转换用例时禁用生成按钮', async () => {
    api.convertPreview.mockResolvedValue({
      data: { summary: { convertible_count: 0, unsupported_count: 3, fully_convertible: false, reasons: [], notes: [] } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-preview"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-apply"]').attributes('disabled')).toBeDefined()
  })

  it('生成草稿后刷新列表并提示确认', async () => {
    api.convertPreview.mockResolvedValue({
      data: { summary: { convertible_count: 1, unsupported_count: 0, fully_convertible: true, reasons: [], notes: [] } },
    })
    api.convertApply.mockResolvedValue({
      data: { version: version({ id: 11, version: 3 }), summary: { convertible_count: 1, unsupported_count: 0 } },
    })
    api.list
      .mockResolvedValueOnce({ data: { items: [], current_grading_version_id: null } })
      .mockResolvedValueOnce({ data: { items: [version({ id: 11, version: 3 })], current_grading_version_id: null } })

    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-preview"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="gv-apply"]').trigger('click')
    expect(api.convertApply).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('每个测试用例会在独立容器中运行')
    await wrapper.get('.confirm-actions .btn-primary').trigger('click')
    await flushPromises()

    expect(api.convertApply).toHaveBeenCalledWith(
      42, { confirm_isolated_case_v2: true }, 'assignment')
    expect(wrapper.get('[data-testid="gv-notice"]').text()).toContain('#3')
    expect(wrapper.get('[data-testid="gv-row-11"]').exists()).toBe(true)
  })

  it('确认/启用/退役都必须先经确认对话框', async () => {
    api.list.mockResolvedValue({
      data: {
        items: [version({ id: 7, status: 'draft' })],
        current_grading_version_id: null,
      },
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('[data-testid="gv-confirm-7"]').trigger('click')
    expect(api.confirm).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('确认评分版本 #1')
    api.confirm.mockResolvedValue({ data: version({ status: 'confirmed' }) })
    await wrapper.get('.confirm-actions .btn-primary').trigger('click')
    await flushPromises()
    expect(api.confirm).toHaveBeenCalledWith(7, 'a'.repeat(64), 'assignment', true)
  })

  it('启用调用 activate 并展示成功提示', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 8, version: 2, status: 'confirmed' })], current_grading_version_id: null },
    })
    api.activate.mockResolvedValue({ data: version({ id: 8, status: 'active' }) })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-activate-8"]').trigger('click')
    await wrapper.get('.confirm-actions .btn-primary').trigger('click')
    await flushPromises()
    expect(api.activate).toHaveBeenCalledWith(8, { acknowledgeInFlight: false, kind: 'assignment' })
    expect(wrapper.get('[data-testid="gv-notice"]').text()).toContain('已启用')
  })

  it('接口失败时展示后端中文信息且不静默', async () => {
    api.list.mockRejectedValueOnce({
      response: { data: { detail: { code: 'FORBIDDEN', message: '没有该作业的授课范围' } } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-error"]').text()).toBe('没有该作业的授课范围')

    api.list.mockResolvedValue({ data: { items: [], current_grading_version_id: null } })
    api.convertPreview.mockRejectedValueOnce({
      response: { data: { detail: { code: 'CONVERSION_PREVIEW_FAILED', message: '源码语法错误: invalid syntax' } } },
    })
    await wrapper.get('[data-testid="gv-refresh"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="gv-preview"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-error"]').text()).toContain('语法错误')
  })
})


describe('GradingVersionPanel 启用前核查', () => {
  it('展示影响面摘要', async () => {
    api.preflight.mockResolvedValue({
      data: { in_flight: 2, unbound_legacy: 5, started_exams: 1, ready: false,
              blocking: ['有 2 个任务正在排队或判题中，切换版本前必须先等它们结束'],
              warnings: ['有 5 条历史记录没有评分版本基线'] },
    })
    const wrapper = mountPanel()
    await flushPromises()
    const text = wrapper.get('[data-testid="gv-preflight"]').text()
    expect(text).toContain('在途任务 2')
    expect(text).toContain('无基线历史 5')
    expect(text).toContain('进行中考试 1')
    expect(text).toContain('启用需显式确认')
  })

  it('无在途任务时直接启用（不带 acknowledge 参数）', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 8, status: 'confirmed' })], current_grading_version_id: null },
    })
    api.activate.mockResolvedValue({ data: version({ id: 8, status: 'active' }) })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-activate-8"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('启用后新提交绑定该版本')
    await wrapper.get('.confirm-actions .btn-primary').trigger('click')
    await flushPromises()
    expect(api.activate).toHaveBeenCalledWith(8, { acknowledgeInFlight: false, kind: 'assignment' })
  })

  it('有在途任务时确认框给出原因并要求显式确认', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 8, status: 'confirmed' })], current_grading_version_id: null },
    })
    api.preflight.mockResolvedValue({
      data: { in_flight: 1, unbound_legacy: 0, started_exams: 0, ready: false,
              blocking: ['有 1 个任务正在排队或判题中，切换版本前必须先等它们结束'],
              warnings: [] },
    })
    api.activate.mockResolvedValue({ data: version({ id: 8, status: 'active' }) })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-activate-8"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('影响面核查未通过')
    expect(wrapper.text()).toContain('仍然启用')
    // 危险样式下确认按钮是 btn-danger-solid，因此按位置取最后一个按钮
    await wrapper.get('.confirm-actions button:last-child').trigger('click')
    await flushPromises()
    expect(api.activate).toHaveBeenCalledWith(8, { acknowledgeInFlight: true, kind: 'assignment' })
  })

  it('预检失败不阻断版本列表', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 7, status: 'draft' })], current_grading_version_id: null },
    })
    api.preflight.mockRejectedValue(new Error('boom'))
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-row-7"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="gv-preflight"]').exists()).toBe(false)
  })
})


describe('GradingVersionPanel 考试题（kind=exam）', () => {
  it('全部调用走 /exams 前缀', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 5, status: 'draft' })], current_grading_version_id: null },
    })
    api.convertPreview.mockResolvedValue({
      data: { summary: { convertible_count: 1, unsupported_count: 0, fully_convertible: true, reasons: [], notes: [] } },
    })
    const wrapper = mountPanel({ kind: 'exam' })
    await flushPromises()
    expect(api.list).toHaveBeenCalledWith(42, 'exam')
    expect(api.preflight).toHaveBeenCalledWith(42, 'exam')

    await wrapper.get('[data-testid="gv-preview"]').trigger('click')
    await flushPromises()
    expect(api.convertPreview).toHaveBeenCalledWith(42, {}, 'exam')

    api.confirm.mockResolvedValue({ data: version({ status: 'confirmed' }) })
    await wrapper.get('[data-testid="gv-confirm-5"]').trigger('click')
    await wrapper.get('.confirm-actions button:last-child').trigger('click')
    await flushPromises()
    expect(api.confirm).toHaveBeenCalledWith(5, 'a'.repeat(64), 'exam', true)
  })

  it('考试题启用时也传递 kind', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 6, status: 'confirmed' })], current_grading_version_id: null },
    })
    api.activate.mockResolvedValue({ data: version({ id: 6, status: 'active' }) })
    const wrapper = mountPanel({ kind: 'exam' })
    await flushPromises()
    await wrapper.get('[data-testid="gv-activate-6"]').trigger('click')
    await flushPromises()
    await wrapper.get('.confirm-actions button:last-child').trigger('click')
    await flushPromises()
    expect(api.activate).toHaveBeenCalledWith(6, { acknowledgeInFlight: false, kind: 'exam' })
  })
})


describe('GradingVersionPanel 未确认草稿与用例明细', () => {
  it('存在比启用版本更新的草稿时给出提示', async () => {
    api.list.mockResolvedValue({
      data: {
        items: [version({ id: 12, version: 3, status: 'draft' }),
                version({ id: 11, version: 2, status: 'active' })],
        current_grading_version_id: 11,
      },
    })
    const wrapper = mountPanel()
    await flushPromises()
    const hint = wrapper.get('[data-testid="gv-draft-hint"]').text()
    expect(hint).toContain('#3')
    expect(hint).toContain('需先「确认」再「启用」')
  })

  it('没有更新的草稿时不提示（已启用版本是最新的）', async () => {
    api.list.mockResolvedValue({
      data: {
        items: [version({ id: 11, version: 2, status: 'active' }),
                version({ id: 10, version: 1, status: 'retired' })],
        current_grading_version_id: 11,
      },
    })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="gv-draft-hint"]').exists()).toBe(false)
  })

  it('查看用例明细：按需拉取详情并展开逐例信息', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 7, status: 'draft' })], current_grading_version_id: null },
    })
    api.detail.mockResolvedValue({
      data: {
        ...version({ id: 7, status: 'draft' }),
        payload: {
          groups: [
            { id: 'F1', dimension: 'F', cases: [
              { id: 'F1-1', compare: { kind: 'literal' }, expected: { t: 'int' } },
              { id: 'F1-2', compare: { kind: 'float', abs_tol: 1e-6 }, expected: { t: 'float' } },
            ] },
            { id: 'R1', dimension: 'R', cases: [
              { id: 'R1-1', compare: { kind: 'exception', types: ['ValueError'] },
                expected: { t: 'none' } },
            ] },
          ],
        },
      },
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-cases-7"]').trigger('click')
    await flushPromises()

    expect(api.detail).toHaveBeenCalledWith(7, 'assignment')
    expect(wrapper.get('[data-testid="gv-case-F1-1"]').text()).toContain('literal')
    expect(wrapper.get('[data-testid="gv-case-F1-2"]').text()).toContain('float')
    expect(wrapper.get('[data-testid="gv-case-R1-1"]').text()).toContain('exception')
    expect(wrapper.get('[data-testid="gv-case-R1-1"]').text()).toContain('R')

    // 再次点击收起
    await wrapper.get('[data-testid="gv-cases-7"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="gv-cases"]').exists()).toBe(false)
  })

  it('详情拉取失败时展示错误且不展开', async () => {
    api.list.mockResolvedValue({
      data: { items: [version({ id: 7, status: 'draft' })], current_grading_version_id: null },
    })
    api.detail.mockRejectedValue({
      response: { data: { detail: { code: 'FORBIDDEN', message: '没有该作业的授课范围' } } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-cases-7"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-error"]').text()).toContain('授课范围')
    expect(wrapper.find('[data-testid="gv-cases"]').exists()).toBe(false)
  })
})


describe('GradingVersionPanel 逐例表单', () => {
  it('填写用例并保存为草稿：先编码字面量再提交 payload', async () => {
    api.encodeLiteral
      .mockResolvedValueOnce({ data: { value: { t: 'tuple', v: [{ t: 'int', v: 1 }] } } })
      .mockResolvedValueOnce({ data: { value: { t: 'int', v: 2 } } })
    api.createDraft.mockResolvedValue({ data: version({ id: 21, version: 4, status: 'draft' }) })
    api.list
      .mockResolvedValueOnce({ data: { items: [], current_grading_version_id: null } })
      .mockResolvedValueOnce({ data: { items: [version({ id: 21, version: 4 })], current_grading_version_id: null } })

    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-form-toggle"]').trigger('click')
    await wrapper.get('[data-testid="gv-form-entry"]').setValue('double_first')
    await wrapper.get('[data-testid="gv-form-add"]').trigger('click')
    await flushPromises()

    const row = wrapper.get('[data-testid="gv-form-row-0"]')
    const inputs = row.findAll('input')
    await inputs[3].setValue('(1,)')
    await inputs[4].setValue('2')
    await wrapper.get('[data-testid="gv-form-save"]').trigger('click')
    await flushPromises()

    expect(api.encodeLiteral).toHaveBeenNthCalledWith(1, '(1,)')
    expect(api.encodeLiteral).toHaveBeenNthCalledWith(2, '2')
    const [questionId, body, kind] = api.createDraft.mock.calls[0]
    expect(questionId).toBe(42)
    expect(kind).toBe('assignment')
    expect(body.entry_function).toBe('double_first')
    expect(body.payload.groups).toHaveLength(1)
    const group = body.payload.groups[0]
    expect(group.dimension).toBe('F')
    expect(group.cases[0]).toEqual({
      id: 'F1-1', input: { t: 'tuple', v: [{ t: 'int', v: 1 }] },
      expected: { t: 'int', v: 2 }, compare: { kind: 'literal' },
    })
    expect(wrapper.get('[data-testid="gv-notice"]').text()).toContain('#4')
  })

  it('缺少入口函数或必填列时给出错误且不提交', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-form-toggle"]').trigger('click')
    await wrapper.get('[data-testid="gv-form-add"]').trigger('click')
    await wrapper.get('[data-testid="gv-form-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-form-error"]').text()).toContain('入口函数')
    expect(api.createDraft).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="gv-form-entry"]').setValue('solve')
    await wrapper.get('[data-testid="gv-form-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-form-error"]').text()).toContain('必须填写')
    expect(api.createDraft).not.toHaveBeenCalled()
  })

  it('字面量编码失败时展示后端原因', async () => {
    api.encodeLiteral.mockRejectedValue({
      response: { data: { detail: { code: 'INVALID_LITERAL', message: '必须是合法的 Python 字面量' } } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-form-toggle"]').trigger('click')
    await wrapper.get('[data-testid="gv-form-entry"]').setValue('solve')
    await wrapper.get('[data-testid="gv-form-add"]').trigger('click')
    await flushPromises()
    const row = wrapper.get('[data-testid="gv-form-row-0"]')
    const inputs = row.findAll('input')
    await inputs[3].setValue('open(1)')
    await inputs[4].setValue('1')
    await wrapper.get('[data-testid="gv-form-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-form-error"]').text()).toContain('Python 字面量')
    expect(api.createDraft).not.toHaveBeenCalled()
  })

  it('浮点与异常规则生成对应比较规则', async () => {
    api.encodeLiteral.mockResolvedValue({ data: { value: { t: 'float', v: 1.0 } } })
    api.createDraft.mockResolvedValue({ data: version({ id: 22, version: 5 }) })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-form-toggle"]').trigger('click')
    await wrapper.get('[data-testid="gv-form-entry"]').setValue('solve')
    await wrapper.get('[data-testid="gv-form-add"]').trigger('click')
    await flushPromises()
    const row = wrapper.get('[data-testid="gv-form-row-0"]')
    const inputs = row.findAll('input')
    await inputs[3].setValue('(1.0,)')
    await inputs[4].setValue('1.0')
    // 行内两个 select：0=维度(F/R)，1=比较规则
    await row.findAll('select')[1].setValue('float')
    await wrapper.get('[data-testid="gv-form-save"]').trigger('click')
    await flushPromises()
    const body = api.createDraft.mock.calls[0][1]
    expect(body.payload.groups[0].cases[0].compare.kind).toBe('float')
    expect(body.payload.groups[0].cases[0].compare.abs_tol).toBeGreaterThan(0)
  })
})


describe('GradingVersionPanel AI 生成入口', () => {
  it('作业题显示 AI 生成按钮并展示生成的草稿', async () => {
    api.aiGenerate.mockResolvedValue({
      data: { version: version({ id: 31, version: 6, status: 'draft', source_kind: 'ai_draft' }),
              summary: { convertible_count: 3, unsupported_count: 1 }, warnings: [] },
    })
    api.list
      .mockResolvedValueOnce({ data: { items: [], current_grading_version_id: null } })
      .mockResolvedValueOnce({ data: { items: [version({ id: 31, version: 6 })], current_grading_version_id: null } })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-ai-generate"]').trigger('click')
    await flushPromises()

    expect(api.aiGenerate).toHaveBeenCalledWith(42, { entry_function: undefined })
    const notice = wrapper.get('[data-testid="gv-notice"]').text()
    expect(notice).toContain('#6')
    expect(notice).toContain('需确认后启用')
    expect(wrapper.get('[data-testid="gv-row-31"]').exists()).toBe(true)
  })

  it('AI 未就绪时展示后端原因且不产生版本', async () => {
    api.aiGenerate.mockRejectedValue({
      response: { data: { detail: { code: 'AI_NOT_READY', message: 'AI 服务未配置 API Key' } } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('[data-testid="gv-ai-generate"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-error"]').text()).toContain('未配置 API Key')
    expect(wrapper.find('[data-testid="gv-row-31"]').exists()).toBe(false)
  })

  it('考试题不显示 AI 生成按钮（考试题没有 function_name）', async () => {
    const wrapper = mountPanel({ kind: 'exam' })
    await flushPromises()
    expect(wrapper.find('[data-testid="gv-ai-generate"]').exists()).toBe(false)
  })
})


describe('GradingVersionPanel 切题（A05）', () => {
  it('题目变化时清空上一题状态并重新加载', async () => {
    api.list
      .mockResolvedValueOnce({ data: { items: [version({ id: 7 })], current_grading_version_id: 7 } })
      .mockResolvedValueOnce({ data: { items: [], current_grading_version_id: null } })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-row-7"]').exists()).toBe(true)

    // 打开一个确认框，切题后必须消失
    await wrapper.get('[data-testid="gv-confirm-7"]').trigger('click')
    expect(wrapper.find('.confirm-actions').exists()).toBe(true)

    await wrapper.setProps({ questionId: 99 })
    await flushPromises()

    expect(api.list).toHaveBeenLastCalledWith(99, 'assignment')
    expect(wrapper.find('[data-testid="gv-row-7"]').exists()).toBe(false), '不得残留上一题的版本'
    expect(wrapper.find('.confirm-actions').exists()).toBe(false), '切题必须关闭确认框'
    expect(wrapper.get('[data-testid="gv-empty"]').exists()).toBe(true)
  })

  it('切题后到达的旧请求结果被丢弃（拒绝迟到响应）', async () => {
    let resolveFirst
    api.list
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({ data: { items: [version({ id: 21 })], current_grading_version_id: null } })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.setProps({ questionId: 100 })
    await flushPromises()
    expect(wrapper.get('[data-testid="gv-row-21"]').exists()).toBe(true)

    // 旧题请求此刻才返回：不得覆盖新题数据
    resolveFirst({ data: { items: [version({ id: 7 })], current_grading_version_id: 7 } })
    await flushPromises()
    expect(wrapper.find('[data-testid="gv-row-7"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="gv-row-21"]').exists()).toBe(true)
  })

  it('kind 变化同样重新加载（作业 ↔ 考试）', async () => {
    api.list.mockResolvedValue({ data: { items: [], current_grading_version_id: null } })
    const wrapper = mountPanel({ kind: 'assignment' })
    await flushPromises()
    await wrapper.setProps({ kind: 'exam' })
    await flushPromises()
    expect(api.list).toHaveBeenLastCalledWith(42, 'exam')
  })
})
