<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth.js'
import { homeForRole } from '../router/roleHome.js'

const router = useRouter()
const auth = useAuthStore()

const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)
const showPwd = ref(false)

async function handleLogin() {
  if (!username.value || !password.value) {
    error.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const user = await auth.login(username.value, password.value)
    router.push(user.must_change_password ? '/account/password' : homeForRole(user.role))
  } catch (e) {
    const detail = e.response?.data?.detail
    error.value = detail?.message || '登录失败，请检查用户名和密码'
  } finally {
    loading.value = false
  }
}

function goWelcome() { router.push('/welcome') }
</script>

<template>
  <div class="login-page">
    <!-- Background layers -->
    <div class="bg-grid"></div>
    <div class="bg-glow bg-glow-a"></div>
    <div class="bg-glow bg-glow-b"></div>

    <!-- Back to welcome -->
    <button class="back-btn" @click="goWelcome">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
        <path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      返回首页
    </button>

    <!-- Login card -->
    <div class="login-card">
      <!-- Brand -->
      <div class="card-brand">
        <div class="brand-mark">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="2.4" fill="currentColor"/>
            <circle cx="5" cy="6.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
            <circle cx="5" cy="17.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
            <circle cx="19" cy="6.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
            <circle cx="19" cy="17.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
            <path d="M6.3 7.5L10.1 10.5M6.3 16.5L10.1 13.5M17.7 7.5L13.9 10.5M17.7 16.5L13.9 13.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          </svg>
        </div>
        <span class="brand-name">人工智能基础实验平台</span>
      </div>

      <!-- Header -->
      <div class="card-header">
        <h1 class="card-title">欢迎回来</h1>
        <p class="card-sub">登录你的学习账号，继续未完成的实验</p>
      </div>

      <!-- Form -->
      <form @submit.prevent="handleLogin" class="login-form">
        <transition name="error-slide">
          <div v-if="error" class="login-error" role="alert">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.3"/>
              <path d="M8 4.5v3.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
              <circle cx="8" cy="11" r="0.7" fill="currentColor"/>
            </svg>
            <span>{{ error }}</span>
          </div>
        </transition>

        <div class="field">
          <label for="login-username">用户名</label>
          <div class="input-wrap">
            <svg class="input-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <circle cx="8" cy="5.5" r="2.5" stroke="currentColor" stroke-width="1.4"/>
              <path d="M3 13.5c0-2.8 2.2-4.5 5-4.5s5 1.7 5 4.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
            </svg>
            <input
              id="login-username"
              v-model="username"
              class="login-input"
              type="text"
              placeholder="请输入用户名"
              autocomplete="username"
            />
          </div>
        </div>

        <div class="field">
          <label for="login-password">密码</label>
          <div class="input-wrap">
            <svg class="input-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <rect x="3" y="7" width="10" height="7" rx="1.5" stroke="currentColor" stroke-width="1.4"/>
              <path d="M5.5 7V5a2.5 2.5 0 015 0v2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
            </svg>
            <input
              id="login-password"
              v-model="password"
              class="login-input login-password"
              :type="showPwd ? 'text' : 'password'"
              placeholder="请输入密码"
              autocomplete="current-password"
            />
            <button type="button" class="pwd-toggle" @click="showPwd = !showPwd" :aria-label="showPwd ? '隐藏密码' : '显示密码'">
              <svg v-if="!showPwd" width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M1 8s2.5-4.5 7-4.5S15 8 15 8s-2.5 4.5-7 4.5S1 8 1 8z" stroke="currentColor" stroke-width="1.4"/>
                <circle cx="8" cy="8" r="2" stroke="currentColor" stroke-width="1.4"/>
              </svg>
              <svg v-else width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M1 8s2.5-4.5 7-4.5S15 8 15 8s-2.5 4.5-7 4.5S1 8 1 8z" stroke="currentColor" stroke-width="1.4"/>
                <path d="M2 2l12 12" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
              </svg>
            </button>
          </div>
        </div>

        <button type="submit" class="login-btn" :disabled="loading">
          <svg v-if="loading" width="14" height="14" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" stroke-dasharray="20 8" stroke-linecap="round">
              <animateTransform attributeName="transform" type="rotate" from="0 8 8" to="360 8 8" dur="0.9s" repeatCount="indefinite"/>
            </circle>
          </svg>
          <span v-if="loading">登录中…</span>
          <span v-else>登 录</span>
          <svg v-if="!loading" width="14" height="14" viewBox="0 0 16 16" fill="none">
            <path d="M3 8h10 M9 4l4 4-4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
      </form>

      <p class="card-foot">
        还没有账号？
        <a href="#" @click.prevent="goWelcome">返回首页了解平台</a>
      </p>
    </div>

    <footer class="page-foot">
      <span>© 2026 人工智能基础实验平台 · Python Learning Studio</span>
    </footer>
  </div>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════════
   Login — 与首页一致的明亮轻量教育科技风
   复用全局 tokens：--paper var(--surface-subtle) / --primary var(--accent) / --ink var(--fg)
   / --text-secondary var(--muted) / --border var(--border) / --radius-xl 16px
   ═══════════════════════════════════════════════════════════════════════ */
.login-page {
  min-height: 100vh;
  background: var(--bg);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
  padding: 40px 24px;
  color: var(--fg);
  font-family: var(--font-body);
}

