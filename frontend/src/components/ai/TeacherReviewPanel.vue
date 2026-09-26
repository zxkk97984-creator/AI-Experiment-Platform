<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
// TeacherReviewPanel：教师复核卡。只负责表单、校验、总分预览、确认弹窗与本地草稿；
// 通过 emit('submit', payload) 提交，API 调用由页面负责。
// 模式：review_required 默认编辑；completed 默认只读 + "调整评分"入口（保留纠错能力）。

import { computed, onMounted, ref, watch } from 'vue'
import { autoTotal, fmtDateTime, safeNumber } from '../../utils/gradingUi.js'

const props = defineProps({
  detail: { type: Object, required: true },
  teacherId: { type: [Number, null], default: null },
  submitting: { type: Boolean, default: false },
})

const emit = defineEmits(['submit'])

const editing = ref(props.detail?.status === 'review_required')
const a = ref(null)
const q = ref(null)
const useFinal = ref(false)
const final = ref(null)
const reason = ref('')
const confirmOpen = ref(false)
const restoredTip = ref(false)

const DRAFT_VERSION = 1
const DRAFT_TTL_MS = 24 * 60 * 60 * 1000
const draftKey = computed(() => `ai_grade_draft_v1_${props.teacherId}_${props.detail?.id}`)

const isReview = computed(() => props.detail?.status === 'review_required')
const confirmTitle = computed(() => (isReview.value ? '确认复核并生效' : '确认覆盖评分'))
const panelTitle = computed(() => (isReview.value ? '等待教师复核' : '评分已生效'))

const origA = computed(() => (props.detail?.algorithm_score == null ? null : safeNumber(props.detail.algorithm_score)))
const origQ = computed(() => (props.detail?.quality_score == null ? null : safeNumber(props.detail.quality_score)))
const origFinal = computed(() => (props.detail?.final_score_100 == null ? null : safeNumber(props.detail.final_score_100)))

const hasChanges = computed(() => {
  const aChanged = a.value != null && safeNumber(a.value) !== safeNumber(origA.value)
  const qChanged = q.value != null && safeNumber(q.value) !== safeNumber(origQ.value)
  const fChanged = useFinal.value && final.value != null && safeNumber(final.value) !== safeNumber(origFinal.value)
  return aChanged || qChanged || fChanged
})

const reasonOk = computed(() => (reason.value || '').trim().length >= 3)
const valid = computed(() => hasChanges.value && reasonOk.value)

// 总分预览（系统自动计算，提交后以后端合分为准）
const preview = computed(() => autoTotal(props.detail, {
  a: a.value, q: q.value, useFinal: useFinal.value, final: final.value,
}))

const changeSummary = computed(() => {
  const parts = []
  if (a.value != null && safeNumber(a.value) !== safeNumber(origA.value)) {
    parts.push(`算法关键步骤 ${origA.value ?? '—'} → ${a.value}`)
  }
  if (q.value != null && safeNumber(q.value) !== safeNumber(origQ.value)) {
    parts.push(`代码质量 ${origQ.value ?? '—'} → ${q.value}`)
  }
  if (useFinal.value && final.value != null && safeNumber(final.value) !== safeNumber(origFinal.value)) {
    parts.push(`最终得分 ${origFinal.value ?? '—'} → ${final.value}`)
  }
  return parts.join('、') || '—'
})

// ── 本地草稿（按教师隔离，仅本机暂存） ─────────────────────────
function canStore() {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem != null
  } catch {
    return false
  }
}

function loadDraft() {
  if (!canStore()) return false
  try {
    const raw = localStorage.getItem(draftKey.value)
    if (!raw) return false
    const d = JSON.parse(raw)
    if (d.version !== DRAFT_VERSION || !d.savedAt) return false
    const age = Date.now() - new Date(d.savedAt).getTime()
    if (!Number.isFinite(age) || age > DRAFT_TTL_MS) {
      localStorage.removeItem(draftKey.value)
      return false
    }
    a.value = d.a ?? null
    q.value = d.q ?? null
    useFinal.value = !!d.useFinal
    final.value = d.final ?? null
    reason.value = d.reason ?? ''
    return true
  } catch {
    return false
  }
}

