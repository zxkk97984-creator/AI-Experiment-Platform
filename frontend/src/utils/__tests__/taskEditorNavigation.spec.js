import { describe, expect, it } from 'vitest'
import { taskEditorReturn } from '../taskEditorNavigation.js'

describe('task editor return navigation', () => {
  it.each(['https://example.com', '/teacher/exams', ['materials'], undefined])('ignores arbitrary return sources: %j', from => {
    expect(taskEditorReturn({ role: 'admin', kind: 'exam', query: { from, return_to: 'https://example.com' } })).toEqual({
      destination: '/admin/offerings', label: '返回授课安排',
    })
  })

  it.each(['-1', '0', '1.5', '1e3', '9007199254740992', ['101'], '/admin/users'])('does not carry invalid query identifiers: %j', id => {
    expect(taskEditorReturn({ role: 'admin', kind: 'assignment', query: { from: 'materials', course_id: id, offering_id: id } }).destination).toEqual({
      path: '/admin/materials', query: {},
    })
  })

  it('fills only absent valid task identifiers from the query context', () => {
    expect(taskEditorReturn({ role: 'admin', kind: 'exam', task: { course_id: 101 }, query: {
      from: 'materials', course_id: '1', offering_id: '109',
    } }).destination).toEqual({ path: '/admin/materials', query: { course_id: '101', offering_id: '109' } })
  })
})
