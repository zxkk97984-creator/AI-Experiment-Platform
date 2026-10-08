<script setup>
import { ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { errorMessage, metricLabels } from './labHelpers.js'
const props = defineProps({ recordId: { type: [Number, String], required: true } })
const data = ref(null)
const error = ref('')
const loading = ref(false)
async function load() {
  loading.value = true
  error.value = ''
  try { data.value = (await labsAPI.leaderboard(props.recordId)).data }
  catch (e) { error.value = errorMessage(e, '排行榜加载失败') }
  finally { loading.value = false }
}
</script>
<template>
  <details class="lab-leaderboard"><summary>本班实验排行榜</summary>
    <p class="lab-muted">仅展示本班同一评分版本的已发布成绩，名次不参与总分。{{ data?.named ? '本班教师已启用姓名展示。' : '默认使用匿名代号。' }}</p>
    <button type="button" class="btn btn-secondary btn-sm" :disabled="loading" @click="load">{{ loading ? '加载中…' : data ? '刷新排行榜' : '查看排行榜' }}</button>
    <p v-if="error" role="alert" class="lab-error">{{ error }}</p>
    <template v-if="data"><p class="lab-muted">第 {{ data.assessment_version }} 版 · {{ data.grade_selection === 'latest' ? '最近一次成绩' : '最高成绩' }} · 同分并列</p>
      <p v-if="!data.items.length">尚无已发布成绩。</p>
      <div v-else class="leaderboard-scroll"><table><thead><tr><th>名次</th><th>同学</th><th>总分</th><th v-if="data.metric">{{ metricLabels[data.metric] }}</th><th>程序耗时</th></tr></thead><tbody><tr v-for="(row, index) in data.items" :key="index"><td>{{ row.rank }}</td><td>{{ row.display_name }}{{ row.is_me ? '（我）' : '' }}</td><td>{{ row.score }}</td><td v-if="data.metric">{{ row.metric ?? '—' }}</td><td>{{ row.program_elapsed_seconds == null ? '—' : `${Number(row.program_elapsed_seconds).toFixed(3)} 秒` }}</td></tr></tbody></table></div>
    </template>
  </details>
</template>
<style scoped>
.leaderboard-scroll { overflow-x: auto; }
table { min-width: 420px; width: 100%; font-size: 13px; }
th, td { padding: 8px 12px; text-align: left; }
</style>
