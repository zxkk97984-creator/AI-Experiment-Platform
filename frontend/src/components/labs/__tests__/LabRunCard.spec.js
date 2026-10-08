import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
vi.mock('../../../api/labs.js', () => ({ labsAPI: { getRun: vi.fn() } }))
import { labsAPI } from '../../../api/labs.js'
import LabRunCard from '../LabRunCard.vue'
describe('运行日志按需加载', () => {
  it('does not fetch large logs until expanded and then shows the actual training curves', async () => {
    labsAPI.getRun.mockResolvedValue({ data: { status: 'succeeded', logs: '训练完成', result: { curves: [{ epoch: 1, train_loss: 0.9, val_loss: 1 }, { epoch: 2, train_loss: 0.4, val_loss: 0.6 }] } } })
    const wrapper = mount(LabRunCard, { props: { run: { id: 7, status: 'succeeded', kind: 'train', result: {} } } })
    expect(labsAPI.getRun).not.toHaveBeenCalled()
    const details = wrapper.get('details')
    details.element.open = true
    await details.trigger('toggle')
    await flushPromises()
    expect(labsAPI.getRun).toHaveBeenCalledWith(7)
    expect(wrapper.get('pre').text()).toBe('训练完成')
    expect(wrapper.findAll('polyline')).toHaveLength(2)
    wrapper.unmount()
  })
})
