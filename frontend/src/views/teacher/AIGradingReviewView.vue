<template>
  <AppLayout content-mode="list">
    <section class="ai-grading-review">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">评分 / AI 评分</p>
          <h1>AI 评分复核</h1>
          <p class="lead">查看自动评分结果，及时处理需要教师复核的记录。</p>
        </div>
      </section>

      <section class="metric-strip review-strip" aria-label="评分汇总">
        <div class="metric"><span class="m-value">{{ summary.total }}</span><span class="m-label">全部评分</span></div>
        <div class="metric em"><span class="m-value">{{ summary.review }}</span><span class="m-label">需复核</span></div>
        <div class="metric"><span class="m-value">{{ summary.completed }}</span><span class="m-label">已完成</span></div>
      </section>

      <nav class="review-tabs" aria-label="复核视图切换">
        <button type="button" class="tab" :class="{ active: viewMode === 'grades' }" @click="switchMode('grades')">评分记录</button>
        <button type="button" class="tab" :class="{ active: viewMode === 'appeals' }" @click="switchMode('appeals')">
          待处理异议<span v-if="appealSummary.pending" class="tab-count">{{ appealSummary.pending }}</span>
        </button>
      </nav>

      <section v-if="viewMode === 'grades'" class="table-wrap" aria-label="AI 评分记录">
        <form class="toolbar filter-bar" @submit.prevent="applyFilters">
          <label class="searchbox" style="width: 260px;">
            <AppIcon name="search" :size="15" />
            <input v-model="filterStudent" type="search" class="input" placeholder="搜索学生 ID 或姓名" @keydown.enter.prevent="applyFilters" />
            <button v-if="filterStudent" type="button" class="clear" aria-label="清空搜索" @click="clearSearch">
              <AppIcon name="close" :size="13" />
            </button>
          </label>

          <label class="select" style="width: 140px;">
            <DsSelect v-model="filterKind" aria-label="评分类型" @change="applyFilters">
              <option value="">全部类型</option>
              <option value="assignment">作业</option>
              <option value="exam">考试</option>
            </DsSelect>
          </label>

          <label class="select" style="width: 150px;">
            <DsSelect v-model="filterStatus" aria-label="评分状态" @change="applyFilters">
              <option value="">全部状态</option>
              <option value="pending">等待中</option>
              <option value="queued">排队中</option>
              <option value="running">评分中</option>
              <option value="completed">已完成</option>
              <option value="review_required">需复核</option>
              <option value="system_error">系统错误</option>
            </DsSelect>
          </label>

          <button class="btn btn-secondary" type="button" @click="applyFilters">查询</button>
          <div class="grow"></div>
          <button v-if="hasFilters" type="button" class="btn btn-ghost btn-sm" @click="resetFilters">清除筛选</button>
        </form>

        <div v-if="loading" class="table-scroll" aria-label="正在加载评分记录">
          <div v-for="row in 5" :key="row" class="skeleton-row">
            <span v-for="cell in 7" :key="cell" class="skeleton"></span>
          </div>
        </div>

        <div v-else-if="error" class="error-panel" role="alert">
          <div class="grow"><div class="e-title">评分记录加载失败</div><div class="e-body">{{ error }}</div></div>
          <button type="button" class="btn btn-secondary btn-sm" @click="load">重新加载</button>
        </div>

        <template v-else-if="items.length">
          <div class="table-scroll">
            <table class="ds-table grade-table">
              <thead>
                <tr>
                  <th>学生信息</th>
                  <th>评分记录</th>
                  <th>分项得分</th>
                  <th class="cell-num">原始分</th>
                  <th class="cell-num">最终得分</th>
                  <th>评分状态</th>
                  <th>复核状态</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in items" :key="item.id">
                  <td>
                    <span class="cell-main">{{ item.student_name || '未知学生' }}</span>
                    <div class="cell-sub">学生 ID：{{ item.student_id ?? '-' }}</div>
                  </td>
                  <td>
                    <span class="cell-main">#{{ item.id }} · {{ item.submission_id ? '作业' : '考试' }}</span>
                    <div class="cell-sub">{{ modeMap[item.mode] || item.mode || '-' }}<template v-if="item.created_at"> · {{ formatDateTime(item.created_at) }}</template></div>
                  </td>
                  <td>
                    <div class="score-parts">
                      <span><small>F</small>{{ displayScore(item.functional_score) }}</span>
                      <span><small>A</small>{{ displayScore(item.algorithm_score) }}</span>
                      <span><small>R</small>{{ displayScore(item.robustness_score) }}</span>
                      <span><small>Q</small>{{ displayScore(item.quality_score) }}</span>
                    </div>
                  </td>
                  <td class="cell-num">
                    <span class="raw-score">{{ displayScore(item.raw_total) }}</span>
                    <div v-if="item.score_cap != null" class="cell-sub">上限 {{ item.score_cap }}</div>
                  </td>
                  <td class="cell-num final-score">{{ displayScore(item.final_score_100) }}</td>
                  <td><span class="badge" :class="statusBadgeClass(item.status)"><span class="dot"></span>{{ statusMap[item.status] || item.status }}</span></td>
                  <td><span class="badge" :class="item.needs_teacher_review ? 'badge-warning' : 'badge-neutral'"><span class="dot"></span>{{ item.needs_teacher_review ? '需复核' : '无需复核' }}</span></td>
                  <td class="col-actions"><router-link class="btn btn-ghost btn-sm row-action" :to="`${basePath}/${item.id}`">查看详情</router-link></td>
                </tr>
              </tbody>
            </table>
          </div>

          <TeacherPagination :current-page="page" :page-count="totalPages" :total="total" :page-size="pageSize" aria-label="评分记录分页" total-suffix="条记录" @change="goToPage" />
        </template>

        <div v-else class="empty">
          <div class="empty-mark"><AppIcon name="assignment" :size="20" /></div>
          <h3>{{ hasFilters ? '没有符合筛选条件的记录' : '暂无 AI 评分记录' }}</h3>
          <p>{{ hasFilters ? '可以调整搜索关键词或筛选条件后再试。' : '产生自动评分结果后，记录会显示在这里。' }}</p>
          <div v-if="hasFilters" class="empty-actions"><button type="button" class="btn btn-secondary btn-sm" @click="resetFilters">清除筛选</button></div>
        </div>
      </section>

      <section v-else class="table-wrap" aria-label="成绩异议">
        <form class="toolbar filter-bar" @submit.prevent="loadAppeals">
          <label class="select" style="width: 160px;">
            <DsSelect v-model="appealStatus" aria-label="异议状态" @change="loadAppeals">
              <option value="pending">待处理</option>
              <option value="regrading">重评中</option>
              <option value="">全部状态</option>
              <option value="adjusted">已改分</option>
              <option value="upheld">维持原分</option>
              <option value="withdrawn">已撤回</option>
            </DsSelect>
          </label>
          <button class="btn btn-secondary" type="button" @click="loadAppeals">刷新</button>
          <div class="grow"></div>
          <span class="cell-sub">重评完成前异议保持处理中，不会提前关闭</span>
        </form>

        <div v-if="appealLoading" class="table-scroll" aria-label="正在加载成绩异议">
          <div v-for="row in 4" :key="row" class="skeleton-row">
            <span v-for="cell in 6" :key="cell" class="skeleton"></span>
          </div>
        </div>

        <div v-else-if="appealError" class="error-panel" role="alert">
          <div class="grow"><div class="e-title">成绩异议加载失败</div><div class="e-body">{{ appealError }}</div></div>
          <button type="button" class="btn btn-secondary btn-sm" @click="loadAppeals">重新加载</button>
        </div>

        <template v-else-if="appeals.length">
          <div class="table-scroll">
            <table class="ds-table grade-table">
              <thead>
                <tr>
                  <th>学生</th>
                  <th>成绩来源</th>
                  <th class="cell-num">申请时分数</th>
                  <th>申请说明</th>
                  <th>状态</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="appeal in appeals" :key="appeal.id">
                  <td>
                    <span class="cell-main">{{ appeal.student_name || '未知学生' }}</span>
                    <div class="cell-sub">{{ appeal.student_no || `ID ${appeal.student_id}` }}</div>
                  </td>
                  <td>
                    <span class="cell-main">{{ targetKindMap[appeal.target_kind] || appeal.target_kind }} #{{ appeal.target_id }}</span>
                    <div class="cell-sub">{{ appeal.title || '—' }}<template v-if="appeal.created_at"> · {{ formatDateTime(appeal.created_at) }}</template></div>
                  </td>
                  <td class="cell-num">{{ displayScore(appeal.score_before) }}</td>
                  <td class="appeal-reason">{{ appeal.reason }}</td>
                  <td><span class="badge" :class="appealBadgeClass(appeal.status)"><span class="dot"></span>{{ appealStatusMap[appeal.status] || appeal.status }}</span></td>
                  <td class="col-actions">
                    <button type="button" class="btn btn-ghost btn-sm" @click="openHandle(appeal, 'uphold')">维持</button>
                    <button type="button" class="btn btn-ghost btn-sm" @click="openHandle(appeal, 'adjust')">改分</button>
                    <button type="button" class="btn btn-ghost btn-sm" @click="openHandle(appeal, 'regrade')">重评</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="cell-sub">共 {{ appealTotal }} 条</p>
        </template>

        <div v-else class="empty">
          <div class="empty-mark"><AppIcon name="clipboard" :size="20" /></div>
          <h3>当前没有待处理的成绩异议</h3>
          <p>学生提交异议后会出现在这里，处理记录与改分结果会同时通知学生。</p>
        </div>
      </section>

      <div v-if="handleTarget" class="handle-mask" role="dialog" aria-modal="true" aria-label="处理成绩异议">
        <div class="handle-card">
          <header>
            <h2>{{ handleActionMap[handleAction] }}成绩异议 #{{ handleTarget.id }}</h2>
            <p class="cell-sub">{{ targetKindMap[handleTarget.target_kind] }} #{{ handleTarget.target_id }} · 申请时分数 {{ displayScore(handleTarget.score_before) }}</p>
          </header>
          <p class="handle-reason">{{ handleTarget.reason }}</p>
          <label v-if="handleAction === 'adjust'" class="field">
            <span>改分后总分（0-100）</span>
            <input v-model.number="adjustScore" type="number" min="0" max="100" step="0.5" class="input" />
          </label>
          <label class="field">
            <span>处理说明（必填）</span>
            <textarea v-model="handleComment" class="input" rows="3" placeholder="说明维持原因、改分依据或重评理由"></textarea>
          </label>
          <p v-if="handleError" class="setup-error" role="alert">{{ handleError }}</p>
          <footer class="handle-actions">
            <button type="button" class="btn btn-secondary btn-sm" :disabled="handleBusy" @click="closeHandle">取消</button>
            <button type="button" class="btn btn-primary btn-sm" :disabled="handleBusy" @click="submitHandle">
              {{ handleBusy ? '处理中…' : '确认处理' }}
            </button>
          </footer>
        </div>
      </div>
    </section>
  </AppLayout>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth.js'
