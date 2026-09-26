<script setup>
/** 评分决策面板：查看自动评估 → 调整最终分数 → 给出反馈 → 发布评分。
 *  校验、API 与提示由父组件处理，提交时上抛规范化载荷。 */
import { computed, ref, watch } from 'vue'
import AppIcon from '../ui/AppIcon.vue'
import { formatDateTime } from '../../utils/format.js'

const props = defineProps({
  submission: { type: Object, required: true },
  saving: Boolean,
})
const emit = defineEmits(['submit'])

const reviewScore = ref('')
const reviewFeedback = ref('')

const isGraded = computed(() => props.submission?.score != null)

// 父组件保存后重新加载提交 → 回填最新分数与反馈
watch(() => props.submission, (submission) => {
  reviewScore.value = submission?.score != null ? String(submission.score) : ''
  reviewFeedback.value = submission?.feedback || ''
}, { immediate: true })

// ── ① 自动评估：从提交快照汇总执行证据 ──────────────────────────────
const evidence = computed(() => {
  const cells = props.submission?.cells_snapshot || {}
  const meta = props.submission?.cell_metadata || {}
  const outputs = props.submission?.outputs_snapshot || {}
  const entries = Object.entries(cells)
  const codeCells = entries.filter(([id]) => (meta[id] || {}).type === 'code').length
  let executed = 0
  let errors = 0
  for (const [id] of entries) {
    const out = outputs[id]
    if (!out) continue
    const outList = out.outputs || []
    if (out.execution_count != null || outList.length) executed += 1
    if (outList.some((o) =>
      o.output_type === 'error' ||
      o.ename ||
      (typeof o.text === 'string' && o.text.includes('Traceback')),
    )) errors += 1
  }
  return { total: entries.length, codeCells, executed, errors }
})
const evidenceHint = computed(() => {
  const ev = evidence.value
  if (ev.errors > 0) return { tone: 'warning', text: `${ev.errors} 个 Cell 执行异常，请查看左侧输出定位问题` }
  if (ev.executed === 0) return { tone: 'muted', text: '学生提交时未运行任何 Cell，请结合代码人工评估' }
  return { tone: 'success', text: `${ev.executed} 个 Cell 已运行，输出结果见左侧内容区` }
})

// ── ② 成绩：档位提示与快捷分数 ──────────────────────────────────────
const parsedScore = computed(() => {
  const raw = String(reviewScore.value).trim()
  if (raw === '') return null
  const value = Number(raw)
  return Number.isFinite(value) ? value : null
})
const scoreLevel = computed(() => {
  const value = parsedScore.value
  if (value == null) return null
  if (value >= 90) return { label: '优秀', tone: 'success' }
  if (value >= 80) return { label: '良好', tone: 'info' }
  if (value >= 60) return { label: '合格', tone: 'warning' }
  return { label: '待改进', tone: 'danger' }
})
const quickScores = [60, 70, 80, 85, 90, 95, 100]
function applyQuickScore(value) {
  reviewScore.value = String(value)
}

function submit() {
  const rawScore = String(reviewScore.value).trim()
  emit('submit', {
    score: rawScore === '' ? null : Number(rawScore),
    feedback: reviewFeedback.value.trim(),
  })
}
</script>

<template>
  <aside class="review-panel" aria-label="评分工作台">
    <header class="panel-head">
      <div>
        <h2>评分工作台</h2>
        <p>评分保存后将同步给学生</p>
      </div>
    </header>

    <!-- 执行证据：学生提交时的运行情况汇总 -->
    <section class="card evidence-card" aria-label="执行证据">
      <div class="evidence-stats">
        <div class="stat"><strong>{{ evidence.total }}</strong><span>内容块</span></div>
        <div class="stat"><strong>{{ evidence.codeCells }}</strong><span>代码 Cell</span></div>
        <div class="stat" :class="{ dim: evidence.executed === 0 }"><strong>{{ evidence.executed }}</strong><span>已运行</span></div>
        <div class="stat" :class="{ alert: evidence.errors > 0 }"><strong>{{ evidence.errors }}</strong><span>执行异常</span></div>
      </div>
      <div class="evidence-footer">
        <p class="evidence-hint" :class="evidenceHint.tone">
          <AppIcon :name="evidenceHint.tone === 'muted' ? 'info' : evidenceHint.tone === 'success' ? 'check' : 'warning'" :size="14" />
          {{ evidenceHint.text }}
        </p>
        <span class="attempt-pill">第 {{ submission?.attempt_number || 1 }} 次提交</span>
      </div>
    </section>

    <!-- ② 调整最终分数 -->
    <section class="card score-card" aria-label="成绩">
      <header class="card-head">
        <div>
          <h3>最终成绩</h3>
          <p>满分 100 分，可精确到 0.5</p>
        </div>
        <span class="status-pill" :class="isGraded ? 'graded' : 'pending'">{{ isGraded ? '已评分' : '待评分' }}</span>
      </header>
      <div class="score-input">
        <input
          id="review-score"
          v-model="reviewScore"
          type="number"
          min="0"
          max="100"
          step="0.5"
          inputmode="decimal"
          aria-label="最终分数"
          placeholder="—"
        />
        <span class="score-max">/ 100 满分</span>
        <span v-if="scoreLevel" class="score-level" :class="scoreLevel.tone">{{ scoreLevel.label }}</span>
      </div>
      <div v-if="isGraded" class="score-current">上次评分：{{ submission.score }} 分</div>
      <div class="quick-scores" aria-label="快捷分数">
        <button
          v-for="value in quickScores"
          :key="value"
          type="button"
          class="score-chip"
          :class="{ active: parsedScore === value }"
          @click="applyQuickScore(value)"
        >
          {{ value }}
        </button>
      </div>
    </section>

    <!-- ③ 教师反馈 -->
    <label class="card feedback-card" for="review-feedback">
      <header class="card-head feedback-head">
        <h3>教师反馈</h3>
        <span class="feedback-count">{{ reviewFeedback.length }} / 500</span>
      </header>
      <textarea
        id="review-feedback"
        v-model="reviewFeedback"
        maxlength="500"
        placeholder="请输入对学生提交内容的评价与建议…"
      ></textarea>
    </label>

    <!-- ④ 发布评分 -->
    <footer class="publish-card">
      <div v-if="submission?.reviewed_at" class="reviewed-time">
        <AppIcon name="clock" :size="15" />
        上次保存于 {{ formatDateTime(submission.reviewed_at) }}
      </div>
      <button type="button" class="save-button" :disabled="saving" @click="submit">
        <AppIcon name="check" :size="17" />
        {{ saving ? '保存中…' : '发布评分' }}
      </button>
    </footer>
  </aside>
