<script setup>
import { onMounted, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { accountAPI, errorMessage } from '../../api/accountManagement.js'
const policy = ref(null)
const days = ref(7)
const current = ref('')
const error = ref('')
const message = ref('')
const busy = ref(false)
const events = ref([])
const page = ref(1)
const total = ref(0)
const target = ref('')
async function loadAudit() {
  try { const { data } = await accountAPI.audit({ page: page.value, target_id: target.value || undefined }); events.value = data.items; total.value = data.total }
  catch (e) { error.value = errorMessage(e) }
}
onMounted(async () => { try { policy.value = (await accountAPI.policy()).data; days.value = policy.value.temporary_days; await loadAudit() } catch (e) { error.value = errorMessage(e) } })
async function save() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    policy.value = (await accountAPI.updatePolicy({ current_password: current.value, temporary_days: days.value })).data
    current.value = ''; message.value = '策略已保存，仅影响之后确认的新建或重置操作'; await loadAudit()
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>
<template>
  <AppLayout content-mode="form"><div class="account-page">
    <header><h1>账号安全与审计</h1><p class="account-muted">学生初始化和重置使用每人独立的随机临时密码。</p></header>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <section class="account-panel"><h2>临时密码策略</h2>
      <p v-if="policy && !policy.encryption_ready" class="account-notice">独立加密密钥尚未配置。请先按部署文档配置，再修改策略或执行批次。</p>
      <form class="account-form" @submit.prevent="save">
        <p>当前策略版本 {{ policy?.version }} · 每人独立随机生成。凭据下载窗口 {{ policy?.credential_export_hours || 24 }} 小时，只有管理员再次验证密码后可下载。</p>
        <label>临时密码有效天数<input v-model.number="days" type="number" min="1" max="30" required /></label>
        <label>管理员当前密码<input v-model="current" type="password" autocomplete="current-password" required /></label>
        <button class="btn-primary" :disabled="busy || !policy?.encryption_ready">保存策略</button><p v-if="message" role="status">{{ message }}</p>
      </form>
    </section>
    <section class="account-panel"><h2>管理操作记录</h2><form class="account-actions" @submit.prevent="page = 1; loadAudit()"><label>目标 ID <input v-model="target" placeholder="留空查看全部" /></label><button class="btn-ghost">查询</button></form>
      <p v-if="!events.length" class="account-muted">暂无符合条件的操作记录。</p>
      <table v-else class="account-data"><thead><tr><th>时间</th><th>操作人</th><th>动作 / 目标</th><th>原因</th></tr></thead><tbody><tr v-for="event in events" :key="event.id"><td>{{ new Date(event.created_at).toLocaleString() }}</td><td>{{ event.actor_id }}</td><td>{{ event.action }}<br />{{ event.target_type }} #{{ event.target_id }}</td><td>{{ event.reason || '—' }} <RouterLink v-if="event.batch_id" :to="`/admin/batches/${event.batch_id}`">查看批次</RouterLink></td></tr></tbody></table>
      <TeacherPagination :current-page="page" :page-count="Math.max(1, Math.ceil(total / 20))" :total="total" :page-size="20" @change="page = $event; loadAudit()" />
    </section>
  </div></AppLayout>
</template>
