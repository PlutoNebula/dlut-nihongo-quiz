# PDF → 双 OCR → 比对提取 → 题库 .md　工作流设计文档

> 状态：**待评审**（本文档只是设计，尚未写任何脚本）
> 位置约定：工作流脚本与**全部中间产物**都在仓库根的独立目录 `pdf-ocr/`（中间产物在 `pdf-ocr/work/`）；`data/raw/<分类名>/` 只接收最终的 `<分类名>.md`
> 目标格式：与 `data/raw/japanese/*.md` 一致，能被现有 parser 直接消费

---

## 1. 目标与硬约束

### 1.1 目标

输入一个 PDF（试卷扫描/电子版），输出**可以直接进题库**的 markdown：

```
PDF ──▶ 每页 PNG ──▶ 两路 step-3.7-flash OCR ──▶ deepseek-flash 比对+题目提取 ──▶ 单个 .md
```

### 1.2 硬约束

| # | 约束 | 落实方式 |
|---|---|---|
| 1 | **格式硬契约必须遵守** | §2.1 的正则是不可协商的规格。工具**只允许产出 100% 合规的 .md**：宁可报错退出，也不产出"差不多能解析"的文件；每次生成后由 §9 离线校验器强制门禁 |
| 2 | 比对/提取模型 = **`deepseek-flash`** | `DEEPSEEK_MODEL` 默认值。⚠️ 原定的 `deepseek-v4.1-flash` 在该端点**不存在**（§20 实测：`/v1/models` 只返回 `deepseek-flash` 与 `deepseek-v4-pro`，写别的名字 HTTP 400），已按 flash 档改为 `deepseek-flash`；想换 pro 档只需改 `.env` 一行 |
| 3 | PDF 拆分**允许下载外部库/工具** | `pip install pymupdf`（本机 Python 3.14.2 + pip 26.0.1 可用） |
| 4 | 输出到 **`data/raw/{分类名}`**，分类名由**用户输入或 AI 总结** | 传了 `--category` 就用它；没传则由 AI 从首页 `paper_identity` 提议，**必须由用户确认**（回车采用 / 输入覆盖 / Ctrl+C 取消）；非交互环境未传 `--category` 直接报错退出。详见 §4.1 |
| 5 | **不改动现有数据** | `data/raw/` 下**只新增**最终 `<分类名>.md`；该目录已存在且非空一律拒绝覆盖（除 `--force`）；中间产物写到 `pdf-ocr/work/`；**绝不写** `public/*.json`，不碰任何既有 raw/processed 文件 |
| 6 | **题目最终输出不分批：全部写入一个文件** | `data/raw/<分类名>/<分类名>.md` 单文件；无 `--batch-size`，无分批边界逻辑 |
| 7 | **双路提示词"适度"异构** | 两路共用同一角色设定与同一输出 schema，只在 2–3 条侧重指令上不同（§7.1）；保证"可比"，同时避免两路错误完全相关 |
| 8 | **跨页断题必须拼接** | §8.2 定义了必须执行的拼接算法；拼接成功才产出该题，失败则该题标 `needs_review` 但仍输出（不丢题） |
| 9 | **Python 脚本 + 工作流方式，不过度打包** | 5 个小脚本，`python pdf-ocr/1_render.py …` 逐个跑；无包、无 `pyproject`、无 CLI 框架；风格对齐 `C:\Users\64247\Desktop\机组\_extract.py` |
| 10 | **`.env` 只放 API 相关三项 × 2**（base_url / 模型版本 / 密钥），并直接写进 `.env.example` | §2.4；dpi、页范围、重试次数等**一律走命令行参数**，不得进 `.env` |
| 11 | OCR 约定沿用 **`data/raw/computer-organization/ocr/`** | 目录结构、`source-manifest.json`、`page-XXX.review.json` 字段名全部镜像 |
| 12 | 每页独立执行；每页每阶段打印进度 `(?/n)` | 见 §6 输出规范 |

### 1.3 非目标（P1–P5 不做）

- 不接 `scripts/parse-japanese-2024-markdown.ts` 的读取列表 —— **整块挪到 P6**（md → 题库 JSON → 网页注册，调研见 §22）。
- 不改 `package.json` / npm 脚本；P5 之后再改 `.env.example`（P5 已追加，见 §13）。
- 不生成 `public/*.json`（P6 才做）。
- 不动 `src/` 下任何文件（P6 才做）。
- 不做图形界面、不做打包分发。

---

## 2. 现状调研结论（决定设计的事实）

### 2.1 目标格式契约（强制，来自真实 parser）

生成的 `.md` 必须满足 `scripts/parse-japanese-2024-markdown.ts`：

| 元素 | 必须写法 | 依据 |
|---|---|---|
| 题块分隔 | `### 第{N}题`（阿拉伯数字） | `:219` `:222` |
| 题组标题 | `## 题组{一…十}：<名称>`（**必须中文数字**） | `:225` `:365` |
| 题干区 | `#### 题目` | `:240` |
| 选项 | 行首 `A.` / `A、` / `A `（`^[A-D][\.\s、]`），一行一个 | `:277` `:304` |
| 翻译（可选） | `题目翻译：…` | `:283` |
| 答案区 | `#### 答案与解析` + **必须含** `**正确答案：D ちこく**` | `:251` `:323` |
| 解析 | 答案行之后的普通行 | `:336` |
| ⚠ 禁忌 | 解析区不能出现 `## ` 开头行 / `### 本组核心知识点总结`（会被截断） | `:262` |
| 收录条件 | 题干非空 + 选项 ≥ 2 + 有答案，否则该题**被静默丢弃** | `:349` |

黄金样例片段（摘自 `data/raw/japanese/2024年日语期末试卷.md`）：

```markdown
## 题组一：汉字读音选择

### 第1题

#### 题目

駅で財布を**拾った**んだけど、だれが落としたのでしょうか。

A. 【不清】
B. ひろった
C. さがった
D. もどった

#### 答案与解析

**正确答案：B ひろった**

「拾う」（ひろう）＝ 捡、拣。句形为「拾った」。…
```

> 这条约束不只是"文档"，而是**写入工具的硬门禁**：`5_check.py` 按上表逐条校验，任何一条不通过就退出码 `3`，并要求修完再入库。

### 2.2 OCR 约定（镜像 `data/raw/computer-organization/ocr/`）

现有产物形态（真实文件实测）：

```
data/raw/computer-organization/ocr/
├── source-manifest.json          # model / endpoint / documents[{source,pages,sha256,completed_pages}] / errors[]
├── computer-2024-final.md        # 合并后的整页转写
├── <试卷>/
│   └── page-001.review.json      # 每页一份
```

`page-001.review.json` 字段：`printed_page_labels[]`、`paper_identity{title,date,variant,declared_printed_pages}`、`question_ranges[]`、`page_condition`、`transcription_md`、`corrections[{location,before,after,evidence,confidence}]`。

**本工作流完全沿用这套字段**，只扩展两处：
- 每页 OCR 分两路：`page-001.a.review.json` / `page-001.b.review.json`。
- 新增 `page-001.merge.json` 保存比对 + 题目提取结论（§7.2）。

### 2.3 本机环境实测

| 项 | 现状 |
|---|---|
| Python | ✅ 3.14.2；pip 26.0.1 |
| 已装库 | ✅ `requests`、`httpx`、`python-dotenv`、`PIL` |
| PDF 库 | ❌ 未装 `pymupdf`（需 `pip install pymupdf`，允许） |
| poppler / ImageMagick / Ghostscript | ❌ 均无（不需要，PyMuPDF 自带渲染） |
| `.env` | ❌ 目前不存在（需新建；`.gitignore` 已忽略 `.env`，只放行 `.env.example`） |
| 既有同类脚本风格 | `机组/_extract.py`：单文件、`os.makedirs('_text')`、`print(f'{f}: N pages')`、UTF-8 stdout 包装 |

### 2.4 `.env`：只放 API 三项 × 2

`.env` **只允许**出现下面 6 行（端点 / 模型版本 / 密钥），其它任何配置都不进 `.env`：

```dotenv
STEPFUN_BASE_URL=https://api.stepfun.com/step_plan/v1/chat/completions
STEPFUN_MODEL=step-3.7-flash
STEPFUN_API_KEY=sk-...

DEEPSEEK_BASE_URL=https://api.deepseek.com/v1/chat/completions
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_API_KEY=sk-...
```

**默认值（已定稿）** —— `.env` 不提供时使用：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `STEPFUN_BASE_URL` | `https://api.stepfun.com/step_plan/v1/chat/completions` | **沿用原来那个端点**（`data/raw/computer-organization/ocr/source-manifest.json` 里记录的就是它） |
| `STEPFUN_MODEL` | `step-3.7-flash` | 两路 OCR 都用它 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1/chat/completions` | 比对+提取 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 比对+提取。端点实测只认 `deepseek-flash` / `deepseek-v4-pro`（原定的 `deepseek-v4.1-flash` 报 HTTP 400，见 §20） |

- **变量命名**：`STEPFUN_*` / `DEEPSEEK_*`（已定稿）。
- **取值优先级**：`.env` > 进程环境变量 > 上表默认值 > 既有 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` 回退（仅当 `DEEPSEEK_*` 全缺时启用；`.env` 优先于环境变量这一点沿用 `scripts/enrich-explanations.mjs` 的既有做法）。
- `--dpi` / `--pages` / `--max-retries` / `--category` 等**全部走命令行参数**，不写进 `.env`。
- 这 6 行已**追加到 `.env.example`**（保留既有 `ANTHROPIC_*` 三项不动，`scripts/enrich-explanations.mjs` 仍在用），作为模板提交；仓库根 `.env` 已按此建好、**密钥留空**待填，且被 `.gitignore` 忽略（不入库）。

