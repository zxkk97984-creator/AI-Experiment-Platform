<script setup>
// AIGradingReviewDetailView（V2）：AI 评分复核工作台。
// 证据链 = 确定性测试（实线） + AI 判断（虚线） + 教师终审（accent 实线）。
// 业务逻辑与 API 不变：加载、定位证据行、覆盖提交、原始 JSON 复制、高级信息审计。

import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import CodeViewer from '../../components/ai/CodeViewer.vue'
import TeacherReviewPanel from '../../components/ai/TeacherReviewPanel.vue'
import { aiGradingAPI } from '../../api/aiGrading.js'
import { useAuthStore } from '../../stores/auth.js'
import {
  fmtDateTime, feedbackBlocks, modeText, reviewState, safeNumber, statusText, testSummary,
} from '../../utils/gradingUi.js'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const gradeId = route.params.id
const detail = ref(null)
const loading = ref(true)
const error = ref('')

const submitting = ref(false)
const activeLine = ref(null)
const codeViewerRef = ref(null)
const reviewPanelRef = ref(null)

const basePath = computed(() => (auth.isAdmin ? '/admin/ai-grading' : '/teacher/ai-grading'))
const state = computed(() => reviewState(detail.value))
const blocks = computed(() => feedbackBlocks(detail.value?.ai_result?.student_feedback))
const summary = computed(() => testSummary(detail.value?.deterministic_details?.groups))

const pageTitle = computed(() => `提交 #${gradeId} · 评分详情`)

const testScore = computed(() => {
  const groups = detail.value?.deterministic_details?.groups || []
  const score = groups.reduce((sum, g) => sum + (Number(g.score) || 0), 0)
  const max = groups.reduce((sum, g) => sum + (Number(g.max_score) || 0), 0)
  return max > 0 ? `${score} / ${max}` : '—'
})

const aiDimensions = computed(() => {
  const r = detail.value?.ai_result
  if (!r) return []
  return [
    { key: 'algorithm', title: '算法关键步骤', items: r.algorithm?.items || [] },
    { key: 'quality', title: '代码质量', items: r.code_quality?.items || [] },
  ]
})

const evidenceLines = computed(() => {
  const seen = new Set()
  for (const dim of aiDimensions.value) {
    for (const item of dim.items) {
      for (const n of item.code_lines || []) {
        if (Number.isInteger(n) && n > 0) seen.add(n)
      }
    }
  }
  return [...seen].sort((a, b) => a - b)
})

function levelText(level) {
  const map = { complete: '完成', partial: '部分完成', missing: '未得分' }
  return map[level] || level || ''
}

function copyRaw() {
  const text = detail.value?.raw_response || ''
  if (!text) return
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text))
  } else {
    fallbackCopy(text)
  }
}

function fallbackCopy(text) {
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  } catch {
    /* 静默失败 */
  }
}

const submittedLabel = computed(() => {
  const v = detail.value?.submitted_at
  return v ? `提交时间 ${fmtDateTime(v)}` : ''
})

const summaryLine = computed(() => {
  const d = detail.value
  if (!d || !d.deterministic_details?.groups?.length) return ''
  const parts = [`测试通过 ${summary.value.passed} / ${summary.value.total}`]
  if (summary.value.failed > 0) parts.push(`失败 ${summary.value.failed}`)
  if (summary.value.errors > 0) parts.push(`错误 ${summary.value.errors}`)
  if (d.execution_time_ms != null) parts.push(`运行时间 ${safeNumber(d.execution_time_ms)} ms`)
  return parts.join(' · ')
})

function focusLine(line) {
  activeLine.value = line
  codeViewerRef.value?.focusLine?.(line)
}

async function handleOverride(payload) {
  submitting.value = true
  error.value = ''
  try {
    await aiGradingAPI.overrideGrade(gradeId, payload)
    await fetchDetail()
    reviewPanelRef.value?.clearDraft()
    reviewPanelRef.value?.closeEditor()
  } catch (e) {
    error.value = e.response?.data?.detail?.message || e.message || '覆盖失败'
  } finally {
    submitting.value = false
  }
}

function goBack() {
  const query = {}
  for (const key of ['kind', 'status', 'page']) {
    if (route.query[key] != null) query[key] = route.query[key]
  }
  router.push({ path: basePath.value, query })
}

