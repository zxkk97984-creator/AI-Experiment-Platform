<script setup>
// 视频编辑页：标题 + 视频来源（外链 / 本地上传）+ 简介
// 外链来源：URL 输入 + 外链预览；上传来源：拖拽/选择上传 + 进度 + 本地播放器
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import { coursesAPI } from '../../api/courses.js'
import { useLessonEditor } from '../../composables/useLessonEditor.js'

const props = defineProps({
  courseId: { type: [String, Number], required: true },
  lessonId: { type: [String, Number], required: true },
  backPath: { type: [String, Object], required: true },
})

const title = ref('')
const videoUrl = ref('')
const content = ref('')
const snapshot = ref(null)

// ── 视频来源状态：external（外链）/ upload（本地上传） ──
const sourceMode = ref('external')
const selectedFile = ref(null)
const uploading = ref(false)
const uploadProgress = ref(null) // 0~100，无进度事件时为 null（不确定进度）
const uploadError = ref('')
const uploadCancelled = ref(false)
const playbackUrl = ref('')
const previewError = ref('')
const uploadController = ref(null)
const showSwitchConfirm = ref(false)
const pendingExternalSave = ref(false)
const dragOver = ref(false)

// 前端快速校验（后端仍执行权威校验）
const MAX_UPLOAD_BYTES = 500 * 1024 * 1024
const ALLOWED_EXTS = ['.mp4', '.webm', '.mov']
const ALLOWED_MIMES = ['video/mp4', 'video/webm', 'video/quicktime']

function isDirty() {
  if (!snapshot.value) return false
  return title.value !== snapshot.value.title
    || videoUrl.value !== snapshot.value.videoUrl
    || content.value !== snapshot.value.content
}

const {
  lesson, loading, loadError, saving, publishing, saveState, showLeaveDialog,
  load, save, publish, goBack, onConfirmLeave, onCancelLeave,
} = useLessonEditor({
  courseId: props.courseId,
  lessonId: props.lessonId,
  backPath: props.backPath,
  isDirty,
  buildPayload: () => ({
    title: title.value.trim(),
    // 空串归一 null：后端字段可空
    video_url: videoUrl.value.trim() || null,
    content: content.value || undefined,
  }),
})

async function fetchPlaybackUrl() {
  if (!lesson.value) return
  previewError.value = ''
  try {
    const res = await coursesAPI.getLessonVideoPlaybackUrl(props.lessonId)
    playbackUrl.value = res.data.url
  } catch (err) {
    previewError.value = '视频预览加载失败，请重试'
    playbackUrl.value = ''
    console.error('[LessonVideoEditor] 获取播放地址失败', err)
  }
}

async function initForm() {
  await load()
  if (lesson.value) {
    title.value = lesson.value.title || ''
    content.value = lesson.value.content || ''
    // 兼容缺少 video_source 的旧响应：视为 external
    if (lesson.value.video_source === 'upload') {
      sourceMode.value = 'upload'
      videoUrl.value = ''
      await fetchPlaybackUrl()
    } else {
      sourceMode.value = 'external'
      videoUrl.value = lesson.value.video_url || ''
    }
    snapshot.value = { title: title.value, videoUrl: videoUrl.value, content: content.value }
  }
}
onMounted(initForm)

// 卸载时取消进行中的上传
onBeforeUnmount(() => {
  if (uploadController.value) uploadController.value.abort()
})

// ── 上传模式 ─────────────────────────────────────────────────────
function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function validateFile(file) {
  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!ALLOWED_EXTS.includes(ext)) return '仅支持 .mp4 / .webm / .mov 视频文件'
  if (!ALLOWED_MIMES.includes(file.type)) return '该文件类型不受支持'
  if (file.size > MAX_UPLOAD_BYTES) return '视频超过 500 MiB 大小限制'
  return ''
}

