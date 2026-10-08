<script setup>
import { computed, ref, watch } from 'vue'
import TeachingClassMultiSelect from './TeachingClassMultiSelect.vue'
import { coursesAPI } from '../../api/courses.js'
import { usersAPI } from '../../api/users.js'
import { assignmentsAPI } from '../../api/assignments.js'
import { examsAPI } from '../../api/exams.js'
import { offeringsAPI } from '../../api/offerings.js'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'
import { useAppStore } from '../../stores/app.js'

const props = defineProps({
  taskKind: { type: String, required: true }, // assignment | exam
  taskId: { type: [Number, String], required: true },
  courseId: { type: [Number, String], default: null },
  offeringId: { type: [Number, String], default: null },
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
const offeringStudents = ref([])
const scopeError = ref('')
const scopeReady = ref(false)
const scopeGuard = createLatestRequestGuard()
const searchGuard = createLatestRequestGuard()
const isScoped = computed(() => Boolean(props.offeringId))
const controlsDisabled = computed(() => props.disabled || (isScoped.value && !scopeReady.value))

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
  if (controlsDisabled.value || (isScoped.value && !offeringStudents.value.some(row => Number(row.id) === Number(student.id)))) return
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
  if (controlsDisabled.value) return
  if (isScoped.value) {
    const query = search.value.trim().toLocaleLowerCase()
    candidates.value = offeringStudents.value.filter(student =>
      !query || [student.real_name, student.student_no, student.username].some(value => String(value || '').toLocaleLowerCase().includes(query)))
    return
  }
  const ticket = searchGuard.begin()
  try {
    const res = await usersAPI.listStudents({ q: search.value.trim() || undefined, page_size: 30 })
    if (!searchGuard.isLatest(ticket) || isScoped.value) return
    const rows = res.data.items || []
    for (const student of rows) studentMap.value.set(Number(student.id), student)
    candidates.value = rows
  } catch {
    if (!searchGuard.isLatest(ticket)) return
    candidates.value = []
    app.showToast('搜索学生失败', 'error')
  }
}

async function loadCourseClasses(ticket) {
  if (!props.courseId) { courseClasses.value = []; return }
  classesLoading.value = true
  try {
    const res = await coursesAPI.get(props.courseId)
    if (!scopeGuard.isLatest(ticket)) return
    courseClasses.value = res.data.teaching_classes || []
  } catch {
    if (!scopeGuard.isLatest(ticket)) return
    courseClasses.value = []
  } finally {
    if (scopeGuard.isLatest(ticket)) classesLoading.value = false
  }
}

async function loadKnownStudents(ticket) {
  try {
    const res = isScoped.value ? await offeringsAPI.students(props.offeringId) : await usersAPI.listStudents({ page_size: 100 })
    if (!scopeGuard.isLatest(ticket)) return
    const rows = isScoped.value ? (res.data.items || []).filter(student => student.status === 'active') : (res.data.items || [])
    if (isScoped.value) offeringStudents.value = rows
    for (const student of rows) studentMap.value.set(Number(student.id), student)
  } catch {
    if (scopeGuard.isLatest(ticket) && isScoped.value) {
      offeringStudents.value = []
      candidates.value = []
      scopeReady.value = false
      scopeError.value = '本班名单加载失败，请重试。'
    }
  }
}

async function loadOptions() {
  const ticket = scopeGuard.begin()
  searchGuard.invalidate()
  courseClasses.value = []
  candidates.value = []
  offeringStudents.value = []
  studentMap.value = new Map()
  scopeError.value = ''
  scopeReady.value = false
  if (!props.courseId) { classesLoading.value = false; return }
  if (!isScoped.value) {
    await Promise.all([loadCourseClasses(ticket), loadKnownStudents(ticket)])
    return
  }
  classesLoading.value = true
  try {
    const [offeringResponse, studentResponse] = await Promise.all([
      offeringsAPI.list({ course_id: props.courseId }),
      offeringsAPI.students(props.offeringId),
    ])
    if (!scopeGuard.isLatest(ticket)) return
    const offering = (offeringResponse.data.items || []).find(row => Number(row.id) === Number(props.offeringId))
    if (!offering || Number(offering.course_id) !== Number(props.courseId)) throw new Error('scope unavailable')
    courseClasses.value = [{ id: offering.teaching_class_id, name: offering.class_name, status: 'active' }]
    offeringStudents.value = (studentResponse.data.items || []).filter(student => student.status === 'active')
    studentMap.value = new Map(offeringStudents.value.map(student => [Number(student.id), student]))
    scopeReady.value = true
  } catch {
    if (!scopeGuard.isLatest(ticket)) return
    scopeError.value = '本班班级和名单加载失败，请重试。'
  } finally {
    if (scopeGuard.isLatest(ticket)) classesLoading.value = false
  }
}

async function importCsv(kind) {
  if (controlsDisabled.value) return
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
    await loadOptions()
  } catch (error) {
    app.showToast(error.response?.data?.detail?.message || '导入失败', 'error')
  } finally {
    importing.value = ''
    if (input) input.value = ''
  }
}

