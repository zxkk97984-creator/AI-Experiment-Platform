import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
vi.mock('../../api/experiments.js', () => ({ experimentsAPI: {
  executeCell: vi.fn(), getExecution: vi.fn(), getActiveExecution: vi.fn(), cancelExecution: vi.fn(),
  saveCells: vi.fn(), submitRecord: vi.fn(), interrupt: vi.fn(), restart: vi.fn(),
} }))
vi.mock('../../api/labs.js', () => ({ labsAPI: { checkCell: vi.fn(), getRun: vi.fn() } }))
import { experimentsAPI } from '../../api/experiments.js'
import { labsAPI } from '../../api/labs.js'
import { useExperimentStore } from '../experiment.js'
const outcome = { data: { status: 'succeeded', stale: false, result: { cell_checks: { p: { passed: true } } } } }
function setup() {
  setActivePinia(createPinia())
  const store = useExperimentStore()
  store.recordId = 8
  store.recordRevision = 3
  store.cells = [{ id: 'code', type: 'code', source: 'result=7', student_editable: true }]
  store.cellCheckTargets = ['code']
  // 异步执行契约：POST 只受理（202 + execution_id），结果由 GET executions/{id} 轮询取得
  experimentsAPI.executeCell.mockResolvedValue({ data: { execution_id: 5, status: 'queued', cell_id: 'code', timeout_seconds: 120 } })
  experimentsAPI.getExecution.mockResolvedValue({ data: { id: 5, cell_id: 'code', status: 'succeeded',
    outputs: [], execution_time_ms: 5, execution_count: 1, state_lost: false, timed_out: false } })
  labsAPI.checkCell.mockResolvedValue({ data: { id: 9 } })
  labsAPI.getRun.mockResolvedValue(outcome)
  return store
}
describe('Cell 练习反馈与源码版本', () => {
  beforeEach(() => vi.resetAllMocks())
  it('checks the saved revision after execution without submitting a grade', async () => {
    const store = setup()
    await store.executeCell('code')
    await flushPromises()
    // 新契约：受理带幂等键，结果来自执行状态查询
    expect(experimentsAPI.executeCell).toHaveBeenCalledWith(8, 'code', 'result=7', expect.any(String))
    expect(experimentsAPI.getExecution).toHaveBeenCalledWith(8, 5)
    expect(labsAPI.checkCell).toHaveBeenCalledWith(8, 'code', expect.objectContaining({ expected_record_revision: 3 }))
    expect(store.cellCheckResults.code.state).toBe('passed')
    expect(experimentsAPI.submitRecord).not.toHaveBeenCalled()
    store.destroy()
  })
  it('invalidates feedback on an edit and rejects an older asynchronous result', async () => {
    const store = setup()
    let deliver
    labsAPI.getRun.mockImplementation(() => new Promise(resolve => { deliver = resolve }))
    await store.executeCell('code')
    store.updateCellSource('code', 'result=8')
    deliver(outcome)
    await flushPromises()
    expect(store.cellCheckResults.code.state).toBe('stale')
    store.destroy()
  })
  it('runs all cells with one combined check job and independent feedback for each cell', async () => {
    const store = setup()
    store.cells.push({ id: 'second', type: 'code', source: 'other=3', student_editable: true })
    store.cellCheckTargets = ['code', 'second']
    labsAPI.getRun.mockResolvedValue({ data: { status: 'succeeded', stale: false, result: { cell_checks: {
      a: { cell_id: 'code', passed: true }, b: { cell_id: 'second', passed: false },
    } } } })
    await store.executeAllCells()
    await flushPromises()
    expect(experimentsAPI.executeCell).toHaveBeenCalledTimes(2)
    expect(labsAPI.checkCell).toHaveBeenCalledTimes(1)
    expect(labsAPI.checkCell).toHaveBeenCalledWith(8, 'second', expect.objectContaining({ all_cells: true }))
    expect(store.cellCheckResults.code.state).toBe('passed')
    expect(store.cellCheckResults.second.state).toBe('failed')
    store.destroy()
  })
})
