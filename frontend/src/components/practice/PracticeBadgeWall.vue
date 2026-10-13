<script setup>
// 徽章墙（批次 6）：四个徽章（首题/10 题/50 题/连续 7 天）。
// 未获得时置灰并显示达成条件与进度；已获得显示获得时间。
import AppIcon from '../ui/AppIcon.vue'
import { formatDateTime } from '../../utils/format.js'

defineProps({
  badges: { type: Array, default: () => [] },
})

function stateText(badge) {
  if (badge.earned) {
    return badge.awarded_at ? `已获得 · ${formatDateTime(badge.awarded_at)}` : '已获得'
  }
  return `进度 ${badge.progress ?? 0}/${badge.threshold ?? 1}`
}
</script>

<template>
  <section class="badge-wall" aria-label="练习徽章墙">
    <header class="section-head">
      <h2 class="section-title">徽章墙</h2>
      <p class="section-sub">练习成绩、排名与徽章都不进入课程总评</p>
    </header>
    <ul v-if="badges.length" class="badges">
      <li
        v-for="badge in badges"
        :key="badge.key"
        class="badge"
        :class="{ earned: badge.earned }"
      >
        <span class="badge-icon" aria-hidden="true">
          <AppIcon :name="badge.earned ? 'trophy' : 'lock'" :size="18" />
        </span>
        <div class="badge-body">
          <p class="badge-label">{{ badge.label }}</p>
          <p class="badge-desc">{{ badge.description }}</p>
          <p class="badge-state">{{ stateText(badge) }}</p>
        </div>
      </li>
    </ul>
    <p v-else class="empty">还没有徽章数据，先完成一道练习题吧。</p>
  </section>
</template>

<style scoped>
.badge-wall { display: flex; flex-direction: column; gap: var(--space-3); }
.section-head { display: flex; flex-direction: column; gap: var(--space-1); }
.section-title { margin: 0; font-size: var(--text-lg); }
.section-sub { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.badges { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--space-3); margin: 0; padding: 0; list-style: none; }
.badge { display: flex; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.badge.earned { border-color: var(--accent); background: var(--accent-soft); }
.badge-icon { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: var(--radius-md); background: var(--surface-2, var(--surface)); color: var(--muted); }
.badge.earned .badge-icon { color: var(--accent); }
.badge-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.badge-label { margin: 0; font-weight: 600; }
.badge-desc { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.badge-state { margin: 0; color: var(--faint); font-size: var(--text-xs); }
.empty { margin: 0; color: var(--muted); font-size: var(--text-sm); }
</style>
