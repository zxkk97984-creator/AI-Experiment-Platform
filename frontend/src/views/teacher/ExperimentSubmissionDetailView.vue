<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { experimentsAPI } from '../../api/experiments.js'
import { useAppStore } from '../../stores/app.js'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import SubmissionSnapshotCell from '../../components/teacher/SubmissionSnapshotCell.vue'
import SubmissionReviewPanel from '../../components/teacher/SubmissionReviewPanel.vue'
import { formatDateTime } from '../../utils/format.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const submission = ref(null)
const loading = ref(true)
const loadError = ref(false)
const saving = ref(false)
const showEmptyCells = ref(false)

const cells = computed(() => {
  if (!submission.value?.cells_snapshot) return []
  const cellMap = submission.value.cell_metadata || {}
  const outputs = submission.value.outputs_snapshot || {}

  return Object.entries(submission.value.cells_snapshot).map(([id, source], index) => {
    const meta = cellMap[id] || {}
    return {
      id,
      // 防御：历史数据可能嵌套为 {"cells": {...}}，source 非字符串时置空，避免渲染崩溃
      source: typeof source === 'string' ? (source || '') : '',
      type: meta.type || 'code',
      order: meta.order ?? index,
      outputs: outputs[id] || null,
    }
  }).sort((a, b) => a.order - b.order)
})

const visibleCells = computed(() => {
  if (showEmptyCells.value) return cells.value
  return cells.value.filter((cell) => cell.source.trim() || cell.outputs?.outputs?.length)
})
const emptyCellCount = computed(() => cells.value.length - cells.value.filter(
  (cell) => cell.source.trim() || cell.outputs?.outputs?.length,
).length)
const isGraded = computed(() => submission.value?.score != null)
const avatarText = computed(() => {
  const value = submission.value?.student_name || submission.value?.student_username || '学'
  return value.trim().slice(0, 1)
})

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const res = await experimentsAPI.getSubmission(route.params.id)
    submission.value = res.data
  } catch {
    loadError.value = true
    app.showToast('加载提交详情失败', 'error')
  } finally {
    loading.value = false
  }
}

async function submitReview({ score, feedback }) {
  if (score != null && (!Number.isFinite(score) || score < 0 || score > 100)) {
    app.showToast('评分必须在 0-100 之间', 'error')
    return
  }
  if (score == null && !feedback) {
    app.showToast('请填写评分或反馈', 'error')
    return
  }

  saving.value = true
  try {
    const payload = { feedback }
    if (score != null) payload.score = score
    await experimentsAPI.updateReview(submission.value.id, payload)
    app.showToast('评分已保存', 'success')
    await load()
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '评分失败', 'error')
  } finally {
    saving.value = false
  }
}

