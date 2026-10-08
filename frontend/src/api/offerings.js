import client from './client.js'

export const offeringsAPI = {
  list: params => client.get('/offerings', { params }),
  create: payload => client.post('/offerings', payload),
  update: (id, payload) => client.patch(`/offerings/${id}`, payload),
  students: id => client.get(`/offerings/${id}/students`),
  addStudents: (id, payload) => client.post(`/offerings/${id}/students`, payload),
  transfer: (id, studentId, payload) => client.post(`/offerings/${id}/students/${studentId}/transfer`, payload),
  owner: (id, payload) => client.post(`/courses/${id}/content-owner`, payload),
  templates: id => client.get(`/courses/${id}/teaching-templates`),
  copyTask: (id, payload) => client.post(`/offerings/${id}/tasks/copy`, payload),
  reviewHistory: (id, params) => client.get(`/offerings/${id}/legacy-review`, { params }),
  confirmRecord: (id, recordId, payload) => client.post(`/offerings/${id}/legacy-records/${recordId}/assign`, payload),
  confirmTask: (id, kind, taskId, payload) => client.post(`/offerings/${id}/legacy-tasks/${kind}/${taskId}/assign`, payload),
  transferRecord: (id, recordId, payload) => client.post(`/offerings/${id}/records/${recordId}/transfer`, payload),
  experiments: id => client.get(`/offerings/${id}/experiments`),
  release: (id, payload) => client.post(`/offerings/${id}/experiments`, payload),
}