---

## 3. 产物与目录布局

**分工原则：中间产物全部留在工具的 `pdf-ocr/work/`，`data/raw/` 只接收最终 `.md`。**

```
pdf-ocr/                                 # 代码 + 工作区（都在仓库根的独立目录里）
├── _common.py
├── 1_render.py … 5_check.py
└── work/<分类名>/                        # ★ 全部中间产物（.gitignore 已忽略）
    ├── source-manifest.json             # 账本：sha256 / pages / dpi / model / endpoint / completed_pages / errors
    ├── pages/
    │   ├── page-001.png                 # 渲染出的页图（保留，便于人工复核）
    │   ├── page-001.a.review.json       # OCR 路 A（沿用 review.json 字段）
    │   ├── page-001.b.review.json       # OCR 路 B
    │   └── page-001.merge.json          # 比对 + 题目提取结果
    ├── transcription.md                 # 所有页转写拼接（对齐现有 computer-*.md 的做法）
    └── report.md                        # 待人工复核清单（冲突 / 低置信 / 拼接失败）

data/raw/<分类名>/                        # 最终结果唯一落点：只放一个 .md
└── <分类名>.md                          # ★ 全部题目写在这一个文件里（不分批）
```

命名（目录与文件同名，镜像 `data/raw/japanese/2024年日语期末试卷.md` 的"文件夹=学科、文件=试卷"习惯）：

- 目录：`data/raw/<分类名>/`（**只有最终 .md**）
- 文件：`data/raw/<分类名>/<分类名>.md`（**单文件**，题号从小到大连续排列）
- 中间产物：`pdf-ocr/work/<分类名>/…`；渲染前的一次性暂存 `pdf-ocr/work/.tmp/<sha8>/`

**安全护栏**（对应硬约束 5）：
- 最终产物 `data/raw/<分类名>/` 已存在且非空 → **报错退出**，除非显式 `--force`（不动既有数据）。
- 写入路径白名单：最终产物必须在 `data/raw/` 之下，中间产物必须在 `pdf-ocr/work/` 之下（两道都防 `..` 逃逸）。
- 只 `open(...,'x')`（独占创建）或"临时文件 + 原子改名"。
- 不触碰 `public/`、`src/`、`data/processed/` 以及 `data/raw/` 下任何既有文件。

---

## 4. 工作流脚本（Python，逐个可跑）

```
pdf-ocr/                      # 仓库根的独立目录，便于单独管理这条工作流
├── _common.py         # .env 解析、HTTP POST + 重试、进度打印、JSON 读写（约 120 行）
├── 1_render.py        # PDF → pages/page-00N.png（PyMuPDF）+ 写 source-manifest.json
├── 2_ocr.py           # 每页两路 OCR → page-00N.{a,b}.review.json
├── 3_merge.py         # 每页 deepseek-flash 比对+提取 → page-00N.merge.json
├── 4_build_md.py      # 跨页拼接 + 汇总 → 单个 <分类名>.md + transcription.md + report.md
└── 5_check.py         # 离线契约校验（不调 API）
```

- 每个脚本可**单独运行**：`python pdf-ocr/2_ocr.py --category 2025年日语期末试卷`
- 统一参数：`--category`、`--force`、`--pages 1-12`、`--dpi 200`、`--quiet`、`--from N`（从第 N 步续跑）。
- **成本护栏参数**（P2/P3，详见 §18.5）：`--max-tokens`（单次响应上限，**OCR 默认 10000000 / merge 默认 393216**，即各端点允许的上限，见 §20.5）、`--budget-tokens`（本次运行累计上限，0=不限）、`--total-budget-tokens`（跨运行累计上限，0=不限）。
- 脚本之间**只通过磁盘产物耦合**（工作流方式：可中断、可重跑、可人工介入中间产物）。
- 依赖只写进本文档（`pip install pymupdf requests`），不做 `requirements.txt`/打包。

**退出码（各脚本统一）**

| 码 | 含义 |
|---|---|
| `0` | 成功（`5_check.py` 另有 `2` 表示"有警告/待复核"） |
| `1` | 参数或环境错误（缺 `--category`、缺密钥、找不到 S1 产物、PDF 不存在…） |
| `2` | 校验有警告：有待复核项但无硬错误（`5_check.py`） |
| `3` | 有页/有项失败（错误已记入 `source-manifest.json.errors`，可续跑补齐） |
| `4` | **成本预算用尽**（`--budget-tokens` 本次超限停在当前页，或 `--total-budget-tokens` 跨运行已超而拒绝开工） |

### 4.1 分类名如何确定（硬约束 4）

1. 传了 `--category 2025年日语期末试卷` → 直接用它（并先检查该目录是否已存在）。
2. 没传 → `1_render.py` 先把页图渲染到**一次性暂存目录** `pdf-ocr/work/.tmp/<sha256前8位>/`（该目录在 `.gitignore` 覆盖范围内），把**第 1 页**交给模型读出卷名，然后在命令行**停下来等用户确认**：

```
[分类名] 首页识别到：2025年日语期末试卷（2025-01-08 · A卷）
[分类名] 建议目录：data/raw/2025年日语期末试卷/
[分类名] 回车采用 / 直接输入别的名字 / Ctrl+C 取消： _
```

3. 确认之后才创建 `pdf-ocr/work/<分类名>/pages/`，把页图移入，并写同目录下的 `source-manifest.json`。
   `data/raw/<分类名>/` 在这一步**不会被创建**，它只等到 S4 写最终 `.md` 时才出现。
4. 目标目录已存在 → **报错退出**并列出该目录已有文件（不改动、不覆盖）；确实要重做须显式 `--force`。
5. 非交互环境（无 TTY）且未传 `--category` → 直接报错退出，提示显式传参（保证无人值守时不会静默写错目录）。
6. 分类名做 Windows 非法字符过滤（`\ / : * ? " < > |`），并去掉首尾空白。

---

## 5. 执行步骤（每步的输入/输出）

| 步 | 脚本 | 输入 | 输出 | 是否需要网络 |
|---|---|---|---|---|
| S1 | `1_render.py` | `.pdf`、`--dpi` | 先渲染到 `pdf-ocr/work/.tmp/<sha8>/`，**确认分类名（§4.1）**后移入 `pdf-ocr/work/<分类名>/pages/page-00N.png`，并写 `pdf-ocr/work/<分类名>/source-manifest.json`（**不写 data/raw**） | ❌ 本地 |
| S2 | `2_ocr.py` | 页图 | `page-00N.a.review.json`、`page-00N.b.review.json` | ✅ StepFun ×2/页 |
| S3 | `3_merge.py` | 两路 review JSON | `page-00N.merge.json` | ✅ DeepSeek ×1/页 |
| S4 | `4_build_md.py` | 全部 merge JSON | `<分类名>.md`（单文件）、`transcription.md`、`report.md` | ❌ 本地 |
| S5 | `5_check.py` | 生成的 .md | 控制台校验报告 + 退出码 | ❌ 本地 |

**每页独立**：S2/S3 逐页执行，页间不共享上下文，单页失败不污染其它页；S4 才做跨页拼接与全局排序。

---

## 6. 进度输出规范（硬要求）

### 6.1 行格式

```
[<阶段>] page <i>/<n> (<i>/<n>) <状态> <耗时>ms <附加信息>
```

- `<状态>`：`✓` 成功 / `↻` 重试中 / `⚠` 需复核 / `✗` 失败
- 页进度**始终**以 `(i/n)` 出现（i = 当前页，n = 总页数）
- 阶段编号固定 `S1/4`、`S2/4`、`S3/4`、`S4/4`，OCR 两路标 `A`/`B`
- 每页四阶段结束后，额外打印一行**当前步骤 + 总进度**：

```
== 第 3/12 页完成：渲染 ✓ | OCR-A ✓ | OCR-B ✓ | 比对提取 ⚠（2 处冲突）| 校验 ✓ | 累计题数 27 ==
```

### 6.2 输出示例（12 页 PDF）

