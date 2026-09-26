<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { notificationsAPI } from '../../api/notifications.js'
import { useAppStore } from '../../stores/app.js'

const router = useRouter()
const app = useAppStore()
const items = ref([])
const unreadCount = ref(0)
const loading = ref(true)

const toneMap = {
  urgent: 'danger',
  important: 'warning',
  normal: 'info',
}

function tone(item) {
  if (item.type === 'work') return item.priority === 'urgent' ? 'danger' : item.priority === 'important' ? 'warning' : 'info'
  return toneMap[item.priority] || 'info'
}

async function load() {
  loading.value = true
  try {
    const { data } = await notificationsAPI.list()
    items.value = data.items || []
    unreadCount.value = data.unread_count || 0
  } catch {
    app.showToast('加载通知失败', 'error')
  } finally {
    loading.value = false
  }
}

function open(item) {
  if (item.route) router.push(item.route)
}

async function markRead(item) {
  if (item.is_read) return
  try {
    await notificationsAPI.markRead(item.id)
    item.is_read = true
    unreadCount.value = Math.max(0, unreadCount.value - 1)
  } catch {
    app.showToast('标记已读失败', 'error')
  }
}

async function markAll() {
  try {
    await notificationsAPI.markAllRead()
    items.value = items.value.map((item) => ({ ...item, is_read: true }))
    unreadCount.value = 0
  } catch {
    app.showToast('操作失败', 'error')
  }
}

const hasUnread = computed(() => unreadCount.value > 0)
onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="notifications-page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">系统 / 通知</p>
          <h1>通知中心</h1>
          <p class="lead">待办任务与课程公告会持久化在这里，已读状态跨设备同步。</p>
        </div>
        <div class="ph-actions">
          <button type="button" class="btn btn-secondary" :disabled="!hasUnread" @click="markAll">全部已读</button>
        </div>
      </section>
      <section class="panel">
        <div class="panel-body">
          <div v-if="loading" class="note">加载中…</div>
          <div v-else-if="items.length === 0" class="empty">
            <div class="empty-mark"><AppIcon name="notification" :size="20" /></div>
            <h3>暂无通知</h3>
          </div>
          <div v-else class="note-list">
            <button v-for="item in items" :key="item.id" type="button" class="note-row" @click="open(item)">
              <span class="note-dot" :class="`dot-${tone(item)}`"></span>
              <span class="note-main">
                <strong>{{ item.title }}</strong>
                <small>{{ item.content }}</small>
              </span>
              <span v-if="item.is_read" class="badge badge-neutral">已读</span>
              <button v-else type="button" class="btn btn-ghost btn-sm" @click.stop="markRead(item)">标记已读</button>
            </button>
          </div>
        </div>
      </section>
    </section>
  </AppLayout>
</template>

<style scoped>
.notifications-page { display: flex; flex-direction: column; gap: var(--space-5); }
.note { color: var(--muted); padding: 12px 0; }
.note-list { display: flex; flex-direction: column; }
.note-row { display: flex; align-items: center; gap: 10px; padding: 12px 10px; border: 0; border-bottom: 1px solid var(--border); background: transparent; text-align: left; }
.note-row:last-child { border-bottom: 0; }
.note-row:hover { background: var(--surface-sunken); }
.note-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--info); flex: none; }
.dot-danger { background: var(--danger); }
.dot-warning { background: var(--warning); }
.note-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.note-main strong { color: var(--fg); }
.note-main small { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
