<script setup>
import { vDialogFocus } from '../../../directives/dialogFocus.js'
// V2 环境编辑器：列表与单个草稿编辑器保持在同一页面，旧 PackageCatalog 写流程不参与。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { environmentsAPI } from '../../../api/environments.js'
import { useAppStore } from '../../../stores/app.js'
import { statusBadge } from '../../../utils/status.js'

const props = defineProps({
  profiles: { type: Array, default: () => [] },
})
const emit = defineEmits(['refresh'])

const app = useAppStore()
const profileList = ref(null)
let listFrame = 0
let listResizeObserver = null
function updateListHeight() {
  cancelAnimationFrame(listFrame)
  listFrame = requestAnimationFrame(() => {
    const list = profileList.value
    if (!list) return
    const top = Math.max(72, list.getBoundingClientRect().top)
    list.style.setProperty('--profile-list-height', `${Math.max(180, window.innerHeight - top - 24)}px`)
  })
}
onMounted(() => {
  window.addEventListener('scroll', updateListHeight, { passive: true })
  window.addEventListener('resize', updateListHeight)
  if (typeof ResizeObserver !== 'undefined') {
    listResizeObserver = new ResizeObserver(updateListHeight)
    if (profileList.value?.parentElement) listResizeObserver.observe(profileList.value.parentElement)
  }
  updateListHeight()
})
onBeforeUnmount(() => {
  window.removeEventListener('scroll', updateListHeight)
  window.removeEventListener('resize', updateListHeight)
  listResizeObserver?.disconnect()
  cancelAnimationFrame(listFrame)
})

const options = ref(null)
const readiness = ref(null)
const selectedId = ref(null)
const detail = ref(null)
const loadingDetail = ref(false)
const saving = ref(false)
const building = ref(false)
const publishing = ref(false)
const rollingBackId = ref(null)
const retrying = ref(false)
const archiving = ref(false)
const abandoning = ref(false)
const errorMessage = ref('')
const fieldErrors = ref([])
const conflict = ref(null)
const showLog = ref(false)
const logText = ref('')
const logLoading = ref(false)
const showCreate = ref(false)
const createTrigger = ref(null)
const createNameInput = ref(null)
const createError = ref('')
const createForm = reactive({ display_name: '', description: '' })
const profileForm = reactive({ display_name: '', description: '' })
const localSnapshot = ref(null)
let pollTimer = null
let detailRequestSequence = 0
let detailAbortController = null
let pollInFlight = false

const draftForm = reactive({
  revision: 1,
  python_version: '3.12',
  minimum_memory_mb: 256,
  python_packages: [],
  system_packages: [],
})
const newPython = reactive({ name: '', version: '' })
const newSystem = reactive({ name: '', version: '' })
const candidateResults = reactive({ pip: [], apt: [] })
const candidateLoading = reactive({ pip: false, apt: false })

const currentProfile = computed(() => detail.value)
const draft = computed(() => currentProfile.value?.draft || null)
const capabilities = computed(() => draft.value?.capabilities || currentProfile.value?.capabilities || {})
const savedSpec = computed(() => draft.value?.requested_spec || { schema_version: 1, python_packages: [], system_packages: [] })
const draftSpec = computed(() => ({
  schema_version: 1,
  python_packages: draftForm.python_packages,
  system_packages: draftForm.system_packages,
}))
const dirty = computed(() => Boolean(draft.value) && (
  draftForm.python_version !== draft.value.python_version
  || draftForm.minimum_memory_mb !== draft.value.minimum_memory_mb
  || JSON.stringify(draftSpec.value) !== JSON.stringify(savedSpec.value)
))
const profileDirty = computed(() => Boolean(currentProfile.value) && (
  profileForm.display_name !== (currentProfile.value.display_name || '')
  || profileForm.description !== (currentProfile.value.description || '')
))
// A local clean draft is not enough: the API must also report a live builder
// heartbeat.  Keeping this gate closed while readiness is loading/unknown
// avoids creating jobs that no worker can consume.
const hasBuildableDraft = computed(() => (
  Boolean(draft.value)
  && readiness.value?.ready === true
  && !dirty.value
  && !profileDirty.value
  && capabilities.value.can_build
))
const candidateVersion = computed(() => (currentProfile.value?.versions || []).find((version) => version.id === draft.value?.candidate_version_id))
const errorDetailText = computed(() => {
  const detailError = currentProfile.value?.recent_build?.error_detail
  return detailError ? JSON.stringify(detailError, null, 2) : ''
})

function diffCount(diff, key) {
  const item = diff?.[key]
  if (!item) return 0
  return (item.added || []).length + (item.removed || []).length + (item.changed || []).length
}

const PROFILE_STATUS_MAP = {
  active: { label: '正常', color: 'success' },
  inactive: { label: '已归档', color: 'neutral' },
}
const VERSION_STATUS_MAP = {
  queued: { label: '排队中', color: 'info' },
  building: { label: '构建中', color: 'warning' },
  available: { label: '待发布', color: 'success' },
  failed: { label: '失败', color: 'danger' },
}

function clone(value) {
  return JSON.parse(JSON.stringify(value))
}

