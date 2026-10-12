<script setup>
// 课程级成绩册模板：只编辑分类与规则（占比、等权/自定义），不绑定任何具体任务 ID。
// 模板只影响之后新开的班；已开班成绩册在首次建册时已复制，改动不会回溯。
import { computed, onMounted, ref } from 'vue'
import CourseFormModal from './CourseFormModal.vue'
import { gradebookAPI } from '../../api/gradebook.js'
import { useAppStore } from '../../stores/app.js'

const props = defineProps({
  course: { type: Object, required: true },
})
const emit = defineEmits(['close', 'saved'])

const app = useAppStore()
const categories = ref([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const weightTotal = computed(() => categories.value.reduce((sum, item) => sum + Number(item.weight || 0), 0))
const canSave = computed(() => !loading.value && !saving.value && categories.value.length > 0
  && weightTotal.value === 100 && categories.value.every(item => item.title.trim() && item.key.trim()))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await gradebookAPI.courseTemplate(props.course.id)
    categories.value = (result.data?.categories || []).map(item => ({
      key: item.key, title: item.title, weight: String(item.weight), weight_mode: item.weight_mode || 'equal',
    }))
  } catch (e) {
    error.value = e.response?.data?.detail?.message || '成绩模板加载失败，请稍后重试'
  } finally { loading.value = false }
}
function addCategory() {
  categories.value.push({ key: `category-${categories.value.length + 1}`, title: '新分类', weight: '0', weight_mode: 'equal' })
}
function removeCategory(index) {
  if (categories.value.length === 1) return
  categories.value.splice(index, 1)
}
async function save() {
  if (!canSave.value) return
  saving.value = true
  error.value = ''
  try {
    await gradebookAPI.saveCourseTemplate(props.course.id, { categories: categories.value })
    app.showToast('课程成绩模板已保存，仅对之后新开的班生效', 'success')
    emit('saved')
  } catch (e) {
    error.value = e.response?.data?.detail?.message || '保存失败，请检查分类比例是否合计 100%'
  } finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <CourseFormModal title="课程成绩模板" title-id="course-gradebook-template-title" :busy="saving"
                   description="设置平时 / 实验 / 期中 / 期末四类初始占比与计分方式。模板只复制到之后新开的班，不回溯已开班。"
                   @close="emit('close')" @submit="save">
    <p v-if="loading" role="status">正在加载成绩模板…</p>
    <p v-else-if="error" class="account-error" role="alert">{{ error }}</p>
    <template v-else>
      <div v-for="(category, index) in categories" :key="index" class="template-row">
        <label>分类标识<input v-model="category.key" required maxlength="64" pattern="[A-Za-z0-9_\-]{1,64}" /></label>
        <label>分类名称<input v-model="category.title" required maxlength="120" /></label>
        <label>总成绩占比 %<input v-model="category.weight" type="number" min="0" max="100" step="0.0001" required /></label>
        <label>同类任务计分<DsSelect v-model="category.weight_mode" aria-label="同类任务计分方式"><option value="equal">默认等权（按分项数均分）</option><option value="custom">自定义比例</option></DsSelect></label>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="categories.length === 1" @click="removeCategory(index)">删除分类</button>
      </div>
      <div class="template-actions"><span>分类占比合计 {{ weightTotal }}%（须等于 100%）</span><button type="button" class="btn btn-ghost btn-sm" @click="addCategory">添加分类</button></div>
    </template>
    <template #actions>
      <button type="button" class="btn btn-ghost" :disabled="saving" @click="emit('close')">取消</button>
      <button type="submit" class="btn btn-primary" :disabled="!canSave">保存课程模板</button>
    </template>
  </CourseFormModal>
</template>

<style scoped>
.template-row { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.template-row label { display: grid; gap: 6px; min-width: 150px; flex: 1; }
.template-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding-top: 14px; color: var(--muted); }
</style>
