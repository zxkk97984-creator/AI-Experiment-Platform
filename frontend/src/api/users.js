import client from './client.js'

export const usersAPI = {
  resetPassword(id, data) { return client.post(`/users/${id}/password-reset`, data) },
  getMyPreferences() { return client.get('/users/me/preferences') },
  updateMyPreferences(data) { return client.patch('/users/me/preferences', data) },
  list(params) { return client.get('/users', { params }) },
  create(data) { return client.post('/users', data) },
  get(id) { return client.get(`/users/${id}`) },
  update(id, data) { return client.patch(`/users/${id}`, data) },
  updatePassword(id, data) { return client.patch(`/users/${id}/password`, data) },
  updateStatus(id, data) { return client.patch(`/users/${id}/status`, data) },
  listStudents(params) { return client.get('/users/students', { params }) },
}
