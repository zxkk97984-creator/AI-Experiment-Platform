import { test, expect } from '@playwright/test'
import { layoutFixture } from './fixtures/layout.js'

// Every read/write is intercepted. These fixtures never mutate the running DB.
async function scopedFixture(page, role, owner = false) {
  await layoutFixture(page, role)
  const state = {
    unavailable: false,
    rows: [{ id: 4, course_id: 1, chapter_id: 2, title: '人工智能章节讲义', description: '课堂示例与课后参考资料。',
      category: 'lecture', source: 'file', status: 'published', filename: '第一章.pdf', content_type: 'application/pdf',
      size_bytes: 1024, version: 2, previewable: true },
    { id: 5, course_id: 1, chapter_id: 2, title: 'Python 官网', category: 'tool', source: 'external', status: 'published',
      external_url: 'https://www.python.org/downloads/', version: 1 },
    { id: 6, course_id: 1, chapter_id: null, title: '教师未发布资料', category: 'notebook', source: 'file', status: 'draft',
      filename: '草稿.ipynb', content_type: 'application/x-ipynb+json', size_bytes: 100, version: 1 }],
    writes: [], accesses: [], ordinaryLimit: 104857600, largeLimit: 524288000,
  }
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/v1', '')
    const reply = json => route.fulfill({ json })
    if (path === '/courses/1/chapters') return reply({ items: [{id:2,title:'第一章 人工智能基础',lessons:[]}] })
    if (path === '/offerings') return reply({items:[{id:9,course_id:1,class_name:'一班',term_name:'秋季学期',status:'active'}]})
    if (path === '/offerings/9/chapter-activities') return reply([{id:8,chapter_id:2,target_type:'assignment',target_id:10,
      title:'本班课后练习',status:'published',route:`/${role}/assignments${role === 'student' ? '/10' : ''}?offering_id=9`}])
    if (path === '/offerings/9/chapter-activity-options') return reply([{target_type:'assignment',target_id:10,title:'本班课后练习',status:'published'}])
    if (path === '/courses/1/materials' && request.method() === 'GET') {
      if (state.unavailable) return route.fulfill({status:503,json:{detail:{message:'资料服务暂时不可用'}}})
      const rows = state.rows.filter(row => role !== 'student' || row.status === 'published')
      return reply({items:rows,total:rows.length,page:1,page_size:50,can_edit:owner,ordinary_max_bytes:state.ordinaryLimit,large_max_bytes:state.largeLimit})
    }
    if (path === '/courses/1/materials' && request.method() === 'POST') {
      const body = request.postDataJSON(); state.writes.push({path,body})
      const row = { ...body, id:12, course_id:1, status:'draft',version:1 }
      state.rows.push(row); return reply(row)
    }
    if (path === '/materials/12/file') {
      state.writes.push({path,multipart:request.postData().includes('filename="新讲义.pdf"')})
      const row = state.rows.find(row => row.id === 12)
      Object.assign(row,{filename:'新讲义.pdf',content_type:'application/pdf',size_bytes:100,previewable:true,version:2})
      return reply(row)
    }
    if (path === '/materials/12' && request.method() === 'PATCH') {
      const body = request.postDataJSON(); state.writes.push({path,body})
      const row = state.rows.find(row => row.id === 12); Object.assign(row,body,{version:row.version+1}); return reply(row)
    }
    if (/^\/materials\/\d+\/access-url$/.test(path)) {
      state.accesses.push({path,offering_id:url.searchParams.get('offering_id'),preview:url.searchParams.get('preview')})
      return reply({url:'/api/v1/media/course-materials/4?sig=fixture',previewable:true,external:false})
    }
    if (path === '/media/course-materials/4') return route.fulfill({status:200,contentType:'application/pdf',body:'%PDF-1.4\n%%EOF'})
    return route.fallback()
  })
  return state
}

async function checkLayout(page) {
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  const box = await page.locator('.materials-page').boundingBox()
  const sizes = await page.evaluate(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth}))
  expect(sizes.scroll).toBeLessThanOrEqual(sizes.width + 1)
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(sizes.width + 1)
}

