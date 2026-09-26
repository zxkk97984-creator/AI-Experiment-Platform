<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, onUpdated, ref, useAttrs, useId, watch } from 'vue'
import AppIcon from './AppIcon.vue'

defineOptions({ inheritAttrs: false })

const props = defineProps({
  modelValue: { type: [String, Number, Boolean, Object], default: '' },
  value: { type: [String, Number, Boolean, Object], default: undefined },
  modelModifiers: { type: Object, default: () => ({}) },
  disabled: { type: Boolean, default: false },
  required: { type: Boolean, default: false },
  name: { type: String, default: undefined },
  id: { type: String, default: undefined },
  hostClass: { type: [String, Array, Object], default: undefined },
})
const emit = defineEmits(['update:modelValue', 'change'])
const attrs = useAttrs()
const instanceId = useId()
const nativeSelect = ref(null)
const trigger = ref(null)
const menu = ref(null)
const open = ref(false)
const options = ref([])
const activeIndex = ref(0)
const invalid = ref(false)
const menuStyle = ref({})
let observer
let typeahead = ''
let typeaheadTimer

const menuId = computed(() => `${props.id || `ds-select-${instanceId}`}-options`)
const effectiveValue = computed(() => props.value === undefined ? props.modelValue : props.value)
const nativeAttrs = computed(() => Object.fromEntries(Object.entries(attrs).filter(([key]) => key !== 'style')))
const ariaLabel = computed(() => attrs['aria-label'])
const ariaLabelledby = computed(() => attrs['aria-labelledby'])
const selectedIndex = computed(() => {
  const index = options.value.findIndex(option => Object.is(option.rawValue, effectiveValue.value) || String(option.rawValue) === String(effectiveValue.value ?? ''))
  return index < 0 ? 0 : index
})
const selectedOption = computed(() => options.value[selectedIndex.value])

function syncOptions() {
  if (!nativeSelect.value) return
  const nextOptions = [...nativeSelect.value.options].map((option, index) => ({
    index,
    label: option.textContent.trim(),
    rawValue: Object.prototype.hasOwnProperty.call(option, '_value') ? option._value : option.value,
    disabled: option.disabled,
  }))
  const unchanged = nextOptions.length === options.value.length && nextOptions.every((option, index) => {
    const current = options.value[index]
    return current?.label === option.label && current?.disabled === option.disabled && Object.is(current?.rawValue, option.rawValue)
  })
  if (unchanged) return
  options.value = nextOptions
  activeIndex.value = selectedIndex.value
  nextTick(syncNativeValue)
}

function syncNativeValue() {
  if (!nativeSelect.value) return
  const match = [...nativeSelect.value.options].findIndex(option => {
    const rawValue = Object.prototype.hasOwnProperty.call(option, '_value') ? option._value : option.value
    return Object.is(rawValue, effectiveValue.value) || String(rawValue) === String(effectiveValue.value ?? '')
  })
  nativeSelect.value.selectedIndex = match
}

function onNativeChange(event) {
  const option = options.value[event.target.selectedIndex]
  if (option) selectOption(option)
}

function updatePosition() {
  if (!open.value || !trigger.value) return
  const rect = trigger.value.getBoundingClientRect()
  const gap = 6
  const viewportGap = 12
  const estimated = Math.min(288, options.value.length * 38 + 8)
  const menuWidth = Math.max(rect.width, 160)
  const below = window.innerHeight - rect.bottom - viewportGap
  const above = rect.top - viewportGap
  const placeAbove = below < Math.min(estimated, 180) && above > below
  const maxHeight = Math.max(96, Math.min(288, (placeAbove ? above : below) - gap))
  menuStyle.value = {
    position: 'fixed',
    left: `${Math.max(viewportGap, Math.min(rect.left, window.innerWidth - menuWidth - viewportGap))}px`,
    top: placeAbove ? 'auto' : `${rect.bottom + gap}px`,
    bottom: placeAbove ? `${window.innerHeight - rect.top + gap}px` : 'auto',
    width: `${menuWidth}px`,
    maxHeight: `${maxHeight}px`,
  }
}

