<script setup>
// 教师只读监考视图：切屏次数、累计离开时长、换机次数、最后保存时间与事件时间线。
// 只记录、只展示——不自动交卷、不自动扣分，页面本身不产生任何写请求。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import { examsAPI } from '../../api/exams.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const examId = Number(route.params.id)

const loading = ref(true)
const error = ref(false)
const data = ref(null)
const selectedStudentId = ref(null)
let pollTimer = null

const exam = computed(() => data.value?.exam || null)
const students = computed(() => data.value?.students || [])
const totals = computed(() => data.value?.totals || { students: 0, in_progress: 0, blur_count: 0, away_total_ms: 0, device_switch_count: 0 })
const events = computed(() => data.value?.events || [])
const selectedStudent = computed(() => students.value.find(row => row.student_id === selectedStudentId.value) || null)

const visibleEvents = computed(() => selectedStudentId.value == null
  ? events.value
  : events.value.filter(event => event.student_id === selectedStudentId.value))

function formatDuration(ms) {
  const total = Math.max(0, Number(ms) || 0)
  if (!total) return '0 秒'
  const minutes = Math.floor(total / 60000)
  const seconds = Math.round((total % 60000) / 1000)
  if (!minutes) return `${seconds} 秒`
  return `${minutes} 分 ${String(seconds).padStart(2, '0')} 秒`
}

const EVENT_LABELS = {
  blur: '离开页面（切屏）',
  focus: '回到页面',
  reconnect: '断网重连补报',
  device_switch: '换机接管',
  autosave_rejected: '旧设备写入被拒',
  warning: '教师延时留痕',
}

function eventLabel(kind) {
  return EVENT_LABELS[kind] || kind
}

function eventDetail(event) {
  if (event.kind === 'focus' && event.duration_ms != null) return `离开 ${formatDuration(event.duration_ms)}`
  if (event.kind === 'device_switch') return `第 ${event.payload?.generation ?? '—'} 代写入权限`
  if (event.kind === 'warning' && event.payload?.action === 'extend') return `延长 ${event.payload?.minutes ?? '—'} 分钟`
  if (event.kind === 'autosave_rejected') return `来源：${event.payload?.source || '—'}`
  return ''
}

function statusLabel(status) {
  return {
    started: '作答中', submitted: '已交卷', grading: '评分中', graded: '已评分', review_required: '待复核',
  }[status] || status
}

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  error.value = false
  try {
    const response = await examsAPI.getProctor(examId, { limit: 200 })
    data.value = response.data
  } catch (err) {
    if (!silent) error.value = true
    if (err.response?.status === 403) app.showToast('只有本场考试的任课教师可以查看监考数据', 'error')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  // 只读轮询刷新，避免教师手动刷新页面
  pollTimer = window.setInterval(() => load({ silent: true }), 15_000)
})

onBeforeUnmount(() => { window.clearInterval(pollTimer) })
</script>

