<script setup>
import { onMounted, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { environmentsAPI } from '../../api/environments.js'
import { useAppStore } from '../../stores/app.js'

const app = useAppStore()
const options = ref([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    const res = await environmentsAPI.listAvailable()
    options.value = res.data || []
  } catch {
    app.showToast('加载运行环境失败', 'error')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="env-page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">系统 / 环境</p>
          <h1>运行环境</h1>
          <p class="lead">查看作业与实验当前可用的运行环境档位和预装包。</p>
        </div>
      </section>

      <section class="metric-strip">
        <div class="metric"><span class="m-value">{{ options.length }}</span><span class="m-label">可用环境</span></div>
        <div class="metric"><span class="m-value">{{ options.reduce((sum, item) => sum + (item.packages?.length || 0), 0) }}</span><span class="m-label">预装包数量</span></div>
      </section>

      <div v-if="loading" class="panel"><div class="panel-body">加载中…</div></div>
      <div v-else-if="options.length === 0" class="empty">
        <div class="empty-mark"><AppIcon name="cube" :size="20" /></div>
        <h3>暂无可用的运行环境</h3>
        <p>请联系管理员构建并发布环境版本。</p>
      </div>
      <div v-else class="env-grid">
        <article v-for="option in options" :key="option.environment_version_id" class="panel env-card">
          <div class="panel-head">
            <div class="ph-label"><p class="eyebrow">{{ option.slug }}</p><h3>{{ option.display_name }}</h3></div>
          </div>
          <div class="panel-body">
            <p class="env-desc">{{ option.description || '暂无说明' }}</p>
            <div class="detail-row"><span>版本</span><strong>v{{ option.version_number }}</strong></div>
            <div class="detail-row"><span>最低内存</span><strong>{{ option.minimum_memory_mb }} MB</strong></div>
            <div class="package-list">
              <span v-for="pkg in option.packages || []" :key="pkg.import_name || pkg.pip_name" class="badge badge-neutral">
                {{ pkg.import_name || pkg.pip_name }} {{ pkg.locked_version || '' }}
              </span>
            </div>
          </div>
        </article>
      </div>
    </section>
  </AppLayout>
</template>

<style scoped>
.env-page { display: flex; flex-direction: column; gap: var(--space-5); }
.env-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--space-4); }
.env-desc { margin: 0 0 12px; color: var(--muted); }
.detail-row { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border); color: var(--muted); }
.package-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
</style>
