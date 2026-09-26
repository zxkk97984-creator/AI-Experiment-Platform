import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import DsSelect from '../DsSelect.vue'

function mountSelect(props = {}) {
  return mount(DsSelect, {
    attachTo: document.body,
    props: { modelValue: 'warning', 'aria-label': '级别', ...props },
    slots: {
      default: '<option value="all">全部</option><option value="warning">警告</option><option value="error" disabled>错误（禁用）</option>',
    },
  })
}

describe('DsSelect', () => {
  it('在页面浮层中渲染选项并选择后恢复触发器焦点', async () => {
    const wrapper = mountSelect()
    await flushPromises()
    const trigger = wrapper.get('.ds-select-trigger')
    await trigger.trigger('click')
    const options = [...document.body.querySelectorAll('.ds-select-menu [role="option"]')]
    expect(options).toHaveLength(3)
    expect(options[1].getAttribute('aria-selected')).toBe('true')
    options[0].click()
    await flushPromises()
    expect(wrapper.emitted('update:modelValue')[0]).toEqual(['all'])
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('支持方向键、Escape 和禁用选项', async () => {
    const wrapper = mountSelect()
    await flushPromises()
    const trigger = wrapper.get('.ds-select-trigger')
    await trigger.trigger('keydown', { key: 'ArrowDown' })
    await flushPromises()
    expect(document.activeElement?.textContent).toContain('警告')
    await document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await flushPromises()
    expect(document.activeElement?.textContent).toContain('全部')
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(document.body.querySelector('.ds-select-menu')).toBeNull()
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('保留原生表单值、必填和禁用语义', async () => {
    const wrapper = mountSelect({ required: true, name: 'level', disabled: true })
    await flushPromises()
    const native = wrapper.get('select')
    expect(native.element.value).toBe('warning')
    expect(native.attributes('required')).toBeDefined()
    expect(native.attributes('disabled')).toBeDefined()
    expect(wrapper.get('.ds-select-trigger').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})
