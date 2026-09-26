# 手动测试数据与 AI 判题指南

> 用途：部署后不依赖 seed 命令，纯手动在教师端录入一套完整数据，验证「课程 → 作业 → 考试 → 实验 → AI 判题」全流程。所有数据可直接复制粘贴。
>
> 约定：以下 Bash 路径写法 `.venv/bin/python` 在 Windows PowerShell 中对应 `.venv\Scripts\python.exe`。

---

## 目录

- [0. 前置准备](#0-前置准备)
- [A. 课程 / 章节 / 课时](#a-课程--章节--课时)
- [B. 作业（legacy 判题）](#b-作业legacy-判题)
- [C. 学生提交测试](#c-学生提交测试)
- [D. 考试](#d-考试)
- [E. 实验模块](#e-实验模块)
- [F. AI 判题测试（shadow / active）](#f-ai-判题测试shadow--active)
- [G. 验收清单](#g-验收清单)
- [H. 常见报错排查](#h-常见报错排查)

---

## 0. 前置准备

1. 本地栈已启动：`DAI_DEV_NO_BROWSER=1 ./scripts/dev-up.sh`（或手动模式）；
2. 教师账号可登录前端 <http://localhost:5173>：
   - 已跑过 seed：用 `teacher_zhang` / `Demo1234!`；
   - 未跑过 seed：用自建管理员创建教师，或直接以 admin 操作；
3. 学生账号：seed 的 `demo_student_elite`、`demo_student_average`、`demo_student_struggling`、`demo_student_new`（密码同上）。`seed-demo` 不会创建本机 MySQL、上传文件或 Docker 镜像；未播种时先在管理员端创建至少 2 名学生并加入课程名单，否则发布范围 `all_enrolled` 的作业/考试学生看不到。

---

## A. 课程 / 章节 / 课时

### A1 创建课程

| 字段 | 填写值 |
| --- | --- |
| 课程名称 | `Python 程序设计基础` |
| 课程编号 | `CS101-2026秋` |
| 课程简介 | 见下方代码块 |
| 可见性 | `class`（默认） |

```text
本课程面向零基础学生，覆盖 Python 基础语法、字符串处理与简单算法。
考核方式：平时作业（40%）+ 期末考试（60%）。
```

### A2 创建章节

| 字段 | 填写值 |
| --- | --- |
| 章节标题 | `第 1 章 Python 入门` |

### A3 在章节下创建课时（Markdown）

| 字段 | 填写值 |
| --- | --- |
| 课时标题 | `1.1 变量与数据类型` |
| 内容类型 | `markdown` |
| 状态 | `published` |
| 课时内容 | 见下方 |

课时内容（整段复制）：

````markdown
# 变量与数据类型

Python 是动态类型语言，变量无需声明类型。

## 基本类型

```python
age = 20            # int
price = 9.9         # float
name = "Alice"      # str
ok = True           # bool
```

## 类型检查

使用 `type()` 查看类型：

```python
print(type(age))   # <class 'int'>
```

## 动手练习

写一个函数 `count_vowels`，为本章末尾的作业做准备。
````

再建第二个课时（可选）：标题 `1.2 列表与字典入门`，任意 Markdown 内容，用于测试学习进度。

### A4 发布课程

课程完整性门禁通过后将状态改为 `published`。

---

## B. 作业（legacy 判题）

### B1 创建作业

| 字段 | 填写值 |
| --- | --- |
| 所属课程 | `Python 程序设计基础` |
| 作业名称 | `作业一：字符串与循环练习` |
| 截止时间 | 今天 +7 天（留空 = 长期开放） |
| 描述 | 见下 |
| Import 策略 | `unrestricted`（默认） |
| 发布范围 | `all_enrolled`（默认） |

```text
本次作业共 2 题满分 100 分。请实现指定函数并通过全部隐藏测试。
每题可多次提交，取最高分。
```

### B2 题目 1：统计元音字母

> ⚠️ **AI 评分模式务必选 `legacy`**（新建题表单默认即 legacy；若误选 shadow/active，未配置 AI 时发布会报 503）。

| 字段 | 填写值 |
| --- | --- |
| 题目标题 | `统计元音字母个数` |
| 函数签名 | `def count_vowels(text: str) -> int:` |
| 题干描述 | 见下 |
| 初始代码 starter_code | 见下 |
| 公开样例（JSON 数组） | `[{"args": ["hello"], "expected": 2}, {"args": ["PYTHON"], "expected": 1}, {"args": [""], "expected": 0}]` |
| 隐藏测试（pytest 源码） | 见下方代码块 |
| 时间限制 / 内存限制 | 默认 `10000` / `256` |
| 最大提交次数 | 留空（不限） |
| AI 评分模式 | **`legacy`** |

题干描述：

```text
实现函数 count_vowels(text)，返回 text 中元音字母（a e i o u，不区分大小写）的个数。
样例：count_vowels("hello") -> 2
```

初始代码：

```python
def count_vowels(text):
    # 请在此实现：返回 text 中元音字母（不分大小写）的个数
    pass
```

隐藏测试（若表单为可视化表格模式请切到 pytest 高级模式后整段粘贴）：

```python
import user_code


def test_basic():
    assert user_code.count_vowels("hello") == 2


def test_uppercase():
    assert user_code.count_vowels("PYTHON IS FUN") == 3


def test_empty_string():
    assert user_code.count_vowels("") == 0


def test_no_vowels():
    assert user_code.count_vowels("rhythm") == 0


def test_mixed():
    assert user_code.count_vowels("AbEcEdE") == 4
```

✅ **参考答案**：

```python
def count_vowels(text):
    return sum(1 for ch in text.lower() if ch in "aeiou")
```

### B3 题目 2：回文判断

| 字段 | 填写值 |
| --- | --- |
| 题目标题 | `回文串判断` |
| 函数签名 | `def is_palindrome(s: str) -> bool:` |
| 题干描述 | `忽略大小写与非字母数字字符，判断 s 是否为回文。如 "A man, a plan, a canal: Panama" -> True` |
| 初始代码 | `def is_palindrome(s):\n    pass`（编辑器内写成正常两行） |
| 公开样例 | `[{"args": ["aba"], "expected": true}, {"args": ["ab"], "expected": false}]` |
| 隐藏测试 | 见下 |
| AI 评分模式 | **`legacy`** |

隐藏测试：

```python
import user_code


def test_simple():
    assert user_code.is_palindrome("aba") is True


def test_with_punctuation():
    assert user_code.is_palindrome("A man, a plan, a canal: Panama") is True


def test_not_palindrome():
    assert user_code.is_palindrome("hello") is False


def test_empty_is_palindrome():
    assert user_code.is_palindrome("") is True
```

✅ **参考答案**：

```python
def is_palindrome(s):
    cleaned = [ch.lower() for ch in s if ch.isalnum()]
    return cleaned == cleaned[::-1]
```

### B4 发布作业

点 **发布**。预期：两道题齐全、无 AI 门禁报错、状态变 `published`。
若报 `AI_NOT_READY`：说明有题的评分模式不是 legacy，回去改题。

---

## C. 学生提交测试

切换学生账号进入作业一。

题目 1 编辑器粘贴：

✅ **通过版**（预期 5/5 全过、得满分）：

```python
def count_vowels(text):
    return sum(1 for ch in text.lower() if ch in "aeiou")
```

❌ **故意错误版**（预期部分失败，验证部分通过展示与断言差异输出）：

```python
def count_vowels(text):
    return sum(1 for ch in text if ch in "aeiou")
```

题目 2 通过版：

```python
def is_palindrome(s):
    cleaned = [ch.lower() for ch in s if ch.isalnum()]
    return cleaned == cleaned[::-1]
```

---

## D. 考试

### D1 创建考试

| 字段 | 填写值 |
| --- | --- |
| 所属课程 | `Python 程序设计基础` |
| 考试名称 | `第一章随堂测验` |
| 考试时长（分钟） | `30` |
| 开始时间 | 当前时间往前推 5 分钟 |
| 结束时间 | 当前时间 +7 天 |

> 开始必须早于现在，否则学生进不去；结束晚于开始即可。

### D2 Q1 单选题 · 10 分

| 字段 | 填写值 |
| --- | --- |
| 题型 | `single_choice` |
| 题干 | `以下关于 Python 列表（list）的说法，正确的是？` |
| 选项 A | `列表是不可变类型` |
| 选项 B | `列表可以通过下标访问和修改元素` |
| 选项 C | `列表只能存储相同类型的元素` |
| 选项 D | `列表长度创建后不能改变` |
| 正确答案 | **B** |
| 分值 | `10` |

### D3 Q2 单选题 · 10 分

| 字段 | 填写值 |
| --- | --- |
| 题干 | `表达式 type(3.14) 的结果是？` |
| 选项 | `A: int` ／ `B: float` ／ `C: str` ／ `D: complex` |
| 正确答案 | **B** |
| 分值 | `10` |

### D4 Q3 多选题 · 15 分

| 字段 | 填写值 |
| --- | --- |
| 题型 | `multi_choice` |
| 题干 | `以下哪些是 Python 中的可变（mutable）类型？` |
| 选项 | `A: list` ／ `B: tuple` ／ `C: dict` ／ `D: set` |
| 正确答案 | **A、C、D** |
| 计分模式 | `all_or_nothing`（全对才得分；也可选 `partial_no_wrong` 测部分给分） |
| 分值 | `15` |

### D5 Q4 填空题 · 15 分

| 字段 | 填写值 |
| --- | --- |
| 题型 | `fill_blank` |
| 题干 | `定义函数需要使用 ____ 关键字。（⚠️ 必须保留四处下划线占位符）` |
| 空 id | `b1` |
| 接受答案 | `def`（不区分大小写） |
| 分值 | `15` |

空设置 JSON：

```json
{"blanks": [{"id": "b1", "accepted_answers": ["def"], "case_sensitive": false}]}
```

### D6 Q5 代码题（legacy）· 50 分

| 字段 | 填写值 |
| --- | --- |
| 题型 | `code` |
| 题干 | `实现 reverse_string(s)，返回 s 的逆序字符串。` |
| 初始代码 | `def reverse_string(s):` + 换行缩进 `pass` |
| 公开样例 | `[{"args": ["abc"], "expected": "cba"}, {"args": [""], "expected": ""}]` |
| 隐藏测试 | 见下 |
| AI 评分模式 | **`legacy`** |

隐藏测试（考试代码题没有独立函数名字段，函数名从初始代码解析，务必保留 `def reverse_string`）：

```python
import user_code


def test_normal():
    assert user_code.reverse_string("abc") == "cba"


def test_empty():
    assert user_code.reverse_string("") == ""


def test_palindrome():
    assert user_code.reverse_string("aba") == "aba"


def test_unicode():
    assert user_code.reverse_string("你好") == "好你"
```

✅ **参考答案**：

```python
def reverse_string(s):
    return s[::-1]
```

### D7 发布考试与学生答题

保存全部 5 题（总分 100 > 0）→ 考试状态改 `published`。

学生答题参考：

- Q1 选 `B` ✅；Q2 选 `B` ✅；Q3 勾 `A C D` ✅；Q4 填 `def`
- Q5 提交：

```python
def reverse_string(s):
    return s[::-1]
```

全对预期成绩 **100**；故意错一道选择题可观察对应扣分。交卷后在教师端逐题改分里复核 Q5。

---

## E. 实验模块

实验管理 → 新建实验：

| 字段 | 填写值 |
| --- | --- |
| 实验名称 | `实验一：Jupyter 环境初体验` |
| 描述 | `熟悉内置 Notebook Player，运行第一个代码单元格。` |
| 截止时间 | +14 天 |

系统自动创建配套 Studio 模板。发布后进 Player 运行 `print("hello dai")` 单元格，验证 Kernel 容器正常。

---

## F. AI 判题测试（shadow / active）

### F0 开启 AI 配置

编辑 `backend/.env`（参照 `backend/.env.example` 中 `DAI_AI_*` 组）：

```env
DAI_AI_ENABLED=true
DAI_AI_BASE_URL=https://aihub.codingpython.cn
DAI_AI_API_KEY=sk-你的真实key
DAI_AI_MODEL=deepseek-v4-flash
```

然后重启 **API 和 Judge Worker**（两者都读取该配置）：

```bash
./scripts/dev-down.sh && DAI_DEV_NO_BROWSER=1 ./scripts/dev-up.sh
```

验证就绪：<http://localhost:8000/api/v1/health/ready> 或教师端设置页的 AI 状态。

### F1 三种评分模式

| 模式 | 判分方式 | 需要 hidden_tests | 需要 test_groups | 需要锁定 Rubric | 适用场景 |
| --- | --- | --- | --- | --- | --- |
| `legacy` | 隐藏测试全过 = 满分，否则 0 | 必填 | 不需要 | 不需要 | 纯客观题判分 |
| `shadow` | 保留 legacy 成绩，同时跑测试组和 AI，AI 结果仅作对比参考，不改正式分 | 必填 | **必填** | **必填** | 上线前对比 AI 与人工标准 |
| `active` | 确定性分（F+R）+ AI 分（A+Q）按公式合成为正式分 | 必填（字段必填） | **必填** | **必填** | AI 正式参与评分 |

合分公式（固定）：`F60 + A20 + R10 + Q10 = 100`
- **F（功能 60 分）/ R（健壮性 10 分）**：来自 test_groups 的确定性 pytest 结果；
- **A（算法思路 20 分）/ Q（代码质量 10 分）**：来自 AI 按 Rubric 打分；
- 考试中再按 `final_score_100 / 100 × 题目分值` 折算。

### F2 AI 作业题：两数之和（active 模式完整示例）

在「作业一」中追加第 3 题（或另建作业二）。

#### 基本字段

| 字段 | 填写值 |
| --- | --- |
| 题目标题 | `两数之和（AI 评分）` |
| 函数签名 | `def two_sum(nums: list[int], target: int) -> list[int]:` |
| 题干描述 | 见下 |
| 初始代码 | 见下 |
| 公开样例 | `[{"args": [[2, 7, 11, 15], 9], "expected": [0, 1]}, {"args": [[3, 2, 4], 6], "expected": [1, 2]}]` |
| 隐藏测试（必填字段） | 见下 |
| 时间 / 内存限制 | 默认 |
| **AI 评分模式** | **`active`** |

题干描述：

```text
给定整数数组 nums 和目标值 target，找出和为 target 的两个整数下标并按升序返回。
数据保证恰好存在一组解；找不到时返回空列表 []。
要求使用哈希表一次遍历实现，避免 O(n²) 双重循环。
```

初始代码：

```python
def two_sum(nums, target):
    # 返回和为 target 的两个下标（升序），找不到返回 []
    pass
```

隐藏测试（active 也必须填这个字段，作为兜底功能校验）：

```python
import user_code


def test_basic():
    assert user_code.two_sum([2, 7, 11, 15], 9) == [0, 1]


def test_duplicates():
    assert user_code.two_sum([3, 3], 6) == [0, 1]


def test_no_solution_returns_empty():
    assert user_code.two_sum([1, 2, 3], 99) == []


def test_negative_target():
    assert user_code.two_sum([-3, 4, 90], -3 + 90 - 87) == [0, 1] if False else True
```

#### 参考答案 reference_solution

```python
def two_sum(nums, target):
    seen = {}
    for i, num in enumerate(nums):
        complement = target - num
        if complement in seen:
            return [seen[complement], i]
        seen[num] = i
    return []
```

#### 教师约束 teacher_constraints（自由 dict，会原文注入 Rubric/AI 提示词）

```json
{
  "要求": ["返回下标按升序排列", "找不到解时返回空列表"],
  "禁止": ["修改函数签名", "引入第三方库", "对输入排序后破坏原下标"],
  "偏好": ["优先哈希表一次遍历（O(n)），接受等价正确实现"]
}
```

#### 测试组 test_groups（核心！F 组合计 ≤60，R 组合计 ≤10）

在题目 AI 配置面板的测试组中添加两个组（每组是一段 pytest 源码 + 元信息）：

**组 1 — 功能维度 F，满分 60**

| 属性 | 值 |
| --- | --- |
| id | `g-functional` |
| 名称 | `基础功能` |
| dimension | `F` |
| max_score | `60` |
| tests | 见下 |

```python
from user_code import *


def test_case_middle_pair():
    assert two_sum([3, 2, 4], 6) == [1, 2]


def test_case_duplicates():
    assert two_sum([3, 3], 6) == [0, 1]


def test_case_negatives():
    assert two_sum([-1, -2, -3, -4, -5], -8) == [2, 4]


def test_case_zero_and_large():
    assert two_sum([0, 4, 3, 0], 0) == [0, 3]


def test_case_single_pair_long_array():
    assert two_sum(list(range(100)) + [999, 1998], 999 + 1998) == [100, 101]
```

**组 2 — 健壮性维度 R，满分 10**

| 属性 | 值 |
| --- | --- |
| id | `g-robustness` |
| 名称 | `边界与健壮性` |
| dimension | `R` |
| max_score | `10` |
| tests | 见下 |

```python
from user_code import *


def test_no_solution_returns_empty():
    assert two_sum([1, 2, 3], 99) == []


def test_minimal_inputs():
    assert two_sum([5], 5) == []
    assert two_sum([], 0) == []


def test_result_order_ascending():
    result = two_sum([7, 3], 10)
    assert result == sorted(result)


def test_repeated_calls_stateless():
    assert two_sum([1, 2], 3) == [0, 1]
    assert two_sum([4, 5], 9) == [0, 1]
```

> 说明：每个组的 tests 若没写 import，系统会自动前置 `from user_code import *`；组内按通过的测试比例给分（如 5 过 1 挂 ≈ 该组 50 分制折算）。

#### 操作顺序（active 关键流程）

1. 填完上述所有字段，AI 模式选 **active** → **保存题目**；
2. 保存后在 AI 配置面板点 **生成 Rubric** → 校对内容 → **锁定 Rubric**（未锁定的 Rubric 无法发布）；
3. 发布作业 —— 此时后端校验 `ai_ready` + 锁定 Rubric，全部满足才能发布成功。

### F3 学生提交测试 active 题

✅ **哈希表满分版**（预期 F=60、R=10 全过，A/Q 由 AI 打高分的可能性大）：

```python
def two_sum(nums, target):
    seen = {}
    for i, num in enumerate(nums):
        complement = target - num
        if complement in seen:
            return [seen[complement], i]
        seen[num] = i
    return []
```

🆚 **暴力循环对比版**（确定性测试全过 → F/R 满分；但 AI 可能因未按要求使用哈希表而在 A 维度扣分——用于验证 AI 维度确实生效）：

```python
def two_sum(nums, target):
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            if nums[i] + nums[j] == target:
                return [i, j]
    return []
```

❌ **缺边界版**（`g-robustness` 组部分失败 → R 扣分，验证确定性维度）：

```python
def two_sum(nums, target):
    seen = {}
    for i, num in enumerate(nums):
        complement = target - num
        if complement in seen:
            return [seen[complement], i]
    return None
```

查看结果：教师端统一提交中心 / AI 复核页可见 F/R/A/Q 四维明细与合成总分；`active` 的分数要等 AI 返回后才落库（期间 grading_status 进行中）。

### F4 shadow 模式示例（可选）

把 B2 的「统计元音字母」复制为新题，评分模式选 **shadow**，并补配一个测试组（hidden_tests 沿用原题）：

| 属性 | 值 |
| --- | --- |
| id | `g-shadow-demo` |
| 名称 | `shadow 对比组` |
| dimension | `F` |
| max_score | `60` |
| tests | 复用 B2 隐藏测试内容即可 |

预期行为：学生提交后**正式成绩仍按 legacy 规则**（全过=满/不过=0），但教师复核处能同时看到 shadow 测试组结果与 AI 参考评分，用于上线前对比。

### F5 考试代码题开启 AI（可选）

将 D6 的 Q5 复制为新题：题型 `code`、分值 50、评分模式改 `active`，按 F2 同样方式配置测试组（dimension/max_score 相同即可）。

考试特有差异：
- 合成分按 `final_score_100 / 100 × 50` 折算为该题得分；
- 考试 active 题交卷后会进入 **review_required（待教师终审）** 状态，需教师在逐题改分/AI 复核中确认后生效——这本身就是要测的流程之一。

---

## G. 验收清单

| # | 步骤 | 预期结果 |
| --- | --- | --- |
| 1 | 课程发布 | 状态 published，学生端可见 |
| 2 | 学习进度 | 学生打开课时后进度更新 |
| 3 | 作业发布（全 legacy） | 无 AI 门禁报错 |
| 4 | 学生交正确代码 | 测试全过、得分 100 |
| 5 | 学生交错误代码 | 部分失败并展示断言差异 |
| 6 | 考试发布 | 校验通过（≥1 题、总分>0、起止时间合法） |
| 7 | 学生全对交卷 | 自动出分 100 |
| 8 | 实验 Player | Kernel 单元格能执行出 `hello dai` |
| 9 | active 题保存→Rubric→发布 | Rubric 可生成并锁定，发布成功 |
| 10 | active 题交哈希表解 | F=60、R=10，A/Q 由 AI 给分，总分≈100 |
| 11 | active 题交暴力解 | F/R 满分但 A 维度可能被 AI 扣分（证明 AI 生效） |
| 12 | active 题交缺边界解 | R 组部分失败被扣分 |
| 13 | shadow 题提交 | 正式分走 legacy，复核处可见 AI 对比 |
| 14 | 考试 active 题交卷 | 进入 review_required，教师终审后生效 |

---

## H. 常见报错排查

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `503 AI_NOT_READY` | 发布含非 legacy 题，但 `DAI_AI_ENABLED`/Key 未配置或服务未重启 | 检查 backend/.env 并重启 API + Judge Worker；或把题目改回 legacy |
| `503 AI_RUBRIC_UNAVAILABLE` | Rubric 生成时 AI 调用失败（网络/Key 无效/超时） | 核对 BASE_URL 与 API_KEY，稍后重试 |
| `shadow/active 需要测试组` | 非 legacy 题没配 test_groups 就有提交进来 | 补测试组 |
| `缺少锁定 Rubric` / 发布被拒 | Rubric 生成了但没锁定，或保存题目前就想发布 | 先保存题目 → 生成 → 锁定 → 再发布 |
| 选择题保存报 422 | correct_answer 里的 key 不在 options 中 | 检查选项键与答案键一致（如只有 A–D） |
| 填空题保存报 422 | 题干缺 `____` 占位符或 blank id 对不上 | 占位符数量与 blanks 一一对应 |
| 考试无法进入 | 当前时间不在 start_at ~ end_at 区间 | 调整考试起止时间 |
| 学生看不到作业/考试 | 学生未加入课程名单而发布范围是 all_enrolled | 管理员端把学生加入课程/班级 |

> 备注：seed_demo 里的历史 AI 成绩是**确定性 Fixture**（非真实 AI 输出），与本指南手动触发的真实 AI 评分为两套来源，互不影响。
