<script setup>
import { ref } from 'vue'
import MarkdownCell from './MarkdownCell.vue'
defineProps({ cell: { type: Object, required: true } })
const emit = defineEmits(['update:source'])
const preview = ref(false)
</script>
<template>
  <section class="report-cell" :aria-label="`实验报告 ${cell.id}`">
    <div class="report-head"><strong>实验报告</strong><button type="button" class="btn btn-ghost btn-sm" :aria-pressed="preview" @click="preview = !preview">{{ preview ? '继续编辑' : '预览报告' }}</button></div>
    <MarkdownCell v-if="preview" :cell="cell" />
    <label v-else class="report-label">分析过程与结论（支持 Markdown）<textarea :value="cell.source" rows="7" :aria-label="`报告内容 ${cell.id}`" placeholder="记录你的方法、参数对比、结果分析和改进方向…" @input="emit('update:source', cell.id, $event.target.value)" /></label>
  </section>
</template>
<style scoped>
.report-cell { padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.report-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; margin-bottom: var(--space-3); }
.report-label { display: flex; flex-direction: column; gap: var(--space-2); color: var(--muted); font-size: var(--text-sm); }
.report-label textarea { width: 100%; min-width: 0; box-sizing: border-box; padding: var(--space-3); resize: vertical; font-family: var(--font-mono); color: var(--fg); background: var(--surface); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); line-height: 1.7; }
.report-label textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
