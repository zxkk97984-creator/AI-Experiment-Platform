<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import CodeCell from './CodeCell.vue'
import MarkdownCell from './MarkdownCell.vue'
import LabAssessmentPanel from '../labs/LabAssessmentPanel.vue'
import AppIcon from '../ui/AppIcon.vue'
import ConfirmDialog from '../ui/ConfirmDialog.vue'
import EnvironmentProfilePicker from '../common/EnvironmentProfilePicker.vue'
import { environmentsAPI } from '../../api/environments.js'
import { useStudioStore } from '../../stores/studio.js'
import { useAppStore } from '../../stores/app.js'

const props = defineProps({
  templateId: { type: [Number, String], required: true },
  /** 返回路径；不传时回退浏览器历史（Studio 返回路径兼容行为） */
  backTo: { type: [String, Object], default: null },
})
const router = useRouter()
const store = useStudioStore()
const app = useAppStore()
const loading = ref(true)
const history = ref([])
const showHistory = ref(false)
const showLabPanel = ref(false)
const labPanelOpened = ref(false)
watch(showLabPanel, (show) => { if (show) labPanelOpened.value = true })
const labDirty = ref(false)
const labConfigured = ref(false)
const focusLabPublishing = ref(0)
const hasUnsavedChanges = computed(() => store.dirty || labDirty.value)
const templateSignature = computed(() => JSON.stringify({
  cells: store.sortedCells.map(({ id, type, source, order, student_editable, source_hidden }) => ({ id, type, source, order, student_editable, source_hidden })),
  environment: store.environmentVersionId, policy: store.importPolicyMode, imports: store.allowedImports,
}))
const editingMarkdownCell = ref(null)
const markdownEditSource = ref('')

// ── Phase 4：环境面板与发布确认 ──────────────────────────────────
const envOptions = ref([])
const historicalEnvLoads = new Map()
const showEnvPanel = ref(false)
const showPublishDialog = ref(false)
const publishBusy = ref(false)
const envDraftId = ref(null)          // 面板中待应用的草稿环境（确认后写 store）
const envDraftPolicy = ref('unrestricted')
const envDraftAllowed = ref([])

const selectedEnv = computed(() => envOptions.value.find((o) => o.environment_version_id === store.environmentVersionId) || null)
const envDraftEnv = computed(() => envOptions.value.find((o) => o.environment_version_id === envDraftId.value) || null)
const envImportCandidates = computed(() => {
  if (!envDraftEnv.value) return []
  const seen = new Set()
  const names = []
  for (const p of envDraftEnv.value.packages || []) {
    for (const name of p.import_names || []) {
      if (!seen.has(name)) { seen.add(name); names.push(name) }
    }
  }
  return names
})
const envMismatch = computed(() => {
  if (envDraftPolicy.value !== 'restricted' || envDraftAllowed.value.length === 0) return ''
  const installed = new Set(envImportCandidates.value)
  const missing = envDraftAllowed.value.filter((name) => !installed.has(name))
  return missing.length ? `注意：${missing.join('、')} 未在当前环境安装` : ''
})
// 发布确认信息：实验标题为主，环境与包摘要作为辅助信息
const publishSummary = computed(() => {
  const env = envOptions.value.find((o) => o.environment_version_id === store.environmentVersionId) || null
  return {
    title: store.name || '未命名模板',
    environment: env ? `${env.display_name} v${env.version_number}` : '',
    packages: env ? (env.packages || []).map((p) => p.pip_name) : [],
  }
})

async function appendHistoricalEnvironment(versionId) {
  if (!versionId || envOptions.value.some((option) => option.environment_version_id === versionId)) return
  if (typeof environmentsAPI.getVersionSummary !== 'function') return
  if (!historicalEnvLoads.has(versionId)) {
    historicalEnvLoads.set(versionId, environmentsAPI.getVersionSummary(versionId).then((res) => {
      if (res.data?.environment_version_id && !envOptions.value.some((option) => option.environment_version_id === versionId)) {
        envOptions.value = [...envOptions.value, { ...res.data, historical: true }]
      }
    }).catch(() => {}).finally(() => historicalEnvLoads.delete(versionId)))
  }
  await historicalEnvLoads.get(versionId)
}

async function fetchEnvOptions() {
 try {
    const res = await environmentsAPI.listAvailable()
    envOptions.value = res.data || []
    await appendHistoricalEnvironment(store.environmentVersionId)
 } catch { /* 环境列表加载失败不阻塞编辑 */ }
}

// The template and environment list load in parallel.  Re-check after either
// one completes so an already-bound historical version is never replaced by
// the current-only picker response.
watch(
  () => [store.environmentVersionId, envOptions.value.length],
  () => {
    if (store.environmentVersionId && envOptions.value.length) void appendHistoricalEnvironment(store.environmentVersionId)
  },
)

function openEnvPanel() {
  envDraftId.value = store.environmentVersionId
  envDraftPolicy.value = store.importPolicyMode
  envDraftAllowed.value = [...store.allowedImports]
  showEnvPanel.value = true
}

