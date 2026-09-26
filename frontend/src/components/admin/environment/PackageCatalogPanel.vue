<script setup>
// 库清单 tab：受控包目录管理
// 表单只包含包元数据（包名/版本/import 名/分类/来源），无 Dockerfile/requirements/pip 参数输入
import { ref, onMounted } from 'vue'
import { environmentsAPI } from '../../../api/environments.js'
import { useAppStore } from '../../../stores/app.js'
import { statusBadge } from '../../../utils/status.js'

const props = defineProps({
  readOnly: { type: Boolean, default: false },
})

const app = useAppStore()
const packages = ref([])
const loading = ref(true)
const showCreate = ref(false)
const editing = ref(null)
const formError = ref('')
const formFieldErrors = ref([])

const SOURCE_MAP = { pypi: 'PyPI', pytorch_cpu: 'PyTorch CPU' }
const PKG_STATUS_MAP = {
  active: { label: '正常', color: 'success' },
  inactive: { label: '已停用', color: 'neutral' },
}

const createForm = ref({
  pip_name: '',
  locked_version: '',
  import_names: '',
  category_tags: '',
  source_key: 'pypi',
})
const editForm = ref({ category_tags: '', status: 'active' })

async function fetch() {
  loading.value = true
  try {
    const res = await environmentsAPI.listPackages()
    packages.value = res.data || []
  } catch {
    app.showToast('加载失败', 'error')
  } finally {
    loading.value = false
  }
}