function errorMessage(err) {
  if (err.code === 'ERR_CANCELED') return '上传已取消'
  if (err.code === 'ECONNABORTED') return '上传超时，请检查网络后重试'
  const status = err.response?.status
  if (status === 413) return '视频超过 500 MiB 大小限制'
  if (status === 415) {
    const code = err.response?.data?.detail?.code
    if (code === 'VIDEO_CONTENT_INVALID') return '视频文件内容格式校验失败'
    return '文件扩展名、MIME 或内容不受支持'
  }
  if (status === 403) return '无权修改该课程的视频'
  if (status === 404) return '课时已不存在'
  const detailMessage = err.response?.data?.detail?.message
  return detailMessage || '视频上传失败，请重试'
}

async function uploadFile(file) {
  const invalid = validateFile(file)
  if (invalid) {
    uploadError.value = invalid
    return
  }
  selectedFile.value = file
  uploading.value = true
  uploadError.value = ''
  uploadCancelled.value = false
  uploadProgress.value = null
  uploadController.value = new AbortController()
  try {
    const res = await coursesAPI.uploadLessonVideo(props.lessonId, file, {
      onUploadProgress: (e) => {
        if (e.total) uploadProgress.value = Math.round((e.loaded / e.total) * 100)
        else uploadProgress.value = null
      },
      signal: uploadController.value.signal,
    })
    // 上传成功：更新课时状态、来源与播放器地址
    lesson.value = res.data.lesson
    sourceMode.value = 'upload'
    videoUrl.value = ''
    playbackUrl.value = res.data.playback_url
    previewError.value = ''
    uploadProgress.value = 100
    snapshot.value = { title: title.value, videoUrl: videoUrl.value, content: content.value }
  } catch (err) {
    if (err.code === 'ERR_CANCELED') {
      uploadCancelled.value = true
      uploadError.value = ''
    } else {
      uploadError.value = errorMessage(err)
    }
    // 失败保留服务器上原视频，不清空现有预览
  } finally {
    uploading.value = false
    uploadController.value = null
  }
}

function onFileChange(e) {
  const file = e.target.files?.[0]
  if (file) uploadFile(file)
  e.target.value = ''
}

function onDrop(e) {
  dragOver.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) uploadFile(file)
}

function cancelUpload() {
  if (uploadController.value) uploadController.value.abort()
}

// ── 移除本地视频 ─────────────────────────────────────────────────
async function removeVideo() {
  uploadError.value = ''
  try {
    await coursesAPI.deleteLessonVideo(props.lessonId)
    sourceMode.value = 'external'
    playbackUrl.value = ''
    selectedFile.value = null
    uploadProgress.value = null
    lesson.value = { ...lesson.value, video_source: 'external', video_filename: null, video_content_type: null, video_size: null }
    snapshot.value = { title: title.value, videoUrl: videoUrl.value, content: content.value }
  } catch (err) {
    uploadError.value = errorMessage(err)
  }
}

// ── 保存（外链切换需确认） ───────────────────────────────────────
async function handleSave() {
  // 课时已有本地视频且填写了外链：保存会切换来源并删除已上传文件，先确认
  if (lesson.value?.video_filename && videoUrl.value.trim()) {
    pendingExternalSave.value = true
    showSwitchConfirm.value = true
    return
  }
  await doSave()
}

async function doSave() {
  const ok = await save()
  if (ok) snapshot.value = { title: title.value, videoUrl: videoUrl.value, content: content.value }
}

async function confirmSwitchToExternal() {
  showSwitchConfirm.value = false
  const ok = await save()
  if (ok) {
    // 保存成功：来源已切换为外链，清空本地视频状态
    sourceMode.value = 'external'
    playbackUrl.value = ''
    if (lesson.value) {
      lesson.value = { ...lesson.value, video_source: 'external', video_filename: null, video_content_type: null, video_size: null }
    }
    snapshot.value = { title: title.value, videoUrl: videoUrl.value, content: content.value }
  }
  pendingExternalSave.value = false
}

function cancelSwitchToExternal() {
  showSwitchConfirm.value = false
  pendingExternalSave.value = false
}

const charCount = computed(() => content.value.length)
const canSave = computed(() => !saving.value && title.value.trim().length > 0 && !uploading.value)
const canUpload = computed(() => !uploading.value)

// 发布状态判断：兼容后端旧字段 published 布尔值（与 ChapterManageView 一致）
const isPublished = computed(() =>
  lesson.value?.status === 'published' || lesson.value?.published === true
)