async function showMenu() {
  if (props.disabled) return
  syncOptions()
  activeIndex.value = selectedIndex.value
  open.value = true
  await nextTick()
  updatePosition()
  focusActive()
}

function closeMenu({ restoreFocus = false } = {}) {
  open.value = false
  if (restoreFocus) nextTick(() => trigger.value?.focus())
}

function moveActive(delta) {
  if (!options.value.length) return
  let next = activeIndex.value
  do next = (next + delta + options.value.length) % options.value.length
  while (options.value[next]?.disabled && next !== activeIndex.value)
  activeIndex.value = next
}

function focusActive() {
  nextTick(() => menu.value?.querySelector(`[data-index="${activeIndex.value}"]`)?.focus())
}

function selectOption(option) {
  if (!option || option.disabled) return
  const value = props.modelModifiers.number ? Number(option.rawValue) : option.rawValue
  invalid.value = false
  emit('update:modelValue', value)
  emit('change', { target: { value }, currentTarget: nativeSelect.value })
  closeMenu({ restoreFocus: true })
}

function onTriggerKeydown(event) {
  if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
    event.preventDefault()
    showMenu()
  } else if (['Enter', ' '].includes(event.key)) {
    event.preventDefault()
    showMenu()
  }
}

function onMenuKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeMenu({ restoreFocus: true })
  } else if (event.key === 'Tab') {
    closeMenu()
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault()
    if (event.key === 'Home') activeIndex.value = 0
    else if (event.key === 'End') activeIndex.value = options.value.length - 1
    else moveActive(event.key === 'ArrowDown' ? 1 : -1)
    focusActive()
  } else if (['Enter', ' '].includes(event.key)) {
    event.preventDefault()
    selectOption(options.value[activeIndex.value])
  } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
    typeahead += event.key.toLocaleLowerCase()
    clearTimeout(typeaheadTimer)
    typeaheadTimer = setTimeout(() => { typeahead = '' }, 500)
    const match = options.value.findIndex(option => !option.disabled && option.label.toLocaleLowerCase().startsWith(typeahead))
    if (match >= 0) { activeIndex.value = match; focusActive() }
  }
}

function onDocumentPointer(event) {
  if (!trigger.value?.contains(event.target) && !menu.value?.contains(event.target)) closeMenu()
}

function onInvalid(event) {
  event.preventDefault()
  invalid.value = true
  trigger.value?.focus()
}

onMounted(() => {
  syncOptions()
  syncNativeValue()
  observer = new MutationObserver(syncOptions)
  observer.observe(nativeSelect.value, { childList: true, subtree: true, characterData: true, attributes: true })
  document.addEventListener('pointerdown', onDocumentPointer)
  window.addEventListener('resize', updatePosition)
  window.addEventListener('scroll', updatePosition, true)
})
watch(effectiveValue, () => nextTick(syncNativeValue))
onUpdated(syncOptions)
onBeforeUnmount(() => {
  observer?.disconnect()
  clearTimeout(typeaheadTimer)
  document.removeEventListener('pointerdown', onDocumentPointer)
  window.removeEventListener('resize', updatePosition)
  window.removeEventListener('scroll', updatePosition, true)
})
</script>

