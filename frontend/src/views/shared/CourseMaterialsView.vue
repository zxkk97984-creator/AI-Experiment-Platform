<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import ConfirmDialog from '../../components/ui/ConfirmDialog.vue'
import MaterialEditor from '../../components/materials/MaterialEditor.vue'
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { coursesAPI } from '../../api/courses.js'
import { courseMaterialsAPI as api } from '../../api/courseMaterials.js'
import { useAuthStore } from '../../stores/auth.js'
import { positiveRouteId } from '../../utils/taskEditorNavigation.js'

const route = useRoute(), router = useRouter(), auth = useAuthStore()
const courses = ref([]), courseId = ref(''), offerings = ref([]), offeringId = ref('')
const chapters = ref([]), materials = ref([]), activities = ref([]), canEdit = ref(false)
const ordinaryMaxBytes = ref(null), largeMaxBytes = ref(null)
const loading = ref(true), error = ref(''), editorError = ref(''), actionError = ref('')
const editing = ref(false), editorMaterial = ref(null), busy = ref(false), progress = ref(null), preview = ref(null), removing = ref(null)
const page = ref(1), total = ref(0), chapterFilter = ref(''), search = ref('')
const activityOptions = ref([])
const linkForm = ref({ chapter_id: '', target_type: 'assignment', target_id: '' })
let controller = null, requestSerial = 0
const categories = { lecture: '课件与讲义', notebook: 'Notebook', tool: '软件与工具', video: '视频' }
const selectedOffering = computed(() => offerings.value.find(row => String(row.id) === String(offeringId.value)))
const canLink = computed(() => auth.role !== 'student' && selectedOffering.value?.status === 'active')
const filteredOptions = computed(() => activityOptions.value.filter(row => row.target_type === linkForm.value.target_type))
const groups = computed(() => {
  const entries = [{ id: null, title: '课程公共资料' }, ...chapters.value]
  return entries.filter(ch => !chapterFilter.value || String(ch.id) === chapterFilter.value).map(ch => ({ ...ch,
    materials: materials.value.filter(row => row.chapter_id === ch.id), activities: activities.value.filter(row => row.chapter_id === ch.id) }))
})
const message = err => err.response?.data?.detail?.message || '操作失败，请稍后重试'
const size = value => value == null ? '' : `${(value / 1048576).toFixed(1)} MiB`

async function allChapters() {
  const first = (await api.chapters(courseId.value, { page: 1, page_size: 100 })).data
  if (Array.isArray(first)) return first
  const rest = []
  for (let index = 2; index <= Math.ceil((first.total || 0) / 100); index++) rest.push(api.chapters(courseId.value, { page: index, page_size: 100 }))
  const pages = await Promise.all(rest)
  return [...(first.items || []), ...pages.flatMap(result => result.data.items || [])]
}