// 发布/转为草稿：一次 PATCH 提交当前内容 + status，成功后重置快照
async function handlePublish() {
  const ok = await publish(isPublished.value ? 'draft' : 'published')
  if (ok) snapshot.value = { title: title.value, videoUrl: videoUrl.value, content: content.value }
}
</script>

<template>
  <div class="editor-page">
    <!-- 页头：返回 + 类型徽标 + 标题 + 保存状态 + 保存 -->
    <header class="editor-header">
      <button class="back-btn" type="button" @click="goBack">
        <AppIcon name="back" :size="16" /> 返回
      </button>
      <em class="type-badge">视频</em>
      <input v-model="title" class="title-input" placeholder="课时标题" aria-label="课时标题" />
      <span class="save-state" :class="{ dirty: isDirty() }">{{ saveState }}</span>
      <button class="btn-primary save-btn" type="button" :disabled="!canSave" @click="handleSave">保存</button>
      <button
        v-if="lesson"
        class="btn-outline publish-btn"
        type="button"
        :disabled="publishing || saving || loading"
        @click="handlePublish"
      >{{ isPublished ? '转为草稿' : '发布' }}</button>
    </header>

    <div v-if="loading" class="editor-body">
      <div v-for="i in 4" :key="i" class="skeleton skeleton-line"></div>
    </div>

    <!-- 加载失败 / 课时不存在 -->
    <div v-else-if="loadError" class="error-card">
      <h2>{{ loadError }}</h2>
      <p>请返回课时列表确认该课时仍然存在。</p>
      <button class="btn-primary" type="button" @click="goBack">返回</button>
    </div>

    <div v-else class="editor-body">
      <!-- 来源切换 -->
      <div class="source-tabs" role="tablist" aria-label="视频来源">
        <button
          type="button"
          class="source-tab"
          :class="{ active: sourceMode === 'external' }"
          :disabled="uploading"
          @click="sourceMode = 'external'"
        >视频链接</button>
        <button
          type="button"
          class="source-tab"
          :class="{ active: sourceMode === 'upload' }"
          :disabled="uploading"
          @click="sourceMode = 'upload'"
        >上传视频文件</button>
      </div>

      <!-- 外链模式 -->
      <template v-if="sourceMode === 'external'">
        <label class="field-label" for="video-url">视频链接 URL</label>
        <input
          id="video-url"
          v-model="videoUrl"
          class="video-url-input"
          type="url"
          placeholder="https://example.com/video.mp4"
          aria-label="视频链接 URL"
        />
      </template>

      <!-- 上传模式 -->
      <template v-else>
        <div
          class="dropzone"
          :class="{ dragging: dragOver, disabled: uploading }"
          @dragover.prevent="dragOver = true"
          @dragleave.prevent="dragOver = false"
          @drop.prevent="onDrop"
        >
          <input
            id="video-file"
            type="file"
            accept=".mp4,.webm,.mov,video/mp4,video/webm,video/quicktime"
            class="file-input"
            :disabled="!canUpload"
            @change="onFileChange"
          />
          <label class="dropzone-inner" for="video-file">
            <AppIcon name="video" :size="22" />
            <p v-if="uploading" class="dropzone-title">正在上传…</p>
            <p v-else class="dropzone-title">点击选择或拖拽视频文件到此处</p>
            <p class="dropzone-hint">支持 .mp4 / .webm / .mov，单文件不超过 500 MiB</p>
            <p class="dropzone-hint">推荐 H.264/AAC MP4 或 VP9/Opus WebM（浏览器兼容最佳）</p>
          </label>
        </div>

        <!-- 上传进度 -->
        <div v-if="uploading || uploadProgress !== null || uploadCancelled" class="upload-status">
          <p v-if="selectedFile" class="upload-file">
            {{ selectedFile.name }}（{{ formatBytes(selectedFile.size) }}）
          </p>
          <div v-if="uploading" class="progress-row">
            <div class="progress-track" aria-label="上传进度">
              <div class="progress-fill" :style="{ width: uploadProgress !== null ? uploadProgress + '%' : '100%' }" :class="{ indeterminate: uploadProgress === null }"></div>
            </div>
            <span class="progress-text">{{ uploadProgress !== null ? uploadProgress + '%' : '上传中…' }}</span>
            <button type="button" class="btn-ghost cancel-btn" @click="cancelUpload">取消</button>
          </div>
          <p v-else-if="uploadCancelled" class="upload-cancelled">上传已取消</p>
        </div>

        <p v-if="uploadError" class="upload-error" role="alert">{{ uploadError }}</p>

        <!-- 当前本地视频 -->
        <section v-if="lesson && lesson.video_filename" class="current-file">
          <p class="field-label">已上传文件</p>
          <p class="file-meta">
            {{ lesson.video_filename }}
            <span v-if="lesson.video_size">（{{ formatBytes(lesson.video_size) }}）</span>
          </p>
          <button type="button" class="btn-ghost remove-btn" @click="removeVideo">移除已上传视频</button>
        </section>
      </template>

      <label class="field-label" for="video-desc">简介</label>
      <textarea
        id="video-desc"
        v-model="content"
        class="content-textarea"
        rows="8"
        placeholder="课程简介（可选）…"
        aria-label="视频简介"
      ></textarea>
      <p class="char-count">{{ charCount }} 字</p>

      <!-- 预览区 -->
      <section class="preview-pane">
        <p class="pane-title">预览</p>
        <!-- 外链预览 -->
        <template v-if="sourceMode === 'external'">
          <p v-if="videoUrl.trim()">
            视频地址：
            <a :href="videoUrl.trim()" target="_blank" rel="noopener noreferrer">{{ videoUrl.trim() }}</a>
          </p>
          <p v-else>该课时尚未设置视频地址。</p>
        </template>
        <!-- 本地视频预览 -->
        <template v-else>
          <p v-if="previewError" class="preview-error">
            {{ previewError }}
            <button type="button" class="btn-ghost retry-btn" @click="fetchPlaybackUrl">重试</button>
          </p>
          <video
            v-else-if="playbackUrl"
            controls
            playsinline
            preload="metadata"
            class="video-player"
            :src="playbackUrl"
          ></video>
          <p v-else>本地视频上传后在此预览。</p>
        </template>
      </section>
    </div>

    <!-- 未保存离开确认（四页统一文案） -->
    <ConfirmDialog
      v-if="showLeaveDialog"
      title="有未保存的修改"
      message="确定离开吗？未保存的内容将丢失。"
      confirm-text="离开"
      cancel-text="取消"
      @confirm="onConfirmLeave"
      @cancel="onCancelLeave"
    />
    <!-- 外链切换确认：切换后将删除已上传文件 -->
    <ConfirmDialog
      v-if="showSwitchConfirm"
      title="切换为视频链接"
      message="切换后将删除已上传文件，确定继续吗？"
      confirm-text="确定切换"
      cancel-text="取消"
      @confirm="confirmSwitchToExternal"
      @cancel="cancelSwitchToExternal"
    />
  </div>
