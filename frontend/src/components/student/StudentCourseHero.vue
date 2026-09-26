<script setup>
// StudentCourseHero：课程概览英雄卡。
// 面包屑 + 课程封面（或占位）+ 课程身份 + 低对比元数据 chips + 进度 + CTA。

import { computed, ref, watch } from 'vue'
import ClassSummary from '../common/ClassSummary.vue'
import AppIcon from '../ui/AppIcon.vue'
import UiProgress from '../ui/UiProgress.vue'
import { getCourseCoverUrl } from '../../utils/courseCover.js'
import { clampProgress } from '../../utils/studentUi.js'

const props = defineProps({
  course: { type: Object, required: true },
  progress: { type: Number, default: 0 },
  totalLessons: { type: Number, default: 0 },
  completedLessons: { type: Number, default: 0 },
  totalChapters: { type: Number, default: 0 },
  enrolled: { type: Boolean, default: false },
  enrolling: { type: Boolean, default: false },
})

defineEmits(['continue', 'enroll', 'back'])

const safeProgress = computed(() => clampProgress(props.progress))

const coverUrl = computed(() => getCourseCoverUrl(props.course))
const coverFailed = ref(false)
// 封面变化（替换/移除）时重置加载失败状态，新 URL 重新尝试
watch(() => props.course?.cover, () => { coverFailed.value = false })
</script>

<template>
  <section class="course-hero">
    <div class="hero-breadcrumb">
      <button type="button" class="breadcrumb-link" @click="$emit('back')">我的课程</button>
      <AppIcon name="chevron-right" :size="14" />
      <span class="breadcrumb-current">{{ course.title }}</span>
    </div>

    <div class="hero-body">
      <!-- 封面：无封面或加载失败时显示纯色占位块，不循环重试损坏 URL -->
      <div class="hero-cover">
        <img
          v-if="coverUrl && !coverFailed"
          class="hero-cover__img"
          :src="coverUrl"
          :alt="`${course.title}课程封面`"
          @error="coverFailed = true"
        />
        <AppIcon v-else name="image" :size="28" />
      </div>

      <div class="hero-identity">
        <h1 class="hero-title">{{ course.title }}</h1>
        <p class="hero-desc" v-if="course.description">{{ course.description }}</p>
      </div>

      <div class="hero-meta">
        <span class="hero-chip">{{ course.academic_term?.name || '未设置学期' }}</span>
        <ClassSummary :classes="course.teaching_classes || []" />
        <span class="hero-chip">{{ totalChapters }} 章节</span>
        <span class="hero-chip">{{ totalLessons }} 课时</span>
        <div class="hero-progress">
          <span class="hero-progress-text">
            已学 {{ safeProgress }}% · {{ completedLessons }}/{{ totalLessons }} 课时
          </span>
          <UiProgress :value="safeProgress" />
        </div>
      </div>

      <div class="hero-cta">
        <button
          v-if="enrolled"
          type="button"
          class="btn-primary hero-continue-btn"
          @click="$emit('continue')"
        >
          继续学习
        </button>
        <button
          v-else
          type="button"
          class="btn-primary hero-enroll-btn"
          :disabled="enrolling"
          @click="$emit('enroll')"
        >
          {{ enrolling ? '选课中…' : '立即选课' }}
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.course-hero {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 150px;
  padding: 18px 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}

/* ── 面包屑 ─────────────────────────────────────────────────────── */
.hero-breadcrumb {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--faint);
}
.breadcrumb-link {
  background: none;
  border: none;
  padding: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  cursor: pointer;
}
.breadcrumb-link:hover { color: var(--accent); }
.breadcrumb-current {
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 主体 ───────────────────────────────────────────────────────── */
.hero-body {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 24px;
  min-width: 0;
}

/* 与课程列表统一为正方形；flex-shrink: 0 保证不挤压进度与 CTA */
.hero-cover {
  flex-shrink: 0;
  width: 120px;
  aspect-ratio: 1 / 1;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--surface-raised, var(--surface-subtle));
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--faint);
}
.hero-cover__img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.hero-identity {
  flex: 1.2;
  min-width: 0;
}
.hero-title {
  margin: 0 0 4px;
  font-family: var(--font-display);
  font-size: var(--text-2xl);
  font-weight: 600;
  color: var(--fg);
  letter-spacing: -0.01em;
  line-height: var(--lh-tight);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.hero-desc {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.hero-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
/* 低对比边框控件，非重填充徽章 */
.hero-chip {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--muted);
  font-size: var(--text-xs);
  font-weight: 500;
  white-space: nowrap;
}
.hero-progress {
  flex: 1 1 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 160px;
}
.hero-progress-text {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--muted);
}

.hero-cta {
  flex-shrink: 0;
}
.hero-cta .btn-primary {
  height: 44px;
  padding: 0 26px;
  border-radius: var(--radius-md);
  font-weight: 600;
}

@media (max-width: 767.98px) {
  .course-hero { padding: 16px; }
  .hero-body { flex-direction: column; align-items: stretch; gap: 14px; }
  /* 移动端封面全宽，仍保持正方形区域 */
  .hero-cover { width: 100%; }
  .hero-cta { align-self: stretch; }
  .hero-cta .btn-primary { width: 100%; }
}

.hero-body { grid-template-columns: 88px minmax(0, 1fr) minmax(240px, .8fr) auto; gap: 16px; }
.hero-cover { width: 88px; height: 88px; }
.hero-identity { min-width: 0; }
.hero-title { font-size: var(--text-3xl); white-space: normal; overflow-wrap: anywhere; }
.hero-meta { min-width: 0; }
</style>