function saveDraft() {
  if (!canStore()) return
  try {
    localStorage.setItem(draftKey.value, JSON.stringify({
      version: DRAFT_VERSION,
      savedAt: new Date().toISOString(),
      a: a.value, q: q.value, useFinal: useFinal.value, final: final.value, reason: reason.value,
    }))
  } catch {
    /* 存储不可用时静默跳过 */
  }
}

watch([a, q, useFinal, final, reason], saveDraft)

/** 覆盖成功后由页面调用：清除本机草稿 */
function clearDraft() {
  if (!canStore()) return
  try {
    localStorage.removeItem(draftKey.value)
  } catch {
    /* 忽略 */
  }
}

/** 覆盖成功后由页面调用：关闭调整评分弹窗 */
function closeEditor() {
  editing.value = false
  confirmOpen.value = false
}

defineExpose({ clearDraft, closeEditor })

onMounted(() => {
  if (loadDraft()) {
    restoredTip.value = true
  }
})

// ── 提交 ────────────────────────────────────────────────────────
function buildPayload() {
  const payload = {}
  if (a.value != null && safeNumber(a.value) !== safeNumber(origA.value)) payload.algorithm_score = a.value
  if (q.value != null && safeNumber(q.value) !== safeNumber(origQ.value)) payload.quality_score = q.value
  if (useFinal.value) payload.final_score_100 = final.value
  payload.reason = (reason.value || '').trim()
  return payload
}

function doConfirm() {
  confirmOpen.value = false
  emit('submit', buildPayload())
}

// ── 评分历史（只显示后端真实存在的字段） ────────────────────────
const historyRows = computed(() => {
  const overrides = props.detail?.overrides || []
  return overrides.map((o) => {
    const orig = o.original_snapshot || {}
    const repl = o.replacement_snapshot || {}
    const diff = []
    for (const [k, label] of [
      ['algorithm_score', '算法关键步骤'], ['quality_score', '代码质量'], ['final_score_100', '最终得分'],
    ]) {
      if (orig[k] !== repl[k]) diff.push(`${label} ${orig[k] ?? '—'} → ${repl[k] ?? '—'}`)
    }
    return { id: o.id, diff, reason: o.reason, createdAt: o.created_at }
  })
})
</script>

