<script setup>
// 管理员练习复核页：处理自主练习的 AI 成绩异议（维持 / 改分 / 重评 / 驳回）。
// 六态：加载 / 空数据（没有待处理异议）/ 失败 / 排队（重评在判题队列中）/ 额度耗尽 / 权限不足。
import { computed, onMounted, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import DsSelect from '../../components/ui/DsSelect.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { listErrorVisible, usePracticeLoad } from '../../components/practice/usePracticeLoad.js'
import { formatScore, listOf, practiceErrorMessage } from '../../components/practice/practiceHelpers.js'
import { adminPracticeAPI } from '../../api/practice.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'

const app = useAppStore()

const appeals = ref([])
const total = ref(0)
const statusFilter = ref('pending')
const meta = ref({})
const active = ref(null)
const busy = ref(false)
const handleError = ref('')
const form = ref({ action: 'uphold', comment: '', score: '' })

const { loading, error, forbidden, quotaExhausted, load } = usePracticeLoad(async () => {
  const res = await adminPracticeAPI.listAppeals({
    status: statusFilter.value || undefined,
    page: 1,
    page_size: 50,
  })
  appeals.value = listOf(res.data)
  total.value = Number(res.data?.total ?? appeals.value.length)
  meta.value = res.data?.meta || {}
  return res
}, { fallbackMessage: '练习异议加载失败，请稍后重试' })

const APPEAL_STATUS = {
  pending: { label: '待处理', tone: 'warning' },
  withdrawn: { label: '学生已撤回', tone: 'neutral' },
  upheld: { label: '维持原分', tone: 'neutral' },
  adjusted: { label: '已改分', tone: 'success' },
  regrading: { label: '重评中', tone: 'info' },
  rejected: { label: '已驳回', tone: 'danger' },
}
const ACTIONS = [
  { value: 'uphold', label: '维持原分' },
  { value: 'adjust', label: '人工改分' },
  { value: 'regrade', label: '发起重评' },
  { value: 'reject', label: '驳回申请' },
]
const queue = computed(() => meta.value.queue || {})
const pendingCount = computed(() => appeals.value.filter((item) => item.status === 'pending').length)
const adjustmentValid = computed(() => {
  if (form.value.action !== 'adjust') return true
  const score = Number(form.value.score)
  return Number.isFinite(score) && score >= 0 && score <= 100
})

function statusMeta(value) {
  return APPEAL_STATUS[value] || APPEAL_STATUS.pending
}

function openHandle(appeal) {
  active.value = appeal
  handleError.value = ''
  form.value = { action: 'uphold', comment: '', score: appeal.score_before ?? '' }
}

async function submitHandle() {
  if (!active.value || busy.value || !adjustmentValid.value) return
  busy.value = true
  handleError.value = ''
  try {
    const payload = { action: form.value.action, comment: form.value.comment }
    if (form.value.action === 'adjust') payload.score = Number(form.value.score)
    await adminPracticeAPI.handleAppeal(active.value.id, payload)
    app.showToast('异议已处理，学生将收到通知', 'success')
    active.value = null
    await load()
  } catch (e) {
    handleError.value = practiceErrorMessage(e, '处理失败，请稍后重试')
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="practice-appeals">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE ADMIN</p>
          <h1>练习复核</h1>
          <p class="lead subtitle">处理自主练习的 AI 成绩异议；正式教学任务的异议由对应教师处理</p>
        </div>
        <div class="ph-actions">
          <DsSelect v-model="statusFilter" aria-label="按状态筛选" @update:model-value="load">
            <option value="pending">待处理</option>
            <option value="">全部状态</option>
            <option value="regrading">重评中</option>
            <option value="adjusted">已改分</option>
            <option value="upheld">维持原分</option>
            <option value="rejected">已驳回</option>
          </DsSelect>
        </div>
      </section>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        练习异议仅管理员可处理；教师账号只能查看只读排行榜。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        AI 预算已用尽，「发起重评」需要等额度恢复后才会执行；维持与人工改分不受影响。
      </PracticeNotice>

      <PracticeNotice
        v-if="!forbidden && Number(queue.queued) > 0"
        tone="warning"
        :title="`练习判题队列排队 ${queue.queued} 项`"
      >
        重评请求会进入练习队列，处理完成前异议保持「重评中」。
      </PracticeNotice>

      <DashboardAsyncState
        :loading="loading"
        :error="listErrorVisible(error, forbidden)"
        :empty="!appeals.length"
        :empty-title="statusFilter === 'pending' ? '没有待处理的练习异议' : '暂无练习异议记录'"
        empty-body="学生提交异议后会出现在这里。"
        @retry="load"
      >
        <div v-if="appeals.length" class="table-wrap">
          <div class="table-scroll">
            <table class="ds-table">
              <caption class="sr-only">练习异议列表</caption>
              <thead>
                <tr>
                  <th scope="col">题目</th>
                  <th scope="col">学生</th>
                  <th scope="col">申请理由</th>
                  <th scope="col">分数</th>
                  <th scope="col">状态</th>
                  <th scope="col">申请时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="appeal in appeals" :key="appeal.id">
                  <td class="title-cell">{{ appeal.problem_title || `题目 #${appeal.target_id}` }}</td>
                  <td>
                    <strong>{{ appeal.student_name || `学生 #${appeal.student_id}` }}</strong>
                    <small v-if="appeal.student_no" class="student-no">{{ appeal.student_no }}</small>
                  </td>
                  <td class="reason-cell" :title="appeal.reason">{{ appeal.reason }}</td>
                  <td class="mono">
                    {{ formatScore(appeal.score_before) }}
                    <template v-if="appeal.score_after !== null && appeal.score_after !== undefined"> → {{ formatScore(appeal.score_after) }}</template>
                  </td>
                  <td><UiStatusPill :tone="statusMeta(appeal.status).tone" :label="statusMeta(appeal.status).label" /></td>
                  <td class="time">{{ appeal.created_at ? formatDateTime(appeal.created_at) : '—' }}</td>
                  <td>
                    <button
                      v-if="appeal.status === 'pending'"
                      type="button"
                      class="btn btn-secondary btn-sm"
                      @click="openHandle(appeal)"
                    >处理</button>
                    <span v-else class="muted">已处理</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </DashboardAsyncState>

      <p v-if="!loading && appeals.length" class="list-note">
        当前筛选下共 {{ total }} 条，其中待处理 {{ pendingCount }} 条。
      </p>

      <div v-if="active" class="modal-backdrop" @click.self="active = null">
        <section class="modal" role="dialog" aria-modal="true" aria-label="处理练习成绩异议">
          <header class="modal-head"><h2>处理练习成绩异议</h2></header>
          <div class="modal-body">
            <p class="appeal-target">
              《{{ active.problem_title }}》 · {{ active.student_name }}
              <span class="mono">当前分 {{ formatScore(active.score_before) }}</span>
            </p>
            <blockquote class="appeal-quote">{{ active.reason }}</blockquote>
            <label class="field">
              <span class="field-label">处理方式</span>
              <DsSelect v-model="form.action" aria-label="处理方式">
                <option v-for="action in ACTIONS" :key="action.value" :value="action.value">{{ action.label }}</option>
              </DsSelect>
            </label>
            <label v-if="form.action === 'adjust'" class="field">
              <span class="field-label">调整后的总分（0–100）</span>
              <input v-model="form.score" class="input" type="number" min="0" max="100" step="0.1" />
            </label>
            <label class="field">
              <span class="field-label">处理说明（学生可见）</span>
              <textarea v-model="form.comment" class="textarea" rows="3" maxlength="2000" placeholder="说明维持原因、改分依据或重评理由"></textarea>
            </label>
            <p v-if="handleError" class="handle-error" role="alert">{{ handleError }}</p>
          </div>
          <footer class="modal-foot">
            <button type="button" class="btn btn-ghost" :disabled="busy" @click="active = null">取消</button>
            <button
              type="button"
              class="btn btn-primary"
              :disabled="busy || !adjustmentValid"
              @click="submitHandle"
            >{{ busy ? '处理中…' : '确认处理' }}</button>
          </footer>
        </section>
      </div>
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-appeals { display: flex; flex-direction: column; gap: var(--space-4); }
.title-cell { white-space: normal; max-width: 220px; overflow-wrap: anywhere; }
.student-no { display: block; color: var(--faint); font-size: var(--text-sm); }
.reason-cell { white-space: normal; max-width: 320px; color: var(--muted); }
.time { color: var(--muted); }
.list-note { margin: 0; color: var(--muted); font-size: var(--text-sm); }
.modal-body { display: flex; flex-direction: column; gap: var(--space-3); }
.appeal-target { margin: 0; font-size: var(--text-md); }
.appeal-quote { margin: 0; padding: var(--space-3); border-left: 3px solid var(--border-strong); background: var(--surface-subtle); color: var(--fg); font-size: var(--text-md); line-height: var(--lh-body); white-space: pre-wrap; }
.handle-error { margin: 0; color: var(--danger); }
</style>
