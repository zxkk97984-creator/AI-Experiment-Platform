<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import { coursesAPI } from '../../api/courses.js'
import { progressAPI } from '../../api/progress.js'
import { useAppStore } from '../../stores/app.js'
import { sanitizeHtml } from '../../utils/sanitize.js'
import { marked } from 'marked'
import CodeBlock from '../../components/common/CodeBlock.vue'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const chapters = ref([])
const course = ref(null)
const lesson = ref(null)
const loading = ref(true)
const forbidden = ref(false)
const dropdownOpen = ref(false)
// TASK-018：本课时服务端进度状态（in_progress/completed，''=未知）
const lessonStatus = ref('')
const progressBusy = ref(false)

// 本地视频播放：签名 URL / 加载中 / 错误态
const videoPlaybackUrl = ref('')
const videoLoading = ref(false)
const videoError = ref('')
let videoRetryCount = 0 // 媒体 error 自动重新获取签名的次数（最多 1 次）

const courseId = computed(() => route.params.id)
const lessonId = computed(() => route.params.lid)

// 扁平的课时导航数组 [{ lesson, chapterTitle, chapterIndex }]
const flatLessons = computed(() => {
  const result = []
  for (let i = 0; i < chapters.value.length; i++) {
    const ch = chapters.value[i]
    if (ch.lessons) {
      for (const l of ch.lessons) {
        result.push({ lesson: l, chapterTitle: ch.title, chapterIndex: i })
      }
    }
  }
  return result
})

const currentIndex = computed(() => {
  return flatLessons.value.findIndex(f => String(f.lesson.id) === String(lessonId.value))
})

const prevLesson = computed(() => {
  if (currentIndex.value <= 0) return null
  return flatLessons.value[currentIndex.value - 1]
})

const nextLesson = computed(() => {
  if (currentIndex.value >= flatLessons.value.length - 1) return null
  return flatLessons.value[currentIndex.value + 1]
})

const currentChapterTitle = computed(() => {
  if (currentIndex.value < 0) return ''
  return flatLessons.value[currentIndex.value]?.chapterTitle || ''
})

function findLesson() {
  for (let i = 0; i < chapters.value.length; i++) {
    const ch = chapters.value[i]
    if (ch.lessons) {
      const found = ch.lessons.find(l => String(l.id) === String(lessonId.value))
      if (found) {
        lesson.value = found
        // notebook 类型自动跳转到 NotebookView
        if (found.content_type === 'notebook') {
          router.replace(`/student/courses/${courseId.value}/notebook/${found.id}`)
        }
        loadVideoForCurrentLesson()
        return
      }
    }
  }
  lesson.value = null
}

// ── 本地视频播放 ────────────────────────────────────────────────────
function resetVideoState() {
  videoPlaybackUrl.value = ''
  videoLoading.value = false
  videoError.value = ''
  videoRetryCount = 0
}

function loadVideoForCurrentLesson() {
  const current = lesson.value
  resetVideoState()
  // 仅本地来源需要签名地址；外链直接渲染链接，不调用播放接口
  if (!current || current.content_type !== 'video' || current.video_source !== 'upload') return
  fetchVideoPlayback(current.id)
}

async function fetchVideoPlayback(id) {
  videoLoading.value = true
  videoError.value = ''
  try {
    const res = await coursesAPI.getLessonVideoPlaybackUrl(id)
    // 防竞态：期间切换了课时则丢弃过期响应
    if (lesson.value && String(lesson.value.id) === String(id)) {
      videoPlaybackUrl.value = res.data.url
    }
  } catch {
    if (lesson.value && String(lesson.value.id) === String(id)) {
      videoError.value = '视频加载失败，请重试'
    }
  } finally {
    if (lesson.value && String(lesson.value.id) === String(id)) {
      videoLoading.value = false
    }
  }
}

