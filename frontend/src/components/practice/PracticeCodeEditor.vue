<script setup>
// 练习作答编辑器：等宽 textarea + 行号栏。
// 不劫持 Tab（保持键盘可逃出），Ctrl/Cmd + Enter 触发提交。
import { computed, ref } from 'vue'
import AppIcon from '../ui/AppIcon.vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  starterCode: { type: String, default: '' },
  readonly: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  label: { type: String, default: '作答代码' },
  placeholder: { type: String, default: '在此编写 Python 代码' },
})

const emit = defineEmits(['update:modelValue', 'submit', 'reset'])

const textarea = ref(null)
const lines = computed(() => Math.max(1, (props.modelValue || '').split('\n').length))
const lineNumbers = computed(() => Array.from({ length: lines.value }, (_, i) => i + 1))

function onInput(event) {
  emit('update:modelValue', event.target.value)
}

/** 行号栏跟随滚动，避免长代码时行号错位 */
function onScroll(event) {
  const gutter = event.currentTarget.previousElementSibling
  if (gutter) gutter.scrollTop = event.currentTarget.scrollTop
}

function onKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    emit('submit')
  }
}

function focusEditor() {
  textarea.value?.focus()
}

defineExpose({ focusEditor })
</script>

<template>
  <div class="practice-editor">
    <div class="editor-toolbar">
      <label class="editor-label" :for="`practice-code-${label}`">{{ label }}</label>
      <div class="editor-actions">
        <span class="editor-hint">Ctrl/⌘ + Enter 提交</span>
        <button
          v-if="!readonly"
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="disabled"
          @click="emit('reset')"
        >
          <AppIcon name="refresh" :size="14" />重置为初始代码
        </button>
        <button v-if="readonly" type="button" class="btn btn-ghost btn-sm" @click="focusEditor">
          <AppIcon name="eye" :size="14" />查看代码
        </button>
      </div>
    </div>
    <div class="editor-body" :class="{ 'is-readonly': readonly }">
      <div class="editor-gutter" aria-hidden="true">
        <span v-for="n in lineNumbers" :key="n">{{ n }}</span>
      </div>
      <textarea
        :id="`practice-code-${label}`"
        ref="textarea"
        class="editor-textarea"
        :value="modelValue"
        :readonly="readonly"
        :disabled="disabled"
        :placeholder="placeholder"
        spellcheck="false"
        autocapitalize="off"
        autocomplete="off"
        :aria-label="label"
        @input="onInput"
        @scroll="onScroll"
        @keydown="onKeydown"
      ></textarea>
    </div>
  </div>
</template>

<style scoped>
.practice-editor { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; background: var(--surface); }
.practice-editor:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.editor-toolbar { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--border); background: var(--surface-subtle); flex-wrap: wrap; }
.editor-label { font-size: var(--text-md); font-weight: 600; color: var(--fg); }
.editor-actions { display: flex; align-items: center; gap: var(--space-2); }
.editor-hint { font-family: var(--font-mono); font-size: var(--text-sm); color: var(--faint); }
.editor-body { display: flex; height: 320px; background: var(--surface-sunken); }
.editor-body.is-readonly { height: 240px; }
.editor-gutter { flex: none; width: 44px; overflow: hidden; padding: var(--space-2) 0; border-right: 1px solid var(--border); background: var(--surface-subtle); text-align: right; }
.editor-gutter span { display: block; padding-right: var(--space-2); font-family: var(--font-mono); font-size: var(--text-sm); line-height: 20px; color: var(--faint); }
.editor-textarea { flex: 1; min-width: 0; padding: var(--space-2) var(--space-3); border: 0; outline: none; resize: none; background: transparent; color: var(--fg); font-family: var(--font-mono); font-size: var(--text-md); line-height: 20px; tab-size: 4; }
.editor-textarea::placeholder { color: var(--faint); }
</style>