function toggleEnvDraftImport(name) {
  const idx = envDraftAllowed.value.indexOf(name)
  if (idx >= 0) envDraftAllowed.value.splice(idx, 1)
  else envDraftAllowed.value.push(name)
}

// 应用环境设置：写入 store 并纳入 dirty，随下一次保存提交（同 revision）
function applyEnvSettings() {
  store.setEnvironment(envDraftId.value, envDraftPolicy.value, envDraftPolicy.value === 'restricted' ? [...envDraftAllowed.value] : [])
  showEnvPanel.value = false
  app.showToast('环境设置已修改，保存后生效', 'success')
}

// 发布前确认（计划 11.2：显示本次将发布的环境版本与包摘要）
function showLabPublishing() {
  showLabPanel.value = true
  focusLabPublishing.value += 1
}

function confirmPublish() {
  if (labConfigured.value) { showLabPublishing(); return }
  showPublishDialog.value = true
}

async function doPublish() {
  publishBusy.value = true
  try {
    const version = await store.publish()
    if (version) showPublishDialog.value = false
    if (store.error?.code === 'LAB_ASSESSMENT_PUBLISH_REQUIRED') {
      showPublishDialog.value = false
      labConfigured.value = true
      showLabPublishing()
    }
  } finally {
    publishBusy.value = false
  }
}
// 未保存离开守卫：自定义确认弹窗（替代原 window.confirm）
const showLeaveDialog = ref(false)
let confirmedLeave = false
let leaveResolver = null

const visibleCells = computed(() => store.studentPreview ? store.sortedCells.filter(c => !c.source_hidden) : store.sortedCells)

onBeforeRouteLeave((_to, _from) => {
  if (confirmedLeave || !((hasUnsavedChanges.value) && !store.conflict)) {
    store.destroy()
    return true
  }
  return new Promise((resolve) => {
    leaveResolver = resolve
    showLeaveDialog.value = true
  })
})

// goBack 路径的确认标志：router.back() 是原生历史导航，不经过路由守卫，
// dirty 时需在按钮内手动弹确认框，确认后再 back
let pendingBack = false

function onConfirmLeave() {
  const resolve = leaveResolver
  leaveResolver = null
  if (pendingBack) {
    pendingBack = false
    confirmedLeave = true
    showLeaveDialog.value = false
    store.destroy()
    router.back()
    return
  }
  if (!resolve) return
  confirmedLeave = true
  showLeaveDialog.value = false
  store.destroy()
  resolve(true)
}

function onCancelLeave() {
  const resolve = leaveResolver
  leaveResolver = null
  pendingBack = false
  showLeaveDialog.value = false
  if (resolve) resolve(false)
}

// 浏览器刷新/关闭兜底：dirty 时触发系统级离开确认。
// 注意：router.back() 走原生历史导航会触发 beforeunload ——
// 已确认离开（confirmedLeave）后必须放行，避免自定义弹窗确认后再次弹浏览器对话框
function onBeforeUnload(e) {
  if (!confirmedLeave && hasUnsavedChanges.value && !store.conflict) e.preventDefault()
}
onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))

function goBack() {
  if (props.backTo) {
    router.push(props.backTo)
    return
  }
  if (hasUnsavedChanges.value && !store.conflict) {
    pendingBack = true
    showLeaveDialog.value = true
    return
  }
  router.back()
}

// ── 拖拽排序（按下立即预备，移动 5px 激活；模块跟随指针、其余实时让位，Esc 取消）──
// 交互约定：
// 1. pointerdown 手柄 → 立即进入预备态（armed：手柄高亮、模块浮起），即时反馈「可拖」；
// 2. 纵向移动超过阈值 → 正式激活：被拖模块半透明带阴影 transform 跟随指针，
//    其余模块以 CSS transition 实时让位（上移/下移一格），dropIndex 实时更新；
// 3. 松手 → 调用 store.moveCellTo 落位，下一帧（store 重排渲染后）清除 transform，
//    被拖模块从指针位置平滑过渡到新位置；Esc / pointercancel / 失焦 → 取消并平滑归位。
const DRAG_THRESHOLD_PX = 5
// 拖拽自动滚动：指针进入滚动容器上下边缘触发带后，每帧向该方向滚动一段。
// 触发带高度与单帧最大步长（像素），步长随距边缘距离加速（贴边最快）
const AUTO_SCROLL_ZONE_PX = 60
const AUTO_SCROLL_MAX_STEP_PX = 16
const cellElMap = new Map()
const armedCellId = ref(null)
const draggingCellId = ref(null)
/** 拖拽会话。拖动期间高频更新直接操作 DOM，不经过响应式，避免每帧触发渲染 */
let dragSession = null

function setCellEl(el, cellId) {
  if (el) cellElMap.set(cellId, el)
  else cellElMap.delete(cellId)
}

