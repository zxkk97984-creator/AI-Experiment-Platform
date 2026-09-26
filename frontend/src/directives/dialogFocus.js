// Local directive: contain keyboard focus and restore the invoking control.
// Escape/close/busy behavior remains owned by each dialog.
const bindings = new WeakMap()
const candidates = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
const focusable = el => [...el.querySelectorAll(candidates)].filter(node => node.getClientRects().length && !node.closest('[inert]'))

export const vDialogFocus = {
  mounted(el) {
    const previous = document.activeElement
    el.tabIndex = -1
    const keydown = event => {
      if (event.key !== 'Tab' || !el.contains(document.activeElement)) return
      const nodes = focusable(el)
      const first = nodes[0] || el
      const last = nodes.at(-1) || el
      if (!nodes.length || (event.shiftKey && [first, el].includes(document.activeElement))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && [last, el].includes(document.activeElement)) {
        event.preventDefault()
        first.focus()
      }
    }
    el.addEventListener('keydown', keydown)
    bindings.set(el, { previous, keydown })
    queueMicrotask(() => { if (el.isConnected) (focusable(el)[0] || el).focus() })
  },
  unmounted(el) {
    const binding = bindings.get(el)
    if (!binding) return
    el.removeEventListener('keydown', binding.keydown)
    if (binding.previous?.isConnected) binding.previous.focus()
    bindings.delete(el)
  },
}
