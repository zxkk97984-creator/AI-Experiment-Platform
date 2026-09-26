<script setup>
// 讲义编辑页：标题 + Markdown 正文（编辑/预览切换），未保存离开弹自定义确认框
import { computed, onMounted, ref } from 'vue'
import { marked } from 'marked'
import AppIcon from '../../components/ui/AppIcon.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import { useLessonEditor } from '../../composables/useLessonEditor.js'
import { sanitizeHtml } from '../../utils/sanitize.js'

const props = defineProps({
  courseId: { type: [String, Number], required: true },
  lessonId: { type: [String, Number], required: true },
  backPath: { type: [String, Object], required: true },
})

const title = ref('')
const content = ref('')
const mode = ref('edit') // edit | preview
// 已保存快照：dirty = 当前表单与快照 diff；保存成功后重置
const snapshot = ref(null)

function isDirty() {
  if (!snapshot.value) return false
  return title.value !== snapshot.value.title || content.value !== snapshot.value.content
}

const {
  lesson, loading, loadError, saving, publishing, saveState, showLeaveDialog,
  load, save, publish, goBack, onConfirmLeave, onCancelLeave,
} = useLessonEditor({
  courseId: props.courseId,
  lessonId: props.lessonId,
  backPath: props.backPath,
  isDirty,
  buildPayload: () => ({ title: title.value.trim(), content: content.value || undefined }),
})

async function initForm() {
  await load()
  if (lesson.value) {
    title.value = lesson.value.title || ''
    content.value = lesson.value.content || ''
    snapshot.value = { title: title.value, content: content.value }
  }
}
onMounted(initForm)

async function handleSave() {
  const ok = await save()
  if (ok) snapshot.value = { title: title.value, content: content.value }
}

// 预览 = marked + sanitizeHtml（与 ChapterManageView / 学生端同款，必须过 sanitize）
const previewHtml = computed(() => {
  const raw = marked.parse(content.value || '', { async: false })
  return sanitizeHtml(typeof raw === 'string' ? raw : '')
})

const charCount = computed(() => content.value.length)
const canSave = computed(() => !saving.value && title.value.trim().length > 0)

// 发布状态判断：兼容后端旧字段 published 布尔值（与 ChapterManageView 一致）
const isPublished = computed(() =>
  lesson.value?.status === 'published' || lesson.value?.published === true
)

// 发布/转为草稿：一次 PATCH 提交当前内容 + status，成功后重置快照
async function handlePublish() {
  const ok = await publish(isPublished.value ? 'draft' : 'published')
  if (ok) snapshot.value = { title: title.value, content: content.value }
}
</script>

<template>
  <div class="editor-page">
    <!-- 页头：返回 + 类型徽标 + 标题 + 保存状态 + 保存 -->
    <header class="editor-header">
      <button class="back-btn" type="button" @click="goBack">
        <AppIcon name="back" :size="16" /> 返回
      </button>
      <em class="type-badge">讲义</em>
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
      <div class="mode-tabs" role="tablist" aria-label="编辑/预览切换">
        <button type="button" :class="{ active: mode === 'edit' }" @click="mode = 'edit'">编辑</button>
        <button type="button" :class="{ active: mode === 'preview' }" @click="mode = 'preview'">预览</button>
      </div>
      <textarea
        v-if="mode === 'edit'"
        v-model="content"
        class="content-textarea"
        rows="18"
        placeholder="支持 Markdown 语法…"
        aria-label="讲义正文"
      ></textarea>
      <div v-else class="preview-body lesson-content" v-html="previewHtml"></div>
      <p class="char-count">{{ charCount }} 字</p>
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
.mode-tabs {
  display: inline-flex;
  gap: 4px;
  padding: 3px;
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
}
.mode-tabs button {
  padding: 5px 16px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  font-weight: 500;
}
.mode-tabs button.active { background: var(--surface); color: var(--fg); box-shadow: var(--shadow-sm); }
.content-textarea {
  width: 100%;
  min-height: 360px;
  resize: vertical;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.7;
}
.char-count { margin: 8px 0 0; color: var(--faint); font-size: 12px; text-align: right; }

/* ── 预览渲染（与学生端同款排版） ─────────────────────────────────── */
.preview-body { color: var(--muted); font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
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

.editor-page { max-width: 1180px; margin-inline: auto; }
.editor-header { position: sticky; top: var(--header-height); z-index: 15; background: var(--bg); }
.content-textarea { min-height: max(360px, calc(100dvh - 270px)); font-size: 14px; }
.preview-body { max-width: 780px; margin-inline: auto; font-size: 16px; line-height: 1.7; }
</style>
