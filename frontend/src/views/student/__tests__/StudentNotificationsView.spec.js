// 学生通知中心（批次 2）：加载、空态、未读标记与跳转
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  markRead: vi.fn(),
  markAllRead: vi.fn(),
}))

const appeals = vi.hoisted(() => ({ mine: vi.fn(), reply: vi.fn(), withdraw: vi.fn(), create: vi.fn() }))

vi.mock('../../../api/notifications.js', () => ({ notificationsAPI: api }))
vi.mock('../../../api/appeals.js', () => ({ appealsAPI: appeals }))

import StudentNotificationsView from '../StudentNotificationsView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/student/notifications', component: StudentNotificationsView },
    { path: '/student/submissions/:id', component: { template: '<div class="submission" />' } },
  ],
})

async function mountView() {
  router.push('/student/notifications')
  await router.isReady()
  return mount(StudentNotificationsView, {
    global: {
      plugins: [router, createPinia()],
      stubs: {
        AppLayout: { template: '<div class="layout-stub"><slot /></div>' },
        AppIcon: { template: '<span class="icon-stub" />', props: ['name', 'size'] },
        DsSelect: { template: '<select><slot /></select>', props: ['modelValue'] },
      },
    },
  })
}

describe('StudentNotificationsView', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    appeals.mine.mockResolvedValue({ data: { items: [] } })
  })

  it('渲染派生通知并显示未读数', async () => {
    api.list.mockResolvedValue({
      data: {
        items: [
          { id: 1, type: 'grading', title: 'AI 评分已完成', content: '你的作业提交已评分：80 分', route: '/student/submissions/3', is_read: false, created_at: '2026-10-13T01:00:00Z' },
        ],
        unread_count: 1,
        total: 1,
      },
    })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('AI 评分已完成')
    expect(wrapper.text()).toContain('1 条未读')
    expect(api.list).toHaveBeenCalledWith({})
  })

  it('空态提示不隐藏未读筛选说明', async () => {
    api.list.mockResolvedValue({ data: { items: [], unread_count: 0, total: 0 } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('暂无通知')
  })

  it('点击查看详情会先标记已读再跳转', async () => {
    api.list.mockResolvedValue({
      data: {
        items: [{ id: 7, type: 'appeal', title: '成绩异议已改分', content: '原分 60 → 82', route: '/student/submissions/3', is_read: false, created_at: '2026-10-13T01:00:00Z' }],
        unread_count: 1,
        total: 1,
      },
    })
    api.markRead.mockResolvedValue({ status: 204 })
    const wrapper = await mountView()
    await flushPromises()
    await wrapper.find('.text-btn').trigger('click')
    await flushPromises()
    expect(api.markRead).toHaveBeenCalledWith(7)
    expect(router.currentRoute.value.path).toBe('/student/submissions/3')
  })

  it('我的成绩异议页签展示进度并支持补充说明', async () => {
    api.list.mockResolvedValue({ data: { items: [], unread_count: 0, total: 0 } })
    appeals.mine.mockResolvedValue({
      data: {
        items: [{
          id: 5, target_kind: 'submission', target_id: 3, status: 'pending',
          reason: '老师您好，这道题的第三组测试我认为我的解法是正确的，请求复核。',
          student_reply: null, handler_comment: null, score_before: 60, score_after: null,
          title: '正数求和', can_withdraw: true,
        }],
      },
    })
    appeals.reply.mockResolvedValue({ data: { ok: true, status: 'pending' } })
    const wrapper = await mountView()
    await flushPromises()
    await wrapper.findAll('.tab')[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('待处理')
    expect(wrapper.text()).toContain('正数求和')

    await wrapper.find('.notice-foot .text-btn').trigger('click')
    await wrapper.find('.handle-card textarea').setValue('补充：我使用了迭代写法')
    await wrapper.find('.handle-actions .btn-primary').trigger('click')
    await flushPromises()
    expect(appeals.reply).toHaveBeenCalledWith(5, '补充：我使用了迭代写法')
  })

  it('加载失败显示重试入口', async () => {
    api.list.mockRejectedValue(new Error('boom'))
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('加载失败')
  })
})