function onVideoMediaError() {
  // 播放过程中 URL 过期后的新 Range 请求可能失败：自动重新获取一次签名
  if (videoRetryCount >= 1) {
    videoError.value = '视频加载失败，请重试'
    return
  }
  videoRetryCount += 1
  videoPlaybackUrl.value = ''
  fetchVideoPlayback(lesson.value?.id)
}

function retryVideo() {
  // 手动重试：重置自动刷新计数
  videoRetryCount = 0
  fetchVideoPlayback(lesson.value?.id)
}

async function recordStart() {
  // TASK-018：打开课时只记录 in_progress 与最后访问时间（服务端事实）。
  // 静默失败不阻断学习；已完成的课时保持 completed（服务端保证）。
  if (!lesson.value) return
  try {
    const res = await progressAPI.start(lesson.value.id)
    lessonStatus.value = res.data.status
  } catch { /* ignore */ }
}

async function completeLesson() {
  if (!lesson.value || progressBusy.value) return
  progressBusy.value = true
  try {
    const res = await progressAPI.complete(lesson.value.id)
    lessonStatus.value = res.data.status
    app.showToast('已标记完成本课时', 'success')
  } catch (e) {
    app.showToast(e.response?.data?.detail?.message || '操作失败', 'error')
  } finally {
    progressBusy.value = false
  }
}

async function revertLesson() {
  if (!lesson.value || progressBusy.value) return
  progressBusy.value = true
  try {
    const res = await progressAPI.revert(lesson.value.id)
    lessonStatus.value = res.data.status
    app.showToast('已撤回完成标记', 'success')
  } catch (e) {
    app.showToast(e.response?.data?.detail?.message || '操作失败', 'error')
  } finally {
    progressBusy.value = false
  }
}

async function fetchData() {
  loading.value = true
  forbidden.value = false
  try {
    const [chRes, cRes] = await Promise.all([
      coursesAPI.getChapters(courseId.value),
      coursesAPI.get(courseId.value),
    ])
    const raw = chRes.data
    chapters.value = Array.isArray(raw) ? raw : (raw.items || [])
    course.value = cRes.data
    lessonStatus.value = ''
    findLesson()
    if (lesson.value) recordStart()
  } catch (e) {
    if (e.response?.status === 403) {
      // 未选课 / 已移出白名单 / 课程不可见：给出明确错误态而非仅 toast
      forbidden.value = true
    } else {
      app.showToast('加载课时失败', 'error')
    }
  } finally { loading.value = false }
}

function goLesson(lessonId) {
  dropdownOpen.value = false
  router.push(`/student/courses/${courseId.value}/lessons/${lessonId}`)
}

function goPrev() {
  if (prevLesson.value) goLesson(prevLesson.value.lesson.id)
}

function goNext() {
  if (nextLesson.value) goLesson(nextLesson.value.lesson.id)
}

function goCourse() {
  router.push(`/student/courses/${courseId.value}`)
}

// ── 内容分段：将 markdown 拆分为文字块和代码块 ──────────────────────

