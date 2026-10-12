<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import TeacherExamPaper from '../../components/teacher/exam/TeacherExamPaper.vue'
import { examsAPI } from '../../api/exams.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'
import { areaPathForRole } from '../../router/roleHome.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const area = (suffix) => areaPathForRole((route.path || '').startsWith('/admin') ? 'admin' : 'teacher', suffix)
const loading = ref(true)
const savingKey = ref(null)
const revertKey = ref(0)
const scoreErrors = reactive({})
const scoreDialog = ref(null)
const scoreReason = ref('')
const scoreReasonError = ref('')
const payload = ref({ exam: {}, student: {}, submission: {}, analysis: {}, answers: [], questions: [] })

const totalScore = computed(() => Number(payload.value.submission.score || 0))
const totalPossible = computed(() => {
  if (payload.value.submission.points_snapshot_complete === false) return null
  const source = payload.value.questions?.length ? payload.value.questions : payload.value.answers || []
  return source.reduce((sum, item) => sum + Number(item.points || 0), 0) || 100
})
const scorePercent = computed(() => totalPossible.value
  ? Math.min(100, Math.round(totalScore.value * 100 / totalPossible.value)) : null)
const accuracy = computed(() => payload.value.analysis.question_count ? Math.round(payload.value.analysis.correct_count * 100 / payload.value.analysis.question_count) : 0)
const rankPercent = computed(() => scorePercent.value == null ? null : Math.min(99, Math.max(1, scorePercent.value)))
const pointsSnapshotMissing = computed(() => payload.value.submission.points_snapshot_complete === false)
const canEditScores = computed(() => !pointsSnapshotMissing.value
  && ['graded', 'review_required'].includes(payload.value.submission.status))
const editModeLabel = computed(() => pointsSnapshotMissing.value
  ? '缺少可信分值基线' : payload.value.submission.status === 'review_required'
    ? '待复核 · 可逐题改分' : '已完成 · 可逐题改分')

function printReport() { window.print() }

async function load() {
  loading.value = true
  try {
    const res = await examsAPI.getGradeDetail(route.params.id, route.params.submissionId)
    payload.value = res.data
  } catch { app.showToast('加载成绩详情失败', 'error') }
  finally { loading.value = false }
}

function findQuestion(questionId) {
  return (payload.value.questions || []).find((question) => Number(question.id) === Number(questionId))
}

function openScoreDialog({ answerId, questionId, score }) {
  if (!canEditScores.value) return
  const question = findQuestion(questionId) || {}
  const answer = (payload.value.answers || []).find((item) => Number(item.question_id) === Number(questionId))
  scoreDialog.value = {
    answerId,
    questionId,
    score,
    expectedResultRevision: answer?.result_revision ?? 0,
    points: question.points ?? answer?.points ?? 0,
    originalScore: answer?.score,
    number: (question.order_index ?? answer?.order_index ?? 0) + 1,
  }
  scoreReason.value = ''
  scoreReasonError.value = ''
}

function cancelScoreChange() {
  scoreDialog.value = null
  scoreReason.value = ''
  scoreReasonError.value = ''
  revertKey.value += 1
}

