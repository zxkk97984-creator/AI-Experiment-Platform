// StudioEditor：dirty 离开守卫（自定义确认弹窗）、返回按钮 backTo/router.back 两态
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import StudioEditor from '../StudioEditor.vue'

const routerState = vi.hoisted(() => ({
  push: vi.fn(),
  back: vi.fn(),
  leaveHook: null,
}))
const appState = vi.hoisted(() => ({ showToast: vi.fn() }))
const environmentsState = vi.hoisted(() => ({
  listAvailable: vi.fn().mockResolvedValue({ data: [] }),
}))

// 可变 store：测试中直接修改 dirty/conflict 等字段
const storeState = vi.hoisted(() => ({
  name: '测试模板',
  description: '',
  draftRevision: 1,
  dirty: false,
  saving: false,
  conflict: false,
  conflictMessage: '',
  studentPreview: false,
  sortedCells: [],
  cells: [],
  runningCellId: null,
  // Phase 4：环境状态（环境面板与发布确认使用）
  environmentVersionId: null,
  importPolicyMode: 'unrestricted',
  allowedImports: [],
  open: vi.fn().mockResolvedValue(),
  destroy: vi.fn(),
  saveDraft: vi.fn(),
  publish: vi.fn(),
  importExisting: vi.fn(),
  exportDraft: vi.fn(),
  exportVersion: vi.fn(),
  previewRun: vi.fn(),
  updateCellSource: vi.fn(),
  addCell: vi.fn(),
  moveCell: vi.fn(),
  moveCellTo: vi.fn(),
  duplicateCell: vi.fn(),
  deleteCell: vi.fn(),
  setCellEditable: vi.fn(),
  setCellHidden: vi.fn(),
  setEnvironment: vi.fn(),
  setImportPolicy: vi.fn(),
  // Q1：运行配置（发布表单）
  cellTimeoutSeconds: 120,
  kernelIdleSeconds: 900,
  setKernelConfig: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerState.push, back: routerState.back }),
  onBeforeRouteLeave: (hook) => {
    routerState.leaveHook = hook
  },
}))

vi.mock('../../../stores/studio.js', () => ({
  useStudioStore: () => storeState,
}))

vi.mock('../../../stores/app.js', () => ({
  useAppStore: () => appState,
}))

// Phase 4：环境 API mock——避免真实 client.js → router 依赖链
vi.mock('../../../api/environments.js', () => ({
  environmentsAPI: environmentsState,
}))

const CellStubs = {
  CodeCell: { name: 'CodeCell', template: '<div class="code-cell-stub" />' },
  MarkdownCell: { name: 'MarkdownCell', template: '<div class="md-cell-stub" />' },
}

const mountedWrappers = []

async function mountEditor(props = {}) {
  const wrapper = mount(StudioEditor, {
    props: { templateId: '5', ...props },
    global: { stubs: CellStubs },
  })
  await flushPromises()
  mountedWrappers.push(wrapper)
  return wrapper
}

