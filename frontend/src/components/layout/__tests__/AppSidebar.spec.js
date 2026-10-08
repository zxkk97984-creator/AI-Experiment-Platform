import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AppSidebar from '../AppSidebar.vue'
import { useAuthStore } from '../../../stores/auth.js'
import { useAppStore } from '../../../stores/app.js'

const routerState = vi.hoisted(() => ({
  push: vi.fn(),
  path: '/student',
}))

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useRoute: () => ({ path: routerState.path }),
    useRouter: () => ({ push: routerState.push }),
  }
})

describe('AppSidebar 角色首页导航', () => {
  function mountAs(role, path, props = {}) {
    routerState.path = path
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAuthStore()
    const app = useAppStore()
    auth.setUser({ id: 1, username: role, real_name: role, role })
    return { wrapper: mount(AppSidebar, { props, global: { plugins: [pinia] } }), auth, app }
  }

  beforeEach(() => {
    localStorage.clear()
    vi.resetAllMocks()
    setActivePinia(createPinia())
  })

  it('学生侧栏首个导航项为首页且根路由高亮', async () => {
    const { wrapper } = mountAs('student', '/student')
    const navItems = wrapper.findAll('.nav-item')
    expect(navItems[0].text()).toContain('首页')
    expect(navItems[0].classes()).toContain('active')
    await navItems[0].trigger('click')
    expect(routerState.push).toHaveBeenCalledWith('/student')
  })

  it('学生工作台变体使用专属品牌、工作台导航与学期信息，且不可折叠', async () => {
    const { wrapper, app } = mountAs('student', '/student', {
      variant: 'student-workspace',
      studentContext: {
        className: '24621601班',
        currentTerm: '2026-2027 学年第一学期（秋季）',
      },
    })
    app.sidebarCollapsed = true
    await wrapper.vm.$nextTick()

    expect(wrapper.get('.sidebar').classes()).toContain('student-workspace-sidebar')
    expect(wrapper.get('.sidebar').classes()).not.toContain('collapsed')
    expect(wrapper.findAll('.nav-item')[0].text()).toContain('工作台')
    expect(wrapper.get('.workspace-brand-mark').text()).toBe('DAI')
    expect(wrapper.get('.workspace-brand-name').text()).toBe('人工智能基础实验平台')
    expect(wrapper.get('.workspace-brand-subtitle').text()).toBe('学生学习空间')
    expect(wrapper.find('.semester-note').exists()).toBe(false)
    expect(wrapper.get('.u-role').text()).toBe('学生 · 24621601班')
    expect(wrapper.find('.collapse-btn').exists()).toBe(false)
  })

  it('活动路由项声明 aria-current="page"，其余项不声明（TASK-024）', () => {
    const { wrapper } = mountAs('student', '/student/courses')
    const courses = wrapper.findAll('.nav-item').find((n) => n.attributes('aria-label') === '课程')
    expect(courses.attributes('aria-current')).toBe('page')
    for (const item of wrapper.findAll('.nav-item')) {
      if (item.element === courses.element) continue
      expect(item.attributes('aria-current')).toBeUndefined()
    }
  })

  it('折叠按钮 aria-label 中文化并随状态切换（TASK-024）', async () => {
    const { wrapper } = mountAs('student', '/student')
    const collapse = wrapper.get('.collapse-btn')
    expect(collapse.attributes('aria-label')).toBe('收起侧栏')
    await collapse.trigger('click')
    expect(wrapper.get('.collapse-btn').attributes('aria-label')).toBe('展开侧栏')
  })

  it('学生首页项在子路由不高亮', () => {
    const { wrapper } = mountAs('student', '/student/courses')
    expect(wrapper.findAll('.nav-item')[0].classes()).not.toContain('active')
  })

  it('教师侧栏首个导航项为首页并指向 /teacher', async () => {
    const { wrapper } = mountAs('teacher', '/teacher')
    const navItems = wrapper.findAll('.nav-item')
    expect(navItems[0].text()).toContain('工作台')
    expect(navItems[0].classes()).toContain('active')
    await navItems[0].trigger('click')
    expect(routerState.push).toHaveBeenCalledWith('/teacher')
  })

  it('教师首页项在子路由不高亮', () => {
    const { wrapper } = mountAs('teacher', '/teacher/courses')
    expect(wrapper.findAll('.nav-item')[0].classes()).not.toContain('active')
  })

  it('教师工作台变体显示教师教学空间且不提供折叠按钮', () => {
    const { wrapper } = mountAs('teacher', '/teacher', { variant: 'teacher-workspace' })

    expect(wrapper.get('.sidebar').classes()).toContain('teacher-workspace-sidebar')
    expect(wrapper.text()).toContain('教师教学空间')
    expect(wrapper.attributes('aria-label')).toBe('教师端主导航')
    expect(wrapper.find('.collapse-btn').exists()).toBe(false)
  })

  it('点击 logo 返回角色首页', async () => {
    const { wrapper } = mountAs('student', '/student/courses')
    await wrapper.get('.logo').trigger('click')
    expect(routerState.push).toHaveBeenCalledWith('/student')
  })

  it('logo 是真实 button 元素，原生键盘可聚焦并带可访问名称', async () => {
    const { wrapper } = mountAs('student', '/student')
    const logo = wrapper.get('.logo')
    expect(logo.element.tagName).toBe('BUTTON')
    expect(logo.attributes('type')).toBe('button')
    expect(logo.attributes('aria-label')).toBe('返回首页')
    // 原生 button：默认可获得键盘焦点（tabindex 无需手动设置）
    expect(logo.element.tabIndex).toBe(0)
    // 原生 button 支持回车/空格激活；点击行为与既有导航一致
    await logo.trigger('click')
    expect(routerState.push).toHaveBeenCalledWith('/student')
  })

  it('导航项始终提供可访问名称（图标栏下文本隐藏时依赖 aria-label）', () => {
    const { wrapper } = mountAs('teacher', '/teacher')
    const navItems = wrapper.findAll('.nav-item')
    expect(navItems.length).toBeGreaterThan(0)
    for (const item of navItems) {
      expect(item.attributes('aria-label')).toBeTruthy()
    }
    expect(navItems[0].attributes('aria-label')).toBe('工作台')
    expect(navItems.find(n => n.attributes('aria-label') === '课程管理')).toBeTruthy()
  })

  it('/student/feedback 镜像参考图 01，高亮首页', () => {
    const { wrapper } = mountAs('student', '/student/feedback')
    const navItems = wrapper.findAll('.nav-item')
    expect(navItems[0].text()).toContain('首页')
    expect(navItems[0].classes()).toContain('active')
  })

  it('折叠按钮切换 collapsed 类并保留导航可访问名称', async () => {
    const { wrapper } = mountAs('student', '/student')
    const sidebar = wrapper.find('.sidebar')
    expect(sidebar.classes()).not.toContain('collapsed')
    await wrapper.get('.collapse-btn').trigger('click')
    expect(sidebar.classes()).toContain('collapsed')
    for (const item of wrapper.findAll('.nav-item')) {
      expect(item.attributes('aria-label')).toBeTruthy()
    }
  })
})

describe('AppSidebar admin 菜单', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetAllMocks()
    setActivePinia(createPinia())
  })

  it('管理员侧栏包含环境档位入口并指向 /admin/environments', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAuthStore()
    auth.setUser({ id: 2, username: 'admin', real_name: 'Admin', role: 'admin' })
    const wrapper = mount(AppSidebar, { global: { plugins: [pinia] } })
    const labels = wrapper.findAll('.nav-item').map((i) => i.attributes('aria-label'))
    expect(labels).toContain('环境档位')
    const envItem = wrapper.findAll('.nav-item').find(
      (i) => i.attributes('aria-label') === '环境档位',
    )
    await envItem.trigger('click')
    expect(routerState.push).toHaveBeenCalledWith('/admin/environments')
  })

  it('/admin/environments 子路由高亮环境档位项', () => {
    routerState.path = '/admin/environments'
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAuthStore()
    auth.setUser({ id: 2, username: 'admin', real_name: 'Admin', role: 'admin' })
    const wrapper = mount(AppSidebar, { global: { plugins: [pinia] } })
    const envItem = wrapper.findAll('.nav-item').find(
      (i) => i.attributes('aria-label') === '环境档位',
    )
    expect(envItem.classes()).toContain('active')
  })
})