/* ── 背景装饰：墨松绿 × 岩灰渐变 + 极淡网格 ─────────────────── */
.bg-grid {
  position: absolute; inset: 0;
  background-image:
    linear-gradient(oklch(0.2 0.01 150 / 0.04) 1px, transparent 1px),
    linear-gradient(90deg, oklch(0.2 0.01 150 / 0.04) 1px, transparent 1px);
  background-size: 32px 32px;
  pointer-events: none;
  mask-image: radial-gradient(ellipse 70% 60% at 50% 45%, var(--fg) 25%, transparent 80%);
}
.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(90px);
  pointer-events: none;
}
.bg-glow-a {
  width: 560px; height: 560px;
  top: -140px; left: -80px;
  background: radial-gradient(circle, color-mix(in oklch, var(--info) 12%, transparent) 0%, transparent 70%);
}
.bg-glow-b {
  width: 500px; height: 500px;
  bottom: -120px; right: -60px;
  background: radial-gradient(circle, color-mix(in oklch, var(--accent) 14%, transparent) 0%, transparent 70%);
}

/* ── 返回首页按钮（浅色描边） ─────────────────────────────────────── */
.back-btn {
  position: absolute; top: 28px; left: 28px;
  z-index: 3;
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--fg);
  font-family: var(--font-body);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: border-color var(--duration-fast) var(--ease-out),
              box-shadow var(--duration-fast) var(--ease-out);
}
.back-btn:hover {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

/* ── 卡片（纯白 + 细边框 + 柔和阴影，无玻璃拟态） ─────────────────── */
.login-card {
  position: relative; z-index: 2;
  width: 100%;
  max-width: 410px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 40px 36px 32px;
  box-shadow: var(--shadow-lg);
  animation: fade-up 480ms var(--ease-out) both;
}
@keyframes fade-up {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* Brand */
.card-brand {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 28px;
}
.brand-mark {
  width: 40px; height: 40px;
  border-radius: var(--radius-md);
  background: var(--accent);
  display: flex; align-items: center; justify-content: center;
  color: var(--surface);
  box-shadow: var(--shadow-sm);
}
.brand-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--fg);
  letter-spacing: -0.01em;
}

/* Header */
.card-header { margin-bottom: 28px; }
.card-title {
  font-size: 26px;
  font-weight: 700;
  color: var(--fg);
  letter-spacing: -0.025em;
  line-height: 1.15;
  margin: 0 0 8px;
}
.card-sub {
  font-size: 14px;
  color: var(--muted);
  line-height: 1.5;
  margin: 0;
}

/* Form */
.login-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.field { display: flex; flex-direction: column; }
.field label {
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--fg);
  margin-bottom: 7px;
}
.input-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.input-icon {
  position: absolute;
  left: 13px;
  color: var(--faint);
  pointer-events: none;
  transition: color var(--duration-fast) var(--ease-out);
}
.input-wrap:focus-within .input-icon { color: var(--accent); }

.field input {
  width: 100%;
  height: 44px;
  padding: 0 14px 0 42px !important;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--fg);
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.5;
  transition: border-color var(--duration-fast) var(--ease-out),
              box-shadow var(--duration-fast) var(--ease-out);
}
.field input.login-password { padding-right: 42px !important; }
.field input::placeholder { color: var(--faint); }
.field input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

/* Password toggle */
.pwd-toggle {
  position: absolute;
  right: 10px;
  background: none;
  border: none;
  padding: 4px;
  color: var(--faint);
  cursor: pointer;
  display: flex; align-items: center;
  border-radius: var(--radius-sm);
  transition: color var(--duration-fast) var(--ease-out);
}
.pwd-toggle:hover { color: var(--muted); }

/* Error */
.login-error {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--danger-bg);
  color: var(--danger);
  padding: 10px 14px;
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: 500;
  border: 1px solid var(--danger-bg);
}
.error-slide-enter-active { transition: all var(--duration-normal) var(--ease-out); }
.error-slide-leave-active { transition: all var(--duration-fast) var(--ease-out); }
.error-slide-enter-from { opacity: 0; transform: translateY(-6px); }
.error-slide-leave-to { opacity: 0; }

/* Button（纯蓝 + 柔和阴影，无荧光） */
.login-btn {
  width: 100%;
  height: 48px;
  padding: 0 16px;
  background: var(--accent);
  border: none;
  border-radius: var(--radius-lg);
  color: var(--surface);
  font-family: var(--font-body);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.02em;
  cursor: pointer;
  margin-top: 6px;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  box-shadow: 0 4px 12px color-mix(in oklch, var(--accent) 22%, transparent);
  transition: background var(--duration-normal) var(--ease-out),
              box-shadow var(--duration-normal) var(--ease-out),
              transform var(--duration-normal) var(--ease-out);
}
.login-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  box-shadow: var(--shadow-lg);
  transform: translateY(-2px);
}
.login-btn:active:not(:disabled) {
  transform: translateY(0);
  box-shadow: var(--shadow-sm);
}
.login-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; box-shadow: none; }

/* Footer */
.card-foot {
  margin: 24px 0 0;
  text-align: center;
  font-size: 13px;
  color: var(--muted);
}
.card-foot a {
  color: var(--accent);
  font-weight: 500;
  text-decoration: none;
}
.card-foot a:hover { text-decoration: underline; }

/* Page footer（深色文字） */
.page-foot {
  position: relative; z-index: 2;
  margin-top: 28px;
  font-size: 12px;
  color: var(--faint);
}

/* ── 响应式 ─────────────────────────────────────────────────────────── */
@media (max-width: 480px) {
  .login-card { padding: 32px 24px 24px; }
  .card-title { font-size: 22px; }
  .back-btn { top: 20px; left: 20px; }
}

@media (prefers-reduced-motion: reduce) {
  .login-card { animation: none; }
  .login-btn:hover:not(:disabled) { transform: none; }
}

@media (min-width: 921px) and (max-height: 800px) {
  .login-card { padding-block: 28px; }
  .card-foot { margin-top: 20px; }
  .page-foot { margin-top: 20px; }
}
</style>