const contentBlocks = computed(() => {
  const src = lesson.value?.content || ''
  if (!src.trim()) return [{ type: 'markdown', content: src }]

  const blocks = []
  // 按 ``` 代码围栏拆分
  const parts = src.split(/(```[\s\S]*?```)/g)
  let headingIndex = 0

  for (const part of parts) {
    if (part.startsWith('```')) {
      const lines = part.split('\n')
      const langLine = lines[0].replace(/```/g, '').trim()
      const language = langLine || 'python'
      // 去掉首尾行（``` 标记行）
      const code = lines.slice(1, -1).join('\n')
      blocks.push({ type: 'code', code, language })
    } else if (part.trim()) {
      const html = marked.parse(part, { async: false })
      const safe = sanitizeHtml(typeof html === 'string' ? html : '')
      // 给 h2/h3 生成 id 以便 TOC 定位
      const withIds = addHeadingIds(safe, headingIndex)
      headingIndex += (safe.match(/<h[23]/g) || []).length
      blocks.push({ type: 'markdown', html: withIds })
    }
  }
  return blocks
})

function addHeadingIds(html, startIndex) {
  let idx = startIndex
  return html.replace(/<(h[23])>/g, (_, tag) => {
    return `<${tag} id="section-${idx++}">`
  })
}

// ── TOC 目录 + 滚动监听 ───────────────────────────────────────────

const tocItems = ref([])
const activeTocId = ref('')
let tocObserver = null
const tocTimerIds = new Set()
let tocDisposed = false

function scheduleTOC(delay) {
  if (tocDisposed) return
  const timerId = setTimeout(() => {
    tocTimerIds.delete(timerId)
    if (tocDisposed) return
    extractTOC()
  }, delay)
  tocTimerIds.add(timerId)
}

function extractTOC() {
  const items = []
  const containers = document.querySelectorAll('.lesson-content')
  containers.forEach(container => {
    const headings = container.querySelectorAll('h2, h3')
    headings.forEach(h => {
      const id = h.getAttribute('id') || h.textContent?.replace(/\s+/g, '-').toLowerCase()
      if (!h.id) h.id = id
      items.push({
        id,
        text: h.textContent?.trim() || '',
        level: h.tagName === 'H2' ? 2 : 3,
      })
    })
  })
  tocItems.value = items
  // 重建 IntersectionObserver
  setupTocObserver()
}

function setupTocObserver() {
  if (tocObserver) tocObserver.disconnect()
  const headingEls = document.querySelectorAll('.lesson-content h2[id], .lesson-content h3[id]')
  if (headingEls.length === 0) return
  tocObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          activeTocId.value = entry.target.id
        }
      }
    },
    { rootMargin: '-80px 0px -60% 0px' }
  )
  headingEls.forEach(el => tocObserver.observe(el))
}

function scrollToHeading(id) {
  activeTocId.value = id
  const el = document.getElementById(id)
  if (el) {
    const topbar = document.querySelector('.lesson-topbar')
    const offset = (topbar?.offsetHeight || 48) + 12
    const top = el.getBoundingClientRect().top + window.pageYOffset - offset
    window.scrollTo({ top, behavior: 'smooth' })
  }
}

function toggleDropdown() {
  dropdownOpen.value = !dropdownOpen.value
}

// 点击外部关闭下拉
function onDocClick(e) {
  const el = document.getElementById('chapter-dropdown')
  if (el && !el.contains(e.target)) dropdownOpen.value = false
}

onMounted(async () => {
  await fetchData()
  document.addEventListener('click', onDocClick)
})

// TOC 在 DOM 更新后提取
onMounted(() => {
  nextTick(() => {
    scheduleTOC(200)
  })
})

// 监听内容变化重新提取 TOC
watch(() => lesson.value?.content, () => {
  nextTick(() => {
    scheduleTOC(300)
  })
})

onBeforeUnmount(() => {
  tocDisposed = true
  document.removeEventListener('click', onDocClick)
  for (const timerId of tocTimerIds) clearTimeout(timerId)
  tocTimerIds.clear()
  tocObserver?.disconnect()
  tocObserver = null
})

// 路由参数变化时重新查找
watch([lessonId, courseId], async ([, newCid], [, oldCid]) => {
  if (newCid !== oldCid || chapters.value.length === 0) {
    await fetchData()
  } else {
    lessonStatus.value = ''
    findLesson()
    if (lesson.value) recordStart()
  }
})
</script>

