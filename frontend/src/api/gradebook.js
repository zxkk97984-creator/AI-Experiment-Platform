import client from './client.js'

export const gradebookAPI = {
  offerings: () => client.get('/gradebooks/offerings'),
  read: (id) => client.get(`/gradebooks/offerings/${id}`),
  configure: (id, data) => client.put(`/gradebooks/offerings/${id}/config`, data),
  entries: (id, data) => client.patch(`/gradebooks/offerings/${id}/entries`, data),
  adjustment: (id, data) => client.post(`/gradebooks/offerings/${id}/adjustment`, data),
  publish: (id, data) => client.post(`/gradebooks/offerings/${id}/publish`, data),
  archive: (id, data) => client.post(`/gradebooks/offerings/${id}/archive`, data),
  snapshots: (id) => client.get(`/gradebooks/offerings/${id}/snapshots`),
  snapshot: (id) => client.get(`/gradebooks/snapshots/${id}`),
  export: (id, format = 'xlsx') => client.get(`/gradebooks/snapshots/${id}/export`, { params: { format }, responseType: 'blob' }),
}
