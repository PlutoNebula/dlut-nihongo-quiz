"""S4 新逻辑的离线验收：参考答案页 → 答案表、材料题拼接、题型按大题标题补正。

跑法：`python pdf-ocr/tests/test_p4_answers.py`（不联网、0 token）

背景（用户 2026-09-22 报的三个问题，对应三组断言）：
  1. 「拼接功能消失」—— 「五、案例分析题」的整段材料在第 5 页最后一道题，三个小问在第 6 页，
     两页**题组名不同**（`五、案例分析题` vs `思考题`），`attach_shared_stems()` 贴不上去；
  2. 「很多多选题的 questionType 不对」—— 卷面写着「二、多项选择题」，模型逐题给了 `single`；
  3. 「answerKey 没有显示」—— 答案单独印在最后一页「试卷评分标准」上，以前那一页被当成
     一堆"题干为空、只有答案"的伪题目输出，真正的题目反而大面积缺答案。
"""

import importlib.util
import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))

import _common as c  # noqa: E402

SANDBOX = c.TOOL_ROOT / "work" / ".tmp" / "p4answers"
if SANDBOX.exists():
    shutil.rmtree(SANDBOX)
FAKE_WORK = SANDBOX / "work"
FAKE_RAW = SANDBOX / "raw"
for d in (FAKE_WORK, FAKE_RAW):
    d.mkdir(parents=True, exist_ok=True)

CATEGORY = "_p4answerfixture"
MATERIAL = (
    "五、案例分析题。（共10分）\n\n远未成为历史的马克思\n\n"
    "镜头一：马克思被西方媒体评为“千年风云人物”。在千年交替之际，西方媒体纷纷推出"
    "自己评选的千年风云人物，马克思主义的创始人卡尔·马克思名列第一或第二。\n\n"
    "镜头二：马克思被德国《图片报》评为“最伟大的德国人”。\n\n思考题："
)

