<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import TeacherPagination from '../../components/teacher/TeacherPagination.vue'
import { academicsAPI } from '../../api/academics.js'
import { accountAPI, batchKinds, batchStatuses, downloadBlob, errorMessage } from '../../api/accountManagement.js'
const route = useRoute()
const router = useRouter()
const kind = ref(route.query.class_id ? 'class_students' : 'students')
const term = ref('')
const classId = ref(route.query.class_id || '')
const terms = ref([])
const classes = ref([])
const file = ref(null)
const batches = ref([])
const page = ref(1)
const total = ref(0)
const busy = ref(false)
const error = ref('')
const resetScope = ref('class')
const resetQuery = ref('')
async function allPages(fetcher) {
  const rows = []; let current = 1
  while (true) { const { data } = await fetcher({ page: current++, page_size: 100 }); rows.push(...data.items); if (rows.length >= data.total || !data.items.length) return rows }
}
async function loadBatches() {
  try { const { data } = await accountAPI.batches({ page: page.value }); batches.value = data.items; total.value = data.total }
  catch (e) { error.value = errorMessage(e) }
}
onMounted(async () => {
  try { [terms.value, classes.value] = await Promise.all([allPages(academicsAPI.listTerms), allPages(academicsAPI.listClasses)]); await loadBatches() }
  catch (e) { error.value = errorMessage(e) }
})
async function template() {
  try { downloadBlob((await accountAPI.template(kind.value)).data, `${kind.value}.xlsx`) }
  catch (e) { error.value = errorMessage(e) }
}
async function preview() {
  if (!file.value) { error.value = '请选择名单文件'; return }
  busy.value = true; error.value = ''
  try {
    const data = new FormData(); data.append('kind', kind.value); data.append('file', file.value)
    if (term.value) data.append('term_id', term.value)
    if (kind.value === 'class_students' && classId.value) data.append('class_id', classId.value)
    const result = await accountAPI.preview(data)
    await router.push(`/admin/batches/${result.data.id}`)
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
async function previewReset() {
  busy.value = true; error.value = ''
  try {
    const selection = resetScope.value === 'class' ? { class_id: Number(classId.value) } : { all_matching: true, q: resetQuery.value || null }
    const { data } = await accountAPI.previewReset(selection)
    await router.push(`/admin/batches/${data.id}`)
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>
<template>
  <AppLayout content-mode="list"><div class="account-page">
    <header><h1>名单与批次</h1><p class="account-muted">先预检，再确认。导入只追加名单，不移除未出现在文件中的学生。</p></header>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <div class="account-grid">
      <section class="account-panel"><h2>Excel 名单导入</h2>
        <form class="account-form" @submit.prevent="preview">
          <label>导入类型<DsSelect v-model="kind"><option value="students">学生账号及多班名单</option><option value="class_students">某班学生名单</option><option value="classes">教学班名单</option></DsSelect></label>
          <label v-if="kind !== 'class_students'">目标学期<DsSelect v-model="term" :required="kind === 'classes'"><option value="">{{ kind === 'classes' ? '请选择目标学期' : '仅学生账号，不关联班级' }}</option><option v-for="t in terms.filter(t => t.status !== 'closed')" :key="t.id" :value="t.id">{{ t.name }}</option></DsSelect></label>
          <label v-else>目标教学班<DsSelect v-model="classId" required><option value="">请选择</option><option v-for="c in classes.filter(c => c.status === 'active')" :key="c.id" :value="c.id">{{ c.name }} · {{ c.code }}</option></DsSelect></label>
          <button type="button" class="btn-ghost" @click="template">下载 Excel 模板</button>
          <label>名单文件<input type="file" accept=".xlsx" required @change="file = $event.target.files[0]" /></label>
          <small class="account-muted">单表，最多 5000 行 / 5 MiB。学号与班级编码请使用文本格式。</small>
          <button class="btn-primary" :disabled="busy">{{ busy ? '正在预检…' : '上传并预检' }}</button>
        </form>
      </section>
      <section class="account-panel"><h2>批量重置学生密码</h2>
        <form class="account-form" @submit.prevent="previewReset">
          <p class="account-muted">每人生成独立随机临时密码，成功后旧登录失效。禁用或正在考试的学生会跳过。完成后请下载凭据发放。</p>
          <label>选择范围<DsSelect v-model="resetScope"><option value="class">整个教学班</option><option value="filter">全部匹配学生</option></DsSelect></label>
          <label v-if="resetScope === 'class'">教学班<DsSelect v-model="classId" required><option value="">请选择</option><option v-for="c in classes" :key="c.id" :value="c.id">{{ c.name }} · {{ c.code }}</option></DsSelect></label>
          <label v-else>精确匹配姓名、账号或学号<input v-model="resetQuery" placeholder="留空表示所有学生，下一步核对人数" /></label>
          <button class="btn-primary" :disabled="busy">预览重置名单</button>
        </form>
      </section>
    </div>
    <section class="account-panel"><div class="account-actions"><h2>操作批次</h2><button class="btn-ghost" @click="loadBatches">刷新</button></div>
      <p v-if="!batches.length" class="account-muted">还没有批次。上传文件预检后，会在这里留下记录。</p>
      <table v-else class="account-data"><thead><tr><th>操作</th><th>状态</th><th>创建时间</th><th>详情</th></tr></thead><tbody><tr v-for="b in batches" :key="b.id"><td>{{ batchKinds[b.kind] }}</td><td>{{ batchStatuses[b.status] }}</td><td>{{ new Date(b.created_at).toLocaleString() }}</td><td><RouterLink :to="`/admin/batches/${b.id}`">查看批次</RouterLink></td></tr></tbody></table>
      <TeacherPagination :current-page="page" :page-count="Math.max(1, Math.ceil(total / 20))" :total="total" :page-size="20" @change="page = $event; loadBatches()" />
    </section>
  </div></AppLayout>
</template>