```
[S1/4 渲染] page  1/12 (1/12)  ✓ 412ms  page-001.png 1654×2339
[S1/4 渲染] page  2/12 (2/12)  ✓ 388ms  page-002.png 1654×2339
…
[S1/4 渲染] 完成 (12/12) ✓ 总耗时 4.9s
[S2/4 OCR-A] page  1/12 (1/12)  ✓ 2180ms 1.9KB  q=1-4
[S2/4 OCR-B] page  1/12 (1/12)  ✓ 2310ms 2.1KB  q=1-4
[S3/4 比对提取] page  1/12 (1/12)  ✓ 11240ms 提取 4 题（冲突 0）
== 第 1/12 页完成：渲染 ✓ | OCR-A ✓ | OCR-B ✓ | 比对提取 ✓ | 校验 ✓ | 累计题数 4 ==
[S2/4 OCR-A] page  2/12 (2/12)  ✓ 2055ms 1.7KB  q=5-8
[S2/4 OCR-B] page  2/12 (2/12)  ↻ 重试 1/3（HTTP 429）3200ms
[S2/4 OCR-B] page  2/12 (2/12)  ✓ 1980ms 1.8KB  q=5-8
[S3/4 比对提取] page  2/12 (2/12)  ⚠ 提取 4 题（冲突 2：第6题选项A / 第8题答案）
== 第 2/12 页完成：渲染 ✓ | OCR-A ✓ | OCR-B ✓ | 比对提取 ⚠ | 校验 ⚠ | 累计题数 8 ==
…
[S4/4 汇总写盘] 跨页拼接 2 题 / 去重 1 题 → 单文件
[S4/4 汇总写盘] data/raw/<分类名>/<分类名>.md ✓ 100 题
[S5/5 校验] 契约检查：题块 100 / 题组 8 / 缺答案 0 / 会被丢弃 0  ✓
[完成] 共 100 题 | 待复核 3 | 失败 0 | 总耗时 6m12s
```

`--quiet` 只保留 `== 第 i/n 页完成 … ==` 与最终摘要。

---

## 7. 中间产物 JSON 契约

### 7.1 OCR 两路（`page-00N.{a,b}.review.json`）

字段与现有 `page-XXX.review.json` **同名同义**，附加调用元信息：

```jsonc
{
  "page": 1,
  "printed_page_labels": ["A-1"],
  "paper_identity": { "title": "…", "date": "…", "variant": "A", "declared_printed_pages": "10" },
  "question_ranges": ["1-4"],
  "page_condition": "clear",              // clear | blurry | cropped | handwritten | mixed
  "transcription_md": "……整页转写（保留原版面）……",
  "corrections": [ { "location": "…", "before": "…", "after": "…", "evidence": "…", "confidence": "high" } ],
  "uncertain": [ { "location": "第1题选项A", "note": "模糊，疑似 ひろった" } ],
  "call": { "pass": "a", "model": "step-3.7-flash", "endpoint": "…", "elapsed_ms": 2180, "usage": {} }
}
```

**提示词：适度异构**（硬约束 7）。两路必须"可比但不相关"：

| | 完全一致的部分（保证可比） | 允许不同的部分（仅 2–3 条，制造差异） |
|---|---|---|
| 角色 | 系统提示词同一句：`你是在做试卷整页转写与结构化提取的助手` | — |
| 输出 | **同一 JSON schema**（上表字段）、同一 `temperature` 与 `max_tokens`、同一图片分辨率 | — |
| 任务 | 都要输出整页 `transcription_md`、都要标 `uncertain` | A 路：强调**逐字转写、保留原版面与换行、先转写后理解**；不确定处尽量只标位置不改字<br>B 路：强调**按题目为单位整理（题干/选项/答案）**、逐项给置信度、允许在明显错字处给出"更可能的读法" |
| 顺序 | 同一字段顺序 | A 路先整页转写再列题目；B 路先列题目再补整页转写 |

> 依据：两路若提示词完全相同，同一模型的错误会高度相关，互校退化为"互相印证错误"；但若差异过大（不同 schema、不同角色），两路结果又无法逐字段比对。所以取"同 schema + 同角色 + 2–3 条侧重差异"的折中。

### 7.2 比对 + 提取（`page-00N.merge.json`，DeepSeek 单次调用）

```jsonc
{
  "page": 1,
  "paper_identity": { "title": "…", "date": "…", "variant": "A" },
  "conflicts": [
    { "question": 6, "field": "options.A", "a": "ちゅうし", "b": "ちょうし",
      "chosen": "ちゅうし", "reason": "A 路该处未标 uncertain，且与题干汉字一致",
      "confidence": "medium" }
  ],
  "questions": [
    {
      "number": 1, "group": "题组一", "groupTitle": "汉字读音选择",
      "stem": "駅で財布を**拾った**んだけど、…",
      "options": [ { "key": "A", "text": "…" }, { "key": "B", "text": "ひろった" } ],
      "answerKey": "B", "answerText": "ひろった",
      "explanation": "…", "translation": "…",
      "confidence": "high", "needs_review": false,
      "continued": false,                    // 是否跨页断题（题干/选项被页边界切开）
      "source": { "page": 1 }
    }
  ]
}
```

判定规则：
- 同一题同一字段两路不一致 → **必进** `conflicts[]`，模型须给出 `chosen` 与 `reason`。
- `confidence != high` 或存在未消解冲突 → 该题 `needs_review: true`，进 `report.md`（附 A/B 原文对照）。
- 页边界处题干/选项不完整 → `continued: true`，交给 S4 拼接。
- **确定性兜底**：两路在"格式无关字段"（`printed_page_labels` / `question_ranges` / `page_condition` / `paper_identity`）上不一致时，即使模型没报冲突，工具也补一条 synthetic conflict 并把整页标 `needs_review`。
  > 为什么不比转写文本：两路提示词刻意异构（A 逐字保版面、B 按题结构化），`transcription_md` 的排版本来就不同，做文本相似度会大量误报；只有上面这几个字段可以稳定逐字段比。
- **只有一路成功**时（另一路失败）仍照常提取，但整页标 `needs_review`，并在 `notes[]` 里注明缺哪一路。

---

## 8. `.md` 生成规则（S4）

### 8.1 基本规则

1. **排序**：跨页拼接后按全局题号升序，**全部写进同一个文件**（无分批）。
2. **题组归属**：`## 题组X：名称` 可能被页边界截断（下一页只剩"（续）"），以最近一次完整标题为准；同一题组只输出一次标题。
3. **去重**：键 = `题号 + 归一化题干`（去空白/标点/大小写，思路同 `parse-history-markdown.ts`）；命中时保留信息更全的一份并记入报告。
4. **题号必须是卷面原题号，跨题组连续**（P6 回填的硬要求，见 §22.5）：
   解析端的 `parse-japanese-2024-markdown.ts:355` 把 `### 第N题` 的 N 直接当 `numberInGroup` 存库。
   所以**不要把每个题组重新从 1 编号** —— 这份期中卷卷面是 1–41 连续编号，就照 1–41 写；
   否则网站上每道题的"第几题"会与原卷对不上。

### 8.2 跨页断题拼接（硬约束 8，必须执行）

识别：`merge.json` 中 `continued: true` 的题目（页尾题干被切断，或选项/答案出现在下一页页首）。

拼接算法（按序尝试，第一条成功即停）：

1. **按题号配对**：第 i 页尾部的 `continued` 题（题号 N）与第 i+1 页首部同为题号 N 的片段 → 两者合并：题干按顺序拼接、选项按 key 去重合并、答案取有值的一侧、解析两侧拼接。
2. **按"题号缺失"配对**：若下一页首部的第一个题目编号 ≠ N+1（说明第 N 题的剩余部分没有独立编号）→ 把下一页首部（在第一个 `### 第N+1题` 之前的全部内容）并入第 N 题。
3. **兜底**：以上都不成立 → 该题保持现状、置 `needs_review = true`，在 `report.md` 记录"疑似跨页断题未拼接"，**但仍输出**（不丢题）。

拼接后必须重新做一次字段完整性检查（题干非空、选项 ≥ 2、有答案），不满足则同样标 `needs_review` 并进报告。

### 8.3 输出模板

```markdown
# <分类名>

> 来源：`<原 PDF 文件名>`（sha256 前 8 位：`9f3c1a2b`，共 12 页）
> 生成：双路 step-3.7-flash OCR + deepseek-flash 比对提取；待复核项见 `ocr/report.md`

## 题组一：汉字读音选择

### 第1题

#### 题目

<题干>

A. …
B. …
C. …
D. …

#### 答案与解析

**正确答案：B ひろった**

<解析正文>
```

待复核标记（**不破坏 parser**，引用行会被跳过，依据 `:289`）：

```markdown
> ⚠ 待核对（OCR 冲突）：选项 A 两路不一致（A 路「ちゅうし」/ B 路「ちょうし」），详见 report.md
```

---

## 9. 离线契约校验（`5_check.py`）—— 硬门禁

复用 parser 的同一套正则，对生成的 `.md` 做**不调 API、不写盘**的检查；**不通过就不允许入库**：

| 检查项 | 说明 |
|---|---|
| 题块数 / 题号连续性 | `### 第N题` 数量与编号是否缺号、重号 |
| 题组标题规范 | 是否 `## 题组{中文数字}：…` |
| 选项与答案 | 选项 ≥ 2；`**正确答案：X**` 存在且 X 落在选项内 |
| **会被 parser 丢弃的题** | 按 `:349` 条件预演，任何会被丢弃的题**必须报出** |
| 截断风险 | 解析区内是否混入 `## ` 开头行 |
| 跨页拼接结果 | `continued` 题是否都已拼接或已标 `needs_review` |

退出码：`0` 全绿；`2` 有警告（待复核）；`3` 有硬错误（缺答案/重复题号/会被丢弃）。

---

## 10. 失败、重试与续跑

