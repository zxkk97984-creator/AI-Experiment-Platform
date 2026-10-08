<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRouter } from 'vue-router'
import MarkdownCell from './MarkdownCell.vue'
import ReportCell from './ReportCell.vue'
import LabStudentPanel from '../labs/LabStudentPanel.vue'
import CodeCell from './CodeCell.vue'
import { useExperimentStore } from '../../stores/experiment.js'
import { useAppStore } from '../../stores/app.js'

const props = defineProps({
  mode: { type: String, required: true },
  entryId: { type: [String, Number], required: true },
  courseId: { type: [String, Number], default: null },
})

const router = useRouter()
const route = router.currentRoute?.value || { query: {} }
const store = useExperimentStore()
const app = useAppStore()

const loading = ref(true)
const loadError = ref(null)
const showMenu = ref(false)
const labEnabled = ref(false)
const labPanel = ref(null)
let leaving = false

const statusClass = computed(() => {
  if (store.saving) return 'saving'
  if (store.conflict) return 'conflict'
  if (store.dirty) return 'dirty'
  if (store.saved) return 'saved'
  return ''
})

const statusText = computed(() => {
  if (store.conflict) return '冲突：请刷新页面'
  if (store.saving) return '保存中…'
  if (store.error) return store.error.message
  if (store.dirty) return '未保存'
  return '已保存'
})

onMounted(async () => {
  try {
    if (props.mode === 'lesson') {
      await (route.query.offering_id ? store.openLesson(props.entryId, props.courseId, Number(route.query.offering_id)) : store.openLesson(props.entryId, props.courseId))
    } else {
      await (route.query.offering_id ? store.openModule(props.entryId, Number(route.query.offering_id)) : store.openModule(props.entryId))
    }
    // 加载提交历史
    await store.loadSubmissions()
  } catch {
    loadError.value = store.error || { code: 'LOAD_FAILED', message: '加载笔记本失败' }
  } finally {
    loading.value = false
  }
})

// ── 路由保护 ──
onBeforeRouteLeave(async (_to, _from) => {
  if (leaving) return true
  leaving = true
  const ok = await store.canNavigate()
  if (!ok) { leaving = false; return false }
  return true
})

// 同一路由切换 lesson/module 时重新 load
onBeforeRouteUpdate(async (to, _from) => {
  if (leaving) return
  const ok = await store.canNavigate()
  if (!ok) return false
  store.destroy()
  loading.value = true
  try {
    if (to.params.lid) {
      await store.openLesson(to.params.lid, to.params.id, to.query.offering_id ? Number(to.query.offering_id) : null)
    } else if (to.params.id) {
      await store.openModule(to.params.id, to.query.offering_id ? Number(to.query.offering_id) : null)
    }
  } catch { app.showToast('加载失败', 'error') }
  finally { loading.value = false }
})

function goBack() {
  router.push(store.context?.returnPath || '/student/courses')
}

// beforeunload
function onBeforeUnload(e) {
  if (store.dirty || store.saving || store.error) {
    e.preventDefault(); e.returnValue = ''
  }
}
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', onBeforeUnload)
  onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))
}

onBeforeUnmount(() => { store.destroy() })

async function handleRun(cellId) {
  await store.executeCell(cellId)
}

function handleUpdateSource(cellId, source) {
  store.updateCellSource(cellId, source)
}
</script>