function onDragPointerDown(e, cellId) {
  if (store.studentPreview) return
  e.preventDefault()
  if (dragSession) finishDrag(false) // 兜底：上一会话残留（如指针在窗口外松手），先清场
  dragSession = {
    cellId,
    pointerId: e.pointerId,
    startY: e.clientY,
    active: false,
    fromIndex: visibleCells.value.findIndex((c) => c.id === cellId),
    dropIndex: -1,
    els: [],
    staticTops: [],
    staticHeights: [],
    draggedEl: null,
  }
  armedCellId.value = cellId
  window.addEventListener('pointermove', onDragPointerMove)
  window.addEventListener('pointerup', onDragPointerUp)
  window.addEventListener('pointercancel', onDragPointerCancel)
  window.addEventListener('keydown', onDragKeydown)
  window.addEventListener('blur', onDragWindowBlur)
  // 捕获阶段兜底：拖拽中在页面任意处再次按下 → 取消当前会话，防止状态残留
  window.addEventListener('pointerdown', onGlobalPointerDown, true)
}

function onDragPointerMove(e) {
  const s = dragSession
  if (!s || e.pointerId !== s.pointerId) return
  if (!s.active) {
    if (Math.abs(e.clientY - s.startY) < DRAG_THRESHOLD_PX) return
    activateDrag()
  }
  updateDrag(e.clientY)
}

/** 超过阈值正式激活：记录各模块静态位置（此刻均无 transform），开始跟随与让位 */
function activateDrag() {
  const s = dragSession
  if (s.fromIndex < 0) {
    finishDrag(false)
    return
  }
  s.active = true
  s.els = visibleCells.value.map((c) => cellElMap.get(c.id)).filter(Boolean)
  s.staticTops = s.els.map((el) => el.getBoundingClientRect().top)
  s.staticHeights = s.els.map((el) => el.getBoundingClientRect().height)
  s.draggedEl = cellElMap.get(s.cellId)
  s.dropIndex = s.fromIndex
  // 滚动容器：页面滚动容器 .content → 编辑器自身 → 文档根（jsdom 下无 .content，走后两者兜底）
  s.scroller = s.draggedEl.closest('.content') || s.draggedEl.closest('.studio-editor') || document.scrollingElement
  // 自动滚动状态：scrollDelta 累计滚动增量，静态坐标一律按 staticTop - scrollDelta 参与计算
  s.scrollDelta = 0
  s.lastClientY = s.startY
  s.autoScrollRaf = null
  s.autoScrollDir = 0
  armedCellId.value = null
  draggingCellId.value = s.cellId
  document.body.classList.add('dragging-cells')
  updateDrag(s.startY)
}

function updateDrag(clientY) {
  const s = dragSession
  s.lastClientY = clientY
  applyDragVisual(s, clientY)
  updateAutoScroll(s, clientY)
}

/**
 * 计算 dropIndex 并应用全部位移。滚动增量 scrollDelta 只用于兄弟模块中心的
 * 内容坐标（staticTop - scrollDelta，随滚动上移），被拖模块的 transform 需
 * 加回滚动补偿，使其视口位置 = 静态位置 - scrollDelta + (指针位移 + scrollDelta)
 * = 静态位置 + 指针位移，滚动前后始终贴在指针下方不漂移
 */
function applyDragVisual(s, clientY) {
  const from = s.fromIndex
  // 被拖模块视觉中心 = 静态中心 + 跟随位移（transform 已含滚动补偿）；越过谁的中心就插到谁前面（不含自己）
  const draggedCenter = s.staticTops[from] + s.staticHeights[from] / 2 + (clientY - s.startY)
  let dropIndex = s.els.length
  for (let i = 0; i < s.els.length; i++) {
    if (i === from) continue
    if (draggedCenter < s.staticTops[i] - s.scrollDelta + s.staticHeights[i] / 2) {
      dropIndex = i
      break
    }
  }
  s.dropIndex = dropIndex
  // 被拖模块跟随指针：位移 = 指针位移 + 滚动补偿（容器滚动时模块仍贴着指针）
  s.draggedEl.style.transform = `translate3d(0px, ${clientY - s.startY + s.scrollDelta}px, 0px)`
  // 其余模块实时让位：向下拖 → 下方模块逐格上移；向上拖 → 上方模块逐格下移
  for (let i = 0; i < s.els.length; i++) {
    if (i === from) continue
    const el = s.els[i]
    let shift = 0
    if (from < dropIndex && i > from && i < dropIndex) shift = s.staticTops[i - 1] - s.staticTops[i]
    else if (from > dropIndex && i >= dropIndex && i < from) shift = s.staticTops[i + 1] - s.staticTops[i]
    el.style.transform = shift ? `translate3d(0px, ${shift}px, 0px)` : ''
  }
}

/** 指针进入滚动容器上下边缘触发带（各 60px）时启动对应方向滚动，否则停止 */
function updateAutoScroll(s, clientY) {
  const scroller = s.scroller
  if (!scroller) return
  const rect = scroller.getBoundingClientRect()
  let dir = 0
  if (clientY <= rect.top + AUTO_SCROLL_ZONE_PX) {
    if (scroller.scrollTop > 0) dir = -1 // 顶部触发带：内容向上滚（scrollTop 减小）
  } else if (clientY >= rect.bottom - AUTO_SCROLL_ZONE_PX) {
    if (scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight) dir = 1 // 底部触发带：向下滚
  }
  if (dir) startAutoScroll(s, dir)
  else stopAutoScroll(s)
}

