<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { useAppStore } from '../../stores/app.js'
import { errorMessage, isActive, statusLabel } from './labHelpers.js'
const selected = defineModel({ type: Number, default: null })
const props = defineProps({ taskType: { type: String, required: true } })
const app = useAppStore()
const datasets = ref([])
const page = ref(1)
const total = ref(0)
const uploading = ref(false)
const error = ref('')
const upload = ref(null)
const name = ref('')
const target = ref('label')
const idColumn = ref('sample_id')
const split = ref('')
const seed = ref(42)
const options = computed(() => datasets.value.filter(d => d.task_type === props.taskType && d.status === 'ready'))
let timer
let disposed = false
let polls = 0
async function load(more = false) {
  try {
    const nextPage = more ? page.value + 1 : 1
    const { data } = await labsAPI.listDatasets({ page: nextPage, page_size: 100 })
    datasets.value = more ? [...datasets.value, ...data.items] : data.items || []
    page.value = nextPage
    total.value = data.total || 0
    if (selected.value && !datasets.value.some(d => d.id === selected.value)) {
      const selectedDataset = (await labsAPI.getDataset(selected.value)).data
      datasets.value.push(selectedDataset)
    }
  }
  catch (e) { error.value = errorMessage(e, '数据集列表加载失败') }
}
async function poll() {
  if (disposed || !upload.value || polls++ >= 360) return
  try {
    upload.value = (await labsAPI.getDataset(upload.value.id)).data
    if (disposed) return
    if (upload.value.status === 'ready') {
      selected.value = upload.value.id
      await load()
      app.showToast('数据集校验完成，已选择该版本', 'success')
    } else if (isActive(upload.value.status)) timer = setTimeout(poll, 5000)
  } catch (e) { error.value = errorMessage(e, '数据集状态加载失败，可手动刷新'); }
}
async function onUpload(event) {
  const file = event.target.files?.[0]
  if (!file) return
  uploading.value = true
  error.value = ''
  clearTimeout(timer)
  try {
    upload.value = (await labsAPI.uploadDataset(file, name.value.trim() || file.name, props.taskType, {
      target_column: target.value, id_column: idColumn.value, split_column: split.value.trim() || null, seed: seed.value,
    })).data
    polls = 0
    await poll()
  } catch (e) { error.value = errorMessage(e, '数据集上传失败') }
  finally { uploading.value = false; event.target.value = '' }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <div>
    <label class="lab-field">数据集版本<select v-model="selected"><option :value="null">请选择已校验的数据集</option><option v-for="d in options" :key="d.id" :value="d.id">{{ d.name }} · 版本 #{{ d.id }}</option></select></label>
    <button v-if="datasets.length < total" class="btn btn-ghost btn-sm" type="button" @click="load(true)">加载更多数据集</button>
    <details><summary>上传新数据集</summary>
      <p class="lab-muted">CSV 支持自动划分；已划分数据可将 train.csv、validation.csv、test.csv 打包为 ZIP。图像支持按类别分目录，或使用 labels.csv（sample_id、image_path、label、split）。默认最大 500 MiB。</p>
      <div class="lab-grid">
        <label class="lab-field">数据集名称<input v-model="name" maxlength="200" placeholder="例如：花卉分类入门" /></label>
        <label class="lab-field">标签列<input v-model="target" placeholder="label" /></label>
        <label class="lab-field">样本编号列<input v-model="idColumn" placeholder="sample_id" /></label>
        <label class="lab-field">划分列（可选）<input v-model="split" placeholder="留空时固定随机划分" /></label>
        <label class="lab-field">随机种子<input v-model.number="seed" type="number" min="0" /></label>
      </div>
      <label class="lab-field">选择 CSV 或 ZIP<input type="file" accept=".csv,.zip" :disabled="uploading" @change="onUpload" /></label>
      <p v-if="uploading" role="status">上传中，请保持页面打开…</p>
      <div v-if="upload" class="lab-notice" role="status">
        {{ upload.name }}：{{ statusLabel(upload.status) }}
        <p v-if="upload.error">{{ upload.error }}</p>
        <button v-if="isActive(upload.status)" class="btn btn-ghost btn-sm" type="button" @click="polls = 0; poll()">刷新校验状态</button>
      </div>
    </details>
    <p v-if="error" role="alert" class="lab-error">{{ error }} <button class="btn btn-ghost btn-sm" @click="load(false)">重试</button></p>
    <p class="lab-muted">训练集与公开验证集提供给学生；最终测试标签由平台独立保管。</p>
  </div>
</template>
