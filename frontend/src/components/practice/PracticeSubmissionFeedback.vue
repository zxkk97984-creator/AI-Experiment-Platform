<script setup>
// 练习作答反馈：排队 → 判题 → 确定性测试结果 → AI 四维反馈 → 异议入口。
// 硬约束：AI 未完成时只展示已完成的确定性分 + 「AI 评分未完成」，
// 不显示 0 分、不显示完整 AI 成绩；判题失败同样不折算成分数。
import { computed } from 'vue'
import AppIcon from '../ui/AppIcon.vue'
import UiStatusPill from '../ui/UiStatusPill.vue'
import StudentAIGradingResult from '../ai/StudentAIGradingResult.vue'
import PracticeNotice from './PracticeNotice.vue'
import PracticeTestResults from './PracticeTestResults.vue'
import PracticeAppealPanel from './PracticeAppealPanel.vue'
import PracticeCodeEditor from './PracticeCodeEditor.vue'
import {
  aiIncomplete, aiStatusText, gradingModeMeta, gradingStatusMeta,
  isActiveStatus, queuePositionText, scoreText, testSummary,
} from './practiceHelpers.js'
import { formatDateTime } from '../../utils/format.js'

const props = defineProps({
  submission: { type: Object, required: true },
})

const emit = defineEmits(['changed'])

const status = computed(() => gradingStatusMeta(props.submission.grading_status))
const mode = computed(() => gradingModeMeta(props.submission.grading_mode))
const queuedText = computed(() => queuePositionText(props.submission))
const active = computed(() => isActiveStatus(props.submission.grading_status))
const failed = computed(() => props.submission.grading_status === 'failed')
const quota = computed(() => props.submission.grading_status === 'quota_exhausted')
const needsReview = computed(() => props.submission.grading_status === 'review_required')
const aiPending = computed(() => aiIncomplete(props.submission))
const aiResult = computed(() => props.submission.ai_result || null)
const deterministic = computed(() => props.submission.deterministic || null)
const versionStale = computed(() => props.submission.grading_version_stale === true)
const scoreVisible = computed(() => {
  if (failed.value || quota.value) return false
  return props.submission.score !== null && props.submission.score !== undefined
})
const tests = computed(() => testSummary(props.submission))
const code = computed(() => props.submission.code || '')

function onAppealChanged() {
  emit('changed')
}
</script>

<template>
  <section class="feedback" aria-label="练习作答反馈">
    <header class="feedback-head">
      <div class="head-main">
        <h2>{{ submission.problem?.title || '练习提交' }}</h2>
        <p class="meta">
          <span v-if="submission.created_at">提交于 {{ formatDateTime(submission.created_at) }}</span>
          <span>{{ mode.label }}</span>
          <span v-if="tests">{{ tests }}</span>
        </p>
      </div>
      <UiStatusPill :tone="status.tone" :label="status.label" />
    </header>

    <PracticeNotice
      v-if="queuedText"
      tone="warning"
      :title="queuedText"
      assertive
    >
      练习使用独立判题队列，前方的提交判完后会自动开始，本页会自动刷新。
    </PracticeNotice>

    <PracticeNotice
      v-else-if="submission.grading_status === 'running'"
      tone="info"
      title="判题中"
    >
      正在运行公开与隐藏测试，完成后自动刷新结果。
    </PracticeNotice>

    <PracticeNotice
      v-if="quota"
      tone="danger"
      title="今日额度已用完"
      assertive
    >
      今日练习提交额度或 AI 预算已用尽，明天 0 点后恢复；已完成的成绩不受影响。
    </PracticeNotice>

    <PracticeNotice
      v-if="failed"
      tone="danger"
      title="判题失败，本次不计分"
      assertive
    >
      判题过程出错，成绩按未完成处理，不会记为 0 分；可稍后重新提交。
    </PracticeNotice>

    <PracticeNotice
      v-if="needsReview"
      tone="warning"
      title="待人工复核"
      assertive
    >
      本次评分需要人工确认后才会计入排名。
    </PracticeNotice>

    <PracticeNotice
      v-if="aiPending"
      tone="warning"
      title="AI 评分未完成"
      assertive
    >
      {{ aiStatusText(submission) }}。当前只展示已完成的确定性测试结果，AI 四维分在评分完成后才会出现，不会用 0 分代替。
    </PracticeNotice>

    <PracticeNotice v-if="versionStale" tone="info" title="本题评分标准已更新">
      历史成绩保留在提交记录中，排行榜只统计当前评分版本的成绩。
    </PracticeNotice>

    <div class="score-row">
      <template v-if="scoreVisible">
        <div class="score-block">
          <span class="score-label">本次得分</span>
          <strong class="score-value mono">{{ scoreText(submission) }}</strong>
        </div>
        <div v-if="submission.grading_status === 'completed'" class="score-note">
          <AppIcon name="check" :size="14" />
          已计入我的练习统计（每题取当前评分版本下的历史最高分）
        </div>
      </template>
      <p v-else-if="active" class="score-pending">判题完成后才会给出分数，排队期间不计分。</p>
      <p v-else class="score-pending">本次没有可展示的分数。</p>
    </div>

    <PracticeTestResults v-if="deterministic" :deterministic="deterministic" />

    <StudentAIGradingResult v-if="aiResult" :breakdown="aiResult" heading="AI 四维评分" />

    <PracticeCodeEditor :model-value="code" readonly label="本次提交的代码" />

    <PracticeAppealPanel
      v-if="submission.grading_mode === 'ai' && !active && !failed"
      :submission="submission"
      @changed="onAppealChanged"
    />
  </section>
</template>

<style scoped>
.feedback { display: flex; flex-direction: column; gap: var(--space-4); }
.feedback-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; }
.head-main { min-width: 0; }
.head-main h2 { margin: 0; font-family: var(--font-display); font-size: var(--text-2xl); overflow-wrap: anywhere; }
.head-main .meta { display: flex; flex-wrap: wrap; gap: var(--space-3); margin: var(--space-2) 0 0; color: var(--muted); font-size: var(--text-sm); font-family: var(--font-mono); }
.score-row { display: flex; align-items: center; gap: var(--space-4); flex-wrap: wrap; padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.score-block { display: flex; align-items: baseline; gap: var(--space-3); }
.score-label { color: var(--muted); font-size: var(--text-sm); }
.score-value { font-size: var(--text-3xl); color: var(--accent); }
.score-note { display: inline-flex; align-items: center; gap: var(--space-2); color: var(--muted); font-size: var(--text-sm); }
.score-pending { margin: 0; color: var(--muted); font-size: var(--text-md); }
</style>
