<script setup>
// 学生「我有异议」面板：复用批次 2 的单次成绩异议接口（target_kind='practice_submission'）。
// 只在 AI 评分已完成、后端给出 can_appeal=true 时出现；排队中/未完成评分不提供入口。
import { computed, ref } from 'vue'
import { appealsAPI } from '../../api/appeals.js'
import { useAppStore } from '../../stores/app.js'
import { practiceErrorMessage } from './practiceHelpers.js'

const props = defineProps({
  submission: { type: Object, required: true },
})

const emit = defineEmits(['changed'])

const app = useAppStore()
const reason = ref('')
const replyText = ref('')
const busy = ref(false)
const error = ref('')

const appeal = computed(() => props.submission.appeal || null)
const canAppeal = computed(() => props.submission.can_appeal === true && !appeal.value)
const statusMeta = {
  pending: { label: '处理中', tone: 'warning' },
  withdrawn: { label: '已撤回', tone: 'neutral' },
  upheld: { label: '维持原分', tone: 'neutral' },
  adjusted: { label: '已改分', tone: 'success' },
  regrading: { label: '重评中', tone: 'info' },
  rejected: { label: '已驳回', tone: 'danger' },
}
const appealStatus = computed(() => statusMeta[appeal.value?.status] || statusMeta.pending)

async function submit() {
  const text = reason.value.trim()
  if (busy.value || text.length < 20) return
  busy.value = true
  error.value = ''
  try {
    await appealsAPI.create({ target_kind: 'practice_submission', target_id: props.submission.id, reason: text })
    reason.value = ''
    app.showToast('已提交异议，管理员处理后可在此查看结果', 'success')
    emit('changed')
  } catch (e) {
    error.value = practiceErrorMessage(e, '异议提交失败，请稍后重试')
  } finally {
    busy.value = false
  }
}

async function withdraw() {
  if (busy.value || !appeal.value) return
  busy.value = true
  error.value = ''
  try {
    await appealsAPI.withdraw(appeal.value.id)
    app.showToast('已撤回异议', 'success')
    emit('changed')
  } catch (e) {
    error.value = practiceErrorMessage(e, '撤回失败，请稍后重试')
  } finally {
    busy.value = false
  }
}

async function reply() {
  const text = replyText.value.trim()
  if (busy.value || !text || !appeal.value) return
  busy.value = true
  error.value = ''
  try {
    await appealsAPI.reply(appeal.value.id, text)
    replyText.value = ''
    app.showToast('已补充说明', 'success')
    emit('changed')
  } catch (e) {
    error.value = practiceErrorMessage(e, '补充说明失败，请稍后重试')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="appeal-panel" aria-label="成绩异议">
    <p v-if="error" class="appeal-error" role="alert">{{ error }}</p>

    <div v-if="appeal" class="appeal-current">
      <header class="appeal-head">
        <h3>我的异议</h3>
        <span class="badge" :class="`badge-${appealStatus.tone}`">
          <span class="dot" aria-hidden="true"></span>{{ appealStatus.label }}
        </span>
      </header>
      <p class="appeal-reason">{{ appeal.reason }}</p>
      <p v-if="appeal.score_before !== null && appeal.score_before !== undefined" class="appeal-score mono">
        申请时分数 {{ appeal.score_before }}<template v-if="appeal.score_after !== null && appeal.score_after !== undefined"> → 复核后 {{ appeal.score_after }}</template>
      </p>
      <p v-if="appeal.handler_comment" class="appeal-comment">处理说明：{{ appeal.handler_comment }}</p>
      <form v-if="appeal.status === 'pending'" class="appeal-reply" @submit.prevent="reply">
        <label class="field">
          <span class="field-label">补充说明</span>
          <textarea v-model="replyText" class="textarea" rows="2" maxlength="2000" placeholder="补充你的依据或说明"></textarea>
        </label>
        <div class="appeal-actions">
          <button type="submit" class="btn btn-secondary btn-sm" :disabled="busy || !replyText.trim()">补充说明</button>
          <button type="button" class="btn btn-ghost btn-sm" :disabled="busy" @click="withdraw">撤回异议</button>
        </div>
      </form>
    </div>

    <form v-else-if="canAppeal" class="appeal-form" @submit.prevent="submit">
      <label class="field">
        <span class="field-label">对本次 AI 评分有异议？说明理由后提交，由管理员复核</span>
        <textarea
          v-model="reason"
          class="textarea"
          rows="3"
          maxlength="2000"
          placeholder="请说明存在疑问的评分项与依据（20–2000 字）"
          required
        ></textarea>
      </label>
      <p class="field-hint">已输入 {{ reason.trim().length }} 字，至少 20 字</p>
      <button
        type="submit"
        class="btn btn-secondary"
        :disabled="busy || reason.trim().length < 20"
      >
        {{ busy ? '提交中…' : '我有异议' }}
      </button>
    </form>

    <p v-else-if="submission.grading_mode === 'ai'" class="appeal-unavailable">
      本次评分暂不支持申请异议（异议窗口为评分结果可见后 7 天内，同一评分版本只允许一条）。
    </p>
  </section>
</template>

<style scoped>
.appeal-panel { display: flex; flex-direction: column; gap: var(--space-3); font-size: var(--text-md); }
.appeal-error { margin: 0; color: var(--danger); }
.appeal-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.appeal-head h3 { margin: 0; font-size: var(--text-md); }
.appeal-current, .appeal-form { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface-subtle); }
.appeal-reason { margin: 0; color: var(--fg); line-height: var(--lh-body); overflow-wrap: anywhere; }
.appeal-score { margin: 0; color: var(--muted); }
.appeal-comment { margin: 0; color: var(--muted); }
.appeal-actions { display: flex; gap: var(--space-2); }
.appeal-reply { display: flex; flex-direction: column; gap: var(--space-2); }
.appeal-unavailable { margin: 0; color: var(--faint); }
</style>