- 重试：`429/5xx/超时` 指数退避（默认 3 次，`--max-retries`）；`4xx` 直接记失败。
- 单页失败不阻断：记入 `source-manifest.json.errors` 与 `report.md`；最终退出码 `3`。
- 续跑：默认按 `completed_pages` + 产物存在性跳过已完成步骤；`--force` 全量重跑。
- 幂等：重跑不重复写 `.md`（临时文件 + 原子改名）；已存在的目标文件默认拒绝覆盖。
- **成本护栏（§18.5）**：每次请求带 `max_tokens`；`--budget-tokens` 在每次调用前检查，超了**停在当前页**（已完成页全部落盘，可续跑）并返回退出码 **4**；`--total-budget-tokens` 基于 manifest 里累计的 `usage`，已超则**拒绝开工**（退出码 4）。`usage` 跨运行累加，可用来看历史消耗。

## 11. 依赖与安装

```bash
pip install pymupdf requests
# requests 本机已装；pymupdf 需新装（本机已装 1.28.2）。
# .env 由脚本自行解析，不需要 python-dotenv。
# 可选（图片预处理/去噪）：pip install pillow
```

无需 poppler / ImageMagick / Ghostscript；无需 Node 侧新增依赖。Token 消耗评测见 §18。

## 12. 安全

- 密钥只从 `.env` / 环境变量读取；**不打印、不写进任何产物**；日志对 `Authorization` 脱敏。
- 两条路径白名单：最终产物必须落在 `data/raw/` 下、中间产物必须落在 `pdf-ocr/work/` 下，都拒绝 `..` 与绝对路径逃逸。
- 文件名过滤 Windows 非法字符（`\ / : * ? " < > |`）。
- 全程 `encoding='utf-8'`；`setup_stdio()` 把 stdout/stderr **和 stdin** 都固定为 UTF-8（对齐既有 `_extract.py` 写法；stdin 若不固定，管道传入的中文会按 ANSI 代码页解码成乱码）。

## 13. 实施计划（评审通过后按序做）

| 阶段 | 内容 | 验收 | 状态 |
|---|---|---|---|
| **P1** | `_common.py` + `1_render.py`：PyMuPDF 渲染、**分类名确认流程（§4.1）**、manifest、进度输出 | 对真 PDF 跑通 S1，控制台输出符合 §6；两条路径都验证：①交互确认后建目录并写 manifest；②目标目录已存在时报错退出、不动既有文件 | ✅ 见 §15 |
| **P2** | `2_ocr.py`：两路"适度异构"提示词 + 重试 + `review.json` | 单页两路产物齐全；断网能看到 `↻` 重试行 | ✅ 见 §16（含 §18.5 成本护栏） |
| **P3** | `3_merge.py`：DeepSeek 比对+提取 + schema 校验 | 构造两路故意不一致的输入，能产出 `conflicts[]` 且 `needs_review` 正确 | ✅ 见 §17（含 §18.5 成本护栏） |
| **P4** | `4_build_md.py` + `5_check.py`：跨页拼接 + 单文件成文 + 契约门禁（**范围不变：只产出 `data/raw/<分类名>/<分类名>.md`，不碰网页**） | `5_check.py` 全绿；抽查题号/答案与页图一致 | ⏳ 下一步 |
| **P5** | 追加 `.env.example` 的 6 行 API 配置（保留既有 `ANTHROPIC_*`）+ 本文档回填实测命令与耗时 | `.env.example` 内容与 §2.4 一致；文档与实际行为一致 | ✅ `.env.example` 已追加、`.env` 已建（密钥留空）；实测记录见 §15/§16 |
| **P6** | **网页接线**（本轮新增，从 P4 拆出）：md → `public/<key>-question-bank.json` → 5 处注册，让新试卷在网站上出现 | 落地页多一张卡片、`/home` 能进去刷题、`_meta.json` 计数正确、`vitest` 全绿 | ⏳ 排在 P4 之后，调研见 §22 |

## 14. 决策记录（已定稿）

| 项 | 结论 |
|---|---|
| StepFun 端点 | **沿用原来的**：`https://api.stepfun.com/step_plan/v1/chat/completions` |
| 模型版本默认 | `STEPFUN_MODEL=step-3.7-flash`（两路 OCR）／`DEEPSEEK_MODEL=deepseek-flash`（比对+提取） |
| `.env` 变量命名 | `STEPFUN_*` / `DEEPSEEK_*`；`.env` 只放那 6 行，其余配置走命令行参数 |
| 分类名 | 未传 `--category` 时：AI 提议 → **用户确认**（§4.1）；非交互环境必须显式传参 |
| 最终输出 | **单文件** `data/raw/<分类名>/<分类名>.md`，不分批 |
| 格式 | 硬契约（§2.1），由 `5_check.py` 强制门禁（§9），不合规不产出 |
| 双路提示词 | 同角色 / 同 schema / 同采样参数，仅 2–3 条侧重异构（§7.1） |
| 跨页断题 | **必须拼接**（§8.2 三步算法），失败则标 `needs_review` 但不丢题 |
| OCR 产物约定 | 镜像 `data/raw/computer-organization/ocr/`（§2.2） |
| 现有数据 | 一律不改：`data/raw/` 下只新增最终的 `<分类名>.md`；中间产物全部留在 `pdf-ocr/work/` |

**下一步**：P1、P2、P3 已完成（见 §15 / §16 / §17），接着做 **P4**（`4_build_md.py` + `5_check.py`：跨页拼接 + 单文件成文 + 契约门禁）。
P4 **范围不变**：只产出 `data/raw/<分类名>/<分类名>.md`，不动 `scripts/`、不动 `src/`、不产出 `public/*.json`。
把 md 接进网站的事整块挪到 **P6**（调研结论见 §22）。

---

## 15. P1 实测记录

环境：Python 3.14.2 · PyMuPDF 1.28.2（`pip install pymupdf`）· 输入用 `Desktop\机组\4.pdf`（28 页）与 `3.pdf`（29 页）

| 用例 | 命令要点 | 结果 |
|---|---|---|
| A 显式分类名 | `1_render.py "…\4.pdf" --category _p1-smoke --dpi 150` | 退出码 0；28 张 PNG + `source-manifest.json`；**17 项格式/manifest 断言全通过**（进度行匹配 §6 正则、`page i/n` 与 `(i/n)` 自洽、每页一行完成行、manifest 沿用现有约定字段并附 `ocr_passes`/`merge_model`） |
| B 目标目录已存在 | 同 A 再跑一次 | 退出码 1，列出已有文件；既有 `source-manifest.json` 哈希不变（**未动既有数据**） |
| C 交互确认（管道给名字） | `'2025年日语期末试卷' \| 1_render.py "…\3.pdf" --dpi 100 --pages 1-2` | 退出码 0；中文分类名正确；第 1 页从 `pdf-ocr/work/.tmp/<sha8>/` 移入工作目录后暂存目录清空 |
| D 无人值守无分类名 | stdin=DEVNULL | 退出码 1，提示「请显式传 `--category`」，**未创建任何目录** |
| E 只渲染部分页 | `--pages 1-3` | 退出码 0，只生成 3 页 |

实测中发现并修掉的两个 bug（都属于"换个环境就会炸"的类型）：

1. **管道传中文被按 ANSI 代码页解码**：分类名变成乱码且带孤立代理字符 → `setup_stdio()` 现在把 **stdin 也显式按 UTF-8 读取**（真实控制台输入走 Windows 宽字符 API，不受影响）。
2. **`pixmap.save(路径)` 走 C 字符串**：遇到不可编码字符报 `argument 2 of type 'char const *'` → 改为 `pixmap.tobytes("png")` + `Path.write_bytes()`。

另外记录两个环境事实，便于以后复现：

- PowerShell `>` 重定向会把子进程输出写成 **UTF-16LE**（脚本自身输出是 UTF-8），校验脚本需自动识别编码。
- 仓库根 `.env` 已建好（**密钥留空，等你填**）：`STEPFUN_API_KEY` 为空时，分类名提议会回退到 PDF 文件名，`2_ocr.py` 会直接提示缺密钥。填上密钥后再验证"模型读图"与真实 OCR 调用。

---

## 16. P2 实测记录

产物：`pdf-ocr/2_ocr.py`（+ `_common.py` 的 HTTP 层重构：单次 `post_json` 抛 `ApiError(retryable)`、`call_with_retry` 负责退避、`image_data_url`、`extract_json_object`）

**验收方式**：本机没有密钥，所以用**离线桩**替掉 `_common.post_json`（记录每次请求的 system/user/温度/模型/图片长度，并让 B 路第一次返回 `HTTP 429`），跑 `2_ocr.py` 的真实主流程。**22 项断言全部通过**：