async function confirmScoreChange() {
  const pending = scoreDialog.value
  if (!pending) return
  const reason = scoreReason.value.trim()
  if (reason.length < 3) {
    scoreReasonError.value = '请填写改分理由（至少 3 个字）'
    return
  }
  const key = pending.answerId ?? `question-${pending.questionId}`
  scoreDialog.value = null
  scoreReasonError.value = ''
  savingKey.value = key
  delete scoreErrors[key]
  try {
    const request = pending.answerId == null
      ? examsAPI.updateGradeQuestionScore(route.params.id, route.params.submissionId, pending.questionId, pending.score, reason, pending.expectedResultRevision)
      : examsAPI.updateGradeAnswerScore(route.params.id, route.params.submissionId, pending.answerId, pending.score, reason, pending.expectedResultRevision)
    const res = await request
    payload.value = res.data
    app.showToast('本题得分已更新', 'success')
  } catch (error) {
    const message = error.response?.data?.detail?.message || '保存分数失败'
    scoreErrors[key] = message
    revertKey.value += 1
    app.showToast(message, 'error')
  } finally {
    savingKey.value = null
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="workspace">
    <section v-if="!loading" class="detail-page">
      <button class="back-link" @click="router.push(area(`/exams/${route.params.id}/grades`))"><AppIcon name="back" :size="17" />返回成绩总览</button>
      <header class="page-head"><div><h1>学生成绩详情</h1><p>考试名称：{{ payload.exam.title }}</p></div></header>

      <section class="identity-card">
        <div class="identity-item student"><span class="avatar">{{ payload.student.name?.slice(0, 1) || '学' }}</span><span><small>学生姓名</small><strong>{{ payload.student.name }}</strong></span></div>
        <div class="identity-item"><span class="identity-icon blue"><AppIcon name="user" :size="21" /></span><span><small>学号</small><strong>{{ payload.student.number }}</strong></span></div>
        <div class="identity-item"><span class="identity-icon purple"><AppIcon name="exam" :size="21" /></span><span><small>考试名称</small><strong>{{ payload.exam.title }}</strong></span></div>
        <div class="identity-item"><span class="identity-icon green"><AppIcon name="course" :size="21" /></span><span><small>所属课程</small><strong>{{ payload.exam.course_title }}</strong></span></div>
        <div class="identity-item"><span class="identity-icon orange"><AppIcon name="clock" :size="21" /></span><span><small>提交时间</small><strong>{{ formatDateTime(payload.submission.submitted_at) }}</strong></span></div>
        <div class="final-score"><small>总分</small><strong>{{ payload.submission.score ?? '—' }}<em> 分</em></strong><span class="complete-pill">{{ payload.submission.status === 'review_required' ? '待复核' : '已完成' }}</span></div>
      </section>
      <p v-if="payload.submission.points_snapshot_complete === false" class="snapshot-warning" role="alert">
        该提交缺少考试开始时的分值快照，系统不会使用当前题目分值代替；请先修复历史基线后再修改正式成绩。
      </p>

      <section class="content-grid">
        <article class="question-panel">
          <header class="paper-panel-head">
            <div>
              <p class="eyebrow">TEACHER REVIEW</p>
              <h2>题目得分明细 · 试卷讲评</h2>
              <p class="panel-subtitle">按学生端试卷版式展示学生作答与标准答案，每道题均可修改得分。</p>
            </div>
            <span class="edit-mode-badge" :class="{ review: payload.submission.status === 'review_required' }">{{ canEditScores ? editModeLabel : '当前状态不可改分' }}</span>
          </header>

          <TeacherExamPaper
            :questions="payload.questions || []"
            :answers="payload.answers || []"
            :editable="canEditScores"
            :saving-key="savingKey"
            :revert-key="revertKey"
            :score-errors="scoreErrors"
            @save-score="openScoreDialog"
          />
        </article>

        <aside class="analysis-column">
          <article class="analysis-card"><h2>成绩分析</h2><div class="analysis-grid"><span><i class="analysis-icon blue"><AppIcon name="clipboard" :size="20" /></i><small>客观题得分</small><strong>{{ payload.analysis.objective_score }} <em>/ {{ payload.analysis.objective_total }}</em></strong></span><span><i class="analysis-icon green"><AppIcon name="code" :size="20" /></i><small>编程题得分</small><strong>{{ payload.analysis.code_score }} <em>/ {{ payload.analysis.code_total }}</em></strong></span><span><i class="analysis-icon purple"><AppIcon name="pie" :size="20" /></i><small>正确率</small><strong>{{ accuracy }}%</strong></span><span><i class="analysis-icon slate"><AppIcon name="clock" :size="20" /></i><small>用时</small><strong>{{ payload.submission.elapsed_minutes ?? '—' }} <em>分钟</em></strong></span><span><i class="analysis-icon purple"><AppIcon name="user" :size="20" /></i><small>成绩水平</small><strong>{{ scorePercent == null ? '待核对' : scorePercent >= 90 ? '优秀' : scorePercent >= 60 ? '合格' : '待提升' }}</strong></span><span><i class="analysis-icon blue"><AppIcon name="trophy" :size="20" /></i><small>超过参考</small><strong>{{ rankPercent == null ? '—' : `${rankPercent}%` }}</strong></span></div></article>

          <article class="teacher-card"><h2>评阅说明</h2><p v-if="payload.submission.review_reason">{{ payload.submission.review_reason }}</p><p v-else>系统已完成本次考试评分。左侧按学生端试卷版式展示标准答案与学生作答；在“已完成”或“待复核”状态下，可直接修改每题得分（0 至本题满分）。</p></article>

          <article class="ability-card"><h2>能力维度分析</h2><div class="score-ring" :style="{ background: `conic-gradient(var(--accent) 0 ${scorePercent ?? 0}%, var(--surface-subtle) ${scorePercent ?? 0}% 100%)` }"><span><strong>{{ payload.submission.score ?? '—' }}</strong><small>总分</small></span></div><div class="dimension-list"><span><small>基础知识</small><strong>{{ payload.analysis.objective_score }} / {{ payload.analysis.objective_total }}</strong><i><b :style="{ width: `${payload.analysis.objective_total ? payload.analysis.objective_score / payload.analysis.objective_total * 100 : 0}%` }"></b></i></span><span><small>程序设计能力</small><strong>{{ payload.analysis.code_score }} / {{ payload.analysis.code_total }}</strong><i><b :style="{ width: `${payload.analysis.code_total ? payload.analysis.code_score / payload.analysis.code_total * 100 : 0}%` }"></b></i></span><span><small>总体完成度</small><strong>{{ scorePercent == null ? '—' : `${scorePercent}%` }}</strong><i><b :style="{ width: `${scorePercent ?? 0}%` }"></b></i></span></div></article>
        </aside>
      </section>

      <div v-if="scoreDialog" class="modal-backdrop" @click.self="cancelScoreChange">
        <div class="score-dialog" v-dialog-focus role="dialog" aria-modal="true" aria-label="确认修改分数">
          <h2>确认修改分数</h2>
          <p class="score-dialog__summary">
            第 {{ scoreDialog.number }} 题：将得分从
            <strong>{{ scoreDialog.originalScore ?? '未作答' }}</strong>
            修改为 <strong>{{ scoreDialog.score }}</strong> / {{ scoreDialog.points }} 分
          </p>
          <label class="score-dialog__reason">
            <span>改分理由</span>
            <textarea v-model="scoreReason" rows="4" placeholder="请填写修改分数的理由（至少 3 个字）"></textarea>
          </label>
          <p v-if="scoreReasonError" class="score-dialog__error">{{ scoreReasonError }}</p>
          <div class="score-dialog__actions">
            <button type="button" class="btn-ghost" @click="cancelScoreChange">取消</button>
            <button type="button" class="btn-primary" @click="confirmScoreChange">确认修改</button>
          </div>
        </div>
      </div>

      <footer class="action-footer"><button @click="printReport"><AppIcon name="clipboard" :size="18" />打印 / 保存 PDF</button><button class="btn-primary" @click="router.push(area(`/exams/${route.params.id}/grades`))">返回成绩总览</button></footer>
    </section>
    <section v-else class="detail-loading"><span v-for="i in 6" :key="i" class="skeleton"></span></section>
  </AppLayout>
</template>

<style scoped>
.snapshot-warning{margin:0 0 14px;padding:12px 14px;border:1px solid var(--warning);border-radius:var(--radius-md);background:var(--warning-bg);color:var(--warning);font-size:13px;line-height:1.6}
.detail-page{display:flex;flex-direction:column;gap:15px;padding-bottom:78px}.back-link{align-self:flex-start;padding:0;border:0;color:var(--accent);background:transparent}.page-head h1{margin:0 0 4px;color:var(--fg);font-size:28px;letter-spacing:-.025em}.page-head p{margin:0;color:var(--muted);font-size:14px}
.identity-card{display:grid;grid-template-columns:1.1fr repeat(4,1fr) 1.05fr;align-items:center;min-height:88px;padding:13px 17px;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface)}.identity-item{display:flex;align-items:center;gap:10px;min-width:0;padding:0 12px}.identity-item.student{padding-left:0}.avatar,.identity-icon{display:grid;place-items:center;flex:none;width:42px;height:42px;border-radius:50%}.avatar{color:var(--surface);background:linear-gradient(135deg,var(--info),var(--info))}.identity-icon{border-radius: var(--radius-lg)}.identity-icon.blue{color:var(--accent);background:var(--accent-soft)}.identity-icon.purple{color:var(--info);background:var(--info-bg)}.identity-icon.green{color:var(--success);background:var(--success-bg)}.identity-icon.orange{color:var(--warning);background:var(--warning-bg)}.identity-item span:last-child{min-width:0}.identity-item small,.identity-item strong{display:block}.identity-item small{color:var(--muted);font-size:12px}.identity-item strong{overflow:hidden;margin-top:3px;color:var(--fg);font-size:13px;text-overflow:ellipsis;white-space:nowrap}.final-score{display:grid;grid-template-columns:1fr auto;gap:2px 10px;padding-left:18px;border-left:1px solid var(--border)}.final-score small{color:var(--muted);font-size:12px}.final-score strong{color:var(--accent);font-size:26px;line-height:1}.final-score em{font-size:12px;font-style:normal}.complete-pill{grid-column:2;grid-row:1/3;align-self:center;padding:4px 9px;border-radius: var(--radius-full);color:var(--success);background:var(--success-bg);font-size:11px}
.content-grid{display:grid;grid-template-columns:1.12fr .88fr;gap:15px}.question-panel,.analysis-card,.teacher-card,.ability-card{border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:none}
.question-panel{overflow:hidden;min-width:0}
.paper-panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:16px 18px;border-bottom:1px solid var(--border)}
.paper-panel-head h2{margin:0;color:var(--fg);font-size:15px}.eyebrow{margin:0 0 5px;color:var(--accent);font:700 10px/1 var(--font-mono);letter-spacing:.14em}.panel-subtitle{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.6}
.edit-mode-badge{flex:none;padding:5px 9px;border-radius: var(--radius-full);color:var(--success);background:var(--success-bg);font-size:11px}.edit-mode-badge.review{color:var(--warning);background:var(--warning-bg)}
.analysis-column{display:flex;flex-direction:column;gap:12px}.analysis-card,.teacher-card,.ability-card{padding:16px}.analysis-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:13px}.analysis-grid>span{display:grid;grid-template-columns:34px 1fr;align-items:center;padding:10px;border:1px solid var(--border);border-radius: var(--radius-md)}.analysis-icon{grid-row:1/3;display:grid;place-items:center;width:30px;height:30px;border-radius: var(--radius-md);font-style:normal}.analysis-icon.blue{color:var(--accent);background:var(--accent-soft)}.analysis-icon.green{color:var(--success);background:var(--success-bg)}.analysis-icon.purple{color:var(--info);background:var(--info-bg)}.analysis-icon.slate{color:var(--muted);background:var(--surface-subtle)}.analysis-grid small{color:var(--muted);font-size:11px}.analysis-grid strong{color:var(--fg);font-size:17px}.analysis-grid em{color:var(--muted);font-size:11px;font-style:normal}.teacher-card p{margin:9px 0 0;color:var(--muted);font-size:13px;line-height:1.7}.ability-card{display:grid;grid-template-columns:126px 1fr;gap:8px 20px}.ability-card h2{grid-column:1/-1}.analysis-card h2,.teacher-card h2,.ability-card h2{margin:0;color:var(--fg);font-size:15px}.score-ring{display:grid;place-items:center;width:116px;height:116px;margin-top:10px;border-radius:50%;position:relative}.score-ring::after{content:'';position:absolute;width:82px;height:82px;border-radius:50%;background:var(--surface)}.score-ring span{z-index:1;display:grid;text-align:center}.score-ring strong{color:var(--fg);font-size:24px}.score-ring small{color:var(--muted);font-size:11px}.dimension-list{display:grid;align-content:center;gap:11px}.dimension-list span{display:grid;grid-template-columns:1fr auto;gap:5px}.dimension-list small{color:var(--muted)}.dimension-list strong{color:var(--fg);font-size:12px}.dimension-list i{grid-column:1/-1;height:5px;overflow:hidden;border-radius: var(--radius-full);background:var(--surface-subtle)}.dimension-list b{display:block;height:100%;border-radius:inherit;background:var(--accent)}
.action-footer{position:fixed;z-index:5;right:0;bottom:0;left:var(--sidebar-width,260px);display:flex;justify-content:center;gap:12px;padding:12px;border-top:1px solid var(--border);background:oklch(0.99 0.001 95 / 0.96);backdrop-filter:blur(8px)}.action-footer button{min-width:145px}.detail-loading{display:grid;gap:14px}.detail-loading .skeleton{height:90px}
.modal-backdrop{position:fixed;inset:0;z-index:300;display:grid;place-items:center;padding:20px;background:oklch(0.2 0.01 150 / 0.35)}
.score-dialog{display:grid;gap:14px;width:min(440px,calc(100vw - 32px));padding:20px;border:1px solid var(--border);border-radius: var(--radius-lg);background:var(--surface);box-shadow:var(--shadow-lg)}
.score-dialog h2{margin:0;color:var(--fg);font-size:18px}
.score-dialog__summary{margin:0;color:var(--muted);font-size:13px;line-height:1.7}
.score-dialog__summary strong{color:var(--fg);font-variant-numeric:tabular-nums}
.score-dialog__reason{display:grid;gap:6px}
.score-dialog__reason span{color:var(--muted);font-size:12px}
.score-dialog__reason textarea{min-height:88px;padding:9px 11px;border:1px solid var(--border);border-radius: var(--radius-md);background:var(--surface);color:var(--fg);font:13px/1.6 var(--font-sans);resize:vertical}
.score-dialog__reason textarea:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
.score-dialog__error{margin:0;color:var(--danger);font-size:12px}
.score-dialog__actions{display:flex;justify-content:flex-end;gap:10px}
.score-dialog__actions button{min-width:110px}
@media(max-width:1250px){.identity-card{grid-template-columns:repeat(3,1fr);gap:14px}.final-score{border-left:0}.content-grid{grid-template-columns:1fr}.analysis-grid{grid-template-columns:repeat(3,1fr)}}@media(max-width:760px){.identity-card{grid-template-columns:1fr 1fr}.analysis-grid{grid-template-columns:1fr 1fr}.ability-card{grid-template-columns:1fr}.ability-card h2{grid-column:auto}.score-ring{justify-self:center}.action-footer{left:0;overflow:auto;justify-content:flex-start}.action-footer button{min-width:130px}}@media print{.back-link,.action-footer{display:none}.detail-page{padding:0}.content-grid{grid-template-columns:1fr}}
</style>
