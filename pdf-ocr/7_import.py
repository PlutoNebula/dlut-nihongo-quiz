"""S7：**文件夹批量导入** —— 一个文件夹 = 一个入口，文件夹里的每个 PDF = 一张试卷卡。

把 S1→S6 串起来全自动跑：

    S1 渲染页图 → S2 双路 OCR → S3 比对提取 → S4 汇总成文 → S5 契约门禁 → S6 发布上站

用法：

    # 入口名默认取**文件夹名**（例：`.../马克思主义原理/` → 入口「马克思主义原理」）
    python pdf-ocr/7_import.py --folder "C:\\Users\\me\\Desktop\\马原试卷"

    # 用一个文件夹批量导入到**指定入口**（文件夹里多份 PDF 都挂到同一入口下）
    python pdf-ocr/7_import.py --folder .\\inbox --entry "英语四级" --entry-key english-cet4 --entry-icon 英

    # 先看计划不跑（打印每个 PDF 的分类名与要执行的步骤）
    python pdf-ocr/7_import.py --folder .\\inbox --dry-run

    # 断点续跑：从第 3 步开始（前面几步的产物已存在会被各自跳过）
    python pdf-ocr/7_import.py --folder .\\inbox --from-step 3

规则：
  * 入口名默认 = **文件夹名**；`--entry` 可覆盖。
  * `--entry-key` 不填时由入口名推（只用 a-z 0-9 -；中文名推不出来时必须显式给）。
  * 每个 PDF 一张试卷：卡片标题默认 = **PDF 文件名**（去扩展名）；`--paper-prefix` 可加前缀。
  * 分类名（= `data/raw/<分类名>/` + `public/<分类名>-question-bank.json` 的 key）默认
    `<entry-key>-<序号>`，可用 `--category-prefix` 换前缀。
  * 某一份 PDF 失败时**停下来**并打印续跑命令；加 `--keep-going` 则继续跑下一份。

退出码：0 全部成功；1 参数/环境错误；3 有试卷失败；4 token 预算耗尽（透传子步骤）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as c  # noqa: E402

STAGE = "S7/7 批量导入"

STEPS: tuple[tuple[int, str, str], ...] = (
    (1, "1_render.py", "渲染页图"),
    (2, "2_ocr.py", "双路 OCR"),
    (3, "3_merge.py", "比对提取"),
    (4, "4_build_md.py", "汇总成文"),
    (5, "5_check.py", "契约门禁"),
    (6, "6_publish.py", "发布上站"),
)


def slug(text: str) -> str:
    """把任意名字压成 `a-z 0-9 -`（入口 key / 分类名前缀用）。中文会被丢掉，可能返回空串。"""
    flat = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").strip()).strip("-").lower()
    return re.sub(r"-{2,}", "-", flat)


def clean_paper_title(stem: str) -> str:
    """把 PDF 文件名收拾成**卡片标题**：去掉平台导出的噪声尾巴。

    实测这些卷的名字长这样：`马原试卷1(1)_0_1790064200614.pdf`、`马原机考题库_202412081720_15114_0_1790063990489.pdf`
    —— 直接当卡片名很难看。这里去掉 `_0_<长数字>` / 结尾的 `_<10 位以上数字>` / 首尾空白。
    """
    text = str(stem or "").strip()
    text = re.sub(r"_0_\d{6,}$", "", text)
    text = re.sub(r"[_\-\s]+\d{10,}$", "", text)
    return text.strip() or str(stem or "").strip()


def collect_pdfs(folder: Path) -> list[Path]:
    """文件夹里的 PDF（不递归子目录；按文件名排序，保证卡片顺序稳定）。"""
    pdfs = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: p.name,
    )
    return pdfs


def plan(folder: Path, pdfs: list[Path], entry_key: str, prefix: str, paper_prefix: str) -> list[dict]:
    """每份 PDF 一行计划：分类名 / 试卷标题 / 输入路径。

    **每份卷一个独立目录**：分类名就是它的目录名（`data/raw/<分类名>/` 放最终 md、
    `pdf-ocr/work/<分类名>/` 放页图与每页 JSON），S1 会自动建。源 PDF **不复制入库**。
    分类名优先用 PDF 文件名推出来的 ASCII 短名；推不出（纯中文名）时退回 `<前缀>-<序号>`。
    卡片标题用文件名清洗后的结果（去掉平台导出的 `_0_<长数字>` 尾巴）。
    """
    rows = []
    used: set[str] = set()
    for index, pdf in enumerate(pdfs, start=1):
        title = clean_paper_title(pdf.stem)
        slugged = slug(title)
        # 必须**含字母**才算"推得出目录名"：纯中文名 slug 后往往只剩几个数字
        # （`马原试卷1(1)` → `1-1`），拿它当目录名既无意义又容易撞车。
        usable = slugged if re.search(r"[a-z]", slugged) else ""
        if len(pdfs) > 1:
            category = usable or f"{prefix}-{index}"
        else:
            category = usable or prefix
        while category in used:  # 两份卷同名时避免撞目录
            category = f"{category}-{index}"
        used.add(category)
        rows.append(
            {
                "index": index,
                "pdf": pdf,
                "category": category,
                "paper": f"{paper_prefix}{title}".strip(),
            }
        )
    return rows


def run_step(step: int, args: list[str], quiet: bool) -> int:
    """跑一个阶段（子进程；stdio 继承，进度行直接打到终端）。"""
    script = c.TOOL_ROOT / STEPS[step - 1][1]
    cmd = [sys.executable, str(script), *args]
    if not quiet:
        c.info(f"      $ python pdf-ocr/{script.name} {' '.join(args)}")
    # 不捕获输出：子进程自己按 §6 的进度规范打印（捕获会破坏进度条与颜色）
    proc = subprocess.run(cmd, cwd=str(c.REPO_ROOT))
    return proc.returncode


def import_one(row: dict, opts: argparse.Namespace, first: bool) -> int:
    """把一份 PDF 从 S1 跑到 S6。返回最后一个非零退出码（0 = 成功）。"""
    category = row["category"]
    pdf: Path = row["pdf"]
    steps = [s for s, _f, _d in STEPS]
    if opts.only_step:
        steps = [opts.only_step]
    elif opts.from_step > 1:
        steps = [s for s in steps if s >= opts.from_step]

    for step in steps:
        started = time.perf_counter()
        if step == 1:
            argv = [str(pdf), "--category", category, "--dpi", str(opts.dpi)]
            if opts.force:
                argv.append("--force")
        elif step == 6:
            argv = ["--category", category, "--entry", opts.entry, "--entry-key", opts.entry_key,
                    "--paper", row["paper"], "--position", str(row["index"])]
            if opts.entry_icon:
                argv += ["--entry-icon", opts.entry_icon]
            if opts.entry_desc:
                argv += ["--entry-desc", opts.entry_desc]
            if opts.no_build:
                argv.append("--no-build")
            if opts.no_verify:
                argv.append("--no-verify")
        else:
            argv = ["--category", category]
            if step == 4 and opts.force:
                argv.append("--force")
        if opts.quiet:
            argv.append("--quiet")

        code = run_step(step, argv, opts.quiet)
        name, desc = STEPS[step - 1][1], STEPS[step - 1][2]
        if code != 0:
            c.always(
                f"[{STAGE}] ✗ {row['pdf'].name} 在 {desc}（{name}）失败，退出码 {code}"
                f"（{c.human_ms(started)}ms）"
            )
            return code
        if not opts.quiet:
            c.info(f"      ✓ {desc}（{c.human_ms(started)}ms）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S7：文件夹批量导入（一个文件夹 = 一个入口，每个 PDF = 一张试卷；S1→S6 全自动）"
    )
    parser.add_argument("--folder", required=True, help="装着 PDF 的文件夹；默认用它当入口名")
    parser.add_argument("--entry", help="入口名（默认=文件夹名）")
    parser.add_argument("--entry-key", help="入口 key（路由 /<key>；默认由入口名推，推不出必须显式给）")
    parser.add_argument("--entry-icon", help="入口图标（1 个字，如「马」）")
    parser.add_argument("--entry-desc", help="入口描述")
    parser.add_argument("--paper-prefix", default="", help="卡片标题前缀（默认空，标题就是 PDF 文件名）")
    parser.add_argument("--category-prefix", help="分类名前缀（默认=入口 key）")
    parser.add_argument("--dpi", type=int, default=200, help="S1 渲染分辨率（默认 200）")
    parser.add_argument("--from-step", type=int, default=1, choices=range(1, 7), help="从第几步开始（断点续跑）")
    parser.add_argument("--only-step", type=int, choices=range(1, 7), help="只跑这一步")
    parser.add_argument("--force", action="store_true", help="让 S1/S4 覆盖已存在的产物")
    parser.add_argument("--keep-going", action="store_true", help="某份失败后继续跑下一份")
    parser.add_argument("--no-build", action="store_true", help="S6 跳过 vue-tsc/vite build")
    parser.add_argument("--no-verify", action="store_true", help="S6 跳过全部校验")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--quiet", action="store_true", help="只打印每份试卷的完成行")
    args = parser.parse_args()

    c.setup_stdio()
    c.set_quiet(args.quiet)

    folder = Path(args.folder).expanduser()
    if not folder.is_absolute():
        folder = (c.REPO_ROOT / folder).resolve()
    if not folder.is_dir():
        c.fail(f"找不到文件夹：{folder}", 1)

    # 入口名默认 = 文件夹名（用户要求）
    args.entry = (args.entry or folder.name).strip()
    args.entry_key = (args.entry_key or slug(args.entry)).strip()
    if not args.entry_key:
        # 中文文件夹名推不出 ASCII key —— 但**不能因此停住**（用户要求全自动）。
        # 用名字的短哈希当兜底：同一个文件夹名永远得到同一个 key，续跑/重跑都幂等。
        digest = hashlib.sha1(args.entry.encode("utf-8")).hexdigest()[:6]
        args.entry_key = f"entry-{digest}"
        c.warn(
            f"入口名「{args.entry}」推不出 ASCII 路由，已自动用 /{args.entry_key}；"
            f"想要好记的路由请重跑时加 --entry-key <英文短名>，例如 --entry-key marxism"
        )
    prefix = (args.category_prefix or args.entry_key).strip()
    if not slug(prefix):
        c.fail(f"--category-prefix「{prefix}」不合法（只能用 a-z 0-9 -）", 1)

    pdfs = collect_pdfs(folder)
    if not pdfs:
        c.fail(f"文件夹里没有 PDF：{folder}", 1)
    rows = plan(folder, pdfs, args.entry_key, prefix, args.paper_prefix)

    c.always(f"[{STAGE}] 文件夹：{folder}")
    c.always(
        f"[{STAGE}] 入口「{args.entry}」（/{args.entry_key}）"
        + (f"，图标 {args.entry_icon}" if args.entry_icon else "")
        + f" ← {len(pdfs)} 份试卷"
    )
    for row in rows:
        c.always(f"[{STAGE}]   {row['index']}. {row['pdf'].name}  →  分类 {row['category']} / 卡片「{row['paper']}」")
    if args.dry_run:
        c.always(f"[{STAGE}] --dry-run：只列计划，未执行（去掉 --dry-run 即开跑）")
        return 0

    started = time.perf_counter()
    failed: list[tuple[str, int]] = []
    for row in rows:
        c.always(
            f"[{STAGE}] === 试卷 {row['index']}/{len(rows)}：{row['pdf'].name}"
            f"（分类 {row['category']}）==="
        )
        code = import_one(row, args, first=row["index"] == 1)
        if code != 0:
            failed.append((row["category"], code))
            if not args.keep_going:
                c.always(
                    f"[{STAGE}] 已停下（加 --keep-going 可跳过失败项继续）。"
                    f"续跑：python pdf-ocr/7_import.py --folder \"{folder}\" "
                    f"--category-prefix {prefix} --from-step <失败那一步>"
                )
                break
        else:
            c.always(f"[{STAGE}] ✓ {row['pdf'].name} 全流程完成")

    ok = len(rows) - len(failed)
    c.always(
        f"[完成] 批量导入：成功 {ok}/{len(rows)}"
        + (f"｜失败：{', '.join(f'{k}(码 {v})' for k, v in failed)}" if failed else "")
        + f"｜总耗时 {c.human_ms(started) / 1000:.1f}s"
    )
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