function goBack() {
  const role = route.meta?.role
  router.push(role === 'admin' ? '/admin/submissions' : '/teacher/submissions')
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="workspace">
    <div class="detail-page">
      <button type="button" class="back-button" @click="goBack">
        <AppIcon name="back" :size="17" />
        返回提交列表
      </button>

      <div v-if="loading" class="detail-loading">
        <div class="skeleton title-skeleton"></div>
        <div class="skeleton meta-skeleton"></div>
        <div class="loading-grid">
          <div class="skeleton content-skeleton"></div>
          <div class="skeleton review-skeleton"></div>
        </div>
      </div>

      <div v-else-if="loadError" class="error-panel">
        <span><AppIcon name="warning" :size="26" /></span>
        <strong>提交详情暂时无法加载</strong>
        <p>请稍后重试，或返回提交列表选择其他记录。</p>
        <button type="button" class="secondary-button" @click="load">重新加载</button>
      </div>

      <template v-else-if="submission">
        <header class="page-head">
          <h1>提交详情 / 评分工作台</h1>
          <p>查看学生实验快照并完成评分反馈</p>
        </header>

        <section class="submission-meta" aria-label="提交信息">
          <div class="student-summary">
            <span class="student-avatar" aria-hidden="true">{{ avatarText }}</span>
            <span>
              <strong>{{ submission.student_name || '未命名学生' }}</strong>
              <small>账号：{{ submission.student_username || '—' }}</small>
            </span>
          </div>
          <dl>
            <div><dt>实验名称</dt><dd>{{ submission.entry_name || '未命名实验' }}</dd></div>
            <div><dt>所属课程</dt><dd>{{ submission.course_name || '独立实验' }}</dd></div>
            <div><dt>提交次数</dt><dd><span class="attempt-pill">第 {{ submission.attempt_number }} 次提交</span></dd></div>
            <div><dt>提交时间</dt><dd>{{ formatDateTime(submission.submitted_at) }}</dd></div>
            <div>
              <dt>当前状态</dt>
              <dd><span class="status-pill" :class="isGraded ? 'graded' : 'pending'">{{ isGraded ? '已评分' : '待评分' }}</span></dd>
            </div>
          </dl>
        </section>

        <div class="workspace-grid">
          <section class="snapshot-panel">
            <header class="panel-head">
              <div>
                <h2>学生提交内容</h2>
                <p>提交时的 Notebook 内容与执行输出，仅供查看</p>
              </div>
              <span>{{ visibleCells.length }} / {{ cells.length }} 个内容块</span>
            </header>

            <div v-if="visibleCells.length" class="cell-list">
              <SubmissionSnapshotCell v-for="cell in visibleCells" :key="cell.id" :cell="cell" />
            </div>
            <div v-else class="empty-content">该提交没有可展示的内容</div>

            <footer v-if="emptyCellCount" class="snapshot-footer">
              <span>{{ emptyCellCount }} 个空单元格{{ showEmptyCells ? '已展开' : '已收起' }}</span>
              <button type="button" @click="showEmptyCells = !showEmptyCells">
                {{ showEmptyCells ? '收起空单元格' : '展开全部' }}
                <AppIcon :name="showEmptyCells ? 'chevron-up' : 'chevron-down'" :size="16" />
              </button>
            </footer>
          </section>

          <SubmissionReviewPanel :submission="submission" :saving="saving" @submit="submitReview" />
        </div>
      </template>
    </div>
  </AppLayout>
</template>

<style scoped>
.detail-page { display: flex; flex-direction: column; gap: 18px; }
.back-button { width: fit-content; padding: 4px 0; display: inline-flex; align-items: center; gap: 6px; border: 0; background: transparent; color: var(--accent); font-size: 13px; cursor: pointer; }
.back-button:hover { color: var(--accent-hover); }
.page-head h1 { margin: 0 0 5px; color: var(--fg); font-size: 28px; line-height: 1.2; letter-spacing: -.025em; }
.page-head p { margin: 0; color: var(--muted); font-size: 13px; }

.submission-meta { display: grid; grid-template-columns: 230px minmax(0, 1fr); align-items: center; padding: 18px 20px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); box-shadow: none; }
.student-summary { display: flex; align-items: center; gap: 12px; padding-right: 20px; border-right: 1px solid var(--border); }
.student-avatar { width: 46px; height: 46px; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-size: 20px; font-weight: 600; }
.student-summary > span:last-child { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
.student-summary strong { overflow: hidden; color: var(--fg); font-size: 15px; text-overflow: ellipsis; white-space: nowrap; }
.student-summary small { color: var(--muted); font-size: 11px; }
.submission-meta dl { display: grid; grid-template-columns: 1.15fr 1.15fr .9fr 1.3fr .8fr; margin: 0; }
.submission-meta dl > div { min-width: 0; padding: 0 18px; border-right: 1px solid var(--border); }
.submission-meta dl > div:last-child { border-right: 0; }
.submission-meta dt { margin-bottom: 5px; color: var(--faint); font-size: 11px; }
.submission-meta dd { overflow: hidden; margin: 0; color: var(--fg); font-size: 12.5px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.attempt-pill, .status-pill { display: inline-flex; width: fit-content; padding: 4px 8px; border: 1px solid var(--accent-soft); border-radius: var(--radius-sm); background: var(--accent-soft); color: var(--accent); font-size: 11px; font-weight: 500; }
.status-pill.pending { border-color: var(--warning-bg); background: var(--warning-bg); color: var(--warning); }
.status-pill.graded { border-color: var(--success-bg); background: var(--success-bg); color: var(--success); }

.workspace-grid { display: grid; grid-template-columns: minmax(0, 1fr) 360px; align-items: start; gap: 18px; }
.snapshot-panel, .review-panel { border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); box-shadow: none; }
.panel-head { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 15px 18px; border-bottom: 1px solid var(--border); }
.panel-head h2 { margin: 0 0 3px; color: var(--fg); font-size: 15px; }
.panel-head p { margin: 0; color: var(--muted); font-size: 11px; }
.panel-head > span { color: var(--faint); font-size: 11px; white-space: nowrap; }
.cell-list { display: flex; flex-direction: column; gap: 12px; padding: 14px; }
.empty-content { padding: 64px 20px; color: var(--muted); font-size: 13px; text-align: center; }
.snapshot-footer { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-top: 1px solid var(--border); color: var(--muted); font-size: 11px; }
.snapshot-footer button { padding: 5px 8px; display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--accent); font-size: 11px; cursor: pointer; }

