<template>
  <div class="ai-config-form">
    <!-- 评分模式 -->
    <div class="form-group">
      <label>评分模式</label>
      <DsSelect v-model="config.grading_mode" @change="emitUpdate">
        <option value="legacy">legacy — 传统判题（仅通过/不通过）</option>
        <option value="shadow">shadow — AI 评分仅教师可见，正式分用旧规则</option>
        <option value="active">active — AI 评分计入正式成绩</option>
      </DsSelect>
    </div>

    <!-- 测试组（F/R） -->
    <div class="section" id="test-groups-section">
      <div class="section-header">
        <h5>功能/鲁棒性测试组 (F + R = 70)</h5>
        <div class="section-header-actions">
          <button
            class="btn-sm btn-ai"
            data-testid="generate-test-groups"
            :disabled="generating"
            @click="$emit('generate-test-groups')"
          >
            {{ generating ? '生成中…' : 'AI 生成测试组' }}
          </button>
          <button class="btn-sm btn-outline" @click="addGroup">+ 添加测试组</button>
        </div>
      </div>
      <div v-if="!config.test_groups?.length" class="hint">暂无测试组。F 组满分总计 60，R 组满分总计 10。</div>
      <div v-for="(g, i) in config.test_groups" :key="i" class="group-card">
        <div class="group-row">
          <input v-model="g.id" placeholder="ID (如 F1)" class="input-sm" @change="emitUpdate" />
          <input v-model="g.name" placeholder="名称" class="input-sm" @change="emitUpdate" />
          <DsSelect v-model="g.dimension" class="input-sm" @change="emitUpdate">
            <option value="F">F 功能 (满分合计 60)</option>
            <option value="R">R 鲁棒性 (满分合计 10)</option>
          </DsSelect>
          <input v-model.number="g.max_score" type="number" placeholder="满分" class="input-sm input-num" @change="emitUpdate" />
          <button class="btn-sm btn-danger-text" @click="removeGroup(i)">删除</button>
        </div>
        <textarea v-model="g.tests" rows="4" class="code-editor" placeholder="pytest 测试代码..." @change="emitUpdate"></textarea>
      </div>
      <div v-if="validationMsg" :class="validationMsg.ok ? 'hint-ok' : 'hint-err'">{{ validationMsg.text }}</div>
    </div>

    <!-- 教师约束 -->
    <div class="form-group">
      <label>教师硬性要求（可选）</label>
      <p class="field-help">此处内容将用于生成评分规则并影响 AI 评分。请填写明确、可判断的硬性要求，不要填写一般性建议。</p>
      <div v-if="hasLegacyConstraints" class="legacy-constraints" role="status">
        <span>旧版结构数据，请重新填写。保存其他配置时将保留原数据。</span>
        <button type="button" class="btn-sm btn-outline" @click="replaceLegacyConstraints">改用自然语言重新填写</button>
      </div>
      <textarea
        v-model="constraintsText"
        data-testid="teacher-constraints-input"
        rows="5"
        class="code-editor"
        :readonly="hasLegacyConstraints"
        maxlength="2000"
        placeholder="每行填写一条必须满足的规则，例如：&#10;必须正确处理空列表&#10;禁止使用全局变量&#10;时间复杂度不得高于 O(n)"
        @input="emitConstraints"
      ></textarea>
      <div v-if="!hasLegacyConstraints" class="character-count">{{ constraintsText.length }}/2000</div>
    </div>

    <!-- 参考答案 -->
    <div class="form-group">
      <label>参考答案 (可选，仅供 AI 理解题目)</label>
      <textarea v-model="config.reference_solution" rows="6" class="code-editor" placeholder="# 参考实现..." @change="emitUpdate"></textarea>
    </div>

    <!-- 上限规则 -->
    <div class="section">
      <div class="section-header">
        <h5>分数上限规则</h5>
        <button class="btn-sm btn-outline" @click="addCapRule">+ 添加上限</button>
      </div>
      <div v-if="!config.score_cap_rules?.length" class="hint">无上限规则。</div>
      <div v-for="(r, i) in config.score_cap_rules" :key="i" class="cap-row">
        <input v-model="r.id" placeholder="ID" class="input-sm" @change="emitUpdate" />
        <DsSelect v-model="r.condition_code" class="input-sm" @change="emitUpdate">
          <option value="off_topic">偏题</option>
          <option value="hardcoded_public_examples">硬编码样例</option>
          <option value="required_algorithm_missing">缺失要求算法</option>
          <option value="required_complexity_missing">复杂度不达标</option>
          <option value="dangerous_operation">危险操作</option>
        </DsSelect>
        <input v-model="r.description" placeholder="描述（必填）" class="input-sm" @change="emitUpdate" style="width:120px" />
        <input v-model.number="r.cap" type="number" placeholder="上限分" class="input-sm input-num" @change="emitUpdate" />
        <button class="btn-sm btn-danger-text" @click="removeCapRule(i)">删除</button>
      </div>
    </div>
  </div>
</template>

<script setup>
// AiConfigForm：AI 评分配置「纯表单层」——只通过 v-model 读写配置数据，
// 不要求 questionId 存在、不直接调后端。父组件（草稿模式/持久化容器）负责
// 持久化与 Rubric 生成。
import { ref, computed } from 'vue'

const props = defineProps({
  modelValue: { type: Object, required: true },  // 配置快照：grading_mode/teacher_constraints/reference_solution/test_groups/score_cap_rules
  /** AI 生成测试组进行中：禁用按钮防重复点击（请求/状态由容器层管理） */
  generating: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'generate-test-groups'])

