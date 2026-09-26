<script setup>
import { ref, onMounted } from 'vue'
import AppLayout from '../components/layout/AppLayout.vue'
import PasswordForm from '../components/account/PasswordForm.vue'
import { usersAPI } from '../api/users.js'
import { accountAPI, errorMessage } from '../api/accountManagement.js'
import { useAuthStore } from '../stores/auth.js'
const auth = useAuthStore()
const profile = ref(null)
const nickname = ref('')
const error = ref('')
const message = ref('')
const busy = ref(false)
const prefs = ref({ preferred_page_size: 20, sidebar_collapsed: false })
async function savePrefs() {
  try { await usersAPI.updateMyPreferences(prefs.value); message.value = '界面偏好已保存' }
  catch (e) { error.value = errorMessage(e) }
}
onMounted(async () => { try { const { data } = await usersAPI.getMyPreferences(); prefs.value = { ...prefs.value, ...data.preferences } } catch { /* retain defaults */ } })
onMounted(async () => { try { profile.value = (await accountAPI.profile()).data; nickname.value = profile.value.nickname || '' } catch (e) { error.value = errorMessage(e) } })
async function save() {
  busy.value = true; error.value = ''; message.value = ''
  try { const { data } = await accountAPI.updateProfile({ nickname: nickname.value }); auth.setUser(data); message.value = '昵称已保存' }
  catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>
<template>
  <AppLayout content-mode="form"><div class="account-page profile-page">
    <header><h1>个人中心</h1><p class="account-muted">教务身份由管理员维护，昵称仅用于你的界面展示。</p></header>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <div class="profile-sections"><section v-if="profile" class="account-panel"><h2>账号资料</h2>
      <dl><dt>正式姓名</dt><dd>{{ profile.real_name }}</dd><dt>登录账号 / 学号</dt><dd>{{ profile.username }} / {{ profile.student_no || '—' }}</dd><dt>院系</dt><dd>{{ profile.department || '—' }}</dd></dl>
      <div class="account-tags"><span v-for="c in profile.classes" :key="c.id">{{ c.name }}</span></div>
      <form class="account-form" @submit.prevent="save"><label>昵称<input v-model="nickname" maxlength="80" /></label><button class="btn-primary" :disabled="busy">保存昵称</button><p v-if="message" role="status">{{ message }}</p></form>
    </section>
    <section class="account-panel"><h2>界面偏好</h2><form class="account-form" @submit.prevent="savePrefs"><label>默认每页条数<input v-model.number="prefs.preferred_page_size" type="number" min="10" max="100" /></label><label><span><input v-model="prefs.sidebar_collapsed" type="checkbox" /> 默认收起侧栏</span></label><button class="btn-ghost">保存偏好</button></form></section>
    <section class="account-panel"><h2>修改密码</h2><PasswordForm /></section>
  </div></div></AppLayout>
</template>

<style scoped>
.profile-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: var(--space-5); }
.profile-sections > section:last-child { grid-column: 1 / -1; }
.profile-sections :deep(.account-form) { max-width: none; }
.profile-sections :deep(dl) { grid-template-columns: 1fr; gap: 4px; }
.profile-sections :deep(dd) { margin-bottom: 12px; }
</style>
