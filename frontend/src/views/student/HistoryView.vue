<script setup>
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import CodeBlock from '../../components/common/CodeBlock.vue'
import MarkdownCell from '../../components/notebook/MarkdownCell.vue'
import LabSubmissionFeedback from '../../components/labs/LabSubmissionFeedback.vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { accountAPI, errorMessage } from '../../api/accountManagement.js'
const route = useRoute()
const rows = ref([])
const detail = ref(null)
const courses = ref([])
const kind = ref('all')
const courseId = ref('')
const page = ref(1)
const total = ref(0)
const error = ref('')
const loading = ref(false)
const labels = { assignment: '作业', exam: '考试', experiment: '实验' }
let generation = 0
async function load() {
  const request = ++generation
  loading.value = true; error.value = ''; detail.value = null
  try {
    if (route.params.id) {
      const { data } = await accountAPI.historyDetail(route.params.kind, route.params.id)
      if (request === generation) detail.value = data
    } else {
      const { data } = await accountAPI.history({ kind: kind.value, course_id: courseId.value || undefined, page: page.value })
      if (request === generation) { rows.value = data.items; total.value = data.total; courses.value = data.courses }
    }
  } catch (e) { if (request === generation) error.value = errorMessage(e) }
  finally { if (request === generation) loading.value = false }
}
watch(() => route.fullPath, load, { immediate: true })
</script>
<template>
  <AppLayout content-mode="list"><div class="account-page">
    <header><h1>{{ detail ? detail.entry_title : '我的学习记录' }}</h1><p class="account-muted">查看自己的历史提交和公开成绩，退班后记录仍然保留。</p></header>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p><p v-if="loading" role="status">正在加载记录…</p>
    <template v-if="detail">
      <RouterLink to="/student/history">← 返回学习记录</RouterLink>
      <section class="account-panel"><h2>{{ labels[detail.kind] }}提交</h2><p>{{ detail.course_title || '独立实验' }} · {{ new Date(detail.submitted_at).toLocaleString() }}</p><p>成绩：{{ detail.score_visible ? detail.score : '尚未公开' }}</p>
        <CodeBlock v-if="detail.code !== undefined" :code="detail.code || ''" filename="submission.py" />
        <article v-for="answer in detail.answers" :key="answer.question_id"><h3>题目 #{{ answer.question_id }}</h3><pre class="account-code">{{ answer.code ?? answer.selected_options ?? answer.text_answers }}</pre></article>
        <article v-for="cell in detail.cells" :key="cell.id"><h3>{{ cell.type }} · {{ cell.id }}</h3><MarkdownCell v-if="cell.type === 'markdown'" :cell="cell" /><CodeBlock v-else :code="cell.source || ''" /><pre v-if="cell.output" class="account-code">{{ JSON.stringify(cell.output, null, 2) }}</pre></article>
        <p v-if="detail.feedback">教师反馈：{{ detail.feedback }}</p>
        <LabSubmissionFeedback v-if="detail.kind === 'experiment' && detail.lab_enabled" :key="route.params.id" :submission-id="route.params.id" />
      </section>
    </template>
    <template v-else-if="!route.params.id">
      <form class="account-actions" @submit.prevent="page = 1; load()"><label>类型 <DsSelect v-model="kind"><option value="all">全部</option><option v-for="(name, key) in labels" :key="key" :value="key">{{ name }}</option></DsSelect></label><label>课程 <DsSelect v-model="courseId"><option value="">全部课程</option><option v-for="c in courses" :key="c.id" :value="c.id">{{ c.name }}</option></DsSelect></label><button class="btn-primary">筛选</button></form>
      <section class="account-panel"><p v-if="!rows.length && !loading" class="account-muted">还没有符合条件的提交记录。</p><table v-else class="account-data"><thead><tr><th>类型</th><th>内容</th><th>成绩</th><th>时间</th></tr></thead><tbody><tr v-for="row in rows" :key="`${row.kind}-${row.submission_id}`"><td>{{ labels[row.kind] }}</td><td><RouterLink :to="`/student/history/${row.kind}/${row.submission_id}`">{{ row.entry_title }}</RouterLink><p class="account-muted">{{ row.course_title || '独立实验' }}</p></td><td>{{ row.score_visible ? row.score : '尚未公开' }}</td><td>{{ new Date(row.submitted_at).toLocaleString() }}</td></tr></tbody></table>
        <TeacherPagination :current-page="page" :page-count="Math.max(1, Math.ceil(total / 20))" :total="total" :page-size="20" @change="page = $event; load()" />
      </section>
    </template>
  </div></AppLayout>
</template>

<style scoped>
.account-data th:nth-child(2) { width: 48%; }
.account-data th:nth-child(3) { width: 14%; }
.account-data th:nth-child(4) { width: 26%; }
.account-panel > article { margin-top: 20px; }
.account-panel :deep(.codeblock-wrap) { max-width: 100%; }
</style>