async function load() {
  const serial = ++requestSerial
  loading.value = true; error.value = ''; actionError.value = ''; preview.value = null
  ordinaryMaxBytes.value = null; largeMaxBytes.value = null; canEdit.value = false
  try {
    const params = { page: page.value, page_size: 50, q: search.value || undefined,
      chapter_id: chapterFilter.value || undefined, offering_id: offeringId.value || undefined }
    const results = await Promise.all([api.list(courseId.value, params), allChapters(), api.offerings(courseId.value)])
    if (serial !== requestSerial) return
    materials.value = results[0].data.items; canEdit.value = results[0].data.can_edit; total.value = results[0].data.total
    ordinaryMaxBytes.value = results[0].data.ordinary_max_bytes; largeMaxBytes.value = results[0].data.large_max_bytes
    chapters.value = results[1]; offerings.value = results[2].data.items || []
    if (!offerings.value.some(row => String(row.id) === String(offeringId.value))) offeringId.value = String(offerings.value[0]?.id || '')
    const activityResults = await Promise.all([
      offeringId.value ? api.activities(offeringId.value) : Promise.resolve({ data: [] }),
      canLink.value ? api.activityOptions(offeringId.value) : Promise.resolve({ data: [] }),
    ])
    if (serial !== requestSerial) return
    activities.value = activityResults[0].data; activityOptions.value = activityResults[1].data
  } catch (err) { if (serial === requestSerial) error.value = message(err) }
  finally { if (serial === requestSerial) loading.value = false }
}
async function init() {
  loading.value = true; error.value = ''; courseId.value = ''
  try {
    const { data } = await coursesAPI.list({ page_size: 100 })
    courses.value = data.items || []
    const requestedValue = route.params.courseId ?? route.query.course_id
    const requested = positiveRouteId(requestedValue)
    if (requestedValue != null && requestedValue !== '' && !requested) {
      error.value = '课程参数无效，请重新选择课程。'
      return
    }
    if (requested && !courses.value.some(row => String(row.id) === requested)) {
      const { data: course } = await coursesAPI.get(requested)
      courses.value = [...courses.value, course]
    }
    courseId.value = requested || String(courses.value[0]?.id || '')
    offeringId.value = String(route.query.offering_id || '')
    if (courseId.value) await load()
  } catch (err) { error.value = message(err) }
  finally { loading.value = false }
}
function filterChanged() { page.value = 1; load() }
function courseChanged() { offeringId.value = ''; chapterFilter.value = ''; filterChanged() }
function edit(row = null) { editorMaterial.value = row; editorError.value = ''; editing.value = true }
async function save(form) {
  busy.value = true; editorError.value = ''; progress.value = null
  try {
    let row = editorMaterial.value
    const metadata = { title: form.title, description: form.description || null, chapter_id: form.chapter_id,
      category: form.category, order_index: form.order_index }
    if (!row) row = (await api.create(courseId.value, { ...metadata, source: form.source,
      external_url: form.source === 'external' ? form.external_url : null })).data
    else row = (await api.update(row.id, { ...metadata, version: row.version,
      ...(row.source === 'external' ? { external_url: form.external_url } : {}) })).data
    editorMaterial.value = row
    if (form.file) {
      controller = new AbortController(); progress.value = 0
      row = (await api.upload(row.id, form.file, row.version, { signal: controller.signal,
        onUploadProgress: event => { progress.value = event.total ? Math.round(event.loaded / event.total * 100) : 0 } })).data
      editorMaterial.value = row
    }
    if (row.status !== form.status) row = (await api.update(row.id, { version: row.version, status: form.status })).data
    editing.value = false; await load()
  } catch (err) { editorError.value = err.code === 'ERR_CANCELED' ? '上传请求已取消，请刷新确认资料状态。' : message(err) }
  finally { busy.value = false; progress.value = null; controller = null }
}
async function access(row, inline = false) {
  actionError.value = ''
  try {
    const { data } = await api.access(row.id, { offering_id: offeringId.value || undefined, preview: inline })
    if (!data.url.startsWith('/api/v1/media/course-materials/')) throw new Error('invalid media URL')
    if (inline && data.previewable) preview.value = { ...row, url: data.url }
    else { const anchor = document.createElement('a'); anchor.href = data.url; anchor.download = row.filename || row.title; anchor.click() }
  } catch (err) { actionError.value = message(err) }
}
async function archive() {
  busy.value = true
  try { await api.archive(removing.value.id, removing.value.version); removing.value = null; await load() }
  catch (err) { actionError.value = message(err); removing.value = null }
  finally { busy.value = false }
}
async function addLink() {
  busy.value = true; actionError.value = ''
  try { await api.link(offeringId.value, { ...linkForm.value, chapter_id: Number(linkForm.value.chapter_id), target_id: Number(linkForm.value.target_id) }); await load() }
  catch (err) { actionError.value = message(err) }
  finally { busy.value = false }
}
async function unlink(row) {
  try { await api.unlink(row.id); await load() } catch (err) { actionError.value = message(err) }
}
function goActivity(row) {
  if (row.route.startsWith(`/${auth.role}/`)) router.push(row.route)
}
onMounted(init)
onBeforeUnmount(() => { ++requestSerial; controller?.abort() })
</script>

