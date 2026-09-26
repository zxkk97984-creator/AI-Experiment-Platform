<script setup>
// TeacherMetricGrid：V2 指标条（映射 .metric-strip / .metric）。
// 单条边框分隔，不再渲染独立 KPI 卡片与图标大色块。
import AppIcon from '../ui/AppIcon.vue'

defineProps({
  items: { type: Array, required: true },
  ariaLabel: { type: String, required: true },
})
</script>

<template>
  <section class="metric-strip teacher-metric-grid" :class="`columns-${items.length}`" :aria-label="ariaLabel">
    <article
      v-for="item in items"
      :key="item.key"
      class="metric teacher-metric-card"
      :class="{ em: item.emphasis && item.tone === 'blue', warn: item.tone === 'orange' && item.emphasis }"
    >
      <span class="teacher-metric-copy">
        <span class="m-value teacher-metric-value">
          <strong :class="{ emphasized: item.emphasis }">{{ item.value }}</strong>
          <em v-if="item.unit">{{ item.unit }}</em>
        </span>
        <span class="m-label">{{ item.label }}</span>
      </span>
      <span class="teacher-metric-icon" :class="item.tone || 'blue'" aria-hidden="true">
        <AppIcon :name="item.icon" :size="14" />
      </span>
    </article>
  </section>
</template>

<style scoped>
/* 指标条视觉来自全局 .metric-strip/.metric；图标仅作弱化辅助标记。 */
.teacher-metric-grid { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.teacher-metric-card { display: flex; align-items: center; justify-content: space-between; flex-direction: row; gap: var(--space-3); }
.teacher-metric-copy { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.teacher-metric-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  color: var(--faint);
  background: var(--surface-subtle);
}
.teacher-metric-icon.blue { color: var(--accent); background: var(--accent-soft); }
.teacher-metric-icon.green { color: var(--success); background: var(--success-bg); }
.teacher-metric-icon.orange { color: var(--warning); background: var(--warning-bg); }
.teacher-metric-icon.purple { color: var(--info); background: var(--info-bg); }
.teacher-metric-value strong { font-size: inherit; font-weight: 600; }
.teacher-metric-value em { margin-left: 6px; color: var(--faint); font-size: var(--text-sm); font-style: normal; }

@media (max-width: 1024px) {
  .teacher-metric-grid.columns-3 { grid-template-columns: repeat(3, 1fr); }
  .teacher-metric-grid.columns-3 .teacher-metric-card:last-child { grid-column: auto; }
}
@media (max-width: 560px) {
  .teacher-metric-grid, .teacher-metric-grid.columns-3 { grid-template-columns: 1fr; }
}
</style>