function hydrateDraft(value) {
  const spec = value?.requested_spec || { schema_version: 1, python_packages: [], system_packages: [] }
  draftForm.revision = value?.revision || 1
  draftForm.python_version = value?.python_version || options.value?.default_python_version || '3.12'
  draftForm.minimum_memory_mb = value?.minimum_memory_mb || options.value?.default_memory_mb || 256
  draftForm.python_packages = clone(spec.python_packages || [])
  draftForm.system_packages = clone(spec.system_packages || [])
  localSnapshot.value = clone(draftSpec.value)
}

function clearError() {
  errorMessage.value = ''
  fieldErrors.value = []
}

function showApiError(error, fallback = '操作失败') {
  const detailError = error?.response?.data?.detail || {}
  errorMessage.value = detailError.message || fallback
  fieldErrors.value = detailError.field_errors || []
  if (detailError.code === 'DRAFT_REVISION_CONFLICT') {
    conflict.value = { code: detailError.code }
  }
}

async function loadDetail(id = selectedId.value) {
  if (!id) return
  selectedId.value = id
  const sequence = ++detailRequestSequence
  detailAbortController?.abort()
  detailAbortController = new AbortController()
  loadingDetail.value = true
  clearError()
  try {
    const res = await environmentsAPI.getProfile(id, { signal: detailAbortController.signal })
    if (sequence !== detailRequestSequence || selectedId.value !== id) return
    detail.value = res.data
    profileForm.display_name = detail.value?.display_name || ''
    profileForm.description = detail.value?.description || ''
    hydrateDraft(detail.value?.draft)
    showLog.value = false
    logText.value = ''
  } catch (error) {
    if (error?.code === 'ERR_CANCELED' || error?.name === 'CanceledError') return
    if (sequence !== detailRequestSequence || selectedId.value !== id) return
    showApiError(error, '无法加载环境详情')
  } finally {
    if (sequence === detailRequestSequence) loadingDetail.value = false
  }
}

async function createProfile() {
  if (!createForm.display_name.trim()) return
  createError.value = ''
  clearError()
  try {
    const res = await environmentsAPI.createProfile({
      display_name: createForm.display_name.trim(),
      description: createForm.description.trim() || null,
      slug: null,
    })
    createForm.display_name = ''
    createForm.description = ''
    closeCreateModal()
    emit('refresh')
    await loadDetail(res.data?.id)
    app.showToast('环境草稿已创建', 'success')
  } catch (error) {
    const detailError = error?.response?.data?.detail || {}
    createError.value = detailError.message || '创建环境失败'
    showApiError(error, '创建环境失败')
  }
}

function openCreateModal() {
  createError.value = ''
  clearError()
  showCreate.value = true
  nextTick(() => createNameInput.value?.focus())
}

function closeCreateModal() {
  showCreate.value = false
  nextTick(() => createTrigger.value?.focus())
}

function addPythonPackage() {
  const name = newPython.name.trim()
  if (!name || draftForm.python_packages.some((item) => item.name === name)) return
  draftForm.python_packages.push({ name, version: newPython.version.trim() || null, import_names: [] })
  newPython.name = ''
  newPython.version = ''
}

function addSystemPackage() {
  const name = newSystem.name.trim()
  if (!name || draftForm.system_packages.some((item) => item.name === name)) return
  draftForm.system_packages.push({ name, version: newSystem.version.trim() || null })
  newSystem.name = ''
  newSystem.version = ''
}

async function searchCandidates(manager) {
  const query = manager === 'pip' ? newPython.name.trim() : newSystem.name.trim()
  if (!query || !draftForm.python_version) return
  candidateLoading[manager] = true
  try {
    const res = await environmentsAPI.listPackageCandidates({
      manager,
      q: query,
      python_version: draftForm.python_version,
    })
    candidateResults[manager] = res.data || []
  } catch (error) {
    showApiError(error, '包候选搜索失败')
  } finally {
    candidateLoading[manager] = false
  }
}

function useCandidate(manager, candidate, version = '') {
  if (manager === 'pip') {
    newPython.name = candidate.name
    newPython.version = version
  } else {
    newSystem.name = candidate.name
    newSystem.version = version
  }
}

function removePackage(manager, index) {
  draftForm[manager].splice(index, 1)
}

function setImportNames(item, value) {
  item.import_names = value
    .split(/[,，]/)
    .map((name) => name.trim())
    .filter(Boolean)
}

async function save() {
  if (!draft.value || saving.value) return
  saving.value = true
  clearError()
  try {
    const res = await environmentsAPI.saveDraft(selectedId.value, {
      revision: draftForm.revision,
      python_version: draftForm.python_version,
      minimum_memory_mb: draftForm.minimum_memory_mb,
      requested_spec: draftSpec.value,
    })
    detail.value = { ...detail.value, draft: res.data }
    hydrateDraft(res.data)
    conflict.value = null
    app.showToast('草稿已保存', 'success')
  } catch (error) {
    showApiError(error, '保存草稿失败')
  } finally {
    saving.value = false
  }
}