<template>
  <AppLayout content-mode="reading">
    <!-- Loading -->
    <div v-if="loading" class="lesson-loading">
      <div class="skeleton" style="height:22px;width:360px;margin-bottom:16px"></div>
      <div class="skeleton" style="height:14px;width:100%;margin-bottom:8px" v-for="i in 8" :key="i"></div>
    </div>

    <!-- 无权访问或课程不可见 -->
    <div v-else-if="forbidden" class="empty-state">
      <p>无权访问或课程不可见</p>
      <button class="btn-primary" @click="goCourse" style="margin-top:12px">返回课程详情</button>
    </div>

    <!-- Not found -->
    <div v-else-if="!lesson" class="empty-state">
      <p>课时不存在</p>
      <button class="btn-primary" @click="goCourse" style="margin-top:12px">返回课程</button>
    </div>

    <!-- Lesson content -->
    <div v-else class="lesson-layout">
      <!-- ── Sticky Top Nav Bar ────────────────────────────────────────── -->
      <div class="lesson-topbar">
        <div class="topbar-inner">
          <button class="topbar-back" @click="goCourse">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3L5 8l5 5"/></svg>
            返回课程
          </button>
          <div class="topbar-breadcrumb">
            <template v-if="currentChapterTitle">
              <span class="topbar-crumb">{{ currentChapterTitle }}</span>
              <span class="topbar-sep">›</span>
            </template>
            <span class="topbar-crumb current">{{ lesson.title }}</span>
          </div>

          <!-- Chapter dropdown -->
          <div class="dropdown-wrap" id="chapter-dropdown">
            <button class="btn-ghost btn-sm dropdown-trigger" @click.stop="toggleDropdown">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="2" y="2" width="10" height="10" rx="1"/><path d="M5 6h4M5 9h2"/></svg>
              快速跳转
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M2 3.5l3 3 3-3"/></svg>
            </button>
            <transition name="dropdown-fade">
              <div v-if="dropdownOpen" class="dropdown-menu">
                <div v-for="ch in chapters" :key="ch.id" class="dropdown-group">
                  <div class="dropdown-chapter">第{{ ch.order_index + 1 }}章  {{ ch.title }}</div>
                  <div
                    v-for="l in ch.lessons" :key="l.id"
                    class="dropdown-item"
                    :class="{ active: l.id === lesson.id }"
                    @click.stop="goLesson(l.id)"
                  >
                    <span class="dropdown-item-icon">
                      <template v-if="l.content_type === 'markdown'">📖</template>
                      <template v-else-if="l.content_type === 'video'">🎥</template>
                      <template v-else>📓</template>
                    </span>
                    {{ l.title }}
                  </div>
                </div>
              </div>
            </transition>
          </div>
        </div>
      </div>

      <!-- ── Body row: 正文 + TOC 双栏 ────────────────────────────────── -->
      <div class="lesson-row">
        <!-- 正文列 -->
        <div class="lesson-body">
          <!-- Meta info -->
          <div class="lesson-meta-row">
            <span class="meta-tag">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="7"/><path d="M8 4v4l3 2"/></svg>
              预计 12 分钟
            </span>
            <span class="meta-dot">·</span>
            <span class="meta-tag">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M2 4h12M2 8h12M2 12h12"/></svg>
              4 个知识点
            </span>
            <span class="meta-dot">·</span>
            <span class="meta-tag">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="2" width="12" height="12" rx="2"/><path d="M5.5 6.5l2 2 3-4"/></svg>
              2 个练习
            </span>
          </div>

          <!-- Content blocks -->
          <template v-if="lesson.content_type === 'markdown'">
            <template v-for="(block, bi) in contentBlocks" :key="bi">
              <div v-if="block.type === 'markdown'" class="lesson-content" v-html="block.html"></div>
              <CodeBlock v-else :code="block.code" :language="block.language" :filename="'example.' + (block.language === 'python' ? 'py' : block.language)" />
            </template>
          </template>

          <div v-else-if="lesson.content_type === 'video'" class="lesson-video">
            <!-- 本地上传：内嵌播放器 -->
            <template v-if="lesson.video_source === 'upload'">
              <div class="video-wrapper">
                <p v-if="videoLoading" class="video-state">视频加载中…</p>
                <p v-else-if="videoError" class="video-state video-error">
                  {{ videoError }}
                  <button class="btn-ghost btn-sm" type="button" @click="retryVideo">重新加载视频</button>
                </p>
                <video
                  v-else-if="videoPlaybackUrl"
                  controls
                  playsinline
                  preload="metadata"
                  class="lesson-video-player"
                  :src="videoPlaybackUrl"
                  @error="onVideoMediaError"
                ></video>
                <p v-else class="video-state">视频暂不可用</p>
              </div>
            </template>
            <!-- 外链：打开视频按钮 -->
            <template v-else>
              <div v-if="lesson.video_url" class="video-wrapper">
                <div class="video-placeholder">
                  <svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1" opacity="0.3"><circle cx="24" cy="24" r="20"/><path d="M20 16v16l12-8z"/></svg>
                  <p class="text-sm text-secondary" style="margin-top:12px">视频课时：{{ lesson.title }}</p>
                  <a :href="lesson.video_url" target="_blank" rel="noopener noreferrer" class="btn-primary" style="margin-top:12px;display:inline-flex;align-items:center;gap:6px">打开视频</a>
                </div>
              </div>
              <div v-else class="empty-state"><p>视频暂不可用</p></div>
            </template>
          </div>

          <div v-else class="lesson-notebook">
            <p>Notebook 课时：{{ lesson.title }}</p>
          </div>

          <!-- Bottom nav -->
          <hr class="lesson-divider" />
          <div class="lesson-nav">
            <button v-if="prevLesson" class="nav-btn nav-prev" @click="goPrev" :title="prevLesson.lesson.title">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M9 3L4 7l5 5"/></svg>
              <span class="nav-label">上一课</span>
              <span class="nav-title">{{ prevLesson.lesson.title }}</span>
            </button>
            <div v-else class="nav-btn nav-prev disabled">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M9 3L4 7l5 5"/></svg>
              <span class="nav-label">上一课</span>
            </div>

            <!-- TASK-018：显式完成/撤回——打开课时只记录 in_progress，不自动完成 -->
            <button
              type="button"
              class="nav-btn nav-complete"
              :class="{ 'is-completed': lessonStatus === 'completed' }"
              :disabled="progressBusy || !lessonStatus"
              :aria-label="lessonStatus === 'completed' ? '撤回完成标记' : '标记完成本课时'"
              @click="lessonStatus === 'completed' ? revertLesson() : completeLesson()"
            >
              <svg v-if="lessonStatus === 'completed'" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="7"/><path d="M5.5 8.5l2 2 3-4"/></svg>
              <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="7"/></svg>
              <span class="nav-label">{{ lessonStatus === 'completed' ? '已学完 · 点击撤回' : '完成本课时' }}</span>
            </button>

            <button v-if="nextLesson" class="nav-btn nav-next" @click="goNext" :title="nextLesson.lesson.title">
              <span class="nav-title">{{ nextLesson.lesson.title }}</span>
              <span class="nav-label">下一课</span>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M5 3l5 4-5 5"/></svg>
            </button>
            <div v-else class="nav-btn nav-next disabled">
              <span class="nav-label">下一课</span>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M5 3l5 4-5 5"/></svg>
            </div>
          </div>
        </div>

        <!-- TOC 目录列 -->
        <aside class="lesson-toc">
          <h4 class="toc-title">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="3" cy="3" r="1.5"/><circle cx="3" cy="8" r="1.5"/><circle cx="3" cy="13" r="1.5"/><path d="M7 3h7M7 8h7M7 13h7"/></svg>
            目录
          </h4>
          <nav class="toc-nav">
            <a
              v-for="item in tocItems" :key="item.id"
              :href="'#' + item.id"
              class="toc-link"
              :class="{ 'toc-sub': item.level === 3, 'toc-active': item.id === activeTocId }"
              @click.prevent="scrollToHeading(item.id)"
            >{{ item.text }}</a>
          </nav>
          <p v-if="tocItems.length === 0" class="toc-empty">暂无目录</p>
        </aside>
      </div>
    </div>
  </AppLayout>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════════
   Lesson View — 分段渲染 + TOC + 阅读排版
   CodeBlock 组件替代 :deep(pre) 代码块样式
   ═══════════════════════════════════════════════════════════════════════ */

