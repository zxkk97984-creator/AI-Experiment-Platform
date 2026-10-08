<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import AppIcon from '../ui/AppIcon.vue'
import CourseFormModal from './CourseFormModal.vue'
import CourseWhitelistManager from './CourseWhitelistManager.vue'
import TeachingClassMultiSelect from './TeachingClassMultiSelect.vue'
import { coursesAPI } from '../../api/courses.js'
import { academicsAPI } from '../../api/academics.js'
import { useAppStore } from '../../stores/app.js'
import { useAuthStore } from '../../stores/auth.js'

const props = defineProps({
  terms: { type: Array, default: () => [] },
})

const emit = defineEmits(['close', 'created'])
const app = useAppStore()
const auth = useAuthStore()

const titleInput = ref(null)
const form = reactive({
  title: '',
  code: '',
  description: '',
  academic_term_id: null,
  teaching_class_ids: [],
  start_time: '',
  visibility: 'class',
  default_score: 100,
  whitelist_students: [],
})
const availableClasses = ref([])
const classesLoading = ref(false)
const classesError = ref('')
const titleError = ref('')
const createError = ref('')
const coverError = ref('')
const selectedCoverFile = ref(null)
const coverPreviewUrl = ref('')
const coverUploadProgress = ref(null)
const phase = ref('idle')
const createdCourseId = ref(null)
const createdCourse = ref(null)
const coverUploadController = ref(null)
const syncedWhitelistStudentIds = new Set()
let classesRequestId = 0
const classPageSize = 50

const MAX_COVER_BYTES = 5 * 1024 * 1024
const ALLOWED_COVER_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp', '.gif']
const ALLOWED_COVER_MIMES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif']

const isBusy = computed(() => ['creating', 'syncing-whitelist', 'uploading'].includes(phase.value))
const canSubmit = computed(() => form.title.trim().length > 0 && !isBusy.value && phase.value === 'idle')
const hasPostCreateFailure = computed(() => phase.value === 'created' && Boolean(createError.value || coverError.value))
const submitLabel = computed(() => {
  if (phase.value === 'creating') return '创建中…'
  if (phase.value === 'syncing-whitelist') return '保存白名单中…'
  if (phase.value === 'uploading') return '封面上传中…'
  return '创建课程'
})

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function revokeCoverPreview() {
  if (coverPreviewUrl.value) URL.revokeObjectURL(coverPreviewUrl.value)
  coverPreviewUrl.value = ''
}

function resetForm() {
  revokeCoverPreview()
  form.title = ''
  form.code = ''
  form.description = ''
  form.academic_term_id = null
  form.teaching_class_ids = []
  form.start_time = ''
  form.visibility = 'class'
  form.default_score = 100
  form.whitelist_students = []
  syncedWhitelistStudentIds.clear()
  availableClasses.value = []
  classesLoading.value = false
  classesError.value = ''
  titleError.value = ''
  createError.value = ''
  coverError.value = ''
  selectedCoverFile.value = null
  coverUploadProgress.value = null
  createdCourseId.value = null
  createdCourse.value = null
  phase.value = 'idle'
}

function closeModal() {
  if (isBusy.value) return
  resetForm()
  emit('close')
}

function handleEscape(event) {
  if (event.key === 'Escape') closeModal()
}

function validateCoverFile(file) {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!ALLOWED_COVER_EXTENSIONS.includes(extension)) return '仅支持 JPG、PNG、WebP、GIF 图片'
  if (!ALLOWED_COVER_MIMES.includes(file.type)) return '该文件类型不受支持'
  if (file.size > MAX_COVER_BYTES) return '封面图片超过 5 MB 大小限制'
  return ''
}

function selectCoverFile(file) {
  if (!file) return
  const validationError = validateCoverFile(file)
  if (validationError) {
    coverError.value = validationError
    selectedCoverFile.value = null
    revokeCoverPreview()
    return
  }
  coverError.value = ''
  selectedCoverFile.value = file
  revokeCoverPreview()
  coverPreviewUrl.value = URL.createObjectURL(file)
}

function handleCoverChange(event) {
  selectCoverFile(event.target.files?.[0])
  event.target.value = ''
}

function handleCoverDrop(event) {
  selectCoverFile(event.dataTransfer?.files?.[0])
}

function courseErrorMessage(error, fallback) {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  return detail?.message || fallback
}

