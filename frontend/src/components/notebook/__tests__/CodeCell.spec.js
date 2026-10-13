import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import CodeCell from '../CodeCell.vue'

// 哨兵：只要组件仍 import oneDark（任何属性访问即抛错），CodeMirror 就无法加载、
// waitForCodeMirror 超时 → 本文件测试全部失败，从而保证组件不再依赖深色主题。
vi.mock('@codemirror/theme-one-dark', () => new Proxy({}, {
  get: () => { throw new Error('oneDark 不应再被 CodeCell 使用') },
}))

const wrappers = []
const originalRangeGetClientRects = Range.prototype.getClientRects

beforeAll(() => {
  if (!originalRangeGetClientRects) {
    Object.defineProperty(Range.prototype, 'getClientRects', {
      configurable: true,
      value: () => [],
    })
  }
})

afterAll(() => {
  if (!originalRangeGetClientRects) {
    delete Range.prototype.getClientRects
  }
})

function makeCell(overrides = {}) {
  return {
    id: 'c1',
    type: 'code',
    source: 'print(1)',
    order: 1,
    student_editable: true,
    outputs: null,
    ...overrides,
  }
}

function mountCell(props = {}) {
  const wrapper = mount(CodeCell, {
    props: { cell: makeCell(), ...props },
  })
  wrappers.push(wrapper)
  return wrapper
}

async function waitForCodeMirror(wrapper) {
  await flushPromises()
  await vi.waitFor(() => {
    expect(wrapper.find('.cm-content').exists()).toBe(true)
  })
}

afterEach(() => {
  for (const wrapper of wrappers.splice(0)) {
    wrapper.unmount()
  }
})

describe('CodeCell', () => {
  it('marks the cell with the light theme marker', () => {
    const wrapper = mountCell()
    expect(wrapper.attributes('data-code-theme')).toBe('light')
  })

  it('keeps the light theme marker while CodeMirror is loaded', async () => {
    const wrapper = mountCell()
    await waitForCodeMirror(wrapper)
    expect(wrapper.attributes('data-code-theme')).toBe('light')
    expect(wrapper.find('.cm-editor').exists()).toBe(true)
  })

  it('installs the CodeMirror cursor drawing layer for editable cells', async () => {
    const wrapper = mountCell()
    await waitForCodeMirror(wrapper)

    expect(wrapper.find('.cm-cursorLayer').exists()).toBe(true)
  })

  it('emits editable textarea changes with the cell id', async () => {
    const wrapper = mountCell()
    const textarea = wrapper.get('textarea')

    await textarea.setValue('print(2)')

    expect(wrapper.emitted('update:source')).toEqual([['c1', 'print(2)']])
  })

  it('keeps a readonly editor immutable but still allows running its source', async () => {
    const wrapper = mountCell({
      cell: makeCell({ student_editable: false }),
      readonly: true,
    })
    const textarea = wrapper.get('textarea')

    expect(textarea.attributes('readonly')).toBeDefined()
    expect(textarea.attributes('disabled')).toBeUndefined()
    await textarea.setValue('hacked()')
    expect(wrapper.emitted('update:source')).toBeUndefined()

    await wrapper.get('.btn-run').trigger('click')
    expect(wrapper.emitted('execute')).toEqual([['c1']])
  })

  it('makes the loaded CodeMirror content non-editable for readonly cells', async () => {
    const wrapper = mountCell({ readonly: true })

    await waitForCodeMirror(wrapper)

    expect(wrapper.get('.cm-content').attributes('contenteditable')).toBe('false')
  })

  it('syncs an external source update into CodeMirror without emitting a student edit', async () => {
    const wrapper = mountCell()
    await waitForCodeMirror(wrapper)

    await wrapper.setProps({ cell: makeCell({ source: 'print(99)' }) })
    await flushPromises()

    expect(wrapper.get('.cm-content').text()).toContain('print(99)')
    expect(wrapper.emitted('update:source')).toBeUndefined()
  })

  it.each([
    ['warning', '单元格执行超过 120 秒，已中断本次执行；内存变量已保留'],
    ['error', '内核无响应，已重建：内存中的变量、导入与模型状态已丢失，请从头重新运行全部单元格'],
  ])('renders the %s execution notice verbatim', (level, message) => {
    const wrapper = mountCell({ notice: { level, message } })

    const notice = wrapper.get('.cell-notice')
    expect(notice.classes()).toContain(level)
    expect(notice.attributes('role')).toBe('status')
    expect(notice.text()).toBe(message)
  })

  it('does not render a notice container when there is no notice', () => {
    const wrapper = mountCell()
    expect(wrapper.find('.cell-notice').exists()).toBe(false)
  })

  it.each([
    [{ disabled: true }, 'disabled'],
    [{ isExecuting: true }, 'executing'],
  ])('blocks execution while $1', async (state) => {
    const wrapper = mountCell(state)

    await wrapper.get('.btn-run').trigger('click')

    expect(wrapper.emitted('execute')).toBeUndefined()
  })
})