/* ── Layout ─────────────────────────────────────────────────────────── */
.lesson-layout { }
.lesson-loading { padding: var(--space-4) 0; }

/* ── Sticky Top Bar（全宽，跨 flex 行上方） ──────────────────────────── */
.lesson-topbar {
  position: sticky; top: var(--header-height); z-index: 20;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  margin: 0 0 var(--space-4);
  padding: 0 var(--space-4);
}
.topbar-inner {
  display: flex; align-items: center; gap: 16px;
  height: 48px; max-width: 1080px; margin: 0 auto;
}

/* ── Body row：正文 + TOC 双栏 ────────────────────────────────────── */
.lesson-row {
  display: flex; justify-content: center; gap: 48px;
  max-width: 1080px; margin: 0 auto;
  padding-top: 4px;
}

/* 正文列 */
.lesson-body {
  flex: 1; min-width: 0; max-width: 780px;
}

/* TOC 目录列 */
.lesson-toc {
  width: 200px; flex-shrink: 0;
  position: sticky; top: 68px; align-self: flex-start;
  max-height: calc(100vh - 100px); overflow-y: auto;
  padding: 8px 0 32px;
}
@media (max-width: 1100px) {
  .lesson-row { gap: 24px; }
  .lesson-toc { width: 160px; }
}
@media (max-width: 900px) {
  .lesson-toc { display: none; }
  .lesson-body { max-width: 100%; }
  .topbar-inner { max-width: 100%; }
}
.topbar-back {
  display: inline-flex; align-items: center; gap: 4px;
  background: none; border: none; padding: 4px 8px;
  color: var(--muted); font-size: var(--text-sm); font-weight: 500;
  cursor: pointer; border-radius: var(--radius-sm);
  transition: color var(--duration-fast) var(--ease-out);
  white-space: nowrap; flex-shrink: 0;
}
.topbar-back:hover { color: var(--accent); }
.topbar-breadcrumb {
  display: flex; align-items: center; gap: 6px;
  min-width: 0; flex: 1;
}
.topbar-crumb {
  font-size: var(--text-sm); color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.topbar-crumb.current { color: var(--fg); font-weight: 500; }
.topbar-sep { color: var(--faint); font-size: var(--text-sm); }

/* ── Dropdown ──────────────────────────────────────────────────────── */
.dropdown-wrap { position: relative; flex-shrink: 0; }
.dropdown-trigger {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: var(--text-xs); white-space: nowrap;
}
.dropdown-menu {
  position: absolute; right: 0; top: 100%; z-index: 50;
  margin-top: 4px; min-width: 280px; max-height: 420px; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-lg);
  padding: var(--space-2);
}
.dropdown-group { margin-bottom: var(--space-1); }
.dropdown-chapter {
  font-size: var(--text-xs); font-weight: 600; color: var(--muted);
  padding: 6px 10px 4px; text-transform: uppercase; letter-spacing: 0.04em;
}
.dropdown-item {
  display: flex; align-items: center; gap: var(--space-2);
  padding: 7px 10px; border-radius: var(--radius-md);
  font-size: var(--text-sm); color: var(--fg); cursor: pointer;
  transition: background var(--duration-fast) var(--ease-out);
}
.dropdown-item:hover { background: var(--surface-subtle); }
.dropdown-item.active { background: var(--accent-soft); color: var(--accent); font-weight: 500; }
.dropdown-item-icon { font-size: 12px; flex-shrink: 0; }

