<script setup>
// 学生通知中心（批次 2 / G-14）：AI 评分完成、教师改分、成绩异议进度与结果。
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import { notificationsAPI } from '../../api/notifications.js'
import { appealsAPI } from '../../api/appeals.js'
import { useAppStore } from '../../stores/app.js'
import { formatDateTime } from '../../utils/format.js'

const router = useRouter()
const app = useAppStore()
const items = ref([])
const unreadCount = ref(0)
const loading = ref(true)
const failed = ref(false)
const unreadOnly = ref(false)
const marking = ref(false)

// 「我的成绩异议」：查看进度、补充说明、撤回，以及发起新申请
const tab = ref('notifications')
const appeals = ref([])
const appealsLoading = ref(false)
const appealsFailed = ref(false)
const replyTarget = ref(null)
const replyText = ref('')
const applying = ref(false)
const applyForm = reactive({ target_kind: 'submission', target_id: null, reason: '' })
const appealStatusText = { pending: '待处理', regrading: '重评中', adjusted: '已改分', upheld: '维持原分', rejected: '未受理', withdrawn: '已撤回' }

const typeMeta = {
  grading: { label: '评分', icon: 'brain' },
  appeal: { label: '成绩异议', icon: 'clipboard' },
  work: { label: '待办', icon: 'assignment' },
  announcement: { label: '公告', icon: 'notification' },
}

const visibleItems = computed(() => items.value)
const hasItems = computed(() => visibleItems.value.length > 0)

