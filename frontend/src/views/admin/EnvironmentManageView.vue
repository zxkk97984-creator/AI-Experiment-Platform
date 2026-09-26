<script setup>
// 环境档位管理（管理员）——一个页面三个 tab：
// 环境档位（档位 + 不可变版本）/ 构建任务（轮询 + 脱敏日志 + 重试）/ 库清单（受控包目录）
import { computed, ref } from 'vue'
import AppLayout from '../../components/layout/AppLayout.vue'
import ProfilePanel from '../../components/admin/environment/ProfilePanel.vue'
import BuildTaskPanel from '../../components/admin/environment/BuildTaskPanel.vue'
import PackageCatalogPanel from '../../components/admin/environment/PackageCatalogPanel.vue'

const allTabs = [
  { key: 'profiles', label: '环境档位' },
  { key: 'builds', label: '构建任务' },
  { key: 'packages', label: '库清单' },
]
const showLegacyTabs = ref(true)
const tabs = computed(() => allTabs.map((tab) => (
  tab.key === 'packages' && !showLegacyTabs.value
    ? { ...tab, label: '库清单（审计）' }
    : tab
)))
const activeTab = ref('profiles')

function handleV2Detected() {
  showLegacyTabs.value = false
  activeTab.value = 'profiles'
}
</script>

<template>
  <AppLayout content-mode="list">
    <div class="page">
      <!-- ── Page Head ─────────────────────────────────────────────────── -->
      <header class="page-head">
        <div>
          <h1 class="page-title">环境档位管理</h1>
          <p class="page-sub">管理员维护不可变判题/实验环境：受控包目录 → 档位版本 → 构建与镜像审计</p>
        </div>
      </header>

      <!-- ── Tabs ─────────────────────────────────────────────────────── -->
      <div class="tab-bar" role="tablist">
        <button
          v-for="t in tabs" :key="t.key"
          class="tab-btn"
          :class="{ active: activeTab === t.key }"
          role="tab"
          :aria-selected="activeTab === t.key ? 'true' : 'false'"
          @click="activeTab = t.key"
        >{{ t.label }}</button>
      </div>

      <!-- ── Tab panels（v-if：切换即卸载，构建轮询随组件销毁停止） ──── -->
      <ProfilePanel v-if="activeTab === 'profiles'" @v2-detected="handleV2Detected" />
      <BuildTaskPanel v-else-if="activeTab === 'builds'" />
      <PackageCatalogPanel v-else-if="activeTab === 'packages'" :read-only="!showLegacyTabs" />
    </div>
  </AppLayout>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 24px; }

/* ── Page Head ─────────────────────────────────────────────────────── */
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title {
  font-size: 28px; font-weight: 700;
  color: var(--fg); letter-spacing: -0.02em; line-height: 1.15;
  margin: 0 0 6px;
}
.page-sub { font-size: var(--text-sm); color: var(--muted); margin: 0; }

/* ── Tab bar ───────────────────────────────────────────────────────── */
.tab-bar {
  display: inline-flex; gap: 4px; padding: 4px;
  background: var(--surface-raised, var(--surface-subtle));
  border-radius: var(--radius-card, 12px);
  width: fit-content;
  border: 1px solid var(--border, var(--border));
}
.tab-btn {
  border: none; background: transparent;
  padding: 8px 18px;
  font-family: inherit; font-size: var(--text-sm, 13px); font-weight: 500;
  color: var(--muted);
  border-radius: var(--radius-control, 7px);
  cursor: pointer;
  transition: background var(--duration-fast, 0.15s) var(--ease-out, ease),
              color var(--duration-fast, 0.15s) var(--ease-out, ease);
}
.tab-btn:hover { color: var(--fg); }
.tab-btn.active {
  background: var(--surface, var(--surface));
  color: var(--accent);
  font-weight: 600;
  box-shadow: 0 1px 3px oklch(0.2 0.01 150 / 0.1);
}

@media (max-width: 768px) {
  .page-head { flex-direction: column; }
  .page-title { font-size: 24px; }
  .tab-bar { width: 100%; }
  .tab-btn { flex: 1; }
}
</style>
