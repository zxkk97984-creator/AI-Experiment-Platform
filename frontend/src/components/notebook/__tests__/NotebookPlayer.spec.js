import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import NotebookPlayer from '../NotebookPlayer.vue'
import { useExperimentStore } from '../../../stores/experiment.js'

const routerState = vi.hoisted(() => ({
  push: vi.fn(),
  leaveHook: null,
  updateHook: null,
  route: { query: {} },
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerState.push, currentRoute: { value: routerState.route } }),
  onBeforeRouteLeave: (hook) => {
    routerState.leaveHook = hook
  },
  onBeforeRouteUpdate: (hook) => {
    routerState.updateHook = hook
  },
}))

vi.mock('../../../api/experiments.js', () => ({
  experimentsAPI: {
    ensureForLesson: vi.fn(),
    ensureForModule: vi.fn(),
    getRecordDetail: vi.fn(),
    saveCells: vi.fn(),
    executeCell: vi.fn(),
    getExecution: vi.fn(),
    getActiveExecution: vi.fn(),
    cancelExecution: vi.fn(),
    interrupt: vi.fn(),
    restart: vi.fn(),
  },
}))

import { experimentsAPI } from '../../../api/experiments.js'

const CodeCellStub = defineComponent({
  name: 'CodeCell',
  props: {
    cell: { type: Object, required: true },
    readonly: { type: Boolean, default: false },
  },
  emits: ['execute', 'update:source'],
  template: '<div class="code-cell-stub" :data-id="cell.id" :data-readonly="String(readonly)" />',
})

const MarkdownCellStub = defineComponent({
  name: 'MarkdownCell',
  props: { cell: { type: Object, required: true } },
  template: '<div class="markdown-cell-stub" :data-id="cell.id" />',
})

function detailResponse(overrides = {}) {
  return {
    data: {
      id: 101,
      record_revision: 2,
      entry_name: '统一 Player',
      entry_description: '课程与独立实验共用',
      cells: [
        {
          id: 'intro',
          type: 'markdown',
          source: '# Intro',
          order: 0,
          student_editable: false,
        },
        {
          id: 'seed',
          type: 'code',
          source: 'seed = 1',
          order: 1,
          student_editable: false,
        },
        {
          id: 'work',
          type: 'code',
          source: 'answer = 0',
          order: 2,
          student_editable: true,
        },
      ],
      ...overrides,
    },
  }
}

async function mountPlayer(props = { mode: 'module', entryId: 7 }) {
  const wrapper = mount(NotebookPlayer, {
    props,
    global: {
      plugins: [createPinia()],
      stubs: {
        CodeCell: CodeCellStub,
        MarkdownCell: MarkdownCellStub,
      },
    },
  })
  await flushPromises()
  return wrapper
}

