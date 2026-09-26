<script setup>
import { onMounted, reactive, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import { useRouter } from 'vue-router'
import { accountAPI } from '../../api/accountManagement.js'
import { usersAPI } from '../../api/users.js'
import { useAppStore } from '../../stores/app.js'
import { useAuthStore } from '../../stores/auth.js'

const router = useRouter()
const app = useAppStore()
const auth = useAuthStore()

const profile = reactive({ real_name: '', department: '', nickname: '' })
const saving = ref(false)
const password = reactive({ current_password: '', password: '', password_confirm: '' })
const passwordSaving = ref(false)
const prefsSaving = ref(false)
const localPrefs = reactive({
  sidebar_collapsed: localStorage.getItem('dai.sidebarCollapsed') === '1',
  preferred_page_size: 10,
})


function loadProfile() {
  profile.nickname = auth.user?.nickname || ''
  profile.real_name = auth.user?.real_name || ''
  profile.department = auth.user?.department || ''
}

async function saveProfile() {
  if (!profile.real_name.trim()) {
    app.showToast('姓名不能为空', 'error')
    return
  }
  saving.value = true
  try {
    const { data } = await accountAPI.updateProfile({ nickname: profile.nickname })
    auth.setUser(data)
    app.showToast('个人信息已保存', 'success')
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

function applySidebarPreference() {
  localStorage.setItem('dai.sidebarCollapsed', localPrefs.sidebar_collapsed ? '1' : '0')
  app.sidebarCollapsed = localPrefs.sidebar_collapsed
}

function toggleSidebarCollapsed() {
  localPrefs.sidebar_collapsed = !localPrefs.sidebar_collapsed
  applySidebarPreference()
  savePreferences()
}

async function loadPreferences() {
  try {
    const { data } = await usersAPI.getMyPreferences()
    const prefs = data?.preferences || {}
    if (typeof prefs.sidebar_collapsed === 'boolean') localPrefs.sidebar_collapsed = prefs.sidebar_collapsed
    if (Number.isInteger(prefs.preferred_page_size)) localPrefs.preferred_page_size = prefs.preferred_page_size
    applySidebarPreference()
  } catch {
    // 后端不可用时保留本地偏好
  }
}

async function savePreferences() {
  prefsSaving.value = true
  try {
    await usersAPI.updateMyPreferences({
      sidebar_collapsed: localPrefs.sidebar_collapsed,
      preferred_page_size: Number(localPrefs.preferred_page_size) || 10,
    })
  } catch {
    app.showToast('偏好保存失败，仅保留在当前浏览器', 'error')
  } finally {
    prefsSaving.value = false
  }
}

async function savePassword() {
  if (!password.current_password || !password.password) {
    app.showToast('请填写当前密码和新密码', 'error')
    return
  }
  if (password.password !== password.password_confirm) {
    app.showToast('两次输入的新密码不一致', 'error')
    return
  }
  passwordSaving.value = true
  try {
    await usersAPI.updatePassword(auth.user.id, {
      current_password: password.current_password,
      password: password.password,
    })
    password.current_password = ''
    password.password = ''
    password.password_confirm = ''
    app.showToast('密码已修改，请重新登录', 'success')
    await auth.logout()
    await router.replace('/login')
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '修改失败', 'error')
  } finally {
    passwordSaving.value = false
  }
}

onMounted(() => {
  loadProfile()
  loadPreferences()
})
</script>

<template>
  <AppLayout content-mode="form">
    <section class="settings-page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">系统 / 设置</p>
          <h1>设置</h1>
          <p class="lead">维护个人资料、密码与界面偏好。</p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="ph-label"><p class="eyebrow">Profile</p><h3>个人资料</h3></div></div>
        <div class="panel-body settings-form">
          <label class="field">
            <span>正式姓名（联系管理员修改）</span>
            <input v-model="profile.real_name" readonly type="text" maxlength="120" />
          </label>
          <label class="field">
            <span>院系</span>
            <input v-model="profile.department" readonly type="text" maxlength="120" placeholder="例如：计算机学院" />
          </label>
          <label class="field"><span>昵称</span><input v-model="profile.nickname" maxlength="80" /></label>
          <div><button type="button" class="btn btn-primary" :disabled="saving" @click="saveProfile">保存资料</button></div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="ph-label"><p class="eyebrow">Security</p><h3>修改密码</h3></div></div>
        <div class="panel-body settings-form">
          <label class="field"><span>当前密码</span><input v-model="password.current_password" type="password" autocomplete="current-password" /></label>
          <label class="field"><span>新密码</span><input v-model="password.password" type="password" autocomplete="new-password" /></label>
          <label class="field"><span>确认新密码</span><input v-model="password.password_confirm" type="password" autocomplete="new-password" /></label>
          <div><button type="button" class="btn btn-primary" :disabled="passwordSaving" @click="savePassword">修改密码</button></div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="ph-label"><p class="eyebrow">Preferences</p><h3>界面偏好</h3></div></div>
        <div class="panel-body preference-row">
          <span>
            <strong>默认收起侧边栏</strong>
            <small>重新进入平台时保留当前侧边栏展开状态。</small>
          </span>
          <button type="button" class="btn" :class="localPrefs.sidebar_collapsed ? 'btn-primary' : 'btn-ghost'" :disabled="prefsSaving" @click="toggleSidebarCollapsed">
            {{ localPrefs.sidebar_collapsed ? '已开启' : '已关闭' }}
          </button>
        </div>
      </section>
    </section>
  </AppLayout>
</template>

<style scoped>
.settings-page { display: flex; flex-direction: column; gap: var(--space-5); }
.settings-form { display: flex; flex-direction: column; gap: 14px; max-width: 560px; }
.field { display: flex; flex-direction: column; gap: 6px; color: var(--muted); font-size: var(--text-sm); }
.preference-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.preference-row span { display: flex; flex-direction: column; gap: 4px; }
.preference-row small { color: var(--muted); }

.settings-page { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-5); align-items: start; }
.settings-page > .page-head, .settings-page > .panel:last-child { grid-column: 1 / -1; }
.settings-form { max-width: none; }
</style>
