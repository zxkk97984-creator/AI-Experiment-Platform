<script setup>
import { computed } from 'vue'
const criteria = defineModel({ type: Array, required: true })
const total = computed(() => criteria.value.reduce((sum, c) => sum + Number(c.max_score || 0), 0))
function addCriterion() {
  criteria.value.push({ id: `criterion_${crypto.randomUUID().slice(0, 8)}`, name: '', description: '', source: 'ai', max_score: 10, levels: [
    { id: 'missing', description: '未达到要求', points: 0 }, { id: 'complete', description: '完整达到要求', points: 10 },
  ] })
}
function setSource(c) {
  if (c.source === 'ai' && !c.levels.length) c.levels = [{ id: 'missing', description: '未达到要求', points: 0 }, { id: 'complete', description: '完整达到要求', points: Number(c.max_score) }]
  if (c.source !== 'ai') c.levels = []
}
</script>
<template>
  <div>
    <p :class="total === 100 ? 'lab-muted' : 'lab-error'">评分总分：{{ total }} / 100</p>
    <fieldset v-for="(criterion, index) in criteria" :key="criterion.id" class="criterion">
      <legend>第 {{ index + 1 }} 项标准</legend>
      <div class="lab-grid">
        <label class="lab-field">名称<input v-model="criterion.name" maxlength="200" required /></label>
        <label class="lab-field">评分依据<select v-model="criterion.source" @change="setSource(criterion)"><option value="ai">AI 按等级评阅</option><option value="metric">平台客观指标</option><option value="check">教师检查脚本</option><option value="cell">单元格检查</option><option value="efficiency">统一环境运行效率</option></select></label>
        <label class="lab-field">满分<input v-model.number="criterion.max_score" type="number" min="1" max="100" step="0.5" required /></label>
      </div>
      <label class="lab-field">具体要求<textarea v-model="criterion.description" rows="2" maxlength="3000" placeholder="明确必须实现的算法、应出现的结果或要回答的问题" required /></label>
      <label v-if="criterion.source === 'check'" class="lab-field">检查项编号（脚本输出使用此编号）<input v-model="criterion.id" pattern="[a-zA-Z0-9_-]{1,40}" maxlength="40" required /></label>
      <template v-if="criterion.source === 'ai'">
        <div v-for="(level, li) in criterion.levels" :key="level.id" class="level-row">
          <label class="lab-field">等级 {{ li + 1 }} 判据<input v-model="level.description" maxlength="1500" required /></label>
          <label class="lab-field">得分<input v-model.number="level.points" type="number" min="0" :max="criterion.max_score" step="0.5" required /></label>
          <button class="btn btn-ghost btn-sm" type="button" :disabled="criterion.levels.length <= 2" :aria-label="`移除第 ${index + 1} 项的等级 ${li + 1}`" @click="criterion.levels.splice(li, 1)">移除</button>
        </div>
        <button class="btn btn-secondary btn-sm" type="button" :disabled="criterion.levels.length >= 10" @click="criterion.levels.push({ id: `level_${crypto.randomUUID().slice(0, 8)}`, description: '', points: 0 })">添加等级</button>
      </template>
      <div class="lab-actions"><button class="btn btn-ghost btn-sm" type="button" @click="criteria.splice(index, 1)">删除这项标准</button></div>
    </fieldset>
    <button class="btn btn-secondary btn-sm" type="button" @click="addCriterion">添加评分项</button>
  </div>
</template>
<style scoped>
.criterion { min-width: 0; margin: var(--space-3) 0; padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-sm); }
.criterion legend { padding-inline: var(--space-2); color: var(--muted); }
.level-row { display: grid; grid-template-columns: minmax(0, 1fr) 90px auto; gap: var(--space-2); align-items: center; }
@media (max-width: 480px) { .level-row { grid-template-columns: minmax(0, 1fr) 75px; } .level-row > button { justify-self: start; } }
</style>
