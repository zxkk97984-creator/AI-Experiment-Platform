<script setup>
// 课时编辑分派壳：加载课时 → 按 content_type 渲染对应编辑器。
// URL 唯一（/teacher/courses/:courseId/lessons/:lessonId/edit），刷新可恢复，不复制四份路由表。
// notebook 模板 id：query.template 优先 → 课时自身 template_id → 首次进入时初始化。
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import CourseFormModal from '../../components/teacher/CourseFormModal.vue'
import StudioEditor from '../../components/notebook/StudioEditor.vue'
import EnvironmentProfilePicker from '../../components/common/EnvironmentProfilePicker.vue'
import LessonMarkdownEditor from './LessonMarkdownEditor.vue'
import LessonExperimentEditor from './LessonExperimentEditor.vue'
import LessonVideoEditor from './LessonVideoEditor.vue'
import { coursesAPI } from '../../api/courses.js'
import { studioAPI } from '../../api/studio.js'
import { useAppStore } from '../../stores/app.js'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

const courseId = computed(() => route.params.courseId || route.params.id)
const lessonId = computed(() => route.params.lessonId)
const managePath = computed(() => `${route.path?.startsWith('/admin') ? '/admin' : '/teacher'}/courses/${courseId.value}/manage`)

const loading = ref(true)
const loadError = ref('')
const lesson = ref(null)

// notebook 模板解析：resolvingTemplate 解析中；templateId 命中；noTemplate 首次初始化
const resolvingTemplate = ref(false)
const templateId = ref(null)
const noTemplate = ref(false)
const creatingTemplate = ref(false)

// ── 首次进入的环境选择（避免静默创建错误环境） ──────────────────
const fallbackEnvOptions = ref([])
const fallbackEnvId = ref(null)
const fallbackPolicy = ref('unrestricted')
const fallbackAllowedImports = ref([])

function onFallbackEnvLoaded(options) {
  fallbackEnvOptions.value = options
  if (options.length && !fallbackEnvId.value) {
    fallbackEnvId.value = options[0].environment_version_id
  }
}

const selectedFallbackEnv = computed(
  () => fallbackEnvOptions.value.find((o) => o.environment_version_id === fallbackEnvId.value) || null,
)
const fallbackImportCandidates = computed(() => {
  if (!selectedFallbackEnv.value) return []
  const seen = new Set()
  const names = []
  for (const p of selectedFallbackEnv.value.packages || []) {
    for (const name of p.import_names || []) {
      if (!seen.has(name)) { seen.add(name); names.push(name) }
    }
  }
  return names
})
const fallbackMismatch = computed(() => {
  if (fallbackPolicy.value !== 'restricted' || fallbackAllowedImports.value.length === 0) return ''
  const installed = new Set(fallbackImportCandidates.value)
  const missing = fallbackAllowedImports.value.filter((name) => !installed.has(name))
  return missing.length ? `注意：${missing.join('、')} 未在当前环境安装` : ''
})

function toggleFallbackImport(name) {
  const idx = fallbackAllowedImports.value.indexOf(name)
  if (idx >= 0) fallbackAllowedImports.value.splice(idx, 1)
  else fallbackAllowedImports.value.push(name)
}

async function loadLesson() {
  loading.value = true
  loadError.value = ''
  lesson.value = null
  templateId.value = null
  noTemplate.value = false
  try {
    const response = await coursesAPI.getChapters(courseId.value)
    const raw = response.data
    const list = Array.isArray(raw) ? raw : (raw.items || [])
    const found = list
      .flatMap((chapter) => chapter.lessons || [])
      .find((item) => String(item.id) === String(lessonId.value))
    if (!found) {
      loadError.value = '课时不存在或已被删除'
      return
    }
    lesson.value = found
    if (found.content_type === 'notebook') {
      await resolveTemplate(found)
    }
  } catch (err) {
    loadError.value = '课时加载失败，请稍后重试'
    console.error('[LessonEditView] 加载课时失败', err)
  } finally {
    loading.value = false
  }
}

/** notebook 模板 id 解析：query → 课时自身字段 → 首次初始化 */
function resolveTemplate(ls) {
  resolvingTemplate.value = true
  try {
    // 1. 创建链路刚建的模板：query.template 数字有效直接使用
    const queryId = route.query.template
    if (queryId && /^\d+$/.test(String(queryId))) {
      templateId.value = Number(queryId)
      return
    }
    // 2. 课时返回的 template_id 是绑定关系的唯一来源，直接进入 Studio
    if (ls.template_id) {
      templateId.value = Number(ls.template_id)
      return
    }
    // 3. 历史孤立课时：首次进入时选择环境并自动完成初始化
    noTemplate.value = true
  } catch (err) {
    console.error('[LessonEditView] 解析模板失败', err)
  } finally {
    resolvingTemplate.value = false
  }
}

/** 首次初始化并进入 Studio（同路由 replace 追加 ?template，供刷新恢复） */
async function createTemplateAndEnter() {
  if (creatingTemplate.value || !lesson.value || !fallbackEnvId.value) return
  creatingTemplate.value = true
  try {
    const res = await studioAPI.createTemplate({
      name: lesson.value.title || '未命名实验',
      description: lesson.value.content || undefined,
      lesson_id: lesson.value.id,
      environment_version_id: fallbackEnvId.value,
      import_policy_mode: fallbackPolicy.value,
      allowed_imports: fallbackPolicy.value === 'restricted' ? [...fallbackAllowedImports.value] : [],
    })
    const template = res.data || res
    // 同路由 query 变化不会重新挂载组件，需同步更新本地状态立即进入 Studio；
    // replace 同步 URL 保证 F5 刷新后可直接恢复
    templateId.value = template.id
    noTemplate.value = false
    router.replace({ query: { ...route.query, template: template.id } })
  } catch (err) {
    app.showToast('进入 Studio 失败', 'error')
    console.error('[LessonEditView] 初始化 Notebook 失败', err)
  } finally {
    creatingTemplate.value = false
  }
}

