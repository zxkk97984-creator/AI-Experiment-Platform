<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { marked } from 'marked'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import CourseCoverUploader from '../../components/teacher/CourseCoverUploader.vue'
import CourseFormModal from '../../components/teacher/CourseFormModal.vue'
import CourseWhitelistManager from '../../components/teacher/CourseWhitelistManager.vue'
import CourseRosterManager from '../../components/teacher/CourseRosterManager.vue'
import TeachingClassMultiSelect from '../../components/teacher/TeachingClassMultiSelect.vue'
import { coursesAPI } from '../../api/courses.js'
import { academicsAPI } from '../../api/academics.js'
import { studioAPI } from '../../api/studio.js'
import EnvironmentProfilePicker from '../../components/common/EnvironmentProfilePicker.vue'
import { useAppStore } from '../../stores/app.js'
import { getCoursePublishMissingFields } from '../../utils/coursePublish.js'
import { sanitizeHtml } from '../../utils/sanitize.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

// ── 后端 API 能力边界 ─────────────────────────────────────────────────
// 只为已接通的 API 渲染真实操作。排序接口尚未实现 → 不显示拖拽手柄。
const API_CAPS = {
  chapterCopy: true, // 复制章节 = 新建章节 + 逐课时复制（POST /chapters + /chapters/:id/lessons）
  chapterEdit: true, // PATCH /chapters/:id
  chapterDelete: true, // DELETE /chapters/:id
  chapterMove: true, // 上移 / 下移 = 批量重排 order_index（PATCH /chapters/:id）
  lessonCopy: true, // 复制课时（POST /chapters/:id/lessons）
  lessonMove: true, // 移动到其他章节（PATCH /lessons/:id 传 chapter_id）
  lessonPublish: true, // 发布 / 设为草稿（PATCH /lessons/:id 传 status）
  lessonDelete: true, // DELETE /lessons/:id
  dragSort: false, // 无排序接口 → 不显示拖拽手柄
}

const CONTENT_TYPES = [
  { type: 'markdown', label: '讲义', desc: '创建文字与代码讲解内容' },
  { type: 'notebook', label: 'Notebook 实验', desc: '创建可运行的交互式实验' },
  { type: 'experiment', label: '普通实验', desc: '创建实验任务和提交要求' },
  { type: 'video', label: '视频', desc: '添加视频课程' },
]

const course = ref(null)
const chapters = ref([])
const loading = ref(true)

const expanded = ref({})
const openMenu = ref(null) // 'chapter:{id}' | 'lesson:{id}'
const chapterDialog = ref(false)
const chapterTitle = ref('')

// 添加课时两步弹窗：{ chapterId, step, type, title, description }
const createWizard = ref(null)
const creatingLesson = ref(false)
const titleInput = ref(null)

// 编辑章节标题（抽屉）
const editingChapter = ref(null)
const chapterEditTitle = ref('')
const savingChapterEdit = ref(false)

// 课时移动到其他章节（弹窗）
const movingLesson = ref(null) // { id, title, targetId, targetTitle }
const moveTargetChapterId = ref(null)
const savingMove = ref(false)

// 删除确认：{ kind: 'chapter' | 'lesson', title, lessonCount? }
const deleteTarget = ref(null)
const deleting = ref(false)

const previewLesson = ref(null)
const previewHtml = ref('')
// 学生视角预览：本地视频的签名播放地址（外链直接使用 video_url）
const previewVideoUrl = ref('')
const previewVideoLoading = ref(false)
const previewVideoError = ref('')

const settingsOpen = ref(false)
const savingSettings = ref(false)
const settings = ref({})
const academicTerms = ref([])
const teachingClasses = ref([])
const publishMissingFields = ref([])
// 封面上传/移除期间禁用保存按钮，避免与普通设置提交互相覆盖
const coverBusy = ref(false)
const publishingCourse = ref(false)

const courseId = computed(() => route.params.courseId || route.params.id)
const rolePrefix = computed(() => route.path?.startsWith('/admin') ? '/admin' : '/teacher')
const lessons = computed(() => chapters.value.flatMap((chapter) => chapter.lessons || []))
const availableClasses = computed(() => teachingClasses.value.filter((item) => Number(item.academic_term_id) === Number(settings.value.academic_term_id)))

const courseStatusLabel = computed(
  () => ({ published: '已发布', draft: '草稿', archived: '已归档' })[course.value?.status] || '待发布',
)
const canPublishCourse = computed(() => course.value?.status === 'draft')
const stats = computed(() => [
  { value: chapters.value.length, label: '章节' },
  { value: lessons.value.length, label: '课时' },
  { value: lessons.value.filter((lesson) => lesson.content_type === 'notebook').length, label: '实验 Notebook' },
  { value: courseStatusLabel.value, label: '课程状态' },
])

function settingsFromCourse(value) {
  return {
    title: value?.title || '',
    description: value?.description || '',
    status: value?.status || 'draft',
    // datetime-local 只需 YYYY-MM-DDTHH:mm，截掉秒与时区
    start_time: (value?.start_time || '').slice(0, 16),
    visibility: value?.visibility || 'class',
    default_score: value?.default_score ?? 100,
    academic_term_id: value?.academic_term_id ?? null,
    teaching_class_ids: (value?.teaching_classes || []).map((item) => item.id),
  }
}

async function loadPage() {
  loading.value = true
  try {
    const [courseResponse, chapterResponse] = await Promise.all([
      coursesAPI.get(courseId.value),
      coursesAPI.getChapters(courseId.value),
    ])
    course.value = courseResponse.data
    const raw = chapterResponse.data
    chapters.value = Array.isArray(raw) ? raw : (raw.items || [])
    settings.value = settingsFromCourse(course.value)
    restoreExpandedState()
  } catch {
    app.showToast('课程内容加载失败', 'error')
  } finally {
    loading.value = false
  }
}

function restoreExpandedState() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(`teacher-course-${courseId.value}-chapters`) || '{}')
    expanded.value = Object.fromEntries(chapters.value.map((chapter) => [chapter.id, saved[chapter.id] !== false]))
  } catch {
    expanded.value = Object.fromEntries(chapters.value.map((chapter) => [chapter.id, true]))
  }
}

function toggleChapter(chapterId) {
  expanded.value[chapterId] = !expanded.value[chapterId]
  sessionStorage.setItem(`teacher-course-${courseId.value}-chapters`, JSON.stringify(expanded.value))
}

function toggleMenu(key) {
  openMenu.value = openMenu.value === key ? null : key
}

function closeMenus() {
  openMenu.value = null
}

function onDocumentClick(event) {
  if (openMenu.value && !event.target.closest('[data-menu]')) openMenu.value = null
}

function onKeydown(event) {
  if (event.key !== 'Escape') return
  openMenu.value = null
  createWizard.value = null
  chapterDialog.value = false
  previewLesson.value = null
  editingChapter.value = null
  movingLesson.value = null
  deleteTarget.value = null
  closeCourseSettings()
}

// ── 展示辅助 ──────────────────────────────────────────────────────────
function isPublished(lesson) {
  return lesson.status === 'published' || lesson.published === true
}

function chapterLessons(chapter) {
  return chapter.lessons || []
}

function statusLabel(lesson) {
  if (isPublished(lesson)) return '已发布'
  if (lesson.status === 'pending') return '待发布'
  return '草稿'
}

function statusClass(lesson) {
  if (isPublished(lesson)) return 'published'
  if (lesson.status === 'pending') return 'pending'
  return 'draft'
}

function typeLabel(type) {
  return { markdown: '讲义', notebook: 'Notebook', experiment: '实验', video: '视频' }[type] || '讲义'
}