/**
 * 启动/维持 rAF 滚动循环：每帧至多滚一步（帧级节流，避免高频 pointermove
 * 重复设置 scrollTop 抖动），指针停在触发带内也持续滚动
 */
function startAutoScroll(s, dir) {
  if (s.autoScrollDir === dir) return
  stopAutoScroll(s)
  s.autoScrollDir = dir
  const tick = () => {
    const cur = dragSession
    if (!cur || cur.autoScrollDir !== dir) return // 会话结束或方向变更：终止循环
    stepAutoScroll(cur, dir)
    cur.autoScrollRaf = requestAnimationFrame(tick)
  }
  s.autoScrollRaf = requestAnimationFrame(tick)
}

function stopAutoScroll(s) {
  if (s.autoScrollRaf) cancelAnimationFrame(s.autoScrollRaf)
  s.autoScrollRaf = null
  s.autoScrollDir = 0
}

/** 滚动一步：步长按距边缘距离加速（贴边最快），并累计 scrollDelta 校准静态坐标 */
function stepAutoScroll(s, dir) {
  const scroller = s.scroller
  let distance
  if (dir === 1) {
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight) { stopAutoScroll(s); return }
    distance = Math.max(0, Math.min(AUTO_SCROLL_ZONE_PX, scroller.getBoundingClientRect().bottom - s.lastClientY))
  } else {
    if (scroller.scrollTop <= 0) { stopAutoScroll(s); return }
    distance = Math.max(0, Math.min(AUTO_SCROLL_ZONE_PX, s.lastClientY - scroller.getBoundingClientRect().top))
  }
  const step = Math.max(1, Math.round(((AUTO_SCROLL_ZONE_PX - distance) / AUTO_SCROLL_ZONE_PX) * AUTO_SCROLL_MAX_STEP_PX))
  scroller.scrollTop += dir * step
  s.scrollDelta += dir * step
}

function onDragPointerUp(e) {
  const s = dragSession
  if (!s || e.pointerId !== s.pointerId) return
  finishDrag(true)
}

function onDragPointerCancel() {
  finishDrag(false)
}

/** 窗口失焦（Alt-Tab 等）：指针事件会丢失，按取消处理避免意外落位与状态残留 */
function onDragWindowBlur() {
  if (dragSession) finishDrag(false)
}

function onGlobalPointerDown() {
  if (dragSession) finishDrag(false)
}

function onDragKeydown(e) {
  if (e.key === 'Escape') finishDrag(false)
}

function finishDrag(commit) {
  const s = dragSession
  if (!s) return
  stopAutoScroll(s) // 终止滚动循环，避免会话结束后仍在滚动
  dragSession = null
  armedCellId.value = null
  window.removeEventListener('pointermove', onDragPointerMove)
  window.removeEventListener('pointerup', onDragPointerUp)
  window.removeEventListener('pointercancel', onDragPointerCancel)
  window.removeEventListener('keydown', onDragKeydown)
  window.removeEventListener('blur', onDragWindowBlur)
  window.removeEventListener('pointerdown', onGlobalPointerDown, true)
  document.body.classList.remove('dragging-cells')
  if (!s.active) return // 预备态直接松手：无任何动作
  if (commit) {
    store.moveCellTo(s.cellId, s.dropIndex)
    // 等 store 重排渲染完成后清除残留 transform。注意：清除必须放在 nextTick
    // （Vue patch 之后）—— 先移除 .dragging 恢复 transition，再清 transform，
    // 被拖模块才能从指针位置平滑过渡到新位置，而不是瞬跳
    requestAnimationFrame(() => {
      draggingCellId.value = null
      void nextTick(clearDragTransforms)
    })
  } else {
    draggingCellId.value = null // 同上：先恢复 transition 再清 transform → 所有模块平滑归位
    void nextTick(clearDragTransforms)
  }
}

function clearDragTransforms() {
  for (const el of cellElMap.values()) el.style.transform = ''
}

onBeforeUnmount(() => {
  // 卸载时兜底清场：不提交（组件已销毁），仅移除监听与残留样式
  if (dragSession) {
    stopAutoScroll(dragSession)
    dragSession = null
    window.removeEventListener('pointermove', onDragPointerMove)
    window.removeEventListener('pointerup', onDragPointerUp)
    window.removeEventListener('pointercancel', onDragPointerCancel)
    window.removeEventListener('keydown', onDragKeydown)
    window.removeEventListener('blur', onDragWindowBlur)
    window.removeEventListener('pointerdown', onGlobalPointerDown, true)
    document.body.classList.remove('dragging-cells')
    clearDragTransforms()
  }
})

async function init() {
  loading.value = true
  try {
    await Promise.all([store.open(props.templateId), fetchEnvOptions()])
  }
  catch { app.showToast('加载模板失败', 'error') }
  finally { loading.value = false }
}
init()

