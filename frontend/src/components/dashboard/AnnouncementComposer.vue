<script setup>
// 教师课程公告发布模态：本地校验、提交中禁用、失败保持打开、成功重置并关闭

import { computed, onMounted, reactive, ref, watch } from 'vue'

import { announcementsAPI } from '../../api/announcements.js'

const props = defineProps({
  courses: { type: Array, default: () => [] },
})

const emit = defineEmits(['close', 'published'])

const form = reactive({
  course_id: null,
  title: '',
  content: '',
  priority: 'normal',
  expires_at: '',
})
const submitting = ref(false)
const error = ref('')
const titleInput = ref(null)
const modalEl = ref(null)

function onTabKeydown(e) {
  // Tab 焦点循环：焦点不逃出模态
  if (e.key !== 'Tab' || !modalEl.value) return
  const focusables = modalEl.value.querySelectorAll(
    'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
  )
  const list = Array.from(focusables).filter((el) => !el.disabled)
  if (!list.length) return
  const first = list[0]
  const last = list[list.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

const canSubmit = computed(() => {
  return (
    form.course_id != null &&
    form.title.trim().length > 0 &&
    form.content.trim().length > 0 &&
    !submitting.value
  )
})

watch(
  () => props.courses,
  (courses) => {
    if (!form.course_id && courses.length) form.course_id = courses[0].id
  },
  { immediate: true },
)

onMounted(() => {
  titleInput.value?.focus()
})

function close() {
  if (submitting.value) return
  emit('close')
}

function resetForm() {
  Object.assign(form, {
    course_id: props.courses[0]?.id ?? null,
    title: '',
    content: '',
    priority: 'normal',
    expires_at: '',
  })
  error.value = ''
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  try {
    const payload = {
      title: form.title.trim(),
      content: form.content.trim(),
      priority: form.priority,
      scope: 'course',
      course_id: form.course_id,
    }
    if (form.expires_at) {
      payload.expires_at = new Date(form.expires_at).toISOString()
    }
    const { data } = await announcementsAPI.create(payload)
    resetForm()
    emit('published', data)
  } catch (e) {
    error.value = e.response?.data?.detail?.message || '发布失败，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="modal-backdrop composer-backdrop" @click.self="close">
    <div
      ref="modalEl"
      class="modal composer-modal"
      role="dialog"
      aria-modal="true"
      aria-label="发布课程公告"
      @keydown.esc="close"
      @keydown="onTabKeydown"
    >
      <div class="modal-head composer-head">
        <h2 class="composer-title">发布公告</h2>
        <button type="button" class="btn btn-ghost btn-sm btn-icon" aria-label="关闭" @click="close">×</button>
      </div>
      <form class="composer-form" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">课程</span>
          <DsSelect v-model="form.course_id" required>
            <option v-for="course in courses" :key="course.id" :value="course.id">
              {{ course.title }}
            </option>
          </DsSelect>
        </label>
        <label class="field">
          <span class="field-label">标题</span>
          <input
            ref="titleInput"
            v-model="form.title"
            type="text"
            maxlength="120"
            required
            placeholder="请输入公告标题"
          />
        </label>
        <label class="field">
          <span class="field-label">内容</span>
          <textarea
            v-model="form.content"
            rows="4"
            maxlength="2000"
            required
            placeholder="请输入公告内容"
          ></textarea>
        </label>
        <div class="field-row">
          <label class="field">
            <span class="field-label">优先级</span>
            <DsSelect v-model="form.priority">
              <option value="normal">普通</option>
              <option value="important">重要</option>
              <option value="urgent">紧急</option>
            </DsSelect>
          </label>
          <label class="field">
            <span class="field-label">过期时间（可选）</span>
            <input v-model="form.expires_at" type="datetime-local" />
          </label>
        </div>
        <p v-if="error" class="composer-error" role="alert">{{ error }}</p>
        <div class="modal-foot composer-actions">
          <button type="button" class="btn btn-ghost" @click="close">取消</button>
          <button type="submit" class="btn btn-primary" :disabled="!canSubmit">发布</button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
/* V2 Modal：全局 .modal-* 提供面板视觉；此处只保留公告表单组合。 */
.composer-backdrop { z-index: 200; }
.composer-modal { max-width: 520px; padding: 0; overflow-y: auto; }
.composer-form { display: flex; flex-direction: column; gap: 14px; padding: 20px; }
.field { display: flex; flex-direction: column; gap: 6px; flex: 1; min-width: 0; }
.field-row { display: flex; gap: 12px; }
.field-label { font-size: var(--text-sm); font-weight: 500; color: var(--muted); }
.field input, .field .ds-select, .field textarea {
  width: 100%;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--fg);
  font-family: var(--font-body);
  font-size: var(--text-md);
}
.field input, .field .ds-select { height: var(--h-input); padding: 0 11px; }
.field textarea { min-height: 88px; padding: 9px 11px; resize: vertical; }
.composer-error { margin: 0; font-size: var(--text-sm); color: var(--danger); }
</style>