function durationLabel(lesson) {
  if (!lesson.duration && !lesson.duration_minutes) return '未设置'
  return `约 ${lesson.duration_minutes || lesson.duration} 分钟`
}

function chapterSummary(chapter) {
  const list = chapter.lessons || []
  const parts = [`${list.length} 个课时`]
  const lectures = list.filter((l) => l.content_type === 'markdown').length
  const labs = list.filter((l) => l.content_type === 'notebook' || l.content_type === 'experiment').length
  const videos = list.filter((l) => l.content_type === 'video').length
  if (lectures) parts.push(`${lectures} 个讲义`)
  if (labs) parts.push(`${labs} 个实验`)
  if (videos) parts.push(`${videos} 个视频`)
  return parts.join(' · ')
}

function renderMarkdown(src) {
  const raw = marked.parse(src || '', { async: false })
  return sanitizeHtml(typeof raw === 'string' ? raw : '')
}

// ── 章节 ──────────────────────────────────────────────────────────────
const chapterTitleInput = ref(null)
watch(chapterDialog, (open) => {
  if (open) nextTick(() => chapterTitleInput.value?.focus())
})

async function createChapter() {
  if (!chapterTitle.value.trim()) return
  try {
    await coursesAPI.createChapter(courseId.value, { title: chapterTitle.value.trim(), order_index: chapters.value.length })
    chapterTitle.value = ''
    chapterDialog.value = false
    await loadPage()
    app.showToast('章节已创建', 'success')
  } catch {
    app.showToast('创建章节失败', 'error')
  }
}

async function copyChapter(chapter) {
  closeMenus()
  try {
    const response = await coursesAPI.createChapter(courseId.value, {
      title: `${chapter.title}（副本）`,
      order_index: chapters.value.length,
    })
    for (const lesson of chapter.lessons || []) {
      await coursesAPI.createLesson(response.data.id, {
        title: lesson.title,
        content_type: lesson.content_type,
        content: lesson.content || undefined,
        notebook_path: lesson.notebook_path || undefined,
        video_url: lesson.video_url || undefined,
        order_index: lesson.order_index || 0,
      })
    }
    await loadPage()
    app.showToast('章节已复制', 'success')
  } catch {
    app.showToast('复制章节失败', 'error')
  }
}

// ── 添加课时（居中弹窗两步创建 → 跳转专属编辑页） ──────────────────
const TYPE_ICONS = { markdown: 'book', notebook: 'cube', experiment: 'experiment', video: 'video' }

// ── Notebook 创建的环境选择（Phase 4） ─────────────────────────────
const notebookEnvOptions = ref([])
const notebookEnvId = ref(null)
const notebookPolicy = ref('unrestricted')
const notebookAllowedImports = ref([])

function onNotebookEnvLoaded(options) {
  notebookEnvOptions.value = options
  if (options.length && !notebookEnvId.value) {
    notebookEnvId.value = options[0].environment_version_id
  }
}

const selectedNotebookEnv = computed(
  () => notebookEnvOptions.value.find((o) => o.environment_version_id === notebookEnvId.value) || null,
)
const notebookImportCandidates = computed(() => {
  if (!selectedNotebookEnv.value) return []
  const seen = new Set()
  const names = []
  for (const p of selectedNotebookEnv.value.packages || []) {
    for (const name of p.import_names || []) {
      if (!seen.has(name)) { seen.add(name); names.push(name) }
    }
  }
  return names
})
const notebookMismatch = computed(() => {
  if (notebookPolicy.value !== 'restricted' || notebookAllowedImports.value.length === 0) return ''
  const installed = new Set(notebookImportCandidates.value)
  const missing = notebookAllowedImports.value.filter((name) => !installed.has(name))
  return missing.length ? `注意：${missing.join('、')} 未在当前环境安装` : ''
})

function toggleNotebookImport(name) {
  const idx = notebookAllowedImports.value.indexOf(name)
  if (idx >= 0) notebookAllowedImports.value.splice(idx, 1)
  else notebookAllowedImports.value.push(name)
}

function openAddLesson(chapterId) {
  closeMenus()
  createWizard.value = { chapterId, step: 1, type: null, title: '', description: '' }
  notebookEnvId.value = null
  notebookPolicy.value = 'unrestricted'
  notebookAllowedImports.value = []
}

function chooseType(type) {
  if (!createWizard.value) return
  createWizard.value.type = type
  createWizard.value.step = 2
}

function backToTypePicker() {
  if (createWizard.value) createWizard.value.step = 1
}

// 进入第二步时聚焦课时名称输入框（v-if 切换后 autofocus 属性不生效）
watch(
  () => createWizard.value?.step,
  async (step) => {
    if (step === 2) {
      await nextTick()
      titleInput.value?.focus()
    }
  },
)

async function createLesson() {
  const wizard = createWizard.value
  if (!wizard?.title.trim() || creatingLesson.value) return
  creatingLesson.value = true
  try {
    let payload
    switch (wizard.type) {
      case 'experiment':
        payload = {
          title: wizard.title.trim(),
          content_type: 'experiment',
          content: `# 实验任务\n\n${wizard.description.trim()}\n\n# 提交要求\n\n`,
          order_index: 0,
        }
        break
      case 'video':
        payload = {
          title: wizard.title.trim(),
          content_type: 'video',
          content: wizard.description.trim() || undefined,
          order_index: 0,
        }
        break
      case 'notebook':
        // 简介不写入 content，随模板创建传入 description
        payload = {
          title: wizard.title.trim(),
          content_type: 'notebook',
          order_index: 0,
        }
        break
      default: // markdown 讲义
        payload = {
          title: wizard.title.trim(),
          content_type: 'markdown',
          content: wizard.description.trim() || undefined,
          order_index: 0,
        }
        break
    }
    const response = await coursesAPI.createLesson(wizard.chapterId, payload)
    const lessonId = response.data?.id ?? response.id
    let target = `${rolePrefix.value}/courses/${courseId.value}/lessons/${lessonId}/edit`
    if (wizard.type === 'notebook') {
      // 创建模板并绑定课时（Phase 4：携带教师选择的环境与白名单）；编辑页凭 ?template 进入 Studio
      const template = await studioAPI.createTemplate({
        name: wizard.title.trim(),
        description: wizard.description.trim() || undefined,
        lesson_id: lessonId,
        environment_version_id: notebookEnvId.value,
        import_policy_mode: notebookPolicy.value,
        allowed_imports: notebookPolicy.value === 'restricted' ? [...notebookAllowedImports.value] : [],
      })
      const templateId = template.data?.id ?? template.id
      target += `?template=${templateId}`
    }
    createWizard.value = null
    router.push(target)
  } catch (err) {
    app.showToast('创建课时失败', 'error')
    console.error('[ChapterManageView] 创建课时失败', err)
  } finally {
    creatingLesson.value = false
  }
}

// ── 课时操作 ──────────────────────────────────────────────────────────
function openEditLesson(lesson) {
  closeMenus()
  // 统一跳转专属编辑页（Notebook 模板解析由编辑页负责，不再依赖 template_id）
  router.push(`${rolePrefix.value}/courses/${courseId.value}/lessons/${lesson.id}/edit`)
}

function openPreview(lesson) {
  closeMenus()
  previewLesson.value = lesson
  previewHtml.value = lesson.content_type === 'video' ? '' : renderMarkdown(lesson.content)
  // 本地视频来源：请求签名播放地址（外链来源不需要）
  previewVideoUrl.value = ''
  previewVideoError.value = ''
  if (lesson.content_type === 'video' && (lesson.video_source === 'upload' || lesson.video_filename)) {
    fetchPreviewVideoUrl(lesson.id)
  }
}