| 类别 | 断言要点 | 结果 |
|---|---|---|
| 流程 | 退出码 0；共 **7 次调用** = 3 页 × 2 路 + 1 次 429 重试；输出含 `↻ 重试 1/3`；进度行含 `(i/n)` | ✅ |
| 产物 | 6 个 `page-00N.{a,b}.review.json`；键与现有 `review.json` 约定一致（另加 `uncertain[]`/`call{}`）；`paper_identity` 四键齐全；`call` 记录 pass/model/端点/耗时 | ✅ |
| 提示词异构 | **系统提示词两路完全一致**、schema 块一致、temperature 一致、model/端点一致；**只有侧重文本不同**（A 含"逐字转写"、B 含"按题结构化"，互不包含）；同一页两路用同一张图（data URL 长度相同） | ✅ |
| 失败路径 | 让桩恒返回 `HTTP 400` → 退出码 **3**，输出行含 `✗`，错误写进 `manifest.errors` | ✅ |
| 缺密钥路径 | 无 `STEPFUN_API_KEY` → 退出码 **1**，提示"在仓库根 `.env` 里填 `STEPFUN_API_KEY`" | ✅ |
| 续跑 | 已存在的 `review.json` 默认跳过（`--force` 才重跑）；`manifest.documents[0].ocr_pages` 回写为已完成两路的页号 | ✅ |

**待真实密钥冒烟确认的一处（唯一外部不确定项）**：StepFun `step_plan/v1/chat/completions` 的**请求体形态**（我按 OpenAI 兼容 vision 实现：`messages[0]=system`、`messages[1].content=[{type:text},{type:image_url}]`、`temperature: 0`）。拿到密钥后先跑单页：

```bash
python pdf-ocr/2_ocr.py --category <分类名> --pages 1
```

若报 400/422，只需改 `2_ocr.py` 的 `build_payload()` 一处；`normalize_review()`、进度输出、重试与账本逻辑都不用动。

---

## 17. P3 实测记录

产物：`pdf-ocr/3_merge.py`（每页一次 `deepseek-flash` 调用 → `page-00N.merge.json`）

**验收方式**：同样用离线桩替掉 HTTP（`_common.post_json`），并**手写两路故意不一致的 review.json** 造场景。**33 项断言全部通过**：

| 类别 | 断言要点 | 结果 |
|---|---|---|
| 流程 | 退出码 0；4 次调用 = 3 页 + 1 次 429 重试；输出含 `↻ 重试 1/3`；进度行含「提取 N 题（冲突 M）」；有冲突页显示 `⚠` | ✅ |
| 冲突识别（P3 验收项） | 第 1 页：模型报的冲突原样保留（`options.A`），`chosen`/`reason` 齐全 | ✅ |
| **确定性兜底** | 第 2 页故意让两路 `question_ranges` 不一致（`3-4` vs `3`）而**让桩返回 0 冲突** → 工具补出 `field=question_ranges` 的 synthetic conflict、`confidence=low`、`notes` 注明来源、整页 `needs_review=true` | ✅ |
| 缺一路 | 第 3 页只有 A 路 → `notes` 写"缺少 OCR 路 B…"、整页 `needs_review=true`，**仍提取到题目** | ✅ |
| 归一化 | 题号转 int、`answerKey` 转大写、`source.page` 记录；**答案不在选项里 → `needs_review` 收紧为 true**；无选项的题 `options=[]` 且答案进 `answerText` | ✅ |
| 跨页信号 | `continued: true` 原样透传给 S4 | ✅ |
| 提示词 | 同时含两路转写正文（各自独有标记都能在提示里找到）、含 schema 与规则、`temperature=0`、带 Authorization、系统提示词统一、**未把图片塞进 merge 请求**（纯文本比对） | ✅ |
| 续跑 | 第二次运行 0 次调用、打印「已存在，跳过」 | ✅ |
| 失败/缺密钥 | 恒 `HTTP 400` → 退出码 3 且写进 `manifest.errors`；缺 `DEEPSEEK_API_KEY` → 退出码 1 并提示写进 `.env` | ✅ |
| 账本 | `manifest.documents[0].merge_pages` 回写为已完成页号 | ✅ |

---

## 18. Token 消耗评测（实测口径）

> ⚠️ **本章是"按字符数推算"的估算，已被 §21.4 的真实账单替换**：一份 4 页试卷实测 **24.2k token/页**
> （OCR 15.2k + merge 9.0k），明显高于本章的区间 —— 差在**推理模型的思维链**（merge 出参的 40%~86%）。
> 做预算请用 §21.4 的数。

**测量方法**（不调 API、不写盘）：

- **页图**：用 PyMuPDF 对真实 PDF（`Desktop\机组\3.pdf` 第 1 页）在 150/200/300 dpi 下真实渲染，取像素与 PNG 字节；
- **提示词与出参**：直接调用 `pdf-ocr` 里的真实代码取长度 —— `2_ocr.build_payload()` / `3_merge.build_payload()` / `normalize_question()`；
- **文本语料**：`data/raw/japanese/2024年日语期末试卷_整理版.md`（99 题），实测每题"题目+选项"= **131.7 字符**。

### 18.1 每页账本（一页按 5 题、页图 200 dpi）

| 阶段 | 输入 | 输出 | 备注 |
|---|---|---|---|
| OCR-A | 625~1,043（文本）+ **1,105~2,316（图）** | 转写（进下一步） | 提示词实测 1,043 字符（系统 65 + schema 802 + 侧重 176） |
| OCR-B | 同 A（两路等量） | 同上 | |
| merge | 1,665~2,775 | 1,293~2,155 | 输入实测 2,775 字符 = 两路转写 + schema/规则（`build_payload` 真实输出） |
| **每页合计** | | | **≈ 6.4k ~ 11.6k token**，其中图像占 **38%~46%** |

整卷：**15 页（≈100 题）≈ 96k ~ 175k**；28 页 ≈ 180k ~ 326k。平均每题约 **1.3k ~ 2.3k** token。

### 18.2 两个口径的不确定性（这是区间宽的原因）

- **图像 token** 按两种主流口径给区间：OpenAI 分块式（短边缩到 768、按 512 分块，`85+170×块数`）≈ **1,105/页**；Anthropic 面积式（长边缩到 1568，`面积/750`）≈ **2,316/页**。StepFun 的真实口径未知。
- **CJK 文本** 按 **0.6~1.0 token/字符** 给区间（中文/日文 BPE 通常在这个范围）。
- **真实值以接口返回的 `usage` 为准**：P2/P3 已经把每次调用的 `usage` 写进 `call.usage`（`page-00N.{a,b}.review.json` 与 `page-00N.merge.json`），跑通一页后即可按页求和标定。

### 18.3 三个实测结论

1. **提高 dpi 几乎不增加 token**：150→200→300 dpi，两种口径的图像 token 都不变（都被服务端/口径缩放到阈值内），涨的只是上传体积 —— PNG 69/97/158 KB，base64 后 92/129/211 KB。所以 OCR 认不清小字时，**提高 dpi 的 token 代价很小，代价在传输与耗时**。
2. **文本占了过半**：图像只占 38%~46%，其余是"两路转写（进 merge）+ merge 出参 + 提示词"。所以省 token 的重点不只是图。
3. **提示词本身很小**（OCR 1,043 字符 / merge 1,107 字符），相对每页 660 字符的转写量，属于固定成本，**不必为它做缓存优化**。

### 18.4 降本杠杆（同一页 5 题）

| 做法 | 效果 | 代价 |
|---|---|---|
| 只跑一路 OCR（`--passes a`） | 每页约 **−38%** | 失去互校；`merge` 会把整页标 `needs_review`（已在实现里兜住） |
| merge 只喂题干+选项、不喂整页转写 | merge 输入约 **−30~40%** | 失去版面互校能力，冲突更难发现 |
| dpi 从 200 降到 150 | token ≈ 不变，上传 −29% | 小字识别率可能下降 |
| 页图先二值化/裁白边（未实现） | 若能把短边压到 768 以下可减少分块 | 需加 PIL 预处理，增加一道风险 |

### 18.5 成本护栏（已实现，P2/P3）

**问题**：`max_tokens` 不设时模型可以话痨，多余内容虽然会被 `extract_json_object()` 丢掉，**但照样计费**；重试与反复重跑也会把账放大。

| 护栏 | 位置 | 默认 | 行为 |
|---|---|---|---|
| `--max-tokens` | 每次请求体 | OCR **10000000** / merge **393216** | 单次响应上限。**2026-09-22 按用户要求放开到各端点允许的最大值**（详见 §20.5）：OCR 侧 StepFun 不校验、自设 10,000,000；merge 侧 DeepSeek 合法区间恰为 `[1, 393216]`，写 10000000 会被 HTTP 400 打回。历史沿革：OCR 2048 → 4096 → 放开；merge 4096 → 6144 → 放开。超过上限时脚本**在发请求前**就报错退出（退出码 1），不会变成"每页刷一次 HTTP 400" |
| `--budget-tokens` | 每次调用**之前** | `0`（不限） | 本次运行累计上限。超了**停在当前页**（已完成页全部落盘、可续跑），打印 `预算已用尽…`，退出码 **4** |
| `--total-budget-tokens` | 开工前 | `0`（不限） | 跨运行累计上限，读 manifest 里的 `usage`；已超则**拒绝开工**（退出码 4），避免"反复调用"把账烧穿 |
| `--max-retries` | 每次调用 | `3` | 429/5xx 才重试；4xx 直接记失败不重试。**第 2 次起自动追加纠偏提示**（见 §19） |
| 续跑 | 每页 | 开 | 已存在的 `review/merge.json` 默认跳过，只有 `--force` 才重烧 |
| Ctrl+C | 进程级 | — | 友好退出（退出码 **130**），提示产物已落盘、重跑自动续跑 |

