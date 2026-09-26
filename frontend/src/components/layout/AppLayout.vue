<script setup>
import { computed, onMounted } from 'vue'
import AppSidebar from './AppSidebar.vue'
import AppHeader from './AppHeader.vue'
import { useAppStore } from '../../stores/app.js'
import { useAuthStore } from '../../stores/auth.js'
import { usersAPI } from '../../api/users.js'

const app = useAppStore()
const auth = useAuthStore()

const props = defineProps({
  contentMode: {
    type: String,
    default: 'list',
    validator: value => ['list', 'form', 'reading', 'workspace'].includes(value),
  },
  variant: {
    type: String,
    default: 'default',
  },
  studentContext: {
    type: Object,
    default: () => ({}),
  },
})

const isStudentWorkspace = computed(() => props.variant === 'student-workspace')
const isTeacherWorkspace = computed(() => props.variant === 'teacher-workspace')
const isWorkspace = computed(() => isStudentWorkspace.value || isTeacherWorkspace.value)

onMounted(async () => {
  if (!auth.isAuthenticated) return
  try {
    const { data } = await usersAPI.getMyPreferences()
    const prefs = data?.preferences || {}
    if (typeof prefs.sidebar_collapsed === 'boolean') {
      app.sidebarCollapsed = prefs.sidebar_collapsed
    }
  } catch {
    // 偏好加载失败时保留本地状态
  }
})
</script>

<template>
  <div
    class="shell"
    :data-content-mode="contentMode"
    :data-role="auth.role"
    :class="{
      'student-workspace-shell': isStudentWorkspace,
      'teacher-workspace-shell': isTeacherWorkspace,
      'workspace-shell': isWorkspace,
      'is-collapsed': !isWorkspace && app.sidebarCollapsed,
      'mobile-nav-open': app.mobileNavOpen,
    }"
  >
    <AppSidebar :variant="variant" :student-context="studentContext" />
    <button
      v-if="isWorkspace"
      type="button"
      class="workspace-mobile-backdrop"
      aria-label="关闭导航"
      @click="app.closeMobileNav()"
    ></button>
    <div class="main">
      <AppHeader :variant="variant" />
      <main class="content">
        <div class="content-inner">
          <slot />
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
/* App Shell 视觉与断点全部来自全局 dai-ds-v2.css 的 .shell/.sidebar/.main/.content。
   组件内不再重复定义宽度、高度、断点或固定侧栏 margin。 */
.workspace-shell {
  --bg: oklch(0.9731 0.0041 91.45);
  --surface: oklch(0.994 0 89.88);
  --fg: oklch(0.2586 0.0159 152.78);
  --muted: oklch(0.4586 0.0139 153.35);
  --border: oklch(0.8979 0.0095 113.18);
  --accent: oklch(0.5179 0.0909 158.07);
  --sidebar-width: 224px;
  --header-height: 72px;
  grid-template-columns: 224px minmax(0, 1fr);
  background: var(--bg);
}

.workspace-shell .content {
  padding: 0;
}

.workspace-shell .content-inner {
  max-width: none;
  margin-inline: 0;
}

.workspace-mobile-backdrop {
  display: none;
}

@media (max-width: 920px) {
  .workspace-shell,
  .workspace-shell.is-collapsed {
    grid-template-columns: minmax(0, 1fr);
  }

  .workspace-shell :deep(.sidebar) {
    position: fixed;
    inset: 0 auto 0 0;
    z-index: 40;
    width: min(286px, 86vw);
    height: 100vh;
    transform: translateX(-102%);
    transition: transform 200ms cubic-bezier(0.22, 1, 0.36, 1);
    box-shadow: 18px 0 46px color-mix(in oklch, var(--fg) 15%, transparent);
  }

  .workspace-shell.mobile-nav-open :deep(.sidebar) {
    transform: translateX(0);
  }

  .workspace-mobile-backdrop {
    position: fixed;
    z-index: 35;
    inset: 0;
    display: block;
    border: 0;
    background: color-mix(in oklch, var(--fg) 22%, transparent);
    opacity: 0;
    pointer-events: none;
    transition: opacity 140ms ease;
  }

  .workspace-shell.mobile-nav-open .workspace-mobile-backdrop {
    opacity: 1;
    pointer-events: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .workspace-shell :deep(.sidebar),
  .workspace-mobile-backdrop {
    transition-duration: 0.01ms;
  }
}
</style>
