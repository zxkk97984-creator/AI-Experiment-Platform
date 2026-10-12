<script setup>
// 学生自主练习中心：题库筛选（难度 / 标签 / 完成状态 + 搜索）。
// 六态：加载 / 空数据 / 失败 / 排队（在途提交）/ 额度耗尽 / 权限不足。
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import DsSelect from '../../components/ui/DsSelect.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import StudentPagination from '../../components/student/StudentPagination.vue'
import PracticeProblemCard from '../../components/practice/PracticeProblemCard.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { listErrorVisible, usePracticeLoad } from '../../components/practice/usePracticeLoad.js'
import { COMPLETION_OPTIONS, DIFFICULTY_OPTIONS, listOf } from '../../components/practice/practiceHelpers.js'
import { practiceAPI } from '../../api/practice.js'

const router = useRouter()

const PAGE_SIZE = 12
const problems = ref([])
const total = ref(0)
const page = ref(1)
const tags = ref([])
const meta = ref({})
const filters = ref({ search: '', difficulty: '', tag: '', completion: 'all' })
let searchTimer = null

const { loading, error, forbidden, quotaExhausted, load } = usePracticeLoad(async () => {
  const res = await practiceAPI.listProblems({
    search: filters.value.search || undefined,
    difficulty: filters.value.difficulty || undefined,
    tag: filters.value.tag || undefined,
    completion: filters.value.completion === 'all' ? undefined : filters.value.completion,
    page: page.value,
    page_size: PAGE_SIZE,
  })
  problems.value = listOf(res.data)
  total.value = Number(res.data?.total ?? problems.value.length)
  meta.value = res.data?.meta || {}
  // 标签选项由题库数据聚合，不额外新增接口
  const collected = new Set(tags.value)
  problems.value.forEach((item) => (item.tags || []).forEach((tag) => collected.add(tag)))
  tags.value = [...collected].sort()
  return res
})

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const activeSubmissions = computed(() => meta.value.active_submissions || [])
const emptyTitle = computed(() => (hasFilters.value ? '没有符合条件的题目' : '题库暂时没有已发布的题目'))
const emptyBody = computed(() => (hasFilters.value ? '尝试放宽筛选条件或清空搜索关键词。' : '管理员发布练习题后会显示在这里。'))
const hasFilters = computed(() =>
  Boolean(filters.value.search || filters.value.difficulty || filters.value.tag || filters.value.completion !== 'all'))

function onSearchInput() {
  if (searchTimer) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    page.value = 1
    load()
  }, 300)
}

function setFilter(key, value) {
  if (filters.value[key] === value) return
  filters.value[key] = value
  page.value = 1
  load()
}

function clearFilters() {
  filters.value = { search: '', difficulty: '', tag: '', completion: 'all' }
  page.value = 1
  load()
}

function changePage(next) {
  page.value = next
  load()
}

function openProblem(problem) {
  router.push(`/student/practice/problems/${problem.id}`)
}

watch(() => filters.value.completion, () => { page.value = 1 })

onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="practice-center">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE</p>
          <h1>自主练习</h1>
          <p class="lead subtitle">全平台公共题库，按难度与标签挑选题目，提交后查看测试与 AI 反馈</p>
        </div>
        <div class="ph-actions">
          <span class="count-pill"><span aria-hidden="true"></span>共 {{ total }} 道题</span>
          <button type="button" class="btn btn-secondary" @click="router.push('/student/practice/mine')">
            <AppIcon name="clipboard" :size="14" />我的练习
          </button>
        </div>
      </section>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        仅学生账号可以使用自主练习，请联系管理员确认账号角色。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        今日提交额度或 AI 预算已用尽，明天 0 点后恢复；已完成的成绩与排名不受影响。
      </PracticeNotice>

      <PracticeNotice
        v-for="item in activeSubmissions"
        :key="item.id"
        :tone="item.grading_status === 'queued' ? 'warning' : 'info'"
        :title="item.grading_status === 'queued' ? `《${item.problem_title}》排队中（第 ${item.queue_position || 1} 位）` : `《${item.problem_title}》判题中`"
      >
        <router-link class="notice-link" :to="`/student/practice/submissions/${item.id}`">查看进度</router-link>
      </PracticeNotice>

      <form v-if="!forbidden" class="filter-bar" role="search" @submit.prevent="load">
        <div class="searchbox grow" :class="{ 'has-value': filters.search }">
          <AppIcon name="search" :size="15" />
          <input
            v-model="filters.search"
            class="input"
            type="search"
            placeholder="搜索题目标题或描述"
            aria-label="搜索练习题"
            @input="onSearchInput"
          />
        </div>
        <DsSelect
          :model-value="filters.difficulty"
          aria-label="按难度筛选"
          host-class="filter-select"
          @update:model-value="setFilter('difficulty', $event)"
        >
          <option v-for="option in DIFFICULTY_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
        </DsSelect>
        <DsSelect
          :model-value="filters.completion"
          aria-label="按完成状态筛选"
          host-class="filter-select"
          @update:model-value="setFilter('completion', $event)"
        >
          <option v-for="option in COMPLETION_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
        </DsSelect>
        <DsSelect
          v-if="tags.length"
          :model-value="filters.tag"
          aria-label="按标签筛选"
          host-class="filter-select"
          @update:model-value="setFilter('tag', $event)"
        >
          <option value="">全部标签</option>
          <option v-for="tag in tags" :key="tag" :value="tag">#{{ tag }}</option>
        </DsSelect>
        <button v-if="hasFilters" type="button" class="btn btn-ghost" @click="clearFilters">清空筛选</button>
      </form>

      <DashboardAsyncState
        :loading="loading"
        :error="listErrorVisible(error, forbidden)"
        :empty="!problems.length"
        :empty-title="emptyTitle"
        :empty-body="emptyBody"
        @retry="load"
      >
        <div v-if="problems.length" class="problem-grid">
          <PracticeProblemCard
            v-for="problem in problems"
            :key="problem.id"
            :problem="problem"
            @open="openProblem"
          />
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
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-center { display: flex; flex-direction: column; gap: var(--space-4); }
.count-pill { display: inline-flex; align-items: center; gap: var(--space-2); padding: var(--space-1) var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-full); background: var(--surface); color: var(--muted); font-size: var(--text-sm); }
.count-pill span { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.filter-bar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.filter-bar .searchbox { min-width: 220px; }
.filter-select { width: 176px; }
.problem-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--space-4); }
.notice-link { color: var(--accent); font-weight: 600; }
@media (max-width: 640px) {
  .problem-grid { grid-template-columns: 1fr; }
  .filter-bar .searchbox { min-width: 100%; }
}
</style>
