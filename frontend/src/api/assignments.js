import client from './client.js'

export const assignmentsAPI = {
  list(params) { return client.get('/assignments', { params }) },
  create(data) { return client.post('/assignments', data) },
  get(id) { return client.get(`/assignments/${id}`) },
  update(id, data) { return client.patch(`/assignments/${id}`, data) },
  publish(id, data) { return client.post(`/assignments/${id}/publish`, data) },
  deleteAssignment(id) { return client.delete(`/assignments/${id}`) },
  unpublishAssignment(id) { return client.post(`/assignments/${id}/unpublish`) },
  myProgress(id) { return client.get(`/assignments/${id}/my-progress`) },
  getQuestions(assignmentId) { return client.get(`/assignments/${assignmentId}/questions`) },
  importAudienceStudents(assignmentId, kind, file) {
    const formData = new FormData()
    formData.append('file', file)
    return client.post(`/assignments/${assignmentId}/audience/import`, formData, { params: { kind } })
  },
  createQuestion(assignmentId, data) { return client.post(`/assignments/${assignmentId}/questions`, data) },
  // Phase 4：题目编辑（环境覆盖/import 策略在 QuestionEditView 使用）
  updateQuestion(assignmentId, questionId, data) { return client.patch(`/assignments/${assignmentId}/questions/${questionId}`, data) },
  deleteQuestion(assignmentId, questionId) { return client.delete(`/assignments/${assignmentId}/questions/${questionId}`) },
}
