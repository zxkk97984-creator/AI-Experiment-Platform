<script setup>
// 教师只读练习排行榜：全平台榜 / 班内榜 × 普通测试榜 / AI 综合榜。
// 只读页面：不提供任何写操作；教师的练习写接口在后端一律 403。
// 六态：加载 / 空数据（还没有人完成）/ 失败 / 排队（队列积压）/ 额度耗尽 / 权限不足。
import { computed, onMounted, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import DsSelect from '../../components/ui/DsSelect.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import PracticeLeaderboardTable from '../../components/practice/PracticeLeaderboardTable.vue'
import { listErrorVisible, usePracticeLoad } from '../../components/practice/usePracticeLoad.js'
import { BOARD_OPTIONS, listOf } from '../../components/practice/practiceHelpers.js'
import { practiceAPI } from '../../api/practice.js'
import { offeringsAPI } from '../../api/offerings.js'
import { formatDateTime } from '../../utils/format.js'

const SCOPE_OPTIONS = [
  { value: 'platform', label: '全平台榜' },
  { value: 'offering', label: '班内榜' },
]

const scope = ref('platform')
const board = ref('deterministic')
const scopeId = ref('')
const offerings = ref([])
const items = ref([])
const total = ref(0)
const meta = ref({})

const { loading, error, forbidden, quotaExhausted, load } = usePracticeLoad(async () => {
  const res = await practiceAPI.leaderboard({
    scope: scope.value,
    scope_id: scope.value === 'offering' && scopeId.value ? scopeId.value : undefined,
    board: board.value,
  })
  items.value = listOf(res.data)
  total.value = Number(res.data?.total ?? items.value.length)
  meta.value = res.data?.meta || {}
  return res
}, { fallbackMessage: '排行榜加载失败，请稍后重试' })

const queue = computed(() => meta.value.queue || {})
const scopeLabel = computed(() => SCOPE_OPTIONS.find((item) => item.value === scope.value)?.label || '')
const boardLabel = computed(() => BOARD_OPTIONS.find((item) => item.value === board.value)?.label || '')
const generatedAt = computed(() => meta.value.generated_at || meta.value.recalculated_at || '')
const awaitingScope = computed(() => scope.value === 'offering' && !scopeId.value)

async function loadOfferings() {
  try {
    const res = await offeringsAPI.list({ page: 1, page_size: 50 })
    offerings.value = listOf(res.data)
    if (offerings.value.length === 1) scopeId.value = offerings.value[0].id
  } catch {
    // 班级列表失败不阻塞全平台榜；切到班内榜时会提示先选择班级
    offerings.value = []
  }
}

function onScopeChange(value) {
  scope.value = value
  if (value === 'platform') load()
  else if (scopeId.value) load()
}

function onBoardChange(value) {
  board.value = value
  load()
}

function onOfferingChange(value) {
  scopeId.value = value
  if (value) load()
}

onMounted(async () => {
  await Promise.all([loadOfferings(), load()])
})
</script>

<template>
  <AppLayout content-mode="list">
    <section class="practice-leaderboard">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE BOARD</p>
          <h1>练习排行榜</h1>
          <p class="lead subtitle">只读视图：可查看任课班级与全平台榜单，不做任何写操作</p>
        </div>
        <div class="ph-actions">
          <span class="count-pill"><span aria-hidden="true"></span>{{ scopeLabel }} · {{ boardLabel }} · {{ total }} 人</span>
        </div>
      </section>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        排行榜仅展示任课班级与全平台聚合数据，教师无权查看学生源码或他人评分详情。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        AI 综合榜的成绩需要等额度恢复后才能继续更新；普通测试榜不受影响。
      </PracticeNotice>

      <PracticeNotice
        v-if="!forbidden && Number(queue.queued) > 0"
        tone="warning"
        :title="`练习判题队列排队 ${queue.queued} 项`"
      >
        排行榜只统计已完成判题的提交，排队中的成绩不会提前计入。
      </PracticeNotice>

      <form v-if="!forbidden" class="filter-bar" @submit.prevent="load">
        <div class="segmented" role="group" aria-label="榜单范围">
          <button
            v-for="option in SCOPE_OPTIONS"
            :key="option.value"
            type="button"
            :class="{ active: scope === option.value }"
            :aria-pressed="scope === option.value"
            @click="onScopeChange(option.value)"
          >{{ option.label }}</button>
        </div>
        <DsSelect
          v-if="scope === 'offering'"
          :model-value="scopeId"
          aria-label="选择班级"
          @update:model-value="onOfferingChange"
        >
          <option value="">请选择班级</option>
          <option v-for="offering in offerings" :key="offering.id" :value="offering.id">
            {{ offering.course_title || offering.title || offering.name || `教学班 #${offering.id}` }}
          </option>
        </DsSelect>
        <div class="segmented" role="group" aria-label="榜单类型">
          <button
            v-for="option in BOARD_OPTIONS"
            :key="option.value"
            type="button"
            :class="{ active: board === option.value }"
            :aria-pressed="board === option.value"
            @click="onBoardChange(option.value)"
          >{{ option.label }}</button>
        </div>
        <button type="button" class="btn btn-secondary" @click="load">
          <AppIcon name="refresh" :size="14" />刷新
        </button>
      </form>

      <PracticeNotice v-if="awaitingScope && !forbidden" tone="info" title="请选择要查看的班级">
        选择班级后展示班内榜；全平台榜始终可用。
      </PracticeNotice>

      <DashboardAsyncState
        v-else
        :loading="loading"
        :error="listErrorVisible(error, forbidden)"
        :empty="!items.length"
        empty-title="还没有人完成练习"
        empty-body="学生提交并判题完成后，成绩会出现在这里；排在后面的同学也会在后续刷新中上榜。"
        @retry="load"
      >
        <PracticeLeaderboardTable v-if="items.length" :items="items" />
      </DashboardAsyncState>

      <p v-if="generatedAt" class="generated-at">榜单更新时间：{{ formatDateTime(generatedAt) }}</p>
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-leaderboard { display: flex; flex-direction: column; gap: var(--space-4); }
.count-pill { display: inline-flex; align-items: center; gap: var(--space-2); padding: var(--space-1) var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-full); background: var(--surface); color: var(--muted); font-size: var(--text-sm); }
.count-pill span { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.filter-bar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.generated-at { margin: 0; color: var(--faint); font-size: var(--text-sm); }
</style>