**用量账本**：每次调用把接口返回的 `usage` 累加进 `pdf-ocr/work/<分类名>/source-manifest.json` 的 `usage` 块（`calls` / `tokens` / `updated_at`，跨运行累加）；每页完成行与脚本结尾都会打印 `[用量] 本次调用 N 次 / X tok；该分类累计 M 次 / Y tok`。`usage_tokens()` 兼容 `total_tokens`、`prompt+completion`、`input+output` 三种字段形态。

**验收（30 项断言全过）**：预算 5000 + 每次 2000 → 第 3 次调用前停下、退出码 4、已完成的 3 个产物落盘、`usage` 记为 3 次/6000 tok；续跑只补剩余 3 次；累计超限时拒绝开工且**一次调用都不发**；`--max-tokens 777` 确实进请求体；P3 同样行为（含 `input/output` 口径累加）。

---

## 19. 真实冒烟记录（第一次，StepFun 已配上密钥）

**跑法**：`python pdf-ocr/2_ocr.py --category LL11512 --pages 3`（S1 已把 37 页渲染进 `pdf-ocr/work/LL11512/`）

### 19.1 好消息：外部不确定项解决了

- 端点/模型/密钥**全部可用**：真实回复、单次耗时 15~18s。
- **请求体形态确认无误**（OpenAI 兼容 vision：`messages[0]=system`、`messages[1].content=[{text},{image_url}]`）—— 这是之前唯一没验证的外部假设。

### 19.2 暴露的三个问题与修复

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | `↻ 重试 1/3（JSON 解析失败：Expecting ',' delimiter: line 3 column 91）` | 模型把整页转写写进 JSON 字符串时用了**裸换行**（不是 `\n`）→ 非法 JSON。长转写必然撞上 | `extract_json()` 新增容错：字符串内控制字符自动转义、去尾随逗号、**逐步回退的截断修复**；并返回 `{repaired, truncated}` 元信息 |
| 2 | `↻ 重试 2/3（回复里找不到 JSON 对象）`，且**看不到模型到底回了什么** | 模型这次回的是纯文字（或正文在别的字段里，如 `reasoning_content`）。原来只读 `message.content`，失败时也不打印原文 | `response_text()` 依次尝试 `content` / `reasoning_content` / `reasoning` / `text`（支持分段数组）；失败信息里**带上原文预览**（`preview()` 压成一行，200 字） |
| 3 | 同一路连续重试 3 次 = 烧 3 倍 token | 重试时原样重发，模型很可能重蹈覆辙 | 第 2 次起自动在提示词末尾追加**纠偏提示**：「只输出一个 JSON 对象、不要围栏、字符串内换行写成 `\n`」 |
| 4 | 之前那个 Traceback | 大概率是 Ctrl+C（`KeyboardInterrupt` 未处理） | `KeyboardInterrupt` 友好退出（退出码 130），提示产物已落盘、可续跑 |
| 5 | 密排页更可能被截断 | `max_tokens=2048` 对长转写不够（该 PDF 第 3 页原图 434KB、第 32 页 1.35MB） | 默认上调 **OCR 2048→4096 / merge 4096→6144**；并记录 `finish_reason`，`length` 时打告警提示加大 `--max-tokens`（这两个默认值后来已按 §20.5 放开到端点上限） |

产物里新增三个可核查字段（都在 `call{}` 里）：`finish_reason`、`json_repaired`、`json_truncated` —— 出现 `truncated: true` 就说明该页该调大 `--max-tokens`（**§20.5 之后默认值已是端点上限**；此时再截断说明撞的是模型自身生成长度，只能拆页或换模型）。

### 19.3 验收（21 项断言全过，全部离线复现上述形态）

- **裸换行**：`{"transcription_md": "第一行␊第二行"}` → 自动转义后解析成功，标 `repaired: true`
- **截断**：切在字符串中间 → 保住半截文本；切在 `"title": ` 之后 → 丢掉不完整键值，仍返回可用对象；两种情况都标 `truncated: true`
- **纯文字回复** → 报错带原文预览（`抱歉…`）；**正文在 `reasoning_content`** → 能正确取到
- **端到端**：第 1 页 A 路裸换行、B 路纯文字（触发纠偏重试后成功）、第 2 页 A 路截断 + `finish_reason=length` → **全部落盘、退出码 0**，`json_repaired` / `json_truncated` / `finish_reason` 都记对
- **持续失败** → 退出码 3，错误行里能看到模型原文（便于定位）

### 19.4 下一步

直接重跑同一页即可（已完成的页会自动跳过，不重复计费）：

```bash
python pdf-ocr/2_ocr.py --category LL11512 --pages 3
```

如果仍失败：日志现在会带**模型原文预览**与 `finish_reason`；若预览显示是**半截 JSON**，说明被截断 → 调大 `--max-tokens`（如 `--max-tokens 8192`）；若是**纯文字**，把预览发我，按模型的真实回复形态再调整提示词。

---

## 20. 真实冒烟记录（第二次：S3 比对阶段）

**跑法**：`python pdf-ocr/3_merge.py --category LL11512 --budget-tokens 200000`（首跑 37 页全部 ✗）

### 20.1 拦路错误：模型名不存在

```
HTTP 400：{"error":{"message":"The supported API model names are deepseek-flash,
deepseek-v4-pro, but you passed deepseek-v4.1-flash."}}
```

**排查**（不猜，直接问端点）：

```bash
python -c "import json,urllib.request; ... 'https://api.deepseek.com/v1/models'"
# → {"data":[{"id":"deepseek-flash"},{"id":"deepseek-v4-pro"}]}
```

该端点**只有两个名字**，原定的 `deepseek-v4.1-flash` 是想象出来的版本号。

| 处置 | 位置 |
|---|---|
| 默认值与 `.env` 全部改为 **`deepseek-flash`**（flash 档，对应"比对用便宜快模型"的初衷） | `.env`、`.env.example`、`pdf-ocr/_common.py` `DEFAULTS` |
| 想换 pro 档只改 `.env` 一行 `DEEPSEEK_MODEL=deepseek-v4-pro`（实测同样可用，延迟约 2×） | — |

> 顺带发现：`.env.example` 里既有的 `ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash` 也是不存在的名字，`scripts/enrich-explanations.mjs` 若要真跑得先确认可用模型。本轮按要求未动该文件的那三行。

### 20.2 更隐蔽的坑：这个端点只提供**推理模型**

连上以后 `call.usage` 里出现 `completion_tokens_details.reasoning_tokens`（第 1 页 103、第 3 页 874），
且极小 `max_tokens` 的探针回复是 `content: ""` + `reasoning_content: "<思维链>"` + `finish_reason: "length"` ——
即 **max_tokens 会先被思维链吃掉**，正文可能一个字符都不剩。

`response_text()` 恰好会把 `reasoning_content` 当正文兜底，于是这种情况的表现是
`回复里找不到 JSON 对象（原文预览：我先想想……）` —— 和 §19.2 里查了三轮的那个坑一模一样，
只是这次根因是 **max_tokens 不够**，不是模型话痨。

**修复**：
- 拆出 `response_text_ex(response)` → `(正文, 取到的字段名)`，`response_text()` 变为薄封装；
- 新增 `reasoning_hint(field, finish_reason, max_tokens, stage_key)`，在 `2_ocr.py` / `3_merge.py` 的 JSON 解析失败处拼接；
- 报错现在直接写出结论与动作，且会看**是否已经顶到端点上限**再给建议（§20.5 之后默认值就在上限上）：
  - 未到顶：`…；正文为空、只取到 reasoning_content，且 finish_reason=length ——该模型是推理模型，max_tokens 被思维链吃光：请调大 --max-tokens 重跑（当前 8192）`
  - 已到顶：`…：当前 --max-tokens 393216 已经是该端点允许的上限，加大没用 —— 这是模型侧的生成长度限制，只能拆页/拆请求或换模型`

**验收**：离线断言 19 项（`content` 优先 / 退到 `reasoning*` / 分段数组 / 裸 `text` / 未到顶与已到顶两种 hint / 5 种非法 `--max-tokens` 被拒）+
集成桩（假 `post_json` 只回思维链、`finish_reason=length`）→ 默认值确实进了请求体、重试 1 次即停且报错含"加到顶也没用"的结论；
随后**真实重跑** page 1 `--force` 走通正常路径（`✓ 1914ms 提取 0 题`）。

### 20.3 首次成功的比对结果（pages 1–3）

```bash
python pdf-ocr/3_merge.py --category LL11512 --pages 0-3 --budget-tokens 200000
# ✓ 1/3 提取 0 题  ✓ 2/3 提取 0 题  ⚠ 3/3 提取 0 题（冲突 5）  合计 5.5k tok
```

**0 题不是 bug**：LL11512 的 1–3 页是封面 / 目录 / 教員紹介，本来就没有题目
（第 1 页 `a` 路转写只有标题、第 3 页是山下茂老师的履历）。第 3 页那 5 条 `conflicts` 全是
`question: 0` 的**整页级分歧**（`paper_identity.title`、阅读顺序、Markdown 强调风格），
模型都给了 `chosen` + `reason`，并且**没有**把版面排版差异误判成内容差异 —— 判定逻辑符合 §7.2。