import { aiGradingAPI } from '../../api/aiGrading.js'
import { appealsAPI } from '../../api/appeals.js'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'
import { formatDateTime } from '../../utils/format.js'

const route = useRoute()
const auth = useAuthStore()
const basePath = computed(() => auth.isAdmin ? '/admin/ai-grading' : '/teacher/ai-grading')
const items = ref([])
const loading = ref(false)
const error = ref('')
const page = ref(1)
const pageSize = 10
const total = ref(0)
const filterKind = ref(typeof route.query.kind === 'string' ? route.query.kind : '')
const filterStatus = ref(typeof route.query.status === 'string' ? route.query.status : '')
const filterStudent = ref('')
const summary = reactive({ total: 0, review: 0, completed: 0 })

// ── 待处理异议（批次 2）：教师侧不新建页面，在本页签内处理 ──
const viewMode = ref(route.query.appeal ? 'appeals' : 'grades')
const appeals = ref([])
const appealLoading = ref(false)
const appealError = ref('')
const appealStatus = ref('pending')
const appealTotal = ref(0)
const appealSummary = reactive({ pending: 0 })
const handleTarget = ref(null)
const handleAction = ref('uphold')
const handleComment = ref('')
const adjustScore = ref(null)
const handleBusy = ref(false)
const handleError = ref('')

