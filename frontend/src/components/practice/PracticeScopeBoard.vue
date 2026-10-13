<script setup>
// 批次 6 练习榜单：范围（全平台/班内）× 榜类型（普通测试/AI 综合）× 视图（完整/只看我的名次）。
// 学生与教师共用：教师传 readonly，页面不渲染 opt-in 开关与任何写操作。
// 数据来自后端快照（practice_student_stats），页面不自己聚合。
import { computed, onMounted, ref } from 'vue'
import DashboardAsyncState from '../dashboard/DashboardAsyncState.vue'
import DsSelect from '../ui/DsSelect.vue'
import AppIcon from '../ui/AppIcon.vue'
import PracticeNotice from './PracticeNotice.vue'
import PracticeLeaderboardTable from './PracticeLeaderboardTable.vue'
import { listErrorVisible, usePracticeLoad } from './usePracticeLoad.js'
import { practiceErrorMessage } from './practiceHelpers.js'
import { practiceSocialAPI } from '../../api/practice.js'
import { offeringsAPI } from '../../api/offerings.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'

defineProps({
  /** 只读模式（教师页）：不渲染 opt-in 开关，不提供任何写操作 */
  readonly: { type: Boolean, default: false },
  /** 是否展示「加入全平台榜」开关（仅学生页） */
  showOptIn: { type: Boolean, default: false },
})

const SCOPE_OPTIONS = [
  { value: 'platform', label: '全平台榜' },
  { value: 'offering', label: '班内榜' },
]
const BOARD_OPTIONS = [
  { value: 'deterministic', label: '普通测试榜' },
  { value: 'ai', label: 'AI 综合榜' },
]
const VIEW_OPTIONS = [
  { value: 'top', label: '完整榜单' },
  { value: 'me', label: '只看我的名次' },
]

const app = useAppStore()
const scope = ref('platform')
const board = ref('deterministic')
const view = ref('top')
const scopeId = ref('')
const offerings = ref([])
const data = ref({})
const optIn = ref(false)
const savingOptIn = ref(false)

const { loading, error, forbidden, quotaExhausted, load } = usePracticeLoad(async () => {
  const res = await practiceSocialAPI.leaderboard({
    scope: scope.value,
    scope_id: scope.value === 'offering' && scopeId.value ? scopeId.value : undefined,
    board: board.value,
    view: view.value,
  })
  data.value = res.data || {}
  if (typeof data.value.opt_in === 'boolean') optIn.value = data.value.opt_in
  return res
}, { fallbackMessage: '排行榜加载失败，请稍后重试' })

const rows = computed(() => (
  view.value === 'me' ? (data.value.window || []) : (data.value.items || [])
))
const scopeLabel = computed(() => SCOPE_OPTIONS.find((item) => item.value === scope.value)?.label || '')
const boardLabel = computed(() => BOARD_OPTIONS.find((item) => item.value === board.value)?.label || '')
const generatedAt = computed(() => data.value.recalculated_at || data.value.generated_at || '')
const awaitingScope = computed(() => scope.value === 'offering' && !scopeId.value)
// 学生视角下「已加入全平台榜」是布尔；教师视角后端返回 null（不暴露任何学生偏好）
const isStudentView = computed(() => typeof data.value.opt_in === 'boolean')
const notOptedIn = computed(() => scope.value === 'platform' && isStudentView.value && !optIn.value)
const me = computed(() => data.value.me || null)
const truncated = computed(() => data.value.truncated === true)
const queue = computed(() => data.value.meta?.queue || {})

async function loadOfferings() {
  try {
    const res = await offeringsAPI.list({ page: 1, page_size: 50 })
    offerings.value = res.data?.items || []
    if (offerings.value.length === 1) scopeId.value = offerings.value[0].id
  } catch {
    offerings.value = []
  }
}

function onScopeChange(value) {
  scope.value = value
  if (value === 'platform' || scopeId.value) load()
}

function onBoardChange(value) {
  board.value = value
  load()
}

function onViewChange(value) {
  view.value = value
  load()
}

function onOfferingChange(value) {
  scopeId.value = value
  if (value) load()
}