async function load() {
  loading.value = true
  failed.value = false
  try {
    const res = await notificationsAPI.list(unreadOnly.value ? { unread_only: true } : {})
    items.value = res.data.items || []
    unreadCount.value = res.data.unread_count || 0
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
}

async function openItem(item) {
  if (!item.is_read) {
    try {
      await notificationsAPI.markRead(item.id)
      item.is_read = true
      unreadCount.value = Math.max(0, unreadCount.value - 1)
    } catch {
      app.showToast('标记已读失败，请稍后重试', 'error')
    }
  }
  if (item.route) router.push(item.route)
}

async function markAllRead() {
  if (marking.value || unreadCount.value === 0) return
  marking.value = true
  try {
    await notificationsAPI.markAllRead()
    app.showToast('已全部标记为已读', 'success')
    await load()
  } catch {
    app.showToast('操作失败，请稍后重试', 'error')
  } finally {
    marking.value = false
  }
}

function toggleUnreadOnly() {
  unreadOnly.value = !unreadOnly.value
  load()
}

async function loadAppeals() {
  appealsLoading.value = true
  appealsFailed.value = false
  try {
    const res = await appealsAPI.mine()
    appeals.value = res.data.items || []
  } catch {
    appealsFailed.value = true
  } finally {
    appealsLoading.value = false
  }
}

function switchTab(next) {
  if (tab.value === next) return
  tab.value = next
  if (next === 'appeals' && appeals.value.length === 0 && !appealsLoading.value) loadAppeals()
}

function startReply(appeal) {
  replyTarget.value = appeal
  replyText.value = appeal.student_reply || ''
}

async function submitReply() {
  if (!replyTarget.value || !replyText.value.trim()) return
  try {
    await appealsAPI.reply(replyTarget.value.id, replyText.value.trim())
    app.showToast('补充说明已提交', 'success')
    replyTarget.value = null
    await loadAppeals()
  } catch {
    app.showToast('补充说明提交失败，请稍后重试', 'error')
  }
}

async function withdrawAppeal(appeal) {
  try {
    await appealsAPI.withdraw(appeal.id)
    app.showToast('已撤回异议，可重新申请', 'success')
    await loadAppeals()
  } catch (e) {
    app.showToast(e.response?.data?.detail?.message || '撤回失败，请稍后重试', 'error')
  }
}

async function submitApply() {
  if (applying.value) return
  const reason = applyForm.reason.trim()
  if (!applyForm.target_id) { app.showToast('请填写成绩记录 ID', 'error'); return }
  if (reason.length < 20 || reason.length > 2000) { app.showToast('异议说明需 20-2000 字', 'error'); return }
  applying.value = true
  try {
    await appealsAPI.create({ target_kind: applyForm.target_kind, target_id: Number(applyForm.target_id), reason })
    app.showToast('异议已提交，处理结果会在这里更新', 'success')
    applyForm.target_id = null
    applyForm.reason = ''
    await loadAppeals()
  } catch (e) {
    const detail = e.response?.data?.detail
    app.showToast(detail?.message || '提交失败，请稍后重试', 'error')
  } finally {
    applying.value = false
  }
}

onMounted(() => { load(); loadAppeals() })
</script>

<template>
  <AppLayout content-mode="list">
    <section class="notification-center">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">NOTIFICATIONS</p>
          <h1>通知中心</h1>
          <p class="lead subtitle">评分结果、成绩异议进度与教师处理结果都会显示在这里</p>
        </div>
        <div class="ph-actions">
          <span class="count-pill"><span></span>{{ unreadCount }} 条未读</span>
          <button type="button" class="btn btn-secondary btn-sm" :disabled="marking || unreadCount === 0" @click="markAllRead">全部已读</button>
        </div>
      </section>

      <nav class="filter-tabs" aria-label="通知视图切换">
        <button type="button" class="tab" :class="{ active: tab === 'notifications' }" @click="switchTab('notifications')">通知列表</button>
        <button type="button" class="tab" :class="{ active: tab === 'appeals' }" @click="switchTab('appeals')">我的成绩异议</button>
      </nav>

      <template v-if="tab === 'appeals'">
        <section class="appeal-panel" aria-label="我的成绩异议">
          <DashboardAsyncState
            :loading="appealsLoading"
            :error="appealsFailed"
            :empty="appeals.length === 0"
            empty-title="还没有提交过成绩异议"
            empty-body="在下方填写成绩记录信息即可申请复核，处理进度会在这里更新"
            @retry="loadAppeals"
          >
            <article v-for="appeal in appeals" :key="appeal.id" class="notice-card">
              <div class="notice-body">
                <header class="notice-head">
                  <h2 class="notice-title">{{ appeal.title || '成绩记录' }} #{{ appeal.target_id }}</h2>
                  <span class="notice-tag">{{ appealStatusText[appeal.status] || appeal.status }}</span>
                </header>
                <p class="notice-content">{{ appeal.reason }}</p>
                <p v-if="appeal.handler_comment" class="notice-content">教师处理：{{ appeal.handler_comment }}</p>
                <p v-if="appeal.score_after != null" class="notice-content">分数：{{ appeal.score_before ?? '—' }} → {{ appeal.score_after }}</p>
                <footer class="notice-foot">
                  <template v-if="appeal.status === 'pending' || appeal.status === 'regrading'">
                    <button type="button" class="text-btn" @click="startReply(appeal)">补充说明</button>
                    <button type="button" class="text-btn" @click="withdrawAppeal(appeal)">撤回申请</button>
                  </template>
                  <span v-else>已处理完成</span>
                </footer>
              </div>
            </article>
          </DashboardAsyncState>

          <section class="appeal-apply" aria-label="申请成绩异议">
            <h3>申请成绩异议</h3>
            <p class="notice-content">仅覆盖实际参与计分的 AI 评分结果（作业、考试编程题、实验），成绩公布后 7 天内可申请；总评成绩册不提供异议入口。</p>
            <div class="appeal-form">
              <label class="field">
                <span>成绩类型</span>
                <DsSelect v-model="applyForm.target_kind">
                  <option value="submission">作业提交</option>
                  <option value="exam_answer">考试编程题</option>
                  <option value="lab_submission">实验提交</option>
                </DsSelect>
              </label>
              <label class="field">
                <span>成绩记录 ID</span>
                <input v-model.number="applyForm.target_id" class="input" type="number" min="1" step="1" />
              </label>
            </div>
            <label class="field">
              <span>异议说明（20-2000 字）</span>
              <textarea v-model="applyForm.reason" class="input" rows="3" placeholder="说明你认为评分有误的具体理由"></textarea>
            </label>
            <button type="button" class="btn btn-primary btn-sm" :disabled="applying" @click="submitApply">提交异议</button>
          </section>
        </section>
      </template>

      <nav v-if="tab === 'notifications'" class="filter-tabs" aria-label="通知筛选">
        <button type="button" class="tab" :class="{ active: !unreadOnly }" @click="unreadOnly && toggleUnreadOnly()">全部通知</button>
        <button type="button" class="tab" :class="{ active: unreadOnly }" @click="!unreadOnly && toggleUnreadOnly()">仅未读</button>
      </nav>

      <section v-if="tab === 'notifications'" class="notice-list" aria-label="通知列表">
        <DashboardAsyncState
          :loading="loading"
          :error="failed"
          :empty="!hasItems"
          :empty-title="unreadOnly ? '没有未读通知' : '暂无通知'"
          :empty-body="unreadOnly ? '切换「全部通知」可以查看历史消息' : '评分或异议处理产生结果后会显示在这里'"
          @retry="load"
        >
          <article
            v-for="item in visibleItems"
            :key="item.id"
            class="notice-card"
            :class="{ unread: !item.is_read }"
          >
            <div class="notice-mark" aria-hidden="true"><AppIcon :name="(typeMeta[item.type] || {}).icon || 'notification'" :size="16" /></div>
            <div class="notice-body">
              <header class="notice-head">
                <h2 class="notice-title">{{ item.title }}</h2>
                <span class="notice-tag">{{ (typeMeta[item.type] || {}).label || item.type }}</span>
                <span v-if="!item.is_read" class="unread-dot" aria-label="未读"></span>
              </header>
              <p v-if="item.content" class="notice-content">{{ item.content }}</p>
              <footer class="notice-foot">
                <time>{{ formatDateTime(item.created_at) }}</time>
                <button v-if="item.route" type="button" class="text-btn" @click="openItem(item)">查看详情</button>
                <button v-else-if="!item.is_read" type="button" class="text-btn" @click="openItem(item)">标记已读</button>
              </footer>
            </div>
          </article>
        </DashboardAsyncState>
      </section>

      <div v-if="replyTarget" class="handle-mask" role="dialog" aria-modal="true" aria-label="补充异议说明">
        <div class="handle-card">
          <h2>补充说明（异议 #{{ replyTarget.id }}）</h2>
          <label class="field">
            <span>补充内容</span>
            <textarea v-model="replyText" class="input" rows="3"></textarea>
          </label>
          <footer class="handle-actions">
            <button type="button" class="btn btn-secondary btn-sm" @click="replyTarget = null">取消</button>
            <button type="button" class="btn btn-primary btn-sm" :disabled="!replyText.trim()" @click="submitReply">提交</button>
          </footer>
        </div>
      </div>
    </section>
  </AppLayout>
</template>

<style scoped>
.notification-center { display: flex; flex-direction: column; gap: var(--space-5); min-width: 0; }
.ph-actions { display: flex; align-items: center; gap: var(--space-3); }
.filter-tabs { display: flex; gap: var(--space-2); }
.tab { height: 32px; padding: 0 var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); color: var(--muted); font-size: var(--text-base); cursor: pointer; }
.tab.active { border-color: var(--accent); background: var(--accent-soft); color: var(--fg); font-weight: 600; }
.notice-list { display: flex; flex-direction: column; gap: var(--space-3); }
.notice-card { display: flex; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.notice-card.unread { border-color: var(--accent); }
.notice-mark { display: grid; place-items: center; width: 32px; height: 32px; flex: none; border-radius: var(--radius-md); background: var(--surface-sunken); color: var(--muted); }
.notice-body { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: var(--space-1); }
.notice-head { display: flex; align-items: center; gap: var(--space-2); }
.notice-title { margin: 0; font-size: var(--text-base); font-weight: 600; }
.notice-tag { padding: 2px 8px; border-radius: var(--radius-sm); background: var(--surface-sunken); color: var(--faint); font-size: var(--text-sm); }
.unread-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
.notice-content { margin: 0; color: var(--muted); font-size: var(--text-base); }
.notice-foot { display: flex; align-items: center; gap: var(--space-3); color: var(--faint); font-size: var(--text-sm); }
.text-btn { border: none; background: none; color: var(--accent); font-size: var(--text-sm); cursor: pointer; }
.appeal-panel { display: flex; flex-direction: column; gap: var(--space-3); }
.appeal-apply { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.appeal-apply h3 { margin: 0; font-size: var(--text-base); }
.appeal-form { display: flex; gap: var(--space-3); flex-wrap: wrap; }
.appeal-form .field { min-width: 160px; }
.field { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-sm); color: var(--muted); }
.handle-mask { position: fixed; inset: 0; z-index: 40; display: grid; place-items: center; padding: var(--space-4); background: rgba(15, 23, 42, 0.45); }
.handle-card { width: min(520px, 100%); display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-5); border-radius: var(--radius-lg); background: var(--surface); }
.handle-card h2 { margin: 0; font-size: var(--text-lg); }
.handle-actions { display: flex; justify-content: flex-end; gap: var(--space-2); }

@media (max-width: 560px) {
  .notice-card { flex-direction: column; }
  .ph-actions { flex-wrap: wrap; }
}
</style>