const targetKindMap = { submission: '作业提交', exam_answer: '考试编程题', lab_submission: '实验提交', practice_submission: '自主练习' }
const appealStatusMap = { pending: '待处理', regrading: '重评中', adjusted: '已改分', upheld: '维持原分', rejected: '未受理', withdrawn: '已撤回' }
const handleActionMap = { uphold: '维持', adjust: '改分', regrade: '重评' }

const statusMap = {
  pending: '等待中', queued: '排队中', running: '评分中', completed: '已完成',
  review_required: '需复核', system_error: '系统错误',
}
const modeMap = { active: '正式评分', shadow: '影子评分', legacy: '传统评分' }
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const hasFilters = computed(() => Boolean(filterKind.value || filterStatus.value || filterStudent.value.trim()))

function statusBadgeClass(status) {
  if (status === 'completed') return 'badge-success'
  if (['pending', 'queued'].includes(status)) return 'badge-warning'
  if (status === 'running') return 'badge-info'
  if (status === 'review_required') return 'badge-accent'
  if (status === 'system_error') return 'badge-danger'
  return 'badge-neutral'
}

function buildParams() {
  const params = { page: page.value, page_size: pageSize }
  if (filterKind.value) params.kind = filterKind.value
  if (filterStatus.value) params.status = filterStatus.value
  const studentFilter = filterStudent.value.trim()
  if (studentFilter) {
    if (/^\d+$/.test(studentFilter)) params.student_id = Number(studentFilter)
    else params.student_name = studentFilter
  }
  return params
}