async function fetchPreviewVideoUrl(lessonId) {
  previewVideoLoading.value = true
  previewVideoError.value = ''
  try {
    const res = await coursesAPI.getLessonVideoPlaybackUrl(lessonId)
    previewVideoUrl.value = res.data.url
  } catch (err) {
    previewVideoError.value = '视频预览加载失败，请稍后重试'
    console.error('[ChapterManageView] 获取预览播放地址失败', err)
  } finally {
    previewVideoLoading.value = false
  }
}

function openStudioForPreview(lesson) {
  if (lesson.template_id) {
    router.push(`${rolePrefix.value}/courses/${courseId.value}/studio/${lesson.template_id}`)
  }
}

async function copyLesson(lesson) {
  closeMenus()
  try {
    await coursesAPI.createLesson(lesson.chapter_id, {
      title: `${lesson.title}（副本）`,
      content_type: lesson.content_type,
      content: lesson.content || undefined,
      notebook_path: lesson.notebook_path || undefined,
      video_url: lesson.video_url || undefined,
      order_index: (lesson.order_index || 0) + 1,
    })
    await loadPage()
    app.showToast('课时已复制', 'success')
  } catch {
    app.showToast('复制课时失败', 'error')
  }
}

// ── 删除（章节 / 课时） ──────────────────────────────────────────────
function askDeleteChapter(chapter) {
  closeMenus()
  deleteTarget.value = {
    kind: 'chapter',
    id: chapter.id,
    title: chapter.title,
    lessonCount: chapterLessons(chapter).length,
  }
}

function askDeleteLesson(lesson) {
  closeMenus()
  deleteTarget.value = { kind: 'lesson', id: lesson.id, title: lesson.title }
}

async function confirmDelete() {
  if (!deleteTarget.value || deleting.value) return
  deleting.value = true
  const kind = deleteTarget.value.kind
  try {
    if (kind === 'chapter') {
      await coursesAPI.deleteChapter(deleteTarget.value.id)
    } else {
      await coursesAPI.deleteLesson(deleteTarget.value.id)
    }
    deleteTarget.value = null
    await loadPage()
    app.showToast(kind === 'chapter' ? '章节已删除' : '课时已删除', 'success')
  } catch {
    app.showToast('删除失败', 'error')
  } finally {
    deleting.value = false
  }
}

// ── 编辑章节 ─────────────────────────────────────────────────────────
function openEditChapter(chapter) {
  closeMenus()
  editingChapter.value = chapter
  chapterEditTitle.value = chapter.title || ''
}

async function saveEditChapter() {
  if (!editingChapter.value || !chapterEditTitle.value.trim()) return
  savingChapterEdit.value = true
  try {
    await coursesAPI.updateChapter(editingChapter.value.id, { title: chapterEditTitle.value.trim() })
    editingChapter.value = null
    await loadPage()
    app.showToast('章节已保存', 'success')
  } catch {
    app.showToast('保存章节失败', 'error')
  } finally {
    savingChapterEdit.value = false
  }
}

// ── 移动章节（上移 / 下移）：重排全部章节的 order_index ──────────────
async function moveChapter(chapter, direction) {
  closeMenus()
  const list = [...chapters.value]
  const index = list.findIndex((item) => item.id === chapter.id)
  const targetIndex = index + direction
  if (index < 0 || targetIndex < 0 || targetIndex >= list.length) return
  const [moved] = list.splice(index, 1)
  list.splice(targetIndex, 0, moved)
  try {
    await Promise.all(
      list.map((item, position) => coursesAPI.updateChapter(item.id, { order_index: position })),
    )
    await loadPage()
  } catch {
    app.showToast('移动章节失败', 'error')
  }
}

// ── 取消发布章节：章节内全部课时设为草稿 ─────────────────────────────
async function unpublishChapter(chapter) {
  closeMenus()
  const pending = chapterLessons(chapter)
  if (!pending.length) return
  try {
    await Promise.all(pending.map((lesson) => coursesAPI.updateLesson(lesson.id, { status: 'draft' })))
    await loadPage()
    app.showToast('章节已取消发布', 'success')
  } catch {
    app.showToast('取消发布失败', 'error')
  }
}

// ── 课时发布状态切换 ─────────────────────────────────────────────────
async function toggleLessonPublish(lesson) {
  closeMenus()
  const next = isPublished(lesson) ? 'draft' : 'published'
  try {
    await coursesAPI.updateLesson(lesson.id, { status: next })
    await loadPage()
    app.showToast(next === 'published' ? '课时已发布' : '课时已设为草稿', 'success')
  } catch {
    app.showToast('操作失败', 'error')
  }
}

// ── 课时移动到其他章节 ───────────────────────────────────────────────
function openMoveLesson(lesson) {
  closeMenus()
  movingLesson.value = lesson
  moveTargetChapterId.value = lesson.chapter_id
}

async function confirmMoveLesson() {
  if (!movingLesson.value || !moveTargetChapterId.value) return
  savingMove.value = true
  try {
    await coursesAPI.updateLesson(movingLesson.value.id, {
      chapter_id: moveTargetChapterId.value,
      order_index: 0,
    })
    movingLesson.value = null
    await loadPage()
    app.showToast('课时已移动', 'success')
  } catch {
    app.showToast('移动课时失败', 'error')
  } finally {
    savingMove.value = false
  }
}

// ── 课程设置 ──────────────────────────────────────────────────────────
async function saveSettings() {
  savingSettings.value = true
  try {
    // 归一化：空值语义对齐后端（start_time 可空清空；default_score 回退满分）
    // 封面由上传组件独立提交，即使误入 settings 也在此显式排除，
    // 避免保存普通设置时覆盖已上传的封面
    const { cover: _ignoredCover, ...payload } = settings.value
    void _ignoredCover
    payload.start_time = payload.start_time ? payload.start_time.slice(0, 16) : null
    payload.default_score =
      payload.default_score === '' || payload.default_score == null
        ? 100
        : Number(payload.default_score)
    if (payload.status === 'published' && course.value?.status !== 'published') {
      publishMissingFields.value = getCoursePublishMissingFields({
        ...course.value,
        ...payload,
        cover: course.value?.cover,
        teaching_classes: (payload.teaching_class_ids || []).map((id) => ({ id })),
      })
      if (publishMissingFields.value.length) {
        app.showToast(`发布前请完善：${publishMissingFields.value.join('、')}`, 'error')
        savingSettings.value = false
        return
      }
    }
    const res = await coursesAPI.update(courseId.value, payload)
    // 以 API 响应更新课程，接收规范化字段（含 visibility）
    course.value = res.data
    settings.value = {
      ...settings.value,
      ...payload,
      visibility: res.data.visibility ?? payload.visibility,
      default_score: res.data.default_score ?? payload.default_score,
    }
    settingsOpen.value = false
    publishMissingFields.value = []
    app.showToast('课程设置已保存', 'success')
  } catch (err) {
    app.showToast(err.response?.data?.detail?.message || '课程设置保存失败', 'error')
  } finally {
    savingSettings.value = false
  }
}

function openCourseSettings() {
  publishMissingFields.value = []
  settings.value = settingsFromCourse(course.value)
  settingsOpen.value = true
}

function closeCourseSettings() {
  settings.value = settingsFromCourse(course.value)
  publishMissingFields.value = []
  settingsOpen.value = false
}

async function publishCourse() {
  if (!course.value || publishingCourse.value || !canPublishCourse.value) return
  const missing = getCoursePublishMissingFields(course.value)
  if (missing.length) {
    settings.value = settingsFromCourse(course.value)
    publishMissingFields.value = missing
    settingsOpen.value = true
    app.showToast(`发布前请完善：${missing.join('、')}`, 'error')
    return
  }

  publishingCourse.value = true
  try {
    const res = await coursesAPI.update(courseId.value, { status: 'published' })
    course.value = res.data
    settings.value = { ...settings.value, status: 'published' }
    app.showToast('课程已发布', 'success')
  } catch (err) {
    app.showToast(err.response?.data?.detail?.message || '课程发布失败', 'error')
  } finally {
    publishingCourse.value = false
  }
}

