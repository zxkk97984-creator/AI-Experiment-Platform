<script setup>
// AppHeader（Design System V2）：
// 56px 顶栏 = 移动端菜单按钮 + 面包屑 + 全局搜索 + 用户菜单。
// 用户菜单键盘可访问性保持不变：打开后焦点移入菜单；
// Escape / 外部点击关闭后焦点恢复；退出登录仍为唯一状态改变动作。

import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppIcon from '../ui/AppIcon.vue'
import { useAppStore } from '../../stores/app.js'
import { useAuthStore } from '../../stores/auth.js'
import { searchAPI } from '../../api/search.js'
import { notificationsAPI } from '../../api/notifications.js'
import { ROLE_MAP, statusBadge } from '../../utils/status.js'

const router = useRouter()
const route = computed(() => router.currentRoute?.value || { path: '/', name: '' })
const app = useAppStore()
const auth = useAuthStore()

const props = defineProps({
  variant: {
    type: String,
    default: 'default',
  },
})

const isStudentWorkspace = computed(() => props.variant === 'student-workspace')
const isTeacherWorkspace = computed(() => props.variant === 'teacher-workspace')
const isWorkspace = computed(() => isStudentWorkspace.value || isTeacherWorkspace.value)

const open = ref(false)
const searchText = ref('')
const searchOpen = ref(false)
const searchLoading = ref(false)
const searchResults = ref(null)
const searchEl = ref(null)
const unreadCount = ref(0)
let notificationsTimer = null
const wrapEl = ref(null)
const triggerEl = ref(null)
const menuEl = ref(null)

const displayName = computed(() => auth.user?.nickname || auth.user?.real_name || auth.user?.username || '同学')
const avatarText = computed(() => displayName.value.trim().slice(0, 1))
const roleBadge = computed(() => statusBadge(ROLE_MAP, auth.role))

const PAGE_TITLES = {
  AdminAccounts: '名单与批次', AdminBatch: '批次详情', AdminAccountSecurity: '账号安全与审计',
  StudentHistory: '学习记录', StudentHistoryDetail: '历史提交', Profile: '个人中心',
  StudentHome: '工作台',
  StudentCourses: '课程',
  StudentCourseDetail: '课程详情',
  StudentLesson: '课时学习',
  StudentAssignments: '作业',
  StudentFeedback: '学习反馈',
  StudentAssignmentDetail: '作业详情',
  StudentSubmission: '提交详情',
  StudentExams: '考试',
  StudentExam: '考试',
  StudentExperiments: '实验',
  StudentExperimentDetail: '实验详情',
  StudentNotebook: 'Notebook',
  TeacherHome: '工作台',
  TeacherCourses: '课程管理',
  TeacherChapterManage: '课程管理',
  TeacherAssignments: '作业管理',
  TeacherAssignmentQuestionEdit: '题目编辑',
  TeacherExams: '考试管理',
  TeacherGrades: '成绩统计',
  TeacherGradeDetail: '成绩详情',
  TeacherExamQuestionEdit: '题目编辑',
  TeacherExperiments: '实验管理',
  TeacherExperimentSubmissions: '提交与评分',
  TeacherExperimentSubmissionDetail: '提交详情',
  TeacherUnifiedSubmissions: '提交与评分',
  TeacherJudgeSubmissionDetail: '提交详情',
  TeacherClasses: '班级与学员',
  TeacherGradeStatistics: '成绩统计',
  TeacherEnvironments: '运行环境',
  TeacherSettings: '设置',
  TeacherNotifications: '通知中心',
  TeacherAIGrading: 'AI 评分复核',
  TeacherAIGradingDetail: 'AI 评分详情',
  TeacherLessonEdit: '课时编辑',
  TeacherStudio: '实验模板',
  AdminHome: '管理概览',
  AdminUsers: '用户管理',
  AdminUserEdit: '用户编辑',
  AdminAcademics: '教务管理',
  AdminCourses: '课程管理',
  AdminCourseManage: '课程管理',
  AdminLessonEdit: '课时编辑',
  AdminCourseStudio: '实验模板',
  AdminExperiments: '实验管理',
  AdminEnvironments: '环境档位',
  AdminExperimentSubmissions: '提交与评分',
  AdminExperimentSubmissionDetail: '提交详情',
  AdminAIGrading: 'AI 评分复核',
  AdminAIGradingDetail: 'AI 评分详情',
}