for (const width of [320, 768, 1440]) {
  test(`学生查看章节资料、预览与本班活动 ${width}`, async ({page}) => {
    const errors = []; page.on('pageerror', error => errors.push(error.message))
    await page.setViewportSize({width,height:1000})
    const state = await scopedFixture(page,'student')
    await page.goto('/student/materials')
    await expect(page.getByRole('heading',{name:'课程资料',exact:true})).toBeVisible()
    await expect(page.getByText('人工智能章节讲义', {exact:true})).toBeVisible()
    await expect(page.getByText('教师未发布资料')).toHaveCount(0)
    await expect(page.getByRole('button',{name:'添加资料'})).toHaveCount(0)
    const official = page.getByRole('link',{name:'打开官网'})
    await expect(official).toHaveAttribute('href','https://www.python.org/downloads/')
    await expect(official).toHaveAttribute('rel','noopener noreferrer')
    await page.getByRole('button',{name:'预览',exact:true}).click()
    await expect(page.locator('iframe[title="人工智能章节讲义 PDF 预览"]')).toBeVisible()
    expect(state.accesses[0]).toMatchObject({offering_id:'9',preview:'true'})
    await page.getByRole('button',{name:'关闭预览'}).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await checkLayout(page)
    await page.screenshot({path:`/tmp/school-materials-student-${width}.png`,fullPage:true,animations:'disabled'})
    expect(errors).toEqual([])
    expect(state.writes).toEqual([])
  })
}

test('授课教师只读共享资料并选择本班活动', async ({page}) => {
  await scopedFixture(page,'teacher')
  await page.goto('/teacher/materials')
  await expect(page.getByText('共享资料由课程负责人维护；你可以管理自己授课班级的活动关联。')).toBeVisible()
  await expect(page.getByRole('button',{name:'添加资料'})).toHaveCount(0)
  await expect(page.locator('.link-form')).toContainText('本班课后练习')
  await expect(page.locator('.link-form input[type="number"]')).toHaveCount(0)
  await checkLayout(page)
})

test('资料上传展示服务配置并在提交前拦截超限文件', async ({page}) => {
  const state = await scopedFixture(page, 'teacher', true)
  state.ordinaryLimit = 1024; state.largeLimit = 2048
  await page.goto('/teacher/materials')
  await page.getByRole('button', { name: '添加资料', exact: true }).click()
  const editor = page.getByRole('dialog'), picker = editor.locator('input[type="file"]')
  await editor.getByLabel('资料标题').fill('配置边界讲义')
  await expect(editor).toContainText('IPYNB 最大 1 KiB')
  await expect(editor).toContainText('视频、ZIP 最大 2 KiB')
  await picker.setInputFiles({ name: 'oversized.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(1025) })
  await expect(editor.getByRole('alert')).toContainText('超过 1 KiB')
  await expect(editor.getByRole('button', { name: '保存资料', exact: true })).toBeDisabled()
  expect(state.writes).toEqual([])
  await picker.setInputFiles({ name: 'resource.ZIP', mimeType: 'application/zip', buffer: Buffer.alloc(2048) })
  await expect(editor.getByRole('alert')).toHaveCount(0)
  await expect(editor.getByRole('button', { name: '保存资料', exact: true })).toBeEnabled()
  await picker.setInputFiles({ name: 'exact.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(1024) })
  await expect(editor.getByRole('alert')).toHaveCount(0)
  await expect(editor.getByRole('button', { name: '保存资料', exact: true })).toBeEnabled()
  await editor.getByRole('button', { name: '取消', exact: true }).click()
  expect(state.writes).toEqual([])
})

test('课程负责人保存文件后发布，键盘关闭编辑弹窗', async ({page}) => {
  const state = await scopedFixture(page,'teacher',true)
  await page.goto('/teacher/materials')
  await page.getByRole('button',{name:'添加资料'}).click()
  const dialog = page.getByRole('dialog',{name:'添加资料'})
  await dialog.getByLabel('资料标题').fill('新讲义')
  await dialog.getByLabel('所属章节').selectOption('2')
  await dialog.getByLabel('资料文件').setInputFiles({name:'新讲义.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.7\nfixture')})
  await dialog.getByLabel('发布状态').selectOption('published')
  await dialog.getByRole('button',{name:'保存资料'}).click()
  await expect(page.getByRole('heading',{name:'新讲义',exact:true})).toBeVisible()
  expect(state.writes.map(row => row.path)).toEqual(['/courses/1/materials','/materials/12/file','/materials/12'])
  expect(state.writes[1].multipart).toBe(true)
  expect(state.writes[2].body).toEqual({version:2,status:'published'})
  await page.getByRole('button',{name:'添加资料'}).click()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('资料加载失败呈现重试并恢复', async ({page}) => {
  const state = await scopedFixture(page,'student'); state.unavailable = true
  await page.goto('/student/materials')
  await expect(page.getByRole('alert')).toContainText('资料服务暂时不可用')
  state.unavailable = false
  await page.getByRole('button',{name:'重试'}).click()
  await expect(page.getByText('人工智能章节讲义',{exact:true})).toBeVisible()
})
