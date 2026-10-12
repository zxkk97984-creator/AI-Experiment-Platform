// 管理员练习设置：判题方式切换、预检未就绪清单、AI 预检 409 拦截与额度耗尽
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'

const api = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  getAiPreflight: vi.fn(),
}))

vi.mock('../../../api/practice.js', () => ({ practiceAPI: {}, adminPracticeAPI: api }))

import PracticeSettingsView from '../PracticeSettingsView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/admin/practice/settings', component: PracticeSettingsView },
    { path: '/admin/practice/problems/:id/edit', component: { template: '<div class="edit" />' } },
  ],
})

const settings = {
  grading_mode: 'deterministic',
  daily_submission_cap_per_student: 20,
  daily_ai_budget_tokens: 100000,
  daily_ai_budget_amount: 10,
  max_concurrent_practice_jobs: 4,
}

const preflightReady = { ready: true, problems: [], checked_at: '2026-10-13T02:00:00Z' }
const preflightBlocked = {
  ready: false,
  problems: [{ id: 4, title: '统计列表中所有正数的和', status: 'published', missing: ['锁定评分标准', '启用评分版本'] }],
  checked_at: '2026-10-13T02:00:00Z',
}

async function mountView() {
  router.push('/admin/practice/settings')
  await router.isReady()
  return mount(PracticeSettingsView, {
    global: {
      plugins: [router, createPinia()],
      stubs: { AppLayout: { template: '<div class="layout"><slot /></div>' } },
    },
  })
}

describe('PracticeSettingsView', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.getSettings.mockResolvedValue({ data: settings })
    api.getAiPreflight.mockResolvedValue({ data: preflightReady })
  })

  it('渲染当前判题方式与额度配置', async () => {
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('当前：普通判题')
    expect(wrapper.text()).toContain('AI 预检通过')
    const capInput = wrapper.findAll('input[type="number"]')[0]
    expect(capInput.element.value).toBe('20')
  })

  it('预检未就绪时列出缺少的评分标准', async () => {
    api.getAiPreflight.mockResolvedValue({ data: preflightBlocked })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('AI 预检有 1 道题未就绪')
    expect(wrapper.text()).toContain('锁定评分标准')
  })

  it('切到 AI 判题走 PATCH，并提示只影响新提交', async () => {
    api.updateSettings.mockResolvedValue({ data: { ...settings, grading_mode: 'ai' } })
    const wrapper = await mountView()
    await flushPromises()
    const aiBtn = wrapper.findAll('button').find((btn) => btn.text() === 'AI 综合判题')
    await aiBtn.trigger('click')
    await flushPromises()
    expect(api.updateSettings).toHaveBeenCalledWith({ grading_mode: 'ai' })
  })

  it('预检未通过时展示后端返回的阻塞清单', async () => {
    api.updateSettings.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { code: 'AI_PREFLIGHT_FAILED', message: 'AI 预检未通过', problems: preflightBlocked.problems } },
      },
    })
    const wrapper = await mountView()
    await flushPromises()
    const aiBtn = wrapper.findAll('button').find((btn) => btn.text() === 'AI 综合判题')
    await aiBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('切换被拒绝，以下题目尚未准备完成')
    expect(wrapper.text()).toContain('统计列表中所有正数的和')
  })

  it('权限不足时不展示设置表单', async () => {
    api.getSettings.mockRejectedValue({ response: { status: 403, data: {} } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('权限不足')
    expect(wrapper.find('.settings-grid').exists()).toBe(false)
  })

  it('AI 预算耗尽时提示额度已用完', async () => {
    api.getAiPreflight.mockResolvedValue({ data: { ...preflightReady, ai_budget: { exhausted: true, tokens_used: 100000, tokens_budget: 100000 } } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('今日额度已用完')
  })

  it('判题队列积压时提示并发占用', async () => {
    api.getAiPreflight.mockResolvedValue({ data: { ...preflightReady, queue: { queued: 3, running: 4, max_concurrent: 4 } } })
    const wrapper = await mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('练习判题队列积压 3 项')
  })
})