<template>
  <AppLayout content-mode="list">
    <div class="materials-page">
      <header class="page-heading"><div><h1>课程资料</h1><p>按章节查看课件、Notebook、软件与视频，以及本班的学习活动。</p></div><button v-if="canEdit" type="button" class="btn-primary" @click="edit()">添加资料</button></header>
      <div class="filters">
        <label>课程<select v-model="courseId" :disabled="loading" @change="courseChanged"><option v-for="course in courses" :key="course.id" :value="String(course.id)">{{ course.title }}</option></select></label>
        <label>授课班级<select v-model="offeringId" :disabled="loading || !offerings.length" @change="filterChanged"><option v-if="!offerings.length" value="">共享课程内容</option><option v-for="offering in offerings" :key="offering.id" :value="String(offering.id)">{{ offering.class_name }} · {{ offering.term_name }}</option></select></label>
        <label>章节<select v-model="chapterFilter" :disabled="loading" @change="filterChanged"><option value="">全部章节</option><option v-for="chapter in chapters" :key="chapter.id" :value="String(chapter.id)">{{ chapter.title }}</option></select></label>
        <form class="search" @submit.prevent="filterChanged"><label>搜索资料<input v-model="search" type="search" maxlength="200" placeholder="资料标题" /></label><button type="submit" class="btn-outline" :disabled="loading || !courseId">搜索</button></form>
      </div>
      <p v-if="!canEdit && auth.role !== 'student' && courseId" class="notice">共享资料由课程负责人维护；你可以管理自己授课班级的活动关联。</p>
      <p v-if="actionError" role="alert" class="error">{{ actionError }}</p>
      <div v-if="loading" class="state" role="status" aria-busy="true">正在加载课程资料…</div>
      <div v-else-if="error" class="state" role="alert"><p>{{ error }}</p><button type="button" class="btn-outline" @click="courseId ? load() : init()">重试</button></div>
      <div v-else-if="!courses.length" class="state">暂无可访问的课程，请联系授课教师。</div>
      <template v-else>
        <section v-for="group in groups" :key="group.id ?? 'common'" class="chapter-section">
          <h2>{{ group.title }}</h2>
          <p v-if="!group.materials.length && !group.activities.length" class="empty">本章节暂无资料或活动。</p>
          <ul class="material-list">
            <li v-for="row in group.materials" :key="row.id" class="material-row">
              <div class="material-copy"><h3>{{ row.title }}<span v-if="row.status !== 'published'" class="badge">草稿</span></h3><p v-if="row.description">{{ row.description }}</p><p class="meta">{{ categories[row.category] }} · {{ row.source === 'external' ? '官网链接' : (row.filename || '待上传文件') }} {{ size(row.size_bytes) }} · 版本 {{ row.version }}</p></div>
              <div class="actions"><a v-if="row.source === 'external'" :href="row.external_url" target="_blank" rel="noopener noreferrer" class="btn-outline">打开官网</a><template v-else><button v-if="row.previewable" type="button" class="btn-outline" @click="access(row, true)">预览</button><button type="button" class="btn-outline" :disabled="!row.filename" @click="access(row)">下载</button></template><button v-if="canEdit" type="button" class="btn-ghost" @click="edit(row)">编辑</button><button v-if="canEdit" type="button" class="btn-ghost" @click="removing = row">移除</button></div>
            </li>
          </ul>
          <ul class="activities"><li v-for="row in group.activities" :key="row.id"><button type="button" class="btn-outline" @click="goActivity(row)">{{ row.title }} <span v-if="row.status !== 'published'">（草稿）</span></button><button v-if="canLink" type="button" class="btn-ghost" :aria-label="`移除 ${row.title} 的关联`" @click="unlink(row)">移除关联</button></li></ul>
        </section>
        <div v-if="total > 50" class="pagination"><button type="button" class="btn-outline" :disabled="page === 1" @click="page--; load()">上一页</button><span>第 {{ page }} 页 · {{ total }} 份资料</span><button type="button" class="btn-outline" :disabled="page * 50 >= total" @click="page++; load()">下一页</button></div>
        <section v-if="canLink" class="chapter-section"><h2>关联本班学习活动</h2><form class="link-form" @submit.prevent="addLink"><label>章节<select v-model="linkForm.chapter_id" required><option value="" disabled>选择章节</option><option v-for="chapter in chapters" :key="chapter.id" :value="chapter.id">{{ chapter.title }}</option></select></label><label>活动类型<select v-model="linkForm.target_type" @change="linkForm.target_id = ''"><option value="assignment">作业</option><option value="exam">考试</option><option value="lesson">课程实验</option><option value="module">独立实验</option></select></label><label>本班活动<select v-model="linkForm.target_id" required><option value="" disabled>{{ filteredOptions.length ? '选择已有活动' : '暂无可关联的活动' }}</option><option v-for="target in filteredOptions" :key="target.target_id" :value="target.target_id">{{ target.title }}{{ target.status === 'published' ? '' : '（草稿）' }}</option></select></label><button type="submit" class="btn-primary" :disabled="busy || !linkForm.target_id">关联活动</button></form><p class="notice">只能关联已有的本班活动；实验需先向本班发布。</p></section>
      </template>
    </div>
    <MaterialEditor v-if="editing" :material="editorMaterial" :chapters="chapters" :ordinary-max-bytes="ordinaryMaxBytes" :large-max-bytes="largeMaxBytes" :busy="busy" :progress="progress" :error="editorError" @save="save" @cancel="editing = false" @abort="controller?.abort()" />
    <ConfirmDialog v-if="removing" title="移除课程资料" message="资料将对所有授课班级不可见，文件不再提供下载。保留版本信息。" confirm-text="移除" danger :busy="busy" @confirm="archive" @cancel="removing = null" />
    <div v-if="preview" class="modal-backdrop" @click.self="preview = null" @keydown.esc="preview = null"><section v-dialog-focus class="modal preview-modal" role="dialog" aria-modal="true" :aria-label="preview.title"><header class="modal-head"><h2>{{ preview.title }}</h2><button type="button" class="btn-ghost" @click="preview = null">关闭预览</button></header><video v-if="preview.content_type?.startsWith('video/')" :src="preview.url" controls preload="metadata"></video><iframe v-else :src="preview.url" :title="`${preview.title} PDF 预览`"></iframe><p class="notice">预览地址有效期 15 分钟；失效后请关闭并重新打开。</p></section></div>
  </AppLayout>
