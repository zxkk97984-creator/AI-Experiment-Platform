// 批次 5 契约：练习域端点的路径、查询参数与请求体
import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }))

vi.mock('../client.js', () => ({ default: client }))

import { adminPracticeAPI, practiceAPI } from '../practice.js'

describe('practiceAPI', () => {
  beforeEach(() => vi.resetAllMocks())

  it('题库列表把筛选条件作为查询参数透传', () => {
    practiceAPI.listProblems({ difficulty: 'hard', tag: 'dp', completion: 'solved', search: '背包', page: 2, page_size: 12 })
    const [path, config] = client.get.mock.calls[0]
    expect(path).toBe('/practice/problems')
    expect(config.params).toMatchObject({ difficulty: 'hard', tag: 'dp', completion: 'solved', search: '背包', page: 2, page_size: 12 })
  })

  it('提交作答携带 client_request_id 以保证重复点击幂等', () => {
    practiceAPI.submit(7, 'print(1)', 'req-1')
    expect(client.post.mock.calls[0]).toEqual([
      '/practice/problems/7/submissions',
      { code: 'print(1)', client_request_id: 'req-1' },
    ])
  })

  it('提交详情与我的练习走学生端点', () => {
    practiceAPI.getSubmission(11)
    practiceAPI.getMine({ page: 1, page_size: 10 })
    expect(client.get.mock.calls[0][0]).toBe('/practice/submissions/11')
    expect(client.get.mock.calls[1][0]).toBe('/practice/me')
  })

  it('排行榜使用 scope 与 board 参数（普通/AI 分榜）', () => {
    practiceAPI.leaderboard({ scope: 'offering', scope_id: 3, board: 'ai' })
    const [path, config] = client.get.mock.calls[0]
    expect(path).toBe('/practice/leaderboard')
    expect(config.params).toEqual({ scope: 'offering', scope_id: 3, board: 'ai' })
  })
})

describe('adminPracticeAPI', () => {
  beforeEach(() => vi.resetAllMocks())

  it('题目 CRUD 与发布 / 归档 / 删除端点', () => {
    adminPracticeAPI.listProblems({ status: 'draft' })
    adminPracticeAPI.getProblem(4)
    adminPracticeAPI.createProblem({ title: '新题' })
    adminPracticeAPI.updateProblem(4, { title: '改名' })
    adminPracticeAPI.publishProblem(4)
    adminPracticeAPI.archiveProblem(4)
    adminPracticeAPI.deleteProblem(4)
    expect(client.get.mock.calls[0][0]).toBe('/admin/practice/problems')
    expect(client.get.mock.calls[1][0]).toBe('/admin/practice/problems/4')
    expect(client.post.mock.calls[0]).toEqual(['/admin/practice/problems', { title: '新题' }])
    expect(client.patch.mock.calls[0]).toEqual(['/admin/practice/problems/4', { title: '改名' }])
    expect(client.post.mock.calls[1][0]).toBe('/admin/practice/problems/4/publish')
    expect(client.post.mock.calls[2][0]).toBe('/admin/practice/problems/4/archive')
    expect(client.delete.mock.calls[0][0]).toBe('/admin/practice/problems/4')
  })

  it('从作业复制题目走集合级端点并新建草稿', () => {
    adminPracticeAPI.copyFromAssignment({ assignment_id: 2, question_id: 9, title: '复制题' })
    expect(client.post.mock.calls[0]).toEqual([
      '/admin/practice/problems/copy-from-assignment',
      { assignment_id: 2, question_id: 9, title: '复制题' },
    ])
  })

  it('设置 / 预检 / 用量 / 复核端点', () => {
    adminPracticeAPI.getSettings()
    adminPracticeAPI.updateSettings({ grading_mode: 'ai' })
    adminPracticeAPI.getAiPreflight()
    adminPracticeAPI.getUsage({ date: '2026-10-13' })
    adminPracticeAPI.listAppeals({ status: 'pending' })
    adminPracticeAPI.handleAppeal(5, { action: 'adjust', comment: '依据充分', score: 88 })
    expect(client.get.mock.calls.map((call) => call[0])).toEqual([
      '/admin/practice/settings',
      '/admin/practice/ai-preflight',
      '/admin/practice/usage',
      '/admin/practice/appeals',
    ])
    expect(client.patch.mock.calls[0]).toEqual(['/admin/practice/settings', { grading_mode: 'ai' }])
    expect(client.post.mock.calls[0]).toEqual([
      '/admin/practice/appeals/5/handle',
      { action: 'adjust', comment: '依据充分', score: 88 },
    ])
  })
})
