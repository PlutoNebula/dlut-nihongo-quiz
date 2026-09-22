"""S3：把同一页的两路 OCR 结果交给 deepseek-flash 比对 + 题目提取
→ pdf-ocr/work/<分类名>/pages/page-00N.merge.json

用法：
  python pdf-ocr/3_merge.py --category <分类名> [--pages 1-3] [--force] [--quiet]
  （密钥读 .env：DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL）

设计要点（docs/pdf-ocr-pipeline.md §7.2）：
  * 每页一次调用，输入是两路的 review.json（不传图片，纯文本比对）。
  * 判定规则：同一题同一字段两路不一致 → 必进 conflicts[]，且模型须给 chosen/reason；
    confidence != high 或冲突未消解 → 该题 needs_review=true；页边界半截题 → continued=true。
  * **确定性兜底**：两路在"格式无关字段"（页码标签 / 题号范围 / page_condition / paper_identity）
    上不一致时，即使模型没报冲突，也补一条 synthetic conflict 并把整页标 needs_review ——
    因为两路提示词刻意异构，转写文本的排版本来就不同，只有这些字段可直接逐字段比。
  * 只有一路成功（另一路失败）时仍提取，但整页标 needs_review 并注明缺一路。
  * **题号覆盖 / 题数匹配兜底**：把两路 `question_ranges` 的并集与"实际提出的题号"对账，
    少了（模型静默漏抽）/ 多了（编题号）/ 同页重复 → 补 synthetic conflict 并整页标 needs_review。
    实测抓到过：page 4 两路都声明 34-41，模型只提出 36-41，丢了第 34、35 题。

产物字段见 §7.2；中间产物留在 pdf-ocr/work/，不写 data/raw。
退出码：0 成功；1 参数/环境错误；3 有页失败。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as c  # noqa: E402

SYSTEM_PROMPT = (
    "你是试卷校对与结构化提取助手。你会看到同一页试卷的两路独立 OCR 结果，"
    "请比对它们并提取题目。只输出一个 JSON 对象，不要输出解释、不要用代码围栏。"
)

SCHEMA_BLOCK = """请输出如下 JSON（键名固定）：

{
  "paper_identity": {"title": "", "date": "", "variant": ""},
  "page_condition": "clear",
  "conflicts": [
    {"question": 6, "field": "options.A", "a": "", "b": "", "chosen": "", "reason": "", "confidence": "medium"}
  ],
  "questions": [
    {
      "number": 1, "group": "题组一", "groupTitle": "汉字读音选择",
      "stem": "题干原文（可含 ** 强调）",
      "options": [{"key": "A", "text": ""}, {"key": "B", "text": ""}],
      "answerKey": "B", "answerText": "",
      "explanation": "", "translation": "",
      "confidence": "high", "needs_review": false, "continued": false
    }
  ]
}

规则：
1) 两路一致的内容直接采用；**任何不一致**都要进 conflicts[]，field 写清楚位置
   （如 options.A / answerKey / stem / number），a、b 分别是两路的原文，chosen 是你采信的值，reason 给依据。
2) 把握不足（confidence 不是 high）或冲突未消解的题，needs_review 必须为 true。
3) 题干/选项被页边界切成两半（本页只有半截）时，continued 为 true —— 交给后续拼接，不要脑补后半截。
4) 只提取本页真实出现的题目，number 用页面上的原始题号；选项 key 用 A/B/C/D。
   没有选项的题（填空/简答）options 给 []，answerKey 给 ""，答案写在 answerText。
