import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useExperimentStore } from '../experiment.js'

vi.mock('../../api/experiments.js', () => ({
  experimentsAPI: {
    ensureForLesson: vi.fn(),
    ensureForModule: vi.fn(),
    getRecordDetail: vi.fn(),
    saveCells: vi.fn(),
    executeCell: vi.fn(),
    interrupt: vi.fn(),
    restart: vi.fn(),
    submitRecord: vi.fn(),
    listSubmissions: vi.fn(),
  },
}))

import { experimentsAPI } from '../../api/experiments.js'

const ensureResponse = { data: { id: 1, record_revision: 5 } }
const detailResponse = {
  data: {
    id: 1,
    lesson_id: 10,
    student_id: 2,
    template_version_id: 1,
    record_revision: 3,
    entry_name: 'Test Notebook',
    entry_description: 'A test notebook',
    cells: [
      {
        id: 'editable',
        type: 'code',
        source: 'x = 1',
        order: 2,
        student_editable: true,
      },
      {
        id: 'markdown',
        type: 'markdown',
        source: '# Heading',
        order: 0,
        student_editable: false,
      },
      {
        id: 'readonly',
        type: 'code',
        source: 'seed = 1',
        order: 1,
        student_editable: false,
      },
    ],
    execution_count: 0,
  },
}

