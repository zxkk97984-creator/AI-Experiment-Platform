<script setup>
// AppSidebar：共享认证侧栏（Design System V2）。
// 视觉遵循 dai-ds-v2.css：浅岩灰底、分组标签、36px 导航项、
// 激活态为 2px 墨松绿左栏 + accent-faint 底；底部用户卡保留真实身份信息。
// 路由、权限与菜单数据保持原业务逻辑不变。

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth.js'
import { useAppStore } from '../../stores/app.js'
import { homeForRole } from '../../router/roleHome.js'
import { dashboardAPI } from '../../api/dashboard.js'
import AppIcon from '../ui/AppIcon.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const app = useAppStore()

const props = defineProps({
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

const menuItems = computed(() => {
  const home = (root) => ({
    path: root,
    label: isStudentWorkspace.value ? '工作台' : '首页',
    sub: isStudentWorkspace.value ? 'Workspace' : 'Home',
    icon: 'home',
    key: 'home',
  })
  const base = [
    { path: '/student/history', label: '学习记录', sub: 'History', icon: 'clipboard', key: 'history' },
    { path: '/student/materials', label: '课程资料', sub: 'Materials', icon: 'course', key: 'materials' },
    { path: '/student/offerings', label: '我的教学班', sub: 'Classes', icon: 'user', key: 'offerings' },
    { path: '/student/gradebook', label: '课程成绩', sub: 'Grades', icon: 'chart', key: 'gradebook' },
    { path: '/student/courses',     label: '课程',     sub: 'Courses',     icon: 'course',     key: 'courses' },
    { path: '/student/assignments', label: '作业',     sub: 'Assignments', icon: 'assignment', key: 'assignments' },
    { path: '/student/exams',       label: '考试',     sub: 'Exams',       icon: 'exam',       key: 'exams' },
    { path: '/student/experiments', label: '实验',     sub: 'Lab',         icon: 'experiment', key: 'experiments' },
    { path: '/student/notifications', label: '通知中心', sub: 'Notifications', icon: 'notification', key: 'notifications' },
    { path: '/student/practice',      label: '自主练习', sub: 'Practice',      icon: 'code',         key: 'practice' },
    { path: '/student/practice/mine', label: '我的练习', sub: 'My Practice',   icon: 'clipboard',    key: 'practice-mine' },
  ]
  if (auth.isTeacher) return [
    { path: '/teacher',                label: '工作台',     sub: 'Workbench',     icon: 'home',        key: 'home' },
    { path: '/teacher/materials', label: '课程资料', sub: 'Materials', icon: 'course', key: 'materials' },
    { path: '/teacher/offerings', label: '班级授课', sub: 'Classes', icon: 'user', key: 'offerings' },
    { path: '/teacher/gradebook', label: '课程成绩簿', sub: 'Gradebook', icon: 'chart', key: 'gradebook' },
    { path: '/teacher/courses',        label: '课程管理',   sub: 'Courses',       icon: 'course',      key: 'courses' },
    { path: '/teacher/assignments',    label: '作业管理',   sub: 'Assignments',   icon: 'assignment',  key: 'assignments' },
    { path: '/teacher/experiments',    label: '实验管理',   sub: 'Experiments',   icon: 'experiment',  key: 'experiments' },
    { path: '/teacher/exams',          label: '考试管理',   sub: 'Exams',         icon: 'exam',        key: 'exams' },
    { path: '/teacher/classes',        label: '班级与学员', sub: 'Classes',       icon: 'user',        key: 'classes' },
    { path: '/teacher/submissions/unified', label: '提交与评分', sub: 'Submissions', icon: 'clipboard', key: 'submissions' },
    { path: '/teacher/ai-grading',     label: 'AI 评分复核', sub: 'AI Grading',   icon: 'brain',       key: 'ai-grading' },
    { path: '/teacher/practice/leaderboard', label: '练习排行榜', sub: 'Practice Board', icon: 'trophy', key: 'practice-board' },
    { path: '/teacher/grades',         label: '成绩统计',   sub: 'Grades',        icon: 'chart',       key: 'grades' },
    { path: '/teacher/environments',   label: '运行环境',   sub: 'Environments',  icon: 'cube',        key: 'environments' },
    { path: '/teacher/settings',       label: '设置',       sub: 'Settings',      icon: 'settings',    key: 'settings' },
  ]
  if (auth.isAdmin) return [
    { path: '/admin/accounts', label: '名单与批次', sub: 'Batches', icon: 'clipboard', key: 'accounts' },
    { path: '/admin/account-security', label: '账号安全与审计', sub: 'Security', icon: 'settings', key: 'security' },
    { path: '/admin/users',         label: '用户管理', sub: 'Users',       icon: 'user',       key: 'users' },
    { path: '/admin/academics',     label: '教务管理', sub: 'Academics',   icon: 'course',     key: 'academics' },
    { path: '/admin/materials', label: '课程资料', sub: 'Materials', icon: 'course', key: 'materials' },
    { path: '/admin/offerings', label: '授课安排', sub: 'Offerings', icon: 'user', key: 'offerings' },
    { path: '/admin/gradebook', label: '课程成绩簿', sub: 'Gradebook', icon: 'chart', key: 'gradebook' },
    { path: '/admin/courses',       label: '课程管理', sub: 'Courses',     icon: 'course',     key: 'courses' },
    { path: '/admin/experiments',   label: '实验管理', sub: 'Experiments', icon: 'experiment', key: 'experiments' },
    { path: '/admin/environments',  label: '环境档位', sub: 'Environments', icon: 'experiment', key: 'environments' },
    { path: '/admin/ai-grading',    label: 'AI 评分复核', sub: 'AI Grading', icon: 'brain',      key: 'ai-grading' },
    { path: '/admin/practice/problems', label: '练习题库', sub: 'Practice',          icon: 'code',     key: 'practice-problems' },
    { path: '/admin/practice/appeals',  label: '练习复核', sub: 'Practice Review',   icon: 'clipboard', key: 'practice-appeals' },
    { path: '/admin/practice/settings', label: '练习设置', sub: 'Practice Settings', icon: 'settings', key: 'practice-settings' },
    { path: '/admin/practice/usage',    label: '练习用量', sub: 'Practice Usage',    icon: 'chart',    key: 'practice-usage' },
    { path: '/admin/logs',          label: '系统日志',     sub: 'System Logs', icon: 'clipboard',  key: 'logs' },
  ]
  return [home('/student'), ...base]
})

// 侧栏分组：视觉按 V2「分组标签 + 组间距」；菜单数据与权限仍来自 menuItems
const navGroups = computed(() => {
  const items = menuItems.value
  const pick = (keys) => items.filter((item) => keys.includes(item.key))
  if (auth.isTeacher) {
    return [
      { label: '教学', items: pick(['home', 'courses', 'materials', 'offerings', 'assignments', 'experiments', 'exams', 'classes']) },
      { label: '评分', items: pick(['submissions', 'ai-grading', 'gradebook', 'grades', 'practice-board']) },
      { label: '系统', items: pick(['environments', 'settings']) },
    ]
  }
  if (auth.isAdmin) {
    return [
      { label: '教务', items: pick(['users', 'academics', 'offerings', 'courses', 'materials', 'experiments']) },
      { label: '评分', items: pick(['ai-grading', 'gradebook', 'practice-problems', 'practice-appeals']) },
      { label: '系统', items: pick(['environments', 'logs', 'accounts', 'security', 'practice-settings', 'practice-usage']) },
    ]
  }
  return [{ label: '学习', items }]
})

const displayName = computed(() => auth.user?.nickname || auth.user?.real_name || auth.user?.username || '同学')
const avatarText = computed(() => displayName.value.trim().slice(0, 1))
const roleText = computed(() => {
  const map = { student: '学生', teacher: '教师', admin: '管理员' }
  const role = map[auth.role] || auth.role || ''
  if (auth.role === 'student' && props.studentContext.className) {
    return `${role} · ${props.studentContext.className}`
  }
  if (auth.role === 'teacher' && auth.user?.department) return `${role} · ${auth.user.department}`
  return role
})

const teacherCounts = ref({ pending_grading_count: 0, pending_review_count: 0 })
let countsTimer = null

async function loadTeacherCounts() {
  if (!auth.isTeacher) return
  try {
    const { data } = await dashboardAPI.teacherCounts()
    teacherCounts.value = data
  } catch {
    // 徽标加载失败静默，不阻塞导航
  }
}

function submissionBadge() {
  if (!auth.isTeacher) return 0
  return Number(teacherCounts.value.pending_grading_count || 0)
}

onMounted(() => {
  loadTeacherCounts()
  if (auth.isTeacher) countsTimer = window.setInterval(loadTeacherCounts, 60000)
})
onBeforeUnmount(() => {
  if (countsTimer) window.clearInterval(countsTimer)
})

function isActive(path) {
  if (path === '/student/assignments' && route.path.startsWith('/student/submissions')) return true
  if (path === '/teacher/submissions/unified' && route.path.startsWith('/teacher/submissions')) return true
  if (path === '/admin/submissions' && route.path.startsWith('/admin/submissions')) return true
  if (path === '/student') return route.path === path || route.path === '/student/feedback'
  if (path === '/teacher') return route.path === path
  return route.path.startsWith(path)
}

function navigate(path) {
  router.push(path)
}
</script>

<template>
  <aside
    class="sidebar"
    :class="{
      collapsed: !isWorkspace && app.sidebarCollapsed,
      'student-workspace-sidebar': isStudentWorkspace,
      'teacher-workspace-sidebar': isTeacherWorkspace,
      'workspace-sidebar': isWorkspace,
    }"
    :aria-label="isStudentWorkspace ? '学生端主导航' : isTeacherWorkspace ? '教师端主导航' : undefined"
  >
    <button
      type="button"
      class="logo sidebar-head"
      aria-label="返回首页"
      @click="navigate(homeForRole(auth.role))"
    >
      <template v-if="isWorkspace">
        <span class="workspace-brand-mark" aria-hidden="true">DAI</span>
        <span class="workspace-brand-copy">
          <strong class="workspace-brand-name">人工智能基础实验平台</strong>
          <small class="workspace-brand-subtitle">{{ isStudentWorkspace ? '学生学习空间' : '教师教学空间' }}</small>
        </span>
      </template>
      <span v-else class="wordmark"><span class="mark">人工智能</span><small>基础实验平台</small></span>
    </button>

    <nav class="sidebar-nav">
      <div v-for="group in navGroups" :key="group.label" class="nav-group">
        <div class="nav-label">{{ group.label }}</div>
        <button
          v-for="item in group.items"
          :key="item.path"
          type="button"
          class="nav-item"
          :class="{ active: isActive(item.path) }"
          :aria-label="item.label"
          :aria-current="isActive(item.path) ? 'page' : undefined"
          :title="!isWorkspace && app.sidebarCollapsed ? item.label : undefined"
          @click="navigate(item.path)"
        >
          <AppIcon :name="item.icon" :size="17" aria-hidden="true" />
          <span class="nav-text">{{ item.label }}</span>
          <span v-if="item.key === 'submissions' && submissionBadge() > 0" class="nav-badge">{{ submissionBadge() }}</span>
        </button>
      </div>
    </nav>

    <div class="sidebar-foot" :class="{ 'workspace-profile-block': isWorkspace }">
      <div class="user-card">
        <span class="avatar" aria-hidden="true">{{ avatarText }}</span>
        <div class="grow">
          <div class="u-name">{{ displayName }}</div>
          <div class="u-role">{{ roleText }}</div>
        </div>
      </div>
      <button
        v-if="!isWorkspace"
        class="collapse-btn"
        type="button"
        @click="app.toggleSidebar()"
        :title="app.sidebarCollapsed ? '展开侧栏' : '收起侧栏'"
        :aria-label="app.sidebarCollapsed ? '展开侧栏' : '收起侧栏'"
      >
        <AppIcon :name="app.sidebarCollapsed ? 'chevron-right' : 'back'" :size="15" aria-hidden="true" />
        <span class="collapse-text">收起</span>
      </button>
    </div>
  </aside>
</template>

<style scoped>
/* 全部可复用视觉来自全局 dai-ds-v2.css；
   此处只保留组件内组合所需的局部结构，不新增颜色 / 圆角 / 字号。 */
.logo {
  width: 100%;
  border: 0;
  background: transparent;
  justify-content: flex-start;
  text-align: left;
}
.logo:hover { background: transparent; border-color: transparent; }
.nav-item { width: auto; margin: 2px 0 0; border: 0; background: transparent; }
.sidebar-foot { display: flex; flex-direction: column; gap: var(--space-1); }
.collapse-btn {
  width: 100%;
  justify-content: center;
  color: var(--faint);
  background: transparent;
  border-color: transparent;
  font-size: var(--text-sm);
}
.collapse-btn:hover { color: var(--fg); background: var(--surface-sunken); border-color: transparent; }
.collapse-text { font-size: var(--text-xs); letter-spacing: 0.02em; }

.workspace-sidebar .sidebar-head {
  min-height: 72px;
  height: 72px;
  gap: 11px;
  padding: 0 22px;
}

.workspace-brand-mark {
  width: 38px;
  flex: 0 0 38px;
  font-family: var(--font-display);
  font-size: 21px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.06em;
  color: var(--fg);
}

.workspace-brand-copy {
  min-width: 0;
  display: block;
}

.workspace-brand-name {
  display: block;
  font-family: var(--font-display);
  font-size: 16px;
  line-height: 1.15;
  white-space: nowrap;
}

.workspace-brand-subtitle {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 400;
  line-height: 1.3;
  letter-spacing: 0.08em;
  white-space: nowrap;
}

.workspace-sidebar .sidebar-nav {
  padding: 0 10px;
}

.workspace-sidebar .nav-label {
  padding: 25px 12px 9px;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.12em;
}

.workspace-sidebar .nav-item {
  width: 100%;
  height: 46px;
  justify-content: flex-start;
  margin: 0 0 4px;
  gap: 12px;
  padding: 0 12px;
  border-radius: var(--radius-md);
  color: var(--muted);
  font-size: 14px;
  font-weight: 500;
}

.workspace-sidebar .nav-item:hover {
  background: color-mix(in oklch, var(--fg) 5%, transparent);
  color: var(--fg);
}

.workspace-sidebar .nav-item.active {
  background: var(--surface);
  color: var(--fg);
  font-weight: 600;
}

.workspace-sidebar .nav-item.active::before {
  left: -10px;
  top: 12px;
  bottom: 12px;
  width: 3px;
  border-radius: 0 3px 3px 0;
}

.workspace-sidebar .nav-item :deep(svg) {
  width: 18px;
  height: 18px;
}

.workspace-sidebar .workspace-profile-block {
  min-height: 74px;
  padding: 12px 18px;
  gap: 0;
}

.workspace-sidebar .user-card {
  width: 100%;
  gap: 10px;
  padding: 0;
}

.workspace-sidebar .avatar {
  width: 34px;
  height: 34px;
  background: color-mix(in oklch, var(--fg) 7%, transparent);
  color: var(--fg);
  font-family: var(--font-display);
  font-weight: 700;
}

.workspace-sidebar .user-card .u-name {
  font-size: 13px;
}

.workspace-sidebar .user-card .u-role {
  margin-top: 2px;
  font-size: 11px;
}
</style>