async function toggleOptIn() {
  const next = !optIn.value
  savingOptIn.value = true
  try {
    const res = await practiceSocialAPI.setOptIn(next)
    optIn.value = res.data?.opt_in === true
    app.showToast(next ? '已加入全平台榜（以化名展示）' : '已退出全平台榜', 'success')
    await load({ silent: true })
  } catch (e) {
    app.showToast(practiceErrorMessage(e, '操作失败，请稍后重试'), 'error')
  } finally {
    savingOptIn.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadOfferings(), load()])
})
</script>

<template>
  <section class="practice-scope-board">
    <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
      仅能查看本人所在班级与全平台聚合榜单；练习模块的写权限不向教师开放。
    </PracticeNotice>

    <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
      AI 综合榜的成绩需要等额度恢复后才会更新；普通测试榜不受影响。
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
      <div v-if="isStudentView" class="segmented" role="group" aria-label="榜单视图">
        <button
          v-for="option in VIEW_OPTIONS"
          :key="option.value"
          type="button"
          :class="{ active: view === option.value }"
          :aria-pressed="view === option.value"
          @click="onViewChange(option.value)"
        >{{ option.label }}</button>
      </div>
      <button type="button" class="btn btn-secondary" @click="load">
        <AppIcon name="refresh" :size="14" />刷新
      </button>
    </form>

    <div v-if="showOptIn && !readonly && !forbidden" class="opt-in-row">
      <label class="opt-in">
        <input
          type="checkbox"
          :checked="optIn"
          :disabled="savingOptIn"
          @change="toggleOptIn"
        />
        <span>加入全平台榜（以化名展示，可随时退出）</span>
      </label>
      <p class="opt-in-hint">
        全平台榜不展示学号、姓名、源码或他人评分详情；退出后立即从全平台榜消失，
        班内榜与历史成绩不受影响。
      </p>
    </div>

    <PracticeNotice
      v-if="!forbidden && Number(queue.queued) > 0"
      tone="warning"
      :title="`练习判题队列排队 ${queue.queued} 项`"
    >
      排行榜只统计已完成判题的提交，排队中的成绩不会提前计入。
    </PracticeNotice>

    <PracticeNotice v-if="notOptedIn && !forbidden" tone="info" title="你尚未加入全平台榜">
      加入后你会以「同学 XXXX」的化名参与排名；不加入也可以正常做题与查看成绩。
    </PracticeNotice>

    <PracticeNotice v-if="awaitingScope && !forbidden" tone="info" title="请选择要查看的班级">
      选择班级后展示班内榜；全平台榜始终可用。
    </PracticeNotice>

    <template v-else>
      <p v-if="!forbidden" class="board-meta">
        {{ scopeLabel }} · {{ boardLabel }} · {{ data.total || 0 }} 人参与
        <span v-if="truncated">（仅展示前 100 名，名次按完整榜单计算）</span>
      </p>

      <PracticeNotice v-if="view === 'me' && !me && !forbidden" tone="info" title="暂时没有你的名次">
        还没有计入榜单的成绩，或你尚未加入全平台榜。
      </PracticeNotice>

      <DashboardAsyncState
        :loading="loading"
        :error="listErrorVisible(error, forbidden)"
        :empty="!rows.length"
        empty-title="还没有人完成练习"
        empty-body="学生提交并判题完成后，成绩会出现在这里。"
        @retry="load"
      >
        <PracticeLeaderboardTable v-if="rows.length" :items="rows" />
      </DashboardAsyncState>
    </template>

    <p v-if="generatedAt" class="generated-at">
      榜单快照时间：{{ formatDateTime(generatedAt) }}（由后台定时重算，非实时聚合）
    </p>
  </section>
</template>

<style scoped>
.practice-scope-board { display: flex; flex-direction: column; gap: var(--space-4); }
.filter-bar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.opt-in-row { display: flex; flex-direction: column; gap: var(--space-1); }
.opt-in { display: inline-flex; align-items: center; gap: var(--space-2); font-weight: 600; }
.opt-in-hint { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.board-meta { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.generated-at { margin: 0; color: var(--faint); font-size: var(--text-sm); }
</style>
