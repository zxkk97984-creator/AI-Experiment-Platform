<script setup>
// EnvironmentProfilePicker：教师环境选择共享组件（Phase 4 作业/Studio 使用）
// 只显示 active 档位下的 available 版本——draft/building/failed/inactive 一律不可见
import { ref, onMounted, computed, watch } from 'vue'
import { environmentsAPI } from '../../api/environments.js'

const props = defineProps({
  modelValue: { type: Number, default: null },
  label: { type: String, default: '运行环境' },
  placeholder: { type: String, default: '请选择环境档位' },
  // 是否在选项文案中展示最低内存（教师端内存门禁提示）
  showMemory: { type: Boolean, default: false },
  // 外部禁用（如已发布作业的环境不可修改）
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'loaded'])

const options = ref([])
const loading = ref(true)
const loadError = ref(false)

const isEmpty = computed(() => !loading.value && !loadError.value && options.value.length === 0)

async function fetchOptions() {
  loading.value = true
  loadError.value = false
  try {
    const res = await environmentsAPI.listAvailable()
    const available = res.data || []
    let nextOptions = available
    const selectedId = props.modelValue == null ? null : Number(props.modelValue)
    if (
      selectedId != null
      && !Number.isNaN(selectedId)
      && !available.some((opt) => Number(opt.environment_version_id) === selectedId)
      && typeof environmentsAPI.getVersionSummary === 'function'
    ) {
      try {
        const historical = await environmentsAPI.getVersionSummary(selectedId)
        if (historical?.data?.environment_version_id) {
          nextOptions = [...available, { ...historical.data, historical: true }]
        }
      } catch {
        // A deleted/unavailable historical image should not prevent the
        // ordinary current-version picker from loading.
      }
    }
    options.value = nextOptions
    emit('loaded', options.value)
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

function optionLabel(opt) {
  const pkgSummary = (opt.packages || []).map((p) => p.pip_name).join(' · ')
  const memory = props.showMemory && opt.minimum_memory_mb ? `（最低 ${opt.minimum_memory_mb} MB）` : ''
  const historical = opt.historical ? '（历史版本，仅供现有绑定）' : ''
  return `${opt.display_name} v${opt.version_number}${historical}${pkgSummary ? ` · ${pkgSummary}` : ''}${memory}`
}

function onSelect(e) {
  const value = Number(e.target.value)
  emit('update:modelValue', Number.isNaN(value) ? null : value)
}

onMounted(fetchOptions)

// Assignment/template data often arrives after the picker itself mounts.  A
// later historical binding must still be appended without making the current
// picker list stale.
watch(
  () => props.modelValue,
  (value, previous) => {
    if (value != null && value !== previous && !loading.value) fetchOptions()
  },
)

defineExpose({ fetchOptions })
</script>

<template>
  <div class="env-picker">
    <label v-if="label" class="env-picker-label">{{ label }}</label>
    <DsSelect
      class="env-picker-select"
      :aria-label="label || placeholder"
      :value="modelValue ?? ''"
      :disabled="loading || isEmpty || disabled"
      @change="onSelect"
    >
      <option value="" disabled>{{ loading ? '加载中…' : placeholder }}</option>
      <option v-for="opt in options" :key="opt.environment_version_id" :value="opt.environment_version_id" :disabled="opt.historical">
        {{ optionLabel(opt) }}
      </option>
    </DsSelect>
    <p v-if="loadError" class="env-picker-hint env-picker-error">加载失败，请刷新重试</p>
    <p v-else-if="isEmpty" class="env-picker-hint">暂无可用环境，请联系管理员</p>
  </div>
</template>

<style scoped>
.env-picker {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.env-picker-label {
  /* 全局 .form-group label 有 margin-bottom: 6px，与 flex gap 叠加会让
     select 比相邻字段低 6px（对齐 bug，2026-08-08 实测 delta=6px） */
  margin-bottom: 0;
  font-size: var(--text-sm, 13px);
  font-weight: 600;
  color: var(--muted);
}
.env-picker-select {
  width: 100%;
  height: auto;
  min-height: 38px;
  padding: 8px 12px;
  border: 1px solid var(--border, var(--border));
  border-radius: var(--radius-control, 7px);
  background: var(--surface, var(--surface));
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-sm, 13px);
  line-height: 1.4;
}
.env-picker-select.is-disabled {
  background: var(--surface-raised, var(--surface-subtle));
  color: var(--faint);
}
.env-picker-hint {
  margin: 0;
  font-size: var(--text-xs, 12px);
  color: var(--faint);
}
.env-picker-error {
  color: var(--danger, var(--danger));
}
</style>
