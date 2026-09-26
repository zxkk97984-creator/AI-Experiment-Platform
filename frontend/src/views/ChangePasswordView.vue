<script setup>
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth.js'
import PasswordForm from '../components/account/PasswordForm.vue'
const auth = useAuthStore()
const router = useRouter()
async function logout() { await auth.logout(); await router.replace('/login') }
</script>
<template>
  <main class="password-gate account-page">
    <section class="account-panel">
      <p class="account-muted">账号安全 · 首次登录</p><h1>设置你的个人密码</h1>
      <p>{{ auth.user?.real_name }}，当前账号使用临时密码。完成修改后即可进入课程和实验。</p>
      <PasswordForm />
      <button class="btn-ghost" @click="logout">退出登录</button>
    </section>
  </main>
</template>
<style scoped>
.password-gate { max-width:560px; margin:clamp(24px, 6vh, 64px) auto; padding:16px; }
.password-gate .btn-ghost { margin-top:16px; }
</style>