<template>
  <div v-if="loading" class="player-loading">
    <div class="skeleton-bar" v-for="i in 3" :key="i" :style="{ width: `${70 + i * 10}%` }"></div>
  </div>

  <div v-else-if="loadError" class="player-error">
    <div class="error-icon">📭</div>
    <h2 class="error-title">{{ loadError.code === 'NOT_FOUND' ? '资源不存在' : '加载失败' }}</h2>
    <p class="error-msg">{{ loadError.message }}</p>
    <button class="btn-ghost" @click="goBack">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M10 3L5 8l5 5"/></svg>
      返回上一页
    </button>
  </div>

  <div v-else class="notebook-player">
    <div class="player-topbar">
      <button class="btn-back" @click="goBack" title="返回">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M10 3L5 8l5 5"/></svg>
        <span>返回</span>
      </button>

      <div class="topbar-center">
        <h1 class="player-title">{{ store.entryName || 'Notebook' }}</h1>
      </div>

      <div class="topbar-right">
        <span class="save-status" :class="statusClass">
          <span class="status-dot"></span>
          {{ statusText }}
        </span>

        <div class="menu-container">
          <button
            class="btn-icon"
            @click="showMenu = !showMenu"
            :aria-expanded="showMenu"
            aria-haspopup="menu"
            title="更多操作"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="3" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="8" cy="13" r="1.5"/></svg>
          </button>
          <div v-if="showMenu" class="menu-dropdown" role="menu" @click.self="showMenu = false">
            <button class="menu-item" role="menuitem" @click="store.executeAllCells(); showMenu = false" :disabled="store.executingCellId !== null">
              ▶ 全部运行
            </button>
            <button class="menu-item" role="menuitem" @click="store.interruptKernel(); showMenu = false" :disabled="!store.executingCellId">
              ⏹ 中断 Kernel
            </button>
            <button class="menu-item" role="menuitem" @click="store.restartKernel(); showMenu = false">
              ↻ 重启 Kernel
            </button>
            <button class="menu-item" role="menuitem" @click="store.flushSave(); showMenu = false" :disabled="!store.dirty">
              💾 强制保存
            </button>
            <div class="menu-divider"></div>
            <button class="menu-item menu-submit" role="menuitem"
              @click="(labEnabled || store.labEnabled) ? labPanel?.submit() : store.submitExperiment(); showMenu = false"
              :disabled="store.submitting || (store.labEnabled && !labPanel?.canSubmit)">
              📤 {{ store.submitting ? '提交中…' : '提交实验' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <p v-if="store.entryDescription" class="player-desc">{{ store.entryDescription }}</p>

    <!-- Phase 5：环境提示（学生无需选择/切换/确认，只读展示） -->
    <div v-if="store.environmentSummary" class="player-env">
      <span class="env-dot"></span>
      <span class="env-name">{{ store.environmentSummary.display_name }} {{ store.environmentSummary.version_label }}</span>
      <span v-if="store.environmentSummary.imports?.length" class="env-imports">
        可用库：{{ store.environmentSummary.imports.join(' · ') }}
      </span>
      <span
        v-if="store.environmentSummary.import_policy_mode === 'restricted' && store.environmentSummary.allowed_imports?.length"
        class="env-imports"
      >
        允许导入：{{ store.environmentSummary.allowed_imports.join(' · ') }}
      </span>
    </div>

    <LabStudentPanel v-if="store.recordId && store.labEnabled" :key="store.recordId" ref="labPanel" @enabled="labEnabled = $event" />

    <!-- 提交状态栏 -->
    <div v-if="store.submitAttemptCount > 0 || store.submitting" class="submit-bar">
      <span v-if="store.submitting" class="submit-status submitting">📤 正在提交...</span>
      <span v-else class="submit-status submitted">
        ✅ 最近提交：第 {{ store.submitAttemptCount }} 次
        <span v-if="store.lastSubmitTime">
          （{{ new Date(store.lastSubmitTime).toLocaleString('zh-CN') }}）
        </span>
      </span>
      <span v-if="store.submissions.length > 1" class="submit-history-hint">
        | 共 {{ store.submissions.length }} 次提交记录
      </span>
    </div>

    <div v-if="store.cells.length === 0" class="player-empty">
      <p>暂无内容</p>
    </div>
    <div v-for="cell in store.cells" :key="cell.id" class="cell-wrapper">
      <ReportCell v-if="cell.type === 'markdown' && cell.student_editable" :cell="cell" @update:source="handleUpdateSource" />
      <MarkdownCell v-else-if="cell.type === 'markdown'" :cell="cell" />
      <CodeCell
        v-else-if="cell.type === 'code'"
        :cell="cell"
        :execution-count="cell.outputs?.execution_count ?? null"
        :disabled="store.executingCellId !== null && store.executingCellId !== cell.id"
        :readonly="!cell.student_editable"
        :is-executing="store.executingCellId === cell.id"
        :check-result="(store.cellCheckResults || {})[cell.id]"
        @execute="handleRun"
        @update:source="handleUpdateSource"
      />
    </div>
  </div>
</template>

<style scoped>
.player-loading { padding: var(--space-6); }
.player-error {
  text-align: center; padding: var(--space-16) var(--space-6);
}
.error-icon { font-size: 48px; margin-bottom: var(--space-4); }
.error-title {
  font-size: var(--text-xl); font-weight: 600; color: var(--fg);
  margin: 0 0 var(--space-2);
}
.error-msg {
  font-size: var(--text-sm); color: var(--muted);
  margin: 0 0 var(--space-6);
}
.skeleton-bar {
  height: 16px; margin-bottom: var(--space-3);
  background: var(--border); border-radius: var(--radius-sm);
  animation: pulse 1.2s infinite;
}
@keyframes pulse { 0%, 100% { opacity: 0.4; } 50% { opacity: 0.8; } }

.notebook-player { max-width: 900px; margin: 0 auto; }

.player-topbar {
  display: flex; align-items: center; gap: var(--space-3);
  padding: var(--space-2) 0; margin-bottom: var(--space-3);
  border-bottom: 1px solid var(--border);
}
.btn-back {
  display: inline-flex; align-items: center; gap: 4px;
  background: none; border: none; cursor: pointer;
  color: var(--muted); font-size: var(--text-sm);
  white-space: nowrap;
}
.btn-back:hover { color: var(--fg); }
.topbar-center { flex: 1; min-width: 0; }
.player-title {
  font-size: var(--text-lg); font-weight: 600; margin: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.topbar-right { display: flex; align-items: center; gap: var(--space-3); }

.save-status { display: flex; align-items: center; gap: 6px; font-size: var(--text-xs); color: var(--muted); white-space: nowrap; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--success); }
.save-status.dirty .status-dot { background: var(--accent); }
.save-status.saving .status-dot { animation: pulse 0.8s infinite; }
.save-status.conflict .status-dot { background: var(--error); }
.save-status.saved .status-dot { background: var(--success); }

/* 菜单容器确保相对定位 */
.menu-container { position: relative; }
.btn-icon {
  background: none; border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 4px 8px; cursor: pointer; color: var(--muted);
}
.btn-icon:hover { border-color: var(--border-strong); color: var(--fg); }
.menu-dropdown {
  position: absolute; right: 0; top: calc(100% + 4px); z-index: 10;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-md); box-shadow: var(--shadow-md);
  display: flex; flex-direction: column; min-width: 160px;
}
.menu-item {
  background: none; border: none; padding: 8px 16px; text-align: left;
  cursor: pointer; font-size: var(--text-sm); color: var(--fg);
}
.menu-item:hover { background: var(--surface-subtle); }
.menu-item:disabled { opacity: .4; cursor: not-allowed; }
.menu-divider { border-top: 1px solid var(--border); margin: 4px 0; }
.menu-submit { color: var(--accent); font-weight: 500; }

.submit-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; margin-bottom: var(--space-3);
  background: var(--surface-subtle); border-radius: var(--radius-md);
  font-size: var(--text-xs); color: var(--muted);
  flex-wrap: wrap;
}
.submit-status.submitting { color: var(--accent); }
.submit-status.submitted { color: var(--success); }
.submit-history-hint { opacity: 0.7; }