const rootLabel = computed(() => {
  const map = { student: '学习', teacher: '教学', admin: '管理' }
  return map[route.value.path.split('/')[1]] || 'DAI'
})

const currentLabel = computed(() => {
  if (route.value.name && PAGE_TITLES[route.value.name]) return PAGE_TITLES[route.value.name]
  const last = route.value.path.split('/').filter(Boolean).pop() || ''
  return last.replace(/-/g, ' ')
})

const groupLabels = {
  courses: '课程',
  assignments: '作业',
  exams: '考试',
  students: '学生',
  submissions: '提交',
}

const flatResults = computed(() => {
  if (!searchResults.value) return []
  return Object.entries(searchResults.value).flatMap(([group, items]) =>
    (items || []).map((item) => ({ ...item, group })),
  )
})

async function submitSearch() {
  const q = searchText.value.trim()
  app.closeMobileNav()
  if (!q) {
    searchOpen.value = false
    searchResults.value = null
    return
  }
  searchLoading.value = true
  searchOpen.value = true
  try {
    const { data } = await searchAPI.global(q)
    searchResults.value = data
  } catch {
    searchResults.value = null
  } finally {
    searchLoading.value = false
  }
}

function openSearch(item) {
  searchOpen.value = false
  searchResults.value = null
  if (item?.route) router.push(item.route)
}

function onSearchFocus() {
  if (searchText.value.trim()) searchOpen.value = true
}

function onSearchBlur() {
  window.setTimeout(() => { searchOpen.value = false }, 120)
}

function onGlobalKeydown(event) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    searchEl.value?.querySelector('input')?.focus()
  }
}

function openMenu() {
  open.value = true
  nextTick(() => {
    const first = menuEl.value?.querySelector('button, [role="menuitem"]')
    first?.focus()
  })
}

function close() {
  if (!open.value) return
  open.value = false
  nextTick(() => triggerEl.value?.focus())
}

function toggle() { open.value ? close() : openMenu() }

function onKeydown(e) {
  if (e.key === 'Escape') close()
}

function onDocPointerDown(e) {
  if (wrapEl.value && wrapEl.value.contains(e.target)) return
  close()
}

function handleLogout() {
  close()
  auth.logout()
  router.push('/login')
}

async function loadUnreadNotifications() {
  if (auth.role !== 'teacher') return
  try {
    const { data } = await notificationsAPI.list({ unread_only: true })
    unreadCount.value = data?.unread_count || 0
  } catch {
    // 通知服务不可用时静默
  }
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocPointerDown)
  document.addEventListener('keydown', onGlobalKeydown)
  loadUnreadNotifications()
  notificationsTimer = window.setInterval(loadUnreadNotifications, 60000)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocPointerDown)
  document.removeEventListener('keydown', onGlobalKeydown)
  if (notificationsTimer) window.clearInterval(notificationsTimer)
})
</script>

