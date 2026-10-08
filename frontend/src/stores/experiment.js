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
        outputs: c.outputs || null, isRunning: false,
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

  async function executeCell(cellId, checkFeedback = true) {
    const cell = cells.value.find(c => c.id === cellId)
    if (!cell || cell.type !== 'code') return
    executingCellId.value = cellId
    try {
      const res = await experimentsAPI.executeCell(recordId.value, cellId, cell.source)
      cell.outputs = {
        outputs: res.data.outputs,
        execution_time_ms: res.data.execution_time_ms,
        execution_count: res.data.execution_count,
      }
      if (checkFeedback && cellCheckTargets.value.includes(cellId)) await checkCell(cellId)
    } catch (e) {
      const code = e.response?.data?.detail?.code
      const message = e.response?.data?.detail?.message
      if (code === 'IMPORT_NOT_ALLOWED' || code === 'IMPORT_NOT_INSTALLED' || code === 'ENVIRONMENT_IMAGE_MISSING') {
        _writeImportDiagnosticOutput(cell, message || '导入受限，请检查代码')
      } else if (e.response?.status === 409) app.showToast('Kernel 正忙，请等待', 'warning')
      else app.showToast('执行失败', 'error')
    } finally {
      executingCellId.value = null
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

  async function restartKernel() {
    try {
      await experimentsAPI.restart(recordId.value)
      for (const c of cells.value) { if (c.type === 'code') c.outputs = null }
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
  }

  return {
    context, recordId, recordRevision, cells, entryName, entryDescription,
    environmentSummary, labEnabled, cellCheckTargets, cellCheckResults,
    dirty, saving, saved, error, conflict, executingCellId,
    submitting, lastSubmitTime, submitAttemptCount, submissions, currentClientRequestId,
    openLesson, openModule,
    updateCellSource, flushSave, canNavigate,
    executeCell, executeAllCells, interruptKernel, restartKernel,
    submitExperiment, loadSubmissions,
    destroy,
  }
})