.dropdown-fade-enter-active,
.dropdown-fade-leave-active { transition: all var(--duration-fast) var(--ease-out); }
.dropdown-fade-enter-from,
.dropdown-fade-leave-to { opacity: 0; transform: translateY(-4px); }

/* ── Meta Info Row ──────────────────────────────────────────────────── */
.lesson-meta-row {
  display: flex; align-items: center; gap: 8px;
  padding: 20px 0 12px;
}
.meta-tag {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: var(--text-xs); font-weight: 500;
  color: var(--accent); background: var(--accent-soft);
  padding: 4px 10px; border-radius: var(--radius-full);
}
.meta-dot { color: var(--faint); font-size: var(--text-xs); }

/* ── Markdown Content Block ─────────────────────────────────────────── */
.lesson-content {
  max-width: 780px; margin: 0 auto;
  font-size: 16px; line-height: 1.85; color: oklch(0.32 0.02 155);
}

.lesson-content :deep(h1) {
  font-family: var(--font-display);
  font-size: 32px; font-weight: 700;
  color: var(--fg); margin: 0 0 20px;
  letter-spacing: -0.025em; line-height: 1.2;
}

.lesson-content :deep(h2) {
  font-family: var(--font-display);
  font-size: 24px; font-weight: 600;
  color: var(--fg); margin: 36px 0 12px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
  letter-spacing: -0.02em; line-height: 1.3;
}
.lesson-content :deep(h2:first-of-type) {
  border-top: none; padding-top: 0; margin-top: 8px;
}

