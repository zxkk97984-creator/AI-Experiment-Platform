<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import TeachingClassMultiSelect from './TeachingClassMultiSelect.vue'
import { coursesAPI } from '../../api/courses.js'
import { usersAPI } from '../../api/users.js'
import { assignmentsAPI } from '../../api/assignments.js'
import { examsAPI } from '../../api/exams.js'
import { useAppStore } from '../../stores/app.js'

const props = defineProps({
  taskKind: { type: String, required: true }, // assignment | exam
  taskId: { type: [Number, String], required: true },
  courseId: { type: [Number, String], default: null },
  audienceMode: { type: String, default: 'all_enrolled' },
  classIds: { type: Array, default: () => [] },
  whitelistIds: { type: Array, default: () => [] },
  excludedIds: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits([
  'update:audienceMode', 'update:classIds', 'update:whitelistIds', 'update:excludedIds',
  'imported',
])
const app = useAppStore()
const courseClasses = ref([])
const classesLoading = ref(false)
const search = ref('')
const candidates = ref([])
const studentMap = ref(new Map())
const importing = ref('')

const mode = computed({
  get: () => props.audienceMode,
  set: (value) => emit('update:audienceMode', value),
})
const classIds = computed({
  get: () => props.classIds,
  set: (value) => emit('update:classIds', value),
})
const whitelistIds = computed({
  get: () => props.whitelistIds,
  set: (value) => emit('update:whitelistIds', value),
})
const excludedIds = computed({
  get: () => props.excludedIds,
  set: (value) => emit('update:excludedIds', value),
})

function studentName(id) {
  return studentMap.value.get(Number(id))?.real_name || `学生 #${id}`
}
function studentNo(id) {
  return studentMap.value.get(Number(id))?.student_no || studentMap.value.get(Number(id))?.username || '—'
}

function addStudent(kind, student) {
  if (kind === 'include') {
    if (whitelistIds.value.some((id) => Number(id) === Number(student.id))) return
    whitelistIds.value = [...whitelistIds.value, Number(student.id)]
  } else {
    if (excludedIds.value.some((id) => Number(id) === Number(student.id))) return
    excludedIds.value = [...excludedIds.value, Number(student.id)]
  }
  studentMap.value.set(Number(student.id), student)
}

function removeStudent(kind, id) {
  if (kind === 'include') whitelistIds.value = whitelistIds.value.filter((x) => Number(x) !== Number(id))
  else excludedIds.value = excludedIds.value.filter((x) => Number(x) !== Number(id))
}

async function searchStudents() {
  try {
    const res = await usersAPI.listStudents({ q: search.value.trim() || undefined, page_size: 30 })
    const rows = res.data.items || []
    for (const student of rows) studentMap.value.set(Number(student.id), student)
    candidates.value = rows
  } catch {
    app.showToast('搜索学生失败', 'error')
  }
}

async function loadCourseClasses() {
  if (!props.courseId) { courseClasses.value = []; return }
  classesLoading.value = true
  try {
    const res = await coursesAPI.get(props.courseId)
    courseClasses.value = res.data.teaching_classes || []
  } catch {
    courseClasses.value = []
  } finally {
    classesLoading.value = false
  }
}

async function loadKnownStudents() {
  try {
    const res = await usersAPI.listStudents({ page_size: 100 })
    for (const student of res.data.items || []) studentMap.value.set(Number(student.id), student)
  } catch { /* 忽略：选中项回退显示 ID */ }
}

async function importCsv(kind) {
  const input = document.getElementById(`audience-csv-${props.taskKind}-${props.taskId}-${kind}`)
  const file = input?.files?.[0]
  if (!file) return
  importing.value = kind
  try {
    const api = props.taskKind === 'assignment' ? assignmentsAPI : examsAPI
    const res = await api.importAudienceStudents(props.taskId, kind, file)
    const data = res.data || {}
    app.showToast(`导入完成：新增 ${data.created || 0}，更新 ${data.updated || 0}，跳过 ${data.skipped || 0}`, 'success')
    emit('imported')
    await loadKnownStudents()
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '导入失败', 'error')
  } finally {
    importing.value = ''
    if (input) input.value = ''
  }
}

onMounted(() => {
  loadCourseClasses()
  loadKnownStudents()
})
watch(() => props.courseId, loadCourseClasses)
</script>

<template>
  <section class="audience-picker" aria-label="发布范围">
    <label class="audience-mode">
      <span>发布范围</span>
      <DsSelect v-model="mode" :disabled="disabled">
        <option value="all_enrolled">课程内全部学生</option>
        <option value="selected_classes">指定教学班</option>
        <option value="whitelist_only">仅白名单学生</option>
      </DsSelect>
    </label>

    <div v-if="mode === 'selected_classes'" class="audience-block">
      <strong>教学班</strong>
      <TeachingClassMultiSelect
        v-model="classIds"
        :options="courseClasses"
        :disabled="disabled"
        :loading="classesLoading"
        placeholder="选择课程内已绑定教学班"
        empty-text="当前课程尚未绑定教学班"
        test-id="audience-class-select"
      />
    </div>

    <div class="audience-block">
      <div class="audience-head">
        <strong>白名单学生</strong>
        <span class="audience-tip">这些学生会在基础范围之外追加</span>
      </div>
      <div v-if="whitelistIds.length" class="student-tags">
        <span v-for="id in whitelistIds" :key="'w' + id" class="student-tag">
          {{ studentName(id) }} · {{ studentNo(id) }}
          <button type="button" :disabled="disabled" @click="removeStudent('include', id)">×</button>
        </span>
      </div>
      <div class="student-add">
        <input v-model="search" type="search" placeholder="搜索姓名、学号或账号" @keyup.enter.prevent="searchStudents" />
        <button type="button" class="btn btn-secondary btn-sm" :disabled="disabled" @click="searchStudents">搜索</button>
        <label class="btn btn-ghost btn-sm">
          {{ importing === 'include' ? '导入中…' : '导入 CSV' }}
          <input :id="`audience-csv-${taskKind}-${taskId}-include`" type="file" accept=".csv,text/csv" hidden :disabled="disabled" @change="importCsv('include')" />
        </label>
      </div>
      <div v-if="candidates.length" class="student-candidates">
        <button v-for="student in candidates" :key="student.id" type="button" @click="addStudent('include', student)">
          {{ student.real_name }} · {{ student.student_no || student.username }} <b>加入</b>
        </button>
      </div>
    </div>

    <div class="audience-block">
      <div class="audience-head">
        <strong>排除名单</strong>
        <span class="audience-tip">从基础范围内排除；白名单优先于排除名单</span>
      </div>
      <div v-if="excludedIds.length" class="student-tags">
        <span v-for="id in excludedIds" :key="'e' + id" class="student-tag">
          {{ studentName(id) }} · {{ studentNo(id) }}
          <button type="button" :disabled="disabled" @click="removeStudent('exclude', id)">×</button>
        </span>
      </div>
      <div class="student-add">
        <input v-model="search" type="search" placeholder="搜索要排除的学生" @keyup.enter.prevent="searchStudents" />
        <button type="button" class="btn btn-secondary btn-sm" :disabled="disabled" @click="searchStudents">搜索</button>
        <label class="btn btn-ghost btn-sm">
          {{ importing === 'exclude' ? '导入中…' : '导入 CSV' }}
          <input :id="`audience-csv-${taskKind}-${taskId}-exclude`" type="file" accept=".csv,text/csv" hidden :disabled="disabled" @change="importCsv('exclude')" />
        </label>
      </div>
      <div v-if="candidates.length" class="student-candidates">
        <button v-for="student in candidates" :key="student.id" type="button" @click="addStudent('exclude', student)">
          {{ student.real_name }} · {{ student.student_no || student.username }} <b>排除</b>
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.audience-picker { display: flex; flex-direction: column; gap: var(--space-4); }
.audience-mode { display: flex; flex-direction: column; gap: 6px; color: var(--muted); font-size: var(--text-sm); }
.audience-mode .ds-select { width: 100%; }
.audience-block { display: flex; flex-direction: column; gap: 8px; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-sunken); }
.audience-block strong { color: var(--fg); }
.audience-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.audience-tip { color: var(--faint); font-size: var(--text-xs); }
.student-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.student-tag { display: inline-flex; align-items: center; gap: 6px; padding: 3px 7px; border-radius: var(--radius-sm); background: var(--accent-soft); color: var(--accent); font-size: var(--text-xs); }
.student-tag button { border: 0; background: transparent; color: var(--danger); cursor: pointer; padding: 0; font-size: 14px; line-height: 1; }
.student-add { display: flex; gap: 6px; flex-wrap: wrap; }
.student-add input { flex: 1; min-width: 180px; }
.student-candidates { display: flex; flex-wrap: wrap; gap: 6px; }
.student-candidates button { display: inline-flex; align-items: center; gap: 6px; padding: 5px 8px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--fg); cursor: pointer; }
.student-candidates b { color: var(--accent); }
</style>
