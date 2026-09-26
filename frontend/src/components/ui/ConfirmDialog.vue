<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
// ConfirmDialog：V2 共享确认弹窗（映射 dai-ds-v2.css 的 .modal）。
// 交互契约不变：遮罩点击（@click.self）与 Escape 均触发 cancel。

import { onBeforeUnmount, onMounted } from 'vue'

defineProps({
  title: { type: String, required: true },
  message: { type: String, default: '' },
  /** 确认按钮文字，默认"离开" */
  confirmText: { type: String, default: '离开' },
  /** 取消按钮文字，默认"取消" */
  cancelText: { type: String, default: '取消' },
  /** 确认按钮是否使用红色危险样式 */
  danger: { type: Boolean, default: false },
  /** 确认操作进行中：禁用确认按钮（发布确认场景） */
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['confirm', 'cancel'])

function onKeydown(e) {
  if (e.key === 'Escape') emit('cancel')
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="modal-backdrop confirm-backdrop" @click.self="emit('cancel')">
    <section class="modal confirm-modal confirm-panel" v-dialog-focus role="dialog" aria-modal="true" :aria-label="title">
      <header class="modal-head">
        <h2>{{ title }}</h2>
      </header>
      <div class="modal-body">
        <p v-if="message">{{ message }}</p>
      </div>
      <footer class="modal-foot confirm-actions">
        <button type="button" class="btn btn-ghost" :disabled="busy" @click="emit('cancel')">
          {{ cancelText }}
        </button>
        <button
          type="button"
          class="btn"
          :class="danger ? 'btn-danger-solid' : 'btn-primary'"
          :disabled="busy"
          @click="emit('confirm')"
        >
          {{ busy ? '处理中…' : confirmText }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
/* 遮罩与面板视觉完全来自全局 .modal-* / .btn-*。
   保留 confirm-backdrop 类以维持跨视图的 z-index 与居中契约。 */
.confirm-backdrop {
  z-index: 60; /* 高于页面级弹窗，确保编辑页守卫弹窗在最上层 */
}
.confirm-modal { max-width: 440px; }
.modal-body p { margin: 0; color: var(--muted); font-size: var(--text-base); line-height: var(--lh-body); }
</style>
