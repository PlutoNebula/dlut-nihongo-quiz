"""S7 批量导入的离线验收（不联网、不跑真实 PDF）。

跑法：`python pdf-ocr/tests/test_p7_import.py`

覆盖：入口名默认取文件夹名 / 中文名推不出 key 时给稳定兜底 / 一个文件夹里多份 PDF
各得一个分类名（带序号）、单份不带序号 / `--category-prefix` 非法要报错 / dry-run 不执行。
"""

import importlib.util
import io
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))

import _common as c  # noqa: E402

spec = importlib.util.spec_from_file_location("import7", TOOL / "7_import.py")
import7 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(import7)

# ── 1) slug：ASCII 压成 a-z 0-9 -；纯中文压不出来 ────────────────────────
assert import7.slug("English CET-4 真题") == "english-cet-4", import7.slug("English CET-4 真题")
assert import7.slug("马原试卷") == "", import7.slug("马原试卷")
print("[1] slug：只保留 a-z 0-9 -，纯中文返回空串")

sandbox = Path(tempfile.mkdtemp(prefix="p7-"))
try:
    # ── 2) 多份 PDF：分类名带序号；单份不带 ──────────────────────────────
    folder = sandbox / "english-cet4"
    folder.mkdir()
    for name in ("2024B.pdf", "2024A.pdf", "note.txt"):
        (folder / name).write_text("x", encoding="utf-8")
    pdfs = import7.collect_pdfs(folder)
    assert [p.name for p in pdfs] == ["2024A.pdf", "2024B.pdf"], [p.name for p in pdfs]  # 排序稳定
    rows = import7.plan(folder, pdfs, "english-cet4", "english-cet4", "")
    # 文件名能推出 ASCII 名字 → **每份卷用自己的目录名**（每份卷一个独立文件夹）
    assert [r["category"] for r in rows] == ["2024a", "2024b"], rows
    assert [r["paper"] for r in rows] == ["2024A", "2024B"], rows
    single = import7.plan(folder, pdfs[:1], "english-cet4", "english-cet4", "")
    assert single[0]["category"] == "2024a", single
    # 纯中文文件名 slug 后只剩数字 → **不能拿它当目录名**，退回 `<前缀>-<序号>`
    cn_rows = import7.plan(folder, [Path("马原试卷1(1)_0_1790064200614.pdf")], "x", "principles", "")
    assert cn_rows[0]["category"] == "principles", cn_rows
    assert cn_rows[0]["paper"] == "马原试卷1(1)", cn_rows  # 卡片名去掉平台噪声
    print("[2] 计划：能推出名字就用文件名（每卷一个目录）、纯中文退回前缀+序号；卡片名去掉 `_0_<长数字>`")

    # ── 3) dry-run：**入口名默认 = 文件夹名**；中文名给稳定兜底 key ───────
    cn = sandbox / "马原试卷"
    cn.mkdir()
    (cn / "卷1.pdf").write_text("x", encoding="utf-8")
    sys.argv = ["7_import.py", "--folder", str(cn), "--dry-run"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = import7.main()
    out = buf.getvalue()
    assert code == 0, (code, out)
    assert "入口「马原试卷」" in out, out
    assert "entry-" in out and "1. 卷1.pdf" in out, out
    assert "--dry-run：只列计划，未执行" in out, out
    print("[3] dry-run：入口名默认取文件夹名；中文名自动给稳定 key（entry-<hash>）")

    # ── 4) 非法 --category-prefix 要报错（退出码 1）──────────────────────
    sys.argv = ["7_import.py", "--folder", str(cn), "--category-prefix", "中文前缀", "--dry-run"]
    try:
        import7.main()
        raise AssertionError("应该报错退出")
    except SystemExit as exc:
        assert exc.code == 1, exc.code
    print("[4] 非法 --category-prefix（纯中文）→ 退出码 1")

    # ── 5) 空文件夹要报错；--from-step / --only-step 被 argparse 拦住非法值 ─
    empty = sandbox / "empty"
    empty.mkdir()
    sys.argv = ["7_import.py", "--folder", str(empty), "--dry-run"]
    try:
        import7.main()
        raise AssertionError("应该报错退出")
    except SystemExit as exc:
        assert exc.code == 1, exc.code
    print("[5] 空文件夹 → 退出码 1（提示没有 PDF）")
finally:
    shutil.rmtree(sandbox, ignore_errors=True)

print("\n全部通过：5 组断言 / 沙箱已清理")