// 本地编辑态：挂载时从 modelValue 深拷贝一份（数据均为纯 JSON，深拷贝安全）；
// 父组件重置数据时通过 :key 重挂本组件重新初始化。
const clone = (v) => JSON.parse(JSON.stringify(v))
const config = ref(clone(props.modelValue))

const sourceConstraints = props.modelValue?.teacher_constraints
const constraintKeys = sourceConstraints && typeof sourceConstraints === 'object' && !Array.isArray(sourceConstraints)
  ? Object.keys(sourceConstraints)
  : []
const isCanonicalConstraints = constraintKeys.length === 1
  && constraintKeys[0] === 'requirements_text'
  && typeof sourceConstraints.requirements_text === 'string'
const hasLegacyConstraints = ref(constraintKeys.length > 0 && !isCanonicalConstraints)
const constraintsText = ref(
  isCanonicalConstraints
    ? sourceConstraints.requirements_text
    : (hasLegacyConstraints.value ? '旧版结构数据，请重新填写' : ''),
)

// 编辑结果整体上抛（仅 v-model 同步数据，持久化由父组件负责）
function emitUpdate() {
  emit('update:modelValue', clone(config.value))
}

// 自然语言在表单边界收敛为后端既有 dict 契约。
function emitConstraints() {
  if (hasLegacyConstraints.value) return
  const requirementsText = constraintsText.value.trim()
  config.value.teacher_constraints = requirementsText ? { requirements_text: requirementsText } : {}
  emitUpdate()
}

// 旧版 dict 默认保持原样；只有教师明确点击替换后才切换到新格式。
function replaceLegacyConstraints() {
  hasLegacyConstraints.value = false
  constraintsText.value = ''
  config.value.teacher_constraints = {}
  emitUpdate()
}

function addGroup() {
  if (!config.value.test_groups) config.value.test_groups = []
  config.value.test_groups.push({ id: '', name: '', dimension: 'F', max_score: 0, tests: '' })
  emitUpdate()
}

function removeGroup(i) {
  config.value.test_groups.splice(i, 1)
  emitUpdate()
}

function addCapRule() {
  if (!config.value.score_cap_rules) config.value.score_cap_rules = []
  config.value.score_cap_rules.push({ id: '', description: '', condition_code: 'off_topic', cap: 0 })
  emitUpdate()
}

function removeCapRule(i) {
  config.value.score_cap_rules.splice(i, 1)
  emitUpdate()
}

// 本地校验提示：shadow/active 后端强校验 F=60、R=10，草稿态提前提示
const validationMsg = computed(() => {
  const groups = config.value.test_groups || []
  if (!groups.length) return null
  const fSum = groups.filter(g => g.dimension === 'F').reduce((s, g) => s + (Number(g.max_score) || 0), 0)
  const rSum = groups.filter(g => g.dimension === 'R').reduce((s, g) => s + (Number(g.max_score) || 0), 0)
  const fOk = Math.abs(fSum - 60) < 1e-6
  const rOk = Math.abs(rSum - 10) < 1e-6
  if (fOk && rOk) return { ok: true, text: `✓ F 总计 ${fSum}/60, R 总计 ${rSum}/10` }
  return { ok: false, text: `⚠ F 总计 ${fSum}/60 (需=60), R 总计 ${rSum}/10 (需=10)` }
})
</script>

<style scoped>
.form-group { margin: 12px 0; }
.form-group label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 4px; color: var(--muted); }
.form-group .ds-select { width: 100%; padding: 6px 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); }
.section { margin: 16px 0; }
.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.section-header h5 { margin: 0; font-size: 13px; font-weight: 600; color: var(--muted); }
.section-header-actions { display: flex; gap: 6px; align-items: center; }
.btn-ai { border: 1px solid var(--info); color: var(--info); background: var(--surface); }
.btn-ai:disabled { opacity: 0.5; cursor: not-allowed; }
.group-card { border: 1px solid var(--border); border-radius: var(--radius-md); padding: 10px; margin: 8px 0; background: var(--surface); }
.group-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.cap-row { display: flex; gap: 6px; align-items: center; margin: 4px 0; }
.input-sm { padding: 4px 8px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); font-size: 12px; }
.input-num { width: 70px; }
.code-editor {
  width: 100%; background: var(--fg); color: var(--border); border: 1px solid oklch(0.32 0.02 155);
  border-radius: var(--radius-sm); padding: 8px; font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px; line-height: 1.5; resize: vertical; margin-top: 4px;
}
.hint { color: var(--faint); font-size: 12px; }
.hint-ok { color: var(--success); font-size: 12px; margin-top: 4px; }
.hint-err { color: var(--danger); font-size: 12px; margin-top: 4px; }
.field-help { margin: 0 0 6px; color: var(--muted); font-size: 12px; line-height: 1.5; }
.legacy-constraints {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  margin: 6px 0; padding: 8px 10px; border: 1px solid var(--warning); border-radius: var(--radius-md);
  background: var(--warning-bg); color: var(--warning); font-size: 12px; line-height: 1.5;
}
.character-count { margin-top: 2px; color: var(--faint); font-size: 11px; text-align: right; }
.btn-sm { padding: 4px 12px; font-size: 12px; border-radius: var(--radius-sm); cursor: pointer; border: 1px solid var(--border-strong); background: var(--surface); }
.btn-outline { border: 1px solid var(--accent); color: var(--accent); background: var(--surface); }
.btn-danger-text { border: none; background: none; color: var(--danger); cursor: pointer; }
.text-sm { font-size: 12px; }
.text-secondary { color: var(--faint); }
</style>
