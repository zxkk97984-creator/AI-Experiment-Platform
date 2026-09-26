<script setup>
import { vDialogFocus } from '../../directives/dialogFocus.js'
import AppIcon from '../ui/AppIcon.vue'

const props = defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
  titleId: { type: String, required: true },
  busy: { type: Boolean, default: false },
  panelClass: { type: String, default: '' },
  bodyClass: { type: String, default: '' },
  actionsClass: { type: String, default: '' },
})

const emit = defineEmits(['close', 'submit'])

function requestClose() {
  if (!props.busy) emit('close')
}
</script>

<template>
  <div class="modal-backdrop course-form-backdrop" @click.self="requestClose">
    <form
      :class="['modal course-form-panel', panelClass]"
      v-dialog-focus role="dialog"
      aria-modal="true"
      :aria-labelledby="titleId"
      @submit.prevent="$emit('submit')"
    >
      <header class="modal-head course-form-heading">
        <div>
          <p class="eyebrow">课程</p>
          <h2 :id="titleId">{{ title }}</h2>
          <p v-if="description">{{ description }}</p>
        </div>
        <button
          class="btn btn-ghost btn-sm btn-icon course-form-close"
          type="button"
          aria-label="关闭"
          :disabled="busy"
          @click="requestClose"
        >
          <AppIcon name="close" :size="15" />
        </button>
      </header>

      <div :class="['modal-body course-form-body', bodyClass]">
        <slot />
      </div>

      <footer :class="['modal-foot course-form-actions', actionsClass]">
        <slot name="actions" />
      </footer>
    </form>
  </div>
</template>

<style scoped>
/* V2 Modal：视觉全部来自全局 .modal-*；课程表单弹窗宽度更大。 */
.course-form-backdrop { z-index: 1000; }
.course-form-panel { max-width: 760px; }
.course-form-heading h2 { margin-bottom: 4px; }
.course-form-heading p { margin: 0; color: var(--muted); font-size: var(--text-base); }
.course-form-actions :deep(button) { height: 36px; }
</style>
