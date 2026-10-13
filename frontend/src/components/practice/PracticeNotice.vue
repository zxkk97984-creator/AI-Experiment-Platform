<script setup>
// 练习域统一提示条：排队 / 额度耗尽 / 权限不足 / AI 未完成 / 评分标准更新 / 失败。
// 文案由调用方传入，色调收敛到 V2 语义色，避免各页自造配色。
import AppIcon from '../ui/AppIcon.vue'

defineProps({
  /** info | warning | danger | success */
  tone: { type: String, default: 'info' },
  title: { type: String, required: true },
  icon: { type: String, default: 'info' },
  /** 断言性提示（额度耗尽、权限不足、判题失败）用 alert，普通状态用 status */
  assertive: { type: Boolean, default: false },
})

const ICONS = { info: 'info', warning: 'warning', danger: 'warning', success: 'check' }
</script>

<template>
  <div
    class="practice-notice"
    :class="`is-${tone}`"
    :role="assertive ? 'alert' : 'status'"
  >
    <span class="notice-icon" aria-hidden="true">
      <AppIcon :name="ICONS[tone] || icon" :size="15" />
    </span>
    <div class="notice-body">
      <p class="notice-title">{{ title }}</p>
      <p v-if="$slots.default" class="notice-text"><slot /></p>
    </div>
    <div v-if="$slots.actions" class="notice-actions"><slot name="actions" /></div>
  </div>
</template>

<style scoped>
.practice-notice {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface-subtle);
  font-size: var(--text-md);
  line-height: var(--lh-body);
}
.notice-icon { display: inline-flex; flex: none; margin-top: 1px; color: var(--muted); }
.notice-body { min-width: 0; }
.notice-title { margin: 0; font-size: var(--text-md); font-weight: 600; }
.notice-text { margin: var(--space-1) 0 0; color: var(--muted); font-size: var(--text-md); }
.notice-actions { margin-left: auto; flex: none; display: flex; gap: var(--space-2); }
.is-info { border-color: var(--info); background: var(--info-bg); }
.is-info .notice-icon { color: var(--info); }
.is-warning { border-color: var(--warning); background: var(--warning-bg); }
.is-warning .notice-icon { color: var(--warning); }
.is-danger { border-color: var(--danger); background: var(--danger-bg); }
.is-danger .notice-icon { color: var(--danger); }
.is-success { border-color: var(--success); background: var(--success-bg); }
.is-success .notice-icon { color: var(--success); }
</style>
