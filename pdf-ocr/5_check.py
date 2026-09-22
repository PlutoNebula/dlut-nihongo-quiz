"""S5：离线契约校验（硬门禁）—— 不调 API、不写盘。

用法：
  python pdf-ocr/5_check.py --category <分类名>      # 校验 data/raw/<分类名>/<分类名>.md
  python pdf-ocr/5_check.py --md <path/to/x.md>      # 校验任意 md

校验内容（docs/pdf-ocr-pipeline.md §9）：**逐条复用解析端 `scripts/parse-japanese-2024-markdown.ts` 的正则**，
不是"另写一套差不多的"，因为只有这样才能真的预演出"解析端会不会把这题静默丢掉"。

  1. 题块数 / 题号连续性（缺号、重号、乱序）
  2. 题组标题规范（`## 题组{一…十}：…`）
  3. 选项与答案（选项 ≥ 2；`**正确答案：X …**` 存在且 X 落在选项里）
  4. **会被解析端丢弃的题**（按 `:349` 条件预演，逐题报出原因）
  5. 截断风险（解析区内混入 `## ` 开头行 / `### 本组核心知识点总结`）
  6. 待复核标记（`> ⚠ 待核对：…`）统计

退出码：0 全绿；2 只有警告；3 有硬错误（会被丢弃 / 重号 / 题组标题不合规 / 没有题块）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as c  # noqa: E402

# ── 解析端正则（逐条对齐 scripts/parse-japanese-2024-markdown.ts）──────────
BLOCK_SPLIT = re.compile(r"(?=### 第\d+题)")
QUESTION_HDR = re.compile(r"^### 第(\d+)题")
# 行首锚定版：用来判断"这一行 `## ` 是不是合规的题组标题"（按行扫，带 re.M）
GROUP_HDR = re.compile(r"^## 题组([一二三四五六七八九十])[：:]")
# 块内查找版：**不能带 `^`**！解析端用的是 `block.matchAll(/## 题组…/g)`（:225/:365），
# 一个题块里除了自己的内容还夹着"下一题的题组标题"，带锚点就会一个都找不到。
GROUP_IN_BLOCK = re.compile(r"## 题组([一二三四五六七八九十])[：:]")
OPTION_LINE = re.compile(r"^([A-D])[\.\s、]+(.+)")
OPTION_TEST = re.compile(r"^[A-D][\.\s、]")
ANSWER_BOLD = re.compile(r"\*\*正确答案[：:]\s*([A-D])\s*(.+?)\*\*")
ANSWER_PLAIN = re.compile(r"正确答案[：:]\s*([A-D])\s*(\S+)")
META_BOUNDARY = re.compile(r"(?:^|\n)(?:##\s|### 本组核心知识点总结)")
CN_NUMERALS = "一二三四五六七八九十"
# 题组标题里自己写的题数声明（如「数值转换题（共10题）」）
COUNT_RE = re.compile(r"共\s*(\d+)\s*[题問问]")


def group_count_check(content: str) -> tuple[dict[str, int], dict[str, int]]:
    """→ (每个题组的实际题数, 标题里声明的题数)。

    题组标题**跨页**才看得出全貌（一个题组可能横跨两页），所以这条在 S5 按整份 md 数。
    """
    actual: dict[str, int] = {}
    declared: dict[str, int] = {}
    current = ""
    for block in BLOCK_SPLIT.split(content):
        if QUESTION_HDR.match(block):
            actual[current] = actual.get(current, 0) + 1
        for match in GROUP_IN_BLOCK.finditer(block):
            current = match.group(1)
    for line in content.split("\n"):
        match = GROUP_HDR.match(line)
        if not match:
            continue
        count = COUNT_RE.search(line)
        if count:
            declared[match.group(1)] = int(count.group(1))
    return actual, declared


def extract_articles(content: str) -> dict[int, str]:
    """公共题干（"文章"）→ 它覆盖的题号。**逐行镜像解析端 `extractArticles`（:150-209）**。

    `**文章：**` 或 `### 文章（X）` 声明的文本会**粘住**：其后每一道题都拿到同一段，
    直到遇到 `## ` 标题（或被新的声明替换）。所以同一段公共题干覆盖整个题组的小题。
    """
    mapping: dict[int, str] = {}
    current = ""
    in_article = False
    for raw in content.split("\n"):
        trimmed = raw.strip()
        if re.match(r"^##\s", trimmed):
            current = ""
            in_article = False
            continue
        if re.fullmatch(r"\*\*文章[：:]\*\*", trimmed):
            in_article = True
            current = ""
            continue
        if re.match(r"^###\s+文章[（(]", trimmed):
            in_article = True
            current = ""
            continue
        match = re.match(r"^###\s+第(\d+)题", trimmed)
        if match:
            body = current.strip()
            if body:
                mapping[int(match.group(1))] = body
            in_article = False
            continue
        if trimmed == "---":
            in_article = False
            continue
        if in_article:
            current += raw + "\n"
    return mapping


def parse_like_parser(content: str) -> tuple[list[dict], list[dict]]:
    """按解析端的算法把 md 走一遍。

    返回 (questions, drops)：
      * questions：**能被解析端收下**的题（字段已按解析逻辑处理过）
      * drops：题块存在、但会被 `:349` 静默丢弃的题（附原因）
    """
    questions: list[dict] = []
    drops: list[dict] = []
    current_group = ""
    article_by_q = extract_articles(content)

    for block in BLOCK_SPLIT.split(content):
        hdr = QUESTION_HDR.match(block)
        if not hdr:
            # 非题块：可能是文件头，或题组标题所在的那一段
            matches = list(GROUP_IN_BLOCK.finditer(block))
            if matches:
                current_group = matches[-1].group(1)
            continue

        number = int(hdr.group(1))
        body = re.sub(r"^### 第\d+题\s*\n*", "", block)
        stem = ""
        exp_section = ""
        if "#### 题目" in body:
            parts = re.split(r"####\s+", body)
            in_stem = in_exp = False
            for part in parts:
                if part.startswith("题目"):
                    in_stem, in_exp = True, False
                    stem = re.sub(r"^题目\s*\n*", "", part).strip()
                    continue
                if part.startswith("答案与解析"):
                    in_stem, in_exp = False, True
                    exp_section = re.sub(r"^答案与解析\s*\n*", "", part).strip()
                    continue
                if in_stem:
                    stem += "\n" + part
                if in_exp:
                    exp_section += "\n" + part

        truncated_at = None
        boundary = META_BOUNDARY.search(exp_section)
        if boundary:
            # 边界行是**合规的题组标题**（`## 题组X：`）时，它本来就该出现在两题之间，不算截断：
            #   解析端在这里切断 expSection，切掉的只是"下一题的题组标题 + 该题组导言"，不是本题解析。
            #   只有边界是**别的东西**（散落的 `## `、`### 本组核心知识点总结`）时，
            #   才说明本题解析真的会被静默切掉一段。
            line_end = exp_section.find("\n", boundary.end())
            boundary_line = exp_section[boundary.start() : line_end if line_end != -1 else None].strip()
            tail = exp_section[line_end:] if line_end != -1 else ""
            substantive = [
                line for line in tail.split("\n") if line.strip() and not line.strip().startswith("#")
            ]
            if substantive and not GROUP_HDR.match(boundary_line):
                truncated_at = boundary_line[:60]
            exp_section = exp_section[: boundary.start()].rstrip()

        clean_stem = ""
        option_lines: list[str] = []
        found_options = False
        for line in stem.split("\n"):
            trimmed = line.strip()
            if not trimmed:
                continue
            if OPTION_TEST.match(trimmed):
                found_options = True
                option_lines.append(trimmed)
                continue
            if trimmed.startswith("#") or trimmed.startswith(">"):
                continue
            if not found_options:
                clean_stem += (clean_stem and " " or "") + trimmed

        options: list[dict] = []
        for line in option_lines:
            match = OPTION_LINE.match(re.sub(r"^([A-D])[\.\s、]+", r"\1 ", line))
            if match:
                options.append({"key": match.group(1), "text": match.group(2).strip()})

        if not options and exp_section:
            for line in exp_section.split("\n"):
                match = OPTION_LINE.match(re.sub(r"^([A-D])[\.\s、]+", r"\1 ", line.strip()))
                if match:
                    options.append({"key": match.group(1), "text": match.group(2).strip()})

        answer_key = ""
        match = ANSWER_BOLD.search(exp_section)
        if match:
            answer_key = match.group(1)
        else:
            match = ANSWER_PLAIN.search(exp_section)
            if match:
                answer_key = match.group(1)

        item = {
            "number": number,
            "group": current_group,
            # 解析端存库的题干是 `文章 + \n\n + cleanStem`（`:350-351`）
            "stem": f"{article_by_q[number]}\n\n{clean_stem}" if number in article_by_q else clean_stem,
            "cleanStem": clean_stem,
            "article": article_by_q.get(number, ""),
            "options": options,
            "answerKey": answer_key,
            "truncatedAt": truncated_at,
            "hasAnswerLine": bool(ANSWER_BOLD.search(exp_section) or ANSWER_PLAIN.search(exp_section)),
        }
        # `parse-japanese-2024-markdown.ts:349` 的收录条件（注意它判的是 cleanStem，不含文章）
        reasons = []
        if not clean_stem:
            reasons.append("题干为空")
        if len(options) < 2:
            reasons.append(f"选项不足 2 个（{len(options)}）")
        if not answer_key:
            reasons.append("缺 `**正确答案：X …**`")
        if reasons:
            drops.append({**item, "reasons": reasons})
        else:
            questions.append(item)

        # 题块末尾的题组标题归属下一题（对齐 :365-370）
        for match in GROUP_IN_BLOCK.finditer(block):
            current_group = match.group(1)

    return questions, drops


def main() -> int:
    parser = argparse.ArgumentParser(description="S5：生成物 md 的离线契约校验（docs §9）")
    parser.add_argument("--category", help="分类名（读 data/raw/<分类名>/<分类名>.md）")
    parser.add_argument("--md", help="直接指定要校验的 md 路径")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总行")
    args = parser.parse_args()

    c.setup_stdio()
    c.set_quiet(args.quiet)

    if not args.category and not args.md:
        c.fail("必须给 --category 或 --md 之一", 1)
    if args.md:
        md_path = Path(args.md).expanduser().resolve()
    else:
        category = c.safe_name(args.category)
        md_path = c.RAW_ROOT / category / f"{category}.md"
    if not md_path.exists():
        c.fail(f"找不到 {md_path}", 1)

    content = md_path.read_text(encoding="utf-8")
    hard: list[str] = []
    soft: list[str] = []

    # ── 题块 / 题号 ──
    numbers = [int(m.group(1)) for m in re.finditer(r"^### 第(\d+)题\s*$", content, re.M)]
    if not numbers:
        hard.append("没有任何 `### 第N题` 题块")
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        hard.append(f"重复题号：{duplicates}")
    if numbers != sorted(numbers):
        soft.append("题号不是升序排列（解析端按出现顺序入库）")
    gaps: list[str] = []
    for prev, cur in zip(numbers, numbers[1:]):
        if cur != prev + 1:
            gaps.append(f"{prev}→{cur}")
    if gaps:
        soft.append(f"题号缺口 {len(gaps)} 处：{', '.join(gaps[:8])}")

    # ── 题组标题 ──
    group_lines = re.findall(r"^## .*$", content, re.M)
    bad_groups = [line for line in group_lines if not GROUP_HDR.match(line)]
    if bad_groups:
        hard.append(f"题组标题不合规 {len(bad_groups)} 行（必须 `## 题组{{一…十}}：`）：{bad_groups[:3]}")
    group_nums = [m.group(1) for line in group_lines if (m := GROUP_HDR.match(line))]
    if len(group_nums) != len(set(group_nums)):
        hard.append(f"同一个题组号出现多次（解析端会用最后一个）：{group_nums}")

    # ── 按解析端算法预演 ──
    kept, drops = parse_like_parser(content)
    if drops:
        hard.append(f"会被解析端丢弃的题 {len(drops)} 道")
    answer_not_in_options = [
        q for q in kept if q["answerKey"] and q["answerKey"] not in {o["key"] for o in q["options"]}
    ]
    if answer_not_in_options:
        soft.append(
            f"答案不在选项里 {len(answer_not_in_options)} 题："
            + ", ".join(f"第{q['number']}题({q['answerKey']})" for q in answer_not_in_options[:8])
        )
    truncated = [q for q in kept + drops if q["truncatedAt"]]
    if truncated:
        soft.append(
            f"解析区被 `## ` 截断风险 {len(truncated)} 题（解析端会静默切掉后半段）："
            + ", ".join(f"第{q['number']}题" for q in truncated[:8])
        )

    # ── 题组题数与标题声明是否一致 ──
    actual_counts, declared_counts = group_count_check(content)
    mismatched = [
        (num, expected, actual_counts.get(num, 0))
        for num, expected in declared_counts.items()
        if actual_counts.get(num, 0) != expected
    ]
    if mismatched:
        soft.append(
            "题数与题组标题声明不符："
            + "；".join(f"题组{n} 声明 {e} 题实际 {a} 题" for n, e, a in mismatched)
        )

    # ── 公共题干（文章）覆盖 ──
    article_blocks = len(re.findall(r"(?m)^\*\*文章[：:]\*\*\s*$", content))
    articles = [q["article"] for q in kept + drops if q.get("article")]
    distinct_articles = {a for a in articles}
    covered_numbers = sorted({q["number"] for q in kept + drops if q.get("article")})
    if article_blocks > len(distinct_articles):
        soft.append(
            f"有 {article_blocks} 个 `**文章：**` 块，但只有 {len(distinct_articles)} 段被解析端接住"
            "（空块或后面没跟题目）"
        )
    if article_blocks:
        c.info(
            f"[信息] 公共题干：{article_blocks} 段 → 解析端会补到 {len(covered_numbers)} 道小题的题干前面"
            f"（{covered_numbers[0]}–{covered_numbers[-1]}）"
            if covered_numbers
            else f"[信息] 公共题干：{article_blocks} 段，但没有覆盖到任何题"
        )

    # ── 待复核标记 ──
    marks = re.findall(r"^> ⚠ 待核对：(.*)$", content, re.M)
    if marks:
        soft.append(f"带待复核标记 {len(marks)} 处")

    # ── 报告 ──
    c.info("")
    if drops:
        c.always("[会被丢弃]")
        for item in drops:
            c.always(f"  第 {item['number']} 题（题组 {item['group'] or '?'}）: {'；'.join(item['reasons'])}")
    if hard:
        for line in hard:
            c.always(f"[硬错误] {line}")
    if soft:
        for line in soft:
            c.always(f"[警告] {line}")

    status = "✗" if hard else ("⚠" if soft else "✓")
    c.always(
        f"[S5/5 校验] 契约检查：题块 {len(numbers)} / 题组 {len(set(group_nums))} / "
        f"会被丢弃 {len(drops)} / 公共题干 {article_blocks} 段 / "
        f"硬错误 {len(hard)} / 警告 {len(soft)}  {status}"
    )
    c.always(f"[S5/5 校验] {md_path.name} → 解析端可收下 {len(kept)} 题")
    return 3 if hard else (2 if soft else 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