<template>
  <div class="ds-select" :class="[hostClass, { 'is-open': open, 'is-disabled': disabled, 'is-invalid': invalid }]" :style="attrs.style">
    <select
      ref="nativeSelect" v-bind="nativeAttrs" class="ds-select-native" :id="id" :name="name" :value="effectiveValue ?? ''"
      :required="required" :disabled="disabled" tabindex="-1" aria-hidden="true"
      @focus="trigger?.focus()" @change="onNativeChange" @invalid="onInvalid"
    ><slot /></select>
    <button
      ref="trigger" type="button" class="ds-select-trigger" :disabled="disabled"
      :aria-label="ariaLabel" :aria-labelledby="ariaLabelledby"
      aria-haspopup="listbox" :aria-expanded="open" :aria-controls="menuId"
      @click="open ? closeMenu() : showMenu()" @keydown="onTriggerKeydown"
    >
      <span class="ds-select-value">{{ selectedOption?.label || '请选择' }}</span>
      <AppIcon name="chevron-down" :size="16" class="ds-select-chevron" />
    </button>
    <Teleport to="body">
      <div
        v-if="open" :id="menuId" ref="menu" class="ds-select-menu" role="listbox"
        :aria-label="ariaLabel" :aria-labelledby="ariaLabelledby" :style="menuStyle" @keydown="onMenuKeydown"
      >
        <button
          v-for="option in options" :key="`${option.index}-${String(option.rawValue)}`"
          type="button" role="option" :data-index="option.index" :disabled="option.disabled"
          :tabindex="activeIndex === option.index ? 0 : -1" :aria-selected="selectedIndex === option.index"
          @focus="activeIndex = option.index" @click="selectOption(option)"
        >
          <span>{{ option.label }}</span><AppIcon v-if="selectedIndex === option.index" name="check" :size="16" />
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style>
.ds-select { position: relative; display: inline-block; box-sizing: border-box; width: 100%; min-width: 0; height: var(--h-input); padding: 0 !important; border: 0 !important; border-radius: var(--radius-md); background: transparent !important; color: var(--fg); font: inherit; }
.ds-select-native { position: absolute !important; width: 1px !important; height: 1px !important; margin: 0 !important; padding: 0 !important; opacity: 0; pointer-events: none; }
.ds-select-trigger { display: flex; align-items: center; justify-content: space-between; width: 100%; height: 100%; min-height: inherit; gap: var(--space-2); padding: 0 10px 0 11px; border: 1px solid var(--border-strong); border-radius: inherit; background: var(--surface); color: inherit; font: inherit; text-align: left; }
.ds-select-value { display: block; min-width: 0; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ds-select-chevron { flex: none; color: var(--muted); transition: transform 120ms ease; }
.ds-select.is-open .ds-select-trigger, .ds-select-trigger:focus-visible { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); outline: none; }
.ds-select.is-open .ds-select-chevron { transform: rotate(180deg); }
.ds-select.is-invalid .ds-select-trigger { border-color: var(--danger); }
.ds-select.is-disabled { color: var(--faint); cursor: not-allowed; }
.ds-select.is-disabled .ds-select-trigger { background: var(--surface-subtle); cursor: not-allowed; }
.select:has(> .ds-select) { background-image: none; }
.select > .ds-select { height: 100%; border-radius: inherit; }
.select > .ds-select .ds-select-trigger { border: 0; background: transparent; box-shadow: none; }
.ds-select-menu { z-index: 1000; overflow: auto; overscroll-behavior: contain; padding: 4px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); box-shadow: 0 8px 24px rgb(0 0 0 / 12%); color: var(--fg); }
.ds-select-menu button { display: flex; align-items: center; justify-content: space-between; width: 100%; min-height: 36px; gap: 8px; padding: 7px 10px; border: 0; border-radius: calc(var(--radius-md) - 2px); background: transparent; color: inherit; font: inherit; text-align: left; white-space: normal; }
.ds-select-menu button > span:first-child { min-width: 0; overflow-wrap: anywhere; }
.ds-select-menu button[aria-selected="true"] { color: var(--accent); background: var(--accent-soft); }
.ds-select-menu button:hover:not(:disabled), .ds-select-menu button:focus-visible { border-color: transparent; background: var(--surface-sunken); outline: 2px solid var(--accent); outline-offset: -2px; }
.ds-select-menu button:disabled { color: var(--faint); }
</style>
