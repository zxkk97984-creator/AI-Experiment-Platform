import { ref } from 'vue'
import { defineStore } from 'pinia'
import { experimentsAPI } from '../api/experiments.js'
import { labsAPI } from '../api/labs.js'
import { useAppStore } from './app.js'

export const useExperimentStore = defineStore('experiment', () => {
  const app = useAppStore()

  const context = ref(null)
  const recordId = ref(null)
  const recordRevision = ref(0)
  const cells = ref([])
  const entryName = ref('')
  const entryDescription = ref('')
  // Phase 5：学生可见环境摘要（NotebookPlayer 标题下提示；不含 digest/tag）
  const environmentSummary = ref(null)
  const labEnabled = ref(false)
  const cellCheckTargets = ref([])
  const cellCheckResults = ref({})
  const cellCheckTimers = new Map()

  const dirty = ref(false)
  const saving = ref(false)
  const saved = ref(false)
  const error = ref(null)
  const conflict = ref(false)
  const executingCellId = ref(null)

  // ── 异步执行状态 ──
  // 单格最长可配 1800 秒：轮询上限必须覆盖它（2000ms × 960 ≈ 1920s），否则长任务会被前端
  // 提前放弃、学生看不到终态。这里沿用 pollCellCheck 的递归 setTimeout 范式。
  const EXEC_POLL_INTERVAL_MS = 2000
  const EXEC_POLL_MAX_ATTEMPTS = 960
  const executionNotices = ref({})
  const activeExecutionId = ref(null)
  const kernelStateLost = ref(false)
  const executionTimers = new Map()
  const executionResolvers = new Map()
  // 网络失败时保留 client_request_id，重试同一份代码不会创建第二个任务（后端唯一约束兜底）
  const pendingExecutionRequests = new Map()

  // ── 提交状态 ──
  const submitting = ref(false)
  const lastSubmitTime = ref(null)
  const submitAttemptCount = ref(0)
  const submissions = ref([])
  const currentClientRequestId = ref(null)
  let currentSubmissionKey = null

  const dirtySources = ref({})

  let debounceTimer = null
  let safetyInterval = null
  let pendingFlush = null

  // ── safety ──
  function _startSafety() {
    _stopSafety()
    safetyInterval = setInterval(() => {
      if (dirty.value && !conflict.value && !saving.value) {
        flushSave()
      }
    }, 30000)
  }

  function _stopSafety() {
    if (safetyInterval) { clearInterval(safetyInterval); safetyInterval = null }
  }

  // ── 加载 ──
  async function openLesson(lessonId, courseId, offeringId = null) {
    context.value = { type: 'lesson', id: lessonId, courseId, offeringId, returnPath: `/student/courses/${courseId}`, title: null }
    await _load(lessonId, null)
  }

  async function openModule(moduleId, offeringId = null) {
    context.value = { type: 'module', id: moduleId, offeringId, returnPath: '/student/experiments', title: null }
    await _load(null, moduleId)
  }

  async function _load(lessonId, moduleId) {
    clearTimeout(debounceTimer)
    _stopSafety()
    if (pendingFlush) {
      await pendingFlush
    }

    error.value = null
    conflict.value = false
    dirty.value = false
    saved.value = false
    dirtySources.value = {}
    // 重置执行相关状态，避免串页（在途任务由 resumeActiveExecution 重新接上）
    _clearExecutionState()
    pendingExecutionRequests.clear()
    executionNotices.value = {}
    kernelStateLost.value = false
    // 重置提交相关状态，避免串页
    submissions.value = []
    submitAttemptCount.value = 0
    lastSubmitTime.value = null
    currentClientRequestId.value = null
    submitting.value = false

    try {
      const ensRes = lessonId != null
        ? await (context.value?.offeringId ? experimentsAPI.ensureForLesson(lessonId, context.value.offeringId) : experimentsAPI.ensureForLesson(lessonId))
        : await (context.value?.offeringId ? experimentsAPI.ensureForModule(moduleId, context.value.offeringId) : experimentsAPI.ensureForModule(moduleId))
      recordId.value = ensRes.data.id
      recordRevision.value = ensRes.data.record_revision

      const detail = await experimentsAPI.getRecordDetail(recordId.value)
      const d = detail.data
      const raw = (d.cells || []).map(c => ({
        id: c.id, type: c.type || 'code', source: c.source || '',
        order: c.order || 0,
        student_editable: c.student_editable == null ? c.type !== 'markdown' : c.student_editable === true,
        outputs: c.outputs || null, isRunning: c.is_running === true,
      }))
      raw.sort((a, b) => a.order - b.order)
      cells.value = raw
      entryName.value = d.entry_name || ''
      entryDescription.value = d.entry_description || ''
      environmentSummary.value = d.environment_summary || null
      labEnabled.value = Boolean(d.lab_enabled)
      if (context.value) context.value.title = entryName.value
      if (d.record_revision != null) recordRevision.value = d.record_revision
      saved.value = true
      _startSafety()
      // 刷新恢复：页面重新打开时接上仍在跑的单元格（不阻塞首屏渲染）
      resumeActiveExecution()
    } catch (e) {
      const code = e.response?.data?.detail?.code
      const message = e.response?.data?.detail?.message
      if (e.response?.status === 404) {
        error.value = { code: code || 'NOT_FOUND', message: message || '实验资源不存在或已被删除' }
      } else if (e.response?.status === 403) {
        error.value = { code: code || 'FORBIDDEN', message: message || '无权访问该实验' }
      } else {
        error.value = { code: code || 'LOAD_FAILED', message: message || '加载实验失败，请稍后重试' }
      }
      throw error.value
    }
  }

  // ── 编辑 ──
  function updateCellSource(cellId, source) {
    const cell = cells.value.find(c => c.id === cellId)
    if (!cell || !cell.student_editable) return
    cell.source = source
    for (const id of Object.keys(cellCheckResults.value)) cellCheckResults.value[id] = { state: 'stale', message: '代码已修改，请重新运行检查' }
    for (const timer of cellCheckTimers.values()) clearTimeout(timer)
    cellCheckTimers.clear()
    dirtySources.value = { ...dirtySources.value, [cellId]: source }
    dirty.value = true
    saved.value = false
    // conflict 不清除——只有 reload 才能清除
    if (!conflict.value) {
      error.value = null
      scheduleDebounce()
    }
  }

  function scheduleDebounce() {
    clearTimeout(debounceTimer)
    if (conflict.value) return
    debounceTimer = setTimeout(() => flushSave(), 1200)
  }

  // ── 串行保存 ──
  async function _drainDirtySources() {
    saving.value = true
    saved.value = false

    try {
      while (Object.keys(dirtySources.value).length > 0) {
        const sourcesToSave = { ...dirtySources.value }
        const revisionToSave = recordRevision.value

        try {
          const res = await experimentsAPI.saveCells(
            recordId.value,
            sourcesToSave,
            revisionToSave,
          )
          recordRevision.value = res.data.record_revision
        } catch (e) {
          const code = e.response?.data?.detail?.code
          if (code === 'REVISION_CONFLICT') {
            conflict.value = true
            error.value = {
              code: 'REVISION_CONFLICT',
              message: '记录已被他人修改，请刷新页面后重试',
            }
          } else {
            error.value = {
              code: code || 'SAVE_FAILED',
              message: e.response?.data?.detail?.message || '保存失败',
            }
          }
          dirty.value = true
          saved.value = false
          return false
        }

        const remaining = { ...dirtySources.value }
        for (const [cellId, source] of Object.entries(sourcesToSave)) {
          if (remaining[cellId] === source) {
            delete remaining[cellId]
          }
        }
        dirtySources.value = remaining
        dirty.value = Object.keys(remaining).length > 0
        saved.value = !dirty.value
        error.value = null
      }

      dirty.value = false
      saved.value = true
      return true
    } finally {
      saving.value = false
    }
  }

  function flushSave() {
    clearTimeout(debounceTimer)
    if (!recordId.value || (!dirty.value && !pendingFlush)) {
      return Promise.resolve(true)
    }
    if (conflict.value) {
      return Promise.resolve(false)
    }
    if (pendingFlush) {
      return pendingFlush
    }

    const trackedDrain = _drainDirtySources().finally(() => {
      if (pendingFlush === trackedDrain) {
        pendingFlush = null
      }
    })
    pendingFlush = trackedDrain
    return trackedDrain
  }

  // ── canNavigate 供路由守卫使用 ──
  async function canNavigate() {
    let savedSuccessfully = true
    if (dirty.value || saving.value || pendingFlush) {
      savedSuccessfully = await flushSave()
    }
    if (!savedSuccessfully || conflict.value) {
      return window.confirm(
        '保存失败（' + (error.value?.message || '未知错误') +
        '），确定离开吗？未保存的修改将丢失。',
      )
    }
    return true
  }

  // ── 执行 ──
  // Phase 5：import 类 API 错误（IMPORT_NOT_ALLOWED → 422 / IMPORT_NOT_INSTALLED → 500）
  // 捕获后为当前 cell 写入无裸 traceback 的合成 error output（计划 12 Notebook）
  function _writeImportDiagnosticOutput(cell, message) {
    cell.outputs = {
      outputs: [{ msg_type: 'error', content: { text: message } }],
      execution_time_ms: null,
      execution_count: (cell.outputs?.execution_count ?? 0) + 1,
    }
  }

  const TERMINAL_EXECUTION_STATUSES = ['succeeded', 'timeout', 'failed', 'cancelled']

  function _setExecutionNotice(cellId, notice) {
    const next = { ...executionNotices.value }
    if (notice) next[cellId] = notice
    else delete next[cellId]
    executionNotices.value = next
  }

  function _clearExecutionTimer(cellId) {
    const timer = executionTimers.get(cellId)
    if (timer) {
      clearTimeout(timer)
      executionTimers.delete(cellId)
    }
  }

  function _releaseExecutionWaiters(cellId) {
    const resolve = executionResolvers.get(cellId)
    if (resolve) {
      executionResolvers.delete(cellId)
      resolve(null)
    }
  }

  function _clearExecutionState() {
    for (const cellId of [...executionTimers.keys()]) _clearExecutionTimer(cellId)
    for (const cellId of [...executionResolvers.keys()]) _releaseExecutionWaiters(cellId)
    for (const cell of cells.value) cell.isRunning = false
    executingCellId.value = null
    activeExecutionId.value = null
  }

  function _terminalNotice(data) {
    if (data.status === 'timeout') {
      return { level: 'warning', message: `单元格执行超过 ${data.timeout_seconds} 秒，已中断本次执行；内存变量已保留` }
    }
    if (data.status === 'failed') {
      if (data.state_lost) {
        return { level: 'error', message: '内核无响应，已重建：内存中的变量、导入与模型状态已丢失，请从头重新运行全部单元格' }
      }
      return { level: 'error', message: data.error_message || '执行失败' }
    }
    if (data.status === 'cancelled') return { level: 'info', message: '已中断本次执行' }
    return null
  }

  function _applyExecutionResult(cell, data) {
    const notice = _terminalNotice(data)
    if (notice) _setExecutionNotice(cell.id, notice)
    if (data.state_lost) kernelStateLost.value = true
    // failed/cancelled 可能是空输出：不要用它覆盖上一次的真实输出
    if (data.status === 'succeeded' || data.status === 'timeout') {
      cell.outputs = {
        outputs: data.outputs || [],
        execution_time_ms: data.execution_time_ms,
        execution_count: data.execution_count,
      }
    }
  }

  /** 轮询一次执行直到终态；返回终态数据（被取消/失败返回 null） */
  function pollExecution(executionId, cellId, execRecordId) {
    return new Promise((resolve) => {
      const tick = async (attempts) => {
        if (recordId.value !== execRecordId) { resolve(null); return }
        const cell = cells.value.find(c => c.id === cellId)
        if (!cell) { resolve(null); return }
        try {
          const { data } = await experimentsAPI.getExecution(execRecordId, executionId)
          if (recordId.value !== execRecordId) { resolve(null); return }
          if (TERMINAL_EXECUTION_STATUSES.includes(data.status)) {
            _clearExecutionTimer(cellId)
            _applyExecutionResult(cell, data)
            cell.isRunning = false
            executingCellId.value = null
            activeExecutionId.value = null
            resolve(data)
            return
          }
          cell.isRunning = true
          executingCellId.value = cellId
          activeExecutionId.value = executionId
          if (attempts >= EXEC_POLL_MAX_ATTEMPTS) {
            _setExecutionNotice(cellId, { level: 'warning', message: '执行时间超出预期，请刷新页面查看最新状态' })
            executingCellId.value = null
            resolve(null)
            return
          }
          executionResolvers.set(cellId, resolve)
          _clearExecutionTimer(cellId)
          executionTimers.set(cellId, setTimeout(() => tick(attempts + 1), EXEC_POLL_INTERVAL_MS))
        } catch {
          _clearExecutionTimer(cellId)
          executingCellId.value = null
          activeExecutionId.value = null
          _setExecutionNotice(cellId, { level: 'error', message: '执行状态加载失败，请刷新页面重试' })
          resolve(null)
        }
      }
      tick(0)
    })
  }

  async function executeCell(cellId, checkFeedback = true) {
    const cell = cells.value.find(c => c.id === cellId)
    if (!cell || cell.type !== 'code') return
    if (executingCellId.value) return
    executingCellId.value = cellId
    kernelStateLost.value = false
    _setExecutionNotice(cellId, null)
    const execRecordId = recordId.value
    if (!pendingExecutionRequests.has(cellId)) pendingExecutionRequests.set(cellId, crypto.randomUUID())
    const clientRequestId = pendingExecutionRequests.get(cellId)
    try {
      const res = await experimentsAPI.executeCell(execRecordId, cellId, cell.source, clientRequestId)
      pendingExecutionRequests.delete(cellId)
      activeExecutionId.value = res.data.execution_id
      const data = await pollExecution(res.data.execution_id, cellId, execRecordId)
      if (data && data.status === 'succeeded' && checkFeedback && cellCheckTargets.value.includes(cellId)) {
        await checkCell(cellId)
      }
    } catch (e) {
      // 有响应说明请求已被处理（4xx/5xx），下次点击生成新的幂等键；网络失败保留原键重试
      if (e.response) pendingExecutionRequests.delete(cellId)
      const code = e.response?.data?.detail?.code
      const message = e.response?.data?.detail?.message
      if (code === 'IMPORT_NOT_ALLOWED' || code === 'IMPORT_NOT_INSTALLED' || code === 'ENVIRONMENT_IMAGE_MISSING') {
        _writeImportDiagnosticOutput(cell, message || '导入受限，请检查代码')
      } else if (e.response?.status === 409) app.showToast('Kernel 正忙，请等待', 'warning')
      else app.showToast('执行失败', 'error')
    } finally {
      if (executingCellId.value === cellId) executingCellId.value = null
    }
  }

  /** 刷新恢复：页面重新打开时接上仍在执行的单元格并继续轮询 */
  async function resumeActiveExecution() {
    if (!recordId.value) return
    const execRecordId = recordId.value
    try {
      const { data } = await experimentsAPI.getActiveExecution(execRecordId)
      if (recordId.value !== execRecordId) return
      const execution = data?.execution
      if (!execution) return
      const cell = cells.value.find(c => c.id === execution.cell_id)
      if (!cell) return
      executingCellId.value = cell.id
      activeExecutionId.value = execution.id
      cell.isRunning = true
      await pollExecution(execution.id, cell.id, execRecordId)
      if (recordId.value === execRecordId) executingCellId.value = null
    } catch {
      // 刷新恢复失败不阻塞页面；下次运行或再次刷新会重试
    }
  }

  async function checkCell(cellId, allCells = false) {
    const checkedRecord = recordId.value
    if (!(await flushSave()) || dirty.value || conflict.value) {
      cellCheckResults.value[cellId] = { state: 'unavailable', message: '保存后才能检查，请解决保存冲突' }
      return
    }
    const revision = recordRevision.value
    const targets = allCells ? cellCheckTargets.value : [cellId]
    for (const target of targets) cellCheckResults.value[target] = { state: 'checking', message: '正在从头重放并检查…' }
    try {
      const { data } = await labsAPI.checkCell(checkedRecord, cellId, { expected_record_revision: revision, client_request_id: crypto.randomUUID(), all_cells: allCells })
      pollCellCheck(data.id, cellId, checkedRecord, revision, 0, targets)
    } catch (e) {
      for (const target of targets) cellCheckResults.value[target] = { state: 'unavailable', message: e.response?.data?.detail?.message || '检查暂不可用，请稍后重新运行' }
    }
  }

  async function pollCellCheck(runId, cellId, checkedRecord, revision, attempts, targets = [cellId]) {
    if (recordId.value !== checkedRecord || recordRevision.value !== revision || dirty.value) return
    try {
      const { data } = await labsAPI.getRun(runId)
      if (recordId.value !== checkedRecord || recordRevision.value !== revision || dirty.value) return
      if (data.stale) for (const target of targets) cellCheckResults.value[target] = { state: 'stale', message: '作答已变化，请重新运行检查' }
      else if (data.status === 'succeeded') {
        const items = Object.values(data.result?.cell_checks || {})
        for (const target of targets) {
          const marks = targets.length > 1 ? items.filter(item => item.cell_id === target) : items
          const passed = marks.length > 0 && marks.every(item => item.passed === true)
          cellCheckResults.value[target] = { state: passed ? 'passed' : 'failed', message: passed ? '单元格检查通过' : '尚未通过全部评分点，请检查实现' }
        }
      } else if (['queued', 'running', 'recovering'].includes(data.status) && attempts < 180) {
        cellCheckTimers.set(cellId, setTimeout(() => pollCellCheck(runId, cellId, checkedRecord, revision, attempts + 1, targets), 2000))
      } else for (const target of targets) cellCheckResults.value[target] = { state: 'unavailable', message: data.error_message || '检查未完成，可稍后重新运行' }
    } catch { for (const target of targets) cellCheckResults.value[target] = { state: 'unavailable', message: '检查状态加载失败，请重新运行或刷新' } }
  }

  async function executeAllCells() {
    for (const cell of cells.value.filter(c => c.type === 'code')) {
      await executeCell(cell.id, false)
    }
    if (cellCheckTargets.value.length) await checkCell(cellCheckTargets.value.at(-1), true)
  }

  async function interruptKernel() {
    try { await experimentsAPI.interrupt(recordId.value); app.showToast('已中断', 'success') }
    catch { app.showToast('中断失败', 'error') }
  }

  /** 中断：优先取消当前执行任务（排队中直接取消、运行中由 worker 中断），无任务时退回内核中断 */
  async function cancelExecution() {
    const execRecordId = recordId.value
    const executionId = activeExecutionId.value
    if (!executionId) { await interruptKernel(); return }
    try {
      await experimentsAPI.cancelExecution(execRecordId, executionId)
      app.showToast('已中断本次执行', 'success')
    } catch {
      app.showToast('中断失败', 'error')
    }
  }

  async function restartKernel() {
    try {
      _clearExecutionState()
      await experimentsAPI.restart(recordId.value)
      for (const c of cells.value) { if (c.type === 'code') c.outputs = null }
      executionNotices.value = {}
      pendingExecutionRequests.clear()
      kernelStateLost.value = false
      app.showToast('Kernel 已重启', 'success')
    } catch { app.showToast('重启失败', 'error') }
  }

  // ── 提交 ──
  async function submitExperiment(options) {
    if (!recordId.value || submitting.value || conflict.value) return false
    submitting.value = true

    // 先 flush 所有待保存数据
    if (dirty.value || saving.value || pendingFlush) {
      const ok = await flushSave()
      if (!ok) {
        app.showToast('保存失败，请检查网络后重试提交', 'error')
        submitting.value = false
        return false
      }
    }

    const payload = options ? { ...options, expected_record_revision: recordRevision.value } : undefined
    const submissionKey = JSON.stringify([recordId.value, recordRevision.value, payload])
    if (currentSubmissionKey !== submissionKey) { currentClientRequestId.value = null; currentSubmissionKey = submissionKey }
    // 生成/复用 client_request_id
    if (!currentClientRequestId.value) {
      currentClientRequestId.value = crypto.randomUUID()
    }

    submitting.value = true
    try {
      const res = await experimentsAPI.submitRecord(
        recordId.value,
        currentClientRequestId.value,
        ...(payload ? [payload] : []),
      )
      const sub = res.data
      lastSubmitTime.value = new Date().toISOString()
      submitAttemptCount.value = sub.attempt_number

      // 添加/更新提交列表
      const existing = submissions.value.findIndex(s => s.id === sub.id)
      if (existing >= 0) {
        submissions.value[existing] = sub
      } else {
        submissions.value.unshift(sub)
      }

      // 提交成功后清除 request_id，下次提交生成新的
      currentClientRequestId.value = null
      app.showToast(`提交成功（第 ${sub.attempt_number} 次提交）`, 'success')
      return true
    } catch (e) {
      const detail = e.response?.data?.detail
      if (detail?.code === 'ALREADY_GRADED') {
        app.showToast('实验已评分，无法再次提交', 'warning')
      } else {
        // 网络错误时保留 client_request_id 以便重试幂等
        app.showToast(detail?.message || '提交失败，请重试', 'error')
      }
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 加载提交历史 */
  async function loadSubmissions() {
    if (!recordId.value) return
    try {
      const res = await experimentsAPI.listSubmissions({ record_id: recordId.value })
      submissions.value = res.data?.items || []
      if (submissions.value.length > 0) {
        submitAttemptCount.value = submissions.value[0].attempt_number
        lastSubmitTime.value = submissions.value[0].submitted_at
      }
    } catch {
      // 静默失败
    }
  }

  function destroy() {
    clearTimeout(debounceTimer)
    _stopSafety()
    for (const timer of cellCheckTimers.values()) clearTimeout(timer)
    cellCheckTimers.clear()
    cellCheckTargets.value = []
    cellCheckResults.value = {}
    _clearExecutionState()
    executionNotices.value = {}
    kernelStateLost.value = false
  }

  return {
    context, recordId, recordRevision, cells, entryName, entryDescription,
    environmentSummary, labEnabled, cellCheckTargets, cellCheckResults,
    dirty, saving, saved, error, conflict, executingCellId,
    executionNotices, activeExecutionId, kernelStateLost,
    submitting, lastSubmitTime, submitAttemptCount, submissions, currentClientRequestId,
    openLesson, openModule,
    updateCellSource, flushSave, canNavigate,
    executeCell, executeAllCells, interruptKernel, cancelExecution, restartKernel,
    resumeActiveExecution,
    submitExperiment, loadSubmissions,
    destroy,
  }
})