const requestGuard = createLatestRequestGuard()

async function load() {
  const token = requestGuard.begin()
  loading.value = true
  error.value = ''
  try {
    const res = await aiGradingAPI.listGrades(buildParams())
    if (!requestGuard.isLatest(token)) return
    items.value = res.data.items || []
    total.value = res.data.total || 0
  } catch (e) {
    if (!requestGuard.isLatest(token)) return
    error.value = e.response?.data?.detail?.message || e.message || '加载失败，请稍后重试'
  } finally {
    if (requestGuard.isLatest(token)) loading.value = false
  }
}

async function loadSummary() {
  try {
    const [all, review, completed] = await Promise.all([
      aiGradingAPI.listGrades({ page: 1, page_size: 1 }),
      aiGradingAPI.listGrades({ page: 1, page_size: 1, status: 'review_required' }),
      aiGradingAPI.listGrades({ page: 1, page_size: 1, status: 'completed' }),
    ])
    summary.total = all.data.total || 0
    summary.review = review.data.total || 0
    summary.completed = completed.data.total || 0
  } catch {
    summary.total = total.value
  }
}

function applyFilters() { page.value = 1; load() }
function clearSearch() { filterStudent.value = ''; applyFilters() }
function resetFilters() { filterStudent.value = ''; filterKind.value = ''; filterStatus.value = ''; applyFilters() }
function goToPage(nextPage) { if (nextPage < 1 || nextPage > totalPages.value || nextPage === page.value) return; page.value = nextPage; load() }
function displayScore(value) { return value == null ? '—' : value }

// ── 异议处理 ──
function appealBadgeClass(status) {
  if (status === 'adjusted') return 'badge-success'
  if (status === 'regrading') return 'badge-info'
  if (status === 'pending') return 'badge-warning'
  if (status === 'rejected') return 'badge-danger'
  return 'badge-neutral'
}

function switchMode(mode) {
  if (viewMode.value === mode) return
  viewMode.value = mode
  if (mode === 'appeals' && appeals.value.length === 0 && !appealLoading.value) loadAppeals()
}

const appealGuard = createLatestRequestGuard()

async function loadAppeals() {
  const token = appealGuard.begin()
  appealLoading.value = true
  appealError.value = ''
  try {
    const params = { page: 1, page_size: 20 }
    if (appealStatus.value) params.status = appealStatus.value
    const res = await appealsAPI.list(params)
    if (!appealGuard.isLatest(token)) return
    appeals.value = res.data.items || []
    appealTotal.value = res.data.total || 0
    if (appealStatus.value === 'pending') appealSummary.pending = res.data.total || 0
  } catch (e) {
    if (!appealGuard.isLatest(token)) return
    appealError.value = e.response?.data?.detail?.message || e.message || '加载失败，请稍后重试'
  } finally {
    if (appealGuard.isLatest(token)) appealLoading.value = false
  }
}

