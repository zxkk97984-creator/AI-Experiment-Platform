<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import CredentialExportPanel from '../../components/admin/CredentialExportPanel.vue'
import { accountAPI, errorMessage } from '../../api/accountManagement.js'
import { usersAPI } from '../../api/users.js'
import { useAppStore } from '../../stores/app.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const isNew = ref(route.params.id === 'new')
const user = ref(null)
const form = ref({ username: '', student_no: '', real_name: '', role: 'student', password: '', department: '', reason: '' })
const saving = ref(false)
const resetPassword = ref('')
const resetReason = ref('')
const resetBusy = ref(false)

onMounted(async () => {
  if (!isNew.value) {
    try {
      const res = await usersAPI.get(route.params.id)
      user.value = res.data
      form.value = {
        username: res.data.username,
        student_no: res.data.student_no || '',
        real_name: res.data.real_name,
        role: res.data.role,
        department: res.data.department || '',
        reason: '',
        password: '',
      }
    } catch { app.showToast('用户不存在', 'error'); router.push('/admin/users') }
  }
})

async function handleSave() {
  if (form.value.role !== 'student' && !form.value.username) { app.showToast('请输入用户名', 'error'); return }
  if (form.value.role === 'student' && !form.value.student_no.trim()) { app.showToast('学生必须填写学号', 'error'); return }
  saving.value = true
  try {
    if (isNew.value) {
      if (form.value.role !== 'student' && !form.value.password) { app.showToast('请输入密码', 'error'); saving.value = false; return }
      const result = await usersAPI.create({ ...form.value, password: form.value.role === 'student' ? undefined : form.value.password })
      app.showToast('用户已创建', 'success')
      if (result.data.role === 'student') {
        user.value = result.data; isNew.value = false
        form.value.username = result.data.username
        await router.replace(`/admin/users/${result.data.id}/edit`)
        return
      }
    } else {
      await usersAPI.update(route.params.id, {
        real_name: form.value.real_name,
        student_no: form.value.role === 'student' ? form.value.student_no : null,
        role: form.value.role,
        department: form.value.department || null,
        reason: form.value.reason || null,
      })

      app.showToast('用户已更新', 'success')
    }
    router.push('/admin/users')
  } catch (e) { app.showToast(e.response?.data?.detail?.message || '保存失败', 'error') }
  finally { saving.value = false }
}
async function resetStudent() {
  resetBusy.value = true
  try {
    const { data } = await accountAPI.previewReset({ student_ids: [user.value.id] })
    await router.push(`/admin/batches/${data.id}`)
  } catch (e) { app.showToast(errorMessage(e), 'error') }
  finally { resetBusy.value = false }
}
async function resetOther() {
  resetBusy.value = true
  try {
    await usersAPI.resetPassword(user.value.id, { current_password: resetPassword.value, password: form.value.password, reason: resetReason.value })
    resetPassword.value = form.value.password = ''; app.showToast('密码已重置', 'success')
  } catch (e) { app.showToast(errorMessage(e), 'error') }
  finally { resetBusy.value = false }
}
</script>

<template>
  <AppLayout content-mode="form">
    <div class="page account-page user-edit-page">
      <!-- ── Page Head ─────────────────────────────────────────────────── -->
      <header class="page-head">
        <div>
          <h1 class="page-title">{{ isNew ? '创建用户' : '编辑用户' }}</h1>
          <p class="page-sub">{{ isNew ? '添加新的平台用户账号' : '修改用户信息与角色权限' }}</p>
        </div>
      </header>

      <p v-if="isNew && form.role === 'student'" class="account-notice">学号作为登录账号，系统为学生生成独立随机临时密码。创建后请下载凭据发放，首次登录必须改密。</p>
      <form class="card form-card" @submit.prevent="handleSave">
        <div v-if="form.role !== 'student' || !isNew" class="form-group"><label for="user-username">登录用户名</label><input id="user-username" v-model="form.username" :disabled="!isNew" placeholder="输入用户名" /><small v-if="!isNew">原账号保留；更正学号时，以原学号登录的账号会同步更正</small></div>
        <div v-if="form.role === 'student'" class="form-group"><label for="user-student_no">学号</label><input id="user-student_no" v-model="form.student_no" required placeholder="输入唯一学号" /></div>
        <div class="form-group"><label for="user-department">院系</label><input id="user-department" v-model="form.department" maxlength="120" /></div><div v-if="!isNew" class="form-group"><label for="user-reason">修改原因</label><input id="user-reason" v-model="form.reason" maxlength="500" placeholder="说明资料更正或角色调整原因" /></div>
        <div class="form-group"><label for="user-real_name">真实姓名</label><input id="user-real_name" v-model="form.real_name" placeholder="输入真实姓名" /></div>
        <div class="form-group">
          <label for="user-role">角色</label>
          <DsSelect id="user-role" v-model="form.role">
            <option value="student">学生</option>
            <option value="teacher">教师</option>
            <option value="admin">管理员</option>
          </DsSelect>
        </div>
        <div v-if="isNew && form.role !== 'student'" class="form-group">
          <label for="user-password">初始密码</label>
          <input id="user-password" v-model="form.password" type="password" :placeholder="isNew ? '设置密码' : '留空则不修改密码'" />
        </div>
        <div class="form-actions">
          <button class="btn-primary" :disabled="saving" type="submit">
            {{ saving ? '保存中...' : '保存' }}
          </button>
          <button type="button" class="btn-ghost" @click="router.push('/admin/users')">取消</button>
        </div>
      </form>
      <CredentialExportPanel v-if="user?.role === 'student'" :user-id="user.id" />
      <section v-if="user" class="account-panel">
        <h2>独立密码重置</h2><p>重置会撤销该用户旧登录；资料保存不会修改密码。</p>
        <button v-if="user.role === 'student'" class="btn-ghost" :disabled="resetBusy" @click="resetStudent">预览学生密码重置</button>
        <form v-else class="account-form" @submit.prevent="resetOther">
          <label>管理员当前密码<input v-model="resetPassword" type="password" required autocomplete="current-password" /></label>
          <label>新的临时密码<input v-model="form.password" type="password" required minlength="8" autocomplete="new-password" /></label>
          <label>原因<input v-model="resetReason" required maxlength="500" /></label>
          <button class="btn-primary" :disabled="resetBusy">重置密码</button>
        </form>
      </section>
    </div>
  </AppLayout>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════════
   User Edit — Code Studio
   page-head + form card
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

/* ── Form Card ─────────────────────────────────────────────────────── */
.form-card {
  max-width: 520px; padding: 28px;
}
.form-actions {
  display: flex; gap: 10px; padding-top: 8px;
}

@media (max-width: 768px) {
  .page-title { font-size: 24px; }
  .form-card { max-width: 100%; padding: 20px; }
}

/* Profile editing is the primary task; reset remains a separate form below. */
.form-card { max-width: none; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.form-card .form-group { min-width: 0; margin: 0; display: grid; align-content: start; gap: var(--space-2); }
.form-card input, .form-card .ds-select { width: 100%; min-width: 0; }
.form-card small { display: block; color: var(--muted); line-height: var(--lh-body); }
.form-actions { grid-column: 1 / -1; }
.account-panel { border-top: 2px solid var(--border-strong); }
</style>
