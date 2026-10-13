import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { studioAPI } from '../api/studio.js'
import { experimentsAPI } from '../api/experiments.js'
import { useAppStore } from './app.js'

export const useStudioStore = defineStore('studio', () => {
  const app = useAppStore()

  const templateId = ref(null)
  const name = ref('')
  const description = ref('')
  const status = ref('draft')
  const draftRevision = ref(1)
  const draftRevisionSaved = ref(1)
  const cells = ref([])
  const currentVersionId = ref(null)
  const lessonId = ref(null)
  const moduleId = ref(null)
  const ownerId = ref(null)
  // ── 草稿环境绑定（Phase 4：教师选择） ─────────────────────
  const environmentVersionId = ref(null)
  const importPolicyMode = ref('unrestricted')
  const allowedImports = ref([])
  // ── 运行配置（Q1：发布前配置、发布后锁定） ────────────────
  // 单格超时秒数 10–1800；内核空闲回收秒数 300–7200（UI 按分钟输入）
  const cellTimeoutSeconds = ref(120)
  const kernelIdleSeconds = ref(900)

  const dirty = ref(false)
  const saving = ref(false)
  const saved = ref(false)
  const conflict = ref(false)
  const conflictMessage = ref('')
  const error = ref(null)
  const studentPreview = ref(false)
  const executingCellId = ref(null)
  const runningCellId = ref(null)

  const sortedCells = computed(() => {
    return [...cells.value].sort((a, b) => a.order - b.order)
  })

  function _genId() {
    return 'cell-' + crypto.randomUUID().slice(0, 8)
  }

  // 加载
  async function open(id) {
    templateId.value = id
    const res = await studioAPI.getTemplate(id)
    const d = res.data
    name.value = d.name
    description.value = d.description || ''
    status.value = d.status
    draftRevision.value = d.draft_revision
    draftRevisionSaved.value = d.draft_revision
    cells.value = (d.draft_cells || []).map(c => ({ ...c })).sort((a, b) => a.order - b.order)
    currentVersionId.value = d.current_version_id
    lessonId.value = d.lesson_id
    moduleId.value = d.module_id
    ownerId.value = d.owner_id
    environmentVersionId.value = d.draft_environment_version_id ?? null
    importPolicyMode.value = d.draft_import_policy_mode || 'unrestricted'
    allowedImports.value = [...(d.draft_allowed_imports || [])]
    cellTimeoutSeconds.value = d.draft_cell_timeout_seconds ?? 120
    kernelIdleSeconds.value = d.draft_kernel_idle_seconds ?? 900
    dirty.value = false
    saved.value = true
    conflict.value = false
    error.value = null
    studentPreview.value = false
  }

  // 空白创建（Phase 4：携带草稿环境三件套）
  async function create(payload) {
    const res = await studioAPI.createTemplate(payload)
    await open(res.data.id)
    return res.data
  }

  // 导入创建（Phase 4：环境字段以 FormData 传输）
  async function importNew(name, desc, file, lessonIdVal, moduleIdVal, env) {
    const fd = new FormData()
    fd.append('name', name)
    if (desc) fd.append('description', desc)
    if (lessonIdVal) fd.append('lesson_id', String(lessonIdVal))
    if (moduleIdVal) fd.append('module_id', String(moduleIdVal))
    if (env) {
      if (env.environment_version_id) fd.append('environment_version_id', String(env.environment_version_id))
      if (env.import_policy_mode) fd.append('import_policy_mode', env.import_policy_mode)
      if (env.allowed_imports?.length) fd.append('allowed_imports_json', JSON.stringify(env.allowed_imports))
    }
    fd.append('file', file)
    const res = await studioAPI.importNew(fd)
    await open(res.data.id)
    return res.data
  }

  // 导入已有模板
  async function importExisting(file) {
    const fd = new FormData()
    fd.append('draft_revision', String(draftRevision.value))
    fd.append('file', file)
    const res = await studioAPI.importExisting(templateId.value, fd)
    _updateFromRead(res.data)
    return res.data
  }

  // 更新元数据
  async function updateMetadata(payload) {
    const res = await studioAPI.updateTemplate(templateId.value, payload)
    name.value = res.data.name
    description.value = res.data.description || ''
  }

  // 绑定
  async function bind(payload) {
    const res = await studioAPI.bindTemplate(templateId.value, payload)
    lessonId.value = res.data.lesson_id
    moduleId.value = res.data.module_id
  }

  // Cell 操作
  function addCell(type, afterCellId) {
    const arr = [...sortedCells.value]
    const anchorIndex = afterCellId == null ? -1 : arr.findIndex(c => c.id === afterCellId)
    const insertIndex = anchorIndex >= 0 ? anchorIndex + 1 : arr.length
    const id = _genId()
    const newCell = {
      id,
      type,
      source: '',
      order: insertIndex,
      student_editable: type === 'code',
      source_hidden: false,
    }
    arr.splice(insertIndex, 0, newCell)
    cells.value = rebaseOrders(arr)
    dirty.value = true
    saved.value = false
  }

  function duplicateCell(cellId) {
    const arr = [...sortedCells.value]
    const idx = arr.findIndex(c => c.id === cellId)
    if (idx < 0) return
    const src = arr[idx]
    const newCell = {
      ...src,
      id: _genId(),
      order: idx + 1,
    }
    arr.splice(idx + 1, 0, newCell)
    cells.value = rebaseOrders(arr)
    dirty.value = true
    saved.value = false
  }

  function deleteCell(cellId) {
    cells.value = rebaseOrders(cells.value.filter(c => c.id !== cellId))
    dirty.value = true
    saved.value = false
  }

  function moveCell(cellId, direction) {
    // 基于按 order 排序后的视图操作（此前在原始数组上交换会被 rebaseOrders 按 order 排序抵消，导致移动无效）
    const sorted = sortedCells.value
    const idx = sorted.findIndex(c => c.id === cellId)
    if (idx < 0) return
    const target = direction === 'up' ? idx - 1 : idx + 1
    if (target < 0 || target >= sorted.length) return
    const arr = [...sorted]
    ;[arr[idx], arr[target]] = [arr[target], arr[idx]]
    cells.value = rebaseOrders(arr)
    dirty.value = true
    saved.value = false
  }

  /** 拖拽排序：把 cell 移动到 targetIndex 位置（排序后视图下标；targetIndex === length 表示末尾） */
  function moveCellTo(cellId, targetIndex) {
    const sorted = sortedCells.value
    const idx = sorted.findIndex(c => c.id === cellId)
    if (idx < 0) return
    // 原地（含拖到自身紧邻后方，splice 后实际位置不变）
    if (targetIndex === idx || targetIndex === idx + 1) return
    if (targetIndex < 0 || targetIndex > sorted.length) return
    const arr = [...sorted]
    const [cell] = arr.splice(idx, 1)
    arr.splice(targetIndex, 0, cell)
    cells.value = rebaseOrders(arr)
    dirty.value = true
    saved.value = false
  }

  function updateCellSource(cellId, source) {
    const cell = cells.value.find(c => c.id === cellId)
    if (!cell) return
    cell.source = source
    dirty.value = true
    saved.value = false
  }

  function setCellEditable(cellId, val) {
    const cell = cells.value.find(c => c.id === cellId)
    if (!cell || !['code', 'markdown'].includes(cell.type)) return
    cell.student_editable = val
    dirty.value = true
    saved.value = false
  }

  function setCellHidden(cellId, val) {
    const cell = cells.value.find(c => c.id === cellId)
    if (!cell || cell.type !== 'code') return
    cell.source_hidden = val
    dirty.value = true
    saved.value = false
  }

  function rebaseOrders(arr) {
    // 把当前数组顺序固化为 order 0..n-1（数组顺序即意图顺序）
    return arr.map((c, i) => ({ ...c, order: i }))
  }

  // 保存草稿（Phase 4：环境三件套与 cells 同一 revision 提交）
  async function saveDraft() {
    if (conflict.value) return false
    saving.value = true
    error.value = null
    try {
      const payload = {
        draft_revision: draftRevision.value,
        cells: cells.value.map(c => ({
          id: c.id,
          type: c.type,
          source: c.source,
          order: c.order,
          student_editable: c.student_editable !== false,
          source_hidden: c.source_hidden === true,
        })),
        environment_version_id: environmentVersionId.value,
        import_policy_mode: importPolicyMode.value,
        allowed_imports: [...allowedImports.value],
        // 后端 strict=True：必须是 number，字符串会 422
        cell_timeout_seconds: Number(cellTimeoutSeconds.value),
        kernel_idle_seconds: Number(kernelIdleSeconds.value),
      }
      const res = await studioAPI.saveDraft(templateId.value, payload)
      _updateFromRead(res.data)
      dirty.value = false
      saved.value = true
      return true
    } catch (e) {
      const code = e.response?.data?.detail?.code
      if (code === 'REVISION_CONFLICT') {
        conflict.value = true
        conflictMessage.value = e.response?.data?.detail?.message || '草稿已被其他会话修改，请刷新后重试'
      } else {
        error.value = { code: code || 'SAVE_FAILED', message: e.response?.data?.detail?.message || '保存失败' }
      }
      saved.value = false
      return false
    } finally {
      saving.value = false
    }
  }

  // 发布
  async function publish() {
    if (dirty.value) {
      const ok = await saveDraft()
      if (!ok) return null
    }
    saving.value = true
    try {
      const res = await studioAPI.publish(templateId.value)
      status.value = 'published'
      currentVersionId.value = res.data.id
      // 实验模板：模板版本发布成功后，显式发布绑定的实验模块。
      // 模块状态变更只能走 /experiments/modules/{id}/publish（后端发布门禁保留），
      // 通用 studio_service.publish_template 不做模块联动。
      if (moduleId.value == null) {
        app.showToast(`已发布版本 ${res.data.version_number}`, 'success')
        return res.data
      }
      try {
        await experimentsAPI.publishModule(moduleId.value)
        app.showToast('实验发布成功', 'success')
      } catch (e) {
        // 部分成功：模板版本已发布、模块仍为草稿。明确报错，避免假成功提示；
        // 模块发布可在实验列表页重试（此时模板版本已就绪，门禁可通过）。
        const detail = e.response?.data?.detail?.message || '实验发布失败'
        error.value = { code: 'MODULE_PUBLISH_FAILED', message: detail }
        app.showToast(`模板版本已发布，但实验发布失败：${detail}，请返回实验列表重试`, 'error')
      }
      return res.data
    } catch (e) {
      error.value = { code: e.response?.data?.detail?.code === 'LAB_ASSESSMENT_PUBLISH_REQUIRED' ? 'LAB_ASSESSMENT_PUBLISH_REQUIRED' : 'PUBLISH_FAILED', message: e.response?.data?.detail?.message || '发布失败' }
      return null
    } finally {
      saving.value = false
    }
  }

  // 导出
  function downloadBlob(resp, filename) {
    const blob = resp.data instanceof Blob ? resp.data : new Blob([resp.data])
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  async function exportDraft() {
    try {
      const res = await studioAPI.exportDraft(templateId.value)
      downloadBlob(res, `template-${templateId.value}-draft.zip`)
    } catch {
      app.showToast('导出失败', 'error')
    }
  }

  async function exportVersion(versionId) {
    try {
      const res = await studioAPI.exportVersion(templateId.value, versionId)
      downloadBlob(res, `template-${templateId.value}-v${versionId}.zip`)
    } catch {
      app.showToast('导出失败', 'error')
    }
  }

  // 预览运行
  async function previewRun(cellId) {
    // 后端 preview_run 只查数据库中的 draft_cells：未保存的本地 cell 后端不可见，
    // 直接运行必然 404。因此有未保存修改时先保存草稿，再按 id 发起运行。
    if (dirty.value) {
      const ok = await saveDraft()
      if (!ok) return null
    }
    if (!cellId) {
      app.showToast('请先运行某个代码 Cell', 'error')
      return null
    }
    runningCellId.value = cellId
    try {
      const res = await studioAPI.previewRun(templateId.value, { cell_id: cellId })
      const cell = cells.value.find(c => c.id === cellId)
      if (cell) {
        cell._previewOutputs = {
          outputs: res.data.outputs,
          execution_time_ms: res.data.execution_time_ms,
        }
      }
      return res.data
    } catch (e) {
      const code = e.response?.data?.detail?.code
      if (code === 'KERNEL_INIT_FAILED') {
        app.showToast('隐藏 Cell 初始化失败，Kernel 已销毁', 'error')
      } else {
        app.showToast('预览执行失败', 'error')
      }
    } finally {
      runningCellId.value = null
    }
  }

  async function previewInterrupt() {
    try { await studioAPI.previewInterrupt(templateId.value) }
    catch { /* ignore */ }
  }

  async function previewReset() {
    try {
      await studioAPI.previewReset(templateId.value)
      cells.value.forEach(c => { c._previewOutputs = null })
    } catch { /* ignore */ }
  }

  function _updateFromRead(data) {
    if (data.draft_revision != null) {
      draftRevision.value = data.draft_revision
      draftRevisionSaved.value = data.draft_revision
    }
    if (data.draft_cells) cells.value = data.draft_cells.map(c => ({ ...c })).sort((a, b) => a.order - b.order)
    if (data.current_version_id != null) currentVersionId.value = data.current_version_id
    status.value = data.status || 'draft'
    name.value = data.name
    description.value = data.description || ''
    if ('draft_environment_version_id' in data) {
      environmentVersionId.value = data.draft_environment_version_id ?? null
      importPolicyMode.value = data.draft_import_policy_mode || 'unrestricted'
      allowedImports.value = [...(data.draft_allowed_imports || [])]
    }
    if (data.draft_cell_timeout_seconds != null) cellTimeoutSeconds.value = data.draft_cell_timeout_seconds
    if (data.draft_kernel_idle_seconds != null) kernelIdleSeconds.value = data.draft_kernel_idle_seconds
  }

  // ── 环境设置（Phase 4）：修改后纳入 dirty，随草稿保存 ──────
  function setEnvironment(envId, mode = null, allowed = null) {
    environmentVersionId.value = envId ?? null
    if (mode !== null) importPolicyMode.value = mode
    if (allowed !== null) allowedImports.value = [...allowed]
    dirty.value = true
    saved.value = false
  }

  // ── 运行配置（Q1）：修改后纳入 dirty，随草稿保存并在发布时冻结 ──
  function setKernelConfig({ cellTimeoutSeconds: timeout, kernelIdleSeconds: idle } = {}) {
    if (timeout != null) cellTimeoutSeconds.value = Number(timeout)
    if (idle != null) kernelIdleSeconds.value = Number(idle)
    dirty.value = true
    saved.value = false
  }

  function setImportPolicy(mode, allowed = []) {
    importPolicyMode.value = mode
    allowedImports.value = [...allowed]
    dirty.value = true
    saved.value = false
  }

  function destroy() {
    cells.value.forEach(c => { c._previewOutputs = null })
  }

  return {
    templateId, name, description, status, draftRevision, draftRevisionSaved,
    cells, sortedCells, currentVersionId, lessonId, moduleId, ownerId,
    environmentVersionId, importPolicyMode, allowedImports,
    cellTimeoutSeconds, kernelIdleSeconds,
    dirty, saving, saved, conflict, conflictMessage, error, studentPreview,
    executingCellId, runningCellId,
    open, create, importNew, importExisting, updateMetadata, bind,
    addCell, duplicateCell, deleteCell, moveCell, moveCellTo,
    updateCellSource, setCellEditable, setCellHidden,
    setEnvironment, setImportPolicy, setKernelConfig,
    saveDraft, publish, exportDraft, exportVersion,
    previewRun, previewInterrupt, previewReset,
    destroy,
  }
})