</template>

<style scoped>
.materials-page { display: flex; flex-direction: column; gap: var(--space-5); min-width: 0; }
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
h1 { margin: 0; font-size: var(--text-xl); } h2 { margin: 0 0 var(--space-3); font-size: var(--text-lg); } h3 { margin: 0; font-size: var(--text-base); overflow-wrap: anywhere; }
.page-heading p, .notice, .meta, .empty { color: var(--muted); font-size: var(--text-sm); }
.filters, .link-form { display: flex; flex-wrap: wrap; gap: var(--space-3); align-items: end; }
label { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; flex: 1 1 160px; font-size: var(--text-sm); }
input, select { min-width: 0; width: 100%; } .search { display: flex; flex: 1 1 220px; gap: var(--space-2); align-items: end; }
.chapter-section { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: var(--space-4); }
.material-list, .activities { list-style: none; margin: 0; padding: 0; }
.material-row { display: flex; justify-content: space-between; gap: var(--space-4); padding: var(--space-3) 0; border-bottom: 1px solid var(--line); }
.material-row:last-child { border-bottom: 0; } .material-copy { min-width: 0; flex: 1; } .material-copy p { margin: var(--space-1) 0; overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); } .badge { margin-left: var(--space-2); font-size: var(--text-sm); color: var(--muted); }
.activities { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-3); } .activities li { display: flex; flex-wrap: wrap; gap: var(--space-1); }
.state { padding: var(--space-6); text-align: center; } .error { color: var(--danger); } .pagination { display: flex; justify-content: center; gap: var(--space-3); align-items: center; }
.preview-modal { width: min(960px, 94vw); max-width: 960px; } .preview-modal iframe, .preview-modal video { width: 100%; height: min(65vh, 650px); border: 0; } .preview-modal p { padding: 0 var(--space-3); }
@media (max-width: 640px) { .page-heading, .material-row { flex-direction: column; } .actions { width: 100%; } .chapter-section { padding: var(--space-3); } .pagination { flex-wrap: wrap; } }
</style>
