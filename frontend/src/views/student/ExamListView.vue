<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import { examsAPI } from '../../api/exams.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'

const router = useRouter()
const app = useAppStore()
const exams = ref([])
const loading = ref(true)
const waitingExam = ref(null)

const statusMeta = {
  scheduled: { label: '未完成 · 未开始', tone: 'scheduled', action: '查看时间安排' },
  ready: { label: '未完成 · 可开始', tone: 'ready', action: '进入考试' },
  in_progress: { label: '未完成 · 进行中', tone: 'progress', action: '继续答题' },
  submitted: { label: '已完成', tone: 'done', action: '查看提交状态', hint: '等待评分' },
  grading: { label: '已完成', tone: 'done', action: '查看提交状态', hint: '评分中' },
  review_required: { label: '已完成', tone: 'done', action: '查看提交状态', hint: '待教师复核' },
  graded: { label: '已完成', tone: 'done', action: '查看结果', hint: '评分完成' },
  missed: { label: '未完成 · 缺考', tone: 'missed', action: '已错过最晚进入时间' },
}

const totalText = computed(() => `共 ${exams.value.length} 场考试`)
const EXAM_KIND_LABELS = { quiz: '测验', midterm: '期中', final: '期末', other: '其他' }
const examKindLabel = (exam) => EXAM_KIND_LABELS[exam.exam_kind] || '测验'
const timingLabel = (exam) => (exam.timing_mode === 'per_student' ? '进入后完整时长' : '统一时间交卷')
const metaFor = (exam) => statusMeta[exam.student_status] || statusMeta.scheduled

onMounted(async () => {
  try {
    const res = await examsAPI.list()
    exams.value = res.data.items || res.data || []
  } catch {
    app.showToast('考试列表加载失败，请稍后重试', 'error')
  } finally {
    loading.value = false
  }
})

function openExam(exam) {
  if (exam.student_status === 'scheduled') {
    waitingExam.value = exam
    return
  }
  if (exam.student_status === 'missed') return
  router.push(`/student/exams/${exam.id}`)
}
</script>

<template>
  <AppLayout content-mode="list">
    <section class="exam-center">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">ASSESSMENTS</p>
          <h1>考试中心</h1>
          <p class="lead subtitle">按服务器时间开放考试，进行中的作答可随时继续</p>
        </div>
        <div class="ph-actions"><span class="count-pill"><span></span>{{ totalText }}</span></div>
      </section>

      <div v-if="loading" class="exam-grid" aria-label="正在加载考试">
        <div v-for="i in 4" :key="i" class="exam-card skeleton-card">
          <div class="skeleton line wide"></div>
          <div class="skeleton line"></div>
          <div class="skeleton line short"></div>
        </div>
      </div>

      <section v-else-if="exams.length === 0" class="empty empty-card">
        <div class="empty-mark empty-icon">✓</div>
        <h3>当前没有考试安排</h3>
        <p>教师发布考试后会显示在这里。</p>
      </section>

      <section v-else class="exam-grid" aria-label="考试列表">
        <article
          v-for="exam in exams"
          :key="exam.id"
          class="exam-card"
          :class="[`is-${metaFor(exam).tone}`, { disabled: exam.student_status === 'missed' }]"
        >
          <div class="card-top">
            <span class="status-dot"></span>
            <span class="status-label">{{ metaFor(exam).label }}</span>
            <span v-if="metaFor(exam).hint" class="status-hint">{{ metaFor(exam).hint }}</span>
          </div>
          <h2>{{ exam.title }}</h2>
          <p class="exam-tags"><span>{{ examKindLabel(exam) }}</span><span>{{ timingLabel(exam) }}</span><span v-if="exam.shuffle_questions">题目乱序</span><span v-if="exam.shuffle_options">选项乱序</span></p>
          <dl>
            <div><dt>考试时长</dt><dd>{{ exam.duration_minutes }} 分钟</dd></div>
            <div><dt>开始时间</dt><dd>{{ formatDateTime(exam.start_at) }}</dd></div>
            <div><dt>{{ exam.timing_mode === 'per_student' ? '最晚可开考' : '最晚进入' }}</dt><dd>{{ formatDateTime(exam.end_at) }}</dd></div>
          </dl>
          <button
            type="button"
            class="card-action"
            :disabled="exam.student_status === 'missed'"
            @click="openExam(exam)"
          >
            {{ metaFor(exam).action }} <span v-if="exam.student_status !== 'missed'">→</span>
          </button>
        </article>
      </section>
    </section>

    <ConfirmDialog
      v-if="waitingExam"
      title="考试尚未开始"
      :message="`本场考试将于 ${formatDateTime(waitingExam.start_at)} 开放。系统以服务器时间为准，请到时再进入。`"
      confirm-text="知道了"
      cancel-text="返回列表"
      @confirm="waitingExam = null"
      @cancel="waitingExam = null"
    />
  </AppLayout>