实测单页成本（对 §18 估算的校正）：

| 页 | OCR-A | OCR-B | merge | 备注 |
|---|---|---|---|---|
| 1 | 3783 | — | 928 | 封面，入参极短 |
| 3 | 6388 | — | 3629 | 密排页；merge 里 874 是思维链 |

### 20.4 遗留风险（下一步要盯）

1. **OCR 截断**：第 3 页 `a` 路曾出现 `finish_reason=length`、`json_truncated=true`（`completion_tokens` 顶到当时的 4096）。
   该页转写末尾恰好完整（截在收尾字段上），**但真正的密排题目页会丢末尾题目**。
   → 已由 §20.5 把默认上限放开解决；再出现 `json_truncated: true` 就说明撞的是**模型自身生成长度**，不是我们的截止线。
2. **跨页拼接与题号抽取尚未验证**：1–3 页无题，`4_build_md.py` 只有拿真实题目页才能验收。
   → 建议先跑 `--pages 4-8`（OCR 两路 + merge）攒出含题页面，再进 P4。

### 20.5 `--max-tokens` 放开到端点上限（用户要求）

**要求**：`--max-tokens` 改为 `10000000`。

**先探边界再改**（两个端点逐个试，`max_tokens` 只影响请求体，探针只花几十 token）：

| 端点 | 8192 | 65536 | 131072 | 393216 | 393217 | 10000000 | 2000000000 |
|---|---|---|---|---|---|---|---|
| `deepseek-flash` | OK | OK | OK | OK | ❌ 400 | ❌ 400 | — |
| `step-3.7-flash` | OK | OK | — | — | — | OK | OK |

- DeepSeek 的报错把合法区间直接写出来了：`the valid range of max_tokens is [1, 393216]`。
  **所以 merge 阶段不能写 10000000** —— 那会让 37 页各报一次 HTTP 400（4xx 不重试，但照样刷屏）。
- StepFun **完全不做校验**（连 2000000000 都照收），10,000,000 是我们自设的兜底值。

**结论（已实现）**：

```python
DEFAULT_MAX_TOKENS = {"ocr": 10_000_000, "merge": 393_216}
MAX_TOKENS_CEILING = {"ocr": 10_000_000, "merge": 393_216}
check_max_tokens(stage_key, value)   # 发请求前校验，超上限 → 退出码 1 + 写明合法区间
```

`--max-tokens` 的 help 文本、`reasoning_hint()` 的建议、以及 OCR 截断告警（原先写"请加大 --max-tokens"）
都已同步改成"已是端点上限"的说法，避免给出一个照做也没用的建议。

**关于"放开会不会更贵"**：不会。`max_tokens` 是**截止线不是预扣**，没生成的 token 不计费；
数字大一点唯一的效果是"模型真话痨/死循环时最多能产多少"。所以主闸门仍然是
`--budget-tokens` / `--total-budget-tokens`（每次调用**之前**检查）。

**验收**：
- 离线断言 **19 项**全过（含 `check_max_tokens` 的 5 种非法输入：`merge` 写 10000000 / 393217、`ocr` 写 10000001、写 0、写负数）
- `3_merge.py --max-tokens 10000000` → 退出码 **1**，**一次请求都没发**，报错写明 `DeepSeek 的合法区间是 [1, 393216]…`
- `2_ocr.py --max-tokens 10000000` → 校验通过（继续走后面的流程）
- 默认值真实冒烟：`merge --pages 1 --force` → `✓ 3254ms`、`finish_reason=stop`、996 tok（其中思维链 210）；
  `ocr --pages 1 --passes a --force` → `✓ 9522ms`、`finish_reason=stop`、`json_truncated=false`、3.3k tok

**顺带发现的账本问题**：`source-manifest.json` 的 `usage` 累计块被重置过
（`created_at=11:59:35` 晚于页图的 `11:30:33`，说明该 manifest 是中途重建的），
所以"该分类累计"从 0 重新算起。代码本身没问题（S1 用 `manifest.update()` 会保留 `usage`，
`usage_block()/add_usage()` 也是累加，`read_json()` 坏了会抛错而不是静默返回空），
但**删掉/重建 manifest 就会丢历史账**，`--total-budget-tokens` 的跨运行约束随之复位 —— 别再手工删它。
页图与 `review/merge.json` 都在，不需要重跑任何阶段。

---

## 21. 真实冒烟记录（第三次：换 PDF、超时事件、4 页跑通）

### 21.1 换了 PDF，且沿用了旧分类名（⚠️ 先说这个）

12:34 的 S1 跑的是一个**新 PDF**：

| | 旧 | 新 |
|---|---|---|
| 文件 | `C:\Users\64247\Desktop\机组\1.pdf`（943KB） | `C:\Users\64247\Desktop\机组\软国计组期中2026-电子签名.pdf`（151KB） |
| 页数 | 37 | **4** |
| sha256 | `e13843b2…` | `27fcf19a…` |
| 分类名 | `LL11512` | `LL11512`（**同名**） |

S1 不删文件，但 `work/LL11512/` 是被**重建**的（manifest 的 `created_at` 与 4 张页图都是 12:33–12:34，
`usage` 键整个消失），所以**旧的 37 页页图 + 已做的 3 页 OCR/merge 已经不在了**。
源 PDF 还在，随时能重跑（代价见 §21.4 的 24.2k token/页 → 37 页约 **0.9M**）。

→ 建议：**一个试卷一个分类名**。否则 `data/raw/<分类名>/<分类名>.md` 会把两份卷子写到同一个名字下。

### 21.2 超时事件：`ReadTimeout … 181015ms`

```
[S2/4 OCR-A] page   1/4 (1/4)  ↻ 重试 1/3（ReadTimeout: HTTPSConnectionPool(host='api.stepfun.com', port=443): Read timed out. (read timeout=180)）181015ms
```

**诊断（用跑完的产物反查，不猜）**：

| 事实 | 数值 |
|---|---|
| 该页重试后 | **29,035ms 成功**（`finish_reason=stop`） |
| 同一 PDF 后续 7 次调用 | 25.7s / 29.2s / 36.3s / 40.0s / 44.1s / 49.8s / 61.1s，**全部成功** |
| 8 次 OCR 出参 | 4,162 ~ 8,210 tokens |

→ 这是 **StepFun 偶发卡住**（180s 里一个字节都没回），**不是** `max_tokens=10000000` 让模型不收敛：
同一个默认值下 8 次调用全部自然收尾，出参离上限差**三个数量级**。
→ 结论：`--timeout 180` **保持不动**（已经是正常耗时的 4~6 倍，调大只会让真卡住的请求多等）；
**看到这行不要手动中断** —— 脚本会自动重试，重试一般 30s 内就回来。

**修复（体验层面）**：新增 `_common.is_timeout()` / `timeout_hint()`；
`2_ocr.py` / `3_merge.py` 在首次超时后补一行：

```
[提示] 这是服务端偶发卡住（本 PDF 正常单页 30~45s），不是参数/密钥问题；脚本会自动重试，重试一般 30s 内就返回，不用手动中断
```

验收：离线桩（第 1 次抛 `ReadTimeout`、第 2 次返回正常 JSON）→ 输出里出现重试行 + 提示行、最终成功；
`is_timeout()` 对 `ReadTimeout` / `ConnectTimeout` / `HTTP 400` / 空串 判定正确。

### 21.3 4 页全流程跑通（S2 + S3 完成）

```bash
python pdf-ocr/2_ocr.py  --category LL11512 --pages 1-4      # 8 次调用
python pdf-ocr/3_merge.py --category LL11512 --pages 1-4     # 4 次调用
```

- S2：4 页 × 2 路 **全部 ✓**，`finish_reason=stop`、`json_truncated=false`（一次都没截断）。
- S3：4 页共提出 **41 题**（9 / 12 / 12 / 8），`answerKey` 从题干里的 `（ B ）` 正确提取；
  只有 page 1 第 8 题（选项 D 是 15 位还是 16 位）被标 `needs_review` —— 这正是双路互校该抓的。
- 唯一"噪音"是 page 1 的 `printed_page_labels` 分歧（A 写 `第1页`、B 写 `试题第1页`，其实是同一件事）。

### 21.4 实测成本（替换 §18 的估算，这才是能拿去算账的数）

| 阶段 | 调用 | 耗时 | token 合计 | **单页** |
|---|---|---|---|---|
| S2 OCR-A | 4 | 25.7~49.8s | 28.8k | — |
| S2 OCR-B | 4 | 29.2~61.1s | 31.9k | — |
| S2 小计 | 8 | | **60.7k** | **15.2k / 页** |
| S3 merge | 4 | 12.1~38.1s | **36.0k** | **9.0k / 页** |
| **合计** | 12 | 约 5.5 分钟 | **96.7k** | **24.2k / 页** |

**这组数据反过来证明了 4096 / 6144 确实太小**（也就是 §20.5 放开上限是对的）：

| 阶段 | 出参（逐次） | 旧默认 | 会截断几次 |
|---|---|---|---|
| OCR | 4162 / 4490 / 6653 / 7296 / 4162 / 8210 / 7152 / 5386 | 4096 | **8 次里 6 次** |
| merge | 3081 / 7897 / 7447 / 9192 | 6144 | **4 次里 3 次** |

