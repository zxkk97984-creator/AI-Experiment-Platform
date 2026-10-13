import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useExperimentStore } from '../../../stores/experiment.js'

const toast = vi.hoisted(() => vi.fn())

vi.mock('../../../stores/app.js', () => ({
  useAppStore: () => ({ showToast: toast }),
}))

vi.mock('../../../api/experiments.js', () => ({
  experimentsAPI: {
    ensureForLesson: vi.fn(),
    ensureForModule: vi.fn(),
    getRecordDetail: vi.fn(),
    saveCells: vi.fn(),
    executeCell: vi.fn(),
    getExecution: vi.fn(),
    getActiveExecution: vi.fn(),
    cancelExecution: vi.fn(),
    interrupt: vi.fn(),
    restart: vi.fn(),
  },
}))

vi.mock('../../../api/labs.js', () => ({
  labsAPI: { checkCell: vi.fn(), getRun: vi.fn() },
}))

import { experimentsAPI } from '../../../api/experiments.js'

async function openStore() {
  experimentsAPI.ensureForModule.mockResolvedValue({ data: { id: 101, record_revision: 1 } })
  experimentsAPI.getRecordDetail.mockResolvedValue({
    data: {
      id: 101,
      record_revision: 1,
      entry_name: '执行状态用例',
      cells: [{ id: 'c1', type: 'code', source: 'print(1)', order: 0, student_editable: true }],
    },
  })
  experimentsAPI.getActiveExecution.mockResolvedValue({ data: { execution: null } })
  const store = useExperimentStore()
  await store.openModule(7)
  return store
}