</template>

<style scoped>
.exam-tags { display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 0; }
.exam-tags span { padding:2px 8px;border-radius: var(--radius-full);background:var(--surface-subtle);color:var(--muted);font-size:11px; }
.exam-center { display: flex; flex-direction: column; gap: var(--space-5); }
.eyebrow { margin: 0 0 8px; color: var(--accent); font: 700 11px/1 var(--font-mono); letter-spacing: .16em; }
h1 { margin: 0; color: var(--fg); font-family: var(--font-display); font-size: var(--text-3xl); font-weight: 600; letter-spacing: -.01em; }
.subtitle { margin-top: 6px; }
.count-pill { display: inline-flex; align-items: center; gap: 9px; padding: 6px 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--muted); font-size: var(--text-sm); }
.count-pill span { width: 7px; height: 7px; border-radius: 50%; background: var(--success); }
.exam-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.exam-card { min-height: 230px; padding: 20px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.exam-card:not(.disabled):hover { border-color: var(--border-strong); }
.card-top { display: flex; align-items: center; gap: 8px; min-height: 24px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
.status-label { font-size: var(--text-sm); font-weight: 600; color: var(--muted); }
.status-hint { margin-left: auto; padding: 2px 8px; border-radius: var(--radius-sm); background: var(--surface-subtle); color: var(--muted); font-size: var(--text-xs); }
.is-ready .status-dot { background: var(--success); }
.is-ready .status-label { color: var(--success); }
.is-progress .status-dot { background: var(--warning); }
.is-progress .status-label { color: var(--warning); }
.is-done .status-dot { background: var(--accent); }
.is-done .status-label { color: var(--accent); }
.is-missed { background: var(--surface-subtle); }
.is-missed .status-dot { background: var(--danger); }
.exam-card h2 { margin: 14px 0 16px; color: var(--fg); font-size: var(--text-xl); font-weight: 600; line-height: 1.4; }
dl { display: grid; gap: 9px; margin: 0; padding: 14px 0; border-block: 1px solid var(--border); }
dl div { display: flex; justify-content: space-between; gap: 16px; font-size: var(--text-sm); }
dt { color: var(--muted); }
dd { margin: 0; color: var(--fg); font-weight: 500; text-align: right; font-variant-numeric: tabular-nums; }
.card-action { width: 100%; margin-top: 14px; padding: 0; border: 0; background: transparent; color: var(--accent); font-weight: 600; text-align: left; cursor: pointer; }
.card-action span { float: right; }
.card-action:disabled { color: var(--faint); cursor: not-allowed; }
.empty-card { border: 1px dashed var(--border-strong); background: var(--surface); }
.empty-icon { color: var(--success); font-size: 20px; }
.skeleton-card { pointer-events: none; }
.line { height: 13px; width: 55%; margin: 18px 0; }
.line.wide { width: 80%; }
.line.short { width: 38%; }
@media (max-width: 820px) { .exam-grid { grid-template-columns: 1fr; } .page-head { flex-direction: column; } }
</style>
