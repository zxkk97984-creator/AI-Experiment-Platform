import { beforeEach, describe, expect, it, vi } from 'vitest'
const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('../client.js', () => ({ default: client }))
import { courseMaterialsAPI as api } from '../courseMaterials.js'

describe('course material contracts', () => {
  beforeEach(() => vi.clearAllMocks())
  it('retains offering scope for listing and signed access', () => {
    api.list(3, { offering_id: 9, page: 1 }); api.access(4, { offering_id: 9, preview: true })
    expect(client.get).toHaveBeenCalledWith('/courses/3/materials', { params: { offering_id: 9, page: 1 } })
    expect(client.get).toHaveBeenCalledWith('/materials/4/access-url', { params: { offering_id: 9, preview: true } })
  })
  it('uploads versioned multipart with cancellation and progress', () => {
    const file = new File(['%PDF-'], 'lesson.pdf', { type: 'application/pdf' })
    const signal = new AbortController().signal, onUploadProgress = vi.fn()
    api.upload(4, file, 2, { signal, onUploadProgress })
    const [path, body, config] = client.put.mock.calls[0]
    expect(path).toBe('/materials/4/file'); expect(body.get('file')).toBe(file); expect(body.get('version')).toBe('2')
    expect(config).toMatchObject({ timeout: 600000, signal, onUploadProgress })
    expect(config.headers['Content-Type']).toBeUndefined()
  })
  it('versions removal and scopes activity mutation', () => {
    api.archive(4, 7); api.link(9, { chapter_id: 2, target_type: 'exam', target_id: 8 })
    expect(client.delete).toHaveBeenCalledWith('/materials/4', { params: { version: 7 } })
    expect(client.post).toHaveBeenCalledWith('/offerings/9/chapter-activities', { chapter_id: 2, target_type: 'exam', target_id: 8 })
  })
})
