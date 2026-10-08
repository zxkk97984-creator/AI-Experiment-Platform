import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ReportCell from '../ReportCell.vue'
describe('学生实验报告', () => {
  it('emits source updates and renders a sanitized Markdown preview', async () => {
    const wrapper = mount(ReportCell, { props: { cell: { id: 'report', type: 'markdown', source: '# 结果\n<script>alert(1)</script>' } } })
    await wrapper.get('textarea').setValue('训练集的损失低于验证集。')
    expect(wrapper.emitted('update:source')[0]).toEqual(['report', '训练集的损失低于验证集。'])
    await wrapper.get('button').trigger('click')
    expect(wrapper.get('.cell-body h1').text()).toBe('结果')
    expect(wrapper.find('.cell-body script').exists()).toBe(false)
    expect(wrapper.find('textarea').exists()).toBe(false)
  })
})