<template>
  <section class="review-panel evidence-block teacher">
    <header class="review-panel__head">
      <div class="review-panel__heading">
        <h3 class="review-panel__title">教师复核</h3>
        <span class="review-panel__status">{{ panelTitle }}</span>
      </div>
      <div class="review-panel__summary">
        <div class="summary-item">
          <span>当前正式得分</span>
          <strong>{{ origFinal ?? '—' }}</strong>
        </div>
        <div class="summary-item">
          <span>是否经教师调整</span>
          <strong>{{ historyRows.length ? `已调整 ${historyRows.length} 次` : '尚未经过教师调整' }}</strong>
        </div>
      </div>
      <button type="button" class="btn-outline review-edit-btn" @click="editing = true">
        {{ isReview ? '立即复核' : '调整评分' }}
      </button>
    </header>

    <!-- 评分历史 -->
    <div v-if="historyRows.length" class="review-history">
      <h4 class="review-history__title">评分历史</h4>
      <ol class="history-list">
        <li v-for="o in historyRows" :key="o.id" class="history-item">
          <p class="history-item__diff">{{ o.diff.join('；') }}</p>
          <p v-if="o.reason" class="history-item__reason">{{ o.reason }}</p>
          <p v-if="o.createdAt" class="history-item__time">{{ fmtDateTime(o.createdAt) }}</p>
        </li>
      </ol>
    </div>

    <!-- 调整评分弹窗：字段与原先内嵌表单一致，仅从页面内嵌改为弹窗填写 -->
    <div v-if="editing" class="confirm-overlay" @click.self="editing = false">
      <div class="review-dialog" v-dialog-focus role="dialog" aria-modal="true" aria-label="教师复核调整">
        <h3 class="review-dialog__title">{{ isReview ? '教师复核' : '调整评分' }}</h3>

        <div class="review-form">
          <div class="field">
            <label class="field__label" for="ov-a">算法关键步骤 A</label>
            <input id="ov-a" v-model.number="a" type="number" min="0" max="20" step="0.1" />
            <p class="field__hint">当前 {{ origA ?? '—' }} 分，范围 0–20</p>
          </div>

          <div class="field">
            <label class="field__label" for="ov-q">代码质量 Q</label>
            <input id="ov-q" v-model.number="q" type="number" min="0" max="10" step="0.1" />
            <p class="field__hint">当前 {{ origQ ?? '—' }} 分，范围 0–10</p>
          </div>

          <label class="final-switch">
            <input v-model="useFinal" type="checkbox" />
            <span>直接调整总分</span>
          </label>

          <div v-if="useFinal" class="field">
            <label class="field__label" for="ov-final">调整后总分</label>
            <input id="ov-final" v-model.number="final" type="number" min="0" max="100" step="0.1" />
            <p class="field__hint">范围 0–100</p>
          </div>

          <div class="preview">
            <div class="preview__head">
              <span>调整后总分</span>
              <strong class="preview__value">{{ preview.final }}</strong>
            </div>
            <p class="preview__hint">
              {{ useFinal ? '将按手动总分提交' : '系统自动计算（F+R+A+Q）' }}，提交后以后端合分为准
            </p>
          </div>

          <div class="field">
            <label class="field__label" for="ov-reason">调整理由</label>
            <textarea id="ov-reason" v-model="reason" rows="3" placeholder="请填写调整原因..."></textarea>
            <p class="field__hint">必填，将记录在评分历史中</p>
          </div>

          <p v-if="!valid" class="form-error">
            请至少修改一个评分项，并填写调整理由（将记录在评分历史中）。
          </p>
          <p v-if="restoredTip" class="draft-tip">已恢复本机未提交的调整内容</p>
        </div>

        <div class="review-dialog__actions">
          <button type="button" class="btn-outline" @click="editing = false">取消</button>
          <button
            type="button"
            class="btn-primary review-submit"
            :disabled="!valid || submitting"
            @click="confirmOpen = true"
          >
            {{ confirmTitle }}
          </button>
        </div>
      </div>
    </div>

    <!-- 确认弹窗 -->
    <div v-if="confirmOpen" class="confirm-overlay confirm-overlay--above" @click.self="confirmOpen = false">
      <div class="confirm-dialog" v-dialog-focus role="dialog" aria-modal="true" aria-label="确认覆盖评分">
        <h3 class="confirm-dialog__title">确认{{ confirmTitle }}？</h3>
        <dl class="confirm-dialog__list">
          <div class="confirm-row">
            <dt>原始得分</dt>
            <dd>{{ origFinal ?? '—' }}</dd>
          </div>
          <div class="confirm-row">
            <dt>调整后得分</dt>
            <dd>{{ preview.final }}</dd>
          </div>
          <div class="confirm-row">
            <dt>调整项</dt>
            <dd>{{ changeSummary }}</dd>
          </div>
          <div class="confirm-row">
            <dt>理由</dt>
            <dd>{{ (reason || '').trim() }}</dd>
          </div>
        </dl>
        <p class="confirm-dialog__note">该操作将记录到评分历史中。</p>
        <div class="confirm-dialog__actions">
          <button type="button" class="btn-outline" @click="confirmOpen = false">取消</button>
          <button type="button" class="btn-danger" :disabled="submitting" @click="doConfirm">
            {{ confirmTitle }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* V2 教师终审面板：accent 实线证据块 + .field/.input/.textarea + .btn 体系。 */
.review-panel { display: flex; flex-direction: column; gap: 14px; }
.review-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.review-panel__heading { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.review-panel__title { margin: 0; font-size: var(--text-lg); font-weight: 600; color: var(--fg); }
.review-panel__status { font-size: var(--text-xs); font-weight: 600; color: var(--faint); }

.review-panel__summary { display: flex; align-items: center; gap: 24px; flex-wrap: wrap; min-width: 0; }
.summary-item { display: flex; align-items: baseline; gap: 8px; font-size: var(--text-md); color: var(--muted); }
.summary-item strong { color: var(--fg); font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.review-edit-btn { flex-shrink: 0; }

.review-form { display: flex; flex-direction: column; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field__label { font-size: var(--text-sm); font-weight: 500; color: var(--muted); }
.field input, .field textarea {
  width: 100%;
  padding: 0 11px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--fg);
  font-family: var(--font-body);
  font-size: var(--text-md);
}
.field textarea { height: auto; min-height: 72px; padding: 9px 11px; resize: vertical; }
.field input { height: var(--h-input); }
.field input:focus, .field textarea:focus { border-color: var(--accent); outline: none; box-shadow: 0 0 0 3px var(--accent-soft); }
.field__hint { margin: 0; font-size: var(--text-xs); color: var(--faint); }

.final-switch {
  display: flex;
  align-items: center;
  align-self: flex-start;
  gap: 8px;
  min-height: 32px;
  padding: 0 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  font-size: var(--text-base);
  color: var(--muted);
  cursor: pointer;
}
.final-switch:hover { border-color: var(--border-strong); background: var(--surface-subtle); }
.final-switch input { width: 16px; height: 16px; flex: 0 0 16px; margin: 0; padding: 0; accent-color: var(--accent); }
.final-switch span { line-height: 1.4; white-space: nowrap; }

.preview {
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface-sunken);
}
.preview__head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.preview__head span { font-size: var(--text-md); font-weight: 600; color: var(--muted); }
.preview__value { font-size: 28px; line-height: 1; font-weight: 600; color: var(--accent); font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.preview__hint { margin: 8px 0 0; font-size: var(--text-xs); color: var(--muted); line-height: 1.55; }

.form-error { margin: 0; font-size: var(--text-xs); color: var(--danger); line-height: 1.6; }
.draft-tip { margin: 0; font-size: var(--text-xs); color: var(--warning); }
.review-submit { min-width: 120px; justify-content: center; }

.review-dialog {
  width: min(520px, calc(100vw - 32px));
  max-height: 90vh;
  overflow-y: auto;
  padding: 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.review-dialog__title { margin: 0; font-size: var(--text-xl); font-weight: 600; color: var(--fg); }
.review-dialog__actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 2px; }

.review-history { border-top: 1px solid var(--border); padding-top: 12px; display: flex; flex-direction: column; gap: 8px; }
.review-history__title { margin: 0; font-size: var(--text-md); font-weight: 600; color: var(--fg); }
.history-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.history-item { padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-sunken); }
.history-item__diff { margin: 0; font-size: var(--text-md); color: var(--fg); font-variant-numeric: tabular-nums; }
.history-item__reason { margin: 4px 0 0; font-size: var(--text-xs); color: var(--muted); line-height: 1.6; }
.history-item__time { margin: 4px 0 0; font-size: var(--text-xs); color: var(--faint); }

.confirm-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: oklch(0.2 0.01 150 / 0.35);
}
.confirm-overlay--above { z-index: 310; }
.confirm-dialog {
  width: min(420px, calc(100vw - 32px));
  padding: 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.confirm-dialog__title { margin: 0; font-size: var(--text-xl); font-weight: 600; color: var(--fg); }
.confirm-dialog__list { margin: 0; display: flex; flex-direction: column; gap: 8px; }
.confirm-row { display: flex; gap: 12px; font-size: var(--text-md); }
.confirm-row dt { color: var(--faint); min-width: 84px; flex-shrink: 0; }
.confirm-row dd { margin: 0; color: var(--fg); word-break: break-word; font-variant-numeric: tabular-nums; }
.confirm-dialog__note { margin: 0; font-size: var(--text-xs); color: var(--faint); }
.confirm-dialog__actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 4px; }

/* 兜底按钮名 → V2 语义 */
.btn-outline { background: var(--surface); border-color: var(--border-strong); color: var(--fg); }
.btn-outline:hover:not(:disabled) { border-color: var(--fg); }
.btn-danger { background: var(--danger); border-color: var(--danger); color: var(--surface); }
.btn-danger:hover:not(:disabled) { background: color-mix(in oklch, var(--danger) 88%, black); border-color: var(--danger); }
.btn-danger:disabled, .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