async function loadClasses(termId) {
  const requestId = ++classesRequestId
  availableClasses.value = []
  form.teaching_class_ids = []
  classesError.value = ''
  if (!termId) return

  classesLoading.value = true
  try {
    const rows = []
    let page = 1
    while (true) {
      if (requestId !== classesRequestId) return
      const res = await academicsAPI.listClasses({
        academic_term_id: Number(termId),
        page,
        page_size: classPageSize,
      })
      const items = res.data.items || []
      rows.push(...items)
      const total = res.data.total
      if (total == null || page >= Math.max(1, Math.ceil(Number(total) / classPageSize))) break
      page += 1
    }
    if (requestId === classesRequestId) availableClasses.value = rows
  } catch {
    if (requestId === classesRequestId) classesError.value = '教学班加载失败，可稍后到课程设置中选择'
  } finally {
    if (requestId === classesRequestId) classesLoading.value = false
  }
}

watch(() => form.academic_term_id, loadClasses)

function buildPayload() {
  return {
    title: form.title.trim(),
    code: form.code.trim() || null,
    description: form.description.trim() || null,
    academic_term_id: form.academic_term_id || null,
    teaching_class_ids: auth.isAdmin ? form.teaching_class_ids.map((id) => Number(id)) : [],
    start_time: form.start_time ? form.start_time.slice(0, 16) : null,
    visibility: form.visibility,
    default_score: Number(form.default_score || 100),
  }
}

async function uploadCover(courseId) {
  if (!selectedCoverFile.value) return true

  phase.value = 'uploading'
  coverError.value = ''
  coverUploadProgress.value = null
  coverUploadController.value = new AbortController()
  try {
    const res = await coursesAPI.uploadCourseCover(courseId, selectedCoverFile.value, {
      onUploadProgress: (event) => {
        coverUploadProgress.value = event.total
          ? Math.round((event.loaded / event.total) * 100)
          : null
      },
      signal: coverUploadController.value.signal,
    })
    createdCourse.value = res.data
    coverUploadProgress.value = 100
    return true
  } catch (error) {
    coverError.value = error.code === 'ERR_CANCELED'
      ? '封面上传已取消，可点击重试'
      : courseErrorMessage(error, '封面上传失败，请重试')
    phase.value = 'created'
    return false
  } finally {
    coverUploadController.value = null
  }
}

async function syncWhitelist(courseId) {
  if (form.visibility !== 'whitelist' || !form.whitelist_students.length) return true

  phase.value = 'syncing-whitelist'
  createError.value = ''
  try {
    for (const student of form.whitelist_students) {
      const studentId = Number(student.id)
      if (syncedWhitelistStudentIds.has(studentId)) continue
      await coursesAPI.addWhitelistStudent(courseId, studentId)
      syncedWhitelistStudentIds.add(studentId)
    }
    return true
  } catch (error) {
    createError.value = courseErrorMessage(error, '学生白名单保存失败，请重试')
    phase.value = 'created'
    return false
  }
}

async function completePostCreate() {
  const whitelistSaved = await syncWhitelist(createdCourseId.value)
  if (!whitelistSaved) return
  const uploaded = await uploadCover(createdCourseId.value)
  if (!uploaded) return
  completeCreation()
}

function completeCreation() {
  app.showToast('课程创建成功', 'success')
  emit('created', createdCourse.value)
}

async function handleSubmit() {
  titleError.value = ''
  createError.value = ''
  if (!form.title.trim()) {
    titleError.value = '请输入课程名称'
    await nextTick()
    titleInput.value?.focus()
    return
  }

  phase.value = 'creating'
  try {
    const res = await coursesAPI.create(buildPayload())
    createdCourseId.value = res.data.id
    createdCourse.value = res.data
    await completePostCreate()
  } catch (error) {
    phase.value = 'idle'
    createError.value = courseErrorMessage(error, '创建课程失败，请重试')
  }
}

async function retryPostCreate() {
  if (!createdCourseId.value || isBusy.value) return
  await completePostCreate()
}

function continueWithoutPostCreate() {
  if (!createdCourse.value || isBusy.value) return
  completeCreation()
}

onMounted(() => {
  document.addEventListener('keydown', handleEscape)
  nextTick(() => titleInput.value?.focus())
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', handleEscape)
  if (coverUploadController.value) coverUploadController.value.abort()
  revokeCoverPreview()
})
</script>