5) 两路都没写清的内容不要编造；宁可在 explanation 里留空 + needs_review=true。
6) **公共题干（题组导言 / 代码块 / 表格）与它下面的小题**：
   a. 一个题组的小题共用一段导言时（如「以下は…空欄を A～D で答えよ」+ 代码块），
      把这段导言**原样**抄到该题组**第一道小题的 stem 开头**（保留换行和 ``` 代码围栏）。
   b. `groupTitle` 只写题组的**短名**（如「题组二」）。**不要把整段导言塞进 groupTitle。**
   c. **该题组下的每一个小题都必须单独成题**，即使小题自己的文字只是「(34) の選択肢：」这样的
      占位符、或者小题的题干不在这页 —— 题号用页面上的小题号，选项/答案照抄卷面，**不许跳过**。
      宁可用占位文字 + needs_review=true，也不能少一道小题。"""


# ── 格式无关字段的确定性比对（兜底）───────────────────────────────────────
def normalize_range(value) -> set[int]:
    """把 ["1-4"] / ["1","2"] 这类题号范围归一化成 {1,2,3,4}。"""
    out: set[int] = set()
    for item in value if isinstance(value, list) else [value]:
        for part in re.split(r"[,，、;；\s]+", str(item or "").strip()):
            if not part:
                continue
            m = re.match(r"^(\d+)\s*[-–—~]\s*(\d+)$", part)
            if m:
                out.update(range(int(m.group(1)), int(m.group(2)) + 1))
            elif part.isdigit():
                out.add(int(part))
    return out


def structural_diffs(review_a: dict, review_b: dict) -> list[dict]:
    """两路在"可直接逐字段比"的字段上的差异（转写文本排版本来就不同，不参与比较）。"""
    diffs: list[dict] = []

    def label_set(review: dict) -> set[str]:
        return {str(x).strip() for x in review.get("printed_page_labels", []) if str(x).strip()}

    if label_set(review_a) != label_set(review_b):
        diffs.append(
            {
                "question": None,
                "field": "printed_page_labels",
                "a": ", ".join(sorted(label_set(review_a))),
                "b": ", ".join(sorted(label_set(review_b))),
            }
        )

    if normalize_range(review_a.get("question_ranges")) != normalize_range(
        review_b.get("question_ranges")
    ):
        diffs.append(
            {
                "question": None,
                "field": "question_ranges",
                "a": ", ".join(review_a.get("question_ranges") or []),
                "b": ", ".join(review_b.get("question_ranges") or []),
            }
        )

    for field in ("page_condition",):
        if str(review_a.get(field, "")).strip() != str(review_b.get(field, "")).strip():
            diffs.append(
                {
                    "question": None,
                    "field": field,
                    "a": str(review_a.get(field, "")),
                    "b": str(review_b.get(field, "")),
                }
            )

    identity_a = review_a.get("paper_identity") or {}
    identity_b = review_b.get("paper_identity") or {}
    for key in ("title", "date", "variant"):
        va, vb = str(identity_a.get(key, "")).strip(), str(identity_b.get(key, "")).strip()
        if va and vb and va != vb:
            diffs.append({"question": None, "field": f"paper_identity.{key}", "a": va, "b": vb})

    for diff in diffs:
        diff.setdefault("chosen", "")
        diff.setdefault("reason", "两路在这项上不一致（确定性比对发现，模型未报）")
        diff.setdefault("confidence", "low")
    return diffs


# ── 题号覆盖 / 题数匹配的确定性兜底 ──────────────────────────────────────
def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def declared_numbers(reviews: dict[str, dict]) -> tuple[set[int], list[str]]:
    """两路 `question_ranges` 的并集 → (期望题号集合, 原始声明文本)。"""
    numbers: set[int] = set()
    sources: list[str] = []
    for label in sorted(reviews):
        ranges = reviews[label].get("question_ranges") or []
        if ranges:
            sources.append(f"{label.upper()} 路：{', '.join(str(x) for x in ranges)}")
        numbers |= normalize_range(ranges)
    return numbers, sources


def coverage_diffs(reviews: dict[str, dict], questions: list[dict]) -> list[dict]:
    """题号覆盖 / 题数匹配的确定性检查（**不发新请求，纯本地比对**）。

    OCR 两路各自声明了本页含哪些题号（`question_ranges`，如 `["34-41"]`）。
    取并集，与"本次实际提出的题号"比一遍：

      * 少了 → 模型**静默漏抽**（实测：page 4 声明 34-41，只提出 36-41，丢了第 34/35 题）
      * 多了 → 模型编了题号，或 OCR 的范围写错
      * 同页重复 → 复制粘贴

    只报案、不阻断：冲突进 `conflicts[]`，整页 `needs_review`。
    """
    expected, sources = declared_numbers(reviews)
    actual = [n for n in (_as_int(q.get("number")) for q in questions) if n is not None]
    actual_set = set(actual)
    source_text = "；".join(sources) or "（两路都没声明题号范围）"
    diffs: list[dict] = []

    if expected:
        missing = sorted(expected - actual_set)
        extra = sorted(actual_set - expected)
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"缺 {len(missing)} 题（{c.format_pages(missing)}）")
            if extra:
                detail.append(f"多出未声明的题号（{c.format_pages(extra)}）")
            diffs.append(
                {
                    "question": None,
                    "field": "question_coverage",
                    "a": f"OCR 声明本页 {len(expected)} 题：{c.format_pages(sorted(expected))}",
                    "b": f"实际提出 {len(actual)} 题：{c.format_pages(sorted(actual_set)) or '无'}",
                    "chosen": "",
                    "reason": (
                        f"题数不匹配：{'；'.join(detail)}。来源：{source_text}。"
                        "少题通常是模型静默漏抽 —— 请对照页图补抽，或确认该题号是 OCR 笔误"
                    ),
                    "confidence": "medium",
                }
            )

    duplicates = sorted({n for n in actual if actual.count(n) > 1})
    if duplicates:
        diffs.append(
            {
                "question": None,
                "field": "question_number_duplicate",
                "a": f"本页提出 {len(actual)} 题",
                "b": f"重复题号：{c.format_pages(duplicates)}",
                "chosen": "",
                "reason": "同页出现重复题号，模型可能复制粘贴了同一题；请对照页图确认",
                "confidence": "medium",
            }
        )
    return diffs


# ── 提示词 ──────────────────────────────────────────────────────────────
def shorten(text: str, limit: int = 12000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…（本页转写过长，已截断）"


def render_pass(label: str, review: dict) -> str:
    uncertain = review.get("uncertain") or []
    corrections = review.get("corrections") or []
    lines = [
        f"【OCR 路 {label}】",
        f"- 印刷页码标签：{', '.join(review.get('printed_page_labels') or []) or '（未标注）'}",
        f"- 题号范围：{', '.join(review.get('question_ranges') or []) or '（未标注）'}",
        f"- 页面状况：{review.get('page_condition') or '未知'}",
        f"- 卷名信息：{review.get('paper_identity') or {}}",
    ]
    if uncertain:
        lines.append(f"- 该路自报没把握的位置：{uncertain}")
    if corrections:
        lines.append(f"- 该路自报的改正：{corrections}")
    lines.append("- 整页转写：")
    lines.append(shorten(str(review.get("transcription_md") or "")))
    return "\n".join(lines)


RETRY_NOTE = (
    "\n\n【重要】上一次回复不是合法 JSON。这一次请**只输出一个 JSON 对象**："
    "不要任何解释文字、不要代码围栏；字符串内部的换行必须写成 \\n（不要出现裸换行）。"
)


def build_payload(
    model: str | None, page: int, reviews: dict[str, dict], max_tokens: int, retry_note: str = ""
) -> dict:
    parts = [f"【本页】第 {page} 页", ""]
    for label in sorted(reviews):
        parts.append(render_pass(label.upper(), reviews[label]))
        parts.append("")
    parts.append(SCHEMA_BLOCK + retry_note)
    return {
        "model": model,
        "temperature": 0,
        # 单次响应上限：merge 出参是整份题目 JSON，按实测留足余量，防止话痨计费
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ],
    }


# ── 结果归一化 ──────────────────────────────────────────────────────────
def normalize_question(raw: dict, page: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    stem = str(raw.get("stem") or "").strip()
    if not stem:
        return None
    options = []
    for option in raw.get("options") or []:
        if isinstance(option, dict):
            key = str(option.get("key") or "").strip().upper()
            text = str(option.get("text") or "").strip()
            if key:
                options.append({"key": key, "text": text})
    answer_key = str(raw.get("answerKey") or "").strip().upper()
    number_raw = raw.get("number")
    try:
        number = int(number_raw)
    except (TypeError, ValueError):
        number = None
    confidence = str(raw.get("confidence") or "medium").strip().lower()
    needs_review = bool(raw.get("needs_review")) or confidence != "high"
    # 答案不在选项里（且确实有选项）→ 一定是把握不足，交给人工复核
    if options and answer_key and not any(o["key"] == answer_key for o in options):
        needs_review = True
    return {
        "number": number,
        "group": str(raw.get("group") or "").strip(),
        "groupTitle": str(raw.get("groupTitle") or "").strip(),
        "stem": stem,
        "options": options,
        "answerKey": answer_key,
        "answerText": str(raw.get("answerText") or "").strip(),
        "explanation": str(raw.get("explanation") or "").strip(),
        "translation": str(raw.get("translation") or "").strip(),
        "questionType": str(raw.get("questionType") or ("single" if options else "other")).strip(),
        "confidence": confidence,
        "needs_review": needs_review,
        "continued": bool(raw.get("continued")),
        "source": {"page": page},
    }


def normalize_merge(
    raw: dict,
    page: int,
    cfg: dict,
    elapsed_ms: int,
    usage,
    notes: list[str],
    call_meta: dict | None = None,
) -> dict:
    conflicts = []
    for item in raw.get("conflicts") or []:
        if not isinstance(item, dict):
            continue
        try:
            question = int(item.get("question")) if item.get("question") is not None else None
        except (TypeError, ValueError):
            question = None
        conflicts.append(
            {
                "question": question,
                "field": str(item.get("field") or "").strip(),
                "a": str(item.get("a") or "").strip(),
                "b": str(item.get("b") or "").strip(),
                "chosen": str(item.get("chosen") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
                "confidence": str(item.get("confidence") or "medium").strip().lower(),
            }
        )
    questions = [q for q in (normalize_question(q, page) for q in raw.get("questions") or []) if q]
    identity = raw.get("paper_identity") if isinstance(raw.get("paper_identity"), dict) else {}
    return {
        "page": page,
        "paper_identity": {
            "title": str(identity.get("title") or ""),
            "date": str(identity.get("date") or ""),
            "variant": str(identity.get("variant") or ""),
        },
        "page_condition": str(raw.get("page_condition") or "").strip(),
        "conflicts": conflicts,
        "questions": questions,
        "notes": notes,
        "needs_review": bool(notes) or bool(conflicts) or any(q["needs_review"] for q in questions),
        "call": {
            "model": cfg.get("model"),
            "endpoint": cfg.get("base_url"),
            "elapsed_ms": elapsed_ms,
            "usage": usage if isinstance(usage, dict) else {},
            **(call_meta or {}),
        },
    }


# ── 单页调用 ────────────────────────────────────────────────────────────
def merge_one_page(
    cfg: dict,
    page: int,
    reviews: dict[str, dict],
    index: int,
    total: int,
    timeout: int,
    max_retries: int,
    max_tokens: int,
) -> dict:
    stage = c.STAGES[3]
    payload = build_payload(cfg.get("model"), page, reviews, max_tokens)
    notes: list[str] = []
    if len(reviews) < 2:
        missing = "B" if "a" in reviews else "A"
        notes.append(f"缺少 OCR 路 {missing} 的结果，无法互校，整页标为待复核")
    last_error = ""

    for attempt in range(1, max_retries + 1):
        started = time.perf_counter()
        attempt_payload = (
            payload
            if attempt == 1
            else build_payload(cfg.get("model"), page, reviews, max_tokens, RETRY_NOTE)
        )
        try:
            response = c.post_json(
                str(cfg["base_url"]), attempt_payload, cfg.get("api_key"), timeout=timeout
            )
            text, text_field = c.response_text_ex(response)
            finish_reason = c.response_finish_reason(response)
            try:
                raw, repair = c.extract_json(text)
            except c.ApiError as exc:  # 补上"为什么取不到 JSON"，否则排查要绕好几圈
                raise c.ApiError(
                    f"{exc}{c.reasoning_hint(text_field, finish_reason, max_tokens, 'merge')}",
                    retryable=exc.retryable,
                ) from exc
            if finish_reason == "length":
                repair = {**repair, "truncated": True}
            if repair.get("truncated"):
                notes.append(f"模型回复疑似被截断（finish_reason={finish_reason or '?'}），已尽力补全")
            if len(reviews) == 2:
                extra = structural_diffs(reviews["a"], reviews["b"])
                # 模型自己可能已经报过同一个字段，别再补一条重复 conflict
                # （2026-09-22 实测：page 1 的 paper_identity.date 被报了两遍）
                reported = {
                    str(cf.get("field") or "")
                    for cf in (raw.get("conflicts") or [])
                    if isinstance(cf, dict)
                }
                extra = [cf for cf in extra if str(cf.get("field") or "") not in reported]
                if extra:
                    raw = dict(raw)
                    raw["conflicts"] = list(raw.get("conflicts") or []) + extra
                    notes.append(f"确定性比对另发现 {len(extra)} 处两路不一致")
            merged = normalize_merge(
                raw,
                page,
                cfg,
                c.human_ms(started),
                response.get("usage"),
                notes,
                {
                    "finish_reason": finish_reason,
                    "json_repaired": repair.get("repaired", False),
                    "json_truncated": repair.get("truncated", False),
                },
            )
            # 题号覆盖 / 题数匹配兜底：与"两路 OCR 自己声明的题号范围"对账
            coverage = coverage_diffs(reviews, merged.get("questions") or [])
            if coverage:
                merged["conflicts"] = list(merged.get("conflicts") or []) + coverage
                notes.append(f"题号覆盖核对发现 {len(coverage)} 处不一致")
                merged["notes"] = notes
                merged["needs_review"] = True
            return merged
        except c.ApiError as exc:
            last_error = str(exc)
            if not exc.retryable:
                raise
        if attempt < max_retries:
            c.retry_line(stage, index, total, attempt, max_retries, last_error, c.human_ms(started))
            if attempt == 1 and c.is_timeout(last_error):
                c.always(c.timeout_hint())
            time.sleep(min(2**attempt, 10))

    raise c.ApiError(f"重试 {max_retries} 次仍失败：{last_error}", retryable=False)


# ── 入口 ────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="S3：两路 OCR → 比对 + 题目提取 → page-00N.merge.json（docs §7.2）"
    )
    parser.add_argument("--category", required=True, help="分类名（= pdf-ocr/work/ 下的目录名）")
    parser.add_argument(
        "--pages",
        help="只处理这些页，如 1-3,7（页码从 1 开始；写 0-3 也接受，0 视为起点；默认全部）",
    )
    parser.add_argument("--timeout", type=int, default=180, help="单次请求超时秒数，默认 180")
    parser.add_argument("--max-retries", type=int, default=3, help="每页最大尝试次数，默认 3")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=c.DEFAULT_MAX_TOKENS["merge"],
        help=(
            f"单次响应 token 上限，默认 {c.DEFAULT_MAX_TOKENS['merge']}"
            f"（= DeepSeek 端点合法上限 {c.MAX_TOKENS_CEILING['merge']}，出参是整份题目 JSON）"
        ),
    )
    parser.add_argument(
        "--budget-tokens",
        type=int,
        default=0,
        help="本次运行的 token 预算（0=不限）；每次调用前检查，超了就停在当前页（退出码 4）",
    )
    parser.add_argument(
        "--total-budget-tokens",
        type=int,
        default=0,
        help="跨运行累计 token 上限（0=不限，基于 manifest 里已记录的 usage）；已超则拒绝开工（退出码 4）",
    )
    parser.add_argument("--force", action="store_true", help="已存在的结果也重跑")
    parser.add_argument("--quiet", action="store_true", help="只打印每页完成行与最终摘要")
    args = parser.parse_args()

    c.setup_stdio()
    c.set_quiet(args.quiet)
    c.check_max_tokens("merge", args.max_tokens)

    category = c.safe_name(args.category)
    work_dir = c.WORK_ROOT / category
    manifest_path = work_dir / "source-manifest.json"
    if not manifest_path.exists():
        c.fail(f"找不到 {manifest_path.relative_to(c.REPO_ROOT)}；请先跑 S1", 1)

    cfg = c.merge_config()
    if not cfg.get("api_key"):
        c.fail(
            "缺少 DEEPSEEK_API_KEY：请在仓库根 .env 里填 DEEPSEEK_API_KEY（可选 DEEPSEEK_BASE_URL/DEEPSEEK_MODEL）",
            1,
        )

    manifest = c.read_json(manifest_path)
    document = (manifest.get("documents") or [{}])[0]
    rendered = [int(n) for n in document.get("rendered_pages", [])]
    if not rendered:
        c.fail(f"{manifest_path.name} 里没有 rendered_pages；请先跑 S1", 1)
    pages = c.parse_pages(args.pages, max(rendered)) if args.pages else rendered
    pages = [n for n in pages if n in set(rendered)]

    pages_dir = work_dir / "pages"
    c.info(
        f"[配置] 分类名={category}  页数={len(pages)}  模型={cfg.get('model')}  端点={cfg.get('base_url')}"
    )

    started_all = time.perf_counter()
    errors: list[str] = []
    done = 0
    total_questions = 0
    total_conflicts = 0
    spent = 0
    calls_made = 0
    budget_hit = False

    prior = c.usage_block(manifest)
    if args.total_budget_tokens > 0 and int(prior.get("tokens", 0)) >= args.total_budget_tokens:
        c.fail(
            f"跨运行预算已用尽：{manifest_path.name} 里累计 {prior.get('tokens')} token ≥ "
            f"--total-budget-tokens {args.total_budget_tokens}；要重新开始请调大预算或删掉该 usage 记录",
            4,
        )

    for index, number in enumerate(pages, start=1):
        target = pages_dir / f"page-{number:03d}.merge.json"
        reviews: dict[str, dict] = {}
        for label in ("a", "b"):
            path = pages_dir / f"page-{number:03d}.{label}.review.json"
            if path.exists():
                reviews[label] = c.read_json(path)

        if not reviews:
            errors.append(f"page {number}: 两路 review.json 都不存在（先跑 S2）")
            c.progress(c.STAGES[3], index, len(pages), "✗", 0, "缺两路 OCR 结果")
            c.page_done(index, len(pages), ["比对提取 ✗"])
            continue

        if target.exists() and not args.force:
            existing = c.read_json(target)
            total_questions += len(existing.get("questions") or [])
            total_conflicts += len(existing.get("conflicts") or [])
            done += 1
            c.progress(
                c.STAGES[3], index, len(pages), "✓", 0, f"{target.name} 已存在，跳过"
            )
            c.page_done(index, len(pages), ["比对提取 ✓（已存在）"])
            continue

        # 预算检查放在**每页调用之前**
        if c.budget_stop(spent, args.budget_tokens):
            budget_hit = True
            c.warn(
                f"本次预算已用尽（{c.human_tokens(spent)} ≥ {c.human_tokens(args.budget_tokens)} token），"
                f"在第 {number} 页停下；已完成的结果都已落盘，可稍后续跑"
            )
            break

        page_started = time.perf_counter()
        try:
            merge = merge_one_page(
                cfg, number, reviews, index, len(pages), args.timeout, args.max_retries, args.max_tokens
            )
            calls_made += 1
            spent += c.usage_tokens(merge["call"].get("usage"))
            c.write_json_atomic(target, merge)
            question_count = len(merge["questions"])
            conflict_count = len(merge["conflicts"])
            total_questions += question_count
            total_conflicts += conflict_count
            done += 1
            status = "⚠" if merge["needs_review"] else "✓"
            c.progress(
                c.STAGES[3],
                index,
                len(pages),
                status,
                merge["call"]["elapsed_ms"],
                f"提取 {question_count} 题（冲突 {conflict_count}）",
            )
            c.page_done(
                index,
                len(pages),
                [f"比对提取 {status}"],
                total_questions,
                usage_note=f"本次累计 {c.human_tokens(spent)} tok",
            )
        except c.ApiError as exc:
            errors.append(f"page {number}: {exc}")
            c.progress(c.STAGES[3], index, len(pages), "✗", c.human_ms(page_started), str(exc)[:80])
            c.page_done(index, len(pages), ["比对提取 ✗"])

    merge_done = sorted(
        n for n in pages if (pages_dir / f"page-{n:03d}.merge.json").exists()
    )
    document["merge_pages"] = merge_done
    manifest["documents"] = [document] + list(manifest.get("documents", [])[1:])
    manifest["errors"] = manifest.get("errors", []) + errors
    manifest["updated_at"] = c.now_iso()
    usage = c.add_usage(manifest, calls=calls_made, tokens=spent)
    c.write_json_atomic(manifest_path, manifest)

    c.stage_done(c.STAGES[3], done, len(pages), c.human_ms(started_all))
    c.always(
        f"[完成] 比对提取 {len(merge_done)}/{len(pages)} 页 | 累计题数 {total_questions} | 冲突 {total_conflicts}"
    )
    c.always(
        f"[用量] 本次调用 {calls_made} 次 / {c.human_tokens(spent)} tok；"
        f"该分类累计 {usage['calls']} 次 / {c.human_tokens(int(usage['tokens']))} tok"
    )
    c.always(f"[清单] {manifest_path.relative_to(c.REPO_ROOT)}")
    if budget_hit:
        return 4
    if errors:
        c.warn(f"有 {len(errors)} 页失败：{'; '.join(errors[:3])}")
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(
            "\n[中断] 收到 Ctrl+C。已完成的产物都在 pdf-ocr/work/ 里，"
            "重跑同一条命令会自动续跑（已存在的页会跳过，不会重复计费）",
            flush=True,
        )
        sys.exit(130)