.lesson-content :deep(h3) {
  font-size: 19px; font-weight: 600;
  color: var(--fg); margin: 24px 0 8px;
  line-height: 1.4;
}

.lesson-content :deep(p) {
  margin: 12px 0; color: oklch(0.32 0.02 155); font-size: 16px; line-height: 1.85;
}

.lesson-content :deep(a) {
  color: var(--accent); text-decoration: none;
}
.lesson-content :deep(a:hover) { color: var(--accent-hover); text-decoration: underline; }

.lesson-content :deep(code:not(pre code)) {
  font-family: var(--font-mono); font-size: 0.88em;
  background: var(--surface-subtle); color: var(--accent);
  padding: 2px 6px; border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}

.lesson-content :deep(table) {
  width: 100%; border-collapse: collapse; margin: 16px 0;
  font-size: var(--text-sm);
}
.lesson-content :deep(th) {
  text-align: left; padding: 10px 14px; border-bottom: 2px solid var(--border);
  font-size: var(--text-xs); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--muted); background: var(--surface-subtle);
}
.lesson-content :deep(td) {
  padding: 10px 14px; border-bottom: 1px solid var(--border); color: oklch(0.32 0.02 155);
}

.lesson-content :deep(blockquote) {
  background: var(--warning-bg); border-left: 3px solid var(--warning);
  padding: var(--space-3) var(--space-4); margin: 16px 0;
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 15px; color: var(--warning); line-height: 1.75;
}
.lesson-content :deep(blockquote p) { margin: 4px 0; color: inherit; }

.lesson-content :deep(img) {
  max-width: 100%; border-radius: var(--radius-md); margin: var(--space-3) 0;
}

.lesson-content :deep(ul), .lesson-content :deep(ol) {
  margin: 10px 0; padding-left: var(--space-6);
  font-size: 16px; color: oklch(0.32 0.02 155); line-height: 1.85;
}
.lesson-content :deep(li) { margin: 4px 0; }

.lesson-content :deep(strong) { color: var(--fg); font-weight: 600; }

/* ── Video / Notebook ──────────────────────────────────────────────── */
.lesson-video { margin: var(--space-6) 0; }
.video-wrapper {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-lg); overflow: hidden;
}
.video-placeholder {
  text-align: center; padding: var(--space-12) var(--space-6);
  display: flex; flex-direction: column; align-items: center;
}
/* 本地视频播放器：宽度 100% 跟随正文列，16:9 比例、黑色背景、圆角一致 */
.lesson-video-player {
  display: block;
  width: 100%;
  max-width: 780px;
  aspect-ratio: 16 / 9;
  border-radius: var(--radius-lg);
  background: var(--fg);
}
.video-state {
  margin: 0;
  padding: var(--space-10) var(--space-4);
  text-align: center;
  color: var(--muted);
  font-size: var(--text-sm);
}
.video-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
}
.lesson-notebook { max-width: 780px; margin: var(--space-6) auto; }