<template>
  <CourseFormModal
    title="创建课程"
    description="课程创建后默认为草稿，其他信息也可以稍后完善。"
    title-id="course-create-title"
    :busy="isBusy"
    panel-class="course-create-panel"
    body-class="course-create-body"
    actions-class="course-create-actions"
    @close="closeModal"
    @submit="handleSubmit"
  >
          <div class="course-create-grid">
            <label class="course-create-field course-create-field-full">
              <span>课程名称 <em>*</em></span>
              <input ref="titleInput" v-model="form.title" placeholder="输入课程名称" :disabled="isBusy" />
              <small v-if="titleError" class="course-create-error">{{ titleError }}</small>
            </label>

            <label class="course-create-field course-create-field-full">
              <span>课程编号 <small>可选</small></span>
              <input v-model="form.code" placeholder="例如：CS101" :disabled="isBusy" />
            </label>

            <label class="course-create-field course-create-field-full">
              <span>课程简介 <small>可选</small></span>
              <textarea v-model="form.description" rows="3" placeholder="输入课程简介" :disabled="isBusy"></textarea>
            </label>

            <label class="course-create-field">
              <span>所属学期 <small>可选</small></span>
              <DsSelect v-model="form.academic_term_id" :disabled="isBusy">
                <option :value="null">暂不设置</option>
                <option v-for="term in props.terms" :key="term.id" :value="term.id" :disabled="term.status === 'closed'">
                  {{ term.name }}{{ term.status === 'closed' ? '（已关闭）' : '' }}
                </option>
              </DsSelect>
            </label>

            <label class="course-create-field">
              <span>开课时间 <small>可选</small></span>
              <input v-model="form.start_time" type="datetime-local" :disabled="isBusy" />
            </label>

            <label class="course-create-field course-create-field-full">
              <span>教学班 <small>{{ auth.isAdmin ? "可多选，可稍后安排授课" : "由管理员在授课安排中分配" }}</small></span>
              <TeachingClassMultiSelect
                v-model="form.teaching_class_ids"
                :options="availableClasses"
                :disabled="!auth.isAdmin || isBusy || !form.academic_term_id || classesLoading"
                :loading="classesLoading"
                placeholder="请先选择所属学期"
                empty-text="该学期暂无教学班"
                loading-text="正在加载教学班…"
                test-id="course-create-teaching-classes"
              />
              <small v-if="classesLoading" class="course-create-hint">正在加载教学班…</small>
              <small v-else-if="classesError" class="course-create-error">{{ classesError }}</small>
              <small v-else class="course-create-hint">点击下拉栏后可搜索并勾选多个教学班</small>
            </label>

            <label class="course-create-field">
              <span>课程可见范围</span>
              <DsSelect v-model="form.visibility" data-testid="course-create-visibility" :disabled="isBusy">
                <option value="private">仅自己可见</option>
                <option value="class">教学班可见</option>
                <option value="whitelist">指定学生可见</option>
              </DsSelect>
            </label>

            <label class="course-create-field">
              <span>默认评分</span>
              <input v-model.number="form.default_score" type="number" min="0" step="1" :disabled="isBusy" />
            </label>
          </div>

          <CourseWhitelistManager
            v-if="form.visibility === 'whitelist'"
            v-model="form.whitelist_students"
            class="course-create-whitelist"
          />

          <section class="course-cover-create-field" aria-label="课程图片上传">
            <div class="course-create-field-label">
              <span>课程图片 <small>可选</small></span>
              <small>支持 JPG、PNG、WebP、GIF，最大 5 MB</small>
            </div>
            <div
              class="course-cover-dropzone"
              :class="{ 'has-preview': coverPreviewUrl, disabled: isBusy }"
              @dragover.prevent
              @drop.prevent="handleCoverDrop"
            >
              <img v-if="coverPreviewUrl" :src="coverPreviewUrl" alt="课程图片预览" class="course-cover-preview" />
              <div v-else class="course-cover-placeholder">
                <AppIcon name="course" :size="24" />
                <strong>点击选择或拖拽图片到此处</strong>
                <span>创建课程后会自动上传</span>
              </div>
              <label class="course-cover-select-label" :class="{ disabled: isBusy }">
                <input
                  type="file"
                  accept=".jpg,.jpeg,.png,.webp,.gif,image/jpeg,image/png,image/webp,image/gif"
                  :disabled="isBusy"
                  @change="handleCoverChange"
                />
                {{ coverPreviewUrl ? '更换图片' : '选择图片' }}
              </label>
            </div>
            <p v-if="selectedCoverFile" class="course-cover-file">
              {{ selectedCoverFile.name }}（{{ formatBytes(selectedCoverFile.size) }}）
            </p>
            <p v-if="coverError" class="course-create-error" role="alert">{{ coverError }}</p>
            <div v-if="phase === 'uploading'" class="course-cover-progress" aria-live="polite">
              <div class="course-cover-progress-track">
                <span :style="{ width: coverUploadProgress !== null ? `${coverUploadProgress}%` : '35%' }"></span>
              </div>
              <span>{{ coverUploadProgress !== null ? `${coverUploadProgress}%` : '上传中…' }}</span>
            </div>
          </section>

          <p v-if="createError" class="course-create-error course-create-form-error" role="alert">{{ createError }}</p>
          <p v-if="hasPostCreateFailure" class="course-create-upload-warning" role="status">
            课程已创建，但{{ createError || `封面${coverError}` }}。可以重试，也可以稍后到课程设置中处理。
          </p>

        <template #actions>
          <button class="btn-ghost" type="button" :disabled="isBusy" @click="closeModal">取消</button>
          <template v-if="hasPostCreateFailure">
            <button class="btn-ghost" type="button" @click="continueWithoutPostCreate">稍后处理</button>
            <button class="btn-primary" type="button" :disabled="isBusy" @click="retryPostCreate">
              {{ coverError && !createError ? '重试上传' : '重试保存' }}
            </button>
          </template>
          <button v-else class="btn-primary" type="submit" :disabled="!canSubmit">{{ submitLabel }}</button>
        </template>
  </CourseFormModal>
