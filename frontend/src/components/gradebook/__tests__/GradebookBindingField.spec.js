import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import GradebookBindingField from '../GradebookBindingField.vue'

const mocks = vi.hoisted(() => ({ bindingOptions: vi.fn() }))
vi.mock('../../../api/gradebook.js', () => ({ gradebookAPI: mocks }))

const CATEGORIES = [
  { key: 'regular', title: '平时成绩', weight_mode: 'equal' },
  { key: 'midterm', title: '期中成绩', weight_mode: 'equal' },
  { key: 'final', title: '期末成绩', weight_mode: 'equal' },
]
const lastEmit = wrapper => wrapper.emitted('update:modelValue')?.at(-1)?.[0]

// 模拟父组件 v-model：把组件 emit 的值回灌成 prop，才能断言受控状态。
async function mountField(props = {}) {
  const wrapper = mount(GradebookBindingField, {
    props: { modelValue: { enabled: true, category_key: null }, ...props },
  })
  await flushPromises()
  await applyEmit(wrapper)
  return wrapper
}

async function applyEmit(wrapper) {
  const value = lastEmit(wrapper)
  if (value) await wrapper.setProps({ modelValue: value })
  await flushPromises()
}

describe('发布任务：计入课程总评与分类', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.bindingOptions.mockResolvedValue({ data: { categories: CATEGORIES } })
  })

  it('默认勾选，并读取班级成绩册分类后预选默认分类', async () => {
    const wrapper = await mountField({ offeringId: 10, defaultKey: 'midterm' })
    expect(mocks.bindingOptions).toHaveBeenCalledWith(10)
    expect(wrapper.get('input[type="checkbox"]').element.checked).toBe(true)
    expect(lastEmit(wrapper)).toEqual({ enabled: true, category_key: 'midterm' })
    expect(wrapper.get('select').element.value).toBe('midterm')
  })

  it('没有授课实例时用内置四类兜底，仍可改分类', async () => {
    const wrapper = await mountField({ defaultKey: 'regular' })
    expect(mocks.bindingOptions).not.toHaveBeenCalled()
    expect(lastEmit(wrapper)).toEqual({ enabled: true, category_key: 'regular' })
    await wrapper.get('select').setValue('final')
    expect(lastEmit(wrapper)).toEqual({ enabled: true, category_key: 'final' })
  })

  it('分类接口失败时回退内置四类，不阻塞发布', async () => {
    mocks.bindingOptions.mockRejectedValue(new Error('offline'))
    const wrapper = await mountField({ offeringId: 10, defaultKey: 'final' })
    expect(lastEmit(wrapper)).toEqual({ enabled: true, category_key: 'final' })
    expect(wrapper.get('select').element.options).toHaveLength(4)
  })

  it('取消勾选后不再携带分类', async () => {
    const wrapper = await mountField({ offeringId: 10, defaultKey: 'regular' })
    await wrapper.get('input[type="checkbox"]').setValue(false)
    expect(lastEmit(wrapper)).toEqual({ enabled: false, category_key: null })
    await applyEmit(wrapper)
    expect(wrapper.find('select').exists()).toBe(false)
  })

  it('考试类型变化时跟随新的默认分类，教师改过分类后不再跟随', async () => {
    const wrapper = await mountField({ offeringId: 10, defaultKey: 'regular' })
    await wrapper.setProps({ defaultKey: 'midterm' })
    expect(lastEmit(wrapper)).toEqual({ enabled: true, category_key: 'midterm' })
    await applyEmit(wrapper)
    await wrapper.get('select').setValue('final')
    expect(lastEmit(wrapper)).toEqual({ enabled: true, category_key: 'final' })
    await applyEmit(wrapper)
    await wrapper.setProps({ defaultKey: 'regular' })
    expect(lastEmit(wrapper)).toEqual({ enabled: true, category_key: 'final' })
  })

  it('其他类型考试必须由教师显式选择分类，不自动预选', async () => {
    const wrapper = await mountField({ offeringId: 10, defaultKey: '', requireChoice: true })
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(wrapper.get('select').element.value).toBe('')
  })
})