async function fetchDetail() {
  loading.value = true
  error.value = ''
  try {
    const res = await aiGradingAPI.getGrade(gradeId)
    detail.value = res.data
  } catch (e) {
    error.value = e.response?.data?.detail?.message || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(fetchDetail)
</script>

<template>
  <AppLayout content-mode="workspace">
    <div class="grading-workspace">
      <div v-if="loading" class="empty"><div class="empty-mark"><span class="skeleton" style="width: 20px; height: 20px;"></span></div><h3>正在加载评分详情</h3></div>

      <div v-else-if="error" class="error-panel">
        <div class="grow"><div class="e-title">评分详情加载失败</div><div class="e-body">{{ error }}</div></div>
        <button type="button" class="btn btn-secondary btn-sm" @click="fetchDetail">重试</button>
      </div>

      <template v-else-if="detail">
        <section class="page-head grading-head">
          <div class="ph-title">
            <p class="eyebrow">提交 #{{ gradeId }} · {{ submittedLabel }}</p>
            <h1>{{ pageTitle }}</h1>
            <div class="row-wrap grading-context">
              <UiStatusPill :tone="state.tone" :label="state.label" />
              <span v-if="detail.student_name" class="meta">{{ detail.student_name }}</span>
              <span v-if="detail.student_username" class="meta">{{ detail.student_username }}</span>
              <span v-if="detail.course_title" class="meta">{{ detail.course_title }}</span>
            </div>
          </div>
          <div class="ph-actions">
            <button type="button" class="btn btn-ghost btn-sm" @click="goBack">返回列表</button>
            <button v-if="detail.raw_response" type="button" class="btn btn-secondary btn-sm" @click="copyRaw">复制原始 JSON</button>
          </div>
        </section>

        <details v-if="detail.question_title" class="question-context"><summary>查看完整题目</summary><p>{{ detail.question_title }}</p></details>

        <section class="row-wrap evidence-legend" aria-label="评分证据图例">
          <span class="evidence-tag deterministic">▣ 确定性证据 · 测试结果</span>
          <span class="evidence-tag ai">◈ AI 判断 · 模型分析</span>
          <span class="evidence-tag teacher">● 教师决策 · 最终结果</span>
          <span class="muted legend-text">实线 = 可复核事实 · 虚线 = 模型建议 · 绿实线 = 教师终审</span>
        </section>

        <section class="metric-strip score-strip" aria-label="评分总览">
          <div class="metric">
            <span class="m-label" style="color: var(--success);">测试结果 · 确定性</span>
            <span class="m-value" style="font-size: 26px;">{{ testScore }}</span>
            <span class="m-sub">{{ summaryLine || '暂无测试摘要' }}</span>
          </div>
          <div class="metric">
            <span class="m-label" style="color: var(--info);">AI 建议分</span>
            <span class="m-value" style="font-size: 26px;">{{ detail.ai_result ? safeNumber(detail.ai_result.final_score_100 ?? detail.final_score_100) : '—' }} / 100</span>
            <span class="m-sub">{{ detail.ai_result?.rubric_version ? `rubric v${detail.ai_result.rubric_version}` : 'rubric 未记录' }}</span>
          </div>
          <div class="metric em">
            <span class="m-label" style="color: var(--accent);">教师最终得分</span>
            <span class="m-value" style="font-size: 26px;">{{ detail.final_score_100 != null ? safeNumber(detail.final_score_100) : '—' }} / 100</span>
            <span class="m-sub">{{ detail.overrides?.length ? '已由教师调整' : '待确认后对学生可见' }}</span>
          </div>
        </section>

        <div class="workbench">
          <div class="wb-col stack wb-col--left">
            <div class="code-panel">
              <div class="code-panel-head">
                <span class="fname">submission.py</span>
                <span class="lang">Python · 已定位 {{ evidenceLines.length }} 处证据</span>
              </div>
              <CodeViewer
                ref="codeViewerRef"
                :code="detail.student_code || ''"
                filename="submission"
                :highlight-lines="evidenceLines"
                :active-line="activeLine"
              />
            </div>

            <div v-if="detail.deterministic_details" class="evidence-block deterministic">
              <div class="row-between">
                <span class="eyebrow" style="color: var(--success);">测试结果 · 确定性证据</span>
                <span class="status-dot"><span class="dot" style="background: var(--success);"></span>通过 {{ summary.passed }} / {{ summary.total }}</span>
              </div>
              <div class="stack test-groups">
                <div v-for="g in detail.deterministic_details.groups" :key="g.id" class="test-group">
                  <div class="row-between"><span>{{ g.name || g.id }} · {{ g.dimension === 'F' ? '功能正确性' : '鲁棒性与性能' }}</span><span class="num">{{ safeNumber(g.score) }}/{{ safeNumber(g.max_score) }}</span></div>
                  <div class="score-bar"><i :class="safeNumber(g.counts?.failed) + safeNumber(g.counts?.errors) > 0 ? 'warn' : 'ok'" :style="{ width: (safeNumber(g.max_score) > 0 ? (safeNumber(g.score) / safeNumber(g.max_score)) * 100 : 0) + '%' }"></i></div>
                  <p class="muted test-meta">
                    通过 {{ safeNumber(g.counts?.passed) }} · 失败 {{ safeNumber(g.counts?.failed) }} · 错误 {{ safeNumber(g.counts?.errors) }}
                    <template v-if="detail.execution_time_ms != null"> · {{ safeNumber(detail.execution_time_ms) }} ms</template>
                  </p>
                </div>
              </div>
              <ul v-if="detail.deterministic_details.system_errors?.length" class="sys-errors">
                <li v-for="(e, ei) in detail.deterministic_details.system_errors" :key="ei">{{ e }}</li>
              </ul>
            </div>

            <div v-if="detail.ai_result?.student_feedback" class="panel">
              <div class="panel-head"><div class="ph-label"><p class="eyebrow">Student feedback</p><h3>给学生的反馈</h3></div></div>
              <div class="panel-body stack feedback-blocks">
                <div v-for="block in blocks" :key="block.key" class="feedback-block">
                  <h4 class="feedback-block__title">{{ block.title }}</h4>
                  <ul v-if="block.items.length" class="feedback-block__list">
                    <li v-for="(item, i) in block.items" :key="i">{{ item }}</li>
                  </ul>
                  <p v-else class="feedback-block__empty">{{ block.emptyText }}</p>
                </div>
              </div>
            </div>

            <div class="panel">
              <div class="panel-head"><div class="ph-label"><p class="eyebrow">Rubric</p><h3>评分标准 · 过程分</h3></div></div>
              <div class="panel-body stack rubric-body">
                <div v-for="dim in aiDimensions" :key="'rubric-' + dim.key" class="rubric-row">
                  <div class="row-between"><span>{{ dim.title }}</span><span class="num">{{ dim.items.reduce((sum, item) => sum + (Number(item.score) || 0), 0) }} / {{ dim.items.reduce((sum, item) => sum + (Number(item.max_score) || 0), 0) }}</span></div>
                  <div class="score-bar"><i :style="{ width: (dim.items.reduce((sum, item) => sum + (Number(item.max_score) || 0), 0) > 0 ? (dim.items.reduce((sum, item) => sum + (Number(item.score) || 0), 0) / dim.items.reduce((sum, item) => sum + (Number(item.max_score) || 0), 0)) * 100 : 0) + '%' }"></i></div>
                </div>
                <div v-if="detail.ai_result?.rubric" class="rubric-row">
                  <div class="row-between"><span>总分</span><span class="num">{{ safeNumber(detail.ai_result.final_score_100) }} / 100</span></div>
                  <div class="score-bar"><i :style="{ width: safeNumber(detail.ai_result.final_score_100) + '%' }"></i></div>
                </div>
              </div>
            </div>
          </div>

          <div class="wb-col stack wb-col--right">
            <div v-if="detail.ai_result" class="evidence-block ai">
              <div class="row-between">
                <span class="eyebrow" style="color: var(--info);">AI 评分依据 · 模型判断</span>
                <span class="meta">{{ detail.ai_result.rubric_version ? `rubric v${detail.ai_result.rubric_version}` : 'rubric' }}</span>
              </div>
              <div v-for="dim in aiDimensions" :key="dim.key" class="ai-dim">
                <h4 class="ai-dim-title">{{ dim.title }}</h4>
                <div v-if="dim.items.length" class="stack">
                  <div v-for="item in dim.items" :key="item.criterion_id" class="evidence-item">
                    <div class="row-between">
                      <span>{{ item.criterion }}</span>
                      <span class="num">{{ safeNumber(item.score) }}/{{ safeNumber(item.max_score) }}</span>
                    </div>
                    <div class="row-wrap evidence-lines">
                      <span class="badge" :class="item.level === 'complete' ? 'badge-success' : item.level === 'partial' ? 'badge-warning' : 'badge-danger'"><span class="dot"></span>{{ levelText(item.level) }}</span>
                      <button
                        v-for="n in item.code_lines"
                        :key="n"
                        type="button"
                        class="line-chip"
                        @click="focusLine(n)"
                      >第 {{ n }} 行</button>
                    </div>
                    <p v-if="item.evidence" class="muted evidence-text">{{ item.evidence }}</p>
                  </div>
                </div>
                <p v-else class="muted evidence-empty">该维度无评分项</p>
              </div>
            </div>
          </div>
        </div>

        <TeacherReviewPanel
          ref="reviewPanelRef"
          :detail="detail"
          :teacher-id="auth.user?.id"
          :submitting="submitting"
          @submit="handleOverride"
        />

        <details class="advanced-info">
          <summary>高级信息（开发与审计）</summary>
          <div class="advanced-info__body">
            <div class="advanced-info__grid">
              <dl class="kv-list">
                <div class="kv-row"><dt>评分模式</dt><dd>{{ modeText(detail.mode) }}（{{ detail.mode }}）</dd></div>
                <div class="kv-row"><dt>评分状态</dt><dd>{{ statusText(detail.status) }}（{{ detail.status }}）</dd></div>
                <div class="kv-row"><dt>评分尝试</dt><dd>{{ detail.attempt_count }} 次</dd></div>
                <div v-if="detail.ai_result?.rubric_version != null" class="kv-row"><dt>评分规则版本</dt><dd>评分规则版本 {{ detail.ai_result.rubric_version }}</dd></div>
                <div v-if="detail.rubric_id" class="kv-row"><dt>Rubric ID</dt><dd>{{ detail.rubric_id }}</dd></div>
                <div v-if="detail.last_error" class="kv-row"><dt>最近错误</dt><dd class="danger-text">{{ detail.last_error }}</dd></div>
              </dl>

              <div v-if="detail.static_analysis?.metrics" class="static-summary">
                <h4>静态分析</h4>
                <p v-if="detail.static_analysis.parse_error" class="danger-text">语法错误：{{ detail.static_analysis.parse_error }}</p>
                <p v-else>{{ detail.static_analysis.metrics.lines }} 行 · 函数 {{ detail.static_analysis.metrics.functions }} 个 · 圈复杂度 {{ detail.static_analysis.metrics.complexity }}</p>
                <ul v-if="detail.static_analysis.diagnostics?.length" class="diag-list">
                  <li v-for="(d, i) in detail.static_analysis.diagnostics.slice(0, 20)" :key="i">{{ d }}</li>
                </ul>
              </div>
            </div>

            <div v-if="detail.raw_response" class="raw-json code-panel">
              <div class="raw-json__head">
                <span>AI 原始响应</span>
                <button type="button" class="btn btn-ghost btn-sm" @click="copyRaw">复制 JSON</button>
              </div>
              <pre class="raw-json__body"><code>{{ detail.raw_response }}</code></pre>
            </div>
          </div>
        </details>
      </template>
    </div>
  </AppLayout>
</template>

<style scoped>
.grading-workspace { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.grading-head { margin-bottom: 0; }
.grading-context { gap: var(--space-3); }
.evidence-legend { margin-bottom: var(--space-2); }
.legend-text { font-size: var(--text-sm); }
.score-strip { grid-template-columns: repeat(3, 1fr); }

/* 双栏工作台：左列 = 代码/测试/反馈/评分标准，右列 = AI 评分依据。
   桌面端两栏固定等高、各自内部滚动，内容多寡不再造成单侧留白。 */
.workbench {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
  gap: var(--space-4);
  align-items: start;
  min-width: 0;
}
.wb-col.stack { min-width: 0; min-height: 0; }

@media (min-width: 1025px) {
  .workbench {
    height: calc(100vh - var(--header-height) - 2 * var(--page-pad));
    align-items: stretch;
  }
  .wb-col.stack {
    overflow-y: auto;
  }
  /* 保持各面板自然高度，由所在栏内部滚动，不挤压代码卡片 */
  .wb-col.stack > * { flex: 0 0 auto; }
}

.test-groups { gap: 12px; }
.test-groups > * { margin-top: 0; }
.test-group { padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.test-meta { margin: 6px 0 0; font-size: var(--text-sm); }
.sys-errors { margin: 10px 0 0; padding: 0; list-style: none; color: var(--danger); font-size: var(--text-sm); line-height: 1.6; }

.ai-dim { display: flex; flex-direction: column; gap: 10px; }
.ai-dim + .ai-dim { margin-top: 4px; }
.ai-dim-title { margin: 0; font-size: var(--text-md); font-weight: 600; color: var(--fg); }
.evidence-item { padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.evidence-meta { margin-top: 6px; }
.ev-chip {
  display: inline-flex; align-items: center; justify-content: center;
  height: 22px; padding: 0 8px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--surface);
  color: var(--info); font-family: var(--font-mono); font-size: 11px; cursor: pointer;
}
.ev-chip:hover { border-color: var(--info); background: var(--info-bg); }
.evidence-text { margin: 8px 0 0; font-size: var(--text-base); line-height: 1.6; }
.evidence-empty { margin: 0; font-size: var(--text-xs); color: var(--faint); }

.feedback-blocks { gap: 12px; }
.feedback-blocks > * { margin-top: 0; }
.feedback-block { padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.feedback-block__title { margin: 0 0 6px; font-size: var(--text-md); font-weight: 600; color: var(--fg); }
.feedback-block__list { margin: 0; padding: 0; list-style: none; font-size: var(--text-base); color: var(--muted); line-height: 1.7; }
.feedback-block__list li { padding-left: 14px; position: relative; }
.feedback-block__list li::before { content: '·'; position: absolute; left: 2px; color: var(--faint); }
.feedback-block__empty { margin: 0; font-size: var(--text-base); color: var(--faint); }
.rubric-body { gap: 12px; }
.rubric-body > * { margin-top: 0; }
.rubric-row { display: flex; flex-direction: column; gap: 6px; }

.advanced-info { border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.advanced-info summary { cursor: pointer; padding: 12px 16px; font-size: var(--text-md); font-weight: 600; color: var(--muted); list-style: none; }
.advanced-info summary::after { content: '展开'; float: right; color: var(--faint); font-weight: 500; }
.advanced-info[open] summary::after { content: '收起'; }
.advanced-info__body { display: flex; flex-direction: column; gap: 16px; padding: 0 16px 16px; min-width: 0; }
.advanced-info__grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.kv-list { margin: 0; display: flex; flex-direction: column; gap: 6px; }
.kv-row { display: flex; gap: 12px; font-size: var(--text-md); }
.kv-row dt { color: var(--faint); min-width: 90px; }
.kv-row dd { margin: 0; color: var(--muted); font-variant-numeric: tabular-nums; }
.danger-text { color: var(--danger); }
.static-summary h4 { margin: 0 0 6px; font-size: var(--text-md); font-weight: 600; color: var(--muted); }
.static-summary p { margin: 0; font-size: var(--text-md); color: var(--muted); }
.diag-list { margin: 8px 0 0; padding: 0 0 0 18px; font-size: var(--text-xs); color: var(--faint); line-height: 1.6; }
.raw-json { overflow: hidden; }
.raw-json__head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 14px; border-bottom: 1px solid oklch(0.32 0.02 155);
  font-size: var(--text-xs); font-weight: 600; color: oklch(0.78 0.015 155);
}
.raw-json__body {
  margin: 0; max-height: 320px; overflow: auto; padding: 12px 14px;
  color: oklch(0.84 0.01 155); font-family: var(--font-mono);
  font-size: var(--text-base); line-height: 1.6;
}

@media (max-width: 1024px) {
  .workbench { grid-template-columns: 1fr; }
  .score-strip { grid-template-columns: 1fr; }
}
@media (max-width: 820px) {
  .advanced-info__grid { grid-template-columns: 1fr; }
}

.question-context { margin-block: 8px 16px; padding: 12px 16px; border: 1px solid var(--border); border-radius: var(--radius-md); }
.question-context summary { cursor: pointer; color: var(--muted); }
.question-context p { margin: 12px 0 0; max-width: 85ch; line-height: 1.7; }
</style>
