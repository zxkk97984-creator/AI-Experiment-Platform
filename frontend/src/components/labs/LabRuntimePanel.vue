<script setup>
import { computed, ref } from 'vue'
import { labsAPI } from '../../api/labs.js'
import { errorMessage } from './labHelpers.js'
import './labs.css'
const runtime = ref(null)
const gpuDevices = computed(() => Array.isArray(runtime.value?.gpu_devices) ? runtime.value.gpu_devices : String(runtime.value?.gpu_devices || '').split(',').map(v => v.trim()).filter(Boolean))
const loading = ref(false)
const error = ref('')
async function load(event) {
  if (event && !event.target.open) return
  loading.value = true
  error.value = ''
  try { runtime.value = (await labsAPI.runtime()).data }
  catch (e) { error.value = errorMessage(e, '无法读取实验资源状态') }
  finally { loading.value = false }
}
</script>
<template>
  <section class="lab-panel">
    <details @toggle="load"><summary>实验训练资源与队列</summary>
      <p class="lab-muted">额度来自部署配置。调整后需由运维重启相关服务；此处显示当前生效值。</p>
      <p v-if="loading" role="status">正在读取资源状态…</p>
      <p v-if="error" class="lab-error" role="alert">{{ error }}</p>
      <template v-if="runtime">
        <p class="lab-notice">后台实验：{{ runtime.enabled ? '已启用' : '未启用' }} · GPU：{{ runtime.gpu_enabled ? '已启用' : '未启用' }}</p>
        <dl class="lab-metrics"><div><dt>训练并发</dt><dd>{{ runtime.training_concurrency }}</dd></div><div><dt>CPU 额度</dt><dd>{{ runtime.total_cpus }} 核</dd></div><div><dt>总内存</dt><dd>{{ runtime.total_memory_mb }} MiB</dd></div><div><dt>交互预留内存</dt><dd>{{ runtime.interactive_reserved_memory_mb }} MiB</dd></div><div><dt>交互内核上限</dt><dd>{{ runtime.max_kernels }}</dd></div><div><dt>排队 / 运行</dt><dd>{{ runtime.queued }} / {{ runtime.running }}</dd></div></dl>
        <p v-if="runtime.gpu_enabled" class="lab-muted">GPU 设备：{{ gpuDevices.join('、') || '未检测到可用设备' }}</p>
        <ul v-if="runtime.leases?.length"><li v-for="(lease, i) in runtime.leases" :key="i">{{ lease.kind === 'interactive' ? '交互内核' : '后台任务' }}：{{ lease.cpus }} 核 · {{ lease.memory_mb }} MiB<span v-if="lease.gpu != null"> · GPU {{ lease.gpu }}</span></li></ul>
      </template>
      <button class="btn btn-secondary btn-sm" :disabled="loading" @click="load()">刷新资源状态</button>
    </details>
  </section>
</template>