async function saveProfile() {
  if (!currentProfile.value || !profileDirty.value) return
  clearError()
  try {
    const res = await environmentsAPI.updateProfile(selectedId.value, {
      display_name: profileForm.display_name.trim(),
      description: profileForm.description.trim() || null,
    })
    detail.value = {
      ...detail.value,
      display_name: res.data?.display_name ?? profileForm.display_name.trim(),
      description: res.data?.description ?? (profileForm.description.trim() || null),
    }
    profileForm.display_name = detail.value.display_name
    profileForm.description = detail.value.description || ''
    emit('refresh')
    app.showToast('环境基本信息已保存', 'success')
  } catch (error) {
    showApiError(error, '保存环境基本信息失败')
  }
}

async function createDraft() {
  if (!currentProfile.value || !capabilities.value.can_create_draft) return
  clearError()
  try {
    const res = await environmentsAPI.createDraft(selectedId.value)
    detail.value = { ...detail.value, draft: res.data }
    hydrateDraft(res.data)
    emit('refresh')
  } catch (error) {
    showApiError(error, '创建草稿失败')
  }
}

async function abandonDraft() {
  if (!draft.value || !capabilities.value.can_abandon_draft || abandoning.value) return
  abandoning.value = true
  clearError()
  try {
    await environmentsAPI.abandonDraft(selectedId.value, draft.value.revision)
    app.showToast('草稿已放弃', 'success')
    emit('refresh')
    await loadDetail(selectedId.value)
  } catch (error) {
    showApiError(error, '放弃草稿失败')
  } finally {
    abandoning.value = false
  }
}

async function refreshDetail() {
  if (!selectedId.value) return
  if (pollInFlight) return
  pollInFlight = true
  try {
    await loadDetail(selectedId.value)
    const state = detail.value?.draft?.state
    if (state !== 'building') stopPolling()
  } finally {
    pollInFlight = false
  }
}

async function build() {
  if (!hasBuildableDraft.value || building.value) return
  building.value = true
  clearError()
  try {
    await environmentsAPI.buildDraft(selectedId.value)
    app.showToast('构建已入队，完成后可检查报告', 'success')
    await refreshDetail()
    startPolling()
  } catch (error) {
    showApiError(error, '构建提交失败')
  } finally {
    building.value = false
  }
}

async function publish() {
  const candidate = detail.value?.draft?.candidate_version_id
  if (!candidate || !capabilities.value.can_publish || publishing.value) return
  publishing.value = true
  try {
    await environmentsAPI.publish(selectedId.value, {
      environment_version_id: candidate,
      expected_current_version_id: detail.value.current_version?.id || null,
    })
    app.showToast('环境版本已发布', 'success')
    emit('refresh')
    await loadDetail(selectedId.value)
  } catch (error) {
    showApiError(error, '发布失败')
  } finally {
    publishing.value = false
    await refreshDetail()
  }
}

async function rollback(version) {
  if (!version?.published || version.current || !capabilities.value.can_rollback || rollingBackId.value !== null) return
  rollingBackId.value = version.id
  try {
    await environmentsAPI.publish(selectedId.value, {
      environment_version_id: version.id,
      expected_current_version_id: detail.value.current_version?.id || null,
    })
    app.showToast(`已回滚到 v${version.version_number}`, 'success')
    emit('refresh')
    await loadDetail(selectedId.value)
  } catch (error) {
    showApiError(error, '回滚失败')
  } finally {
    rollingBackId.value = null
    await refreshDetail()
  }
}

async function retry() {
  const job = detail.value?.recent_build
  if (!job?.capabilities?.can_retry || retrying.value) return
  retrying.value = true
  try {
    await environmentsAPI.retryBuild(job.id)
    app.showToast('已重新入队', 'success')
    await refreshDetail()
    startPolling()
  } catch (error) {
    showApiError(error, '重试失败')
  } finally {
    retrying.value = false
    await refreshDetail()
  }
}

async function loadLog() {
  const job = detail.value?.recent_build
  if (!job) return
  logLoading.value = true
  try {
    const res = await environmentsAPI.getBuildLog(job.id)
    logText.value = res.data?.log_text || ''
    showLog.value = true
  } catch (error) {
    showApiError(error, '无法加载构建日志')
  } finally {
    logLoading.value = false
  }
}

async function setProfileStatus(active) {
  if (archiving.value) return
  archiving.value = true
  try {
    await environmentsAPI.updateProfile(selectedId.value, { status: active ? 'active' : 'inactive' })
    emit('refresh')
    await loadDetail(selectedId.value)
  } catch (error) {
    showApiError(error, active ? '恢复失败' : '归档失败')
  } finally {
    archiving.value = false
    await refreshDetail()
  }
}

function useServerDraft() {
  conflict.value = null
  refreshDetail()
}

