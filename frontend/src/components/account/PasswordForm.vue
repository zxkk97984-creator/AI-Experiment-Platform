<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { accountAPI, errorMessage } from '../../api/accountManagement.js'
import { useAuthStore } from '../../stores/auth.js'
import { useAppStore } from '../../stores/app.js'
const auth = useAuthStore()
const app = useAppStore()
const router = useRouter()
const current = ref('')
const password = ref('')
const confirmation = ref('')
const busy = ref(false)
const error = ref('')
async function submit() {
  error.value = ''
  if (password.value !== confirmation.value) { error.value = '两次新密码不一致'; return }
  if (password.value.length < 8 || new TextEncoder().encode(password.value).length > 72) {
    error.value = '密码至少 8 个字符，最多 72 个 UTF-8 字节'; return
  }
  busy.value = true
  try {
    await accountAPI.changePassword({ current_password: current.value, password: password.value })
    current.value = password.value = confirmation.value = ''
    await auth.logout()
    app.showToast('密码已修改，请重新登录', 'success')
    await router.replace('/login')
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>
<template>
  <form class="account-form" @submit.prevent="submit">
    <p class="account-muted">至少 8 个字符；不能使用当前密码、共享初始密码、用户名或学号。</p>
    <label>当前密码<input v-model="current" type="password" autocomplete="current-password" required /></label>
    <label>新密码<input v-model="password" type="password" autocomplete="new-password" minlength="8" required /></label>
    <label>确认新密码<input v-model="confirmation" type="password" autocomplete="new-password" minlength="8" required /></label>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <button class="btn-primary" :disabled="busy">{{ busy ? '正在修改…' : '修改密码并重新登录' }}</button>
  </form>
</template>