async function handleSave() {
  if (editingMarkdownCell.value) handleMarkdownEdit(editingMarkdownCell.value)
  return await store.saveDraft()
}

function onFileChange(e) {
  const f = e.target.files[0]
  if (!f) return
  store.importExisting(f).then(() => app.showToast('导入成功', 'success')).catch(e => app.showToast(e.response?.data?.detail?.message || '导入失败', 'error'))
}

function envName(versionId) {
  const env = envOptions.value.find((o) => o.environment_version_id === versionId)
  return env ? `${env.display_name} v${env.version_number}` : ''
}

async function loadHistory() {
  try {
    const { studioAPI } = await import('../../api/studio.js')
    const res = await studioAPI.getVersions(props.templateId)
    history.value = res.data || []
  } catch { /* 历史加载失败不阻塞查看 */ }
  showHistory.value = true
}

function replaceWithStarter(cells) {
  store.cells = cells.map((cell, index) => ({ ...cell, order: index }))
  store.dirty = true
  store.saved = false
}

function handleRun(cellId) { store.previewRun(cellId) }
function handleUpdateSource(cellId, source) { store.updateCellSource(cellId, source) }
function handleMarkdownEdit(cellId) {
  if (editingMarkdownCell.value === cellId) {
    const cell = store.cells.find(c => c.id === cellId)
    if (cell) store.updateCellSource(cell.id, markdownEditSource.value)
    editingMarkdownCell.value = null
  } else {
    const cell = store.cells.find(c => c.id === cellId)
    markdownEditSource.value = cell?.source || ''
    editingMarkdownCell.value = cellId
  }
}
</script>
<template>
  <div v-if="loading" class="studio-loading">
    <div class="skeleton-bar" v-for="i in 3" :key="i" :style="{ width: (60 + i * 15) + '%' }"></div>
  </div>
  <div v-else class="studio-editor">
    <div class="studio-toolbar">
      <div class="toolbar-left">
        <button class="tb-btn" type="button" @click="goBack">
          <AppIcon name="back" :size="14" /> 返回
        </button>
        <h2 class="studio-name">{{ store.name || '未命名模板' }}</h2>
        <span class="revision-badge">rev {{ store.draftRevision }}</span>
      </div>
      <div class="toolbar-right">
        <button v-if="store.lessonId || store.moduleId" class="tb-btn" :class="{ active: showLabPanel }" :aria-expanded="showLabPanel" @click="showLabPanel = !showLabPanel">实验评测{{ labDirty ? ' · 未保存' : '' }}</button>
        <button class="tb-btn" :class="{ active: showEnvPanel }" @click="showEnvPanel ? (showEnvPanel = false) : openEnvPanel()" title="运行环境与导入规则">
          环境{{ selectedEnv ? `：${selectedEnv.display_name} v${selectedEnv.version_number}` : '' }}
        </button>
        <span class="save-state" :class="{ dirty: store.dirty, conflict: store.conflict }">{{ store.conflict ? '冲突' : store.dirty ? '未保存' : store.saving ? '保存中…' : '已保存' }}</span>
        <button class="tb-btn" @click="handleSave" :disabled="store.saving || store.conflict">保存</button>
        <button class="tb-btn tb-btn-accent" @click="confirmPublish" :disabled="store.saving">{{ labConfigured ? '实验发布' : '发布' }}</button>
        <label class="tb-btn">导入<input type="file" accept=".ipynb,.zip" class="hidden-input" @change="onFileChange" /></label>
        <button class="tb-btn" @click="store.exportDraft()">导出</button>
        <button class="tb-btn" @click="loadHistory">历史</button>
        <button class="tb-btn" :class="{ active: store.studentPreview }" @click="store.studentPreview = !store.studentPreview">预览</button>
      </div>
    </div>

    <LabAssessmentPanel v-if="labPanelOpened && (store.lessonId || store.moduleId)" v-show="showLabPanel" :kind="store.lessonId ? 'lesson' : 'module'" :entry-id="store.lessonId || store.moduleId" :environment-id="store.environmentVersionId" :cells="store.sortedCells" :template-dirty="store.dirty" :template-signature="templateSignature" :focus-publishing="focusLabPublishing" :save-template="handleSave" @dirty="labDirty = $event" @configured="labConfigured = $event" @environment="store.setEnvironment($event, store.importPolicyMode, [...store.allowedImports])" @import-starter="replaceWithStarter" />

    <!-- ── 环境设置面板（Phase 4：教师选择，纳入 dirty 随草稿保存） ── -->
    <div v-if="showEnvPanel" class="env-panel">
      <div class="env-panel-grid">
        <div>
          <EnvironmentProfilePicker v-model="envDraftId" show-memory label="运行环境" />
          <p v-if="!envOptions.length" class="env-panel-hint warn">暂无可用环境，请联系管理员</p>
        </div>
        <div>
          <label class="env-panel-label">导入规则</label>
          <DsSelect v-model="envDraftPolicy" class="env-panel-select">
            <option value="unrestricted">不限制（学生可导入任何库）</option>
            <option value="restricted">限定白名单（教学规则）</option>
          </DsSelect>
        </div>
      </div>
      <div v-if="envDraftPolicy === 'restricted'" class="env-panel-chips">
        <label v-for="name in envImportCandidates" :key="name" class="env-panel-chip">
          <input type="checkbox" :checked="envDraftAllowed.includes(name)" @change="toggleEnvDraftImport(name)" />
          {{ name }}
        </label>
        <p v-if="!envImportCandidates.length" class="env-panel-hint">当前环境未提供教学库，可留空白名单（仅标准库）</p>
      </div>
      <p v-if="envMismatch" class="env-panel-hint warn">{{ envMismatch }}</p>
      <div class="env-panel-actions">
        <button class="tb-btn" @click="showEnvPanel = false">取消</button>
        <button class="tb-btn tb-btn-accent" @click="applyEnvSettings">应用（保存后生效）</button>
      </div>
    </div>
    <div v-if="store.conflict" class="conflict-banner">
      <span>{{ store.conflictMessage }}</span>
      <button class="conflict-reload" @click="init">刷新</button>
    </div>
    <p v-if="store.description" class="studio-desc">{{ store.description }}</p>
    <div v-if="store.studentPreview" class="preview-notice">学生视角 — 隐藏 Cell 不显示，不可编辑</div>
    <div
      v-for="cell in visibleCells"
      :key="cell.id"
      class="cell-editor-wrapper"
      :class="{ armed: armedCellId === cell.id, dragging: draggingCellId === cell.id }"
      :data-cell-id="cell.id"
      :ref="(el) => setCellEl(el, cell.id)"
    >
      <div v-if="!store.studentPreview" class="cell-toolbar">
        <button
          class="cell-drag-handle"
          :class="{ armed: armedCellId === cell.id || draggingCellId === cell.id }"
          type="button"
          aria-label="按住拖动排序"
          title="按住拖动排序"
          @pointerdown="onDragPointerDown($event, cell.id)"
        >
          <AppIcon name="drag" :size="14" />
        </button>
        <span class="cell-type-badge" :class="cell.type">{{ cell.type === 'code' ? 'Python' : 'MD' }}</span>
        <div class="cell-actions">
          <button class="cell-action-btn" :class="{ on: cell.student_editable === true }" :aria-pressed="cell.student_editable === true" @click="store.setCellEditable(cell.id, cell.student_editable !== true)">{{ cell.type === 'markdown' ? '学生填写' : '编辑' }}</button>
          <button v-if="cell.type === 'code'" class="cell-action-btn" :class="{ on: cell.source_hidden }" @click="store.setCellHidden(cell.id, !cell.source_hidden)">隐藏</button>
          <button class="cell-action-btn" @click="store.addCell('markdown', cell.id)">+讲解</button>
          <button class="cell-action-btn" @click="store.addCell('code', cell.id)">+代码</button>
          <button class="cell-action-btn" @click="store.duplicateCell(cell.id)">复制</button>
          <button class="cell-action-btn" @click="store.moveCell(cell.id, 'up')">上移</button>
          <button class="cell-action-btn" @click="store.moveCell(cell.id, 'down')">下移</button>
          <button class="cell-action-btn cell-action-del" @click="store.deleteCell(cell.id)">删除</button>
        </div>
      </div>
      <template v-if="cell.type === 'markdown'">
        <div v-if="editingMarkdownCell === cell.id && !store.studentPreview" class="md-editor-wrap">
          <textarea v-model="markdownEditSource" @input="store.updateCellSource(cell.id, $event.target.value)" class="md-textarea" placeholder="Markdown 内容…" rows="6"></textarea>
          <div class="md-editor-bar"><button class="tb-btn tb-btn-sm" @click="handleMarkdownEdit(cell.id)">完成</button></div>
        </div>
        <div v-else class="md-show-wrap" @dblclick="!store.studentPreview && handleMarkdownEdit(cell.id)">
          <MarkdownCell :cell="{ ...cell, source: cell.source || '', rendered_html: '' }" />
          <button v-if="!store.studentPreview" class="md-edit-hint" @click="handleMarkdownEdit(cell.id)">双击编辑</button>
        </div>
      </template>
      <CodeCell
        v-else
        :cell="{ id: cell.id, type: 'code', source: cell.source, outputs: cell._previewOutputs || null, student_editable: cell.student_editable !== false }"
        :execution-count="cell._previewOutputs?.execution_count ?? null"
        :disabled="cell.source_hidden || (store.runningCellId !== null && store.runningCellId !== cell.id)"
        :readonly="store.studentPreview && cell.student_editable === false"
        :is-executing="store.runningCellId === cell.id"
        @execute="handleRun"
        @update:source="handleUpdateSource"
      />
    </div>
    <div v-if="visibleCells.length === 0 && !loading" class="studio-empty">
      <p>暂无 Cell</p>
      <div class="empty-actions">
        <button class="tb-btn" @click="store.addCell('markdown', -1)">+ 添加讲解</button>
        <button class="tb-btn" @click="store.addCell('code', -1)">+ 添加代码</button>
      </div>
    </div>
    <div v-if="showHistory" class="modal-overlay" @click.self="showHistory = false">
      <div v-dialog-focus class="modal-content history-modal" role="dialog" aria-modal="true" aria-label="版本历史" @keydown.esc="showHistory = false">
        <div class="modal-header"><h3>版本历史</h3><button class="modal-close" aria-label="关闭版本历史" @click="showHistory = false">✕</button></div>
        <div class="modal-body">
          <p v-if="history.length === 0" class="text-secondary">暂无已发布版本</p>
          <div v-for="v in history" :key="v.id" class="history-item">
            <span class="version-num">v{{ v.version_number }}</span>
            <span class="version-env">{{ envName(v.environment_version_id) }}</span>
            <span class="version-sha">{{ v.sha256.slice(0, 12) }}</span>
            <span class="version-date">{{ new Date(v.published_at).toLocaleString() }}</span>
            <button class="tb-btn tb-btn-sm" @click="store.exportVersion(v.id)">导出</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 发布确认：显示实验标题，并附带本次使用的环境与包摘要 -->
    <ConfirmDialog
      v-if="showPublishDialog"
      title="确认发布？"
      :message="`本次将发布：${publishSummary.title}${publishSummary.environment ? `；运行环境：${publishSummary.environment}` : ''}${publishSummary.packages.length ? `；可用库：${publishSummary.packages.join(' · ')}` : ''}`"
      confirm-text="发布"
      cancel-text="取消"
      :busy="publishBusy"
      @confirm="doPublish"
      @cancel="showPublishDialog = false"
    />

    <!-- 未保存离开确认（替代原 window.confirm，四页统一） -->
    <ConfirmDialog
      v-if="showLeaveDialog"
      title="有未保存的修改"
      message="确定离开吗？未保存的内容将丢失。"
      confirm-text="离开"
      cancel-text="取消"
      @confirm="onConfirmLeave"
      @cancel="onCancelLeave"
    />
  </div>
