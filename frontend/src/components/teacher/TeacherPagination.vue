<script setup>
import { computed, ref, watch } from 'vue'
import AppIcon from '../ui/AppIcon.vue'

const props = defineProps({
  currentPage: { type: Number, required: true },
  pageCount: { type: Number, required: true },
  total: { type: Number, required: true },
  pageSize: { type: Number, required: true },
  ariaLabel: { type: String, default: '列表分页' },
  totalSuffix: { type: String, default: '条' },
})

const emit = defineEmits(['change'])
const jumpPage = ref(String(props.currentPage))

const pageItems = computed(() => {
  const count = Math.max(1, props.pageCount)
  if (count <= 3) {
    return Array.from({ length: count }, (_, index) => ({
      key: index + 1,
      value: index + 1,
      label: index + 1,
    }))
  }

  const start = Math.min(Math.max(props.currentPage - 1, 1), count - 2)
  return [start, start + 1, start + 2].map((value) => ({
    key: value,
    value,
    label: value,
  }))
})

watch(() => [props.currentPage, props.pageCount], () => {
  jumpPage.value = String(props.currentPage)
})

function change(nextPage) {
  if (!nextPage || nextPage < 1 || nextPage > props.pageCount || nextPage === props.currentPage) return
  emit('change', nextPage)
}

function jumpToPage() {
  const value = jumpPage.value.trim()
  if (!/^\d+$/.test(value)) {
    jumpPage.value = String(props.currentPage)
    return
  }

  const nextPage = Number(value)
  if (nextPage < 1 || nextPage > props.pageCount) {
    jumpPage.value = String(props.currentPage)
    return
  }

  change(nextPage)
}
</script>

<template>
  <footer v-if="total > 0" class="teacher-pagination pagination-bar">
    <span>共 {{ total }} {{ totalSuffix }}</span>
    <nav class="teacher-pagination-controls" :aria-label="ariaLabel">
      <button type="button" :disabled="currentPage === 1" aria-label="上一页" @click="change(currentPage - 1)">
        <AppIcon name="back" :size="16" />
      </button>
      <button
        v-for="item in pageItems"
        :key="item.key"
        type="button"
        :class="{ active: item.value === currentPage }"
        :aria-label="`第 ${item.value} 页`"
        :aria-current="item.value === currentPage ? 'page' : undefined"
        @click="change(item.value)"
      >
        {{ item.label }}
      </button>
      <button type="button" :disabled="currentPage === pageCount" aria-label="下一页" @click="change(currentPage + 1)">
        <AppIcon name="chevron-right" :size="16" />
      </button>
    </nav>
    <div class="teacher-pagination-jump">
      <label class="teacher-pagination-jump-field">
        <span>跳转至</span>
        <input
          v-model="jumpPage"
          type="text"
          inputmode="numeric"
          aria-label="跳转页码"
          @keydown.enter.prevent="jumpToPage"
        />
        <span>页</span>
      </label>
      <span>{{ pageSize }} 条/页</span>
    </div>
  </footer>
</template>

<style scoped>
/* 分页视觉来自全局 .pagination / .pg-btn；仅保留组件扩展（跳页输入）。 */
.teacher-pagination.pagination-bar {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  min-height: 58px;
  padding: 12px 16px;
  color: var(--muted);
  font-size: var(--text-sm);
}

.teacher-pagination-controls { display: flex; align-items: center; gap: 2px; }
.teacher-pagination-controls button {
  min-width: 30px;
  height: 30px;
  padding: 0 6px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  color: var(--muted);
  background: transparent;
  font-size: var(--text-base);
}
.teacher-pagination-controls button:hover:not(:disabled) { background: var(--surface-sunken); color: var(--fg); }
.teacher-pagination-controls button.active { background: var(--accent); color: var(--surface); font-weight: 600; }
.teacher-pagination-controls button:disabled { opacity: .35; }
.teacher-pagination-jump { display: flex; flex-wrap: wrap; align-items: center; justify-self: end; gap: 14px; white-space: nowrap; }
.teacher-pagination-jump-field { display: inline-flex; align-items: center; gap: 5px; }
.teacher-pagination-jump input {
  width: 52px;
  height: 30px;
  padding: 0 7px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  color: var(--fg);
  background: var(--surface);
  text-align: center;
  font-size: var(--text-base);
}
.teacher-pagination-jump input:focus { border-color: var(--accent); outline: 0; box-shadow: 0 0 0 3px var(--accent-soft); }

@media (max-width: 720px) {
  .teacher-pagination { grid-template-columns: 1fr auto; gap: 10px; }
  .teacher-pagination-controls { grid-column: 1 / -1; grid-row: 1; justify-content: center; }
  .teacher-pagination-controls button { min-width: 32px; height: 32px; }
  .teacher-pagination-jump { justify-self: end; gap: 8px; }
}
</style>
