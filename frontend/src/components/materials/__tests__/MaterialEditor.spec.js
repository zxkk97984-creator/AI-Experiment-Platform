import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import MaterialEditor from '../MaterialEditor.vue'

async function choose(wrapper, name, size) {
  const input = wrapper.find('input[type="file"]')
  const file = new File(['x'], name)
  Object.defineProperty(file, 'size', { value: size })
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  await input.trigger('change')
  return file
}

const props = { ordinaryMaxBytes: 2 * 1048576, largeMaxBytes: 5 * 1048576 }
describe('material upload limits', () => {
  it('renders server limits and stops an oversized ordinary file before save', async () => {
    const wrapper = mount(MaterialEditor, { props })
    await wrapper.find('input[maxlength="200"]').setValue('讲义')
    expect(wrapper.text()).toContain('IPYNB 最大 2 MiB')
    expect(wrapper.text()).toContain('视频、ZIP 最大 5 MiB')
    await choose(wrapper, 'lecture.pdf', props.ordinaryMaxBytes + 1)
    expect(wrapper.get('[role="alert"]').text()).toContain('超过 2 MiB')
    expect(wrapper.get('button[type="submit"]').element.disabled).toBe(true)
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('save')).toBeUndefined()
    const file = await choose(wrapper, 'lecture.pdf', props.ordinaryMaxBytes)
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('save')[0][0].file).toBe(file)
  })

  it.each(['zip', 'mp4', 'webm', 'mov'])('uses the large server limit for .%s rather than category', async extension => {
    const wrapper = mount(MaterialEditor, { props })
    await wrapper.find('input[maxlength="200"]').setValue('教学资源')
    await choose(wrapper, `resource.${extension.toUpperCase()}`, props.largeMaxBytes)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.get('button[type="submit"]').element.disabled).toBe(false)
    await choose(wrapper, `resource.${extension}`, props.largeMaxBytes + 1)
    expect(wrapper.get('[role="alert"]').text()).toContain('超过 5 MiB')
  })

  it('refreshes preflight when server limits change and never invents a missing limit', async () => {
    const wrapper = mount(MaterialEditor, { props })
    await wrapper.find('input[maxlength="200"]').setValue('讲义')
    await choose(wrapper, 'lecture.pdf', props.ordinaryMaxBytes)
    await wrapper.setProps({ ordinaryMaxBytes: 1048576 })
    expect(wrapper.get('[role="alert"]').text()).toContain('超过 1 MiB')
    await wrapper.setProps({ ordinaryMaxBytes: null })
    expect(wrapper.get('[role="alert"]').text()).toContain('上传大小限制暂不可用')
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('save')).toBeUndefined()
  })

  it('an external link does not retain a previously selected upload', async () => {
    const wrapper = mount(MaterialEditor, { props })
    await wrapper.find('input[maxlength="200"]').setValue('官网资料')
    await choose(wrapper, 'lecture.pdf', props.ordinaryMaxBytes + 1)
    await wrapper.findAll('select')[2].setValue('external')
    await wrapper.find('input[type="url"]').setValue('https://www.python.org/')
    await wrapper.find('form').trigger('submit')
    expect(wrapper.emitted('save')[0][0]).toMatchObject({ source: 'external', file: null })
  })
})