describe('StudioEditor', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    routerState.leaveHook = null
    storeState.dirty = false
    storeState.conflict = false
    storeState.name = '测试模板'
    storeState.environmentVersionId = null
    storeState.open.mockResolvedValue()
    environmentsState.listAvailable.mockResolvedValue({ data: [] })
  })

  afterEach(() => {
    for (const wrapper of mountedWrappers.splice(0)) {
      wrapper.unmount()
    }
  })

  describe('未保存离开守卫', () => {
    it('dirty 时拦截并弹自定义确认框', async () => {
      storeState.dirty = true
      const wrapper = await mountEditor()
      const ret = routerState.leaveHook({}, {})
      expect(ret).toBeInstanceOf(Promise)
      await flushPromises()
      expect(wrapper.text()).toContain('有未保存的修改')
    })

    it('确认离开 → resolve(true) 并 destroy', async () => {
      storeState.dirty = true
      const wrapper = await mountEditor()
      const ret = routerState.leaveHook({}, {})
      await flushPromises()
      await wrapper.findAll('button').find((b) => b.text() === '离开').trigger('click')
      await flushPromises()
      expect(await ret).toBe(true)
      expect(storeState.destroy).toHaveBeenCalled()
    })

    it('取消离开 → resolve(false) 不 destroy', async () => {
      storeState.dirty = true
      const wrapper = await mountEditor()
      const ret = routerState.leaveHook({}, {})
      await flushPromises()
      await wrapper.findAll('button').find((b) => b.text() === '取消').trigger('click')
      await flushPromises()
      expect(await ret).toBe(false)
      expect(storeState.destroy).not.toHaveBeenCalled()
    })

    it('conflict 时不拦截，直接放行', async () => {
      storeState.dirty = true
      storeState.conflict = true
      await mountEditor()
      expect(routerState.leaveHook({}, {})).toBe(true)
    })

    it('不 dirty 直接放行并 destroy', async () => {
      await mountEditor()
      expect(routerState.leaveHook({}, {})).toBe(true)
      expect(storeState.destroy).toHaveBeenCalled()
    })

    it('确认离开后 beforeunload 放行（避免 router.back 双重确认）', async () => {
      storeState.dirty = true
      const wrapper = await mountEditor()
      const ret = routerState.leaveHook({}, {})
      await flushPromises()
      await wrapper.findAll('button').find((b) => b.text() === '离开').trigger('click')
      await flushPromises()
      expect(await ret).toBe(true)
      // confirmedLeave 置 true 后，beforeunload 不再 preventDefault
      const evt = new Event('beforeunload')
      const preventSpy = vi.spyOn(evt, 'preventDefault')
      window.dispatchEvent(evt)
      expect(preventSpy).not.toHaveBeenCalled()
    })
  })

  describe('返回按钮', () => {
    it('backTo 存在时 push 到目标路径', async () => {
      const wrapper = await mountEditor({ backTo: '/teacher/courses/1/manage' })
      await wrapper.findAll('button').find((b) => b.text().includes('返回')).trigger('click')
      expect(routerState.push).toHaveBeenCalledWith('/teacher/courses/1/manage')
      expect(routerState.back).not.toHaveBeenCalled()
    })

    it('无 backTo 且未修改时回退浏览器历史', async () => {
      const wrapper = await mountEditor()
      await wrapper.findAll('button').find((b) => b.text().includes('返回')).trigger('click')
      expect(routerState.back).toHaveBeenCalled()
      expect(routerState.push).not.toHaveBeenCalled()
    })

    it('无 backTo 且有修改时先弹自定义确认框，确认后 destroy 并 back', async () => {
      storeState.dirty = true
      const wrapper = await mountEditor()
      await wrapper.findAll('button').find((b) => b.text().includes('返回')).trigger('click')
      await flushPromises()
      // 弹确认框而非直接 back（router.back 不走路由守卫）
      expect(wrapper.text()).toContain('有未保存的修改')
      expect(routerState.back).not.toHaveBeenCalled()
      await wrapper.findAll('button').find((b) => b.text() === '离开').trigger('click')
      await flushPromises()
      expect(storeState.destroy).toHaveBeenCalled()
      expect(routerState.back).toHaveBeenCalled()
    })

    it('无 backTo 且有修改时取消不 back', async () => {
      storeState.dirty = true
      const wrapper = await mountEditor()
      await wrapper.findAll('button').find((b) => b.text().includes('返回')).trigger('click')
      await flushPromises()
      await wrapper.findAll('button').find((b) => b.text() === '取消').trigger('click')
      await flushPromises()
      expect(routerState.back).not.toHaveBeenCalled()
      expect(storeState.destroy).not.toHaveBeenCalled()
    })
  })

  describe('发布确认', () => {
    it('优先显示实验标题，并将环境与可用库作为辅助信息', async () => {
      storeState.name = '基础环境：排序与查找'
      storeState.environmentVersionId = 7
      environmentsState.listAvailable.mockResolvedValue({
        data: [{
          environment_version_id: 7,
          display_name: 'Python 基础',
          version_number: 1,
          packages: [{ pip_name: 'pytest' }],
        }],
      })

      const wrapper = await mountEditor()
      await wrapper.findAll('button').find((button) => button.text() === '发布').trigger('click')
      await flushPromises()

      expect(wrapper.text()).toContain('本次将发布：基础环境：排序与查找')
      expect(wrapper.text()).toContain('运行环境：Python 基础 v1')
      expect(wrapper.text()).toContain('可用库：pytest')
      expect(wrapper.text()).not.toContain('本次将发布：Python 基础 v1')
    })

    it('requires the run configuration inside the publish form and refuses out-of-range values', async () => {
      const wrapper = await mountEditor()
      await wrapper.findAll('button').find((button) => button.text() === '发布').trigger('click')
      await flushPromises()

      const timeoutInput = wrapper.get('input[aria-label="单格执行超时秒数"]')
      const idleInput = wrapper.get('input[aria-label="内核空闲回收分钟数"]')
      expect(timeoutInput.element.value).toBe('120')
      expect(idleInput.element.value).toBe('15')
      expect(wrapper.text()).toContain('发布后锁定')

      const modal = wrapper.get('.publish-modal')
      const modalPublish = () => modal.findAll('button').find((button) => button.text() === '发布')

      await timeoutInput.setValue('5')
      await flushPromises()
      expect(modalPublish().element.disabled).toBe(true)
      expect(wrapper.text()).toContain('单格执行超时需在 10–1800 秒之间')

      await timeoutInput.setValue('300')
      await idleInput.setValue('60')
      await flushPromises()
      expect(modalPublish().element.disabled).toBe(false)
      await modalPublish().trigger('click')
      await flushPromises()

      expect(storeState.setKernelConfig).toHaveBeenCalledWith({ cellTimeoutSeconds: 300, kernelIdleSeconds: 3600 })
      expect(storeState.publish).toHaveBeenCalled()
    })
  })

  describe('按当前位置新增 Cell', () => {
    const cells = [
      { id: 'c1', type: 'markdown', source: '', order: 0, student_editable: false, source_hidden: false },
      { id: 'c2', type: 'code', source: 'x = 1', order: 1, student_editable: true, source_hidden: false },
      { id: 'c3', type: 'markdown', source: '', order: 2, student_editable: false, source_hidden: false },
    ]

    beforeEach(() => {
      storeState.sortedCells = cells
    })

    afterEach(() => {
      storeState.sortedCells = []
    })

    it('在第二个 Cell 点击 +代码时将当前 Cell ID 作为插入锚点', async () => {
      const wrapper = await mountEditor()
      const secondCell = wrapper.get('[data-cell-id="c2"]')

      await secondCell.findAll('button').find((button) => button.text() === '+代码').trigger('click')

      expect(storeState.addCell).toHaveBeenCalledWith('code', 'c2')
    })

    it('在第二个 Cell 点击 +讲解时将当前 Cell ID 作为插入锚点', async () => {
      const wrapper = await mountEditor()
      const secondCell = wrapper.get('[data-cell-id="c2"]')

      await secondCell.findAll('button').find((button) => button.text() === '+讲解').trigger('click')

      expect(storeState.addCell).toHaveBeenCalledWith('markdown', 'c2')
    })
  })

  describe('拖拽排序', () => {
    const twoCells = [
      { id: 'c1', type: 'markdown', source: '', order: 0, student_editable: true, source_hidden: false },
      { id: 'c2', type: 'code', source: 'x = 1', order: 1, student_editable: true, source_hidden: false },
    ]
    // 自动滚动用例：4 个模块，静态位置 50/160/270/380（高 100，间距 110）
    const fourCells = [
      { id: 'c1', type: 'markdown', source: '', order: 0, student_editable: true, source_hidden: false },
      { id: 'c2', type: 'code', source: 'x = 1', order: 1, student_editable: true, source_hidden: false },
      { id: 'c3', type: 'markdown', source: '', order: 2, student_editable: true, source_hidden: false },
      { id: 'c4', type: 'code', source: 'x = 2', order: 3, student_editable: true, source_hidden: false },
    ]
    // 各模块静态位置（含间距）：c1 顶部 50 高 100（中心 100），c2 顶部 160 高 100（中心 210）
    const cellRects = {
      c1: { top: 50, height: 100 },
      c2: { top: 160, height: 100 },
    }
    const rectSpy = () =>
      vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
        const r = cellRects[this.dataset?.cellId] || { top: 0, height: 0 }
        return { top: r.top, height: r.height, bottom: r.top + r.height, left: 0, right: 200, width: 200, x: 0, y: r.top, toJSON: () => ({}) }
      })

    /**
     * mock 滚动容器（.studio-editor，jsdom 下 closest('.content') 为 null，
     * 由 closest('.studio-editor') 分支命中）的矩形与滚动属性。
     * 返回 scrollState，其 scrollTop 即容器当前滚动位置（动态 rect 用例据此联动）
     */
    const mockScroller = (wrapper, { top = 0, bottom = 800, clientHeight = 800, scrollHeight = 2000, initialScrollTop = 0 } = {}) => {
      const scroller = wrapper.get('.studio-editor').element
      const scrollState = { scrollTop: initialScrollTop }
      Object.defineProperty(scroller, 'getBoundingClientRect', {
        configurable: true,
        value: () => ({ top, bottom, height: bottom - top, left: 0, right: 200, width: 200, x: 0, y: top, toJSON: () => ({}) }),
      })
      Object.defineProperty(scroller, 'scrollTop', {
        configurable: true,
        get: () => scrollState.scrollTop,
        set: (v) => { scrollState.scrollTop = v },
      })
      Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => clientHeight })
      Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => scrollHeight })
      return scrollState
    }

    /** 等待一帧 rAF（落位后的 transform 清理在下一帧执行） */
    const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve))

    /**
     * 按下手柄：原生 dispatchEvent 构造 PointerEvent（vue-test-utils 的 trigger
     * 无法给 jsdom 只读的 clientY 赋值），返回 nextTick 以便断言响应式 class
     */
    const pressHandle = (handle, clientY = 60) => {
      handle.element.dispatchEvent(new PointerEvent('pointerdown', { pointerId: 1, clientY, bubbles: true }))
      return nextTick()
    }

    beforeEach(() => {
      storeState.sortedCells = twoCells
    })
    afterEach(() => {
      storeState.sortedCells = []
      vi.restoreAllMocks()
    })

    it('每个模块渲染三杠拖拽手柄', async () => {
      const wrapper = await mountEditor()
      expect(wrapper.findAll('.cell-drag-handle')).toHaveLength(2)
    })

    it('studentPreview 模式下不渲染手柄', async () => {
      storeState.studentPreview = true
      const wrapper = await mountEditor()
      expect(wrapper.findAll('.cell-drag-handle')).toHaveLength(0)
      storeState.studentPreview = false
    })

    it('按下立即进入预备态（手柄高亮提示可拖）', async () => {
      const wrapper = await mountEditor()
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      expect(handle.classes()).not.toContain('armed')
      await pressHandle(handle)
      expect(handle.classes()).toContain('armed')
      expect(wrapper.get('[data-cell-id="c1"]').classes()).toContain('armed')
      // 未越过阈值前不进入拖拽态
      expect(document.body.classList.contains('dragging-cells')).toBe(false)
    })

    it('短按（无移动）松手不触发拖拽', async () => {
      const wrapper = await mountEditor()
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1 }))
      await nextTick()
      expect(storeState.moveCellTo).not.toHaveBeenCalled()
      expect(document.body.classList.contains('dragging-cells')).toBe(false)
      expect(handle.classes()).not.toContain('armed')
    })

    it('微小移动（不足 5px）不激活', async () => {
      const wrapper = await mountEditor()
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 63 }))
      await nextTick()
      expect(document.body.classList.contains('dragging-cells')).toBe(false)
      expect(wrapper.get('[data-cell-id="c1"]').classes()).not.toContain('dragging')
    })

    it('移动超过阈值激活：模块跟随指针，其余模块实时让位', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      // 向下拖 400px：c1 视觉中心 500 越过 c2 中心 210 → 落位末尾，c2 上移让位
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 460 }))
      await nextTick()
      expect(document.body.classList.contains('dragging-cells')).toBe(true)
      const c1 = wrapper.get('[data-cell-id="c1"]')
      const c2 = wrapper.get('[data-cell-id="c2"]')
      expect(c1.classes()).toContain('dragging')
      expect(c1.element.style.transform).toBe('translate3d(0px, 400px, 0px)')
      // c2 上移一格：160 → 50，位移 -110
      expect(c2.element.style.transform).toBe('translate3d(0px, -110px, 0px)')
    })

    it('松手落位：调用 moveCellTo 并清除拖拽样式', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 460 }))
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1 }))
      expect(storeState.moveCellTo).toHaveBeenCalledWith('c1', 2)
      await flushPromises()
      await nextFrame()
      await nextTick()
      expect(wrapper.get('[data-cell-id="c1"]').classes()).not.toContain('dragging')
      expect(wrapper.get('[data-cell-id="c1"]').element.style.transform).toBe('')
      expect(wrapper.get('[data-cell-id="c2"]').element.style.transform).toBe('')
      expect(document.body.classList.contains('dragging-cells')).toBe(false)
    })

    it('向上拖：上方模块下移让位，落位到目标下标', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      const handle = wrapper.findAll('.cell-drag-handle')[1]
      await pressHandle(handle)
      // 向上拖 400px：c2 视觉中心 -190 越过 c1 中心 100 → 落位 0，c1 下移让位
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: -340 }))
      await nextTick()
      const c1 = wrapper.get('[data-cell-id="c1"]')
      expect(c1.classes()).not.toContain('dragging')
      expect(c1.element.style.transform).toBe('translate3d(0px, 110px, 0px)')
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1 }))
      expect(storeState.moveCellTo).toHaveBeenCalledWith('c2', 0)
    })

    it('首尾不越界：拖到最上方仍落位在 0', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      const handle = wrapper.findAll('.cell-drag-handle')[1]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: -10000 }))
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1 }))
      expect(storeState.moveCellTo).toHaveBeenCalledWith('c2', 0)
    })

    it('拖拽中按 Esc 取消：不落位、状态复位、transform 归位', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 460 }))
      expect(document.body.classList.contains('dragging-cells')).toBe(true)
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      await nextTick()
      expect(storeState.moveCellTo).not.toHaveBeenCalled()
      expect(document.body.classList.contains('dragging-cells')).toBe(false)
      expect(wrapper.get('[data-cell-id="c1"]').classes()).not.toContain('dragging')
      expect(wrapper.get('[data-cell-id="c1"]').element.style.transform).toBe('')
      expect(wrapper.get('[data-cell-id="c2"]').element.style.transform).toBe('')
    })

    // ── 拖拽自动滚动：指针压到滚动容器视口边缘时跟随滚动（见计划 notebook-drag-autoscroll.md）──

    it('拖到视口底部触发带：滚动容器向下滚动', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      // 可视区 0~800：底部触发带为 740~800
      const scrollState = mockScroller(wrapper, { bottom: 800 })
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 780 }))
      await nextFrame() // 等一帧 rAF：滚动发生在帧回调内
      expect(scrollState.scrollTop).toBeGreaterThan(0)
    })

    it('拖到视口顶部触发带：滚动容器向上滚动', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      // 顶部触发带为 0~60，内容已向下滚过 500px（scrollTop > 0 才可向上滚）
      const scrollState = mockScroller(wrapper, { bottom: 800, initialScrollTop: 500 })
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 20 }))
      await nextFrame()
      expect(scrollState.scrollTop).toBeLessThan(500)
    })

    it('已到滚动末尾时不再滚动', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      // clientHeight === scrollHeight：scrollTop + clientHeight >= scrollHeight，已无下滚空间
      const scrollState = mockScroller(wrapper, { bottom: 800, scrollHeight: 800 })
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 780 }))
      await nextFrame()
      expect(scrollState.scrollTop).toBe(0)
    })

    it('滚动发生后 dropIndex 重新校准：落位基于滚动后的静态位置', async () => {
      storeState.sortedCells = fourCells
      const wrapper = await mountEditor()
      // 4 个模块的视口坐标随滚动上移（真实浏览器滚动后的布局效果），可视区 0~300（触发带 240~300）
      const baseTops = { c1: 50, c2: 160, c3: 270, c4: 380 }
      const scrollState = mockScroller(wrapper, { bottom: 300, clientHeight: 300 })
      for (const id of Object.keys(baseTops)) {
        const el = wrapper.get(`[data-cell-id="${id}"]`).element
        Object.defineProperty(el, 'getBoundingClientRect', {
          configurable: true,
          value: () => {
            const top = baseTops[id] - scrollState.scrollTop
            return { top, height: 100, bottom: top + 100, left: 0, right: 200, width: 200, x: 0, y: top, toJSON: () => ({}) }
          },
        })
      }
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      // 拖到视口底部：越过 c2、c3 中心 → 目标应在 c4 前（下标 3）
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 290 }))
      await nextFrame() // 滚动一步，下方模块进入视野
      expect(scrollState.scrollTop).toBeGreaterThan(0)
      // 滚动后再移动：dropIndex 必须基于滚动后的坐标重算，松手落位仍正确
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 290 }))
      window.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1 }))
      expect(storeState.moveCellTo).toHaveBeenCalledWith('c1', 3)
    })

    it('armed 未激活阶段不触发滚动', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      const scrollState = mockScroller(wrapper, { bottom: 800 })
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      // 指针起点就在底部触发带内，但移动不足 5px 阈值，未激活
      await pressHandle(handle, 780)
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 782 }))
      await nextFrame()
      expect(scrollState.scrollTop).toBe(0)
    })

    // ── 缺陷修复回归（2026-08-05 用户反馈：自动滚动时被拖模块相对指针漂移）──
    // 根因：transform 纯指针位移未补偿滚动增量，容器滚动 N px 时模块随内容上移 N px，
    // 却仍用旧位移，导致模块向上漂移。修复后位移 = 指针位移 + scrollDelta

    it('自动滚动时被拖模块 transform 补偿滚动增量（不漂移）', async () => {
      const wrapper = await mountEditor()
      rectSpy()
      const scrollState = mockScroller(wrapper, { bottom: 800 })
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      // 拖到视口底部触发带并激活：指针位移 = 780 - 60 = 720
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 780 }))
      await nextFrame() // 滚动一步：初始 scrollTop 0 且单向向下滚，scrollDelta === scrollTop
      expect(scrollState.scrollTop).toBeGreaterThan(0)
      // 滚动后再次移动：位移必须 = 指针位移 + 滚动补偿，模块始终贴在指针下方
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 780 }))
      const c1 = wrapper.get('[data-cell-id="c1"]')
      expect(c1.element.style.transform).toBe(`translate3d(0px, ${720 + scrollState.scrollTop}px, 0px)`)
    })

    it('自动滚动时被拖模块视觉中心保持跟手（相对指针不漂移）', async () => {
      storeState.sortedCells = fourCells
      const wrapper = await mountEditor()
      // 4 个模块视口坐标随滚动上移，可视区 0~300（触发带 240~300）
      const baseTops = { c1: 50, c2: 160, c3: 270, c4: 380 }
      const scrollState = mockScroller(wrapper, { bottom: 300, clientHeight: 300 })
      for (const id of Object.keys(baseTops)) {
        const el = wrapper.get(`[data-cell-id="${id}"]`).element
        Object.defineProperty(el, 'getBoundingClientRect', {
          configurable: true,
          value: () => {
            const top = baseTops[id] - scrollState.scrollTop
            return { top, height: 100, bottom: top + 100, left: 0, right: 200, width: 200, x: 0, y: top, toJSON: () => ({}) }
          },
        })
      }
      const handle = wrapper.findAll('.cell-drag-handle')[0]
      await pressHandle(handle)
      // 拖到视口底部触发带：滚动前跟手位移 230px
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 290 }))
      const c1 = wrapper.get('[data-cell-id="c1"]')
      expect(c1.element.style.transform).toBe('translate3d(0px, 230px, 0px)')
      await nextFrame() // 滚动一步
      expect(scrollState.scrollTop).toBeGreaterThan(0)
      // 滚动后再次移动：位移 = 230 + scrollDelta，模块视口中心 = 50 - scrollDelta + (230 + scrollDelta) + 50 = 330 不变
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientY: 290 }))
      expect(c1.element.style.transform).toBe(`translate3d(0px, ${230 + scrollState.scrollTop}px, 0px)`)
    })
  })
})
