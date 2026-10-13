<script setup>
// 任务发布时的「计入课程总评 + 分类」控件：默认勾选并预选分类，教师可以取消或改分类。
// 分类列表来自班级成绩册（含课程模板回落）；取不到时退回内置四类，保证发布流程不被网络问题挡住。
import { computed, onMounted, ref, watch } from 'vue'
import { gradebookAPI } from '../../api/gradebook.js'

const props = defineProps({
  modelValue: { type: Object, default: () => ({ enabled: true, category_key: null }) },
  offeringId: { type: [Number, String], default: null },
  defaultKey: { type: String, default: 'regular' },
  // 「其他」类型考试必须由教师显式选择分类，此时不自动预选。
  requireChoice: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const FALLBACK = [
  { key: 'regular', title: '平时成绩' },
  { key: 'labs', title: '实验成绩' },
  { key: 'midterm', title: '期中成绩' },
  { key: 'final', title: '期末成绩' },
]
const categories = ref(FALLBACK)
const enabled = computed(() => props.modelValue?.enabled !== false)
const selected = computed(() => props.modelValue?.category_key || '')

function publishValue(key) {
  emit('update:modelValue', { enabled: true, category_key: key })
}

async function load() {
  if (props.offeringId) {
    try {
      const result = await gradebookAPI.bindingOptions(props.offeringId)
      categories.value = result.data?.categories?.length ? result.data.categories : FALLBACK
    } catch { categories.value = FALLBACK }
  } else {
    categories.value = FALLBACK
  }
  // 预选分类：优先沿用上级已选/任务默认分类，分类被删除时回落到第一个可用分类。
  selectPreferred(props.modelValue?.category_key || props.defaultKey)
}

function selectPreferred(preferred) {
  if (props.requireChoice && !categories.value.some(item => item.key === preferred)) return
  const key = categories.value.some(item => item.key === preferred) ? preferred : categories.value[0]?.key || null
  if (enabled.value && key !== props.modelValue?.category_key) publishValue(key)
}

// 任务默认分类随「考试类型」等条件变化：教师没有手动改过时才跟随更新。
let lastDefaultKey = props.defaultKey
watch(() => props.defaultKey, key => {
  const followsDefault = !props.modelValue?.category_key || props.modelValue.category_key === lastDefaultKey
  lastDefaultKey = key
  if (!followsDefault) return
  if (!key && props.requireChoice) {
    if (props.modelValue?.category_key) emit('update:modelValue', { enabled: true, category_key: null })
    return
  }
  selectPreferred(key)
})
watch(() => props.requireChoice, () => selectPreferred(props.modelValue?.category_key || props.defaultKey))

watch(() => props.offeringId, load)
onMounted(load)
</script>

<template>
  <fieldset class="binding-field" :disabled="disabled">
    <legend>课程总评</legend>
    <label class="binding-toggle">
      <input type="checkbox" :checked="enabled" :disabled="disabled"
             @change="enabled ? emit('update:modelValue', { enabled: false, category_key: null }) : publishValue(selected || categories[0]?.key || null)" />
      计入课程总评
    </label>
    <label v-if="enabled" class="binding-category">计入分类
      <DsSelect :model-value="selected" :disabled="disabled" aria-label="计入课程总评的分类"
                @update:model-value="publishValue($event)">
        <option v-if="requireChoice" value="" disabled>请选择分类</option>
        <option v-for="item in categories" :key="item.key" :value="item.key">{{ item.title }}</option>
      </DsSelect>
    </label>
    <small class="account-muted">不计入的发布不会出现在班级成绩册；计入后仍可在成绩册中调整栏目与比例。</small>
  </fieldset>
</template>

<style scoped>
.binding-field { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.binding-field legend { padding: 0 6px; font-size: 12px; color: var(--muted); }
.binding-toggle { display: flex; align-items: center; gap: 6px; font-size: 14px; }
.binding-category { display: grid; gap: 6px; min-width: 180px; }
.binding-field small { flex-basis: 100%; }
</style>