// 封面上传/移除完成：用 API 返回的课程更新当前课程，无需重新加载整个章节列表
function handleCoverUpdated(updatedCourse) {
  course.value = updatedCourse
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onKeydown)
  Promise.all([
    academicsAPI.listTerms({ page_size: 100 }),
    academicsAPI.listClasses({ page_size: 100 }),
  ]).then(([termsRes, classesRes]) => {
    academicTerms.value = termsRes.data.items || []
    teachingClasses.value = classesRes.data.items || []
  }).catch(() => {})
  loadPage()
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <AppLayout content-mode="list">
    <div class="catalog-page">
      <!-- 顶部课程概览 -->
      <section class="course-overview">
        <div class="overview-heading">
          <h1>{{ course?.title || '课程管理' }}</h1>
          <p class="subtitle">管理章节、课时、讲义与实验内容</p>
        </div>
        <div class="overview-actions">
          <button class="button button-secondary" type="button" @click="openCourseSettings">
            <AppIcon name="save" :size="16" /> 保存草稿
          </button>
          <button class="button button-secondary" type="button" @click="openCourseSettings">
            <AppIcon name="settings" :size="16" /> 课程设置
          </button>
          <button class="button button-outline-primary" type="button" @click="chapterDialog = true">
            <AppIcon name="plus" :size="16" /> 新增章节
          </button>
          <button
            class="button button-primary"
            type="button"
            :disabled="!canPublishCourse || publishingCourse"
            @click="publishCourse"
          >
            <AppIcon name="send" :size="16" />
            {{ publishingCourse ? '发布中…' : course?.status === 'published' ? '已发布' : '发布课程' }}
          </button>
        </div>
      </section>

      <!-- 课程统计 -->
      <section class="stats-grid" aria-label="课程统计">
        <article v-for="stat in stats" :key="stat.label" class="stat-card">
          <strong>{{ stat.value }}</strong>
          <span>{{ stat.label }}</span>
        </article>
      </section>

      <!-- 章节列表（空状态/卡片见下） -->
      <div v-if="loading" class="loading-card">正在加载课程目录…</div>

      <!-- 课程空状态 -->
      <section v-else-if="chapters.length === 0" class="empty-card">
        <h2>尚未创建课程章节</h2>
        <p>创建章节后，可以在章节中添加讲义、Notebook 和实验内容。</p>
        <button class="button button-primary" type="button" @click="chapterDialog = true">创建第一个章节</button>
      </section>

      <!-- 章节卡片 -->
      <section v-else class="chapter-stack" aria-label="课程章节">
        <article v-for="(chapter, index) in chapters" :key="chapter.id" class="chapter-card">
          <header class="chapter-header">
            <div class="chapter-heading">
              <span class="chapter-number">第 {{ index + 1 }} 章</span>
              <h2 class="chapter-name">{{ chapter.title }}</h2>
            </div>
            <div class="chapter-actions">
              <button class="text-button" type="button" @click="openAddLesson(chapter.id)">
                <AppIcon name="plus" :size="14" /> 添加课时
              </button>
              <button v-if="API_CAPS.chapterEdit" class="text-button" type="button" @click="openEditChapter(chapter)">
                <AppIcon name="edit" :size="14" /> 编辑章节
              </button>
              <div class="menu-wrap" data-menu>
                <button
                  class="icon-button"
                  type="button"
                  aria-label="章节更多操作"
                  @click.stop="toggleMenu(`chapter:${chapter.id}`)"
                >
                  <AppIcon name="more" :size="18" />
                </button>
                <div v-if="openMenu === `chapter:${chapter.id}`" class="action-menu">
                  <button v-if="API_CAPS.chapterCopy" type="button" @click="copyChapter(chapter)">
                    <AppIcon name="copy" :size="15" /> 复制章节
                  </button>
                  <template v-if="API_CAPS.chapterMove">
                    <button type="button" :disabled="index === 0" @click="moveChapter(chapter, -1)">
                      <AppIcon name="chevron-up" :size="15" /> 上移
                    </button>
                    <button type="button" :disabled="index === chapters.length - 1" @click="moveChapter(chapter, 1)">
                      <AppIcon name="chevron-down" :size="15" /> 下移
                    </button>
                  </template>
                  <template v-if="API_CAPS.lessonPublish && chapterLessons(chapter).length">
                    <div class="menu-divider"></div>
                    <button type="button" @click="unpublishChapter(chapter)">
                      <AppIcon name="eye-off" :size="15" /> 取消发布章节
                    </button>
                  </template>
                  <template v-if="API_CAPS.chapterDelete">
                    <div class="menu-divider"></div>
                    <button class="menu-danger" type="button" @click="askDeleteChapter(chapter)">
                      <AppIcon name="trash" :size="15" /> 删除章节
                    </button>
                  </template>
                </div>
              </div>
              <button
                class="icon-button"
                type="button"
                :aria-label="expanded[chapter.id] ? '收起章节' : '展开章节'"
                @click="toggleChapter(chapter.id)"
              >
                <AppIcon :name="expanded[chapter.id] ? 'chevron-down' : 'chevron-right'" :size="18" />
              </button>
            </div>
          </header>

          <div v-if="expanded[chapter.id]" class="chapter-body">
            <!-- 课时列表 -->
            <div v-if="chapter.lessons?.length" class="lesson-table">
              <div class="lesson-row lesson-head">
                <span>编号</span>
                <span>课时标题</span>
                <span>类型</span>
                <span class="duration-col">时长</span>
                <span>状态</span>
                <span>操作</span>
              </div>
              <div
                v-for="(lesson, lessonIndex) in chapter.lessons"
                :key="lesson.id"
                class="lesson-row"
                @click="openEditLesson(lesson)"
              >
                <span class="lesson-index">{{ index + 1 }}.{{ lessonIndex + 1 }}</span>
                <span class="lesson-title">{{ lesson.title }}</span>
                <span class="type-cell">
                  <em class="lesson-type">{{ typeLabel(lesson.content_type) }}</em>
                  <span class="duration-inline">· {{ durationLabel(lesson) }}</span>
                </span>
                <span class="duration-col duration">{{ durationLabel(lesson) }}</span>
                <span class="status-cell">
                  <em class="publish-status" :class="statusClass(lesson)">
                    <i class="status-dot"></i>{{ statusLabel(lesson) }}
                  </em>
                </span>
                <span class="lesson-actions" @click.stop>
                  <button class="row-action" type="button" @click="openEditLesson(lesson)">
                    <AppIcon name="edit" :size="14" /> 编辑
                  </button>
                  <button class="row-action" type="button" @click="openPreview(lesson)">
                    <AppIcon name="eye" :size="14" /> 预览
                  </button>
                  <div class="menu-wrap" data-menu>
                    <button
                      class="row-action more"
                      type="button"
                      aria-label="更多操作"
                      @click="toggleMenu(`lesson:${lesson.id}`)"
                    >
                      <AppIcon name="more" :size="16" />
                    </button>
                    <div v-if="openMenu === `lesson:${lesson.id}`" class="action-menu">
                      <button v-if="API_CAPS.lessonCopy" type="button" @click="copyLesson(lesson)">
                        <AppIcon name="copy" :size="15" /> 复制课时
                      </button>
                      <button v-if="API_CAPS.lessonMove" type="button" @click="openMoveLesson(lesson)">
                        <AppIcon name="move" :size="15" /> 移动到其他章节
                      </button>
                      <template v-if="API_CAPS.lessonPublish">
                        <div class="menu-divider"></div>
                        <button type="button" @click="toggleLessonPublish(lesson)">
                          <AppIcon :name="isPublished(lesson) ? 'eye-off' : 'eye'" :size="15" />
                          {{ isPublished(lesson) ? '设为草稿' : '发布课时' }}
                        </button>
                      </template>
                      <template v-if="API_CAPS.lessonDelete">
                        <div class="menu-divider"></div>
                        <button class="menu-danger" type="button" @click="askDeleteLesson(lesson)">
                          <AppIcon name="trash" :size="15" /> 删除课时
                        </button>
                      </template>
                    </div>
                  </div>
                </span>
              </div>
            </div>

            <!-- 章节空状态 -->
            <div v-else class="chapter-empty">
              <h3>本章节暂无课时</h3>
              <p>添加讲义、Notebook 或实验，开始构建课程内容。</p>
              <button class="button button-secondary" type="button" @click="openAddLesson(chapter.id)">添加课时</button>
            </div>
          </div>

          <!-- 折叠摘要 -->
          <div v-else class="chapter-summary">{{ chapterSummary(chapter) }}</div>
        </article>
      </section>
    </div>

    <!-- 课程设置抽屉 -->
    <CourseFormModal
      v-if="settingsOpen"
      title="编辑课程"
      description="完善课程基本信息、教学班与可见范围。"
      title-id="course-settings-title"
      :busy="savingSettings || coverBusy"
      panel-class="side-panel course-settings-panel"
      body-class="course-settings-body"
      actions-class="form-actions"
      @close="closeCourseSettings"
      @submit="saveSettings"
    >
        <div class="course-settings-content">
          <label>
            课程名称
            <input v-model="settings.title" />
          </label>
          <label>
            课程简介
            <textarea v-model="settings.description" rows="4"></textarea>
          </label>
          <label>
            课程状态
            <DsSelect v-model="settings.status">
              <option value="draft">草稿</option>
              <option value="published">已发布</option>
              <option value="archived">已归档</option>
            </DsSelect>
          </label>
          <div v-if="publishMissingFields.length" class="publish-requirements" role="alert">
            <strong>发布前还需完善</strong>
            <span>{{ publishMissingFields.join('、') }}</span>
          </div>
          <div class="settings-grid">
            <label>
              所属学期
              <DsSelect v-model="settings.academic_term_id" @change="settings.teaching_class_ids = []">
                <option :value="null">未设置学期</option>
                <option v-for="term in academicTerms" :key="term.id" :value="term.id" :disabled="term.status === 'closed'">{{ term.name }}</option>
              </DsSelect>
            </label>
            <label>
              开课时间
              <input v-model="settings.start_time" type="datetime-local" />
            </label>
          </div>
          <label class="settings-field-full">
            教学班（可多选）
            <TeachingClassMultiSelect
              v-model="settings.teaching_class_ids"
              :options="availableClasses"
              :disabled="savingSettings || coverBusy || !settings.academic_term_id"
              placeholder="请选择教学班"
              empty-text="该学期暂无教学班"
              loading-text="正在加载教学班…"
              test-id="course-settings-teaching-classes"
            />
            <small class="settings-field-hint">点击下拉栏后可搜索并勾选多个教学班</small>
          </label>
          <CourseCoverUploader
            v-if="course"
            :course-id="courseId"
            :course="course"
            @updated="handleCoverUpdated"
            @busy-change="coverBusy = $event"
          />
          <div class="settings-grid">
            <label>
              课程可见范围
              <DsSelect v-model="settings.visibility" data-testid="visibility-select">
                <option value="private">仅自己可见</option>
                <option value="class">教学班可见</option>
                <option value="whitelist">指定学生可见</option>
              </DsSelect>
            </label>
            <label>
              默认评分设置
              <input v-model.number="settings.default_score" type="number" placeholder="100" min="0" />
            </label>
          </div>
          <div
            v-if="settings.visibility === 'private'"
            class="settings-note"
          >
            仅教师及存量已选学生访问，学生无法自行发现或选课。
          </div>
          <div
            v-else-if="settings.visibility === 'class'"
            class="settings-note"
          >
            只有绑定教学班的学生可发现并选课，学生名单会随教学班自动同步。
          </div>
          <div v-else class="settings-note">
            只有指定学生可发现并选课；切换离开本范围不会删除已有名单。
          </div>
          <CourseWhitelistManager
            v-if="settings.visibility === 'whitelist'"
            :course-id="courseId"
          />
          <CourseRosterManager :course-id="courseId" @changed="loadPage" />
        </div>
        <template #actions>
          <button class="button button-secondary" type="button" @click="closeCourseSettings">取消</button>
          <button class="button button-primary" type="submit" :disabled="savingSettings || coverBusy">
            {{ savingSettings ? '保存中…' : '保存设置' }}
          </button>
        </template>
    </CourseFormModal>

    <!-- 添加课时两步弹窗 -->
    <div v-if="createWizard" class="modal-backdrop create-backdrop" @click.self="createWizard = null">
      <div class="create-panel" v-dialog-focus role="dialog" aria-modal="true" aria-label="添加课时">
        <header class="create-heading">
          <strong>添加课时</strong>
          <button class="create-close" type="button" aria-label="关闭" @click="createWizard = null">
            <AppIcon name="close" :size="16" />
          </button>
        </header>

        <!-- 第一步：选择类型 -->
        <div v-if="createWizard.step === 1" class="create-types">
          <button
            v-for="item in CONTENT_TYPES"
            :key="item.type"
            class="create-type-card"
            type="button"
            @click="chooseType(item.type)"
          >
            <AppIcon :name="TYPE_ICONS[item.type]" :size="22" />
            <strong>{{ item.label }}</strong>
            <span>{{ item.desc }}</span>
          </button>
        </div>

        <!-- 第二步：课时名称 + 简介 -->
        <form v-else class="create-form" @submit.prevent="createLesson">
          <label class="create-field">
            <span>课时名称</span>
            <input ref="titleInput" v-model="createWizard.title" placeholder="例如：变量与数据类型" />
          </label>
          <label class="create-field">
            <span>课时简介（可选）</span>
            <textarea v-model="createWizard.description" rows="4" placeholder="课程简介（可选）"></textarea>
          </label>
          <p v-if="createWizard.type === 'video'" class="create-hint create-hint-video">
            视频可在创建后进入编辑页上传。
          </p>
          <!-- Notebook 环境选择（Phase 4：教师选择） -->
          <template v-if="createWizard.type === 'notebook'">
            <EnvironmentProfilePicker
              v-model="notebookEnvId"
              show-memory
              label="运行环境"
              @loaded="onNotebookEnvLoaded"
            />
            <p v-if="!notebookEnvOptions.length" class="create-hint env-warn">暂无可用环境，请联系管理员</p>
            <label class="create-field">
              <span>导入规则</span>
              <DsSelect v-model="notebookPolicy" class="import-policy-select">
                <option value="unrestricted">不限制</option>
                <option value="restricted">限定白名单</option>
              </DsSelect>
            </label>
            <div v-if="notebookPolicy === 'restricted'" class="import-candidates">
              <label v-for="name in notebookImportCandidates" :key="name" class="import-chip">
                <input type="checkbox" :checked="notebookAllowedImports.includes(name)" @change="toggleNotebookImport(name)" />
                {{ name }}
              </label>
              <p v-if="!notebookImportCandidates.length" class="create-hint">当前环境未提供教学库，可留空白名单</p>
            </div>
            <p v-if="notebookMismatch" class="create-hint env-warn">{{ notebookMismatch }}</p>
          </template>
          <div class="create-actions">
            <button class="button button-secondary" type="button" @click="backToTypePicker">上一步</button>
            <button class="button button-primary" type="submit" :disabled="!createWizard.title.trim() || creatingLesson">
              {{ creatingLesson ? '创建中…' : '创建并编辑' }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- 添加章节弹窗 -->
    <div v-if="chapterDialog" class="modal-backdrop create-backdrop" @click.self="chapterDialog = false">
      <form class="create-panel" v-dialog-focus role="dialog" aria-modal="true" aria-label="添加章节" @submit.prevent="createChapter">
        <header class="create-heading">
          <strong>添加章节</strong>
          <button class="create-close" type="button" aria-label="关闭" @click="chapterDialog = false">
            <AppIcon name="close" :size="16" />
          </button>
        </header>
        <div class="create-form">
          <label class="create-field">
            <span>章节名称</span>
            <input ref="chapterTitleInput" v-model="chapterTitle" placeholder="例如：Python 基础" />
          </label>
          <div class="create-actions">
            <button class="button button-secondary" type="button" @click="chapterDialog = false">取消</button>
            <button class="button button-primary" type="submit" :disabled="!chapterTitle.trim()">创建章节</button>
          </div>
        </div>
      </form>
    </div>

    <!-- 学生视角预览 -->
    <div v-if="previewLesson" class="modal-backdrop preview-backdrop" @click.self="previewLesson = null">
      <div class="preview-panel" v-dialog-focus role="dialog" aria-modal="true" aria-label="课时预览">
        <header class="preview-header">
          <div>
            <p class="panel-eyebrow">学生视角预览</p>
            <h2>{{ previewLesson.title }}</h2>
            <p class="preview-meta">
              <em class="lesson-type">{{ typeLabel(previewLesson.content_type) }}</em>
              <span>· {{ durationLabel(previewLesson) }}</span>
              <em class="publish-status" :class="statusClass(previewLesson)">
                <i class="status-dot"></i>{{ statusLabel(previewLesson) }}
              </em>
            </p>
          </div>
          <button class="icon-button" type="button" aria-label="关闭" @click="previewLesson = null">
            <AppIcon name="close" :size="18" />
          </button>
        </header>
        <div v-if="previewLesson.content_type === 'video'" class="preview-body">
          <!-- 本地视频：内嵌播放器 -->
          <template v-if="previewLesson.video_source === 'upload' || previewLesson.video_filename">
            <p v-if="previewVideoLoading" class="preview-video-state">视频加载中…</p>
            <p v-else-if="previewVideoError" class="preview-video-state preview-video-error">
              {{ previewVideoError }}
              <button class="button button-secondary retry-btn" type="button" @click="fetchPreviewVideoUrl(previewLesson.id)">重试</button>
            </p>
            <video
              v-else-if="previewVideoUrl"
              controls
              playsinline
              preload="metadata"
              class="preview-video"
              :src="previewVideoUrl"
            ></video>
            <p v-else class="preview-video-state">该课时尚未上传视频。</p>
          </template>
          <!-- 外链视频：打开视频 -->
          <p v-else-if="previewLesson.video_url">
            视频地址：
            <a :href="previewLesson.video_url" target="_blank" rel="noopener noreferrer">{{ previewLesson.video_url }}</a>
          </p>
          <p v-else>该课时尚未设置视频地址。</p>
        </div>
        <div v-else class="preview-body lesson-content" v-html="previewHtml"></div>
        <footer v-if="previewLesson.content_type === 'notebook' && previewLesson.template_id" class="preview-footer">
          <button class="button button-secondary" type="button" @click="openStudioForPreview(previewLesson)">
            在 Studio 中打开
          </button>
        </footer>
      </div>
    </div>

    <!-- 删除确认弹窗 -->
    <div v-if="deleteTarget" class="modal-backdrop confirm-backdrop" @click.self="deleteTarget = null">
      <div class="confirm-panel" v-dialog-focus role="dialog" aria-modal="true" aria-labelledby="delete-confirm-title">
        <h2 id="delete-confirm-title">确认删除{{ deleteTarget.kind === 'chapter' ? '章节' : '课时' }}？</h2>
        <p>
          {{ deleteTarget.kind === 'chapter' ? '章节' : '课时' }}“{{ deleteTarget.title }}”删除后将无法恢复。
          <template v-if="deleteTarget.kind === 'chapter' && deleteTarget.lessonCount > 0">
            章节内 {{ deleteTarget.lessonCount }} 个课时将一并删除。
          </template>
          如果已有学生学习记录，不建议直接删除。
        </p>
        <div class="confirm-actions">
          <button class="button button-secondary" type="button" @click="deleteTarget = null">取消</button>
          <button class="button button-danger" type="button" :disabled="deleting" @click="confirmDelete">
            {{ deleting ? '删除中…' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 编辑章节抽屉 -->
    <div v-if="editingChapter" class="modal-backdrop" @click.self="editingChapter = null">
      <form class="side-panel" @submit.prevent="saveEditChapter">
        <div class="panel-header">
          <div>
            <p class="panel-eyebrow">编辑章节</p>
            <h2>{{ editingChapter.title }}</h2>
          </div>
          <button class="icon-button" type="button" aria-label="关闭" @click="editingChapter = null">
            <AppIcon name="close" :size="18" />
          </button>
        </div>
        <label>
          章节名称
          <input v-model="chapterEditTitle" />
        </label>
        <div class="form-actions">
          <button class="button button-secondary" type="button" @click="editingChapter = null">取消</button>
          <button class="button button-primary" type="submit" :disabled="savingChapterEdit">
            {{ savingChapterEdit ? '保存中…' : '保存章节' }}
          </button>
        </div>
      </form>
    </div>

    <!-- 移动到其他章节弹窗 -->
    <div v-if="movingLesson" class="modal-backdrop confirm-backdrop" @click.self="movingLesson = null">
      <form class="confirm-panel" v-dialog-focus role="dialog" aria-modal="true" aria-label="移动到其他章节" @submit.prevent="confirmMoveLesson">
        <h2>移动课时</h2>
        <p>课时“{{ movingLesson.title }}”将移动到：</p>
        <label class="move-select">
          <DsSelect v-model="moveTargetChapterId">
            <option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">
              {{ chapter.title }}
            </option>
          </DsSelect>
        </label>
        <div class="confirm-actions">
          <button class="button button-secondary" type="button" @click="movingLesson = null">取消</button>
          <button class="button button-primary" type="submit" :disabled="savingMove || !moveTargetChapterId">
            {{ savingMove ? '移动中…' : '确认移动' }}
          </button>
        </div>
      </form>
    </div>
  </AppLayout>
</template>

<style scoped>
/* 变量定义需同时作用于页面主体与抽屉/弹窗容器（.modal-backdrop）：
   抽屉在 DOM 中是 .catalog-page 的兄弟节点，若变量只定义在 .catalog-page 上，
   抽屉内按钮的 var(--primary-hover) 将解析为空 → hover 时背景回退为透明导致按钮消失 */
.catalog-page,
.modal-backdrop {
  --primary: var(--accent);
  --primary-hover: var(--accent-hover);
  --text-primary: var(--fg);
  --text-secondary: var(--muted);
  --text-muted: var(--muted);
  --border: var(--border);
  --border-light: var(--surface-subtle);
  --page-bg: var(--surface-subtle);
  --card-bg: var(--surface);
  --hover-bg: var(--surface-subtle);
}
.catalog-page {
  width: 100%;
  max-width: 1280px;
  margin: 0 auto;
  padding: 32px 32px 48px;
  color: var(--fg);
  background: var(--page-bg);
}

/* ── 顶部课程概览 ─────────────────────────────────────────────────── */
.course-overview {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
}
.overview-heading { min-width: 0; }
.course-overview h1 {
  margin: 0;
  font-size: 30px;
  line-height: 1.25;
  font-weight: 700;
  letter-spacing: -0.02em;
  overflow-wrap: anywhere;
}
.subtitle { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
.overview-actions { display: flex; align-items: center; gap: 8px; flex: none; }

.button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 42px;
  padding: 0 16px;
  border-radius: var(--radius-md);
  font-size: 14px;
  font-weight: 600;
}
.button-primary { color: var(--surface); background: var(--accent); border-color: var(--accent); }
.button-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.button-outline-primary { color: var(--accent); background: var(--surface); border-color: var(--accent); }
.button-outline-primary:hover { color: var(--accent-hover); background: var(--accent-soft); border-color: var(--accent-hover); }
.button-secondary { color: var(--muted); background: var(--surface); border-color: var(--border); }
.button-secondary:hover { color: var(--fg); background: var(--surface); border-color: var(--border-strong); }
.button-danger { color: var(--surface); background: var(--danger); border-color: var(--danger); }
.button-danger:hover { background: var(--danger); border-color: var(--danger); }

/* ── 课程统计 ─────────────────────────────────────────────────────── */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 28px;
}
.stat-card {
  min-height: 82px;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 3px;
  border: 1px solid var(--surface-subtle);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.stat-card strong { font-size: 22px; line-height: 1.15; font-weight: 700; color: var(--fg); }
.stat-card span { color: var(--muted); font-size: 13px; }

/* ── 章节卡片 ─────────────────────────────────────────────────────── */
.chapter-card {
  margin-top: 16px;
  /* 不能用 overflow:hidden：会把课时行向下弹出的更多菜单裁掉 */
  overflow: visible;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.chapter-header {
  min-height: 64px;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid var(--surface-subtle);
}
.chapter-heading { display: flex; align-items: center; min-width: 0; }
.chapter-number {
  flex: none;
  padding: 5px 10px;
  border-radius: var(--radius-md);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}
.chapter-name {
  min-width: 0;
  margin-left: 14px;
  font-size: 17px;
  font-weight: 650;
  color: var(--fg);
  overflow-wrap: anywhere;
}
.chapter-actions { display: flex; align-items: center; gap: 6px; flex: none; }

.text-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  font-weight: 500;
}
.text-button:hover { color: var(--accent); background: transparent; border-color: transparent; }

.icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--muted);
  border-radius: var(--radius-md);
}
.icon-button:hover { color: var(--fg); background: var(--hover-bg); border-color: transparent; }

.menu-wrap { position: relative; }
.action-menu {
  position: absolute;
  /* 高于相邻卡片内容，避免被后面章节卡片覆盖；低于弹窗层(40) */
  z-index: 30;
  top: calc(100% + 6px);
  right: 0;
  min-width: 150px;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  box-shadow: var(--shadow-sm), 0 6px 16px oklch(0.2 0.01 150 / 0.08);
}
.action-menu button {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: flex-start;
  padding: 8px 10px;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  border-radius: var(--radius-md);
}
.action-menu button:hover { background: var(--hover-bg); color: var(--fg); border-color: transparent; }
.action-menu button:disabled { color: var(--border-strong); cursor: not-allowed; }
.action-menu button:disabled:hover { background: transparent; color: var(--border-strong); }
/* 课时行的菜单向上弹出：避免最后一行课时向下弹出超出视口底部被裁 */
.lesson-row .action-menu,
.lesson-actions .action-menu {
  top: auto;
  bottom: calc(100% + 6px);
}
.menu-divider { height: 1px; margin: 5px 8px; background: var(--border-light); }
.menu-danger { color: var(--danger) !important; }
.menu-danger:hover { background: var(--danger-bg) !important; color: var(--danger) !important; }

/* 移动课时弹窗 */
.move-select { display: block; margin: 0 0 20px; }
.move-select .ds-select { width: 100%; border-radius: var(--radius-md); }

/* ── 课时列表 ─────────────────────────────────────────────────────── */
.chapter-body { padding: 0 18px; }
.lesson-table { width: 100%; }
.lesson-row {
  min-height: 56px;
  padding: 0;
  display: grid;
  align-items: center;
  grid-template-columns: 52px minmax(240px, 1fr) 110px 110px 100px auto;
  border-bottom: 1px solid var(--border-light);
  background: var(--surface);
  font-size: 13px;
  transition: background var(--duration-fast, 120ms) ease;
}
.lesson-row:last-child { border-bottom: 0; }
.lesson-row:not(.lesson-head):hover { background: var(--hover-bg); }
.lesson-head { min-height: 40px; color: var(--muted); font-size: 12px; }
.lesson-head span { white-space: nowrap; }

.lesson-index {
  color: var(--muted);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
.lesson-title {
  min-width: 0;
  font-size: 15px;
  font-weight: 550;
  color: var(--fg);
  overflow-wrap: anywhere;
}
.type-cell { min-width: 0; display: flex; align-items: center; gap: 4px; }
.duration-inline { display: none; }
.duration { color: var(--muted); font-size: 13px; white-space: nowrap; }

.lesson-type {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  padding: 3px 8px;
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
  font-style: normal;
  white-space: nowrap;
}

.publish-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 9px;
  border-radius: var(--radius-md);
  font-size: 12px;
  font-style: normal;
  white-space: nowrap;
}
.publish-status .status-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.publish-status.published { color: var(--success); background: var(--success-bg); }
.publish-status.draft { color: var(--muted); background: var(--surface-subtle); }
.publish-status.pending { color: var(--danger); background: var(--warning-bg); }

.lesson-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  white-space: nowrap;
}
.row-action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 7px;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
  border-radius: var(--radius-md);
}
.row-action:hover { color: var(--accent); background: var(--hover-bg); border-color: transparent; }
.row-action.more { color: var(--muted); padding: 6px 8px; }
.row-action.more:hover { color: var(--fg); }

