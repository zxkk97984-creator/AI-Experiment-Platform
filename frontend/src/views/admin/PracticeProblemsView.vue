<script setup>
// 管理员练习题库列表：筛选、发布 / 归档 / 删除草稿、从已有作业复制题目。
// 六态：加载 / 空数据 / 失败 / 排队（判题队列积压）/ 额度耗尽 / 权限不足。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import DsSelect from '../../components/ui/DsSelect.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import StudentPagination from '../../components/student/StudentPagination.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { listErrorVisible, usePracticeLoad } from '../../components/practice/usePracticeLoad.js'
import {
  DIFFICULTY_OPTIONS, difficultyMeta, gradingModeMeta, listOf, practiceErrorMessage, problemStatusMeta,
} from '../../components/practice/practiceHelpers.js'
import { adminPracticeAPI } from '../../api/practice.js'
import { assignmentsAPI } from '../../api/assignments.js'
import { useAppStore } from '../../stores/app.js'

const router = useRouter()
const app = useAppStore()

const PAGE_SIZE = 10
const problems = ref([])
const total = ref(0)
const summary = ref({})
const page = ref(1)
const filters = ref({ search: '', status: '', difficulty: '' })
const meta = ref({})
const busyId = ref(null)
const pendingAction = ref(null)

const { loading, error, forbidden, quotaExhausted, load } = usePracticeLoad(async () => {
  const res = await adminPracticeAPI.listProblems({
    search: filters.value.search || undefined,
    status: filters.value.status || undefined,
    difficulty: filters.value.difficulty || undefined,
    page: page.value,
    page_size: PAGE_SIZE,
  })
  problems.value = listOf(res.data)
  total.value = Number(res.data?.total ?? problems.value.length)
  summary.value = res.data?.summary || {}
  meta.value = res.data?.meta || {}
  return res
}, { fallbackMessage: '题库加载失败，请稍后重试' })

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const metrics = computed(() => [
  { label: '全部题目', value: summary.value.total ?? total.value },
  { label: '已发布', value: summary.value.published ?? 0 },
  { label: '草稿', value: summary.value.draft ?? 0 },
  { label: '已归档', value: summary.value.archived ?? 0 },
])
const queue = computed(() => meta.value.queue || {})
const hasFilters = computed(() => Boolean(filters.value.search || filters.value.status || filters.value.difficulty))

function setFilter(key, value) {
  filters.value[key] = value
  page.value = 1
  load()
}

function clearFilters() {
  filters.value = { search: '', status: '', difficulty: '' }
  page.value = 1
  load()
}

function changePage(next) {
  page.value = next
  load()
}

function askPublish(problem) {
  pendingAction.value = { kind: 'publish', problem }
}

function askArchive(problem) {
  pendingAction.value = { kind: 'archive', problem }
}

function askDelete(problem) {
  pendingAction.value = { kind: 'delete', problem }
}

const pendingTitle = computed(() => {
  const action = pendingAction.value
  if (!action) return ''
  return { publish: '发布练习题', archive: '归档练习题', delete: '删除草稿题' }[action.kind]
})

const pendingMessage = computed(() => {
  const action = pendingAction.value
  if (!action) return ''
  if (action.kind === 'publish') return `发布后学生即可在练习中心看到《${action.problem.title}》。`
  if (action.kind === 'archive') return `归档后学生不再看到《${action.problem.title}》，历史提交与成绩仍然保留。`
  return `将删除草稿题《${action.problem.title}》，仅草稿且无人提交的题目可以删除。`
})

async function confirmPending() {
  const action = pendingAction.value
  if (!action) return
  busyId.value = action.problem.id
  try {
    if (action.kind === 'publish') await adminPracticeAPI.publishProblem(action.problem.id)
    else if (action.kind === 'archive') await adminPracticeAPI.archiveProblem(action.problem.id)
    else await adminPracticeAPI.deleteProblem(action.problem.id)
    app.showToast({ publish: '已发布', archive: '已归档', delete: '已删除' }[action.kind], 'success')
    pendingAction.value = null
    await load()
  } catch (e) {
    app.showToast(practiceErrorMessage(e, '操作失败，请稍后重试'), 'error')
  } finally {
    busyId.value = null
  }
}

/* ── 从已有作业复制题目 ─────────────────────────────────────────────── */
const copyOpen = ref(false)
const copyAssignments = ref([])
const copyQuestions = ref([])
const copyLoading = ref(false)
const copySaving = ref(false)
const copyError = ref('')
const copyForm = ref({ assignment_id: '', question_id: '', title: '' })

async function openCopy() {
  copyOpen.value = true
  copyError.value = ''
  copyForm.value = { assignment_id: '', question_id: '', title: '' }
  copyQuestions.value = []
  copyLoading.value = true
  try {
    const res = await assignmentsAPI.list({ page: 1, page_size: 50 })
    copyAssignments.value = listOf(res.data)
  } catch (e) {
    copyError.value = practiceErrorMessage(e, '作业列表加载失败')
  } finally {
    copyLoading.value = false
  }
}