function openHandle(appeal, action) {
  handleTarget.value = appeal
  handleAction.value = action
  handleComment.value = ''
  adjustScore.value = null
  handleError.value = ''
}

function closeHandle() {
  if (handleBusy.value) return
  handleTarget.value = null
  handleError.value = ''
}

function handleErrorMessage(e) {
  const detail = e.response?.data?.detail
  if (e.response?.status === 409 && detail?.code === 'GRADING_IN_PROGRESS') return '判题仍在进行，请稍后重试'
  if (e.response?.status === 409 && detail?.code === 'AI_GRADING_IN_PROGRESS') return 'AI 评分仍在进行，请稍后重试'
  if (e.response?.status === 409 && detail?.code === 'MANUAL_SCORE_FINAL') return '人工最终分已生效，请改用人工评分通道'
  return detail?.message || e.message || '处理失败，请稍后重试'
}

async function submitHandle() {
  if (!handleTarget.value || handleBusy.value) return
  const payload = { action: handleAction.value, comment: handleComment.value.trim() }
  if (handleAction.value === 'adjust') {
    if (!payload.comment) { handleError.value = '改分必须填写处理说明'; return }
    if (adjustScore.value == null || Number.isNaN(adjustScore.value)) { handleError.value = '请填写改分后的总分'; return }
    payload.final_score_100 = Number(adjustScore.value)
  }
  handleBusy.value = true
  handleError.value = ''
  try {
    await appealsAPI.handle(handleTarget.value.id, payload)
    handleTarget.value = null
    await loadAppeals()
  } catch (e) {
    handleError.value = handleErrorMessage(e)
  } finally {
    handleBusy.value = false
  }
}

onMounted(() => { load(); loadSummary(); if (viewMode.value === 'appeals') loadAppeals() })
onBeforeUnmount(() => { requestGuard.invalidate(); appealGuard.invalidate() })
</script>

<style scoped>
.ai-grading-review { display: flex; flex-direction: column; gap: var(--space-5); min-width: 0; }
.review-strip { grid-template-columns: repeat(3, 1fr); }
.skeleton-row { display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--border); }
.skeleton-row .skeleton { height: 15px; }

.score-parts { display: grid; grid-template-columns: repeat(4, minmax(32px, 1fr)); gap: 5px; }
.score-parts span { padding: 4px 3px; border-radius: var(--radius-sm); background: var(--surface-sunken); text-align: center; color: var(--fg); font-size: var(--text-sm); font-family: var(--font-mono); }
.score-parts small { display: block; margin-bottom: 2px; color: var(--faint); font-size: 10px; }
.final-score { font-weight: 600; }
.row-action { text-decoration: none; }

/* ── 待处理异议（批次 2） ── */
.review-tabs { display: flex; gap: var(--space-2); }
.tab { display: inline-flex; align-items: center; gap: var(--space-2); height: 32px; padding: 0 var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); color: var(--muted); font-size: var(--text-base); cursor: pointer; }
.tab.active { border-color: var(--accent); background: var(--accent-soft); color: var(--fg); font-weight: 600; }
.tab-count { padding: 0 6px; border-radius: var(--radius-sm); background: var(--accent); color: var(--surface); font-size: var(--text-sm); }
.appeal-reason { max-width: 320px; color: var(--muted); font-size: var(--text-sm); }
.handle-mask { position: fixed; inset: 0; z-index: 40; display: grid; place-items: center; padding: var(--space-4); background: rgba(15, 23, 42, 0.45); }
.handle-card { width: min(520px, 100%); display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-5); border-radius: var(--radius-lg); background: var(--surface); }
.handle-card h2 { margin: 0; font-size: var(--text-lg); }
.handle-reason { margin: 0; padding: var(--space-3); border-radius: var(--radius-md); background: var(--surface-sunken); color: var(--muted); font-size: var(--text-sm); }
.handle-actions { display: flex; justify-content: flex-end; gap: var(--space-2); }
.field { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-sm); color: var(--muted); }
.setup-error { margin: 0; color: var(--danger); font-size: var(--text-sm); }

@media (max-width: 560px) {
  .review-strip { grid-template-columns: 1fr; }
}
</style>
