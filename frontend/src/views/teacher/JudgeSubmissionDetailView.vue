<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import CodeBlock from '../../components/common/CodeBlock.vue'
import { submissionsAPI } from '../../api/submissions.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const detail = ref(null)
const loading = ref(true)
const loadError = ref(false)

const statusMap = {
  pending: { label: '等待中', tone: 'badge-warning' },
  queued: { label: '排队中', tone: 'badge-warning' },
  running: { label: '判题中', tone: 'badge-info' },
  completed: { label: '已完成', tone: 'badge-success' },
  system_error: { label: '系统错误', tone: 'badge-danger' },
  accepted: { label: '通过', tone: 'badge-success' },
  wrong_answer: { label: '答案错误', tone: 'badge-danger' },
  runtime_error: { label: '运行错误', tone: 'badge-danger' },
  time_limit_exceeded: { label: '超时', tone: 'badge-warning' },
}

const gradeStatus = computed(() => statusMap[detail.value?.grading_status] || { label: detail.value?.grading_status || '—', tone: 'badge-neutral' })
const judgeStatus = computed(() => statusMap[detail.value?.status] || { label: detail.value?.status || '—', tone: 'badge-neutral' })

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const { data } = await submissionsAPI.getTeacherJudgeDetail(route.params.id)
    detail.value = data
  } catch {
    loadError.value = true
    app.showToast('加载提交详情失败', 'error')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="workspace">
    <div class="judge-detail-page">
      <button class="back-link" @click="router.back()"><AppIcon name="back" :size="17" />返回</button>
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">评分 / 作业提交</p>
          <h1>作业提交详情</h1>
          <p class="lead">{{ detail?.assignment_title || '—' }} · {{ detail?.question_title || '' }}</p>
        </div>
      </section>

      <div v-if="loading" class="panel"><div class="panel-body">加载中…</div></div>
      <div v-else-if="loadError" class="panel"><div class="panel-body">加载失败，请刷新重试。</div></div>

      <template v-else-if="detail">
        <section class="metric-strip detail-strip" aria-label="提交摘要">
          <div class="metric"><span class="m-value">{{ detail.student_name || '—' }}</span><span class="m-label">学生</span></div>
          <div class="metric"><span class="m-value">{{ detail.student_no || '—' }}</span><span class="m-label">学号</span></div>
          <div class="metric"><span class="m-value">{{ detail.tests_total != null ? `${detail.tests_passed ?? 0} / ${detail.tests_total}` : '—' }}</span><span class="m-label">测试通过</span></div>
          <div class="metric em"><span class="m-value">{{ detail.ai_score != null ? detail.ai_score.toFixed(1) : detail.score != null ? detail.score.toFixed(1) : '—' }}</span><span class="m-label">AI 得分 / 总分</span></div>
        </section>

        <section class="grid-2-1 detail-grid">
          <article class="panel">
            <div class="panel-head"><div class="ph-label"><p class="eyebrow">Judge</p><h3>判题状态</h3></div></div>
            <div class="panel-body stack">
              <div class="detail-row"><span>判题状态</span><span class="badge" :class="judgeStatus.tone"><span class="dot"></span>{{ judgeStatus.label }}</span></div>
              <div class="detail-row"><span>评分状态</span><span class="badge" :class="gradeStatus.tone"><span class="dot"></span>{{ gradeStatus.label }}</span></div>
              <div class="detail-row"><span>提交时间</span><span>{{ formatDateTime(detail.finished_at || detail.created_at) }}</span></div>
              <div class="detail-row"><span>课程</span><span>{{ detail.course_title || '—' }}</span></div>
              <div v-if="detail.ai_grade_id" class="detail-row">
                <span>AI 评分</span>
                <span>
                  <span class="badge" :class="detail.ai_needs_review ? 'badge-warning' : 'badge-success'"><span class="dot"></span>{{ detail.ai_needs_review ? '需复核' : '已评分' }}</span>
                  <button type="button" class="btn btn-ghost btn-sm" @click="router.push(`/teacher/ai-grading/${detail.ai_grade_id}`)">查看 AI 评分详情</button>
                </span>
              </div>
            </div>
          </article>

          <article class="panel">
            <div class="panel-head"><div class="ph-label"><p class="eyebrow">Output</p><h3>运行输出</h3></div></div>
            <div class="panel-body">
              <pre class="terminal-output"><code>{{ detail.stdout || detail.stderr || detail.last_error || '暂无输出' }}</code></pre>
            </div>
          </article>
        </section>

        <section class="panel">
          <div class="panel-head"><div class="ph-label"><p class="eyebrow">Source</p><h3>学生代码</h3></div></div>
          <div class="panel-body">
            <CodeBlock :code="detail.code" language="python" />
          </div>
        </section>
      </template>
    </div>
  </AppLayout>
</template>

<style scoped>
.judge-detail-page { display: flex; flex-direction: column; gap: var(--space-5); }
.back-link { align-self: flex-start; padding: 0; border: 0; background: transparent; color: var(--accent); }
.detail-strip { grid-template-columns: repeat(4, 1fr); }
.detail-grid { align-items: start; }
.stack { display: flex; flex-direction: column; gap: var(--space-3); }
.detail-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
.detail-row:last-child { border-bottom: 0; }
.detail-row > span:first-child { color: var(--muted); }
.terminal-output {
  margin: 0;
  padding: 12px;
  min-height: 120px;
  max-height: 280px;
  overflow: auto;
  background: var(--surface-sunken);
  border-radius: var(--radius-md);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  white-space: pre-wrap;
}
@media (max-width: 900px) { .detail-grid { grid-template-columns: 1fr; } .detail-strip { grid-template-columns: 1fr 1fr; } }
</style>