</template>

<style scoped>
.editor-page {
  max-width: 960px;
  margin: 0 auto;
  padding: 32px 24px 48px;
  color: var(--fg);
}

/* ── 页头 ─────────────────────────────────────────────────────────── */
.editor-header {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 16px;
  margin-bottom: 24px;
  border-bottom: 1px solid var(--border);
}
.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--muted);
  font-size: 13px;
  font-weight: 500;
}
.back-btn:hover { border-color: var(--border-strong); color: var(--fg); }
.type-badge {
  flex: none;
  padding: 3px 8px;
  border-radius: var(--radius-md);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 12px;
  font-weight: 500;
  font-style: normal;
  white-space: nowrap;
}
.title-input {
  flex: 1;
  min-width: 220px;
  font-size: 16px;
  font-weight: 600;
}
.save-state { font-size: 12px; color: var(--muted); white-space: nowrap; }
.save-state.dirty { color: var(--warning); }
.save-btn { flex: none; }
.publish-btn {
  flex: none;
  border: 1px solid var(--accent);
  color: var(--accent);
  background: var(--surface);
  font-weight: 500;
}
.publish-btn:hover:not(:disabled) {
  background: var(--accent-soft);
  border-color: var(--accent);
}

/* ── 正文区 ───────────────────────────────────────────────────────── */
.editor-body { min-height: 200px; }
.skeleton-line { height: 18px; margin-bottom: 14px; }
.field-label {
  display: block;
  margin: 0 0 6px;
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
}
.video-url-input { margin-bottom: 18px; }
.content-textarea {
  width: 100%;
  min-height: 120px;
  resize: vertical;
  font-size: 13px;
  line-height: 1.7;
}
.char-count { margin: 8px 0 0; color: var(--faint); font-size: 12px; text-align: right; }