async function reapplyLocalDraft() {
  if (!conflict.value || !detail.value?.draft) return
  const serverRevision = detail.value.draft.revision
  conflict.value = null
  try {
    const res = await environmentsAPI.saveDraft(selectedId.value, {
      revision: serverRevision,
      python_version: draftForm.python_version,
      minimum_memory_mb: draftForm.minimum_memory_mb,
      requested_spec: draftSpec.value,
    })
    detail.value = { ...detail.value, draft: res.data }
    hydrateDraft(res.data)
  } catch (error) {
    showApiError(error, '重新应用本地修改失败')
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(refreshDetail, 2500)
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

async function loadSupportData() {
  try {
    const [optionRes, readinessRes] = await Promise.all([
      environmentsAPI.getEditorOptions(),
      environmentsAPI.getBuildReadiness(),
    ])
    options.value = optionRes.data
    readiness.value = readinessRes.data
  } catch (error) {
    showApiError(error, '构建服务状态暂时不可用')
  }
}

watch(
  () => selectedId.value,
  (id) => {
    if (id && !detail.value) loadDetail(id)
  },
)

watch(
  () => props.profiles,
  (profiles) => {
    if (profiles?.length && !profiles.some((profile) => profile.id === selectedId.value)) {
      selectProfile(profiles[0])
    }
  },
  { immediate: true },
)

onMounted(async () => {
  await loadSupportData()
  if (selectedId.value === null) {
    // The parent owns the list fetch; the first row is selected when the prop
    // arrives through this watcher in the template-level effect below.
  }
})
onBeforeUnmount(() => {
  stopPolling()
  detailRequestSequence += 1
  detailAbortController?.abort()
})

// Expose a small imperative hook for the list click handlers without adding a
// second state store; Vue's template can call this local function directly.
function selectProfile(profile) {
  if (profile.id === selectedId.value && detail.value) return
  stopPolling()
  detail.value = null
  loadDetail(profile.id)
}
</script>

<template>
  <div class="v2-editor">
    <section class="v2-toolbar">
      <div>
        <h2>环境列表</h2>
        <p class="muted">声明依赖、构建不可变镜像，检查报告后再发布给教师。</p>
      </div>
      <button ref="createTrigger" class="btn-primary" type="button" :aria-expanded="showCreate" @click="openCreateModal">
        新建环境
      </button>
    </section>

    <div v-if="readiness && !readiness.ready" class="notice notice-warning" role="status">
      <strong>构建服务需要关注：</strong>
      <span v-for="(check, key) in readiness.checks" :key="key" v-show="check.status !== 'healthy' && check.status !== 'configured'">
        {{ check.message }}
      </span>
    </div>

    <div v-if="showCreate" class="modal-backdrop create-env-backdrop" @click.self="closeCreateModal">
      <form
        class="modal create-env-modal"
        v-dialog-focus role="dialog"
        aria-modal="true"
        aria-labelledby="create-env-title"
        @keydown.esc="closeCreateModal"
        @submit.prevent="createProfile"
      >
        <header class="modal-head">
          <h2 id="create-env-title">新建环境</h2>
          <button class="btn btn-ghost btn-sm btn-icon" type="button" aria-label="关闭" @click="closeCreateModal">×</button>
        </header>
        <div class="modal-body">
          <div v-if="createError" class="notice notice-danger create-error" role="alert">
            {{ createError }}
          </div>
          <div class="form-grid">
            <div class="form-group">
              <label for="v2-display-name">环境名称</label>
              <input ref="createNameInput" id="v2-display-name" v-model="createForm.display_name" autocomplete="off" />
            </div>
            <div class="form-group">
              <label for="v2-description">描述</label>
              <input id="v2-description" v-model="createForm.description" autocomplete="off" />
            </div>
          </div>
        </div>
        <footer class="modal-foot">
          <button class="btn btn-ghost" type="button" @click="closeCreateModal">取消</button>
          <button class="btn btn-primary" type="submit" :disabled="!createForm.display_name.trim()">创建并编辑</button>
        </footer>
      </form>
    </div>

    <div class="v2-layout">
      <section ref="profileList" class="card profile-list" aria-labelledby="profile-list-title">
        <div class="section-head">
          <h3 id="profile-list-title">环境</h3>
          <span class="muted text-sm">{{ profiles.length }} 个</span>
        </div>
        <div v-if="!profiles.length" class="empty-state compact" role="status">还没有环境，先创建一个草稿。</div>
        <button
          v-for="profile in profiles"
          :key="profile.id"
          type="button"
          class="profile-row"
          :class="{ selected: selectedId === profile.id }"
          @click="selectProfile(profile)"
        >
          <span class="profile-row-main">
            <strong>{{ profile.display_name }}</strong>
            <small>{{ profile.description || profile.slug }}</small>
            <small class="profile-row-details">
              Python {{ profile.draft?.python_version || profile.current_version?.python_version || '—' }}
              · {{ (profile.draft?.requested_spec?.python_packages || profile.current_version?.requested_spec?.python_packages || []).length }} Python 包
              · {{ (profile.draft?.requested_spec?.system_packages || profile.current_version?.requested_spec?.system_packages || []).length }} 系统包
            </small>
          </span>
          <span class="profile-row-meta">
            <span v-if="profile.current_version" class="vnum">v{{ profile.current_version.version_number }}</span>
            <span v-else class="muted">未发布</span>
            <small v-if="profile.draft" class="muted">草稿：{{ profile.draft.state }}</small>
            <small v-if="profile.recent_build" class="muted">构建：{{ profile.recent_build.phase }}</small>
            <span class="badge" :class="'badge-' + statusBadge(PROFILE_STATUS_MAP, profile.status).color">
              {{ statusBadge(PROFILE_STATUS_MAP, profile.status).label }}
            </span>
          </span>
        </button>
      </section>

      <section class="card editor-card" aria-labelledby="editor-title">
        <div v-if="loadingDetail" class="empty-state compact" role="status">加载环境详情…</div>
        <div v-else-if="!currentProfile" class="empty-state compact" role="status">
          <strong id="editor-title">选择一个环境</strong>
          <span>草稿编辑器会显示在这里。</span>
        </div>
        <template v-else>
          <div class="section-head editor-title-row">
            <div>
              <p class="eyebrow">{{ currentProfile.slug }}</p>
              <h3 id="editor-title">{{ currentProfile.display_name }}</h3>
            </div>
            <div class="button-row">
              <button v-if="draft" class="btn-ghost btn-sm" type="button" :disabled="!capabilities.can_abandon_draft || abandoning" @click="abandonDraft">{{ abandoning ? '处理中…' : '放弃草稿' }}</button>
              <button v-if="currentProfile.status === 'active'" class="btn-ghost btn-sm" type="button" :disabled="!capabilities.can_archive || archiving" @click="setProfileStatus(false)">{{ archiving ? '处理中…' : '归档' }}</button>
              <button v-else class="btn-ghost btn-sm" type="button" :disabled="!capabilities.can_restore || archiving" @click="setProfileStatus(true)">{{ archiving ? '处理中…' : '恢复' }}</button>
            </div>
          </div>

          <div v-if="errorMessage" class="notice notice-danger" role="alert">
            <strong>{{ errorMessage }}</strong>
            <ul v-if="fieldErrors.length">
              <li v-for="field in fieldErrors" :key="`${field.path}-${field.code}`">{{ field.path }}：{{ field.message }}</li>
            </ul>
          </div>
          <div v-if="conflict" class="notice notice-warning" role="alert">
            草稿已被其他管理员更新。你的本地修改仍保留。
            <div class="button-row">
              <button class="btn-ghost btn-sm" type="button" @click="useServerDraft">使用服务器草稿</button>
              <button class="btn-primary btn-sm" type="button" @click="reapplyLocalDraft">基于最新 revision 重新应用</button>
            </div>
          </div>

          <section class="editor-section profile-settings">
            <div class="section-head"><h4>基础设置</h4><span v-if="dirty" class="dirty-label">有未保存修改</span></div>
            <div class="form-grid">
              <div class="form-group">
                <label for="v2-profile-name">环境名称</label>
                <input id="v2-profile-name" v-model="profileForm.display_name" :disabled="!capabilities.can_edit_profile" />
              </div>
              <div class="form-group">
                <label for="v2-profile-description">描述</label>
                <input id="v2-profile-description" v-model="profileForm.description" :disabled="!capabilities.can_edit_profile" />
              </div>
            </div>
            <div class="editor-actions profile-actions">
              <button class="btn-ghost" type="button" :disabled="!profileDirty || !profileForm.display_name.trim() || !capabilities.can_edit_profile" @click="saveProfile">保存基本信息</button>
              <span v-if="profileDirty" class="muted text-sm">名称或描述尚未保存</span>
            </div>
          </section>

          <div v-if="draft" class="editor-content">
            <section class="editor-section">
              <div class="section-head"><h4>依赖设置</h4><span class="muted text-sm">草稿修改会在下一次构建中生效</span></div>
              <div class="form-grid three">
                <div class="form-group">
                  <label for="v2-python">Python 版本</label>
                  <DsSelect id="v2-python" v-model="draftForm.python_version" :disabled="!capabilities.can_edit_draft">
                    <option v-for="version in (options?.python_versions || ['3.10', '3.11', '3.12'])" :key="version" :value="version">Python {{ version }}</option>
                  </DsSelect>
                </div>
                <div class="form-group">
                  <label for="v2-memory">最小内存（MB）</label>
                  <input id="v2-memory" v-model.number="draftForm.minimum_memory_mb" type="number" min="64" max="65536" :disabled="!capabilities.can_edit_draft" />
                </div>
                <div class="form-group readonly-field">
                  <span>草稿 revision</span>
                  <strong>r{{ draftForm.revision }}</strong>
                </div>
              </div>
            </section>

            <section class="editor-section">
              <div class="section-head"><h4>Python 包</h4><span class="muted text-sm">直接依赖 · 未填版本=最新兼容稳定版</span></div>
              <div class="add-row">
                <label class="sr-only" for="v2-python-name">Python 包名</label>
                <input id="v2-python-name" v-model="newPython.name" placeholder="包名，例如 numpy" :disabled="!capabilities.can_edit_draft" @keyup.enter="addPythonPackage" />
                <label class="sr-only" for="v2-python-version">Python 包版本</label>
                <input id="v2-python-version" v-model="newPython.version" placeholder="精确版本（可选）" :disabled="!capabilities.can_edit_draft" @keyup.enter="addPythonPackage" />
                <button class="btn-ghost" type="button" :disabled="!capabilities.can_edit_draft" @click="addPythonPackage">添加</button>
                <button class="btn-ghost btn-sm" type="button" :disabled="!capabilities.can_edit_draft || candidateLoading.pip" @click="searchCandidates('pip')">{{ candidateLoading.pip ? '搜索中…' : '搜索' }}</button>
              </div>
              <div v-if="candidateResults.pip.length" class="candidate-list" aria-label="Python 包候选">
                <div v-for="candidate in candidateResults.pip" :key="`pip-${candidate.name}`" class="candidate-item">
                  <button class="candidate-name" type="button" :disabled="candidate.denied" @click="useCandidate('pip', candidate)">{{ candidate.name }}（最新兼容版）</button>
                  <button v-for="version in candidate.versions" :key="`${candidate.name}-${version}`" class="candidate-version" type="button" :disabled="candidate.denied" @click="useCandidate('pip', candidate, version)">{{ version }}</button>
                  <small v-if="candidate.indexing" class="muted">候选索引生成中，构建时会再次验证</small>
                </div>
              </div>
              <ul class="dependency-list">
                <li v-for="(item, index) in draftForm.python_packages" :key="`${item.name}-${index}`">
                  <span class="dependency-main"><strong>{{ item.name }}</strong><small>{{ item.version ? `==${item.version}` : '最新兼容稳定版' }}</small><input class="import-names-input" :value="(item.import_names || []).join(', ')" placeholder="高级：import 名（逗号分隔）" :disabled="!capabilities.can_edit_draft" @input="setImportNames(item, $event.target.value)" /></span>
                  <button class="icon-button" type="button" :aria-label="`删除 ${item.name}`" :disabled="!capabilities.can_edit_draft" @click="removePackage('python_packages', index)">×</button>
                </li>
                <li v-if="!draftForm.python_packages.length" class="muted">暂无直接 Python 包</li>
              </ul>
            </section>

            <section class="editor-section">
              <div class="section-head"><h4>系统包</h4><span class="muted text-sm">Debian 快照 · 平台安全策略会在构建前校验</span></div>
              <div class="add-row">
                <label class="sr-only" for="v2-system-name">系统包名</label>
                <input id="v2-system-name" v-model="newSystem.name" placeholder="apt 包名，例如 ffmpeg" :disabled="!capabilities.can_edit_draft" @keyup.enter="addSystemPackage" />
                <label class="sr-only" for="v2-system-version">系统包版本</label>
                <input id="v2-system-version" v-model="newSystem.version" placeholder="快照版本（可选）" :disabled="!capabilities.can_edit_draft" @keyup.enter="addSystemPackage" />
                <button class="btn-ghost" type="button" :disabled="!capabilities.can_edit_draft" @click="addSystemPackage">添加</button>
                <button class="btn-ghost btn-sm" type="button" :disabled="!capabilities.can_edit_draft || candidateLoading.apt" @click="searchCandidates('apt')">{{ candidateLoading.apt ? '搜索中…' : '搜索' }}</button>
              </div>
              <div v-if="candidateResults.apt.length" class="candidate-list" aria-label="系统包候选">
                <div v-for="candidate in candidateResults.apt" :key="`apt-${candidate.name}`" class="candidate-item">
                  <button class="candidate-name" type="button" :disabled="candidate.denied" @click="useCandidate('apt', candidate)">{{ candidate.name }}（最新快照版）</button>
                  <button v-for="version in candidate.versions" :key="`${candidate.name}-${version}`" class="candidate-version" type="button" :disabled="candidate.denied" @click="useCandidate('apt', candidate, version)">{{ version }}</button>
                  <small v-if="candidate.description" class="muted">{{ candidate.description }}</small>
                  <small v-if="candidate.denied" class="text-danger">{{ candidate.deny_reason }}</small>
                  <small v-else-if="candidate.indexing" class="muted">apt 索引尚未生成，可直接输入后构建验证</small>
                </div>
              </div>
              <ul class="dependency-list">
                <li v-for="(item, index) in draftForm.system_packages" :key="`${item.name}-${index}`">
                  <span><strong>{{ item.name }}</strong><small>{{ item.version || '最新快照版本' }}</small></span>
                  <button class="icon-button" type="button" :aria-label="`删除 ${item.name}`" :disabled="!capabilities.can_edit_draft" @click="removePackage('system_packages', index)">×</button>
                </li>
                <li v-if="!draftForm.system_packages.length" class="muted">暂无直接系统包</li>
              </ul>
            </section>

            <div class="editor-actions">
              <button class="btn-primary" type="button" :disabled="!dirty || saving || !capabilities.can_edit_draft" @click="save">{{ saving ? '保存中…' : '保存草稿' }}</button>
              <button class="btn-build" type="button" :disabled="!hasBuildableDraft || building" @click="build">{{ building ? '提交中…' : '构建并解析' }}</button>
              <span v-if="dirty" class="muted text-sm">保存后才能构建</span>
            </div>

            <section v-if="draft.candidate_version_id" class="report-section">
              <div class="section-head"><h4>构建报告</h4><span class="badge" :class="'badge-' + statusBadge(VERSION_STATUS_MAP, candidateVersion?.status || 'available').color">{{ draft.state }}</span></div>
              <p v-if="draft.state === 'building'" class="muted">Worker 正在解析依赖和验证镜像，编辑区已锁定。</p>
              <p v-else-if="draft.state === 'failed'" class="text-danger">构建失败，请查看构建任务日志或修改草稿后重新构建。</p>
              <p v-if="detail.recent_build?.error_code" class="build-error-code text-danger">
                错误码：<code>{{ detail.recent_build.error_code }}</code>
              </p>
              <p v-else-if="draft.state === 'ready'" class="muted">候选版本已构建成功。确认报告后才会成为教师可选版本。</p>
              <p v-if="candidateVersion?.diff" class="muted diff-summary">
                相对来源版本：Python {{ candidateVersion.diff.python_version?.from || '新建' }} → {{ candidateVersion.diff.python_version?.to }}；
                {{ diffCount(candidateVersion.diff, 'python_packages') }} 个 Python 依赖变化，
                {{ diffCount(candidateVersion.diff, 'system_packages') }} 个系统依赖变化。
              </p>
              <dl v-if="candidateVersion?.build_report" class="report-facts">
                <div><dt>镜像大小</dt><dd>{{ candidateVersion.build_report.image_size_bytes || '—' }} bytes</dd></div>
                <div><dt>lock SHA256</dt><dd class="mono">{{ candidateVersion.build_report.lock_sha256 || '—' }}</dd></div>
                <div><dt>自动发现 import</dt><dd>{{ (candidateVersion.build_report.imports || []).join('、') || '—' }}</dd></div>
              </dl>
              <pre v-if="errorDetailText" class="build-error-detail">{{ errorDetailText }}</pre>
              <button v-if="detail.recent_build" class="btn-ghost btn-sm" type="button" :disabled="logLoading" @click="loadLog">{{ logLoading ? '加载日志…' : (showLog ? '刷新日志' : '查看日志') }}</button>
              <pre v-if="showLog" class="build-log">{{ logText || '暂无日志' }}</pre>
              <button v-if="capabilities.can_retry" class="btn-ghost" type="button" :disabled="retrying" @click="retry">{{ retrying ? '重试提交中…' : '重试最近一次构建' }}</button>
              <button v-if="capabilities.can_publish" class="btn-primary" type="button" :disabled="publishing" @click="publish">{{ publishing ? '发布中…' : '确认发布候选版本' }}</button>
            </section>

          </div>
          <div v-else class="notice notice-warning">
            <span>当前没有草稿。创建草稿后可以基于当前发布版本继续修改。</span>
            <button class="btn-primary btn-sm" type="button" :disabled="!capabilities.can_create_draft" @click="createDraft">创建草稿</button>
          </div>

          <section v-if="currentProfile.versions?.length" class="history-section">
            <div class="section-head"><h4>版本历史</h4><span class="muted text-sm">已发布版本内容不可修改</span></div>
            <div v-for="version in currentProfile.versions" :key="version.id" class="history-row">
              <span class="vnum">v{{ version.version_number }}</span>
              <span class="history-status">{{ version.python_version }} · {{ version.status }}</span>
              <small v-if="version.diff" class="muted">{{ diffCount(version.diff, 'python_packages') + diffCount(version.diff, 'system_packages') }} 项依赖变化</small>
              <span v-if="version.current" class="badge badge-success">当前</span>
              <span v-else-if="version.published" class="badge badge-neutral">曾发布</span>
              <button v-if="version.published && !version.current" class="btn-ghost btn-sm" type="button" :disabled="!capabilities.can_rollback || rollingBackId !== null" @click="rollback(version)">{{ rollingBackId === version.id ? '回滚中…' : '回滚' }}</button>
            </div>
          </section>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.v2-editor { display: flex; flex-direction: column; gap: 16px; }
.v2-toolbar, .section-head, .editor-title-row, .button-row, .editor-actions, .add-row { display: flex; align-items: center; gap: 12px; }
.v2-toolbar, .section-head, .editor-title-row { justify-content: space-between; }
.v2-toolbar h2, .section-head h3, .section-head h4, .editor-title-row h3 { margin: 0; }
.v2-toolbar h2 { font-size: 20px; }
.create-env-backdrop { z-index: 100; }
.create-env-modal { max-width: 640px; }
.create-error { margin-bottom: 12px; }
.create-env-modal .form-grid { margin: 0; }
.eyebrow { margin: 0 0 4px; color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .1em; }
.muted { color: var(--muted); }
.text-sm { font-size: var(--text-sm, 13px); }
.v2-layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 16px; align-items: start; }
.profile-list, .editor-card, .create-card { padding: 16px; }
.profile-list { display: flex; flex-direction: column; gap: 8px; }
.profile-list > .section-head { flex-shrink: 0; }
.profile-row { flex-shrink: 0; min-height: 84px; width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 10px; border: 1px solid transparent; background: transparent; border-radius: var(--radius-control, 8px); padding: 11px 10px; text-align: left; color: var(--fg); cursor: pointer; }
.profile-row:hover { background: var(--surface-subtle); }
.profile-row.selected { border-color: var(--accent); background: var(--accent-soft); }
.profile-row-main, .profile-row-meta { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.profile-row-main strong, .profile-row-main small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.profile-row-main small { color: var(--muted); }
.profile-row-details { font-size: 11px; }
.profile-row-meta { align-items: flex-end; white-space: nowrap; }
.vnum { padding: 2px 6px; border-radius: var(--radius-sm, 5px); background: var(--accent-soft); color: var(--accent); font-size: 12px; }
.editor-card { min-height: 0; }
.editor-content { display: flex; flex-direction: column; gap: 18px; }
.editor-section, .report-section { border-top: 1px solid var(--border); padding-top: 16px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
.form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-group { display: flex; flex-direction: column; gap: 5px; }
.form-group label, .readonly-field span { color: var(--muted); font-size: var(--text-sm, 13px); }
.form-group input, .form-group select, .add-row input { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-control, 8px); padding: 8px 10px; background: var(--surface); color: var(--fg); font: inherit; }
.readonly-field { justify-content: end; gap: 5px; }
.add-row { margin: 12px 0; }
.add-row input:first-of-type { flex: 1.2; }
.add-row input { flex: 1; }
.dependency-list { display: flex; flex-direction: column; gap: 6px; padding: 0; margin: 0; list-style: none; }
.dependency-list li { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 10px; border: 1px solid var(--border); border-radius: var(--radius-control, 8px); }
.dependency-list li span { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.dependency-main { flex-wrap: wrap; }
.dependency-list small { color: var(--muted); }
.import-names-input { min-width: 220px; flex: 1; border: 1px solid var(--border); border-radius: 5px; padding: 4px 7px; background: var(--surface); color: var(--fg); font: inherit; font-size: 12px; }
.candidate-list { display: flex; flex-direction: column; gap: 6px; margin: 8px 0 12px; }
.candidate-item { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 7px 9px; border: 1px dashed var(--border); border-radius: var(--radius-control, 8px); }
.candidate-name, .candidate-version { border: 0; border-radius: 5px; padding: 4px 7px; background: var(--surface-subtle); color: var(--fg); cursor: pointer; font: inherit; font-size: 12px; }
.candidate-name { color: var(--accent); font-weight: 600; }
.candidate-name:disabled, .candidate-version:disabled { cursor: not-allowed; opacity: .5; }
.icon-button { border: 0; background: transparent; color: var(--muted); font-size: 20px; cursor: pointer; }
.icon-button:hover { color: var(--danger, #b42318); }
.dirty-label { color: var(--warning, #9a6700); font-size: 12px; }
.btn-build { border: 1px solid var(--accent); color: var(--accent); background: var(--accent-soft); border-radius: var(--radius-control, 8px); padding: 8px 14px; font: inherit; cursor: pointer; }
.btn-build:disabled, button:disabled { cursor: not-allowed; opacity: .55; }
.report-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 12px 0 0; }
.report-facts div { min-width: 0; padding: 8px 10px; border: 1px solid var(--border); border-radius: var(--radius-control, 8px); }
.report-facts dt { color: var(--muted); font-size: 11px; }
.report-facts dd { margin: 4px 0 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.mono { font-family: var(--font-mono, ui-monospace, monospace); }
.build-error-detail, .build-log { margin: 10px 0 0; max-height: 180px; overflow: auto; padding: 10px; border-radius: var(--radius-control, 8px); background: var(--surface-subtle); color: var(--muted); font: 12px/1.5 var(--font-mono, ui-monospace, monospace); white-space: pre-wrap; word-break: break-word; }
.notice { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; border-radius: var(--radius-control, 8px); padding: 10px 12px; font-size: var(--text-sm, 13px); }
.notice-warning { background: color-mix(in srgb, var(--warning, #9a6700) 12%, var(--surface)); color: var(--fg); }
.notice-danger { background: color-mix(in srgb, var(--danger, #b42318) 12%, var(--surface)); color: var(--fg); }
.history-section { border-top: 1px solid var(--border); padding-top: 16px; }
.history-row { display: flex; align-items: center; gap: 10px; min-height: 38px; border-bottom: 1px solid var(--border); }
.history-status { flex: 1; color: var(--muted); font-size: var(--text-sm, 13px); }
.notice ul { width: 100%; margin: 0; padding-left: 20px; }
.empty-state.compact { display: flex; flex-direction: column; gap: 6px; padding: 34px 12px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (max-width: 900px) { .v2-layout { grid-template-columns: 1fr; } }
@media (max-width: 640px) { .form-grid, .form-grid.three, .report-facts { grid-template-columns: 1fr; } .add-row { flex-wrap: wrap; } .add-row input { min-width: 40%; } .v2-toolbar { align-items: flex-start; } }

@media (min-width: 1100px) {
  .profile-list { position: sticky; top: 72px; height: var(--profile-list-height, calc(100dvh - 96px)); overflow-y: auto; overscroll-behavior: contain; }
  .v2-toolbar { gap: 8px; }
  .v2-toolbar h2 { font-size: var(--text-lg); }
}
</style>