describe('experiment store async cell execution', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.useFakeTimers()
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('posts with an idempotency key and polls until the terminal state', async () => {
    const store = await openStore()
    experimentsAPI.executeCell.mockResolvedValue({
      data: { execution_id: 5, status: 'queued', cell_id: 'c1', timeout_seconds: 120 },
    })
    experimentsAPI.getExecution
      .mockResolvedValueOnce({ data: { status: 'running', cell_id: 'c1' } })
      .mockResolvedValueOnce({
        data: {
          status: 'succeeded', cell_id: 'c1', execution_time_ms: 12, execution_count: 1,
          outputs: [{ msg_type: 'stream', content: { text: 'ok' } }],
        },
      })

    const running = store.executeCell('c1', false)
    await vi.advanceTimersByTimeAsync(0)

    expect(experimentsAPI.executeCell).toHaveBeenCalledTimes(1)
    const requestId = experimentsAPI.executeCell.mock.calls[0][3]
    expect(requestId).toBeTruthy()
    expect(store.executingCellId).toBe('c1')
    expect(store.cells[0].isRunning).toBe(true)

    await vi.advanceTimersByTimeAsync(2000)
    await running

    expect(experimentsAPI.getExecution).toHaveBeenCalledTimes(2)
    expect(store.cells[0].outputs.outputs[0].content.text).toBe('ok')
    expect(store.cells[0].outputs.execution_count).toBe(1)
    expect(store.cells[0].isRunning).toBe(false)
    expect(store.executingCellId).toBeNull()
    expect(store.executionNotices.c1).toBeUndefined()
  })

  it('keeps the idempotency key across a network failure so a retry cannot duplicate work', async () => {
    const store = await openStore()
    experimentsAPI.executeCell.mockRejectedValueOnce(new Error('network down'))

    await store.executeCell('c1', false)
    expect(experimentsAPI.executeCell).toHaveBeenCalledTimes(1)
    const firstKey = experimentsAPI.executeCell.mock.calls[0][3]

    experimentsAPI.executeCell.mockResolvedValue({
      data: { execution_id: 6, status: 'queued', cell_id: 'c1', timeout_seconds: 120 },
    })
    experimentsAPI.getExecution.mockResolvedValueOnce({
      data: { status: 'cancelled', cell_id: 'c1', timeout_seconds: 120 },
    })
    const retry = store.executeCell('c1', false)
    await vi.advanceTimersByTimeAsync(0)
    await retry

    expect(experimentsAPI.executeCell.mock.calls[1][3]).toBe(firstKey)
  })

  it('reports a timeout as an interrupted run with variables preserved', async () => {
    const store = await openStore()
    experimentsAPI.executeCell.mockResolvedValue({
      data: { execution_id: 9, status: 'queued', cell_id: 'c1', timeout_seconds: 120 },
    })
    experimentsAPI.getExecution.mockResolvedValueOnce({
      data: {
        status: 'timeout', cell_id: 'c1', timeout_seconds: 120, state_lost: false,
        outputs: [{ msg_type: 'stream', content: { text: '1/100' } }], execution_time_ms: 120000,
      },
    })

    const running = store.executeCell('c1', false)
    await vi.advanceTimersByTimeAsync(0)
    await running

    expect(store.executionNotices.c1.level).toBe('warning')
    expect(store.executionNotices.c1.message).toContain('已中断本次执行')
    expect(store.executionNotices.c1.message).toContain('内存变量已保留')
    expect(store.kernelStateLost).toBe(false)
    expect(store.cells[0].outputs.outputs[0].content.text).toBe('1/100')
  })

  it('flags a rebuilt kernel as state lost and asks the student to rerun from scratch', async () => {
    const store = await openStore()
    experimentsAPI.executeCell.mockResolvedValue({
      data: { execution_id: 11, status: 'queued', cell_id: 'c1', timeout_seconds: 120 },
    })
    experimentsAPI.getExecution.mockResolvedValueOnce({
      data: {
        status: 'failed', cell_id: 'c1', state_lost: true, error_code: 'KERNEL_UNRESPONSIVE',
        error_message: '内核无响应，已重建：内存中的变量、导入与模型状态已丢失，请从头重新运行全部单元格',
        outputs: [],
      },
    })

    const running = store.executeCell('c1', false)
    await vi.advanceTimersByTimeAsync(0)
    await running

    expect(store.kernelStateLost).toBe(true)
    expect(store.executionNotices.c1.level).toBe('error')
    expect(store.executionNotices.c1.message).toContain('请从头重新运行全部单元格')
    expect(store.cells[0].outputs).toBeNull()
  })

  it('resumes polling for an execution that was still running when the page was reloaded', async () => {
    const store = await openStore()
    experimentsAPI.getActiveExecution.mockResolvedValue({
      data: { execution: { id: 21, cell_id: 'c1', status: 'running', timeout_seconds: 120 } },
    })
    experimentsAPI.getExecution.mockResolvedValueOnce({
      data: {
        status: 'succeeded', cell_id: 'c1', execution_count: 3, execution_time_ms: 50,
        outputs: [{ msg_type: 'stream', content: { text: 'resumed' } }],
      },
    })

    const resumed = store.resumeActiveExecution()
    await vi.advanceTimersByTimeAsync(0)
    await resumed

    expect(experimentsAPI.getActiveExecution).toHaveBeenCalledWith(101)
    expect(store.cells[0].outputs.outputs[0].content.text).toBe('resumed')
    expect(store.executingCellId).toBeNull()
  })

  it('cancels the active execution instead of the kernel when one is running', async () => {
    const store = await openStore()
    experimentsAPI.executeCell.mockResolvedValue({
      data: { execution_id: 31, status: 'queued', cell_id: 'c1', timeout_seconds: 120 },
    })
    experimentsAPI.getExecution
      .mockResolvedValueOnce({ data: { status: 'running', cell_id: 'c1' } })
      .mockResolvedValueOnce({ data: { status: 'cancelled', cell_id: 'c1' } })
    experimentsAPI.cancelExecution.mockResolvedValue({ data: { id: 31, status: 'cancelled' } })

    const running = store.executeCell('c1', false)
    await vi.advanceTimersByTimeAsync(0)
    await store.cancelExecution()

    expect(experimentsAPI.cancelExecution).toHaveBeenCalledWith(101, 31)
    expect(experimentsAPI.interrupt).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(2000)
    await running
    expect(store.executionNotices.c1.message).toBe('已中断本次执行')
  })

  it('falls back to the kernel interrupt when no execution is tracked', async () => {
    const store = await openStore()
    experimentsAPI.interrupt.mockResolvedValue({ data: { status: 'interrupted' } })

    await store.cancelExecution()

    expect(experimentsAPI.interrupt).toHaveBeenCalledWith(101)
  })
})
