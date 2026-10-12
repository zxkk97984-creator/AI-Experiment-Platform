<script setup>
/**
 * 教师成绩详情 - 试卷讲评视图。
 * 复用学生端考试试卷的版式（题卡、选项、填空、代码），教师端额外展示
 * 标准答案/参考答案，并允许在 0 ~ 本题满分 之间逐题修改得分。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { areaPathForRole } from '../../../router/roleHome.js'

const router = useRouter()
const route = useRoute()

const props = defineProps({
  questions: { type: Array, default: () => [] },
  answers: { type: Array, default: () => [] },
  editable: { type: Boolean, default: false },
  savingKey: { type: [Number, String], default: null },
  revertKey: { type: Number, default: 0 },
  scoreErrors: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['save-score'])

const TYPE_META = {
  single_choice: { label: '单选题', order: 1 },
  multi_choice: { label: '多选题', order: 2 },
  code: { label: '编程题', order: 3 },
  fill_blank: { label: '填空题', order: 4 },
}
const CHINESE_NUMBERS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十']

const savingActive = computed(() => props.savingKey !== null && props.savingKey !== undefined)

const answerByQuestion = computed(() => new Map(
  (props.answers || []).map((answer) => [String(answer.question_id), answer]),
))

const rows = computed(() => {
  const source = (props.questions || []).length
    ? (props.questions || []).map((question) => ({
        question,
        answer: answerByQuestion.value.get(String(question.id)) || null,
      }))
    : (props.answers || []).map((answer, index) => ({
        question: {
          id: answer.question_id,
          question_type: answer.question_type,
          prompt: answer.prompt,
          points: answer.points,
          order_index: answer.order_index ?? index,
          options: null,
          correct_answer: null,
        },
        answer,
      }))
  return source
    .slice()
    .sort((a, b) => Number(a.question.order_index ?? 0) - Number(b.question.order_index ?? 0))
    .map((row, index) => ({ ...row, number: index + 1 }))
})

const groups = computed(() => {
  const map = new Map()
  for (const row of rows.value) {
    const type = row.question.question_type || 'other'
    if (!map.has(type)) {
      map.set(type, {
        type,
        label: TYPE_META[type]?.label || '其他题型',
        order: TYPE_META[type]?.order || 9,
        rows: [],
      })
    }
    map.get(type).rows.push(row)
  }
  return [...map.values()].sort((a, b) => a.order - b.order).map((group, index) => ({
    ...group,
    number: CHINESE_NUMBERS[index] || index + 1,
    score: group.rows.reduce((sum, row) => sum + Number(row.answer?.score || 0), 0),
    total: group.rows.reduce((sum, row) => sum + Number(row.question.points || 0), 0),
  }))
})

function typeLabel(type) {
  return TYPE_META[type]?.label || '其他题型'
}

function fmtScore(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return String(Number(number.toFixed(2)))
}

function currentScore(row) {
  if (!row.answer || row.answer.score == null) return ''
  return fmtScore(row.answer.score)
}

function answerState(row) {
  if (!row.answer || row.answer.score == null) return { label: '待评分', tone: 'pending' }
  const score = Number(row.answer.score)
  const points = Number(row.question.points)
  if (score >= points) return { label: '正确', tone: 'correct' }
  if (score <= 0) return { label: '错误', tone: 'wrong' }
  return { label: '部分正确', tone: 'partial' }
}

function correctKeys(question) {
  return (question?.correct_answer?.correct || []).map(String)
}

function isCorrectKey(question, key) {
  return correctKeys(question).includes(String(key))
}

function isSelected(row, key) {
  return (row.answer?.selected_options || []).map(String).includes(String(key))
}

function optionTone(row, key) {
  const correct = isCorrectKey(row.question, key)
  const selected = isSelected(row, key)
  return {
    selected,
    correct,
    wrong: selected && !correct,
    missed: !selected && correct,
  }
}

function optionVerdict(row, key) {
  if (isCorrectKey(row.question, key)) return { text: '正确答案', tone: 'correct' }
  if (isSelected(row, key)) return { text: '学生误选', tone: 'wrong' }
  return null
}

function selectedSummary(row) {
  if (row.question.question_type === 'code') {
    return String(row.answer?.code_answer || '').trim() || '未作答'
  }
  if (row.question.question_type === 'fill_blank') {
    const ids = blankIds(row.question)
    if (!ids.length || !row.answer?.text_answers) return '未作答'
    const parts = ids.map((id, index) => {
      const value = String(row.answer.text_answers[id] || '').trim()
      return `第${index + 1}空：${value || '未作答'}`
    })
    return parts.join('；')
  }
  const selected = (row.answer?.selected_options || []).map(String)
  return selected.length ? selected.join('、') : '未作答'
}

function blankIds(question) {
  return [...String(question?.prompt || '').matchAll(/\[\[blank:([A-Za-z0-9_-]+)\]\]/g)].map((match) => match[1])
}

function promptSegments(question) {
  const text = String(question?.prompt || '')
  const segments = []
  let lastIndex = 0
  for (const match of text.matchAll(/\[\[blank:([A-Za-z0-9_-]+)\]\]/g)) {
    if (match.index > lastIndex) segments.push({ type: 'text', text: text.slice(lastIndex, match.index) })
    segments.push({ type: 'blank', id: match[1] })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) segments.push({ type: 'text', text: text.slice(lastIndex) })
  return segments
}

function blankValue(row, blankId) {
  const value = String(row.answer?.text_answers?.[blankId] || '').trim()
  return value || '未作答'
}

function blankAnswers(question) {
  return (question?.correct_answer?.blanks || []).map((blank, index) => ({
    id: blank.id || `blank${index + 1}`,
    accepted: blank.accepted_answers || [],
  }))
}

function gradingModeLabel(mode) {
  return { legacy: '传统判题', shadow: 'AI 影子评分', active: 'AI 正式评分' }[mode] || mode || '未配置'
}

function judgeCounts(row) {
  const passed = row.answer?.ai_grading?.tests_passed ?? row.answer?.tests_passed
  const total = row.answer?.ai_grading?.tests_total ?? row.answer?.tests_total
  if (passed == null && total == null) return null
  return { passed: Number(passed ?? 0), total: Number(total ?? 0) }
}

function deterministicSummary(row) {
  const ai = row.answer?.ai_grading
  if (!ai) return ''
  const parts = []
  if (ai.functional_score != null) parts.push(`功能 ${fmtScore(ai.functional_score)}`)
  if (ai.robustness_score != null) parts.push(`健壮性 ${fmtScore(ai.robustness_score)}`)
  return parts.join(' · ')
}

const AI_STATUS_LABELS = { pending: '排队中', queued: '排队中', running: '评分中', completed: '已完成', review_required: '需人工复核' }

function aiState(row) {
  const ai = row.answer?.ai_grading
  if (!ai) return null
  if (ai.status === 'review_required' || (ai.needs_teacher_review && !['pending', 'queued', 'running'].includes(ai.status))) {
    return {
      tone: 'review',
      label: AI_STATUS_LABELS[ai.status] || ai.status,
      detail: ai.scaled_score != null
        ? `AI 折算分 ${fmtScore(ai.scaled_score)} / 100（待教师确认）`
        : 'AI 自动评分终止，本题暂无得分',
      reason: ai.review_reason || ai.last_error || '',
      gradeId: ai.id,
    }
  }
  if (['pending', 'queued', 'running'].includes(ai.status)) {
    return {
      tone: 'running',
      label: AI_STATUS_LABELS[ai.status] || ai.status,
      detail: `已尝试 ${ai.attempt_count ?? 0} 次`,
      reason: '',
      gradeId: ai.id,
    }
  }
  return {
    tone: 'done',
    label: AI_STATUS_LABELS.completed,
    detail: ai.scaled_score != null ? `折算分 ${fmtScore(ai.scaled_score)} / ${fmtScore(row.question.points)}` : '',
    reason: '',
    gradeId: ai.id,
  }
}

function goAiReview(gradeId) {
  // 共享页面：管理员必须在 /admin 前缀下打开，否则会被角色守卫重定向
  router.push(areaPathForRole((route.path || '').startsWith('/admin') ? 'admin' : 'teacher',
                              `/ai-grading/${gradeId}`))
}

function scoreKey(row) {
  return row.answer ? row.answer.id : `question-${row.question.id}`
}

function commitScore(row, event) {
  const input = event.target
  const original = currentScore(row)
  if (!props.editable) {
    input.value = original
    return
  }
  const raw = input.value.trim()
  if (raw === '') {
    input.value = original
    return
  }
  const score = Number(raw)
  const maxPoints = Number(row.question.points)
  if (!Number.isFinite(score) || score < 0 || score > maxPoints) {
    input.value = original
    return
  }
  if (original !== '' && score === Number(original)) {
    input.value = original
    return
  }
  emit('save-score', { answerId: row.answer?.id ?? null, questionId: row.question.id, score })
}
</script>

<template>
  <div v-if="!rows.length" class="paper-empty">暂无题目与答题明细</div>

  <div v-else class="teacher-paper">
    <section v-for="group in groups" :key="group.type" class="paper-group">
      <header class="paper-group__head">
        <strong>{{ group.number }}、{{ group.label }}（共 {{ group.rows.length }} 题，{{ fmtScore(group.total) }} 分）</strong>
        <span>得分：{{ fmtScore(group.score) }} / {{ fmtScore(group.total) }}</span>
      </header>

      <article v-for="row in group.rows" :key="`${row.question.id}-${row.answer?.id || 'none'}`" class="question-card">
        <header class="question-head">
          <div class="question-meta">
            <span class="question-index">{{ String(row.number).padStart(2, '0') }}</span>
            <span class="type-chip">{{ typeLabel(row.question.question_type) }}</span>
            <span class="question-points">满分 {{ fmtScore(row.question.points) }} 分</span>
          </div>

          <div class="score-edit">
            <label>
              <span>本题得分</span>
              <span v-if="savingKey === scoreKey(row)" class="saving-label">保存中…</span>
              <input
                :key="`score-input-${scoreKey(row)}-${revertKey}`"
                :value="currentScore(row)"
                type="text"
                inputmode="decimal"
                autocomplete="off"
                :disabled="!editable || savingActive"
                :aria-label="`第 ${row.number} 题得分（满分 ${fmtScore(row.question.points)} 分）`"
                @change="commitScore(row, $event)"
                @keydown.enter.prevent="$event.target.blur()"
              >
              <em>/ {{ fmtScore(row.question.points) }} 分</em>
            </label>
            <span class="answer-state" :class="answerState(row).tone">{{ answerState(row).label }}</span>
            <p v-if="row.answer?.manual_score_reason" class="manual-score-note">改分理由：{{ row.answer.manual_score_reason }}</p>
            <p v-if="scoreErrors[scoreKey(row)]" class="score-error">{{ scoreErrors[scoreKey(row)] }}</p>
          </div>
        </header>

        <p v-if="row.question.question_type !== 'fill_blank'" class="prompt">{{ row.question.prompt }}</p>
        <div v-else class="fill-prompt">
          <template v-for="(segment, segmentIndex) in promptSegments(row.question)" :key="`${segment.type}-${segmentIndex}`">
            <span v-if="segment.type === 'text'">{{ segment.text }}</span>
            <span v-else class="blank-value" :class="{ empty: blankValue(row, segment.id) === '未作答' }">{{ blankValue(row, segment.id) }}</span>
          </template>
        </div>

        <div v-if="['single_choice', 'multi_choice'].includes(row.question.question_type)" class="options" aria-label="题目选项与作答对照">
          <div
            v-for="(option, key) in row.question.options || {}"
            :key="key"
            class="option-row"
            :class="optionTone(row, key)"
          >
            <b>{{ key }}</b>
            <span>{{ option }}</span>
            <i v-if="optionVerdict(row, key)" :class="optionVerdict(row, key).tone">{{ optionVerdict(row, key).text }}</i>
          </div>
        </div>

        <template v-else-if="row.question.question_type === 'code'">
          <div class="student-answer code-answer" :class="answerState(row).tone">
            <span>学生代码</span>
            <pre>{{ row.answer?.code_answer || '未作答' }}</pre>
          </div>
          <div class="code-meta">
            <span>评分方式：{{ gradingModeLabel(row.question.grading_mode) }}</span>
            <span v-if="judgeCounts(row)">判题样例 {{ judgeCounts(row).passed }} / {{ judgeCounts(row).total }}</span>
            <span v-if="deterministicSummary(row)">确定性得分：{{ deterministicSummary(row) }}</span>
            <span v-if="(row.question.public_cases || []).length">公开样例 {{ row.question.public_cases.length }} 个</span>
            <span v-if="(row.question.test_groups || []).length">测试组 {{ row.question.test_groups.length }} 个</span>
            <span v-if="row.question.has_locked_rubric">Rubric 已锁定</span>
          </div>
          <div v-if="aiState(row)" class="ai-state" :class="aiState(row).tone" role="status">
            <b>AI 评分 · {{ aiState(row).label }}</b>
            <strong>{{ aiState(row).detail }}</strong>
            <p v-if="aiState(row).reason">{{ aiState(row).reason }}</p>
            <button
              v-if="aiState(row).tone === 'review'"
              type="button"
              class="ai-review-link"
              @click="goAiReview(aiState(row).gradeId)"
            >前往 AI 评分复核处理 →</button>
          </div>
        </template>

        <div class="standard-answer" :class="answerState(row).tone">
          <span>{{ row.question.question_type === 'code' ? '参考答案' : '标准答案' }}</span>
          <code v-if="['single_choice', 'multi_choice'].includes(row.question.question_type)">
            {{ correctKeys(row.question).join('、') || '未配置' }}
          </code>
          <ul v-else-if="row.question.question_type === 'fill_blank'">
            <li v-for="blank in blankAnswers(row.question)" :key="blank.id">
              <code>{{ blank.id }}</code>
              <span>{{ blank.accepted.join(' / ') || '未配置' }}</span>
            </li>
          </ul>
          <pre v-else-if="row.question.question_type === 'code' && row.question.reference_solution">{{ row.question.reference_solution }}</pre>
          <code v-else-if="row.question.question_type === 'code'">暂未配置参考答案</code>
        </div>

        <div v-if="row.question.question_type !== 'code'" class="student-answer" :class="answerState(row).tone">
          <span>学生作答</span>
          <strong>{{ selectedSummary(row) }}</strong>
        </div>

        <p v-if="row.answer?.system_error" class="system-error">评分异常：{{ row.answer.system_error }}</p>
      </article>
    </section>
  </div>
</template>

<style scoped>
.teacher-paper { display: grid; gap: 12px; }
.paper-empty { padding: 42px 20px; color: var(--muted); text-align: center; }
.paper-group { overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.paper-group__head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-subtle);
  color: var(--fg);
  font-size: 13px;
}
.paper-group__head span { font-weight: 650; font-variant-numeric: tabular-nums; }

.question-card {
  padding: 22px 24px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.question-card:last-child { border-bottom: 0; }
.question-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.question-meta { display: flex; align-items: center; gap: 9px; min-width: 0; }
.question-index { font: 750 18px/1 var(--font-mono); color: var(--fg); }
.type-chip {
  padding: 4px 8px;
  border-radius: var(--radius-full);
  background: var(--accent-soft);
  color: var(--accent-hover);
  font-size: 10px;
  font-weight: 700;
  white-space: nowrap;
}
.question-points { color: var(--muted); font-size: 12px; }

.score-edit { display: grid; justify-items: end; gap: 5px; flex: none; }
.score-edit label { display: flex; align-items: center; gap: 7px; }
.score-edit label > span { color: var(--muted); font-size: 11px; }
.score-edit label > span.saving-label { color: var(--warning); }
.score-edit input {
  width: 84px;
  height: 34px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--fg);
  font: 650 13px/1 var(--font-mono);
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.score-edit input:focus { border-color: var(--accent); outline: none; box-shadow: 0 0 0 3px var(--accent-soft); }
.score-edit input:disabled { color: var(--muted); background: var(--surface-subtle); cursor: not-allowed; opacity: 1; }
.score-edit em { color: var(--muted); font-size: 11px; font-style: normal; }
.answer-state { font-size: 11px; text-align: center; }
.answer-state.correct { color: var(--success); }
.answer-state.partial { color: var(--warning); }
.answer-state.wrong { color: var(--danger); }
.answer-state.pending { color: var(--muted); }
.score-error { grid-column: 1 / -1; max-width: 260px; margin: 0; color: var(--danger); font-size: 11px; line-height: 1.5; text-align: right; }
.manual-score-note { grid-column: 1 / -1; max-width: 260px; margin: 0; color: var(--warning); font-size: 11px; line-height: 1.5; text-align: right; }

.prompt, .fill-prompt { margin: 0 0 18px; color: var(--fg); font-size: 14px; line-height: 1.9; white-space: pre-wrap; }
.fill-prompt .blank-value {
  display: inline-block;
  min-width: 120px;
  margin: 3px 7px;
  padding: 5px 10px;
  border: 0;
  border-bottom: 2px solid var(--info-bg);
  border-radius: 3px;
  background: var(--accent-soft);
  color: var(--fg);
  font: 600 13px/1.6 var(--font-sans);
}
.fill-prompt .blank-value.empty { color: var(--muted); }

.options { display: grid; gap: 8px; margin-bottom: 16px; }
.option-row {
  display: grid;
  grid-template-columns: 30px 1fr auto;
  align-items: center;
  gap: 9px;
  padding: 11px 13px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--fg);
  font-size: 13px;
}
.option-row b { color: var(--muted); font-size: 12px; }
.option-row i { font-size: 11px; font-style: normal; }
.option-row i.correct { color: var(--success); }
.option-row i.wrong { color: var(--danger); }
.option-row.correct { border-color: var(--success-bg); background: var(--success-bg); }
.option-row.wrong { border-color: var(--danger-bg); background: var(--danger-bg); }
.option-row.missed { border-color: var(--warning-bg); background: var(--warning-bg); }
.option-row.selected { border-style: solid; }

.student-answer {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin: 12px 0 0;
  padding: 11px 13px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  font-size: 13px;
}
.student-answer > span { flex: none; color: var(--muted); font-size: 11px; }
.student-answer strong { color: var(--fg); word-break: break-word; }
.student-answer.correct { border-color: var(--success-bg); background: var(--success-bg); }
.student-answer.partial { border-color: var(--warning-bg); background: var(--warning-bg); }
.student-answer.wrong { border-color: var(--danger-bg); background: var(--danger-bg); }
.student-answer.code-answer { display: block; }
.student-answer.code-answer pre {
  max-height: 260px;
  margin: 9px 0 0;
  overflow: auto;
  padding: 13px;
  border-radius: var(--radius-md);
  color: var(--border);
  background: var(--fg);
  font: 12px/1.65 var(--font-mono);
  white-space: pre-wrap;
  word-break: break-word;
}

.code-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 10px; color: var(--muted); font-size: 11px; }

.ai-state {
  display: grid;
  gap: 4px;
  margin-top: 10px;
  padding: 11px 13px;
  border: 1px solid var(--border);
  border-left-width: 3px;
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
  font-size: 12px;
}
.ai-state b { color: var(--muted); font-size: 11px; }
.ai-state strong { color: var(--fg); }
.ai-state p { margin: 0; color: var(--muted); line-height: 1.6; word-break: break-word; }
.ai-state.review { border-color: var(--warning-bg); background: var(--warning-bg); }
.ai-state.review b, .ai-state.review strong { color: var(--warning); }
.ai-state.running { border-color: var(--info-bg); }
.ai-state.done { border-color: var(--success-bg); background: var(--success-bg); }
.ai-state.done b, .ai-state.done strong { color: var(--success); }
.ai-review-link {
  justify-self: start;
  margin-top: 3px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--accent);
  font-size: 12px;
  cursor: pointer;
}
.ai-review-link:hover { text-decoration: underline; }

.standard-answer {
  margin-top: 12px;
  padding: 11px 13px;
  border: 1px dashed var(--success-bg);
  border-radius: var(--radius-md);
  background: var(--surface);
  font-size: 13px;
}
.standard-answer > span { display: block; margin-bottom: 6px; color: var(--success); font-size: 11px; }
.standard-answer code { color: var(--success); white-space: pre-wrap; word-break: break-all; }
.standard-answer ul { display: grid; gap: 4px; margin: 0; padding-left: 18px; }
.standard-answer li { color: var(--success); }
.standard-answer li code { margin-right: 8px; color: var(--fg); }
.standard-answer pre {
  max-height: 260px;
  margin: 0;
  overflow: auto;
  padding: 12px;
  border-radius: var(--radius-md);
  color: var(--success);
  background: var(--success-bg);
  font: 12px/1.65 var(--font-mono);
  white-space: pre-wrap;
  word-break: break-word;
}

.system-error { margin: 10px 0 0; color: var(--danger); font-size: 12px; line-height: 1.6; }

@media (max-width: 760px) {
  .question-card { padding: 16px; }
  .question-head { flex-direction: column; gap: 12px; }
  .score-edit { justify-items: start; }
  .score-error { text-align: left; }
  .option-row { grid-template-columns: 26px 1fr; }
  .option-row i { grid-column: 2; }
}
@media print {
  .score-edit input { border: 0; padding: 0; background: transparent; width: 52px; }
  .score-error { display: none; }
}
</style>
