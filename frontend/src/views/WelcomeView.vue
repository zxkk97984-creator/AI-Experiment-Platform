<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { navLinks, learningSteps } from './welcome/welcomeContent.js'
import WelcomeHero from '../components/welcome/WelcomeHero.vue'
import CapabilityShowcase from '../components/welcome/CapabilityShowcase.vue'
import LearningFlow from '../components/welcome/LearningFlow.vue'
import RoleShowcase from '../components/welcome/RoleShowcase.vue'

const router = useRouter()

function goLogin() {
  router.push('/login')
}

function scrollTo(id) {
  const el = document.getElementById(id)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

const heroRef = ref(null)
const capabilityRef = ref(null)
const loopRef = ref(null)
const roleRef = ref(null)

const heroVisible = ref(true)
const capabilityVisible = ref(false)
const loopVisible = ref(false)
const roleVisible = ref(false)

let observers = []

function createObserver(el, setter) {
  if (typeof IntersectionObserver === 'undefined') {
    setter(true)
    return null
  }
  const observer = new IntersectionObserver(
    ([entry]) => {
      if (entry.isIntersecting) {
        setter(true)
        observer.disconnect()
      }
    },
    { threshold: 0.12, rootMargin: '0px 0px -60px 0px' }
  )
  observer.observe(el)
  return observer
}

onMounted(() => {
  if (heroRef.value) {
    const obs = createObserver(heroRef.value, (v) => { heroVisible.value = v })
    if (obs) observers.push(obs)
  }
  if (capabilityRef.value) {
    const obs = createObserver(capabilityRef.value, (v) => { capabilityVisible.value = v })
    if (obs) observers.push(obs)
  }
  if (loopRef.value) {
    const obs = createObserver(loopRef.value, (v) => { loopVisible.value = v })
    if (obs) observers.push(obs)
  }
  if (roleRef.value) {
    const obs = createObserver(roleRef.value, (v) => { roleVisible.value = v })
    if (obs) observers.push(obs)
  }
})

onUnmounted(() => {
  observers.forEach((o) => o.disconnect())
})
</script>

<template>
  <div class="welcome-page">
    <header class="w-nav" role="banner">
      <div class="w-nav-inner">
        <div class="w-nav-brand">
          <span class="w-nav-logo" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="2.4" fill="currentColor"/>
              <circle cx="5" cy="6.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
              <circle cx="5" cy="17.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
              <circle cx="19" cy="6.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
              <circle cx="19" cy="17.5" r="1.6" stroke="currentColor" stroke-width="1.5"/>
              <path d="M6.3 7.5L10.1 10.5M6.3 16.5L10.1 13.5M17.7 7.5L13.9 10.5M17.7 16.5L13.9 13.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
            </svg>
          </span>
          <span class="w-nav-name">人工智能基础实验平台</span>
        </div>

        <nav class="w-nav-links" aria-label="页面导航">
          <button
            v-for="link in navLinks"
            :key="link.id"
            class="w-nav-link"
            @click="scrollTo(link.id)"
          >
            {{ link.label }}
          </button>
        </nav>

        <button class="w-nav-login" @click="goLogin">
          进入平台
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>
    </header>

    <div ref="heroRef">
      <WelcomeHero :is-visible="heroVisible" @explore="scrollTo('capabilities')" @login="goLogin" />
    </div>

    <div ref="capabilityRef" id="capabilities">
      <CapabilityShowcase />
    </div>

    <div ref="loopRef">
      <LearningFlow :steps="learningSteps" :is-visible="loopVisible" />
    </div>

    <div ref="roleRef" id="roles">
      <RoleShowcase @login="goLogin" />
    </div>

    <footer class="w-footer" role="contentinfo">
      <div class="w-footer-inner">
        <span>© 2026 人工智能基础实验平台</span>
        <span class="w-footer-sep">·</span>
        <span>Python Learning Studio</span>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.welcome-page {
  min-height: 100vh;
  background: var(--surface-subtle);
  color: var(--fg);
  font-family: var(--font-body);
  -webkit-font-smoothing: antialiased;
}

.w-nav {
  position: sticky;
  top: 0;
  z-index: 50;
  background: oklch(0.99 0.001 95 / 0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid oklch(0.90 0.008 110 / 0.7);
}

.w-nav-inner {
  display: flex;
  align-items: center;
  gap: 32px;
  max-width: 1180px;
  margin: 0 auto;
  padding: 12px 24px;
}

.w-nav-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.w-nav-logo {
  color: var(--accent);
  display: flex;
  flex-shrink: 0;
}

.w-nav-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--fg);
  letter-spacing: -0.01em;
  white-space: nowrap;
}

.w-nav-links {
  display: flex;
  gap: 4px;
  margin-left: auto;
}

.w-nav-link {
  background: none;
  border: none;
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  color: var(--muted);
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: all 0.2s ease;
  font-family: inherit;
}

.w-nav-link:hover,
.w-nav-link:focus-visible {
  color: var(--accent);
  background: oklch(0.52 0.095 158 / 0.06);
}

.w-nav-login {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 20px;
  font-size: 13px;
  font-weight: 600;
  color: var(--surface);
  background: var(--accent);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all 0.2s ease;
  font-family: inherit;
  white-space: nowrap;
}

.w-nav-login:hover {
  background: var(--accent-hover);
  transform: translateY(-1px);
  box-shadow: var(--shadow-md);
}

.w-footer {
  border-top: 1px solid var(--border);
  padding: 24px 24px 32px;
}

.w-footer-inner {
  max-width: 1180px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--faint);
}

.w-footer-sep {
  color: var(--border-strong);
}

@media (prefers-reduced-motion: reduce) {
  .w-nav-login:hover {
    transform: none;
  }
}

@media (max-width: 768px) {
  .w-nav-inner {
    padding: 10px 24px;
    gap: 16px;
  }
  .w-nav-links {
    display: none;
  }
  .w-nav-login {
    margin-left: auto;
  }
  .w-footer {
    padding: 20px 24px 24px;
  }
}
</style>
