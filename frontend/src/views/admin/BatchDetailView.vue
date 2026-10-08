<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import CredentialExportPanel from '../../components/admin/CredentialExportPanel.vue'
import { accountAPI, batchKinds, batchStatuses, downloadBlob, errorMessage } from '../../api/accountManagement.js'
import { useAuthStore } from '../../stores/auth.js'
const route = useRoute()
const auth = useAuthStore()
const batch = ref(null)
const page = ref(1)
const error = ref('')
const busy = ref(false)
const acknowledged = ref(false)
const password = ref('')
const reason = ref('')
let timer
let disposed = false
let generation = 0
const batchErrors = { PREVIEW_CHANGED: '名单或班级发生变化，请重新上传预检', ACTOR_UNAVAILABLE: '操作人的管理权限已变化，任务已停止', DATABASE_CONFLICT: '数据发生并发冲突，请核对结果后重新预检', RETRY_EXHAUSTED: '后台任务多次重试未成功，请联系管理员检查服务', ACCOUNT_SECRET_INVALID: '账号加密配置不可用，请联系系统维护人员' }
const names = { new_accounts: '新账号', reused_accounts: '复用账号', memberships: '班级关系', filled_departments: '补全院系', unchanged: '无变化', errors: '错误行', selected: '已选择', eligible: '可重置', skipped: '跳过', succeeded: '成功', create_class: '新教学班' }
const summary = computed(() => Object.entries(batch.value?.summary || {}).filter(([k]) => names[k]))
async function load() {
  clearTimeout(timer)
  const request = ++generation
  try {
    const { data } = await accountAPI.batch(route.params.id, { page: page.value })
    if (request !== generation || disposed) return
    batch.value = data
  }
  catch (e) { if (request === generation && !disposed) error.value = errorMessage(e) }
  if (request === generation && !disposed && ['queued', 'preparing', 'applying'].includes(batch.value?.status)) timer = setTimeout(load, 3000)
}
onMounted(load)
onUnmounted(() => { disposed = true; clearTimeout(timer) })
async function confirm() {
  if (busy.value || !acknowledged.value || batch.value?.summary.errors || batch.value?.status !== 'preview') return
  busy.value = true; error.value = ''
  try {
    await accountAPI.confirm(batch.value.id, { preview_digest: batch.value.preview_digest, current_password: password.value || null, reason: reason.value || null })
    password.value = ''; await load()
  } catch (e) { error.value = errorMessage(e); await load() }
  finally { busy.value = false }
}
async function report() {
  try { downloadBlob((await accountAPI.report(batch.value.id)).data, '名单处理结果.xlsx') }
  catch (e) { error.value = errorMessage(e) }
}
function explanation(row) {
  if (row.result.errors?.length) return row.result.errors.join('；')
  if (row.result.message) return row.result.message
  if (row.result.duplicate) return '重复行，不重复创建'
  return ({ create_class: '创建班级', create_user: '创建学生账号', reuse_user: '复用现有账号', unchanged: '无变化' })[row.result.action] || row.status
}
</script>
<template>
  <AppLayout content-mode="list"><div class="account-page">
    <RouterLink to="/admin/accounts">← 返回名单与批次</RouterLink>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <template v-if="batch">
      <header><h1>{{ batchKinds[batch.kind] }}</h1><p role="status">{{ batchStatuses[batch.status] }} <span v-if="batch.status === 'preparing'"> · 已准备 {{ batch.prepared }} 份凭据，尚未代表账号已创建</span></p></header>
      <div class="account-tags"><span v-for="[key, value] in summary" :key="key">{{ names[key] }}：{{ value }}</span></div>
      <p v-if="batch.error_code" class="account-error">{{ batchErrors[batch.error_code] || '任务未完成，请核对结果并联系系统维护人员' }}。</p>
      <CredentialExportPanel v-if="['succeeded', 'partially_succeeded'].includes(batch.status) && batch.kind !== 'classes'" :batch-id="batch.id" :deliveries="batch.credentials || []" />
      <section v-if="batch.status === 'preview' && batch.actor_id === auth.user?.id" class="account-panel">
        <h2>确认操作范围</h2>
        <p class="account-notice">{{ batch.kind === 'password_reset' ? '执行后会撤销目标学生的旧登录，学生需使用临时密码重新设置个人密码。' : '有错误行时整批不执行。已有学生密码不变，名单只追加。' }}</p>
        <form class="account-form" @submit.prevent="confirm">
          <template v-if="batch.kind === 'password_reset'"><label>重置原因<textarea v-model="reason" maxlength="500" required /></label><label>管理员当前密码<input v-model="password" type="password" autocomplete="current-password" required /></label></template>
          <label><span><input v-model="acknowledged" type="checkbox" required /> 我已核对本批次 {{ batch.total }} 行及上述影响</span></label>
          <button class="btn-primary" :disabled="busy || !acknowledged || !!batch.summary.errors">{{ busy ? '正在确认…' : '确认并后台执行' }}</button>
        </form>
      </section>
      <section class="account-panel"><div class="account-actions"><h2>逐行结果</h2><button class="btn-ghost" @click="report">下载结果</button><button class="btn-ghost" @click="load">刷新状态</button></div>
        <table class="account-data"><thead><tr><th>行号</th><th>学号 / 班级</th><th>姓名 / 名称</th><th>结果</th></tr></thead><tbody><tr v-for="row in batch.items" :key="row.row_number"><td>{{ row.row_number }}</td><td>{{ row.data.student_no || row.data.class_code }}</td><td>{{ row.data.real_name || row.data.class_name }}</td><td>{{ explanation(row) }}</td></tr></tbody></table>
        <TeacherPagination :current-page="page" :page-count="Math.max(1, Math.ceil(batch.total / 20))" :total="batch.total" :page-size="20" @change="page = $event; load()" />
      </section>
    </template><p v-else role="status">正在加载批次…</p>
  </div></AppLayout>
</template>
