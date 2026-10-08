<script setup>
import { computed, ref, watch } from 'vue'
import { vDialogFocus } from '../../directives/dialogFocus.js'

const props = defineProps({
  material: { type: Object, default: null }, chapters: { type: Array, default: () => [] },
  busy: Boolean, progress: { type: Number, default: null }, error: { type: String, default: '' },
  ordinaryMaxBytes: { type: Number, default: null }, largeMaxBytes: { type: Number, default: null },
})
const emit = defineEmits(['save', 'cancel', 'abort'])
const form = ref({})
const file = ref(null)
const validLimit = value => Number.isSafeInteger(value) && value > 0
const limitLabel = value => {
  if (value < 1024) return `${value} 字节`
  const unit = value < 1048576 ? 'KiB' : 'MiB'
  return `${(value / (unit === 'KiB' ? 1024 : 1048576)).toLocaleString('zh-CN', { maximumFractionDigits: 3 })} ${unit}`
}
const limitsAvailable = computed(() => validLimit(props.ordinaryMaxBytes) && validLimit(props.largeMaxBytes))
const fileError = computed(() => {
  if (form.value.source !== 'file' || !file.value) return ''
  const large = /\.(zip|mp4|webm|mov)$/i.test(file.value.name)
  const maximum = large ? props.largeMaxBytes : props.ordinaryMaxBytes
  if (!validLimit(maximum)) return '上传大小限制暂不可用，请关闭窗口并刷新课程资料后重试。'
  return file.value.size > maximum ? `文件大小超过 ${limitLabel(maximum)} 限制，请选择较小文件。` : ''
})
watch(() => props.material, (row) => {
  form.value = { title: row?.title || '', description: row?.description || '', chapter_id: row?.chapter_id || null,
    category: row?.category || 'lecture', source: row?.source || 'file', external_url: row?.external_url || '',
    status: row?.status || 'draft', order_index: row?.order_index || 0 }
  file.value = null
}, { immediate: true })
function save() {
  if (props.busy || fileError.value) return
  emit('save', { ...form.value, file: form.value.source === 'file' ? file.value : null })
}
</script>

<template>
  <div class="modal-backdrop" @click.self="!busy && emit('cancel')" @keydown.esc="!busy && emit('cancel')">
    <section v-dialog-focus class="modal material-editor" role="dialog" aria-modal="true" aria-labelledby="material-editor-title">
      <header class="modal-head"><h2 id="material-editor-title">{{ material ? '编辑资料' : '添加资料' }}</h2></header>
      <form @submit.prevent="save">
        <div class="modal-body fields">
          <label>资料标题<input v-model="form.title" required maxlength="200" :disabled="busy" /></label>
          <label>所属章节<select v-model="form.chapter_id" :disabled="busy"><option :value="null">课程公共资料</option><option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">{{ chapter.title }}</option></select></label>
          <label>资料分类<select v-model="form.category" :disabled="busy"><option value="lecture">课件与讲义</option><option value="notebook">Notebook</option><option value="tool">软件与工具</option><option value="video">视频</option></select></label>
          <label v-if="!material">资料来源<select v-model="form.source" :disabled="busy"><option value="file">上传文件</option><option value="external">官网链接</option></select></label>
          <label v-if="form.source === 'external'">官网地址<input v-model="form.external_url" type="url" required placeholder="https://" :disabled="busy" /></label>
          <label v-else>{{ material?.filename ? '替换文件（可选）' : '资料文件' }}<input type="file" accept=".pdf,.ppt,.pptx,.ipynb,.zip,.mp4,.webm,.mov" :disabled="busy" @change="file = $event.target.files?.[0] || null" /></label>
          <p v-if="form.source === 'file' && limitsAvailable" class="hint">PDF、PPT、PPTX、IPYNB 最大 {{ limitLabel(ordinaryMaxBytes) }}；视频、ZIP 最大 {{ limitLabel(largeMaxBytes) }}。替换后保留版本信息，旧文件不再提供下载。</p>
          <p v-else-if="form.source === 'file'" class="hint">上传大小限制暂不可用，请关闭窗口并刷新课程资料后重试。</p>
          <label>资料说明<textarea v-model="form.description" rows="3" maxlength="5000" :disabled="busy"></textarea></label>
          <label>显示顺序<input v-model.number="form.order_index" type="number" min="0" max="100000" :disabled="busy" /></label>
          <label>发布状态<select v-model="form.status" :disabled="busy"><option value="draft">草稿</option><option value="published">发布给学生</option></select></label>
          <p v-if="busy" role="status">{{ progress === null ? '正在保存…' : `正在上传 ${progress}%` }}<button v-if="progress !== null" type="button" class="btn-ghost" @click="emit('abort')">取消上传</button></p>
          <p v-if="error" role="alert" class="error">{{ error }}</p>
          <p v-if="fileError" role="alert" class="error">{{ fileError }}</p>
        </div>
        <footer class="modal-foot"><button type="button" class="btn-ghost" :disabled="busy" @click="emit('cancel')">取消</button><button type="submit" class="btn-primary" :disabled="busy || !form.title.trim() || !!fileError">{{ busy ? '保存中…' : '保存资料' }}</button></footer>
      </form>
    </section>
  </div>
</template>

<style scoped>
.material-editor { max-width: 600px; max-height: 90vh; overflow-y: auto; }
.fields { display: flex; flex-direction: column; gap: var(--space-3); }
label { display: flex; flex-direction: column; gap: var(--space-1); font-weight: 600; }
input, select, textarea { width: 100%; min-width: 0; }
.hint { color: var(--muted); font-size: var(--text-sm); margin: 0; }
.error { color: var(--danger); }
</style>