</template>
<style scoped>
.studio-loading { padding: var(--space-6); }
.skeleton-bar { height: 16px; margin-bottom: var(--space-3); background: var(--border); border-radius: var(--radius-sm); animation: pulse 1.2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.4; } 50% { opacity: 0.8; } }
.studio-editor { max-width: 1280px; margin: 0 auto; }
.studio-toolbar { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) 0; margin-bottom: var(--space-3); border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.toolbar-left { display: flex; align-items: center; gap: var(--space-2); }
.studio-name { font-size: var(--text-lg); font-weight: 600; margin: 0; }
.revision-badge { font-size: var(--text-xs); color: var(--muted); background: var(--surface-subtle); padding: 2px 6px; border-radius: var(--radius-sm); }
.toolbar-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.save-state { font-size: var(--text-xs); color: var(--muted); white-space: nowrap; }
.save-state.dirty { color: var(--accent); }
.save-state.conflict { color: var(--error); }
.tb-btn { display: inline-flex; align-items: center; gap: 4px; padding: 5px 10px; font-size: var(--text-xs); font-weight: 500; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--fg); cursor: pointer; transition: all var(--duration-fast); white-space: nowrap; position: relative; }
.tb-btn:hover { border-color: var(--border-strong); background: var(--surface-subtle); }
.tb-btn:disabled { opacity: .5; cursor: not-allowed; }
.tb-btn.active { border-color: var(--accent); color: var(--accent); }
.tb-btn-accent { background: var(--accent); color: var(--surface); border-color: var(--accent); }
.tb-btn-accent:hover { background: var(--accent-hover); }
.tb-btn-sm { padding: 3px 8px; font-size: 11px; }
.hidden-input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.conflict-banner { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-3) var(--space-4); margin-bottom: var(--space-3); background: oklch(0.54 0.20 25 / 0.08); border: 1px solid oklch(0.54 0.20 25 / 0.3); border-radius: var(--radius-md); color: var(--error); font-size: var(--text-sm); }
.conflict-reload { margin-left: auto; background: var(--error); color: var(--surface); border: none; padding: 4px 12px; border-radius: var(--radius-sm); cursor: pointer; font-size: var(--text-xs); }
.studio-desc { font-size: var(--text-sm); color: var(--muted); margin-bottom: var(--space-3); }
.preview-notice { padding: var(--space-2) var(--space-4); margin-bottom: var(--space-3); background: var(--accent-soft); border-left: 3px solid var(--accent); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; font-size: var(--text-xs); color: var(--accent-hover); }
.cell-editor-wrapper { position: relative; margin-bottom: var(--space-3); transition: transform 150ms ease; }
.cell-toolbar { display: flex; align-items: center; gap: 4px; padding: 4px 8px; background: var(--surface-subtle); border: 1px solid var(--border); border-bottom: none; border-radius: var(--radius-sm) var(--radius-sm) 0 0; }
.cell-editor-wrapper.armed { box-shadow: var(--shadow-md); }
/* 被拖模块：半透明 + 阴影 + 置顶，跟随期间必须关闭 transition 避免拖影 */
.cell-editor-wrapper.dragging { opacity: .45; z-index: 5; box-shadow: var(--shadow-lg); transition: none; }
.cell-drag-handle {
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid transparent; border-radius: var(--radius-sm);
  padding: 2px 4px; font-size: 12px; cursor: grab; color: var(--faint);
  background: none; touch-action: none; user-select: none; -webkit-user-select: none;
}
.cell-drag-handle:hover { border-color: var(--border); color: var(--muted); }
.cell-drag-handle.armed { cursor: grabbing; color: var(--accent); border-color: var(--accent); background: var(--surface); }
body.dragging-cells { user-select: none; -webkit-user-select: none; cursor: grabbing; }
.cell-type-badge { font-size: 10px; font-weight: 600; text-transform: uppercase; padding: 1px 6px; border-radius: var(--radius-sm); }
.cell-type-badge.code { background: oklch(0.52 0.095 158 / 0.1); color: var(--accent); }
.cell-type-badge.markdown { background: oklch(0.52 0.095 158 / 0.1); color: var(--accent); }
.cell-actions { margin-left: auto; display: flex; gap: 2px; }
.cell-action-btn { background: none; border: 1px solid transparent; border-radius: var(--radius-sm); padding: 1px 5px; font-size: 12px; cursor: pointer; color: var(--muted); }
.cell-action-btn:hover { border-color: var(--border); background: var(--surface); }
.cell-action-btn.on { color: var(--accent); }
.cell-action-del:hover { color: var(--error); border-color: oklch(0.54 0.20 25 / 0.3); }
.md-editor-wrap { border: 1px solid var(--border); border-top: none; }
.md-textarea { width: 100%; padding: var(--space-3) var(--space-4); border: none; outline: none; font-family: var(--font-mono); font-size: 13px; line-height: 1.6; resize: vertical; background: var(--surface); color: var(--fg); }
.md-editor-bar { padding: var(--space-2) var(--space-3); border-top: 1px solid var(--border); background: var(--surface-subtle); }
.md-show-wrap { position: relative; cursor: default; }
.md-edit-hint { position: absolute; top: 4px; right: 8px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 1px 6px; font-size: 10px; color: var(--faint); cursor: pointer; }
.studio-empty { text-align: center; padding: var(--space-12); color: var(--muted); }
.empty-actions { display: flex; gap: var(--space-2); justify-content: center; margin-top: var(--space-3); }
.modal-overlay { position: fixed; inset: 0 0 0 var(--modal-left, 0); z-index: 100; background: oklch(0 0 0 / 0.3); display: flex; align-items: center; justify-content: center; }
.modal-content { background: var(--surface); border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); max-width: 560px; width: 90vw; max-height: 80vh; overflow-y: auto; }
.modal-header { display: flex; align-items: center; justify-content: space-between; padding: var(--space-4); border-bottom: 1px solid var(--border); }
.modal-header h3 { margin: 0; font-size: var(--text-md); }
.modal-close { background: none; border: none; cursor: pointer; font-size: 16px; color: var(--muted); }
.modal-body { padding: var(--space-4); }
.history-item { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-2) 0; border-bottom: 1px solid var(--border); font-size: var(--text-sm); }
.history-item:last-child { border-bottom: none; }
.version-num { font-weight: 600; min-width: 36px; }
.version-env { color: var(--muted); font-size: var(--text-xs, 12px); }
/* ── Phase 4：环境面板 ─────────────────────────────────────────── */
.env-panel {
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-raised, var(--surface-subtle));
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.env-panel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.env-panel-label { display: block; font-size: var(--text-sm, 13px); font-weight: 600; color: var(--muted); margin-bottom: 6px; }
.env-panel-select {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control, 7px);
  background: var(--surface, var(--surface));
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-sm, 13px);
}
.env-panel-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.env-panel-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--surface, var(--surface));
  font-size: var(--text-sm, 13px);
  cursor: pointer;
}
.env-panel-chip input { margin: 0; }
.env-panel-hint { margin: 0; font-size: var(--text-xs, 12px); color: var(--faint); }
.env-panel-hint.warn { color: var(--warning, var(--warning)); }
.env-panel-actions { display: flex; justify-content: flex-end; gap: 6px; }
@media (max-width: 768px) { .env-panel-grid { grid-template-columns: 1fr; } }
.version-sha { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }
.version-date { color: var(--muted); flex: 1; }

.studio-toolbar { position: sticky; top: var(--header-height); z-index: 15; background: var(--bg); padding-block: 12px; }
.toolbar-left { min-width: 0; flex: 1 1 280px; }
.studio-name { overflow-wrap: anywhere; }
.toolbar-right { border-left: 1px solid var(--border); padding-left: 12px; }
.cell-actions { flex-wrap: wrap; justify-content: flex-end; }
.cell-toolbar { gap: 8px; }
.modal-content { display: flex; flex-direction: column; overflow: hidden; }
.modal-body { overflow: auto; }
.modal-header { flex: none; }
</style>
