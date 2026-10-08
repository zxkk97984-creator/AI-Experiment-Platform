# 成绩簿与AI回归补充验证

日期：2026-10-03。成绩簿浏览器验证使用 `http://127.0.0.1:5175` 的真实Vite页面和Chromium；所有 `/api/v1` 读写由Playwright本地fixture响应，未访问或修改运行中的业务数据库。

## 结果与边界

| 检查 | 结果 |
|---|---|
| AI旧回归两文件 | 22 passed；只在5个setup尾部显式补管理员确认的授课fixture |
| 成绩簿交互E2E | 7 passed；teacher/admin/student各390px与1440px，另含来源409重审 |
| 成绩簿Vue组件 | 5 passed |
| 新E2E与成绩簿页面ESLint | 通过 |
| AI回归文件Ruff | 通过 |
| 页面身份、非空、Vite错误覆盖层 | 全部通过 |
| 页面运行错误/相关console warning | 未发现；fixture主动中止字体CDN，排除相应网络错误 |
| 页面宽度 | 390px与1440px的document/body均无横向溢出；成绩分项表格内部可以横滑 |

保留原AI断言：最终分覆盖保留原始总分、考试折算分按题目分值重新计算、shadow不向学生泄露AI评分、AI默认关闭/零外呼与关闭时legacy路径可发布。没有放宽生产授权或增加全局自动授课hook。

Browser插件尝试IAB返回不可用，浏览器库存读取超时；本次按照项目已有Playwright工作流执行。没有新增浏览器依赖。页面交互与截图是fixture UI证据，不代表真实API鉴权、MySQL并发、实验执行、生产容量或真实手机/Safari验收。真实隔离API与数据库恢复由主整合验证单独记录。

## 已验证交互

- 教师与管理员：30/40/30两级配置保存 → 待评分阻止发布 → 为选定学生录入80/90/70 → 计算81分 → 发布 → 归档81分 → 填写原因调整为88分 → 重新发布 → 旧归档仍为81分。
- 历史版本视图只读，隐藏配置、录入、调分与发布入口，返回当前草稿后可继续处理当前成绩。
- 学生：只看到本人官方分项与总分、没有配置或调分入口；CSV实际下载只有本人一行，未包含另一学生资料。
- 来源版本变化409：保留发布原因、没有创建官方版本；刷新重新审核后才能发布。
- 手机视图直接显示本人最终成绩和计算总分；较多分项可在表格内横滑。

## 复现

```bash
cd /home/zxk/Projects/dai-experiment-platform/backend
env -u DAI_DATABASE_URL -u DAI_REDIS_URL .venv/bin/pytest -q tests/automated/test_ai_regression_v4.py tests/automated/test_ai_governance_gate.py
cd /home/zxk/Projects/dai-experiment-platform/frontend
E2E_BASE_URL=http://127.0.0.1:5175 npx playwright test e2e/school-gradebook-flow.spec.js --workers=1 --reporter=line --output=/tmp/school-gradebook-playwright-results
npx vitest run src/views/__tests__/GradebookView.spec.js
```

## 截图

截图由复现命令生成在仓库外，默认目录`/tmp`；可设置`GRADEBOOK_SCREENSHOT_DIR`改变位置。临时截图不随源码发布，以下为默认输出文件名。截图中的姓名、学号、成绩与日期全部是fixture数据。

| 角色 | 390px | 1440px |
|---|---|---|
| 教师历史归档 | `school-gradebook-teacher-390.png` | `school-gradebook-teacher-1440.png` |
| 管理员历史归档 | `school-gradebook-admin-390.png` | `school-gradebook-admin-1440.png` |
| 学生本人官方成绩 | `school-gradebook-student-390.png` | `school-gradebook-student-1440.png` |