/* ── Divider ────────────────────────────────────────────────────────── */
.lesson-divider {
  border: none; border-top: 1px solid var(--border);
  margin: 40px 0 28px;
}

/* ── Bottom Navigation ──────────────────────────────────────────────── */
.lesson-nav {
  display: flex; justify-content: space-between; align-items: stretch;
  gap: var(--space-4); margin-bottom: 40px;
}
.nav-btn {
  display: flex; align-items: center; gap: var(--space-3);
  padding: var(--space-4); border: 1px solid var(--border);
  border-radius: var(--radius-md); background: var(--surface);
  cursor: pointer; transition: all var(--duration-fast) var(--ease-out);
  flex: 1; max-width: 48%; color: var(--fg); text-align: left;
}
.nav-complete {
  flex: 0 0 auto; max-width: none; justify-content: center;
  white-space: nowrap;
}
.nav-complete.is-completed {
  color: var(--success); border-color: var(--success-bg); background: var(--success-bg);
}
.nav-complete:disabled { opacity: .6; cursor: default; }
.nav-btn:hover {
  border-color: var(--border-strong); background: var(--surface-subtle);
  box-shadow: var(--shadow-md);
}
.nav-btn.disabled { opacity: 0.35; cursor: not-allowed; pointer-events: none; }
.nav-prev { justify-content: flex-start; }
.nav-next { justify-content: flex-end; }
.nav-label {
  font-size: var(--text-xs); color: var(--muted);
  font-weight: 500; white-space: nowrap;
}
.nav-title {
  font-size: var(--text-sm); font-weight: 500;
  color: var(--fg); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}

/* ═══════════════════════════════════════════════════════════════════════
   TOC — flex 列内 sticky 目录，带层级缩进 + 当前位置激活态
   ═══════════════════════════════════════════════════════════════════════ */
.toc-title {
  display: flex; align-items: center; gap: 6px;
  font-size: 11px; font-weight: 600;
  color: var(--faint); text-transform: uppercase;
  letter-spacing: 0.06em; margin: 0 0 12px;
}
.toc-nav {
  display: flex; flex-direction: column; gap: 1px;
  border-left: 1px solid var(--border);
}
.toc-empty {
  font-size: var(--text-xs); color: var(--faint); margin: 0;
}
.toc-link {
  display: block; padding: 5px 12px;
  font-size: 12px; color: var(--muted);
  text-decoration: none; border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  transition: all var(--duration-fast) var(--ease-out);
  line-height: 1.45; position: relative;
  border-left: 2px solid transparent; margin-left: -1px;
}
.toc-link:hover {
  color: var(--accent); background: var(--accent-soft);
}
.toc-link.toc-sub {
  padding-left: 22px; font-size: 11px;
}
.toc-link.toc-active {
  color: var(--accent); font-weight: 500;
  border-left-color: var(--accent);
  background: var(--accent-soft);
}

/* ── Empty state ────────────────────────────────────────────────────── */
.empty-state {
  text-align: center; padding: var(--space-12) var(--space-6);
  color: var(--muted);
}
.empty-state p { font-size: var(--text-sm); margin-bottom: var(--space-3); }

/* ── Responsive ─────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  .lesson-topbar {
    margin: -20px -16px 0; padding: 0 16px;
  }
  .lesson-content { font-size: 15px; }
  .lesson-content :deep(h1) { font-size: 26px; }
  .lesson-content :deep(h2) { font-size: 20px; }
  .lesson-content :deep(h3) { font-size: 17px; }
  .lesson-nav { flex-direction: column; }
  .nav-btn { max-width: 100%; }
  .nav-title { display: none; }
  .dropdown-menu { left: 0; right: auto; min-width: auto; width: 100%; }
}
</style>