</template>

<style scoped>
/* ── 面板布局：吸顶占满视口高度，反馈区弹性拉伸，消除大块空白 ── */
.review-panel {
  position: sticky;
  top: 72px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: calc(100dvh - 320px);
  min-height: 0;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: none;
}
.review-panel > * { flex-shrink: 0; }
.panel-head {
  min-height: 58px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.panel-head h2 { margin: 0 0 3px; color: var(--fg); font-size: 15px; }
.panel-head p { margin: 0; color: var(--muted); font-size: 11px; }

/* ── 卡片分区 ───────────────────────────────────────────────── */
.card {
  margin: 0 14px;
  padding: 12px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
}
.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.card-head h3 { margin: 0 0 3px; color: var(--fg); font-size: 12.5px; font-weight: 600; }
.card-head p { margin: 0; color: var(--muted); font-size: 10.5px; }
.attempt-pill {
  display: inline-flex;
  flex: 0 0 auto;
  padding: 3px 8px;
  margin-top: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--surface-subtle);
  color: var(--muted);
  font-size: 10.5px;
  white-space: nowrap;
}

/* ① 执行证据 */
.evidence-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.evidence-footer {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
}
.stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 7px 4px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-subtle);
}
.stat strong { color: var(--fg); font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat span { color: var(--faint); font-size: 10.5px; }
.stat.dim strong { color: var(--faint); }
.stat.alert strong { color: var(--danger); }
.evidence-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  padding: 7px 10px;
  border-radius: var(--radius-md);
  font-size: 11px;
  line-height: 1.5;
}
.evidence-hint.success { background: var(--success-bg); color: var(--success); }
.evidence-hint.warning { background: var(--warning-bg); color: var(--warning); }
.evidence-hint.muted { background: var(--surface-subtle); color: var(--muted); }

/* ② 成绩 */
.score-input { display: flex; align-items: center; gap: 10px; }
.score-input input {
  width: 118px;
  height: 44px;
  color: var(--accent);
  font-size: 24px;
  font-weight: 700;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.score-max { color: var(--muted); font-size: 11px; white-space: nowrap; }
.score-level {
  display: inline-flex;
  padding: 4px 10px;
  border-radius: var(--radius-full);
  font-size: 11px;
  font-weight: 600;
}
.score-level.success { background: var(--success-bg); color: var(--success); }
.score-level.info { background: var(--info-bg); color: var(--info); }
.score-level.warning { background: var(--warning-bg); color: var(--warning); }
.score-level.danger { background: var(--danger-bg); color: var(--danger); }
.score-current { margin-top: 6px; color: var(--muted); font-size: 11px; }
.quick-scores { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.score-chip {
  min-width: 40px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition: all var(--duration-fast, 120ms);
}
.score-chip:hover { border-color: var(--accent); color: var(--accent); }
.score-chip.active { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }

/* ③ 反馈：弹性拉伸占满剩余高度，消除右侧空白 */
.feedback-card {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 200px;
  padding: 12px 16px;
  cursor: default;
}
.feedback-head { align-items: center; margin-bottom: 8px; }
.feedback-head h3 { margin: 0; }
.feedback-count { color: var(--faint); font-size: 10.5px; white-space: nowrap; }
.feedback-card textarea {
  flex: 1;
  min-height: 150px;
  resize: vertical;
  line-height: 1.65;
}

/* ④ 发布评分 */
.publish-card {
  position: sticky;
  bottom: 0;
  margin-top: auto;
  padding: 8px 18px 14px;
  background: var(--surface);
}
.reviewed-time {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0 8px;
  color: var(--faint);
  font-size: 11px;
}
.save-button {
  width: 100%;
  height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid var(--accent);
  border-radius: var(--radius-md);
  background: var(--accent);
  color: var(--surface);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.save-button:hover:not(:disabled) { border-color: var(--accent-hover); background: var(--accent-hover); box-shadow: var(--shadow-sm); }
.save-button:disabled { opacity: .55; cursor: not-allowed; }

/* 双栏折叠为单栏时：面板回归自然高度 */
@media (max-width: 1199px) {
  .review-panel { position: static; height: auto; min-height: 0; overflow-y: visible; }
  .publish-card { position: static; }
  .feedback-card { flex: none; }
  .feedback-card textarea { min-height: 200px; }
}
</style>
