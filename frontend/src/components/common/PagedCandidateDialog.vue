<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import TeacherPagination from '../teacher/TeacherPagination.vue'
import { vDialogFocus } from '../../directives/dialogFocus.js'
import { createLatestRequestGuard } from '../../utils/latestRequest.js'

const props = defineProps({
  title: { type: String, required: true },
  fetchPage: { type: Function, required: true },
  selected: { type: Array, default: () => [] },
  multiple: { type: Boolean, default: false },
  label: { type: Function, required: true },
  disabledReason: { type: Function, default: () => '' },
  searchPlaceholder: { type: String, default: '输入名称搜索' },
})
const emit = defineEmits(['confirm', 'close'])
const pageSize = 20
const query = ref(''), rows = ref([]), page = ref(1), total = ref(0)
const loading = ref(false), error = ref('')
const searchInput = ref(null)
const chosen = ref(new Map(props.selected.map(row => [Number(row.id), row])))
const selectedRows = computed(() => [...chosen.value.values()])
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const guard = createLatestRequestGuard()
let timer, pendingPage = 1

async function load(nextPage = 1) {
  clearTimeout(timer)
  const ticket = guard.begin()
  pendingPage = nextPage
  loading.value = true; error.value = ''
  try {
    const { data } = await props.fetchPage({ page: nextPage, page_size: pageSize, q: query.value.trim() || undefined })
    if (!guard.isLatest(ticket)) return
    rows.value = data.items || []
    total.value = Number(data.total ?? rows.value.length)
    page.value = nextPage
    for (const row of rows.value) {
      if (chosen.value.has(Number(row.id))) chosen.value.set(Number(row.id), row)
    }
  } catch (e) {
    if (guard.isLatest(ticket)) error.value = e.response?.data?.detail?.message || '候选列表加载失败，请重试'
  } finally {
    if (guard.isLatest(ticket)) loading.value = false
  }
}
function toggle(row) {
  if (loading.value || error.value || props.disabledReason(row)) return
  const id = Number(row.id)
  if (!props.multiple) chosen.value = new Map([[id, row]])
  else if (chosen.value.has(id)) chosen.value.delete(id)
  else chosen.value.set(id, row)
}
function confirm() {
  if (loading.value || error.value || selectedRows.value.some(row => props.disabledReason(row))) return
  emit('confirm', selectedRows.value)
}
async function remove(row) {
  chosen.value.delete(Number(row.id))
  await nextTick()
  searchInput.value?.focus()
}
watch(query, () => {
  guard.invalidate(); clearTimeout(timer)
  loading.value = true; error.value = ''; pendingPage = 1
  timer = setTimeout(() => load(1), 250)
})
onMounted(() => load())
onBeforeUnmount(() => { clearTimeout(timer); guard.invalidate() })
</script>

<template>
  <Teleport to="body">
    <div class="candidate-backdrop" @click.self="emit('close')">
      <section v-dialog-focus class="candidate-dialog" role="dialog" aria-modal="true" :aria-label="title" @keydown.esc.stop="emit('close')">
        <header><h2>{{ title }}</h2><button type="button" aria-label="关闭选择窗口" @click="emit('close')">关闭</button></header>
        <label class="candidate-search">搜索<input ref="searchInput" v-model="query" type="search" :aria-label="`搜索${title}`" :placeholder="searchPlaceholder" /></label>
        <p class="candidate-hint">{{ searchPlaceholder }}</p>
        <section class="candidate-selected" aria-label="已选项目">
          <p>已选 {{ selectedRows.length }} 项</p>
          <ul v-if="selectedRows.length"><li v-for="row in selectedRows" :key="row.id"><span>{{ label(row) }}</span><button type="button" :aria-label="`移除${label(row)}`" @click="remove(row)">移除</button></li></ul>
        </section>
        <p v-if="error" role="alert">{{ error }} <button type="button" @click="load(pendingPage)">重试</button></p>
        <p v-if="loading" role="status">正在加载候选…</p>
        <div class="candidate-results" :aria-busy="loading">
          <label v-for="row in rows" :key="row.id" class="candidate-row">
            <input :type="multiple ? 'checkbox' : 'radio'" name="candidate" :checked="chosen.has(Number(row.id))" :disabled="loading || !!error || !!disabledReason(row)" @change="toggle(row)" />
            <span>{{ label(row) }}<small v-if="disabledReason(row)">{{ disabledReason(row) }}</small></span>
          </label>
          <p v-if="!loading && !error && !rows.length">没有匹配的候选，请调整搜索条件。</p>
        </div>
        <fieldset :disabled="loading || !!error" class="candidate-pagination"><TeacherPagination :current-page="page" :page-count="pageCount" :total="total" :page-size="pageSize" :aria-label="`${title}分页`" @change="load" /></fieldset>
        <footer><button type="button" @click="emit('close')">取消</button><button type="button" :disabled="loading || !!error || (!multiple && !selectedRows.length) || selectedRows.some(row => disabledReason(row))" @click="confirm">确认选择</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.candidate-backdrop{position:fixed;inset:0;z-index:1000;background:var(--overlay,rgba(0,0,0,.45));display:flex;align-items:center;justify-content:center;padding:16px}
.candidate-dialog{width:min(720px,100%);max-height:calc(100dvh - 32px);overflow:auto;background:var(--surface);color:var(--fg);border:1px solid var(--border);border-radius:var(--radius-lg,12px);padding:24px;display:grid;gap:16px;box-sizing:border-box}
header,footer{display:flex;justify-content:space-between;gap:16px;align-items:center}header h2{margin:0;font-size:var(--text-lg,20px)}footer{justify-content:flex-end}.candidate-dialog button{padding:8px 12px;border:1px solid var(--border);border-radius:var(--radius-md,6px);background:var(--surface);color:var(--fg);cursor:pointer}.candidate-dialog button:disabled{opacity:.5;cursor:default}
.candidate-search{display:grid;gap:8px}.candidate-search input{width:100%;min-width:0;padding:10px;box-sizing:border-box;border:1px solid var(--border);border-radius:var(--radius-md,6px);background:var(--surface);color:var(--fg)}.candidate-hint{margin:0;color:var(--muted);font-size:var(--text-sm,13px)}.candidate-selected p{margin:0 0 8px}.candidate-selected ul{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0;margin:0;max-height:120px;overflow:auto}.candidate-selected li{display:flex;align-items:center;gap:8px;border:1px solid var(--border);padding:4px 8px;max-width:100%;box-sizing:border-box}.candidate-selected span{overflow-wrap:anywhere;min-width:0}.candidate-selected button{flex-shrink:0}
.candidate-results{display:grid;gap:4px;max-height:320px;overflow:auto}.candidate-row{display:flex;align-items:start;gap:12px;padding:8px;border-bottom:1px solid var(--border)}.candidate-row input{flex-shrink:0;margin-top:4px}.candidate-row span{min-width:0;overflow-wrap:anywhere}.candidate-row small{display:block;color:var(--muted)}.candidate-pagination{border:0;padding:0;margin:0;min-width:0}[role=alert]{color:var(--danger,#b91c1c)}
@media(max-width:480px){.candidate-backdrop{padding:8px}.candidate-dialog{padding:16px;max-height:calc(100dvh - 16px)}.candidate-dialog :deep(.teacher-pagination){padding:8px 0;gap:8px}}
</style>