.form-actions { display: flex; align-items: center; gap: 8px; }

/* ── 添加课时两步弹窗 ─────────────────────────────────────────────── */
/* 注意：双类选择器必须压过基础 .modal-backdrop 的 justify-content: flex-end
   （基础样式定义在后面，单类 .create-backdrop 会被覆盖导致弹窗偏右） */
.modal-backdrop.create-backdrop {
  justify-content: center;
  align-items: center;
}
.create-panel {
  width: min(520px, calc(100% - 32px));
  padding: 24px;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border);
  background: var(--surface);
  box-shadow: var(--shadow-lg);
}
.create-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.create-heading strong { font-size: 17px; color: var(--fg); }
.create-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--muted);
  border-radius: var(--radius-md);
}
.create-close:hover { background: var(--hover-bg); color: var(--fg); border-color: transparent; }
.create-types {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.create-type-card {
  min-height: 96px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  text-align: left;
  font-weight: 500;
}
.create-type-card:hover { border-color: var(--info-bg); background: var(--accent-soft); }
.create-type-card .app-icon { color: var(--accent); }
/* ── Phase 4：Notebook 环境选择 ─────────────────────────────────── */
.import-policy-select {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control, 7px);
  background: var(--surface, var(--surface));
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-sm, 13px);
}
.env-warn { color: var(--warning, var(--warning)); }
.import-candidates {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.import-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--surface-raised, var(--surface-subtle));
  font-size: var(--text-sm, 13px);
  cursor: pointer;
}
.import-chip input { margin: 0; }
.create-type-card strong { color: var(--fg); font-size: 14px; }
.create-type-card span { color: var(--muted); font-size: 12px; line-height: 1.4; }
.create-form { display: grid; gap: 14px; }
.create-field { display: grid; gap: 6px; }
.create-field > span { color: var(--muted); font-size: 13px; font-weight: 600; }
.create-field input,
.create-field textarea { border-radius: var(--radius-md); }
.create-hint { margin: 0; color: var(--muted); font-size: 12px; }
.create-hint-video {
  margin: -6px 0 12px;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  border: 1px dashed var(--border-strong);
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.create-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }

/* ── 章节空状态 / 折叠摘要 / 加载 / 课程空状态 ─────────────────────── */
.chapter-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 20px 44px;
  text-align: center;
}
.chapter-empty h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--fg); }
.chapter-empty p { margin: 0; color: var(--muted); font-size: 13px; }

.chapter-summary {
  padding: 14px 20px 18px;
  color: var(--muted);
  font-size: 13px;
}

.loading-card,
.empty-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.loading-card,
.empty-card { padding: 48px 24px; text-align: center; }
.empty-card h2 { margin: 0 0 8px; font-size: 18px; color: var(--fg); }
.empty-card p { margin: 0 0 20px; color: var(--muted); font-size: 14px; }

/* ── 抽屉 / 弹窗 ──────────────────────────────────────────────────── */
.modal-backdrop {
  position: fixed;
  z-index: 40;
  /* left 随侧栏宽度（--modal-left 由 AppLayout 按收起状态提供），
     弹窗以内容区为基准居中，而不是整个视口 */
  inset: 0 0 0 var(--modal-left, 0);
  display: flex;
  justify-content: flex-end;
  background: oklch(0.2 0.01 150 / 0.25);
}
.side-panel {
  width: min(460px, 100%);
  height: 100%;
  overflow-y: auto;
  padding: 28px;
  background: var(--surface);
  box-shadow: -4px 0 16px oklch(0.2 0.01 150 / 0.08);
}
.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 24px;
}
.panel-header h2 { margin: 0; font-size: 22px; line-height: 1.3; color: var(--fg); overflow-wrap: anywhere; }
.panel-eyebrow { margin: 0 0 4px; color: var(--muted); font-size: 12px; font-weight: 600; }
.course-settings-content label {
  display: grid;
  gap: 6px;
  margin-bottom: 16px;
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
}
.course-settings-content input,
.course-settings-content textarea,
.course-settings-content .ds-select {
  width: 100%;
  border-radius: var(--radius-md);
}
.settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.settings-field-full { width: 100%; min-width: 0; }
.settings-field-hint { color: var(--faint); font-size: 12px; font-weight: 400; }
.publish-requirements {
  display: grid;
  gap: 4px;
  margin: -4px 0 16px;
  padding: 10px 12px;
  border: 1px solid var(--danger-bg);
  border-radius: var(--radius-md);
  background: var(--danger-bg);
  color: var(--danger);
  font-size: 12px;
  line-height: 1.5;
}
.settings-note {
  margin: 4px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  background: var(--hover-bg);
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.side-panel .form-actions { justify-content: flex-end; margin-top: 24px; }

.preview-backdrop {
  justify-content: center;
  align-items: flex-start;
  padding: 40px 16px;
  overflow-y: auto;
}
.preview-panel {
  width: min(760px, 100%);
  border-radius: var(--radius-lg);
  border: 1px solid var(--border);
  background: var(--surface);
  box-shadow: var(--shadow-sm), 0 12px 32px oklch(0.2 0.01 150 / 0.1);
  overflow: hidden;
}
.preview-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 24px;
  border-bottom: 1px solid var(--border-light);
}
.preview-header h2 { margin: 0; font-size: 20px; color: var(--fg); overflow-wrap: anywhere; }
.preview-meta { display: flex; align-items: center; gap: 8px; margin: 8px 0 0; color: var(--muted); font-size: 13px; }
.preview-body { padding: 24px; color: var(--muted); font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
.preview-footer { padding: 12px 24px 20px; }
.preview-video {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  border-radius: var(--radius-md);
  background: var(--fg);
}
.preview-video-state { margin: 0; color: var(--muted); font-size: 13px; }
.preview-video-error { display: flex; align-items: center; gap: 8px; color: var(--danger, var(--danger)); }
.retry-btn { flex: none; }
.lesson-content :deep(p) { margin: 0 0 12px; }
.lesson-content :deep(h1),
.lesson-content :deep(h2),
.lesson-content :deep(h3) { margin: 20px 0 10px; color: var(--fg); }
.lesson-content :deep(pre) { padding: 12px 14px; overflow-x: auto; background: var(--surface-subtle); border-radius: var(--radius-md); }
.lesson-content :deep(code) { background: var(--surface-subtle); border: 0; color: var(--fg); }
.lesson-content :deep(a) { color: var(--accent); }
.lesson-content :deep(ul),
.lesson-content :deep(ol) { margin: 0 0 12px; padding-left: 22px; }
.lesson-content :deep(blockquote) { margin: 0 0 12px; padding-left: 12px; border-left: 3px solid var(--border); color: var(--muted); }

.confirm-backdrop {
  justify-content: center;
  align-items: center;
}
.confirm-panel {
  width: min(420px, calc(100% - 32px));
  padding: 24px;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border);
  background: var(--surface);
  box-shadow: var(--shadow-lg);
}
.confirm-panel h2 { margin: 0 0 10px; font-size: 17px; color: var(--fg); }
.confirm-panel p { margin: 0 0 20px; color: var(--muted); font-size: 13px; line-height: 1.6; }
.confirm-actions { display: flex; justify-content: flex-end; gap: 8px; }