function parseList(value) {
  return value
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

async function handleCreate() {
  if (!createForm.value.pip_name || !createForm.value.locked_version) return
  formError.value = ''
  formFieldErrors.value = []
  try {
    await environmentsAPI.createPackage({
      pip_name: createForm.value.pip_name,
      locked_version: createForm.value.locked_version,
      import_names: parseList(createForm.value.import_names),
      category_tags: parseList(createForm.value.category_tags),
      source_key: createForm.value.source_key,
    })
    app.showToast('包已创建', 'success')
    showCreate.value = false
    createForm.value = { pip_name: '', locked_version: '', import_names: '', category_tags: '', source_key: 'pypi' }
    fetch()
  } catch (e) {
    const detail = e.response?.data?.detail || {}
    formError.value = detail.message || '创建失败'
    formFieldErrors.value = detail.field_errors || []
    app.showToast(formError.value, 'error')
  }
}

function startEdit(pkg) {
  editing.value = pkg
  editForm.value = {
    category_tags: (pkg.category_tags || []).join(', '),
    status: pkg.status,
  }
}

async function handleEditSave() {
  if (!editing.value) return
  const patch = {
    category_tags: parseList(editForm.value.category_tags),
    status: editForm.value.status,
  }
  // 未引用的包允许一并修改核心字段
  if (!editing.value.referenced) {
    patch.pip_name = editing.value.pip_name
    patch.locked_version = editing.value.locked_version
    patch.import_names = editing.value.import_names
    patch.source_key = editing.value.source_key
  }
  try {
    await environmentsAPI.updatePackage(editing.value.id, patch)
    app.showToast('已保存', 'success')
    editing.value = null
    fetch()
  } catch (e) {
    app.showToast(e.response?.data?.detail?.message || '保存失败', 'error')
  }
}

async function handleDeactivate(pkg) {
  try {
    await environmentsAPI.deactivatePackage(pkg.id)
    app.showToast('包已停用', 'success')
    fetch()
  } catch (e) {
    app.showToast(e.response?.data?.detail?.message || '操作失败', 'error')
  }
}

onMounted(fetch)
</script>

<template>
  <div class="panel">
    <!-- ── 操作条 ─────────────────────────────────────────────────────── -->
    <div class="panel-bar">
      <p class="panel-hint">受控包目录：{{ props.readOnly ? 'V2 兼容期只读审计入口。' : '包名、版本与 import 名严格校验；已引用包不可原地修改，需创建新目录版本。' }}</p>
      <button v-if="!props.readOnly" class="btn-primary create-pkg-btn" @click="showCreate = !showCreate; formError = ''; formFieldErrors = []">
        {{ showCreate ? '取消' : '新建包' }}
      </button>
    </div>

    <!-- ── 新建包表单（只有元数据字段） ─────────────────────────────── -->
    <div v-if="showCreate && !props.readOnly" class="card pkg-form">
      <div v-if="formError" class="form-error" role="alert">
        <strong>{{ formError }}</strong>
        <ul v-if="formFieldErrors.length">
          <li v-for="field in formFieldErrors" :key="`${field.path}-${field.code}`">{{ field.path }}：{{ field.message }}</li>
        </ul>
      </div>
      <div class="form-grid">
        <div class="form-group">
          <label>包名 *</label>
          <input v-model="createForm.pip_name" name="pip_name" placeholder="如 numpy" />
        </div>
        <div class="form-group">
          <label>版本 *</label>
          <input v-model="createForm.locked_version" name="locked_version" placeholder="精确版本，如 2.1.3" />
        </div>
        <div class="form-group">
          <label>import 名</label>
          <input v-model="createForm.import_names" name="import_names" placeholder="逗号分隔，如 numpy, numpy.testing" />
        </div>
        <div class="form-group">
          <label>分类</label>
          <input v-model="createForm.category_tags" name="category_tags" placeholder="逗号分隔，如 data" />
        </div>
        <div class="form-group">
          <label>来源</label>
          <DsSelect v-model="createForm.source_key" name="source_key">
            <option value="pypi">PyPI</option>
            <option value="pytorch_cpu">PyTorch CPU（官方 CPU index）</option>
          </DsSelect>
        </div>
      </div>
      <div class="form-actions">
        <button class="btn-primary submit-btn" @click="handleCreate">确认创建</button>
      </div>
    </div>

    <!-- ── 编辑面板 ──────────────────────────────────────────────────── -->
    <div v-if="editing && !props.readOnly" class="card pkg-form">
      <h3 class="form-title">编辑包：{{ editing.pip_name }}=={{ editing.locked_version }}</h3>
      <p v-if="editing.referenced" class="immutable-hint">
        ⚠️ 该包已被环境版本引用：修改将创建新目录版本，历史环境不变。
      </p>
      <p v-else class="immutable-hint muted">
        该包未被任何环境版本引用，可修改分类与状态；核心字段变更将原地生效。
      </p>
      <div class="form-grid">
        <div class="form-group">
          <label>分类</label>
          <input v-model="editForm.category_tags" name="category_tags" placeholder="逗号分隔" />
        </div>
        <div class="form-group">
          <label>状态</label>
          <DsSelect v-model="editForm.status" name="status">
            <option value="active">正常</option>
            <option value="inactive">停用</option>
          </DsSelect>
        </div>
      </div>
      <div class="form-actions">
        <button class="btn-primary submit-btn" @click="handleEditSave">保存</button>
        <button class="btn-ghost" @click="editing = null">取消</button>
      </div>
    </div>

    <!-- ── 表格 ──────────────────────────────────────────────────────── -->
    <div v-if="loading" class="card table-card">
      <div class="skeleton-row" v-for="i in 4" :key="i">
        <div class="skeleton skel-cell w-15"></div>
        <div class="skeleton skel-cell w-10"></div>
        <div class="skeleton skel-cell w-20"></div>
        <div class="skeleton skel-cell w-15"></div>
        <div class="skeleton skel-cell w-10"></div>
        <div class="skeleton skel-cell w-20"></div>
      </div>
    </div>
    <div v-else-if="packages.length === 0" class="empty-state">
      <p>📦 暂无包目录条目</p>
    </div>
    <div v-else class="card table-card">
      <table>
        <thead>
          <tr><th>包名</th><th>版本</th><th>import 名</th><th>分类</th><th>来源</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="pkg in packages" :key="pkg.id">
            <td>
              {{ pkg.pip_name }}
              <span v-if="pkg.referenced" class="refer-tag" title="已被环境版本引用">已引用</span>
            </td>
            <td class="mono">{{ pkg.locked_version }}</td>
            <td class="mono text-secondary">{{ (pkg.import_names || []).join(', ') }}</td>
            <td>{{ (pkg.category_tags || []).join(', ') }}</td>
            <td>{{ SOURCE_MAP[pkg.source_key] || pkg.source_key }}</td>
            <td>
              <span class="badge" :class="'badge-' + statusBadge(PKG_STATUS_MAP, pkg.status).color">
                {{ statusBadge(PKG_STATUS_MAP, pkg.status).label }}
              </span>
            </td>
            <td class="actions-cell">
              <button v-if="!props.readOnly" class="btn-ghost btn-sm edit-pkg-btn" @click="startEdit(pkg)">编辑</button>
              <button
                v-if="pkg.status === 'active' && !props.readOnly"
                class="btn-sm deactivate-btn"
                @click="handleDeactivate(pkg)"
              >停用</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; gap: 16px; }
.panel-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.panel-hint { margin: 0; font-size: var(--text-xs); color: var(--muted); }
.pkg-form { padding: 16px; }
.form-error { margin-bottom: 12px; padding: 9px 11px; border-radius: var(--radius-control, 8px); background: color-mix(in srgb, var(--danger, #b42318) 12%, var(--surface)); color: var(--fg); font-size: var(--text-sm, 13px); }
.form-error ul { margin: 5px 0 0; padding-left: 18px; }
.form-title { margin: 0 0 8px; font-size: 15px; }
.immutable-hint { margin: 0 0 12px; font-size: var(--text-sm); color: var(--warning, var(--warning)); }
.immutable-hint.muted { color: var(--faint); }
.form-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px;
}
.form-actions { display: flex; gap: 8px; margin-top: 14px; }
.refer-tag {
  display: inline-block; margin-left: 6px; padding: 1px 6px;
  font-size: 11px; border-radius: var(--radius-sm);
  background: var(--accent-soft); color: var(--accent);
}
.mono { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; }
.actions-cell {
  display: table-cell;
  vertical-align: middle;
  white-space: nowrap;
}
.actions-cell button + button { margin-left: 8px; }
</style>
