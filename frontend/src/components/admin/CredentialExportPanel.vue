<script setup>
import { computed, ref, watch } from 'vue'
import { accountAPI, downloadBlob, errorMessage } from '../../api/accountManagement.js'
import { formatDateTime } from '../../utils/format.js'
const props = defineProps({ userId: { type: [Number, String], default: null }, batchId: { type: String, default: null }, deliveries: { type: Array, default: null } })
const entries = ref([])
const password = ref('')
const busy = ref(false)
const error = ref('')
const message = ref('')
let loadVersion = 0
const available = computed(() => (props.deliveries || entries.value).filter(item => item.available))
async function load() {
  if (!props.userId || props.batchId) return
  const current = ++loadVersion
  entries.value = []
  try { const result = await accountAPI.credentialDeliveries(props.userId); if (current === loadVersion) entries.value = result.data.items || [] }
  catch (e) { if (current === loadVersion) error.value = errorMessage(e) }
}
watch(() => props.userId, load, { immediate: true })
async function exportCredentials() {
  if (busy.value || !available.value.length) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const payload = { current_password: password.value }
    const result = props.batchId ? await accountAPI.exportBatchCredentials(props.batchId, payload) : await accountAPI.exportCredential(available.value[0].id, payload)
    downloadBlob(result.data, '学生独立临时凭据.xlsx')
    message.value = '凭据文件已下载，请只向对应学生发放。学生设置个人密码后，原凭据不再可导出。'
  } catch (e) {
    if (e.response?.data instanceof Blob) {
      try { const body = JSON.parse(await e.response.data.text()); error.value = body.detail?.message || '凭据导出失败' }
      catch { error.value = '凭据导出失败，请刷新交付状态后重试' }
    } else error.value = errorMessage(e)
    await load()
  } finally { password.value = ''; busy.value = false }
}
</script>
<template>
  <section class="account-panel" aria-label="临时凭据交付">
    <h2>临时凭据交付</h2>
    <p class="account-muted">每位学生拥有独立随机临时密码。凭据签发后默认 24 小时内可由管理员下载，首次改密期限默认 7 天。</p>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <template v-if="available.length">
      <p>{{ available.length }} 份可交付凭据 · 最早下载截止 {{ formatDateTime(available.map(item => item.export_expires_at).sort()[0]) }}</p>
      <form class="account-form" @submit.prevent="exportCredentials">
        <label>管理员当前密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
        <button class="btn-primary" :disabled="busy">{{ busy ? '正在下载…' : '验证并下载凭据 Excel' }}</button>
      </form>
    </template>
    <p v-else class="account-muted">暂无可下载凭据。已有账号导入不生成新密码；过期、已改密或再次重置的旧凭据不可下载。</p>
    <p v-if="message" role="status">{{ message }}</p>
  </section>
</template>