<template>
  <AppLayout content-mode="workspace">
    <section class="proctor-page">
      <header class="page-heading">
        <div>
          <button type="button" class="back-link" @click="router.push('/teacher/exams')">← 返回考试管理</button>
          <p class="eyebrow">PROCTOR</p>
          <h1>{{ exam?.title || '监考视图' }}</h1>
          <p class="heading-note">
            {{ exam?.timing_mode === 'per_student' ? '进入后完整时长' : '统一时间交卷' }} ·
            时长 {{ exam?.duration_minutes ?? '—' }} 分钟 ·
            {{ exam?.shuffle_questions ? '题目乱序' : '题目顺序固定' }} ·
            {{ exam?.shuffle_options ? '选项乱序' : '选项顺序固定' }}
          </p>
        </div>
        <button type="button" class="btn btn-ghost" @click="load()">刷新</button>
      </header>

      <p class="readonly-note">
        本页为只读观测：切屏次数、离开时长与换机事件仅用于了解考试过程，<b>不自动认定作弊、也不自动扣分</b>；
        页面不会自动交卷，也不会修改任何成绩。
      </p>

      <DashboardAsyncState :loading="loading" :error="error" :empty="!students.length"
                           empty-title="还没有学生开始考试"
                           empty-body="学生点击开始后，这里会显示作答状态与异常事件。"
                           @retry="load()">
        <section class="totals">
          <article><span>参与学生</span><strong>{{ totals.students }}</strong></article>
          <article><span>作答中</span><strong>{{ totals.in_progress }}</strong></article>
          <article><span>切屏次数</span><strong>{{ totals.blur_count }}</strong></article>
          <article><span>累计离开</span><strong>{{ formatDuration(totals.away_total_ms) }}</strong></article>
          <article><span>换机次数</span><strong>{{ totals.device_switch_count }}</strong></article>
        </section>

        <section class="panel">
          <header class="panel-head">
            <h2>学生状态</h2>
            <button v-if="selectedStudentId != null" type="button" class="btn btn-ghost" @click="selectedStudentId = null">查看全部事件</button>
          </header>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>学生</th><th>状态</th><th>切屏</th><th>累计离开</th><th>换机</th><th>最后保存</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="row in students" :key="row.submission_id" :class="{ selected: row.student_id === selectedStudentId }">
                  <td><strong>{{ row.display_name }}</strong><small>{{ row.username }}</small></td>
                  <td><span class="pill" :class="row.status">{{ statusLabel(row.status) }}</span></td>
                  <td>{{ row.blur_count }}</td>
                  <td>{{ formatDuration(row.away_total_ms) }}</td>
                  <td>{{ row.device_switch_count }}</td>
                  <td>{{ row.last_saved_at ? formatDateTime(row.last_saved_at) : '—' }}</td>
                  <td><button type="button" class="btn btn-ghost" @click="selectedStudentId = row.student_id">事件</button></td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="panel">
          <header class="panel-head">
            <h2>事件时间线</h2>
            <span class="panel-note">{{ selectedStudent ? `${selectedStudent.display_name} · ` : '' }}最近 {{ visibleEvents.length }} 条</span>
          </header>
          <ol v-if="visibleEvents.length" class="timeline">
            <li v-for="event in visibleEvents" :key="event.id" :class="event.kind">
              <span class="time">{{ formatDateTime(event.server_ts) }}</span>
              <strong>{{ eventLabel(event.kind) }}</strong>
              <span v-if="eventDetail(event)" class="detail">{{ eventDetail(event) }}</span>
            </li>
          </ol>
          <p v-else class="panel-empty">暂无异常事件。</p>
        </section>
      </DashboardAsyncState>
    </section>
  </AppLayout>
</template>

<style scoped>
.proctor-page { display: flex; flex-direction: column; gap: 16px; }
.page-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; }
.back-link { margin: 0 0 12px; padding: 0; border: 0; background: transparent; color: var(--muted); cursor: pointer; }
.eyebrow { margin: 0 0 4px; color: var(--accent); font: 700 11px/1 var(--font-mono); letter-spacing: .14em; }
.page-heading h1 { margin: 0; color: var(--fg); font-size: 24px; }
.heading-note { margin: 6px 0 0; color: var(--muted); font-size: 12px; }
.readonly-note { margin: 0; padding: 12px 16px; border: 1px solid var(--info-bg); border-radius: var(--radius-lg); background: var(--accent-soft); color: var(--muted); font-size: 12px; line-height: 1.8; }
.readonly-note b { color: var(--fg); }
.totals { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.totals article { padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.totals span { display: block; color: var(--muted); font-size: 12px; }
.totals strong { display: block; margin-top: 8px; color: var(--fg); font-size: 20px; }
.panel { padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.panel + .panel { margin-top: 16px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
.panel-head h2 { margin: 0; color: var(--fg); font-size: 16px; }
.panel-note { color: var(--muted); font-size: 12px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { padding: 8px; color: var(--muted); font-weight: 600; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 10px 8px; color: var(--fg); border-bottom: 1px solid var(--border); vertical-align: middle; }
td small { display: block; color: var(--faint); font-size: 11px; }
tr.selected { background: var(--surface-subtle); }
.pill { display: inline-block; padding: 2px 8px; border-radius: var(--radius-full); background: var(--surface-subtle); color: var(--muted); font-size: 11px; }
.pill.started { background: var(--accent-soft); color: var(--accent-hover); }
.pill.graded { background: var(--success-bg); color: var(--success); }
.pill.review_required { background: var(--warning-bg); color: var(--warning); }
.timeline { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.timeline li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px; padding: 8px 12px; border-left: 3px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-subtle); font-size: 12px; }
.timeline li.blur, .timeline li.autosave_rejected { border-left-color: var(--warning); }
.timeline li.device_switch { border-left-color: var(--accent); }
.timeline .time { color: var(--faint); font: 11px var(--font-mono); }
.timeline strong { color: var(--fg); }
.timeline .detail { color: var(--muted); }
.panel-empty { margin: 0; color: var(--faint); font-size: 12px; }
@media (max-width: 860px) {
  .totals { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .page-heading { flex-direction: column; align-items: flex-start; }
}
</style>
