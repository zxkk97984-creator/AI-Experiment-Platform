<script setup>
import { ref, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { accountAPI, errorMessage } from '../../api/accountManagement.js'
import { usersAPI } from '../../api/users.js'
import { useAppStore } from '../../stores/app.js'
import { statusBadge, ROLE_MAP, USER_STATUS_MAP } from '../../utils/status.js'

const router = useRouter()
const app = useAppStore()
const users = ref([])
const selected = ref([])
const batchBusy = ref(false)
const loading = ref(true)
const total = ref(0)
const page = ref(1)
const filters = ref({ query: '', role: '', status: '' })
let searchTimer

function requestParams() {
  return {
    page: page.value,
    page_size: 20,
    q: filters.value.query.trim() || undefined,
    role: filters.value.role || undefined,
    status_filter: filters.value.status || undefined,
  }
}

async function fetch() {
  loading.value = true
  selected.value = []
  try {
    const res = await usersAPI.list(requestParams())
    users.value = res.data.items; total.value = res.data.total
  }
  catch { app.showToast('加载失败', 'error') }
  finally { loading.value = false }
}

function resetAndFetch() {
  page.value = 1
  fetch()
}

watch(() => filters.value.query, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(resetAndFetch, 250)
})

onBeforeUnmount(() => clearTimeout(searchTimer))

async function toggleStatus(u) {
  const newStatus = u.status === 'active' ? 'disabled' : 'active'
  try { await usersAPI.updateStatus(u.id, { status: newStatus }); app.showToast('状态已更新', 'success'); fetch() }
  catch { app.showToast('操作失败', 'error') }
}

onMounted(fetch)
async function resetSelected(allMatching = false) {
  batchBusy.value = true
  try {
    const selection = allMatching ? { all_matching: true, q: filters.value.query.trim() || null, status_filter: filters.value.status || null } : { student_ids: selected.value }
    const { data } = await accountAPI.previewReset(selection)
    await router.push(`/admin/batches/${data.id}`)
  } catch (e) { app.showToast(errorMessage(e), 'error') }
  finally { batchBusy.value = false }
}
</script>