</template>

<style scoped>
/* V2 创建课程表单：字段、网格与上传区均映射全局 token，不新增颜色。 */
.course-create-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.course-create-field { display: flex; min-width: 0; flex-direction: column; gap: 7px; color: var(--fg); font-size: var(--text-base); font-weight: 600; }
.course-create-field-full { grid-column: 1 / -1; }
.course-create-field > span, .course-create-field-label > span { display: flex; align-items: baseline; gap: 5px; }
.course-create-field > span small, .course-create-field-label > small, .course-create-field-label > span small { color: var(--faint); font-size: var(--text-sm); font-weight: 400; }
.course-create-field em { color: var(--danger); font-style: normal; }
.course-create-field input, .course-create-field textarea, .course-create-field .ds-select { width: 100%; min-width: 0; border-color: var(--border-strong); border-radius: var(--radius-md); font-size: var(--text-md); }
.course-create-hint, .course-cover-file { margin: 0; color: var(--faint); font-size: var(--text-sm); font-weight: 400; }
.course-create-error { margin: 0; color: var(--danger); font-size: var(--text-sm); font-weight: 400; }
.course-create-form-error { margin-top: 16px; }
.course-create-whitelist { margin-top: 20px; }
.course-cover-create-field { display: flex; flex-direction: column; gap: 8px; margin-top: 20px; }
.course-create-field-label { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; color: var(--fg); font-size: var(--text-base); font-weight: 600; }
.course-cover-dropzone {
  position: relative; min-height: 148px; overflow: hidden;
  border: 1px dashed var(--border-strong); border-radius: var(--radius-lg);
  background: var(--surface-subtle);
}
.course-cover-dropzone:hover:not(.disabled) { border-color: var(--accent); background: var(--accent-faint); }
.course-cover-dropzone.disabled { opacity: 0.65; }
.course-cover-dropzone.has-preview { min-height: 180px; }
.course-cover-placeholder { display: flex; align-items: center; justify-content: center; min-height: 148px; flex-direction: column; gap: 5px; color: var(--faint); }
.course-cover-placeholder strong { color: var(--muted); font-size: var(--text-base); }
.course-cover-placeholder span { font-size: var(--text-sm); }
.course-cover-preview { display: block; width: 100%; height: 180px; object-fit: cover; }
.course-cover-select-label {
  position: absolute; right: 12px; bottom: 12px;
  display: inline-flex; align-items: center; min-height: 32px; padding: 0 12px;
  border-radius: var(--radius-md); background: oklch(0.225 0.018 155); color: oklch(0.84 0.01 155);
  cursor: pointer; font-size: var(--text-sm); font-weight: 500;
}
.course-cover-select-label.disabled { cursor: not-allowed; }
.course-cover-select-label input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.course-cover-file { overflow-wrap: anywhere; }
.course-cover-progress { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: var(--text-sm); }
.course-cover-progress-track { height: 6px; flex: 1; overflow: hidden; border-radius: var(--radius-sm); background: var(--surface-sunken); }
.course-cover-progress-track span { display: block; height: 100%; border-radius: inherit; background: var(--accent); }
.course-cover-progress > span { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.course-create-upload-warning { margin: 12px 0 0; color: var(--warning); font-size: var(--text-sm); }
</style>
