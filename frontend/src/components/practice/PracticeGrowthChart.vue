<script setup>
// 成长曲线（批次 6）：近 30 天「新增通过题数」与「当天平均分」两条折线。
// 窄屏（≤640px）降级为列表，避免横向溢出；颜色取设计系统 --viz-* 色板。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  points: { type: Array, default: () => [] },
  /** null = 按视口自适应；测试可显式传 true/false 强制列表/折线 */
  narrow: { type: Boolean, default: null },
})

const WIDTH = 300
const HEIGHT = 120
const autoNarrow = ref(false)
let media = null

function syncNarrow(event) {
  autoNarrow.value = event.matches
}

onMounted(() => {
  if (typeof window === 'undefined' || !window.matchMedia) return
  media = window.matchMedia('(max-width: 640px)')
  autoNarrow.value = media.matches
  media.addEventListener?.('change', syncNarrow)
})

onBeforeUnmount(() => {
  media?.removeEventListener?.('change', syncNarrow)
})

const isNarrow = computed(() => (props.narrow === null ? autoNarrow.value : props.narrow))
const hasAvg = computed(() => props.points.some(
  (point) => point.avg_score !== null && point.avg_score !== undefined,
))
const maxPassed = computed(() => Math.max(
  1, ...props.points.map((point) => Number(point.passed_count) || 0),
))

function polyline(values, max) {
  const step = props.points.length > 1 ? WIDTH / (props.points.length - 1) : 0
  return values
    .map((value, index) => {
      const ratio = Math.min(1, Math.max(0, Number(value || 0) / max))
      return `${(index * step).toFixed(2)},${(HEIGHT - ratio * HEIGHT).toFixed(2)}`
    })
    .join(' ')
}

const passedLine = computed(() => polyline(
  props.points.map((point) => point.passed_count), maxPassed.value))
const avgLine = computed(() => polyline(
  props.points.map((point) => point.avg_score ?? 0), 100))
</script>

<template>
  <section class="growth" aria-label="近 30 天成长曲线">
    <header class="section-head">
      <h2 class="section-title">成长曲线</h2>
      <p class="section-sub">近 30 天：新增通过题数（<span class="swatch passed" aria-hidden="true"></span>）
        与当天平均分（<span class="swatch avg" aria-hidden="true"></span>）</p>
    </header>

    <p v-if="!points.length" class="empty">近 30 天还没有练习记录。</p>

    <!-- 窄屏降级：逐日列表，不做横向滚动 -->
    <ul v-else-if="isNarrow" class="growth-list">
      <li v-for="point in points" :key="point.date" class="growth-row">
        <span class="date">{{ point.date }}</span>
        <span>通过 {{ point.passed_count ?? 0 }} 题</span>
        <span>平均分 {{ point.avg_score === null || point.avg_score === undefined ? '—' : point.avg_score }}</span>
      </li>
    </ul>

    <div v-else class="chart">
      <svg
        :viewBox="`0 0 ${WIDTH} ${HEIGHT}`"
        preserveAspectRatio="none"
        role="img"
        aria-label="近 30 天新增通过题数与平均分折线图"
      >
        <polyline
          class="line passed"
          :points="passedLine"
          vector-effect="non-scaling-stroke"
        />
        <polyline
          v-if="hasAvg"
          class="line avg"
          :points="avgLine"
          vector-effect="non-scaling-stroke"
        />
      </svg>
      <div class="axis">
        <span>{{ points[0]?.date }}</span>
        <span>{{ points[points.length - 1]?.date }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.growth { display: flex; flex-direction: column; gap: var(--space-3); }
.section-head { display: flex; flex-direction: column; gap: var(--space-1); }
.section-title { margin: 0; font-size: var(--text-lg); }
.section-sub { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.swatch { display: inline-block; width: 10px; height: 3px; border-radius: 2px; vertical-align: middle; }
.swatch.passed { background: var(--viz-1); }
.swatch.avg { background: var(--viz-2); }
.chart { display: flex; flex-direction: column; gap: var(--space-1); }
.chart svg { width: 100%; height: 120px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); }
.line { fill: none; stroke-width: 2; }
.line.passed { stroke: var(--viz-1); }
.line.avg { stroke: var(--viz-2); }
.axis { display: flex; justify-content: space-between; color: var(--faint); font-size: var(--text-xs); }
.growth-list { display: flex; flex-direction: column; gap: var(--space-1); margin: 0; padding: 0; list-style: none; }
.growth-row { display: flex; flex-wrap: wrap; gap: var(--space-3); padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); font-size: var(--text-sm); }
.growth-row .date { color: var(--muted); font-variant-numeric: tabular-nums; }
.empty { margin: 0; color: var(--muted); font-size: var(--text-sm); }
</style>