function goBack() {
  router.push(managePath.value)
}

onMounted(loadLesson)
</script>

<template>
  <AppLayout content-mode="workspace">
    <div class="lesson-edit-page">
      <!-- 加载骨架屏（复用全局 .skeleton） -->
      <div v-if="loading" class="loading-card">
        <div v-for="i in 4" :key="i" class="skeleton skeleton-line"></div>
      </div>

      <!-- 加载失败 / 课时不存在 -->
      <div v-else-if="loadError" class="error-card">
        <h2>{{ loadError }}</h2>
        <p>请返回课时列表确认该课时仍然存在。</p>
        <button class="btn-primary" type="button" @click="goBack">返回</button>
      </div>

      <!-- notebook 模板解析中 -->
      <div v-else-if="resolvingTemplate" class="loading-card">
        <div v-for="i in 3" :key="i" class="skeleton skeleton-line"></div>
      </div>

      <!-- 历史 Notebook 首次进入：在当前页面弹窗选择环境并自动初始化 -->
      <CourseFormModal
        v-else-if="noTemplate"
        title="首次进入 Notebook"
        description="请选择运行环境，完成后即可进入 Studio。"
        title-id="notebook-setup-title"
        body-class="notebook-setup-body"
        :busy="creatingTemplate"
        @close="goBack"
        @submit="createTemplateAndEnter"
      >
        <div class="fallback-env">
          <EnvironmentProfilePicker
            v-model="fallbackEnvId"
            show-memory
            label="运行环境"
            @loaded="onFallbackEnvLoaded"
          />
          <p v-if="!fallbackEnvOptions.length" class="fallback-hint env-warn">暂无可用环境，请联系管理员</p>
          <div class="fallback-field">
            <label class="fallback-field-label" for="fallback-import-policy">导入规则</label>
            <DsSelect id="fallback-import-policy" v-model="fallbackPolicy" class="fallback-select">
              <option value="unrestricted">不限制</option>
              <option value="restricted">限定白名单</option>
            </DsSelect>
          </div>
          <div v-if="fallbackPolicy === 'restricted'" class="fallback-chips">
            <label v-for="name in fallbackImportCandidates" :key="name" class="fallback-chip">
              <input type="checkbox" :checked="fallbackAllowedImports.includes(name)" @change="toggleFallbackImport(name)" />
              {{ name }}
            </label>
            <p v-if="!fallbackImportCandidates.length" class="fallback-hint">当前环境未提供教学库，可留空白名单</p>
          </div>
          <p v-if="fallbackMismatch" class="fallback-hint env-warn">{{ fallbackMismatch }}</p>
        </div>
        <template #actions>
          <button class="btn btn-ghost" type="button" :disabled="creatingTemplate" @click="goBack">取消</button>
          <button class="btn btn-primary" type="submit" :disabled="creatingTemplate || !fallbackEnvId">
            <AppIcon name="arrow-right" :size="16" /> 进入 Studio
          </button>
        </template>
      </CourseFormModal>

      <!-- 分派：四类编辑器 -->
      <LessonMarkdownEditor
        v-else-if="lesson?.content_type === 'markdown'"
        :course-id="courseId"
        :lesson-id="lessonId"
        :back-path="managePath"
      />
      <LessonExperimentEditor
        v-else-if="lesson?.content_type === 'experiment'"
        :course-id="courseId"
        :lesson-id="lessonId"
        :back-path="managePath"
      />
      <LessonVideoEditor
        v-else-if="lesson?.content_type === 'video'"
        :course-id="courseId"
        :lesson-id="lessonId"
        :back-path="managePath"
      />
      <StudioEditor
        v-else-if="lesson?.content_type === 'notebook' && templateId"
        :template-id="templateId"
        :back-to="managePath"
      />
    </div>
  </AppLayout>
</template>

<style scoped>
.lesson-edit-page {
  min-height: 100%;
  background: var(--page-bg);
}
.loading-card {
  max-width: 960px;
  margin: 0 auto;
  padding: 48px 24px;
}
.skeleton-line { height: 18px; margin-bottom: 14px; }

.error-card {
  max-width: 960px;
  margin: 0 auto;
  padding: 48px 24px;
  text-align: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--surface);
}
/* ── 首次进入环境选择 ───────────────────────────────────────────── */
.fallback-env {
  max-width: 420px;
  width: 100%;
  margin: 0 auto 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  text-align: left;
}
.fallback-field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; line-height: var(--lh-body, 1.55); color: var(--muted); }
.fallback-field-label {
  display: block;
  padding-top: var(--space-1);
  color: inherit;
  font-size: inherit;
  font-weight: inherit;
  line-height: var(--lh-body, 1.55);
}
.fallback-select {
  width: 100%;
  height: auto;
  min-height: 38px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control, 7px);
  background: var(--surface);
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-sm, 13px);
  line-height: 1.4;
}
.fallback-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.fallback-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--surface-raised, var(--surface-subtle));
  font-size: var(--text-sm, 13px);
  cursor: pointer;
}
.fallback-chip input { margin: 0; }
.fallback-hint { margin: 0; font-size: var(--text-xs, 12px); color: var(--faint); }
.env-warn { color: var(--warning, var(--warning)); }
</style>
