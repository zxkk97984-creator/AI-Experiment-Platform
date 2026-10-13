<script setup>
// 打卡日历（批次 6）：近 N 天里「有新增通关」的日期高亮。
// 同一天多次通关只算一天；连续天数由后端按同一口径计算，前端只展示。
import { computed } from 'vue'

const props = defineProps({
  activeDates: { type: Array, default: () => [] },
  days: { type: Number, default: 30 },
  streakDays: { type: Number, default: 0 },
  today: { type: String, default: '' },
})

const cells = computed(() => {
  const anchor = props.today ? new Date(`${props.today}T00:00:00Z`) : new Date()
  const active = new Set(props.activeDates)
  const list = []
  for (let offset = props.days - 1; offset >= 0; offset -= 1) {
    const day = new Date(anchor.getTime() - offset * 86400000)
    const key = day.toISOString().slice(0, 10)
    list.push({ key, label: key.slice(5), active: active.has(key) })
  }
  return list
})

const activeCount = computed(() => cells.value.filter((cell) => cell.active).length)
</script>

<template>
  <section class="checkin" aria-label="打卡日历">
    <header class="section-head">
      <h2 class="section-title">打卡日历</h2>
      <p class="section-sub">
        近 {{ days }} 天打卡 {{ activeCount }} 天 · 连续 {{ streakDays }} 天（每日首次新增通关记 1 天）
      </p>
    </header>
    <ul class="grid">
      <li
        v-for="cell in cells"
        :key="cell.key"
        class="day"
        :class="{ active: cell.active }"
        :aria-label="`${cell.key} ${cell.active ? '已打卡' : '未打卡'}`"
        :title="`${cell.key} ${cell.active ? '已打卡' : '未打卡'}`"
      >{{ cell.label }}</li>
    </ul>
  </section>
</template>

<style scoped>
.checkin { display: flex; flex-direction: column; gap: var(--space-3); }
.section-head { display: flex; flex-direction: column; gap: var(--space-1); }
.section-title { margin: 0; font-size: var(--text-lg); }
.section-sub { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.grid { display: grid; grid-template-columns: repeat(10, minmax(0, 1fr)); gap: var(--space-1); margin: 0; padding: 0; list-style: none; }
.day { display: flex; align-items: center; justify-content: center; padding: var(--space-1) 0; border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--faint); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
.day.active { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); font-weight: 600; }
@media (max-width: 640px) {
  .grid { grid-template-columns: repeat(6, minmax(0, 1fr)); }
}
</style>
