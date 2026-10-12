<script setup>
// 学生作答反馈页：按提交 id 展示排队/判题状态、测试结果、AI 四维反馈与异议入口。
// 六态：加载 / 空数据（提交不存在）/ 失败 / 排队 / 额度耗尽 / 权限不足。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import PracticeSubmissionFeedback from '../../components/practice/PracticeSubmissionFeedback.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { usePracticeLoad } from '../../components/practice/usePracticeLoad.js'
import { isActiveStatus } from '../../components/practice/practiceHelpers.js'
import { practiceAPI } from '../../api/practice.js'

const route = useRoute()
const router = useRouter()
const submission = ref(null)
const missing = ref(false)
let pollTimer = null
let pollCount = 0
const POLL_MS = 3000
const MAX_POLLS = 200

const { loading, error, forbidden, quotaExhausted, load } = usePracticeLoad(async () => {
  const res = await practiceAPI.getSubmission(route.params.id)
  submission.value = res.data
  if (isActiveStatus(res.data?.grading_status) && pollCount < MAX_POLLS) {
    pollCount += 1
    schedule()
  }
  return res
}, { fallbackMessage: '提交详情加载失败，请稍后重试' })

const problemId = computed(() => submission.value?.problem?.id)

function schedule() {
  stopPolling()
  pollTimer = window.setTimeout(() => load({ silent: true }).catch(() => stopPolling()), POLL_MS)
}

function stopPolling() {
  if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = null }
}

function refresh() {
  pollCount = 0
  load()
}

onMounted(async () => {
  await load()
  if (!submission.value && !forbidden.value && error.value) missing.value = true
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <AppLayout content-mode="detail">
    <section class="practice-submission">
      <nav class="crumb-row" aria-label="面包屑">
        <button type="button" class="btn btn-ghost btn-sm" @click="router.push('/student/practice/mine')">
          <AppIcon name="back" :size="14" />我的练习
        </button>
        <button
          v-if="problemId"
          type="button"
          class="btn btn-secondary btn-sm"
          @click="router.push(`/student/practice/problems/${problemId}`)"
        >
          再做一次
        </button>
      </nav>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        只能查看自己的练习提交，其他同学的提交与源码不可见。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        今日提交额度或 AI 预算已用尽，明天 0 点后恢复。
      </PracticeNotice>

      <DashboardAsyncState
        :loading="loading"
        :error="Boolean(error) && !forbidden && !missing"
        :empty="missing"
        empty-title="提交记录不存在"
        empty-body="该提交可能已被删除，返回「我的练习」查看其它记录。"
        @retry="refresh"
      >
        <PracticeSubmissionFeedback v-if="submission" :submission="submission" @changed="refresh" />
      </DashboardAsyncState>
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-submission { display: flex; flex-direction: column; gap: var(--space-4); }
.crumb-row { display: flex; gap: var(--space-2); flex-wrap: wrap; }
</style>