async function onCopyAssignmentChange(value) {
  copyForm.value.assignment_id = Number(value) || ''
  copyForm.value.question_id = ''
  copyQuestions.value = []
  if (!copyForm.value.assignment_id) return
  copyLoading.value = true
  copyError.value = ''
  try {
    const res = await assignmentsAPI.getQuestions(copyForm.value.assignment_id)
    copyQuestions.value = Array.isArray(res.data) ? res.data : listOf(res.data)
  } catch (e) {
    copyError.value = practiceErrorMessage(e, '作业题目加载失败')
  } finally {
    copyLoading.value = false
  }
}

function onCopyQuestionChange(value) {
  copyForm.value.question_id = Number(value) || ''
  const question = copyQuestions.value.find((item) => item.id === copyForm.value.question_id)
  if (question && !copyForm.value.title) copyForm.value.title = question.title
}

async function submitCopy() {
  if (!copyForm.value.assignment_id || !copyForm.value.question_id) return
  copySaving.value = true
  copyError.value = ''
  try {
    const res = await adminPracticeAPI.copyFromAssignment({
      assignment_id: copyForm.value.assignment_id,
      question_id: copyForm.value.question_id,
      title: copyForm.value.title || undefined,
    })
    app.showToast('已复制为草稿题，请确认内容后发布', 'success')
    copyOpen.value = false
    const id = res.data?.id
    if (id) router.push(`/admin/practice/problems/${id}/edit`)
    else await load()
  } catch (e) {
    copyError.value = practiceErrorMessage(e, '复制失败，请稍后重试')
  } finally {
    copySaving.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="admin-problems">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE ADMIN</p>
          <h1>练习题库</h1>
          <p class="lead subtitle">全平台公共题库：录题、配置评分标准、发布与归档</p>
        </div>
        <div class="ph-actions">
          <button type="button" class="btn btn-secondary" @click="openCopy">
            <AppIcon name="copy" :size="14" />从作业复制
          </button>
          <button type="button" class="btn btn-primary" @click="router.push('/admin/practice/problems/new')">
            <AppIcon name="plus" :size="14" />新建题目
          </button>
        </div>
      </section>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        练习题库仅管理员可维护；教师只能查看只读排行榜。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        AI 预算或提交额度已用尽，题目仍可编辑与发布，新的 AI 生成任务需要等额度恢复。
      </PracticeNotice>

      <PracticeNotice
        v-if="!forbidden && Number(queue.queued) > 0"
        tone="warning"
        :title="`判题队列积压 ${queue.queued} 项`"
      >
        练习使用独立队列，不会挤占正式作业与考试的判题容量；学生端会看到「排队中（第 N 位）」。
      </PracticeNotice>

      <div v-if="!forbidden" class="metric-strip" aria-label="题库概览">
        <div v-for="metric in metrics" :key="metric.label" class="metric">
          <span class="m-value">{{ metric.value }}</span>
          <span class="m-label">{{ metric.label }}</span>
        </div>
      </div>

      <form v-if="!forbidden" class="filter-bar" role="search" @submit.prevent="load">
        <div class="searchbox grow" :class="{ 'has-value': filters.search }">
          <AppIcon name="search" :size="15" />
          <input
            v-model="filters.search"
            class="input"
            type="search"
            placeholder="搜索题目标题"
            aria-label="搜索练习题"
          />
        </div>
        <button type="submit" class="btn btn-secondary">搜索</button>
        <DsSelect :model-value="filters.status" host-class="filter-select" aria-label="按状态筛选" @update:model-value="setFilter('status', $event)">
          <option value="">全部状态</option>
          <option value="draft">草稿</option>
          <option value="published">已发布</option>
          <option value="archived">已归档</option>
        </DsSelect>
        <DsSelect :model-value="filters.difficulty" host-class="filter-select" aria-label="按难度筛选" @update:model-value="setFilter('difficulty', $event)">
          <option v-for="option in DIFFICULTY_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
        </DsSelect>
        <button v-if="hasFilters" type="button" class="btn btn-ghost" @click="clearFilters">清空筛选</button>
      </form>

      <DashboardAsyncState
        :loading="loading"
        :error="listErrorVisible(error, forbidden)"
        :empty="!problems.length"
        :empty-title="hasFilters ? '没有符合条件的题目' : '题库还没有题目'"
        :empty-body="hasFilters ? '尝试放宽筛选条件。' : '点击「新建题目」或「从作业复制」录入第一道题。'"
        @retry="load"
      >
        <div v-if="problems.length" class="table-wrap">
          <div class="table-scroll">
            <table class="ds-table">
              <caption class="sr-only">练习题库列表</caption>
              <thead>
                <tr>
                  <th scope="col">题目</th>
                  <th scope="col">难度</th>
                  <th scope="col">状态</th>
                  <th scope="col">判题方式</th>
                  <th scope="col">通过 / 尝试</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="problem in problems" :key="problem.id">
                  <td class="title-cell">
                    <strong>{{ problem.title }}</strong>
                    <span v-if="(problem.tags || []).length" class="tag-line">{{ (problem.tags || []).map((t) => `#${t}`).join(' ') }}</span>
                  </td>
                  <td><UiStatusPill :tone="difficultyMeta(problem.difficulty).tone" :label="difficultyMeta(problem.difficulty).label" /></td>
                  <td><UiStatusPill :tone="problemStatusMeta(problem.status).tone" :label="problemStatusMeta(problem.status).label" /></td>
                  <td>{{ gradingModeMeta(problem.grading_mode).label }}</td>
                  <td class="mono">{{ problem.distinct_solvers ?? 0 }} / {{ problem.total_attempts ?? 0 }}</td>
                  <td class="actions">
                    <button type="button" class="btn btn-ghost btn-sm" @click="router.push(`/admin/practice/problems/${problem.id}/edit`)">编辑</button>
                    <button
                      v-if="problem.status !== 'published'"
                      type="button"
                      class="btn btn-secondary btn-sm"
                      :disabled="busyId === problem.id"
                      @click="askPublish(problem)"
                    >发布</button>
                    <button
                      v-if="problem.status === 'published'"
                      type="button"
                      class="btn btn-secondary btn-sm"
                      :disabled="busyId === problem.id"
                      @click="askArchive(problem)"
                    >归档</button>
                    <button
                      v-if="problem.status === 'draft'"
                      type="button"
                      class="btn btn-danger btn-sm"
                      :disabled="busyId === problem.id"
                      @click="askDelete(problem)"
                    >删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </DashboardAsyncState>

      <StudentPagination
        v-if="!loading && problems.length && pageCount > 1"
        :current-page="page"
        :page-count="pageCount"
        :total="total"
        :page-size="PAGE_SIZE"
        aria-label="题库分页"
        total-suffix="道题"
        @change="changePage"
      />

      <ConfirmDialog
        v-if="pendingAction"
        :title="pendingTitle"
        :message="pendingMessage"
        :confirm-text="pendingAction.kind === 'delete' ? '删除' : '确认'"
        :danger="pendingAction.kind === 'delete'"
        :busy="busyId === pendingAction.problem.id"
        @confirm="confirmPending"
        @cancel="pendingAction = null"
      />

      <div v-if="copyOpen" class="modal-backdrop" @click.self="copyOpen = false">
        <section class="modal" role="dialog" aria-modal="true" aria-label="从已有作业复制题目">
          <header class="modal-head"><h2>从已有作业复制题目</h2></header>
          <div class="modal-body">
            <p class="copy-hint">复制会新建一条草稿题，原作业不受影响；发布前请确认判题用例与评分标准。</p>
            <label class="field">
              <span class="field-label">选择作业</span>
              <DsSelect :model-value="copyForm.assignment_id" aria-label="选择作业" @update:model-value="onCopyAssignmentChange">
                <option value="">请选择作业</option>
                <option v-for="item in copyAssignments" :key="item.id" :value="item.id">{{ item.title }}</option>
              </DsSelect>
            </label>
            <label class="field">
              <span class="field-label">选择题目</span>
              <DsSelect
                :model-value="copyForm.question_id"
                aria-label="选择题目"
                :disabled="!copyForm.assignment_id"
                @update:model-value="onCopyQuestionChange"
              >
                <option value="">请选择题目</option>
                <option v-for="item in copyQuestions" :key="item.id" :value="item.id">{{ item.title }}</option>
              </DsSelect>
            </label>
            <label class="field">
              <span class="field-label">题目名称（可修改）</span>
              <input v-model="copyForm.title" class="input" type="text" placeholder="留空则使用原题目标题" />
            </label>
            <p v-if="copyLoading" class="copy-hint">正在加载…</p>
            <p v-if="copyError" class="copy-error" role="alert">{{ copyError }}</p>
          </div>
          <footer class="modal-foot">
            <button type="button" class="btn btn-ghost" @click="copyOpen = false">取消</button>
            <button
              type="button"
              class="btn btn-primary"
              :disabled="copySaving || !copyForm.assignment_id || !copyForm.question_id"
              @click="submitCopy"
            >
              {{ copySaving ? '复制中…' : '复制为草稿' }}
            </button>
          </footer>
        </section>
      </div>
    </section>
  </AppLayout>
</template>

<style scoped>
.admin-problems { display: flex; flex-direction: column; gap: var(--space-4); }
.filter-bar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.filter-bar .searchbox { min-width: 220px; }
.filter-select { width: 176px; }
.title-cell { white-space: normal; max-width: 360px; }
.title-cell strong { display: block; overflow-wrap: anywhere; }
.tag-line { display: block; margin-top: var(--space-1); color: var(--faint); font-size: var(--text-sm); }
.actions { display: flex; gap: var(--space-2); }
.copy-hint { margin: 0 0 var(--space-3); color: var(--muted); font-size: var(--text-md); }
.copy-error { margin: var(--space-3) 0 0; color: var(--danger); font-size: var(--text-md); }
.modal-body { display: flex; flex-direction: column; gap: var(--space-3); }
</style>
