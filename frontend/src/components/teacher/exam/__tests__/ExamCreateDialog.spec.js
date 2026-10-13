/** ExamCreateDialog 创建考试弹窗组件契约测试：open/close 控制、save 载荷规范化 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ExamCreateDialog from '../ExamCreateDialog.vue'

const courses = [{ id: 2, title: 'Python 程序设计' }]

function mountDialog(props = {}) {
  return mount(ExamCreateDialog, {
    props: { open: false, courses: [], ...props },
  })
}

describe('ExamCreateDialog', () => {
  it('open 为 false 时不渲染', () => {
    const wrapper = mountDialog()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
  })

  it('填写表单并选择课程后提交规范化载荷', async () => {
    const wrapper = mountDialog({ open: true, courses })
    await wrapper.get('[name="title"]').setValue('期末考试')
    await wrapper.get('[name="duration-minutes"]').setValue('90')
    // 打开课程选择弹窗并点选课程
    await wrapper.get('.course-picker').trigger('click')
    await wrapper.get('.course-item').trigger('click')
    await wrapper.get('[data-action="save-exam"]').trigger('click')

    expect(wrapper.emitted('save')[0][0]).toEqual({
      title: '期末考试',
      course_id: 2,
      duration_minutes: 90,
      start_at: null,
      end_at: null,
      // 批次 3：考试类型与考试秩序字段随创建载荷一起提交（默认值）
      exam_kind: 'quiz',
      timing_mode: 'unified',
      shuffle_questions: false,
      shuffle_options: false,
    })
  })

  it('选择考试类型、时间模式与乱序后提交对应载荷', async () => {
    const wrapper = mountDialog({ open: true, courses })
    await wrapper.get('[name="title"]').setValue('期中考试')
    await wrapper.get('.course-picker').trigger('click')
    await wrapper.get('.course-item').trigger('click')
    await wrapper.get('[data-testid="exam-kind"]').setValue('midterm')
    await wrapper.get('[data-testid="exam-timing"]').setValue('per_student')
    await wrapper.get('[data-testid="shuffle-questions"]').setValue(true)
    await wrapper.get('[data-testid="shuffle-options"]').setValue(true)
    await wrapper.get('[data-action="save-exam"]').trigger('click')

    expect(wrapper.emitted('save')[0][0]).toEqual({
      title: '期中考试',
      course_id: 2,
      duration_minutes: 60,
      start_at: null,
      end_at: null,
      exam_kind: 'midterm',
      timing_mode: 'per_student',
      shuffle_questions: true,
      shuffle_options: true,
    })
  })

  it('未填必填项时确定按钮禁用', () => {
    const wrapper = mountDialog({ open: true, courses })
    expect(wrapper.get('[data-action="save-exam"]').attributes('disabled')).toBeDefined()
  })

  it('取消与关闭按钮上抛 close 事件', async () => {
    const wrapper = mountDialog({ open: true, courses })
    await wrapper.findAll('button').find((b) => b.text().includes('取消')).trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('课程弹窗支持手动输入课程 ID 回填', async () => {
    const wrapper = mountDialog({ open: true, courses })
    await wrapper.get('.course-picker').trigger('click')
    await wrapper.get('.course-id-input').setValue('7')
    await wrapper.get('.manual-confirm').trigger('click')
    expect(wrapper.text()).toContain('课程 ID: 7')
  })
})


it('creates a class exam only with an offering from the selected course', async () => {
  const wrapper = mountDialog({ open: true, courses, offerings: [
    { id: 7, course_id: 2, status: 'active', term_name: '秋季', class_name: '一班' },
    { id: 8, course_id: 9, status: 'active', term_name: '秋季', class_name: '其他课程' },
  ] })
  await wrapper.get('[name="title"]').setValue('班级考试')
  await wrapper.get('.course-picker').trigger('click')
  await wrapper.get('.course-item').trigger('click')
  expect(wrapper.get('#exam-offering').text()).not.toContain('其他课程')
  await wrapper.get('#exam-offering').setValue('7')
  await wrapper.get('[data-action="save-exam"]').trigger('click')
  expect(wrapper.emitted('save')[0][0]).toMatchObject({ course_id: 2, offering_id: 7 })
  await wrapper.get('.course-picker').trigger('click')
  await wrapper.get('.course-id-input').setValue('9')
  await wrapper.get('.manual-confirm').trigger('click')
  expect(wrapper.get('#exam-offering').element.value).toBe('')
})