/* ── 响应式 ───────────────────────────────────────────────────────── */
/* 1024–1199px：隐藏时长列，保留标题/类型/状态/操作 */
@media (max-width: 1199px) {
  .catalog-page { padding-inline: 24px; }
  .lesson-row { grid-template-columns: 52px minmax(180px, 1fr) 110px 100px auto; }
  .duration-col { display: none; }
}

/* <900px：课时改为上下两层，时长合并进类型行 */
@media (max-width: 899px) {
  .catalog-page { padding: 24px 16px 40px; }
  .course-overview { align-items: flex-start; flex-direction: column; }
  .overview-actions { width: 100%; flex-wrap: wrap; }
  .overview-actions .button { flex: 1 1 auto; }
  .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }

  .chapter-header {
    flex-wrap: wrap;
    align-items: flex-start;
    padding: 12px 16px;
    gap: 10px;
  }
  .chapter-heading { flex: 1 1 auto; min-width: 0; }
  .chapter-actions { margin-left: auto; }
  .chapter-body { padding-inline: 12px; }

  .lesson-row {
    min-height: 0;
    grid-template-columns: 48px minmax(0, 1fr) auto;
    row-gap: 4px;
    padding: 10px 12px;
  }
  .lesson-head { display: none; }
  .lesson-row > :nth-child(1) { grid-column: 1; grid-row: 1; }
  .lesson-row > :nth-child(2) { grid-column: 2; grid-row: 1; }
  .lesson-row > :nth-child(3) { grid-column: 2; grid-row: 2; }
  .lesson-row > :nth-child(4) { display: none; }
  .lesson-row > :nth-child(5) { grid-column: 3; grid-row: 1; justify-self: end; }
  .lesson-row > :nth-child(6) { grid-column: 3; grid-row: 2; justify-self: end; }
  .duration-inline { display: inline; }

  .create-types { grid-template-columns: 1fr; }
}

@media (max-width: 520px) {
  .course-overview h1 { font-size: 26px; }
  .stats-grid { gap: 8px; }
  .stat-card { padding: 14px; }
  .settings-grid { grid-template-columns: 1fr; }
  .preview-body { padding: 16px; }
}
</style>