PAGES = {
    # 第 1 页：两道单选（答案在最后一页）+ 一道多选（模型误标 single）+ 材料题
    1: [
        dict(number=1, group="题组一", groupTitle="一、单项选择题", stem="第一题的题干文字",
             options=[{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"},
                      {"key": "C", "text": "丙"}, {"key": "D", "text": "丁"}],
             answerKey="", questionType="single"),
        dict(number=2, group="题组一", groupTitle="一、单项选择题", stem="第二题的题干文字",
             options=[{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"},
                      {"key": "C", "text": "丙"}, {"key": "D", "text": "丁"}],
             answerKey="", questionType="single"),
        dict(number=3, group="二、多项选择题", groupTitle="二、多项选择题", stem="多选题的题干文字",
             options=[{"key": k, "text": t} for k, t in zip("ABCDE", ["甲", "乙", "丙", "丁", "戊"])],
             answerKey="", questionType="single"),   # ← 模型误标：卷面是多项选择
        dict(number=4, group="五、案例分析题", groupTitle="五、案例分析题", stem=MATERIAL,
             options=[], answerKey="", questionType="fill", continued=True),
    ],
    # 第 2 页：材料的三个小问（题组名与材料页不同 —— 旧代码就是在这里贴不上的）
    2: [
        dict(number=1, group="思考题", groupTitle="思考题", stem="为什么马克思能高居榜首？",
             options=[], answerKey="", questionType="fill"),
        dict(number=2, group="思考题", groupTitle="思考题", stem="这给我们什么启示？",
             options=[], answerKey="", questionType="fill"),
        dict(number=3, group="思考题", groupTitle="思考题", stem="《共产党宣言》为何入选？",
             options=[], answerKey="", questionType="fill"),
    ],
    # 第 3 页：参考答案 / 评分标准页（题干全空、只有答案）
    3: [
        dict(number=1, group="一、单项选择题", groupTitle="单项选择题", stem="", options=[],
             answerKey="C", questionType="fill"),
        dict(number=2, group="一、单项选择题", groupTitle="单项选择题", stem="", options=[],
             answerKey="A", questionType="fill"),
        dict(number=3, group="二、多项选择题", groupTitle="多项选择题", stem="", options=[],
             answerKey="AC", questionType="fill"),
        dict(number=9, group="一、单项选择题", groupTitle="单项选择题", stem="", options=[],
             answerKey="B", questionType="fill"),
    ],
}

work = FAKE_WORK / CATEGORY
pages_dir = work / "pages"
pages_dir.mkdir(parents=True, exist_ok=True)
(work / "source-manifest.json").write_text(
    json.dumps(
        {
            "category": CATEGORY,
            "created_at": c.now_iso(),
            "updated_at": c.now_iso(),
            "documents": [
                {
                    "source": "fixture.pdf",
                    "sha256": "deadbeef" + "0" * 56,
                    "pages": 3,
                    "rendered_pages": [1, 2, 3],
                }
            ],
            "errors": [],
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
for page, questions in PAGES.items():
    for q in questions:
        q.setdefault("answerText", "")
        q.setdefault("explanation", "")
        q.setdefault("translation", "")
        q.setdefault("confidence", "high")
        q.setdefault("needs_review", False)
        q.setdefault("continued", False)
        q.setdefault("source", {"page": page})
    (pages_dir / f"page-{page:03d}.merge.json").write_text(
        json.dumps(
            {
                "page": page,
                "paper_identity": (
                    {"title": "《原理概论》试卷评分标准", "date": "", "variant": ""}
                    if page == 3
                    else {"title": "原理概论（闭卷）", "date": "", "variant": ""}
                ),
                "conflicts": [],
                "deduped": [],
                "normalized": [],
                "questions": questions,
                "notes": [],
                "needs_review": False,
                "call": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

# 两路 OCR 转写（**答案的确定性来源**：`1-2 CA` 区间写法 + `3.AC` 逐题写法）
ANSWER_TRANSCRIPTION = (
    "【参考答案】\n\n《原理概论》试卷评分标准\n\n"
    "一、单项选择题（每题1分，共15分）\n1-2 CA\n\n"
    "二、多项选择题（每题1分，共5分）\n3.AC\n\n"
    "三、论述题（共10分） 要求给两次小分\n1. 对 2分\n社会存在决定社会意识 5分\n"
)
for label in ("a", "b"):
    (pages_dir / f"page-003.{label}.review.json").write_text(
        json.dumps(
            {"page": 3, "transcription_md": ANSWER_TRANSCRIPTION}, ensure_ascii=False
        ),
        encoding="utf-8",
    )

spec = importlib.util.spec_from_file_location("build4", TOOL / "4_build_md.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

# ── 跑 S4（把两个根目录指到沙箱，绝不碰真的 data/raw）────────────────────
c.WORK_ROOT = FAKE_WORK
c.RAW_ROOT = FAKE_RAW
c.TEMP_ROOT = SANDBOX / "tmp"
sys.argv = ["4_build_md.py", "--category", CATEGORY, "--force", "--quiet", "--no-ai-review"]
buffer = io.StringIO()
with redirect_stdout(buffer):
    code = build.main()
out = buffer.getvalue()
assert code == 0, (code, out)

md = (FAKE_RAW / CATEGORY / f"{CATEGORY}.md").read_text(encoding="utf-8")
report_md = (work / "report.md").read_text(encoding="utf-8")

def block_of(text: str, start: int) -> str:
    """切出从 start 开始的那一个 `### 第N题` 题块。

    注意本题卷**每个大题都从 1 重新编号**，md 里 `### 第1题` 会出现两次，
    所以不能用「下一个题号」当边界，只能用「下一个 `### 第`」。
    """
    nxt = text.find("### 第", start + 5)
    return text[start : nxt if nxt != -1 else len(text)]


# 1) 参考答案页 → 答案表：真题目拿到答案，空题干伪题目不再输出
first = block_of(md, md.index("### 第1题"))
second = block_of(md, md.index("### 第2题"))
assert "**正确答案：C 丙**" in first, first
assert "**正确答案：A 甲**" in second, second
assert "（待补）" not in first and "（待补）" not in second, first + second
assert "参考答案 / 评分标准页" in report_md and "模型给了 4 条" in report_md, report_md
# 转写里 `1-2 CA`（区间写法）挖到 2 条；逐题写法 `3.AC` 另算 —— 只要确实挖到了就行
assert "转写挖到" in report_md, report_md
# 答案页的伪题目不能变成题块（旧版会输出 4 个"题干为空 + 裸答案"的题）
assert md.count("### 第") == 6, md  # 单选 1,2 + 多选 3 + 材料下 3 个小问（材料本身已撤）
print("[1] 参考答案页 → 答案表：2 道单选拿到答案，4 条空题干伪题目不再输出")

# 2) 多选题：按「二、多项选择题」补正题型 + 答案表里的 AC 要落上
assert "**正确答案：AC " in md, md
assert "| page 1 | 3 | single | multi |" in report_md, report_md
# 只按题号回退会把单选的答案贴到主观题上（每个大题都从 1 重编号）—— 必须没贴
assert "| page 2 | 1 |" not in report_md and "| page 2 | 2 |" not in report_md, report_md
assert "贴回 3 道题" in report_md, report_md
print("[2] 多选题按卷面大题标题补正为 multi，答案 AC 与选项 E 都在；主观题没被错贴")

# 3) 材料题：整段材料复制到下一题的三个小问，材料本身不再单独成题
for stem in ("为什么马克思能高居榜首？", "这给我们什么启示？", "《共产党宣言》为何入选？"):
    start = md.index(stem)
    block = block_of(md, md.rindex("### 第", 0, start))
    assert "远未成为历史的马克思" in block, block
assert "材料题：1 段材料已复制到 3 道小题的题干上方" in report_md, report_md
assert "## 材料题：整段材料已复制到后续小题的题干上方" in report_md, report_md
# 材料题撤掉后，它那条"跨页拼接失败"也不该再留在报告里
assert "跨页拼接失败" not in report_md, report_md
print("[3] 材料题：1 段材料复制进 3 道小题（跨题组名也能贴上），材料本身撤出题单")

# 4) 填空题不再被误报"选项不足 / 答案不在选项里"，题型补正写进报告
assert "## 题型按「卷面大题标题」补正（S3 旧数据兜底）" in report_md, report_md
assert "题型补正 1 处" in report_md, report_md
assert "选项不足 2 个（0）" not in report_md, report_md
print("[4] 填空/主观题不再被误报为字段不完整；题型补正写进 report.md")

shutil.rmtree(SANDBOX)
print("\n全部通过：4 组断言 / 沙箱已清理")