describe('useExperimentStore', () => {
  let store

  beforeEach(() => {
    vi.resetAllMocks()
    setActivePinia(createPinia())
    store = useExperimentStore()
  })

  afterEach(() => {
    store.destroy()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  async function openLesson() {
    experimentsAPI.ensureForLesson.mockResolvedValue(ensureResponse)
    experimentsAPI.getRecordDetail.mockResolvedValue(detailResponse)
    await store.openLesson(10, 5)
  }

  it('opens lesson and module contexts through the shared API and sorts cells', async () => {
    await openLesson()
    expect(experimentsAPI.ensureForLesson).toHaveBeenCalledWith(10)
    expect(store.context).toEqual({
      type: 'lesson',
      id: 10,
      courseId: 5,
      offeringId: null,
      returnPath: '/student/courses/5',
      title: 'Test Notebook',
    })
    expect(store.cells.map(cell => cell.id)).toEqual([
      'markdown',
      'readonly',
      'editable',
    ])

    experimentsAPI.ensureForModule.mockResolvedValue(ensureResponse)
    experimentsAPI.getRecordDetail.mockResolvedValue({
      data: { ...detailResponse.data, module_id: 20, lesson_id: null },
    })
    await store.openModule(20)
    expect(experimentsAPI.ensureForModule).toHaveBeenCalledWith(20)
    expect(store.context.type).toBe('module')
    expect(store.context.returnPath).toBe('/student/experiments')
  })

  it('exposes no student cell structure mutation methods', () => {
    expect(store.addCell).toBeUndefined()
    expect(store.removeCell).toBeUndefined()
    expect(store.copyCell).toBeUndefined()
    expect(store.reorderCells).toBeUndefined()
  })

  it('only marks editable code cells dirty', async () => {
    await openLesson()

    store.updateCellSource('markdown', '# Changed')
    store.updateCellSource('readonly', 'seed = 99')
    expect(store.cells[0].source).toBe('# Heading')
    expect(store.cells[1].source).toBe('seed = 1')
    expect(store.dirty).toBe(false)

    store.updateCellSource('editable', 'x = 2')
    expect(store.cells[2].source).toBe('x = 2')
    expect(store.dirty).toBe(true)
    expect(store.saved).toBe(false)
  })

  it('debounces a normal edit for 1200ms', async () => {
    vi.useFakeTimers()
    experimentsAPI.saveCells.mockResolvedValue({
      data: { record_revision: 4 },
    })
    await openLesson()

    store.updateCellSource('editable', 'x = 2')
    await vi.advanceTimersByTimeAsync(1199)
    expect(experimentsAPI.saveCells).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(1)
    expect(experimentsAPI.saveCells).toHaveBeenCalledWith(
      1,
      { editable: 'x = 2' },
      3,
    )
  })

  it('uses the independent 30s safety flush while edits keep resetting debounce', async () => {
    vi.useFakeTimers()
    experimentsAPI.saveCells.mockResolvedValue({
      data: { record_revision: 4 },
    })
    await openLesson()

    store.updateCellSource('editable', 'x = 0')
    for (let second = 1; second <= 29; second += 1) {
      await vi.advanceTimersByTimeAsync(1000)
      store.updateCellSource('editable', `x = ${second}`)
    }
    expect(experimentsAPI.saveCells).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1000)
    expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(1)
    expect(experimentsAPI.saveCells).toHaveBeenCalledWith(
      1,
      { editable: 'x = 29' },
      3,
    )
  })

  it('serializes mid-save edits into exactly two revision-ordered requests', async () => {
    let resolveFirst
    let resolveSecond
    experimentsAPI.saveCells
      .mockImplementationOnce(() => new Promise(resolve => {
        resolveFirst = resolve
      }))
      .mockImplementationOnce(() => new Promise(resolve => {
        resolveSecond = resolve
      }))
    await openLesson()

    store.updateCellSource('editable', 'x = 2')
    const firstFlush = store.flushSave()
    expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(1)
    expect(experimentsAPI.saveCells.mock.calls[0]).toEqual([
      1,
      { editable: 'x = 2' },
      3,
    ])

    store.updateCellSource('editable', 'x = 3')
    const secondFlush = store.flushSave()
    let secondFlushResolved = false
    secondFlush.then(() => {
      secondFlushResolved = true
    })
    expect(secondFlushResolved).toBe(false)
    expect(store.dirty).toBe(true)
    expect(store.saved).toBe(false)

    resolveFirst({ data: { record_revision: 4 } })
    await vi.waitFor(() => {
      expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(2)
    })
    expect(experimentsAPI.saveCells.mock.calls[1]).toEqual([
      1,
      { editable: 'x = 3' },
      4,
    ])
    expect(store.dirty).toBe(true)
    expect(store.saved).toBe(false)
    expect(secondFlushResolved).toBe(false)

    resolveSecond({ data: { record_revision: 5 } })
    await Promise.all([firstFlush, secondFlush])
    expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(2)
    expect(store.recordRevision).toBe(5)
    expect(store.dirty).toBe(false)
    expect(store.saved).toBe(true)
    expect(store.saving).toBe(false)
  })

  it('returns false once on an ordinary save failure instead of retrying forever', async () => {
    experimentsAPI.saveCells.mockRejectedValue({
      response: {
        data: { detail: { code: 'STORAGE_ERROR', message: 'Disk unavailable' } },
      },
    })
    await openLesson()
    store.updateCellSource('editable', 'x = 2')

    await expect(store.flushSave()).resolves.toBe(false)
    expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(1)
    expect(store.error).toEqual({
      code: 'STORAGE_ERROR',
      message: 'Disk unavailable',
    })
    expect(store.dirty).toBe(true)
    expect(store.saved).toBe(false)
  })

  it('does not retry a revision conflict after more edits or safety intervals', async () => {
    vi.useFakeTimers()
    experimentsAPI.saveCells.mockRejectedValue({
      response: {
        data: { detail: { code: 'REVISION_CONFLICT', message: 'Conflict' } },
      },
    })
    await openLesson()
    store.updateCellSource('editable', 'x = 2')

    await expect(store.flushSave()).resolves.toBe(false)
    store.updateCellSource('editable', 'x = 3')
    await vi.advanceTimersByTimeAsync(60000)
    await expect(store.flushSave()).resolves.toBe(false)

    expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(1)
    expect(store.conflict).toBe(true)
    expect(store.dirty).toBe(true)
  })

  it.each([
    [false, false],
    [true, true],
  ])('asks before leaving after save failure (confirm=%s)', async (answer, expected) => {
    experimentsAPI.saveCells.mockRejectedValue({
      response: {
        data: { detail: { code: 'STORAGE_ERROR', message: 'Save failed' } },
      },
    })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(answer)
    await openLesson()
    store.updateCellSource('editable', 'x = 2')

    await expect(store.canNavigate()).resolves.toBe(expected)
    expect(confirmSpy).toHaveBeenCalledOnce()
    expect(experimentsAPI.saveCells).toHaveBeenCalledTimes(1)
  })

  it('keeps conflict until a successful reload', async () => {
    experimentsAPI.saveCells.mockRejectedValue({
      response: { data: { detail: { code: 'REVISION_CONFLICT' } } },
    })
    await openLesson()
    store.updateCellSource('editable', 'x = 2')
    await store.flushSave()
    expect(store.conflict).toBe(true)

    await openLesson()
    expect(store.conflict).toBe(false)
    expect(store.dirty).toBe(false)
    expect(store.saved).toBe(true)
  })

  // P1-7: 实验前端状态串页
  it('resets submission state on re-load to prevent cross-page pollution', async () => {
    // 模拟已提交过的状态
    store.submissions = [{ id: 99, attempt_number: 3 }]
    store.submitAttemptCount = 3
    store.lastSubmitTime = '2026-01-01T00:00:00Z'
    store.currentClientRequestId = 'old-uuid'
    store.submitting = true

    // 重新打开（模拟切换 lesson）
    experimentsAPI.ensureForLesson.mockResolvedValue(ensureResponse)
    experimentsAPI.getRecordDetail.mockResolvedValue(detailResponse)
    experimentsAPI.listSubmissions.mockResolvedValue({ data: { items: [] } })
    await store.openLesson(20, 99)

    // 验证所有提交状态已重置
    expect(store.submissions).toEqual([])
    expect(store.submitAttemptCount).toBe(0)
    expect(store.lastSubmitTime).toBeNull()
    expect(store.currentClientRequestId).toBeNull()
    expect(store.submitting).toBe(false)
  })
})

// ═══════════════════════════════════════════════════════════════
// Phase 5：学生端环境提示与 import 诊断
// ═══════════════════════════════════════════════════════════════

describe('Phase 5: environment summary and import diagnostics', () => {
  let store
  const envDetailResponse = {
    data: {
      ...detailResponse.data,
      environment_summary: {
        display_name: '数据分析',
        version_label: 'v1',
        python_version: '3.12',
        imports: ['numpy', 'pandas'],
        import_policy_mode: 'restricted',
        allowed_imports: ['numpy'],
      },
    },
  }

  beforeEach(async () => {
    store = useExperimentStore()
    experimentsAPI.ensureForLesson.mockResolvedValue(ensureResponse)
    experimentsAPI.getRecordDetail.mockResolvedValue(envDetailResponse)
    experimentsAPI.listSubmissions.mockResolvedValue({ data: { items: [] } })
    await store.openLesson(20, 99)
  })

  it('loads environment summary from record detail', () => {
    expect(store.environmentSummary).toEqual({
      display_name: '数据分析',
      version_label: 'v1',
      python_version: '3.12',
      imports: ['numpy', 'pandas'],
      import_policy_mode: 'restricted',
      allowed_imports: ['numpy'],
    })
  })

  it('writes synthetic Chinese error output for IMPORT_NOT_ALLOWED without traceback', async () => {
    experimentsAPI.executeCell.mockRejectedValue({
      response: {
        status: 422,
        data: { detail: { code: 'IMPORT_NOT_ALLOWED', message: 'numpy 未在本实验允许范围内' } },
      },
    })
    await store.executeCell('editable')
    const out = store.cells.find(c => c.id === 'editable').outputs
    expect(out.outputs[0].msg_type).toBe('error')
    expect(out.outputs[0].content.text).toBe('numpy 未在本实验允许范围内')
    expect(out.outputs[0].content.text).not.toMatch(/Traceback/)
  })

  it('writes synthetic Chinese error output for IMPORT_NOT_INSTALLED without traceback', async () => {
    experimentsAPI.executeCell.mockRejectedValue({
      response: {
        status: 500,
        data: { detail: { code: 'IMPORT_NOT_INSTALLED', message: '环境配置错误：本实验允许 numpy，但当前环境未安装' } },
      },
    })
    await store.executeCell('editable')
    const out = store.cells.find(c => c.id === 'editable').outputs
    expect(out.outputs[0].content.text).toBe('环境配置错误：本实验允许 numpy，但当前环境未安装')
    expect(out.outputs[0].content.text).not.toMatch(/Traceback|ModuleNotFoundError/)
  })
})