/* ── 来源切换 ─────────────────────────────────────────────────────── */
.source-tabs {
  display: inline-flex;
  gap: 4px;
  margin-bottom: 18px;
  padding: 3px;
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  border: 1px solid var(--border);
}
.source-tab {
  padding: 6px 14px;
  border: none;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}
.source-tab.active { background: var(--accent); color: var(--surface); }
.source-tab:disabled { opacity: 0.5; cursor: not-allowed; }

/* ── 上传区 ───────────────────────────────────────────────────────── */
.dropzone {
  position: relative;
  margin-bottom: 18px;
  border: 1.5px dashed var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  transition: border-color 0.15s, background 0.15s;
}
.dropzone.dragging { border-color: var(--accent); background: var(--accent-soft); }
.dropzone.disabled { opacity: 0.6; }
.file-input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  opacity: 0;
  cursor: pointer;
}
.dropzone-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 28px 16px;
  text-align: center;
  cursor: pointer;
}
.dropzone-title { margin: 0; font-size: 14px; font-weight: 600; color: var(--fg); }
.dropzone-hint { margin: 0; font-size: 12px; color: var(--faint); }

/* ── 上传进度与错误 ───────────────────────────────────────────────── */
.upload-status { margin: 4px 0 14px; }
.upload-file { margin: 0 0 8px; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.progress-row { display: flex; align-items: center; gap: 10px; }
.progress-track {
  flex: 1;
  height: 8px;
  border-radius: var(--radius-sm);
  background: var(--border);
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  border-radius: var(--radius-sm);
  background: var(--accent);
  transition: width 0.2s;
}
.progress-fill.indeterminate { animation: indeterminate 1.2s linear infinite; background: var(--accent); }
@keyframes indeterminate {
  0% { width: 20%; margin-left: -20%; }
  100% { width: 20%; margin-left: 100%; }
}
.progress-text { font-size: 12px; color: var(--muted); white-space: nowrap; }
.cancel-btn { flex: none; }
.upload-cancelled { margin: 0; font-size: 13px; color: var(--muted); }
.upload-error {
  margin: 0 0 14px;
  padding: 8px 12px;
  border: 1px solid var(--danger-soft, var(--danger-bg));
  border-radius: var(--radius-md);
  background: var(--danger-bg, var(--danger-bg));
  color: var(--danger);
  font-size: 13px;
}
.current-file { margin-bottom: 18px; }
.file-meta { margin: 0 0 8px; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.remove-btn { border-color: var(--danger-soft, var(--danger-bg)); color: var(--danger); }

/* ── 预览区 ───────────────────────────────────────────────────────── */
.preview-pane {
  margin-top: 20px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  font-size: 13px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.pane-title { margin: 0 0 6px; font-size: 12px; font-weight: 600; color: var(--faint); }
.preview-pane a { color: var(--accent); }
.preview-error { margin: 0; display: flex; align-items: center; gap: 8px; color: var(--danger); }
.retry-btn { flex: none; }
.video-player {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  border-radius: var(--radius-md);
  background: var(--fg);
}

/* ── 错误态 ───────────────────────────────────────────────────────── */
.error-card {
  padding: 48px 24px;
  text-align: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
}
.error-card h2 { margin: 0 0 8px; font-size: 18px; color: var(--fg); }
.error-card p { margin: 0 0 20px; color: var(--muted); font-size: 14px; }

.editor-header { position: sticky; top: var(--header-height); z-index: 15; background: var(--bg); }
.editor-page { max-width: 1100px; margin-inline: auto; }
video { max-height: calc(100dvh - 320px); width: 100%; }
</style>
