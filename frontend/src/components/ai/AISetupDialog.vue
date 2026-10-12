<script setup>
// 一键 AI 判题对话框（批次 2，Q2 主路径）：
// 【AI 判题】→ 填写参考解法与评分侧重（可选是否生成 F/R 测试组）→ 异步生成
// → 分阶段进度 → 预览/微调 → 可选试判 → 保存并启用。
// 现有 AIQuestionConfig / AiConfigForm 高级入口保持不变，本对话框只是更快的入口。
import { reactive, ref } from 'vue'
import AppIcon from '../ui/AppIcon.vue'
import AISetupPanel from './AISetupPanel.vue'
import { useAppStore } from '../../stores/app.js'

const props = defineProps({
  kind: { type: String, required: true },
  questionId: { type: Number, required: true },
  defaultReferenceSolution: { type: String, default: '' },
  defaultConstraintsText: { type: String, default: '' },
  defaultGradingMode: { type: String, default: 'active' },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['enabled'])

const app = useAppStore()
const open = ref(false)
const started = ref(false)
const form = reactive({
  reference_solution: '',
  constraints_text: '',
  grading_mode: props.defaultGradingMode || 'active',
  generate_test_groups: true,
})

function openDialog() {
  form.reference_solution = props.defaultReferenceSolution || ''
  form.constraints_text = props.defaultConstraintsText || ''
  form.grading_mode = props.defaultGradingMode || 'active'
  form.generate_test_groups = true
  started.value = false
  open.value = true
}

function closeDialog() {
  open.value = false
  started.value = false
}

function beginGenerate() {
  if (!form.reference_solution.trim()) {
    app.showToast('请先填写参考解法，AI 需要它理解评分侧重', 'error')
    return
  }
  started.value = true
}

function onEnabled(payload) {
  app.showToast(`AI 判题配置已保存并启用（评分版本 v${payload.version}）`, 'success')
  emit('enabled', payload)
  closeDialog()
}
</script>

<template>
  <span class="ai-setup-entry">
    <button
      type="button"
      class="btn btn-sm btn-outline ai-setup-trigger"
      :disabled="disabled"
      @click="openDialog"
    >
      <AppIcon name="brain" :size="14" />AI 判题
    </button>

    <div v-if="open" class="ai-setup-mask" role="dialog" aria-modal="true" aria-label="一键 AI 判题">
      <div class="ai-setup-dialog">
        <header class="dialog-head">
          <div>
            <p class="section-eyebrow">一键 AI 判题</p>
            <h2>填写参考解法与评分侧重</h2>
          </div>
          <button type="button" class="btn btn-ghost btn-sm" aria-label="关闭" @click="closeDialog">关闭</button>
        </header>

        <template v-if="!started">
          <label class="field">
            <span>参考解法（必填）</span>
            <textarea
              v-model="form.reference_solution"
              class="input"
              rows="5"
              placeholder="粘贴一份可运行的参考实现；AI 按它生成评分标准与测试组"
            ></textarea>
          </label>
          <label class="field">
            <span>评分侧重（可选，多行填写）</span>
            <textarea
              v-model="form.constraints_text"
              class="input"
              rows="3"
              placeholder="例如：强调边界条件处理&#10;不允许使用递归"
            ></textarea>
          </label>
          <div class="dialog-row">
            <label class="check">
              <input v-model="form.generate_test_groups" type="checkbox" />
              <span>同时生成测试用例（F/R 测试组）</span>
            </label>
            <label class="field field-inline">
              <span>评分模式</span>
              <DsSelect v-model="form.grading_mode">
                <option value="active">正式评分（active）</option>
                <option value="shadow">影子评分（shadow）</option>
                <option value="legacy">传统评分（legacy）</option>
              </DsSelect>
            </label>
          </div>
          <footer class="dialog-actions">
            <button type="button" class="btn btn-secondary btn-sm" @click="closeDialog">取消</button>
            <button type="button" class="btn btn-primary btn-sm" :disabled="!form.reference_solution.trim()" @click="beginGenerate">开始生成</button>
          </footer>
        </template>

        <AISetupPanel
          v-else
          :kind="kind"
          :question-id="questionId"
          :reference-solution="form.reference_solution"
          :teacher-constraints="form.constraints_text.trim() ? { requirements_text: form.constraints_text.trim() } : {}"
          :grading-mode="form.grading_mode"
          :generate-test-groups="form.generate_test_groups"
          :auto-start="true"
          @enabled="onEnabled"
        />
      </div>
    </div>
  </span>
</template>

<style scoped>
.ai-setup-entry { display: inline-flex; }
.ai-setup-trigger { display: inline-flex; align-items: center; gap: var(--space-1); }
.ai-setup-mask { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: var(--space-4); background: rgba(15, 23, 42, 0.45); overflow: auto; }
.ai-setup-dialog { width: min(760px, 100%); max-height: 90vh; overflow: auto; display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-5); border-radius: var(--radius-lg); background: var(--surface); }
.dialog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
.dialog-head h2 { margin: 2px 0 0; font-size: var(--text-lg); }
.dialog-row { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; }
.check { display: inline-flex; align-items: center; gap: var(--space-2); color: var(--muted); font-size: var(--text-sm); }
.field { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-sm); color: var(--muted); }
.field-inline { min-width: 200px; }
.dialog-actions { display: flex; justify-content: flex-end; gap: var(--space-2); }
</style>
