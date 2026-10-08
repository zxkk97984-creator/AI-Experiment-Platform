export function positiveRouteId(value) {
  if (typeof value !== 'string' && typeof value !== 'number') return undefined
  if (!/^\d+$/.test(String(value))) return undefined
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? String(id) : undefined
}

export function taskEditorReturn({ role, kind, task, query = {} }) {
  if (role !== 'admin') {
    return { destination: `/teacher/${kind}s`, label: kind === 'exam' ? '返回考试列表' : '取消' }
  }
  if (query.from !== 'materials') {
    return { destination: '/admin/offerings', label: '返回授课安排' }
  }
  const context = {}
  for (const key of ['course_id', 'offering_id']) {
    const id = positiveRouteId(task?.[key]) || positiveRouteId(query[key])
    if (id) context[key] = id
  }
  return { destination: { path: '/admin/materials', query: context }, label: '返回课程资料' }
}
