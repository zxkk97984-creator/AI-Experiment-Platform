<script setup>
// 管理员练习设置页：判题方式切换（普通 / AI）+ 额度与并发配置 + AI 预检未就绪清单。
// 六态：加载 / 空数据（暂无未就绪题目）/ 失败 / 排队（队列积压）/ 额度耗尽 / 权限不足。
import { computed, onMounted, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import DashboardAsyncState from '../../components/dashboard/DashboardAsyncState.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import UiStatusPill from '../../components/ui/UiStatusPill.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import { gradingModeMeta, missingRequirementsText, practiceErrorMessage } from '../../components/practice/practiceHelpers.js'
import { adminPracticeAPI } from '../../api/practice.js'
import { formatDateTime } from '../../utils/format.js'
import { useAppStore } from '../../stores/app.js'

const app = useAppStore()

const loading = ref(true)
const error = ref('')
const forbidden = ref(false)
const saving = ref(false)
const switching = ref(false)
const quotaExhausted = ref(false)
const settings = ref(null)
const preflight = ref(null)
const blocked = ref([])
const form = ref({
  daily_submission_cap_per_student: 0,
  daily_ai_budget_tokens: 0,
  daily_ai_budget_amount: 0,
  max_concurrent_practice_jobs: 1,
})

const currentMode = computed(() => settings.value?.grading_mode || 'deterministic')
const modeMeta = computed(() => gradingModeMeta(currentMode.value))
const notReady = computed(() => preflight.value?.problems || [])
const preflightReady = computed(() => preflight.value?.ready === true)
const queue = computed(() => preflight.value?.queue || {})
const aiBudget = computed(() => preflight.value?.ai_budget || {})

async function load() {
  loading.value = true
  error.value = ''
  forbidden.value = false
  try {
    const [settingsRes, preflightRes] = await Promise.all([
      adminPracticeAPI.getSettings(),
      adminPracticeAPI.getAiPreflight(),
    ])
    settings.value = settingsRes.data
    form.value = {
      daily_submission_cap_per_student: settingsRes.data?.daily_submission_cap_per_student ?? 0,
      daily_ai_budget_tokens: settingsRes.data?.daily_ai_budget_tokens ?? 0,
      daily_ai_budget_amount: settingsRes.data?.daily_ai_budget_amount ?? 0,
      max_concurrent_practice_jobs: settingsRes.data?.max_concurrent_practice_jobs ?? 1,
    }
    preflight.value = preflightRes.data
    quotaExhausted.value = preflightRes.data?.ai_budget?.exhausted === true
  } catch (e) {
    if (e.response?.status === 403) forbidden.value = true
    error.value = practiceErrorMessage(e, '练习设置加载失败，请稍后重试')
    app.showToast(error.value, 'error')
  } finally {
    loading.value = false
  }
}

async function switchMode(mode) {
  if (mode === currentMode.value || switching.value) return
  switching.value = true
  blocked.value = []
  try {
    const res = await adminPracticeAPI.updateSettings({ grading_mode: mode })
    settings.value = res.data
    app.showToast(mode === 'ai' ? '已切换为 AI 综合判题，仅影响新提交' : '已切换为普通判题，仅影响新提交', 'success')
    await load()
  } catch (e) {
    const detail = e.response?.data?.detail
    if (detail?.code === 'AI_PREFLIGHT_FAILED') {
      // 全局错误信封只保留 {code, message, fields}（app/main.py _normalize_detail）
      blocked.value = detail.fields?.problems || []
      app.showToast('AI 预检未通过，请先补齐未就绪题目', 'error')
    } else {
      app.showToast(practiceErrorMessage(e, '切换失败，请稍后重试'), 'error')
    }
  } finally {
    switching.value = false
  }
}

async function saveQuota() {
  saving.value = true
  try {
    const res = await adminPracticeAPI.updateSettings({
      daily_submission_cap_per_student: Number(form.value.daily_submission_cap_per_student) || 0,
      daily_ai_budget_tokens: Number(form.value.daily_ai_budget_tokens) || 0,
      daily_ai_budget_amount: Number(form.value.daily_ai_budget_amount) || 0,
      max_concurrent_practice_jobs: Number(form.value.max_concurrent_practice_jobs) || 1,
    })
    settings.value = res.data
    app.showToast('额度与并发配置已保存', 'success')
  } catch (e) {
    if (e.response?.status === 429) quotaExhausted.value = true
    app.showToast(practiceErrorMessage(e, '保存失败，请稍后重试'), 'error')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="detail">
    <section class="practice-settings">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE ADMIN</p>
          <h1>练习设置</h1>
          <p class="lead subtitle">全局判题方式、AI 额度与练习队列并发；切换只影响新提交，历史提交保留原模式</p>
        </div>
        <div class="ph-actions">
          <UiStatusPill :tone="modeMeta.tone" :label="`当前：${modeMeta.label}`" />
        </div>
      </section>

      <PracticeNotice v-if="forbidden" tone="danger" title="权限不足" assertive>
        练习设置仅管理员可修改，教师账号只能查看只读排行榜。
      </PracticeNotice>

      <PracticeNotice v-else-if="quotaExhausted" tone="danger" title="今日额度已用完" assertive>
        AI 预算已用尽，新的 AI 判题提交会被拒绝并显示「今日额度已用完」；普通判题不受影响。
      </PracticeNotice>

      <PracticeNotice
        v-if="!forbidden && Number(queue.queued) > 0"
        tone="warning"
        :title="`练习判题队列积压 ${queue.queued} 项`"
      >
        当前运行 {{ queue.running ?? 0 }} / 上限 {{ queue.max_concurrent ?? form.max_concurrent_practice_jobs }}，超额提交会排队并显示名次。
      </PracticeNotice>

      <DashboardAsyncState v-if="!forbidden" :loading="loading" :error="Boolean(error)" :empty="false" @retry="load">
        <div class="settings-grid">
          <section class="settings-card">
            <h2 class="card-title">判题方式</h2>
            <p class="card-sub">默认普通判题；切换为 AI 综合判题前会做全量预检。</p>
            <div class="segmented" role="group" aria-label="判题方式">
              <button
                type="button"
                :class="{ active: currentMode === 'deterministic' }"
                :aria-pressed="currentMode === 'deterministic'"
                :disabled="switching"
                @click="switchMode('deterministic')"
              >普通判题</button>
              <button
                type="button"
                :class="{ active: currentMode === 'ai' }"
                :aria-pressed="currentMode === 'ai'"
                :disabled="switching"
                @click="switchMode('ai')"
              >AI 综合判题</button>
            </div>
            <p class="card-hint">
              历史提交保留提交时刻的判题方式与成绩，不重算、不迁移；两种模式的榜单分别统计。
            </p>

            <div class="ready-row">
              <UiStatusPill
                :tone="preflightReady ? 'success' : 'warning'"
                :label="preflightReady ? 'AI 预检通过' : `AI 预检有 ${notReady.length} 道题未就绪`"
              />
              <button type="button" class="btn btn-ghost btn-sm" @click="load">
                <AppIcon name="refresh" :size="14" />重新预检
              </button>
            </div>

            <div v-if="blocked.length" class="blocked-box" role="alert">
              <p class="blocked-title">切换被拒绝，以下题目尚未准备完成：</p>
              <ul class="blocked-list">
                <li v-for="item in blocked" :key="item.id">
                  <strong>{{ item.title }}</strong>
                  <span class="blocked-reason">{{ missingRequirementsText(item.missing) || '评分标准未锁定或未启用' }}</span>
                </li>
              </ul>
            </div>
          </section>

          <section class="settings-card">
            <h2 class="card-title">额度与并发</h2>
            <p class="card-sub">0 表示不限制；额度按日重置，用 Redis 计数并以数据库为准。</p>
            <div class="field-grid">
              <label class="field">
                <span class="field-label">每生每日提交上限</span>
                <input v-model.number="form.daily_submission_cap_per_student" class="input" type="number" min="0" />
              </label>
              <label class="field">
                <span class="field-label">每日 AI 预算（tokens）</span>
                <input v-model.number="form.daily_ai_budget_tokens" class="input" type="number" min="0" />
              </label>
              <label class="field">
                <span class="field-label">每日 AI 预算（金额）</span>
                <input v-model.number="form.daily_ai_budget_amount" class="input" type="number" min="0" step="0.01" />
              </label>
              <label class="field">
                <span class="field-label">练习队列并发上限</span>
                <input v-model.number="form.max_concurrent_practice_jobs" class="input" type="number" min="1" max="64" />
              </label>
            </div>
            <div class="card-actions">
              <button type="button" class="btn btn-primary" :disabled="saving" @click="saveQuota">
                {{ saving ? '保存中…' : '保存配置' }}
              </button>
            </div>
            <p v-if="aiBudget.tokens_budget" class="card-hint">
              今日 AI 用量 {{ aiBudget.tokens_used ?? 0 }} / {{ aiBudget.tokens_budget }} tokens
            </p>
          </section>

          <section class="settings-card wide">
            <h2 class="card-title">AI 预检未就绪清单</h2>
            <p class="card-sub">已发布题目必须同时具备锁定的评分标准与已启用的评分版本。</p>
            <ul v-if="notReady.length" class="ready-list">
              <li v-for="item in notReady" :key="item.id" class="ready-item">
                <div class="ready-main">
                  <strong>{{ item.title }}</strong>
                  <span class="ready-missing">缺少：{{ missingRequirementsText(item.missing) || '评分标准或评分版本' }}</span>
                </div>
                <span class="badge badge-warning">{{ item.status === 'published' ? '已发布' : item.status }}</span>
                <router-link class="btn btn-secondary btn-sm" :to="`/admin/practice/problems/${item.id}/edit`">去补齐</router-link>
              </li>
            </ul>
            <div v-else class="ready-empty">
              <p class="ready-empty-title">全部题目已就绪</p>
              <p class="card-hint">当前没有缺少评分标准或评分版本的已发布题目。</p>
            </div>
            <p v-if="preflight?.checked_at" class="card-hint">预检时间：{{ formatDateTime(preflight.checked_at) }}</p>
          </section>
        </div>
      </DashboardAsyncState>
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-settings { display: flex; flex-direction: column; gap: var(--space-4); }
.settings-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: var(--space-4); align-items: start; }
.settings-card { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }
.settings-card.wide { grid-column: 1 / -1; }
.card-title { margin: 0; font-size: var(--text-md); }
.card-sub { margin: 0; color: var(--muted); font-size: var(--text-md); }
.card-hint { margin: var(--space-2) 0 0; color: var(--faint); font-size: var(--text-sm); }
.field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-3); }
.card-actions { display: flex; gap: var(--space-2); }
.ready-row { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.blocked-box { padding: var(--space-3); border: 1px solid var(--danger); border-radius: var(--radius-md); background: var(--danger-bg); }
.blocked-title { margin: 0 0 var(--space-2); font-size: var(--text-md); color: var(--danger); font-weight: 600; }
.blocked-list, .ready-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: var(--space-2); }
.blocked-list li { display: flex; flex-direction: column; font-size: var(--text-md); }
.blocked-reason { color: var(--muted); font-size: var(--text-sm); }
.ready-item { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-md); }
.ready-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.ready-main strong { overflow-wrap: anywhere; }
.ready-missing { color: var(--muted); font-size: var(--text-sm); }
.ready-empty { padding: var(--space-3); border: 1px dashed var(--border-strong); border-radius: var(--radius-md); text-align: center; }
.ready-empty-title { margin: 0; font-weight: 600; color: var(--muted); }
@media (max-width: 640px) {
  .field-grid { grid-template-columns: 1fr; }
}
</style>