<template>
  <AppLayout content-mode="list">
    <div class="page">
      <!-- ── Page Head ─────────────────────────────────────────────────── -->
      <header class="page-head">
        <div>
          <h1 class="page-title">用户管理</h1>
          <p class="page-sub">创建、编辑、管理用户账号与角色权限</p>
        </div>
        <div class="page-meta">
          <button class="btn-primary" @click="router.push('/admin/users/new/edit')">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
            创建用户
          </button>
        </div>
      </header>

      <!-- ── Filters ───────────────────────────────────────────────────── -->
      <div class="filter-bar">
        <label class="searchbox user-searchbox" :class="{ 'has-value': filters.query }">
          <AppIcon name="search" :size="15" />
          <input
            v-model="filters.query"
            type="search"
            class="input"
            placeholder="按用户名、学号或姓名精准搜索"
            aria-label="搜索用户名、学号或姓名"
          />
          <button v-if="filters.query" type="button" class="clear" aria-label="清空搜索" @click="filters.query = ''">
            <AppIcon name="close" :size="13" />
          </button>
        </label>
        <DsSelect v-model="filters.role" @change="resetAndFetch">
          <option value="">全部角色</option>
          <option value="student">学生</option>
          <option value="teacher">教师</option>
          <option value="admin">管理员</option>
        </DsSelect>
        <DsSelect v-model="filters.status" @change="resetAndFetch">
          <option value="">全部状态</option>
          <option value="active">正常</option>
          <option value="disabled">已禁用</option>
        </DsSelect>
        <div class="filter-count">
          共 {{ total }} 名用户
        </div>
      </div>

      <div class="account-actions">
        <RouterLink to="/admin/accounts">Excel 导入与批次记录</RouterLink>
        <button :disabled="!selected.length || batchBusy" class="btn-ghost" @click="resetSelected(false)">预览重置已选 {{ selected.length }} 人</button>
        <button :disabled="batchBusy || (filters.role && filters.role !== 'student')" class="btn-ghost" @click="resetSelected(true)">预览全部匹配学生</button>
      </div>
      <!-- ── Loading ────────────────────────────────────────────────────── -->
      <div v-if="loading" class="card table-card">
        <div class="skeleton-row" v-for="i in 5" :key="i">
          <div class="skeleton skel-cell w-10"></div>
          <div class="skeleton skel-cell w-20"></div>
          <div class="skeleton skel-cell w-15"></div>
          <div class="skeleton skel-cell w-15"></div>
          <div class="skeleton skel-cell w-15"></div>
          <div class="skeleton skel-cell w-20"></div>
        </div>
      </div>

      <!-- ── Empty ──────────────────────────────────────────────────────── -->
      <div v-else-if="users.length === 0" class="empty-state">
        <p>暂无匹配用户</p>
      </div>

      <!-- ── Table ──────────────────────────────────────────────────────── -->
      <div v-else class="card table-card">
        <table class="ds-table">
          <thead>
            <tr><th><input type="checkbox" aria-label="选择当前页学生" :checked="!!selected.length && selected.length === users.filter(u => u.role === 'student').length" @change="selected = $event.target.checked ? users.filter(u => u.role === 'student').map(u => u.id) : []" /></th><th>ID</th><th>用户名</th><th>学号</th><th>姓名</th><th>角色</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="u in users" :key="u.id">
              <td><input v-if="u.role === 'student'" v-model="selected" type="checkbox" :value="u.id" :aria-label="`选择 ${u.real_name}`" /></td>
              <td class="text-sm text-secondary">{{ u.id }}</td>
              <td>
                <a class="user-link" @click="router.push(`/admin/users/${u.id}/edit`)">{{ u.username }}</a>
              </td>
              <td>{{ u.student_no || '—' }}</td>
              <td>{{ u.real_name }}</td>
              <td>
                <span class="badge" :class="'badge-' + statusBadge(ROLE_MAP, u.role).color">
                  {{ statusBadge(ROLE_MAP, u.role).label }}
                </span>
              </td>
              <td>
                <span class="badge" :class="'badge-' + statusBadge(USER_STATUS_MAP, u.status).color">
                  {{ statusBadge(USER_STATUS_MAP, u.status).label }}
                </span>
              </td>
              <td class="actions-cell">
                <button class="btn-ghost btn-sm" @click="router.push(`/admin/users/${u.id}/edit`)">编辑</button>
                <button class="btn-sm" :class="u.status === 'active' ? 'btn-danger-outline' : ''" @click="toggleStatus(u)">
                  {{ u.status === 'active' ? '禁用' : '启用' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ── Pagination ─────────────────────────────────────────────────── -->
      <nav v-if="total > 20" class="pagination">
        <button class="pg-btn" :disabled="page <= 1" @click="page--;fetch()">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M10 3l-5 5 5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          上一页
        </button>
        <div class="pg-info">
          <span class="pg-current">{{ page }}</span>
          <span class="pg-sep">/</span>
          <span class="pg-total">{{ Math.ceil(total / 20) }}</span>
        </div>
        <button class="pg-btn" :disabled="page >= Math.ceil(total / 20)" @click="page++;fetch()">
          下一页
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </nav>
    </div>
  </AppLayout>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════════
   Admin User List — Code Studio
   page-head + filter bar + skeleton table + data table + pagination
   ═══════════════════════════════════════════════════════════════════════ */
.page { display: flex; flex-direction: column; gap: 24px; }

/* ── Page Head ─────────────────────────────────────────────────────── */
.page-head {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 16px;
}
.page-title {
  font-size: 28px; font-weight: 700;
  color: var(--fg); letter-spacing: -0.02em; line-height: 1.15;
  margin: 0 0 6px;
}
.page-sub {
  font-size: var(--text-sm); color: var(--muted); margin: 0;
}

/* ── Filter bar ────────────────────────────────────────────────────── */
.filter-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.user-searchbox {
  width: 280px;
  max-width: 100%;
}
.user-searchbox .input { width: 100%; }
.filter-bar .ds-select {
  min-width: 120px;
}
.filter-count {
  font-size: var(--text-xs); color: var(--muted);
  margin-left: auto;
}

/* ── Table card ────────────────────────────────────────────────────── */
.table-card {
  padding: 0; overflow: hidden;
}
.table-card table { margin: 0; }

/* ── Skeleton ──────────────────────────────────────────────────────── */
.skeleton-row {
  display: flex; gap: 16px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}
.skeleton-row:last-child { border-bottom: none; }
.skel-cell { height: 16px; border-radius: var(--radius-sm); }
.w-10 { width: 10%; }
.w-15 { width: 15%; }
.w-20 { width: 20%; }

/* ── User link ─────────────────────────────────────────────────────── */
.user-link {
  color: var(--accent); cursor: pointer; font-weight: 500;
  transition: color var(--duration-fast) var(--ease-out);
}
.user-link:hover { color: var(--accent-hover); }

/* ── Actions ───────────────────────────────────────────────────────── */
.actions-cell {
  display: table-cell;
  vertical-align: middle;
  white-space: nowrap;
}
.actions-cell button + button { margin-left: 8px; }
.btn-danger-outline {
  color: var(--danger);
  border-color: var(--danger);
  background: transparent;
}
.btn-danger-outline:hover {
  background: var(--danger);
  color: var(--surface);
  border-color: var(--danger);
}

/* ── Pagination ────────────────────────────────────────────────────── */
.pagination {
  display: flex; align-items: center; justify-content: center;
  gap: 16px;
}
.pg-btn {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 8px 14px;
  font-size: var(--text-sm); font-weight: 500;
  color: var(--fg); cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: background var(--duration-fast) var(--ease-out),
              border-color var(--duration-fast) var(--ease-out);
}
.pg-btn:hover:not(:disabled) {
  background: var(--surface-subtle);
  border-color: var(--border-strong);
}
.pg-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.pg-info {
  display: flex; align-items: baseline; gap: 4px;
  font-size: var(--text-sm); font-family: var(--font-mono);
  padding: 0 8px;
}
.pg-current { color: var(--accent); font-weight: 700; font-size: 15px; }
.pg-sep { color: var(--faint); }
.pg-total { color: var(--muted); }

@media (max-width: 768px) {
  .page-head { flex-direction: column; }
  .page-title { font-size: 24px; }
  .filter-bar { flex-direction: column; align-items: stretch; }
  .user-searchbox { width: 100%; }
  .filter-count { margin-left: 0; }
}
</style>