另外：merge 的出参里**思维链占了 40%~86%**（1241/3081、5575/7897、4784/7447、7879/9192）——
推理模型的这部分开销是实打实的钱，而且**不计入"看得见的题面长度"**。

### 21.5 顺带修掉的重复 conflict

`structural_diffs()` 的确定性补漏不检查模型是不是已经报过同一个字段，
于是 page 1 的 `paper_identity.date` 在 `conflicts[]` 里**出现了两次**（一条带 `chosen`、一条 `chosen=""`）。

修复：按 `field` 去重，模型已报过的不再补。验收：`--pages 1 --force` 重跑 →
`conflicts` **4 → 3 条**，`notes` 从"另发现 2 处"变成"另发现 1 处"，9 题 / 1 题待复核不变。

---

## 22. P6 预备调研：md 怎么变成网站上的一个页面

**结论：不会自动出现。** 这个站是固定路由 SPA + 代码里写死的分类清单，**没有任何运行时发现机制**
（没有 `import.meta.glob('public/*.json')` 之类的动态扫描）。`data/raw/<分类名>/<分类名>.md` 在 P6 之前是一份没人读的文件。

### 22.1 第一道坎：现有 parser 都不吃这份 md

| 管线 | 读什么 | 写什么 |
|---|---|---|
| 日语线 `scripts/parse-japanese-2024-markdown.ts:379-380` | **markdown**，但路径**硬编码**成 `data/raw/japanese/2024年日语期末试卷.md` | `public/question-bank.json` |
| 计算机组成线 `scripts/parse-computer-banks.mjs:148,156` | `data/raw/computer-organization/<key>.json`（**手写 JSON，不是 markdown**） | `public/<key>-question-bank.json` |
| 其它 | `parse-markdown.ts:311` / `parse-word-markdown.ts:243-251` 硬编码单文件；`parse-history|party|military-markdown.ts:514/331/438` 扫目录 | 各自一个 `public/*-question-bank.json` |

### 22.2 第二道坎：分类必须在代码里注册

| 必改 | 位置 | 不改会怎样 |
|---|---|---|
| ① `Category` 联合类型 | `src/types/question.ts:1-9` | TS 编译失败 |
| ② `CATEGORIES` 数组 | `src/config/categories.ts:16` | 落地页不会出现这张卡片 |
| ③ `COURSE_TREE` 叶子 | `src/config/courseTree.ts:15` | 侧栏/课程树里点不进去 |
| ④ 硬编码 key 列表的测试 | `src/config/categories.test.ts:11-20` | `vitest` 直接红 |
| ⑤ `_meta.json` 生成名单 | `scripts/generate-meta.mjs:16-24` | 首页题数显示 0，并**回退全量加载所有题库**（~8MB） |

| 可选 | 位置 | 作用 |
|---|---|---|
| `parse:all` 挂载 | `package.json:40` | 一条命令重建全部 |
| SW 缓存版本 | `scripts/bump-sw-cache.mjs` | 不 bump 用户可能吃到旧缓存 |
| `public/sitemap.xml` | — | SEO |

**"新页面"的准确含义**：`src/router/index.ts` 只有 `/`、`/home`、`/quiz` 等固定路由，**没有 per-category 页面**。
落地页多一张学科卡片（`LandingPage.vue:99-102` 直接 `v-for CATEGORIES`），点进去 = `setActiveCategory(cat)` + `router.push('/home')`（`:86-89`）。

### 22.3 好消息：注册之后，题组/题单是自动的

`HomePage.vue:519-538`：题单列表 = `groupOrder` 里列的 + **其余自动追加**：

```js
for (const g of groups) if (!seen.has(g.groupId)) ordered.push(...)   // 533-537 行
```

`groups` 来自 `getAllGroups()`，从题目自身的 `groupId/groupTitle` 聚合。所以 md 里的 `## 题组一：…`
会自动变成一张题单，`groupOrder` 只用来控制排序。

### 22.4 决定：沿用既有解析逻辑，不重写解析器

三条 md 管线用的是**三套不同方言**：

| parser | 题目标题正则 | 答案格式 |
|---|---|---|
| **japanese-2024（＝ 本项目的硬契约 §2.1）** | `### 第N题` + `#### 题目` | `**正确答案：X text**` |
| history | `#### 12. stem`（`parse-history-markdown.ts:82`） | `**答案：xxx**` |
| military | `**12. stem**`（`parse-military-markdown.ts:109`） | `**答案**：xxx` |

→ **不能**直接把 history/military 那两个"扫目录"的脚本套上来（一条正则都不匹配）。
能沿用的是 japanese 那条，而本项目的 md 契约本来就是照它定的。`parse-japanese-2024-markdown.ts:211` 的
`parseMarkdown(filePath)` **已经按路径传参**，逐条核对可复用度：

| 部件 | 行 | 能否直接用 |
|---|---|---|
| `parseMarkdown(filePath)` | 211 | ✅ 直接调 |
| 题组 `## 题组一：` → `g01`（中文数字推导） | 225-230, 365-370 | ✅ 无硬编码 |
| 块切分 `### 第N题` / `#### 题目` / `#### 答案与解析` | 219, 240-260 | ✅ 就是硬契约 |
| 选项 `^[A-D][\.\s、]` + 从解析段兜底取选项 | 277, 304, 312-321 | ✅ |
| 答案 `**正确答案：X text**` + 表格兜底 | 323, 330 | ✅ |
| 阅读题 `**文章：**` / `### 文章（X）` 挂载 | 170-198, 350-351 | ✅ 试卷无阅读题时空转 |
| `tagQuestion()` 日语语法点表 | 33-… | ⚠️ 无意义 → 返回空 `tags` |
| `GROUP_OFFSET = 20`、`g01→g21` 偏移 | 376, 388-389 | ❌ 为避开学习通 g01–g10，必须去掉 |
| `id = g21-q01`、`groupTitle = '2024 · …'` | 391-393 | ❌ 改成本项目的命名 |
| 路径 + 合并进 `public/question-bank.json` + `filter(g2*)` | 379-380, 419 | ❌ 换成自己的库文件 |

**P6 落地形态（最小改动）**：

```
scripts/lib/parse-jp-exam-md.ts           ← 原样搬 parseMarkdown + extractArticles + CHINESE_NUM_MAP
scripts/parse-<key>-md.ts                 ← ~40 行：读 data/raw/<分类名>/<分类名>.md → 调 parseMarkdown → 写 public/<key>-question-bank.json
scripts/parse-japanese-2024-markdown.ts   ← 改成 import 共享版（防两份正则漂移）
```

字段映射（唯一需要动脑的地方）：

```ts
id: `${key}-q${String(q.numberInGroup).padStart(3, '0')}`,  // 必须全局唯一 + 必须以 <key>- 开头
category: key,
groupId: `${key}-${q.groupId}`,
groupTitle: q.groupTitle,
grammarPoints: [], tags: [],
status: 'ready',
answerProvenance: 'printed',                                // 答案印在卷面上（（ B ）那种）
source: { file: '<分类名>.md', group: q.groupTitle, position: i + 1 },
```

**免费的回归测试（黄金样本）**：`data/raw/japanese/2024年日语期末试卷.md` + `public/question-bank.json` 里现成的
g21–g28。先把 `parseMarkdown` 搬到共享模块，再拿那份 md 跑一遍，只取 `groupId.startsWith('g2')` 的题目，
与现有库**逐字节对比** —— 一致即证明搬迁没坏。**不花 API 钱，也不碰新试卷。**

### 22.5 P6 硬要求（是代码在查，不是约定）

1. `id` **必须以 `<categoryKey>-` 开头**：`parse-computer-banks.mjs:24` 校验它，`LandingPage.vue:65` 靠它匹配进度 ——
   不满足则卡片永远显示"未做"。
2. 每题 `groupId` 必须在 `groups` 里存在且 `groupTitle` 一致。
3. **`### 第N题` 的 N 必须是卷面原题号**（这份期中卷是 1–41 连续编号，跨题组不断）。
   `parseMarkdown` 把它直接当 `numberInGroup`，所以这条**同时约束 P4 的 `4_build_md.py` 输出**：
   不要把每个题组重新从 1 编号。
4. 不改任何既有数据：新分类用自己的 `data/raw/<分类名>/` 与新 `public/<key>-question-bank.json`。

### 22.6 P6 待办清单

- [ ] `scripts/lib/parse-jp-exam-md.ts`（抽共享）+ 黄金样本逐字节回归
- [ ] `scripts/parse-<key>-md.ts` 薄入口 + `package.json` 加 `parse:<key>`
- [ ] `src/types/question.ts` 加 `Category`
- [ ] `src/config/categories.ts` 加条目（`bankFile` / `short` / `long` / `desc` / `icon` / `groupViewTitle`）
- [ ] `src/config/courseTree.ts` 加叶子
- [ ] `src/config/categories.test.ts` 更新硬编码 key 列表
- [ ] `scripts/generate-meta.mjs` 的 `banks` 加一行 → 重跑 `npm run generate:meta`
- [ ] `npm run build` + `vitest` 全绿；浏览器里实点一遍（落地页卡片 → `/home` → 刷题 → 错题本）




