<script setup>
// 题库卡片：难度/标签/我的完成状态 + 已通过题目的最高分。
// 卡片整体可点击，但焦点落在内部按钮上，保证 Tab 可达与 Enter 触发。
import { computed } from 'vue'
import AppIcon from '../ui/AppIcon.vue'
import UiStatusPill from '../ui/UiStatusPill.vue'
import { difficultyMeta, formatScore, gradingModeMeta } from './practiceHelpers.js'

const props = defineProps({
  problem: { type: Object, required: true },
})

const emit = defineEmits(['open'])

const MY_STATUS_META = {
  not_started: { label: '未开始', tone: 'neutral', action: '开始作答' },
  attempted: { label: '做过未通过', tone: 'warning', action: '继续作答' },
  solved: { label: '已通过', tone: 'success', action: '再做一次' },
}

const difficulty = computed(() => difficultyMeta(props.problem.difficulty))
const myStatus = computed(() => MY_STATUS_META[props.problem.my_status] || MY_STATUS_META.not_started)
const mode = computed(() => gradingModeMeta(props.problem.grading_mode))
const bestScore = computed(() => {
  const value = props.problem.my_best_score
  if (value === null || value === undefined) return ''
  return `最高分 ${formatScore(value)}`
})
const tags = computed(() => (props.problem.tags || []).slice(0, 4))
const extraTags = computed(() => Math.max(0, (props.problem.tags || []).length - tags.value.length))
</script>

<template>
  <article class="problem-card" :class="`is-${problem.my_status || 'not_started'}`">
    <header class="card-head">
      <UiStatusPill :tone="difficulty.tone" :label="difficulty.label" />
      <UiStatusPill :tone="myStatus.tone" :label="myStatus.label" />
    </header>
    <h3 class="card-title" :title="problem.title">{{ problem.title }}</h3>
    <p v-if="problem.prompt" class="card-prompt">{{ problem.prompt }}</p>
    <div v-if="tags.length" class="tag-row">
      <span v-for="tag in tags" :key="tag" class="tag">#{{ tag }}</span>
      <span v-if="extraTags" class="tag">+{{ extraTags }}</span>
    </div>
    <dl class="card-meta">
      <div><dt>判题方式</dt><dd>{{ mode.label }}</dd></div>
      <div v-if="problem.time_limit_ms"><dt>时间限制</dt><dd>{{ Math.round(problem.time_limit_ms / 1000) }} 秒</dd></div>
      <div><dt>通过人数</dt><dd>{{ problem.distinct_solvers ?? 0 }}</dd></div>
    </dl>
    <footer class="card-foot">
      <span v-if="bestScore" class="best-score">{{ bestScore }}</span>
      <span v-else class="best-score faint">尚未提交</span>
      <button type="button" class="btn btn-primary btn-sm" @click="emit('open', problem)">
        {{ myStatus.action }}<AppIcon name="arrow-right" :size="14" />
      </button>
    </footer>
  </article>
</template>

<style scoped>
.problem-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  transition: border-color 0.12s ease, box-shadow 0.12s ease;
}
.problem-card:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.problem-card.is-solved { border-left: 3px solid var(--success); }
.problem-card.is-attempted { border-left: 3px solid var(--warning); }
.card-head { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.card-title { margin: 0; font-family: var(--font-display); font-size: var(--text-xl); line-height: var(--lh-tight); overflow-wrap: anywhere; }
.card-prompt {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-md);
  line-height: var(--lh-body);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tag-row { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.tag { font-size: var(--text-sm); color: var(--muted); background: var(--surface-subtle); border-radius: var(--radius-sm); padding: 2px var(--space-2); }
.card-meta { display: flex; flex-wrap: wrap; gap: var(--space-4); margin: 0; font-size: var(--text-sm); }
.card-meta div { display: flex; align-items: baseline; gap: var(--space-2); }
.card-meta dt { color: var(--faint); }
.card-meta dd { margin: 0; color: var(--fg); font-family: var(--font-mono); }
.card-foot { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); margin-top: auto; }
.best-score { font-family: var(--font-mono); font-size: var(--text-sm); color: var(--accent); }
.best-score.faint { color: var(--faint); }
</style>
