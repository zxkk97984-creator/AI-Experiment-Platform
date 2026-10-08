import client from './client.js'

export const courseMaterialsAPI = {
  list(courseId, params) { return client.get(`/courses/${courseId}/materials`, { params }) },
  chapters(courseId, params) { return client.get(`/courses/${courseId}/chapters`, { params }) },
  create(courseId, data) { return client.post(`/courses/${courseId}/materials`, data) },
  update(id, data) { return client.patch(`/materials/${id}`, data) },
  archive(id, version) { return client.delete(`/materials/${id}`, { params: { version } }) },
  upload(id, file, version, { onUploadProgress, signal } = {}) {
    const form = new FormData()
    form.append('file', file)
    form.append('version', String(version))
    return client.put(`/materials/${id}/file`, form, {
      headers: { 'Content-Type': undefined }, timeout: 600000, onUploadProgress, signal,
    })
  },
  access(id, params) { return client.get(`/materials/${id}/access-url`, { params }) },
  offerings(courseId) { return client.get('/offerings', { params: { course_id: courseId } }) },
  activities(offeringId) { return client.get(`/offerings/${offeringId}/chapter-activities`) },
  activityOptions(offeringId) { return client.get(`/offerings/${offeringId}/chapter-activity-options`) },
  link(offeringId, data) { return client.post(`/offerings/${offeringId}/chapter-activities`, data) },
  unlink(id) { return client.delete(`/chapter-activities/${id}`) },
}
