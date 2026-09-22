"""S4：把每页 merge.json 拼成一份可直接入库的 markdown（工作流第 4 步，**纯本地、不调 API**）。

用法：
  python pdf-ocr/4_build_md.py --category <分类名> [--pages 1-4] [--force] [--quiet]

输入：pdf-ocr/work/<分类名>/pages/page-00N.merge.json（S3 产物）
输出：
  * data/raw/<分类名>/<分类名>.md          ← 唯一最终产物（单文件，不分批）
  * pdf-ocr/work/<分类名>/transcription.md ← 每页转写汇总（人工复核用）
  * pdf-ocr/work/<分类名>/report.md        ← 待人工复核清单

做的事（docs/pdf-ocr-pipeline.md §8）：
  1. **题组归一化**：merge.json 的 group 字段实测很杂（'一、数值转换题' / '二' / '六、题组二' / ''），
     统一成"中文题组号 + 标题"；模型漏填的按"沿用上一题题组"补（跨页续题必然漏填）。
  2. **判断题补选项**：卷面自己写着「正确的选A，错误的选B」，但 OCR 抽出来 options=[]；
     不补的话解析端 :349 会把整题**静默丢弃**。补的内容直接来自卷面声明，不算编造。
  3. **公共题干复制到每道小题**（§8.4）：题组导言（含代码块/表格）会**复制**进该题组每一道小题的
     `#### 题目` 上方，让每个小问独立成题（单看任意一题都不缺上下文）。
  4. **跨页断题拼接**（§8.2 三步）。
  5. **全局按题号排序 + 去重**（§8.1）。
  6. **渲染 md**（§8.3）并复查字段完整性（题干非空 / 选项≥2 / 有答案），不满足只标 needs_review，**不丢题**。

退出码：0 成功；1 参数/环境错误；3 有页缺 S3 产物（已产出的部分照常写盘，可续跑）。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as c  # noqa: E402

CN_NUMERALS = "一二三四五六七八九十"
CN_RE = re.compile(r"([一二三四五六七八九十])")
# 解析端 CHINESE_NUM_MAP 只认「一…十」单个字，超过 10 个题组无法表达
MAX_GROUPS = len(CN_NUMERALS)

STAGE = c.STAGES[4]


# ── 小工具 ──────────────────────────────────────────────────────────────
def flatten(text) -> str:
    """压成一行。

    解析端本来就会把多行题干用空格拼起来（`parse-japanese-2024-markdown.ts:296`），
    压成一行既等价、又能避免"题干里某行以 `A.` 开头被误判成选项"。
    """
    return " ".join(str(text or "").split())


def sanitize(text) -> str:
    """去掉可能把题块切碎的行内 heading 记号（`#### ` / `### 第N题`），并压成一行。"""
    flat = flatten(text)
    return re.sub(r"#{2,}", "", flat).strip()


OPTION_LIKE = re.compile(r"^[A-Da-d][\.\s、]")


def sanitize_block(text) -> str:
    """题干清洗（**保留换行**）：公共题干里常有代码块/表格，压成一行就没法渲染了。

    两个必须处理的坑：
      * 行首 `#` 会被解析端跳过（`:289`）→ 去掉行首的井号；
      * 行首 `A.` 这种会被解析端**当成选项**、还会把它后面的题干整段丢掉（`:277-297`）
        → 前面加个 HTML 注释挡一下（渲染时不可见）。
    """
    lines: list[str] = []
    for raw in str(text or "").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            line = re.sub(r"^\s*#+\s*", "", line)
            stripped = line.strip()
        if OPTION_LIKE.match(stripped):
            line = f"<!-- -->{line}"
        lines.append(line)
    return "\n".join(lines).strip()


def norm_stem(text) -> str:
    """去重键用的归一化题干：去空白/标点/大小写（思路同 parse-history-markdown.ts）。"""
    flat = flatten(text).lower()
    return re.sub(r"[\s\W_]+", "", flat, flags=re.UNICODE)


def option_text(q: dict, key: str) -> str:
    for item in q.get("options") or []:
        if str(item.get("key") or "").strip().upper() == key:
            return flatten(item.get("text"))
    return ""


def info_score(q: dict) -> int:
    """判断"哪一份信息更全"，用于去重时保留更好的那份。"""
    return (
        len(q.get("options") or []) * 2
        + (2 if flatten(q.get("answerKey")) else 0)
        + (1 if flatten(q.get("answerText")) else 0)
        + (1 if flatten(q.get("explanation")) else 0)
        + (1 if flatten(q.get("translation")) else 0)
    )


# ── 1) 题组归一化 ───────────────────────────────────────────────────────
# groupTitle 里出现这些字样 / 长度超标 / 带代码围栏 → 它其实是"整段公共题干"而不是题组名
PASSAGE_TITLE_LIMIT = 40
PASSAGE_HINT = re.compile(r"以下|次の|空欄|答えよ|コード|配列|プログラム|文章|表を|問に|ソース")
CODE_FENCE = re.compile(r"```")
CN_PREFIX = re.compile(r"^\s*[一二三四五六七八九十]+\s*[、.．]\s*")
# `六、题组二` 这种"行首就是题组号"的写法（优先采信）
SECTION_PREFIX = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、.．]")
# `二` 这种光秃秃只有一个号码的写法
BARE_NUMERAL = re.compile(r"^\s*([一二三四五六七八九十]+)\s*$")
# 整页转写里的题组标题行：`六、题组二`
SECTION_LINE = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、.．]\s*(.+?)\s*$")


def looks_like_passage(text) -> bool:
    """这段文字像"公共题干"而不是"题组名"吗？"""
    raw = str(text or "")
    if len(flatten(raw)) > PASSAGE_TITLE_LIMIT:
        return True
    if CODE_FENCE.search(raw):
        return True
    return bool(PASSAGE_HINT.search(raw))


def title_from_group_field(raw) -> str:
    """从 `group` 字段里剥出真正的题组名：`'六、题组二'` → `'题组二'`、`'一、数值转换题'` → `'数值转换题'`。"""
    return CN_PREFIX.sub("", str(raw or "")).strip()


def transcription_section(pages_dir: Path, page: int, title: str) -> str:
    """在整页转写里找 `X、<title>` 形式的题组标题，返回它的中文题组号。

    为什么需要它：模型给 `group` 的写法不稳定 —— 实测同一页两次运行分别给出
    `'六、题组二'` 和 `'题组二'`。后者如果按"取第一个中文数字"就会被当成**题组二**，
    于是第 36/37 题会被并进前面真正的题组二里。转写里的 `六、题组二` 才是权威。
    """
    wanted = flatten(title)
    if not wanted:
        return ""
    for label in ("a", "b"):
        path = pages_dir / f"page-{page:03d}.{label}.review.json"
        if not path.exists():
            continue
        for line in str(c.read_json(path).get("transcription_md") or "").split("\n"):
            match = SECTION_LINE.match(line)
            if not match:
                continue
            head = flatten(match.group(2))
            if head == wanted or head.startswith(wanted) or wanted.startswith(head):
                return match.group(1)
    return ""


def resolve_numeral(
    raw: str, title: str, page: int, pages_dir: Path, report: dict
) -> str:
    """从 `group`/`groupTitle` 里定出题组号（按可靠性从高到低）。"""
    match = SECTION_PREFIX.match(raw)
    if match:
        return match.group(1)
    match = BARE_NUMERAL.match(raw)
    if match:
        return match.group(1)
    for candidate in (title, title_from_group_field(raw)):
        hit = transcription_section(pages_dir, page, candidate)
        if hit:
            return hit
    if raw or title:
        report["unresolved_groups"].append((page, raw[:20], title[:20]))
    return ""


def normalize_groups(entries: list[dict], pages_dir: Path, report: dict) -> list[str]:
    """给每题补 `numeral` / `group_title`，返回题组出现顺序。"""
    order: list[str] = []
    titles: dict[str, str] = {}
    current_num = ""
    current_title = ""

    for entry in entries:
        q = entry["q"]
        raw = str(q.get("group") or "").strip()
        title = str(q.get("groupTitle") or "").strip()
        numeral = resolve_numeral(raw, title, entry["page"], pages_dir, report)

        # 标题兜底：为空、或明显是"整段公共题干"时，改从 group 字段里取真正的题组名
        fallback = title_from_group_field(raw) or (f"题组{numeral}" if numeral else "")
        if looks_like_passage(title):
            entry["passage_candidate"] = title
            report["passage_in_title"].append((numeral or "?", len(flatten(title))))
            title = fallback
        elif not title:
            title = fallback

        if numeral:
            if numeral != current_num:
                current_num = numeral
                current_title = title or f"题组{numeral}"
                if numeral in titles and titles[numeral] != current_title:
                    report["group_title_conflicts"].append((numeral, titles[numeral], current_title))
                elif numeral not in titles:
                    titles[numeral] = current_title
                    order.append(numeral)
            elif title and title != current_title:
                # 同一个题组号出现了两个标题：以**第一次**看到的为准，记一笔
                report["group_title_conflicts"].append((numeral, current_title, title))
        elif not current_num:
            # 整份卷子一个题组号都没有 → 兜一个，别让题组标题缺失
            current_num, current_title = "一", title or "全部题目"
            order.append(current_num)
            titles[current_num] = current_title
            report["notes"].append("全卷没有中文题组号，已统一并入「题组一」")

        entry["numeral"] = current_num
        entry["group_title"] = titles.get(current_num, current_title)

    report["groups"] = [(n, titles.get(n, "")) for n in order]
    return order


# ── 2) 判断题补选项 ─────────────────────────────────────────────────────
JUDGE_HINT = re.compile(r"判断|正误|对错|○×|○|×")
JUDGE_OPTIONS = [{"key": "A", "text": "正确"}, {"key": "B", "text": "错误"}]
# 卷面把答案印在题干末尾时（`…である。 ( B )`），OCR 会把它一起抄进题干
TRAILING_ANSWER = re.compile(r"\s*[（(]\s*([A-Da-d])\s*[）)]\s*$")


def strip_trailing_answer_mark(entries: list[dict], report: dict) -> None:
    """题干末尾的 `（X）` 若与答案一致，就是卷面答案标记，不是题干内容 → 去掉。

    实测 39 题里有 3 题（第 22/23/24 判断题）带着它；只在**字母与答案完全一致**时才删，
    避免把题干里正常的括号（如填空的 `（　）`）误删。
    """
    for entry in entries:
        q = entry["q"]
        key = flatten(q.get("answerKey")).upper()
        stem = flatten(q.get("stem"))
        if not key or not stem:
            continue
        match = TRAILING_ANSWER.search(stem)
        if match and match.group(1).upper() == key:
            q["stem"] = stem[: match.start()].rstrip()
            report["answer_mark_stripped"].append((entry["page"], q.get("number")))


def fill_judgement_options(entries: list[dict], report: dict) -> None:
    """无选项 + 答案是 A/B → 按卷面声明补「A. 正确 / B. 错误」。

    只在**能看出是判断题**时才补：题组标题里出现「判断」等字样，或题目自带 judgement 类型。
    补不出来的一律原样保留（后面完整性复查会把它标成"会被 parser 丢弃"）。
    """
    for entry in entries:
        q = entry["q"]
        if q.get("options"):
            continue
        key = flatten(q.get("answerKey")).upper()
        if key not in ("A", "B"):
            continue
        hint = " ".join(
            [
                str(entry.get("group_title") or ""),
                str(q.get("groupTitle") or ""),  # 题组标题按"首次出现"锁定，后来那句更具体的也要看
                str(q.get("questionType") or ""),
                flatten(q.get("stem")),
            ]
        )
        if not JUDGE_HINT.search(hint):
            continue
        q["options"] = [dict(o) for o in JUDGE_OPTIONS]
        q["questionType"] = "judgement"
        if not flatten(q.get("answerText")):
            q["answerText"] = option_text(q, key)
        report["judgement_filled"].append((entry["page"], q.get("number")))


# ── 3) 公共题干（题组导言）挂载 ─────────────────────────────────────────
# 小题自己的文字只是占位符的形态：`(30) の選択肢：` / `選択肢：` / 光秃秃一个 `（30）`
PLACEHOLDER_STEM = re.compile(r"の選択肢|選択肢[：:]")
BARE_MARKER = re.compile(r"^[（(]?\s*\d+\s*[）)]?\s*[：:．.]?$")


def is_thin_stem(q: dict) -> bool:
    """题干是不是"只是个占位符、必须靠公共题干才成立"。

    注意**不能只看长度**：题组七的 `swは何形式か。` 只有 8 个字，但它自足 ——
    按长度判会把它误当成公共题干题组（实测踩过）。
    """
    stem = flatten(q.get("stem"))
    return bool(PLACEHOLDER_STEM.search(stem)) or bool(BARE_MARKER.match(stem))


def clean_passage(text) -> str:
    """公共题干里不能出现 `## ` 开头行（会被解析端当成题组标题、并截断解析）和 `---`（会中止文章累积）。"""
    lines = []
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("## ") or stripped == "---":
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def passage_from_transcription(pages_dir: Path, page: int, numeral: str, first_number: int) -> str:
    """从该页 OCR 转写里取"题组标题行 → 第一个小题行"之间的文本（公共题干）。

    实测题组五的导言（含 MIPS 代码块）只存在于整页转写里：merge.json 的 groupTitle 是 `题组一`，
    小题 stem 全是 `(30) の選択肢：`，导言本身没被任何字段接住 —— 只能回落到转写。
    """
    if not numeral or not first_number:
        return ""
    head_re = re.compile(rf"^\s*{re.escape(numeral)}\s*[、.．]")
    stop_re = re.compile(rf"^\s*{first_number}\s*[.．、]")
    for label in ("a", "b"):
        path = pages_dir / f"page-{page:03d}.{label}.review.json"
        if not path.exists():
            continue
        lines = str(c.read_json(path).get("transcription_md") or "").split("\n")
        start = None
        for index, line in enumerate(lines):
            if head_re.match(line.strip()):
                start = index
        if start is None:
            continue
        body: list[str] = []
        for line in lines[start + 1 :]:
            if stop_re.match(line.strip()):
                break
            body.append(line)
        fragment = clean_passage("\n".join(body))
        if fragment:
            return fragment
    return ""


def _common_prefix_len(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    return index


def strip_leading_passage(stem: str, passage: str) -> tuple[str, bool]:
    """题干开头若已经把公共题干抄了一遍，就切掉它（避免"文章 + 题干"里重复一遍）。

    两份文本来自不同的 OCR 路，标点/顿号会有差异，所以按**归一化后**的最长公共前缀判断：
    公共前缀 ≥30 字且覆盖导言归一化长度的 80% 以上，就认定"这一题的题干开头就是导言"。
    """
    target = norm_stem(passage)
    if len(target) < 30:
        return stem, False
    prefix = _common_prefix_len(norm_stem(stem), target)
    if prefix < 30 or prefix < 0.8 * len(target):
        return stem, False

    count = 0
    for index, char in enumerate(stem):
        if re.sub(r"[\s\W_]+", "", char.lower()):
            count += 1
        if count >= prefix:
            # 归一化会把反引号当非单词字符丢掉，所以切点可能停在代码块围栏**之前**，
            # 残余的 ``` 要一并剥掉（实测题组六第 36 题就是这里剩了个裸围栏）
            rest = stem[index + 1 :].lstrip(" \n\t:：。、.`")
            return (rest, True) if rest else (stem, False)
    return stem, False


def passage_from_stem(stem: str) -> tuple[str, str]:
    """从"模型已经把导言抄进第一道小题题干"的那种文本里，切出 (导言, 该小题自己的题干)。

    新提示词（§8.4 / 规则 6a）要求模型把题组导言抄进第一道小题的 stem 开头，
    所以导言**本来就在 JSON 里**，不用去转写里找（转写那份还缺代码围栏、行号也对不上）。
    切法按可靠性：
      1. 以**最后一个代码围栏**收尾（导言里的代码块到这里结束）→ 导言 = 到围栏为止；
      2. 以**小题占位符**（`(30) の選択肢：`）开头 → 导言 = 占位符之前；
      3. 都不成立 → 交回调用方（改用 groupTitle / 转写）。
    """
    text = str(stem or "")
    fence_count = text.count("```")
    if fence_count >= 2:
        last_fence = text.rfind("```")
        line_end = text.find("\n", last_fence)
        line_end = len(text) if line_end == -1 else line_end
        head, tail = text[: last_fence + 3].strip(), text[line_end:].strip()
        if len(flatten(head)) >= 30:
            return head, tail
    match = PLACEHOLDER_STEM.search(text)
    if match and match.start() > 0 and len(flatten(text[: match.start()])) >= 30:
        return text[: match.start()].strip(), text[match.start() :].strip()
    return "", text.strip()


def attach_shared_stems(entries: list[dict], pages_dir: Path, report: dict) -> None:
    """把"题组公共题干"**复制**到该题组每一道小题的题干上方，让每个小问独立成题（§8.4）。

    **为什么是复制、不是解析端的"文章"机制**：用户明确要求"公共题干复制到每个小问的上方，
    独立成题"。所以 md 里每道小题的 `#### 题目` 都自带完整导言 ——
    单看任意一题（错题本、单题分享、搜索命中）都不缺上下文，不依赖题组上下文。

    导言来源（按可靠性）：
      1. `merge.json` 的 `groupTitle` 其实是整段导言（`passage_candidate`，实测出现过）
      2. **整页 OCR 转写里"题组标题行 → 第一个小题行"之间的文本**（实测题组五、六都靠它）
    触发条件：上面任一条取到导言，且导言像公共题干（含代码块 / 较长），或该题组有 ≥2 道小题题干是占位符。

    防重复：某一题的题干开头已经抄了一遍导言时（实测题组六第 36 题），先按**归一化后的最长公共前缀**
    切掉原有那份，再补上标准的一份 —— 保证每道小题的题干里导言**恰好出现一次**。
    """
    by_group: dict[str, list[dict]] = {}
    for entry in entries:
        by_group.setdefault(entry["numeral"], []).append(entry)

    for numeral, group in by_group.items():
        if len(group) < 2:
            continue
        thin = sum(1 for e in group if is_thin_stem(e["q"]))
        candidate = next((e["passage_candidate"] for e in group if e.get("passage_candidate")), "")
        first = group[0]

        # ① 模型按新提示词把导言抄进了第一道小题的题干 → 直接从那里切（那份带代码围栏，最完整）
        from_stem, first_body = passage_from_stem(str(first["q"].get("stem") or ""))
        if from_stem:
            passage, source = from_stem, "第一道小题题干里的导言（提示词规则 6a 要求的位置）"
        elif candidate:
            passage, source = candidate, "merge.json 的 groupTitle（模型把整段导言塞进了标题）"
        else:
            from_transcription = passage_from_transcription(
                pages_dir, first["page"], numeral, int(first["q"].get("number") or 0)
            )
            if from_transcription:
                passage, source = from_transcription, f"page {first['page']} 的 OCR 转写"
            else:
                if thin >= 2:
                    report["shared_stem_missing"].append((numeral, len(group), thin))
                continue

        passage = clean_passage(passage)
        if not passage:
            report["shared_stem_missing"].append((numeral, len(group), thin))
            continue
        # 别把"题组标题下面恰好接着的普通一行"误当导言：要求它确实像公共题干
        if not (
            from_stem
            or candidate
            or thin >= 2
            or CODE_FENCE.search(passage)
            or len(flatten(passage)) > 60
        ):
            continue

        for entry in group:
            original = str(entry["q"].get("stem") or "").strip()
            if entry is first and from_stem:
                body = first_body
            else:
                rest, changed = strip_leading_passage(flatten(original), passage)
                if changed:
                    report["shared_stem_stripped"].append(entry["q"].get("number"))
                body = (rest if changed else original).strip()
            # 公共题干**复制**到每一道小题的题干上方 → 每个小问都是独立可读的题
            entry["q"]["stem"] = f"{passage}\n\n{body}".strip() if body else passage
            report["shared_stem_inlined"].append(entry["q"].get("number"))
        report["shared_stems"].append((numeral, len(group), source, len(flatten(passage))))


# ── 4) 跨页断题拼接（§8.2）───────────────────────────────────────────────
def merge_pair(target: dict, follower: dict) -> None:
    """把 follower 并进 target（同题号的跨页两半）。"""
    a, b = flatten(target.get("stem")), flatten(follower.get("stem"))
    if b and b not in a:
        target["stem"] = f"{a} {b}".strip()
    if not flatten(target.get("answerKey")) and flatten(follower.get("answerKey")):
        target["answerKey"] = follower["answerKey"]
    if not flatten(target.get("answerText")) and flatten(follower.get("answerText")):
        target["answerText"] = follower["answerText"]
    if not flatten(target.get("translation")) and flatten(follower.get("translation")):
        target["translation"] = follower["translation"]
    a_exp, b_exp = flatten(target.get("explanation")), flatten(follower.get("explanation"))
    if b_exp and b_exp not in a_exp:
        target["explanation"] = f"{a_exp}\n\n{b_exp}".strip()
    # 选项按 key 合并，先到先得；key 相同但文本不同的记一笔
    options = {str(o.get("key") or "").upper(): dict(o) for o in target.get("options") or []}
    for item in follower.get("options") or []:
        k = str(item.get("key") or "").upper()
        if not k:
            continue
        if k in options:
            if flatten(options[k].get("text")) != flatten(item.get("text")):
                target.setdefault("_splice_option_conflicts", []).append(k)
        else:
            options[k] = dict(item)
    target["options"] = [options[k] for k in sorted(options)]
    target["continued"] = bool(follower.get("continued"))
    target["needs_review"] = bool(target.get("needs_review")) or bool(follower.get("needs_review"))
    target["confidence"] = "low" if target.get("continued") else target.get("confidence")


def page_head_fragment(pages_dir: Path, page: int, limit: int = 1200) -> str:
    """下一页"第一个题号之前"的文本片段（用于 §8.2 第 2 步）。

    merge.json 里没有"页首未归属文本"字段，只能回落到该页的整页转写里取开头。
    取不到 / 太长就返回空串 —— 宁可交给人工，也不猜。
    """
    for label in ("a", "b"):
        path = pages_dir / f"page-{page:03d}.{label}.review.json"
        if not path.exists():
            continue
        text = str(c.read_json(path).get("transcription_md") or "").strip()
        if not text:
            continue
        head: list[str] = []
        for line in text.split("\n"):
            if re.match(r"^\s*#{0,4}\s*第\s*\d+\s*[題题]", line) or re.match(
                r"^\s*[（(]?\d+[)）.、]\s*\S", line
            ):
                break
            head.append(line)
        fragment = flatten(" ".join(head))
        if fragment and len(fragment) <= limit:
            return fragment
    return ""


def splice_continued(entries: list[dict], pages_dir: Path, report: dict) -> list[dict]:
    """§8.2 三步：按题号配对 → 按"题号缺失"配对 → 兜底标待复核（不丢题）。"""
    by_page: dict[int, list[dict]] = {}
    for entry in entries:
        by_page.setdefault(entry["page"], []).append(entry)

    # 第 1 步：同题号跨页配对
    dropped: set[int] = set()
    for entry in entries:
        q = entry["q"]
        if not q.get("continued") or id(entry) in dropped:
            continue
        number = q.get("number")
        followers = [
            e
            for e in by_page.get(entry["page"] + 1, [])
            if e["q"].get("number") == number and id(e) not in dropped
        ]
        if not followers:
            continue
        follower = followers[0]
        merge_pair(q, follower["q"])
        dropped.add(id(follower))
        report["splices"].append((entry["page"], entry["page"] + 1, number, "按题号配对"))

    kept = [e for e in entries if id(e) not in dropped]

    # 第 2 步 / 第 3 步
    for entry in kept:
        q = entry["q"]
        if not q.get("continued"):
            continue
        number = q.get("number")
        heads = by_page.get(entry["page"] + 1, [])
        first_next = heads[0]["q"].get("number") if heads else None
        if first_next is not None and first_next != (number or 0) + 1:
            fragment = page_head_fragment(pages_dir, entry["page"] + 1)
            if fragment:
                q["stem"] = f"{flatten(q.get('stem'))} {fragment}".strip()
                q["needs_review"] = True
                q["_splice_note"] = f"页首片段已并入（{len(fragment)} 字），请与页图核对"
                report["splices"].append(
                    (entry["page"], entry["page"] + 1, number, "页首片段并入（需人工确认）")
                )
                continue
        # 第 3 步前的现实检查：模型把完整的题也标成 continued（实测第 34/35 题：选项 4 个、答案齐全）。
        # 选项 ≥2 且有答案 → 这题不缺东西，不该按"拼接失败"惊动人工。
        if len(q.get("options") or []) >= 2 and flatten(q.get("answerKey")):
            report["splice_false_alarms"].append((entry["page"], number))
            continue
        q["needs_review"] = True
        q["_splice_note"] = "疑似跨页断题，未能自动拼接"
        report["splice_failed"].append((entry["page"], number, flatten(q.get("stem"))[:60]))

    if dropped:
        report["splices_dropped"] = len(dropped)
    return kept


# ── 5) 排序 + 去重（§8.1）────────────────────────────────────────────────
def sort_and_dedupe(entries: list[dict], report: dict) -> list[dict]:
    ordered = sorted(entries, key=lambda e: (int(e["q"].get("number") or 0), e["page"], e["index"]))
    seen: dict[tuple, dict] = {}
    result: list[dict] = []
    for entry in ordered:
        q = entry["q"]
        key = (int(q.get("number") or 0), norm_stem(q.get("stem")))
        if key in seen:
            kept = seen[key]
            winner, loser = (entry, kept) if info_score(q) > info_score(kept["q"]) else (kept, entry)
            report["duplicates"].append(
                (q.get("number"), int(winner["page"]), int(loser["page"]), flatten(q.get("stem"))[:50])
            )
            if winner is entry:
                result[result.index(kept)] = entry
                seen[key] = entry
            continue
        seen[key] = entry
        result.append(entry)
    return result


# ── 6) 完整性复查 + 渲染 ────────────────────────────────────────────────
GROUP_COUNT_RE = re.compile(r"共\s*(\d+)\s*[题問问]")


def paper_title(document: dict, category: str) -> str:
    """题单名（= 卷名）：`{title}（{variant}）{date}`，拼不出东西就用分类名。"""
    identity = document.get("paper_identity") or {}
    title = flatten(identity.get("title"))
    variant = flatten(identity.get("variant"))
    date = flatten(identity.get("date"))
    parts = title or category
    if variant and variant not in parts:
        parts += f"（{variant}）"
    if date and date not in parts:
        parts += date
    return parts.strip() or category


def collapse_groups(entries: list[dict], document: dict, category: str, report: dict) -> None:
    """把题组划分收成 **1 个**：全卷作为一张题单（用户 2026-09-22 要求取消「选择题/判断题」这种划分）。

    注意：**必须在 attach_shared_stems / check_group_counts 之后调用** ——
    那两个步骤依赖"按题型分的题组"来定位公共题干和核对题数，收拢后就找不到题组边界了。

    为什么还保留一个 `## 题组一：…` 标题：解析端没有 `## 题组X：` 时会把所有题挂到 `g00`
    且 `groupTitle` 为空 —— 站上的题单名会变成空白。所以留一个标题装卷名。
    """
    title = paper_title(document, category)
    for entry in entries:
        entry["numeral"] = "一"
        entry["group_title"] = title
    report["groups"] = [("一", title)]
    report["notes"].append(f"已取消题型分题组：全卷 1 张题单「{title}」")


def check_group_counts(entries: list[dict], report: dict) -> None:
    """题组标题里写了「共 N 题」的，按最终题数核对一遍。

    这是**跨页才做得成**的检查（一个题组可能横跨两页），所以放在 S4 而不是 S3。
    实测这份卷子：题组一 共10题→10、题组二 共9题→9、题组三 共5题→5、题组四 共 5 题→5，全对。
    """
    declared: dict[str, tuple[int, str]] = {}
    for entry in entries:
        for title in (entry.get("group_title"), entry["q"].get("groupTitle")):
            match = GROUP_COUNT_RE.search(str(title or ""))
            if match:
                declared.setdefault(entry["numeral"], (int(match.group(1)), str(title)))
    for numeral, (expected, title) in declared.items():
        actual = sum(1 for e in entries if e["numeral"] == numeral)
        if actual != expected:
            report["count_mismatch"].append((numeral, expected, actual, title))
    # 题组标题太长通常是模型把"整段公共题干"塞进了 groupTitle（实测重跑 S3 后题组六变成这样）。
    # 不替它编名字（那属于编造），只提醒人工在 report 里改短。
    for numeral, title in report.get("groups") or []:
        if len(title) > 40:
            report["long_group_titles"].append((numeral, len(title), title[:60]))


def review_completeness(entries: list[dict], report: dict) -> None:
    for entry in entries:
        q = entry["q"]
        problems = []
        if not flatten(q.get("stem")):
            problems.append("题干为空")
        if len(q.get("options") or []) < 2:
            problems.append(f"选项不足 2 个（{len(q.get('options') or [])}）")
        key = flatten(q.get("answerKey")).upper()
        if not key:
            problems.append("缺答案")
        elif key not in {str(o.get("key") or "").upper() for o in q.get("options") or []}:
            problems.append(f"答案 {key} 不在选项里")
        entry["problems"] = problems
        if problems:
            q["needs_review"] = True
            report["incomplete"].append((entry["page"], q.get("number"), "；".join(problems)))


def render_question(entry: dict) -> str:
    q = entry["q"]
    number = int(q.get("number") or 0)
    key = flatten(q.get("answerKey")).upper()
    answer_text = flatten(q.get("answerText")) or option_text(q, key)
    lines = [f"### 第{number}题", "", "#### 题目", "", sanitize_block(q.get("stem"))]
    translation = sanitize(q.get("translation"))
    if translation:
        lines += ["", f"题目翻译：{translation}"]
    lines.append("")
    for item in q.get("options") or []:
        lines.append(f"{str(item.get('key') or '').upper()}. {sanitize(item.get('text'))}")
    # 待复核标记放在题目区（解析端 `:289-290` 会跳过 `>` 开头行，不污染题干）
    marks: list[str] = []
    for conflict in entry.get("conflicts") or []:
        marks.append(f"OCR 冲突：{conflict.get('field')}（A 路「{conflict.get('a')}」/ B 路「{conflict.get('b')}」）")
    if q.get("_splice_note"):
        marks.append(str(q["_splice_note"]))
    for problem in entry.get("problems") or []:
        marks.append(problem)
    for mark in dict.fromkeys(marks):
        lines.append(f"> ⚠ 待核对：{mark}")
    lines += ["", "#### 答案与解析", ""]
    if key:
        # 解析端的正则要求答案后面**至少还有一个字符**（`.+?`），所以不能只写 `**正确答案：B**`
        lines.append(f"**正确答案：{key} {answer_text or key}**")
    else:
        lines.append("**正确答案：（待补）**")
    explanation = sanitize(q.get("explanation"))
    if explanation:
        lines += ["", explanation]
    lines.append("")
    return "\n".join(lines)


def render_md(
    category: str, document: dict, group_order: list[str], group_titles: dict, entries: list[dict]
) -> str:
    source = document.get("source") or ""
    sha8 = str(document.get("sha256") or "")[:8]
    pages = len(document.get("rendered_pages") or [])
    head = [
        f"# {category}",
        "",
        f"> 来源：`{source}`（sha256 前 8 位：`{sha8}`，共 {pages} 页）",
        f"> 生成：双路 step-3.7-flash OCR + deepseek-flash 比对提取；待复核项见 `report.md`",
        "",
    ]
    body: list[str] = []
    for numeral in group_order:
        group_entries = [e for e in entries if e["numeral"] == numeral]
        if not group_entries:
            continue
        body += [f"## 题组{numeral}：{flatten(group_titles.get(numeral, ''))}", ""]
        for entry in group_entries:
            body.append(render_question(entry))
    return "\n".join(head + body).rstrip() + "\n"


def render_transcription(document: dict, pages, pages_dir: Path) -> str:
    source = document.get("source") or ""
    lines = [
        f"# {source} — 双路 OCR 转写汇总",
        "",
        "模型：step-3.7-flash（两路）。按 PDF 物理页排列；A 路逐字保版面、B 路按题结构化，"
        "两路不一致处见 `report.md`。此稿仅供人工核对，未解题、未生成标准答案。",
        "",
    ]
    for page in pages:
        lines += [f"## PDF 第 {page} 页", ""]
        for label, name in (("a", "A 路（逐字转写）"), ("b", "B 路（按题结构化）")):
            path = pages_dir / f"page-{page:03d}.{label}.review.json"
            if not path.exists():
                lines += [f"### {name}", "", "（缺该路结果）", ""]
                continue
            review = c.read_json(path)
            lines += [f"### {name}", "", str(review.get("transcription_md") or "").strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def render_report(
    category: str, entries: list[dict], report: dict, all_conflicts: list[dict], failures: list[str]
) -> str:
    review = [e for e in entries if e["q"].get("needs_review")]
    lines = [
        f"# {category} — 待人工复核清单",
        "",
        f"- 题数：**{len(entries)}**（其中待复核 **{len(review)}**）",
        f"- 题组：{len(report['groups'])} → " + "、".join(f"题组{n}" for n, _ in report["groups"]),
        f"- 跨页拼接：{len(report['splices'])} 处成功 / {len(report['splice_failed'])} 处失败",
        f"- OCR 冲突：{len(all_conflicts)} 条；判断题自动补选项：{len(report['judgement_filled'])} 题；"
        f"题干末尾答案标记已清理：{len(report['answer_mark_stripped'])} 题",
        f"- 去重：{len(report['duplicates'])} 题；缺页/缺产物：{len(failures)}",
        f"- 公共题干：{len(report['shared_stems'])} 个题组已复制到 "
        f"{len(report['shared_stem_inlined'])} 道小题的题干上方"
        + (f"；{len(report['shared_stem_missing'])} 个题组取不到导言（需人工补）" if report["shared_stem_missing"] else ""),
        "",
    ]
    if failures:
        lines += ["## 缺产物（先补跑 S2/S3）", ""] + [f"- {f}" for f in failures] + [""]
    if report["shared_stems"]:
        lines += [
            "## 公共题干（题组导言）已复制到每道小题的题干上方",
            "",
            "每道小题的 `#### 题目` 都自带完整导言，**单看任意一题都不缺上下文**（独立成题）。",
            "",
            "| 题组 | 覆盖小题 | 导言来源 | 导言字数 |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {n} | {count} 道 | {source} | {length} |"
            for n, count, source, length in report["shared_stems"]
        ]
        lines.append("")
        if report["shared_stem_stripped"]:
            lines += [
                "- 以下小题的题干开头原本就抄了一份导言，已切掉再补标准的一份（保证只出现一次）："
                + "、".join(f"第{n}题" for n in report["shared_stem_stripped"]),
                "",
            ]
    if report["shared_stem_missing"]:
        lines += ["## ⚠ 疑似有公共题干、但取不到导言（需要人工补）", ""]
        lines += [
            f"- 题组{n}：{count} 道小题，其中 {thin} 道题干是占位符 —— merge.json 没接住导言，"
            f"OCR 转写里也没找到，请人工把导言补进该题组**每道小题**的题干开头"
            for n, count, thin in report["shared_stem_missing"]
        ]
        lines.append("")
    if report["passage_in_title"]:
        lines += ["## 题组标题其实是公共题干（已改短 + 转成 文章）", ""]
        lines += [f"- 题组{n}：原 groupTitle 长达 {length} 字" for n, length in report["passage_in_title"]]
        lines.append("")
    if report["unresolved_groups"]:
        lines += ["## ⚠ 定不出题组号的题（已沿用上一题题组）", ""]
        lines += [f"- page {page}：group=`{raw}` title=`{title}`" for page, raw, title in report["unresolved_groups"]]
        lines.append("")
    if report["incomplete"]:
        lines += ["## 字段不完整（会被解析端丢弃，必须手工补）", ""]
        lines += [f"- 第 {n} 题（page {p}）：{why}" for p, n, why in report["incomplete"]]
        lines.append("")
    if report["count_mismatch"]:
        lines += ["## 题组题数与标题声明不符", ""]
        lines += [
            f"- 题组{num}：标题写「{title}」＝共 {expected} 题，实际 {actual} 题"
            for num, expected, actual, title in report["count_mismatch"]
        ]
        lines.append("")
    if review:
        lines += ["## 待复核题目", "", "| 页 | 题号 | 题组 | 置信度 | 原因 |", "|---|---|---|---|---|"]
        for entry in review:
            q = entry["q"]
            why = entry.get("problems") or []
            if q.get("_splice_note"):
                why.insert(0, q["_splice_note"])
            if q.get("confidence") != "high":
                why.append(f"置信度 {q.get('confidence')}")
            lines.append(
                f"| {entry['page']} | {q.get('number')} | {entry['numeral']} | "
                f"{q.get('confidence')} | {'；'.join(why) or '模型标记 needs_review'} |"
            )
        lines.append("")
    if all_conflicts:
        lines += ["## OCR 两路冲突", "", "| 页 | 题号 | 字段 | A 路 | B 路 | 采信 | 依据 |", "|---|---|---|---|---|---|---|"]
        for item in all_conflicts:
            lines.append(
                f"| {item.get('page')} | {item.get('question') or '（整页）'} | {item.get('field')} | "
                f"{str(item.get('a'))[:40]} | {str(item.get('b'))[:40]} | "
                f"{str(item.get('chosen'))[:30]} | {str(item.get('reason'))[:60]} |"
            )
        lines.append("")
    if report["splice_failed"]:
        lines += ["## 跨页拼接失败", ""]
        lines += [f"- page {p} 第 {n} 题：`{s}`…" for p, n, s in report["splice_failed"]]
        lines.append("")
    if report["splice_false_alarms"]:
        lines += ["## 模型误标 continued 的题（字段完整，按完整题处理）", ""]
        lines += [f"- page {p} 第 {n} 题：选项 ≥2 且有答案" for p, n in report["splice_false_alarms"]]
        lines.append("")
    if report["duplicates"]:
        lines += ["## 去重记录", ""]
        lines += [f"- 第 {n} 题：page {keep} 胜出（丢弃 page {drop} 的重复）" for n, keep, drop, _ in report["duplicates"]]
        lines.append("")
    if report["notes"] or report["group_title_conflicts"] or report["long_group_titles"]:
        lines += ["## 其它", ""]
        lines += [f"- {n}" for n in report["notes"]]
        lines += [f"- 题组{num} 标题不一致：以「{first}」为准，另有「{other}」" for num, first, other in report["group_title_conflicts"]]
        lines += [
            f"- 题组{num} 的标题长达 {length} 字（疑似模型把公共题干塞进了 groupTitle），建议在 md 里改短：`{preview}…`"
            for num, length, preview in report["long_group_titles"]
        ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_final(path: Path, text: str, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        c.fail(f"{path.relative_to(c.REPO_ROOT)} 已存在；确认覆盖请加 --force（硬约束 5：不动既有数据）", 1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ── 入口 ────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="S4：全部 merge.json → 单个 <分类名>.md + transcription.md + report.md（docs §8）"
    )
    parser.add_argument("--category", required=True, help="分类名（= pdf-ocr/work/ 下的目录名）")
    parser.add_argument(
        "--pages",
        help="只汇总这些页，如 1-4（页码从 1 开始；写 0-3 也接受，0 视为起点；默认全部已渲染页）",
    )
    parser.add_argument(
        "--group-by",
        choices=("paper", "type"),
        default="paper",
        help="题组怎么分：paper=全卷 1 张题单（默认，取消「选择题/判断题」这类划分）；"
        "type=按原卷题型分题组",
    )
    parser.add_argument("--force", action="store_true", help="已存在的最终 .md 也覆盖")
    parser.add_argument("--quiet", action="store_true", help="只打印每页完成行与最终摘要")
    args = parser.parse_args()

    c.setup_stdio()
    c.set_quiet(args.quiet)

    category = c.safe_name(args.category)
    work_dir = c.WORK_ROOT / category
    pages_dir = work_dir / "pages"
    manifest_path = work_dir / "source-manifest.json"
    if not manifest_path.exists():
        c.fail(f"找不到 {manifest_path.relative_to(c.REPO_ROOT)}；请先跑 S1", 1)

    manifest = c.read_json(manifest_path)
    document = (manifest.get("documents") or [{}])[0]
    rendered = [int(n) for n in document.get("rendered_pages") or []]
    if not rendered:
        c.fail(f"{manifest_path.name} 里没有 rendered_pages；请先跑 S1", 1)
    pages = c.parse_pages(args.pages, max(rendered)) if args.pages else rendered
    pages = [n for n in pages if n in set(rendered)]

    c.info(f"[配置] 分类名={category}  页数={len(pages)}  工作目录={work_dir.relative_to(c.REPO_ROOT)}")

    started_all = time.perf_counter()
    entries: list[dict] = []
    failures: list[str] = []
    all_conflicts: list[dict] = []
    report: dict = {
        "groups": [],
        "group_title_conflicts": [],
        "judgement_filled": [],
        "answer_mark_stripped": [],
        "splices": [],
        "splice_failed": [],
        "splice_false_alarms": [],
        "duplicates": [],
        "incomplete": [],
        "count_mismatch": [],
        "long_group_titles": [],
        "passage_in_title": [],
        "shared_stems": [],
        "shared_stem_missing": [],
        "shared_stem_stripped": [],
        "shared_stem_inlined": [],
        "unresolved_groups": [],
        "notes": [],
    }

    # ① 读入
    for index, number in enumerate(pages, start=1):
        page_started = time.perf_counter()
        merge_path = pages_dir / f"page-{number:03d}.merge.json"
        if not merge_path.exists():
            failures.append(f"page {number}: 缺 page-{number:03d}.merge.json（先跑 S3）")
            c.progress(STAGE, index, len(pages), "✗", c.human_ms(page_started), "缺 merge.json")
            c.page_done(index, len(pages), ["汇总 ✗"], total_questions=len(entries))
            continue
        page_data = c.read_json(merge_path)
        page_questions = page_data.get("questions") or []
        # 卷名信息（用于"全卷 1 张题单"的题单名）——取第一份有内容的
        if not (document.get("paper_identity") or {}).get("title"):
            identity = page_data.get("paper_identity") or {}
            if any(flatten(value) for value in identity.values()):
                document["paper_identity"] = identity
        for order, q in enumerate(page_questions):
            q.setdefault("source", {}).setdefault("page", number)
            entries.append({"page": number, "index": order, "q": q})
        for conflict in page_data.get("conflicts") or []:
            all_conflicts.append({**conflict, "page": number})
        status = "⚠" if any(q.get("needs_review") for q in page_questions) else "✓"
        c.progress(
            STAGE, index, len(pages), status, c.human_ms(page_started), f"读出 {len(page_questions)} 题"
        )
        c.page_done(index, len(pages), [f"汇总 {status}"], total_questions=len(entries))

    if not entries:
        c.fail("所有页都没有题目（merge.json 里 questions 为空）；请检查 S3 结果", 1)

    # ② 加工
    group_order = normalize_groups(entries, pages_dir, report)
    if len(group_order) > MAX_GROUPS:
        c.warn(
            f"题组数 {len(group_order)} 超过 {MAX_GROUPS} 个；解析端只认「一…十」单个汉字，"
            f"多出来的题组会串到「题组十」下面，请合并题组"
        )
    fill_judgement_options(entries, report)
    strip_trailing_answer_mark(entries, report)
    entries = splice_continued(entries, pages_dir, report)
    entries = sort_and_dedupe(entries, report)
    attach_shared_stems(entries, pages_dir, report)
    review_completeness(entries, report)
    check_group_counts(entries, report)
    for numeral, length, _preview in report["long_group_titles"]:
        c.warn(
            f"题组{numeral} 的标题长达 {length} 字（疑似模型把公共题干塞进了 groupTitle），"
            f"网站上的题单名会很难看 —— 建议在 md 里改短（见 report.md）"
        )

    # ③ 写盘
    if args.group_by == "paper":
        # 取消题型分题组：全卷 1 张题单（必须在上面那几步之后，否则公共题干/题数核对就找不到题组边界）
        collapse_groups(entries, document, category, report)
        group_order = ["一"]
    group_titles = dict(report["groups"])
    md_text = render_md(category, document, group_order, group_titles, entries)
    out_path = c.RAW_ROOT / category / f"{category}.md"
    c.ensure_under(out_path, c.RAW_ROOT)
    write_final(out_path, md_text, args.force)

    trans_path = work_dir / "transcription.md"
    trans_path.write_text(render_transcription(document, pages, pages_dir), encoding="utf-8")
    report_path = work_dir / "report.md"
    report_path.write_text(render_report(category, entries, report, all_conflicts, failures), encoding="utf-8")

    # ④ 收尾
    errors = list(manifest.get("errors") or []) + failures
    document["build_pages"] = sorted({int(e["page"]) for e in entries})
    document["build_questions"] = len(entries)
    manifest["documents"] = [document] + list(manifest.get("documents") or [])[1:]
    manifest["errors"] = errors
    manifest["updated_at"] = c.now_iso()
    c.write_json_atomic(manifest_path, manifest)

    review_count = sum(1 for e in entries if e["q"].get("needs_review"))
    c.always(
        f"[{STAGE}] 跨页拼接 {len(report['splices'])} 题 / 去重 {len(report['duplicates'])} 题 → 单文件"
    )
    c.always(f"[{STAGE}] {out_path.relative_to(c.REPO_ROOT)} ✓ {len(entries)} 题")
    c.always(
        f"[{STAGE}] 题组 {len(group_order)} / 缺答案 {len(report['incomplete'])} / "
        f"题数不符 {len(report['count_mismatch'])} / 公共题干 {len(report['shared_stems'])} / "
        f"待复核 {review_count} / 缺产物 {len(failures)}"
    )
    c.always(
        f"[完成] 共 {len(entries)} 题 | 待复核 {review_count} | 失败 {len(failures)} | "
        f"总耗时 {c.human_ms(started_all) / 1000:.1f}s"
    )
    c.info(f"[清单] {report_path.relative_to(c.REPO_ROOT)}")
    c.info(f"[清单] {trans_path.relative_to(c.REPO_ROOT)}")
    return 3 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[中断] 已写盘的产物保留，重跑会自动跳过", file=sys.stderr)
        sys.exit(130)