<template>
  <header
    class="header"
    :class="{
      'student-workspace-header': isStudentWorkspace,
      'teacher-workspace-header': isTeacherWorkspace,
      'workspace-header': isWorkspace,
    }"
    @keydown="onKeydown"
  >
    <button
      type="button"
      class="btn btn-ghost btn-icon menu-btn"
      aria-label="打开菜单"
      @click="app.toggleMobileNav()"
    >
      <AppIcon name="drag" :size="17" />
    </button>

    <div class="crumb">
      <span>{{ rootLabel }}</span>
      <span class="sep">/</span>
      <span class="current">{{ currentLabel }}</span>
    </div>

    <div class="grow"></div>

    <div ref="searchEl" class="header-search-wrap">
      <label class="header-search" :class="{ 'workspace-control': isWorkspace }">
        <AppIcon name="search" :size="15" aria-hidden="true" />
        <input
          v-model="searchText"
          :placeholder="isStudentWorkspace ? '搜索课程、作业、考试' : isTeacherWorkspace ? '搜索课程、作业、学生、提交' : '全局搜索：课程 / 作业 / 学生 / 提交'"
          aria-label="全局搜索"
          @keydown.enter.prevent="submitSearch"
          @focus="onSearchFocus"
          @blur="onSearchBlur"
        />
        <kbd>⌘K</kbd>
      </label>
      <div v-if="searchOpen" class="search-results" role="listbox" aria-label="搜索结果">
        <p v-if="searchLoading" class="search-note">搜索中…</p>
        <p v-else-if="flatResults.length === 0" class="search-note">没有找到相关内容</p>
        <template v-else>
          <button
            v-for="item in flatResults"
            :key="item.group + '-' + item.id"
            type="button"
            class="search-result"
            role="option"
            @mousedown.prevent="openSearch(item)"
          >
            <span class="search-result-type">{{ groupLabels[item.group] || item.group }}</span>
            <span class="search-result-main">
              <strong>{{ item.title }}</strong>
              <small>{{ item.subtitle || item.meta || '' }}</small>
            </span>
          </button>
        </template>
      </div>
    </div>

    <button
      type="button"
      class="btn btn-ghost btn-icon notify-btn"
      :class="{ 'workspace-control': isWorkspace }"
      aria-label="通知"
      title="通知"
      @click="router.push(auth.role === 'student' ? '/student' : auth.role === 'admin' ? '/admin' : '/teacher/notifications')"
    >
      <AppIcon name="notification" :size="17" />
      <span v-if="unreadCount > 0" class="notify-badge">{{ unreadCount > 99 ? '99+' : unreadCount }}</span>
    </button>

    <div class="header-right">
      <div ref="wrapEl" class="user-menu-wrap">
        <button
          ref="triggerEl"
          type="button"
          class="user-trigger"
          :class="{ 'workspace-control': isWorkspace }"
          :aria-expanded="open"
          aria-haspopup="menu"
          @click="toggle"
        >
          <span class="user-name">{{ displayName }}</span>
          <span class="user-avatar" aria-hidden="true">
            <span v-if="isWorkspace">{{ avatarText }}</span>
            <AppIcon v-else name="user" :size="17" />
          </span>
          <span class="user-chevron" aria-hidden="true">
            <AppIcon name="chevron-down" :size="15" />
          </span>
        </button>

        <div v-if="open" ref="menuEl" class="user-menu" role="menu" aria-label="用户菜单">
          <div class="user-menu-head">
            <span class="user-menu-name">{{ displayName }}</span>
            <span class="user-menu-role">{{ roleBadge.label }}</span>
          </div>
          <div class="user-menu-sep"></div>
          <button type="button" class="user-menu-item" role="menuitem" @click="open = false; router.push('/account/profile')">个人中心</button>
          <button type="button" class="user-menu-item" role="menuitem" @click="handleLogout">
            <AppIcon name="logout" :size="16" />
            <span>退出登录</span>
          </button>
        </div>
      </div>
    </div>
  </header>
</template>

<style scoped>
/* 顶栏整体度量来自全局 .header；此处仅定义用户菜单浮层与搜索微调。 */
.header-right { position: relative; }

.user-trigger {
  height: 34px;
  padding: 0 6px 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--fg);
}
.user-trigger:hover { background: var(--surface-sunken); border-color: var(--border-strong); }
.user-trigger[aria-expanded='true'] { border-color: var(--accent); }

.user-name {
  font-size: var(--text-base);
  font-weight: 500;
  color: var(--fg);
  white-space: nowrap;
}

.user-avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.user-chevron {
  display: inline-flex;
  color: var(--faint);
  transition: transform 120ms ease;
}
.user-trigger[aria-expanded='true'] .user-chevron { transform: rotate(180deg); }

.user-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  min-width: 210px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: 6px;
  z-index: 200;
}