.player-desc { font-size: var(--text-sm); color: var(--muted); margin-bottom: var(--space-4); }

/* Phase 5：学生端环境提示条 */
.player-env {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 12px; margin-bottom: var(--space-3);
  background: var(--surface-subtle); border: 1px solid var(--border);
  border-left: 3px solid var(--accent); border-radius: var(--radius-md);
  font-size: var(--text-xs); color: var(--muted);
}
.env-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); flex-shrink: 0; }
.env-name { color: var(--fg); font-weight: 600; white-space: nowrap; }
.env-imports { font-family: var(--font-mono); opacity: 0.85; }
.player-empty { text-align: center; padding: var(--space-12); color: var(--muted); }
.cell-wrapper { margin-bottom: var(--space-3); }

@media (max-width: 768px) {
  .notebook-player { max-width: 100%; padding: 0 var(--space-2); }
  .player-title { font-size: var(--text-base); }
}

@media (max-width: 600px) {
  .player-topbar { display: grid; grid-template-columns: minmax(0, 1fr) auto; }
  .topbar-center { grid-row: 2; grid-column: 1 / -1; }
  .topbar-right { justify-content: flex-end; }
  .btn-back { justify-self: start; }
}

.notebook-player { max-width: 1180px; }
.player-topbar { position: sticky; top: var(--header-height); z-index: 15; padding: 12px 0; background: var(--bg); }
.player-title { font-size: 24px; white-space: normal; overflow-wrap: anywhere; }
.player-description { max-width: 80ch; line-height: 1.7; }
</style>
