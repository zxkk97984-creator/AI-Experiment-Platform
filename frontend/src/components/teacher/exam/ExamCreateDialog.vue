<script setup>
import { vDialogFocus } from '../../../directives/dialogFocus.js'
/** 创建考试弹窗：表单与课程选择在组件内，save 上抛规范化载荷，完整校验与 API 由父组件负责。 */
import { computed, ref } from 'vue'
import AppIcon from '../../ui/AppIcon.vue'

const props = defineProps({
  open: { type: Boolean, required: true },
  courses: { type: Array, required: true },
})
const emit = defineEmits(['save', 'close'])

const form = ref({ title: '', course_id: '', duration_minutes: 60, start_at: '', end_at: '' })
const courseModalOpen = ref(false)
const manualCourseId = ref('')

const selectedCourse = computed(
  () => props.courses.find((course) => String(course.id) === String(form.value.course_id)) || null,
)
const canSave = computed(() => Boolean(form.value.title.trim() && form.value.course_id && Number(form.value.duration_minutes) > 0))

function openCourseModal() {
  manualCourseId.value = form.value.course_id ? String(form.value.course_id) : ''
  courseModalOpen.value = true
}

function closeCourseModal() {
  courseModalOpen.value = false
}

function pickCourse(course) {
  form.value.course_id = String(course.id)
  courseModalOpen.value = false
}

function confirmManualCourse() {
  const id = manualCourseId.value.trim()
  if (!id) return
  form.value.course_id = id
  courseModalOpen.value = false
}

function save() {
  emit('save', {
    title: form.value.title.trim(),
    course_id: Number(form.value.course_id),
    duration_minutes: Number(form.value.duration_minutes),
    start_at: form.value.start_at || null,
    end_at: form.value.end_at || null,
  })
}
</script>

<template>
  <div v-if="open" class="modal-backdrop create-backdrop" @click.self="emit('close')">
    <div class="create-panel create-form" v-dialog-focus role="dialog" aria-modal="true" aria-label="创建考试">
      <header class="create-heading"><strong>创建考试</strong><button class="create-close" aria-label="关闭" @click="emit('close')"><AppIcon name="close" :size="18" /></button></header>
      <div class="form-group"><label>考试名称</label><input v-model="form.title" name="title" placeholder="输入考试名称" /></div>
      <div class="grid-2"><div class="form-group"><label>课程</label><button type="button" class="course-picker" @click="openCourseModal"><span v-if="selectedCourse">{{ selectedCourse.title }}（ID: {{ selectedCourse.id }}）</span><span v-else-if="form.course_id">课程 ID: {{ form.course_id }}</span><span v-else class="placeholder">选择课程</span><AppIcon name="chevron-down" :size="17" /></button></div><div class="form-group"><label>时长（分钟）</label><input v-model.number="form.duration_minutes" name="duration-minutes" type="number" min="1" /></div></div>
      <div class="grid-2"><div class="form-group"><label>开始时间</label><input v-model="form.start_at" type="datetime-local" /></div><div class="form-group"><label>最晚进入时间</label><input v-model="form.end_at" type="datetime-local" /></div></div>
      <p class="form-hint">“最晚进入时间”只限制新学生进入；已开始的学生始终获得完整考试时长。成绩、题目和答案默认不公开。</p>
      <div class="create-actions"><button class="btn-ghost" @click="emit('close')">取消</button><button class="btn-primary" data-action="save-exam" :disabled="!canSave" @click="save">确定</button></div>
    </div>

    <div v-if="courseModalOpen" class="modal-backdrop create-backdrop" @click.self="closeCourseModal">
      <div class="create-panel course-picker-panel" v-dialog-focus role="dialog" aria-modal="true" aria-label="选择课程">
        <header class="create-heading"><strong>选择课程</strong><button class="create-close" aria-label="关闭" @click="closeCourseModal"><AppIcon name="close" :size="18" /></button></header>
        <div class="manual-row"><input v-model="manualCourseId" class="course-id-input" placeholder="输入课程 ID" @keyup.enter="confirmManualCourse" /><button class="btn-primary manual-confirm" :disabled="!manualCourseId.trim()" @click="confirmManualCourse">确定</button></div>
        <div class="course-list"><button v-for="course in courses" :key="course.id" class="course-item" :class="{ active: String(course.id) === form.course_id }" @click="pickCourse(course)">{{ course.title }}（ID: {{ course.id }}）</button><p v-if="courses.length === 0" class="empty-tip">暂无课程，可直接输入课程 ID</p></div>
        <div class="create-actions"><button class="btn-ghost" @click="closeCourseModal">取消</button></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-backdrop {
  position: fixed;
  z-index: 40;
  inset: 0 0 0 var(--modal-left, 0);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  overflow-y: auto;
  background: oklch(0.2 0.01 150 / 0.3);
}
.modal-backdrop.create-backdrop {
  align-items: center;
  justify-content: center;
}
.create-panel {
  width: min(560px, 100%);
  max-height: calc(100vh - 48px);
  overflow: auto;
  padding: 24px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-xl);
}
.create-form input:not([type='checkbox']):not([type='radio']),
.create-form textarea {
  width: 100%;
  min-width: 0;
}
.create-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.create-heading strong { font-size: 19px; }
.create-close {
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  background: transparent;
}
.course-picker {
  width: 100%;
  justify-content: space-between;
  min-height: 42px;
  text-align: left;
}
.course-picker > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.placeholder { color: var(--faint); }
.create-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.manual-row { display: flex; gap: 10px; }
.manual-confirm { flex: none; }
.course-list { display: grid; gap: 7px; max-height: 250px; margin-top: 14px; overflow: auto; }
.course-item { justify-content: flex-start; text-align: left; }
.course-item.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.empty-tip { text-align: center; color: var(--muted); }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.grid-2 > .form-group { min-width: 0; }
.form-hint { margin: 6px 0 0; font-size: var(--text-sm); color: var(--muted); }
@media (max-width: 720px) { .grid-2 { grid-template-columns: 1fr; } }
</style>