/* .review-panel 布局与吸顶行为由 SubmissionReviewPanel 组件内部管理 */
.current-status { display: flex; align-items: center; justify-content: space-between; padding: 13px 18px; border-bottom: 1px solid var(--border); color: var(--muted); font-size: 12px; }
.score-row { padding: 18px; border-bottom: 1px solid var(--border); }
.score-row > label, .feedback-field > span { display: block; margin-bottom: 8px; color: var(--fg); font-size: 12px; font-weight: 600; }
.score-input { display: flex; align-items: center; gap: 10px; }
.score-input input { width: 126px; height: 46px; color: var(--accent); font-size: 21px; font-weight: 700; text-align: center; }
.score-input span { color: var(--muted); font-size: 12px; }
.feedback-field { position: relative; display: block; padding: 18px; border-bottom: 1px solid var(--border); }
.feedback-field textarea { min-height: 166px; resize: vertical; line-height: 1.65; }
.feedback-field small { position: absolute; right: 28px; bottom: 26px; color: var(--faint); font-size: 10px; }
.reviewed-time { display: flex; align-items: center; gap: 6px; padding: 13px 18px 0; color: var(--faint); font-size: 11px; }
.save-button { width: calc(100% - 36px); height: 42px; margin: 16px 18px 18px; display: inline-flex; align-items: center; justify-content: center; gap: 7px; border: 1px solid var(--accent); border-radius: var(--radius-md); background: var(--accent); color: var(--surface); font-size: 13px; font-weight: 600; cursor: pointer; }
.save-button:hover:not(:disabled) { border-color: var(--accent-hover); background: var(--accent-hover); box-shadow: var(--shadow-sm); }
.save-button:disabled { opacity: .55; cursor: not-allowed; }

.detail-loading { display: flex; flex-direction: column; gap: 16px; }
.title-skeleton { width: 260px; height: 34px; }
.meta-skeleton { height: 100px; }
.loading-grid { display: grid; grid-template-columns: 1.3fr .7fr; gap: 18px; }
.content-skeleton, .review-skeleton { height: 420px; }
.error-panel { min-height: 430px; display: flex; flex-direction: column; align-items: center; justify-content: center; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); text-align: center; }
.error-panel > span { width: 54px; height: 54px; display: inline-flex; align-items: center; justify-content: center; margin-bottom: 14px; border-radius: 50%; background: var(--danger-bg); color: var(--danger); }
.error-panel strong { color: var(--fg); }
.error-panel p { margin: 6px 0 16px; color: var(--muted); font-size: 12px; }
.secondary-button { padding: 8px 16px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface); color: var(--fg); cursor: pointer; }

@media (max-width: 1199px) {
  .submission-meta { grid-template-columns: 1fr; gap: 16px; }
  .student-summary { padding: 0 0 16px; border-right: 0; border-bottom: 1px solid var(--border); }
  .submission-meta dl > div:first-child { padding-left: 0; }
  .workspace-grid { grid-template-columns: 1fr; }
  .review-panel { position: static; }
}
@media (max-width: 800px) {
  .detail-page { gap: 14px; }
  .page-head h1 { font-size: 23px; }
  .submission-meta { padding: 16px; }
  .submission-meta dl { grid-template-columns: 1fr 1fr; row-gap: 16px; }
  .submission-meta dl > div { padding: 0 12px 0 0; border-right: 0; }
  .panel-head { align-items: flex-start; }
  .loading-grid { grid-template-columns: 1fr; }
}
</style>
