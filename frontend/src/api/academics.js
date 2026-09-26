import client from './client.js'

export const academicsAPI = {
  reopenTerm(id, data) { return client.post(`/academic-terms/${id}/reopen`, data) },
  restoreClass(id, data) { return client.post(`/teaching-classes/${id}/restore`, data) },
  transferStudents(id, data) { return client.post(`/teaching-classes/${id}/transfer`, data) },
  listTerms(params) { return client.get('/academic-terms', { params }) },
  createTerm(data) { return client.post('/academic-terms', data) },
  updateTerm(id, data) { return client.patch(`/academic-terms/${id}`, data) },
  closeTerm(id) { return client.delete(`/academic-terms/${id}`) },
  listClasses(params) { return client.get('/teaching-classes', { params }) },
  createClass(data) { return client.post('/teaching-classes', data) },
  updateClass(id, data) { return client.patch(`/teaching-classes/${id}`, data) },
  archiveClass(id) { return client.delete(`/teaching-classes/${id}`) },
  listClassStudents(id, params) { return client.get(`/teaching-classes/${id}/students`, { params }) },
  addClassStudents(id, studentIds) { return client.post(`/teaching-classes/${id}/students`, { student_ids: studentIds }) },
  removeClassStudent(id, studentId) { return client.delete(`/teaching-classes/${id}/students/${studentId}`) },
}