watch(() => [props.courseId, props.offeringId, props.taskId, props.taskKind], loadOptions, { immediate: true })
</script>

<template>
  <section class="audience-picker" aria-label="发布范围">
    <p v-if="isScoped" class="audience-tip">仅可选择本授课班在册学生，补选学生需先由管理员加入本班。</p>
    <div v-if="scopeError" role="alert">
      {{ scopeError }}
      <button type="button" class="btn btn-secondary btn-sm" @click="loadOptions">重试</button>
    </div>
    <label class="audience-mode">
      <span>发布范围</span>
      <DsSelect v-model="mode" :disabled="controlsDisabled">
        <option value="all_enrolled">{{ isScoped ? '本授课班全部学生' : '课程内全部学生' }}</option>
        <option value="selected_classes">指定教学班</option>
        <option value="whitelist_only">仅白名单学生</option>
      </DsSelect>
    </label>

    <div v-if="mode === 'selected_classes'" class="audience-block">
      <strong>教学班</strong>
      <TeachingClassMultiSelect
        v-model="classIds"
        :options="courseClasses"
        :disabled="controlsDisabled"
        :loading="classesLoading"
        :placeholder="isScoped ? '选择本任务所属教学班' : '选择课程内已绑定教学班'"
        :empty-text="isScoped ? '本班信息暂不可用' : '当前课程尚未绑定教学班'"
        test-id="audience-class-select"
      />
    </div>

    <div class="audience-block">
      <div class="audience-head">
        <strong>白名单学生</strong>
        <span class="audience-tip">{{ isScoped ? '在本班基础范围之外追加' : '这些学生会在基础范围之外追加' }}</span>
      </div>
      <div v-if="whitelistIds.length" class="student-tags">
        <span v-for="id in whitelistIds" :key="'w' + id" class="student-tag">
          {{ studentName(id) }} · {{ studentNo(id) }}
          <button type="button" :disabled="disabled" @click="removeStudent('include', id)">×</button>
        </span>
      </div>
      <div class="student-add">
        <input v-model="search" type="search" :disabled="controlsDisabled" placeholder="搜索姓名、学号或账号" @keyup.enter.prevent="searchStudents" />
        <button type="button" class="btn btn-secondary btn-sm" :disabled="controlsDisabled" @click="searchStudents">搜索</button>
        <label class="btn btn-ghost btn-sm">
          {{ importing === 'include' ? '导入中…' : '导入 CSV' }}
          <input :id="`audience-csv-${taskKind}-${taskId}-include`" type="file" accept=".csv,text/csv" hidden :disabled="controlsDisabled" @change="importCsv('include')" />
        </label>
      </div>
      <div v-if="candidates.length" class="student-candidates">
        <button v-for="student in candidates" :key="student.id" type="button" :disabled="controlsDisabled" @click="addStudent('include', student)">
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
        <input v-model="search" type="search" :disabled="controlsDisabled" placeholder="搜索要排除的学生" @keyup.enter.prevent="searchStudents" />
        <button type="button" class="btn btn-secondary btn-sm" :disabled="controlsDisabled" @click="searchStudents">搜索</button>
        <label class="btn btn-ghost btn-sm">
          {{ importing === 'exclude' ? '导入中…' : '导入 CSV' }}
          <input :id="`audience-csv-${taskKind}-${taskId}-exclude`" type="file" accept=".csv,text/csv" hidden :disabled="controlsDisabled" @change="importCsv('exclude')" />
        </label>
      </div>
      <div v-if="candidates.length" class="student-candidates">
        <button v-for="student in candidates" :key="student.id" type="button" :disabled="controlsDisabled" @click="addStudent('exclude', student)">
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