.user-menu-head {
  padding: 10px 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.user-menu-name { font-size: var(--text-md); font-weight: 600; color: var(--fg); }
.user-menu-role { font-size: var(--text-xs); color: var(--muted); }
.user-menu-sep { height: 1px; background: var(--border); margin: 4px 0; }

.user-menu-item {
  width: 100%;
  justify-content: flex-start;
  padding: 0 12px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-md);
  font-weight: 500;
}
.user-menu-item:hover { background: var(--danger-bg); color: var(--danger); }

@media (max-width: 820px) {
  .user-name { display: none; }
}

.notify-btn { position: relative; }
.notify-badge {
  position: absolute;
  right: -2px;
  top: -2px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: var(--radius-full);
  background: var(--danger);
  color: var(--surface);
  font-size: 10px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
}

.header-search-wrap { position: relative; }
.search-results {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: min(460px, 90vw);
  max-height: 420px;
  overflow: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: 6px;
  z-index: 220;
}
.search-note { margin: 0; padding: 14px 12px; color: var(--muted); font-size: var(--text-sm); }
.search-result {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  text-align: left;
}
.search-result:hover, .search-result:focus-visible { background: var(--surface-sunken); }
.search-result-type {
  flex: none;
  min-width: 52px;
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 700;
}
.search-result-main { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.search-result-main strong { font-size: var(--text-md); font-weight: 500; color: var(--fg); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-result-main small { color: var(--muted); font-size: var(--text-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.workspace-header {
  min-height: 72px;
  height: 72px;
  gap: 8px;
  padding: 12px 28px;
  background: color-mix(in oklch, var(--bg) 91%, transparent);
  backdrop-filter: blur(14px);
}

.workspace-header .crumb {
  gap: 9px;
  font-family: var(--font-body);
  font-size: 13px;
}

.workspace-header .header-search {
  width: min(340px, 30vw);
  min-width: min(340px, 30vw);
  max-width: none;
  height: 44px;
  gap: 10px;
  padding: 0 12px;
  border-radius: var(--radius-md);
  background: var(--surface);
}

.workspace-header .header-search:hover,
.workspace-header .header-search:focus-within {
  border-color: var(--fg);
  background: var(--bg);
}

.workspace-header .header-search input {
  padding: 0;
  border: 0;
  background: transparent;
  box-shadow: none;
  font-size: 13px;
}

.workspace-header .header-search kbd {
  border-radius: var(--radius-sm);
  background: var(--bg);
}

.workspace-header .notify-btn {
  width: 44px;
  min-width: 44px;
  height: 44px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
}

.workspace-header .notify-btn:hover {
  border-color: var(--border);
  background: var(--surface);
}

.workspace-header .user-trigger {
  min-height: 44px;
  height: 44px;
  gap: 9px;
  padding: 0 8px 0 10px;
  border-radius: var(--radius-md);
}

.workspace-header .user-avatar {
  order: -1;
  width: 34px;
  height: 34px;
  background: color-mix(in oklch, var(--fg) 7%, transparent);
  color: var(--fg);
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 700;
}

.workspace-header .user-name {
  font-size: 13px;
  font-weight: 600;
}

@media (max-width: 920px) {
  .workspace-header {
    padding-inline: 18px;
  }

  .workspace-header .menu-btn {
    display: inline-flex;
    width: 44px;
    min-width: 44px;
    height: 44px;
  }

  .workspace-header .crumb {
    display: none;
  }

  .workspace-header .header-search-wrap {
    margin-left: auto;
  }

  .workspace-header .header-search {
    display: flex;
    width: 44px;
    min-width: 44px;
    padding: 0;
    justify-content: center;
  }

  .workspace-header .header-search input,
  .workspace-header .header-search kbd {
    display: none;
  }

  .workspace-header .user-name {
    display: inline;
  }
}

@media (max-width: 680px) {
  .workspace-header .user-trigger {
    width: 44px;
    padding: 0;
    justify-content: center;
    border-color: transparent;
    background: transparent;
  }

  .workspace-header .user-name,
  .workspace-header .user-chevron {
    display: none;
  }
}
</style>