describe('NotebookPlayer', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    routerState.leaveHook = null
    routerState.updateHook = null
    routerState.route = { query: {} }
    setActivePinia(createPinia())
    experimentsAPI.ensureForLesson.mockResolvedValue({
      data: { id: 101, record_revision: 2 },
    })
    experimentsAPI.ensureForModule.mockResolvedValue({
      data: { id: 101, record_revision: 2 },
    })
    experimentsAPI.getRecordDetail.mockResolvedValue(detailResponse())
  })

  it.each([
    [{ mode: 'lesson', entryId: 11, courseId: 3 }, 'ensureForLesson', 11],
    [{ mode: 'module', entryId: 7 }, 'ensureForModule', 7],
  ])('loads both student routes through the shared player', async (props, method, id) => {
    const wrapper = await mountPlayer(props)

    expect(experimentsAPI[method]).toHaveBeenCalledWith(id)
    expect(wrapper.get('.player-title').text()).toBe('统一 Player')
    expect(wrapper.findAll('.code-cell-stub')).toHaveLength(2)
  })

  it('renders fixed notebook structure and passes readonly only to locked cells', async () => {
    const wrapper = await mountPlayer()
    const codeCells = wrapper.findAll('.code-cell-stub')

    expect(codeCells[0].attributes('data-readonly')).toBe('true')
    expect(codeCells[1].attributes('data-readonly')).toBe('false')
    expect(wrapper.text()).not.toMatch(/添加 Cell|删除 Cell|复制 Cell|上移|下移|排序/)
  })

  it('keeps advanced Kernel actions in the secondary menu', async () => {
    const wrapper = await mountPlayer()

    expect(wrapper.text()).not.toContain('重启 Kernel')
    await wrapper.get('[title="更多操作"]').trigger('click')
    expect(wrapper.text()).toContain('全部运行')
    expect(wrapper.text()).toContain('中断运行')
    expect(wrapper.text()).toContain('重启 Kernel')
  })

  it('shows the running cell status and cancels the active execution from the menu', async () => {
    const wrapper = await mountPlayer()
    const store = useExperimentStore()
    experimentsAPI.cancelExecution.mockResolvedValue({ data: { id: 5, status: 'cancelled' } })
    store.cells[0].isRunning = true
    store.executingCellId = 'intro'
    store.activeExecutionId = 5
    await wrapper.vm.$nextTick()

    expect(wrapper.get('.player-exec-alert.running').text()).toContain('正在运行单元格')
    await wrapper.get('[title="更多操作"]').trigger('click')
    await wrapper.findAll('.menu-item').find((item) => item.text().includes('中断运行')).trigger('click')
    expect(experimentsAPI.cancelExecution).toHaveBeenCalledWith(101, 5)
  })

  it('surfaces state-lost as an explicit alert instead of a silent failure', async () => {
    const wrapper = await mountPlayer()
    const store = useExperimentStore()
    store.kernelStateLost = true
    await wrapper.vm.$nextTick()

    expect(wrapper.get('.player-exec-alert.error').text()).toContain('内核状态已丢失')
    expect(wrapper.get('.player-exec-alert.error').text()).toContain('从头重新运行')
  })

  it('blocks route leave when the store cannot safely save', async () => {
    const wrapper = await mountPlayer()
    const store = useExperimentStore()
    vi.spyOn(store, 'canNavigate').mockResolvedValue(false)

    await expect(routerState.leaveHook({}, {})).resolves.toBe(false)
    expect(store.canNavigate).toHaveBeenCalledOnce()

    wrapper.unmount()
  })

  it('allows route leave after a successful flush', async () => {
    const wrapper = await mountPlayer()
    const store = useExperimentStore()
    vi.spyOn(store, 'canNavigate').mockResolvedValue(true)

    await expect(routerState.leaveHook({}, {})).resolves.toBe(true)

    wrapper.unmount()
  })

  it('does not replace the record when an in-place route update cannot save', async () => {
    const wrapper = await mountPlayer()
    const store = useExperimentStore()
    vi.spyOn(store, 'canNavigate').mockResolvedValue(false)
    experimentsAPI.ensureForModule.mockClear()

    await expect(routerState.updateHook({ params: { id: '8' } }, {})).resolves.toBe(false)
    expect(experimentsAPI.ensureForModule).not.toHaveBeenCalled()

    wrapper.unmount()
  })
  it('keeps the offering query on initial load and on an in-place route change', async () => {
    routerState.route = { query: { offering_id: '11' } }
    const wrapper = await mountPlayer()
    expect(experimentsAPI.ensureForModule).toHaveBeenCalledWith(7, 11)
    const store = useExperimentStore()
    vi.spyOn(store, 'canNavigate').mockResolvedValue(true)
    await routerState.updateHook({ params: { id: 8 }, query: { offering_id: '12' } }, {})
    expect(experimentsAPI.ensureForModule).toHaveBeenCalledWith(8, 12)
    wrapper.unmount()
  })
  it('shows environment hint under the title (Phase 5)', async () => {
    experimentsAPI.getRecordDetail.mockResolvedValue(detailResponse({
      environment_summary: {
        display_name: '数据分析',
        version_label: 'v1',
        python_version: '3.12',
        imports: ['numpy', 'pandas', 'sklearn'],
        import_policy_mode: 'restricted',
        allowed_imports: ['numpy'],
      },
    }))
    const wrapper = await mountPlayer()
    const env = wrapper.find('.player-env')
    expect(env.exists()).toBe(true)
    expect(env.text()).toContain('数据分析')
    expect(env.text()).toContain('v1')
    expect(env.text()).toContain('numpy · pandas · sklearn')
    expect(env.text()).toContain('允许导入：numpy')
    wrapper.unmount()
  })

  it('hides environment hint when no summary (Phase 5)', async () => {
    const wrapper = await mountPlayer()
    expect(wrapper.find('.player-env').exists()).toBe(false)
    wrapper.unmount()
  })
})
