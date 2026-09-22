"""S6 发布器的入口/位置逻辑离线验收（不联网、0 token、不碰真文件）。

跑法：`python pdf-ocr/tests/test_p6_publish.py`

覆盖：
  * 入口已存在 → 把试卷加进去（不新建）
  * 入口不存在 → 新建入口再挂试卷（`--entry` + `--entry-key`）
  * 中文入口名推不出 key 时必须报错退出（不猜）
  * 位置参数：`--position N` / `--before` / `--after` / `--last`
  * 幂等：同样的输入不产生改动
  * 一份试卷只能属于一个入口（会从别的入口摘掉）
"""

import importlib.util
import pathlib
import sys

TOOL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))

spec = importlib.util.spec_from_file_location("pub6", TOOL / "6_publish.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)

SAMPLE = """\
/** 文件头注释 */

import type { Category } from '../types/question'

export const ENTRIES: EntryMeta[] = [
  {
    key: 'computer-organization',
    name: '计算机组成（软国际）',
    icon: '组',
    papers: [
      'computer-2021-final',
      'computer-2024-final',
      'computer-c-exam',
    ],
  },
]

export function findEntry() {}
"""


def meta_for(entry_key: str, entry_name: str, **kw) -> dict:
    return {
        "short": '2026期中（软国）',
        "long": '2026期中（软国）',
        "entryKey": entry_key,
        "entryName": entry_name,
        "entryIcon": "组",
        "entryDesc": "",
        **kw,
    }


def papers_of(text: str, entry_key: str) -> list[str]:
    _, entries, _ = pub.parse_entries(text)
    for entry in entries:
        if entry["key"] == entry_key:
            return entry["papers"]
    return []


# ① 解析
_, entries, _ = pub.parse_entries(SAMPLE)
assert len(entries) == 1, entries
assert entries[0]["papers"] == ["computer-2021-final", "computer-2024-final", "computer-c-exam"]
# 渲染一次再解析，必须完全等价（幂等的根基）
_start = SAMPLE.index(pub.ENTRIES_MARKER) + len(pub.ENTRIES_MARKER)
_end = SAMPLE.index("\n]\n", _start)
_rebuilt = SAMPLE[:_start] + pub.render_entries(entries) + SAMPLE[_end:]
assert pub.parse_entries(_rebuilt)[1] == entries, pub.parse_entries(_rebuilt)[1]
print("[1] parse_entries / render_entries 往返一致")

# ② 入口已存在 → 直接加进去，不新建
text = SAMPLE
key, name, icon, desc, created = pub.resolve_entry(
    text, "computer-2026-midterm", {}, "计算机组成（软国际）", None
)
assert (key, name, created) == ("computer-organization", "计算机组成（软国际）", False), (key, name, created)
updated, detail = pub.patch_entries(text, "computer-2026-midterm", meta_for(key, name), {"mode": "last"})
assert papers_of(updated, key)[-1] == "computer-2026-midterm", papers_of(updated, key)
assert "插入到" in detail, detail
print("[2] 入口存在 → 追加试卷：", detail)

# ③ 入口不存在 → 新建（并挂上试卷）
key2, name2, _, _, created2 = pub.resolve_entry(text, "english-2026", {}, "英语四级", "english-cet4")
assert (key2, name2, created2) == ("english-cet4", "英语四级", True), (key2, name2, created2)
updated2, detail2 = pub.patch_entries(text, "english-2026", meta_for(key2, name2), {"mode": "last"})
assert "新建入口" in detail2 and "英语四级" in detail2, detail2
assert papers_of(updated2, "english-cet4") == ["english-2026"], papers_of(updated2, "english-cet4")
assert papers_of(updated2, "computer-organization") == [
    "computer-2021-final",
    "computer-2024-final",
    "computer-c-exam",
], papers_of(updated2, "computer-organization")
print("[3] 入口不存在 → 新建入口：", detail2)

# ④ 中文入口名推不出 key → 必须报错（不猜、不静默）
try:
    pub.resolve_entry(text, "english-2026", {}, "英语四级", None)
except SystemExit as exc:
    assert exc.code == 1, exc.code
else:  # pragma: no cover
    raise SystemExit("中文入口名没给 --entry-key 时应该报错")
print("[4] 中文入口名没给 --entry-key → 退出码 1")

# ⑤ 位置：第一张 / 某张之后 / 最后一张
first, _ = pub.patch_entries(text, "computer-2026-midterm", meta_for("computer-organization", "计算机组成（软国际）"), {"mode": "first"})
assert papers_of(first, "computer-organization")[0] == "computer-2026-midterm"
after, _ = pub.patch_entries(text, "computer-2026-midterm", meta_for("computer-organization", "计算机组成（软国际）"), {"mode": "after", "value": "computer-2024-final"})
assert papers_of(after, "computer-organization") == [
    "computer-2021-final",
    "computer-2024-final",
    "computer-2026-midterm",
    "computer-c-exam",
], papers_of(after, "computer-organization")
last, _ = pub.patch_entries(text, "computer-2026-midterm", meta_for("computer-organization", "计算机组成（软国际）"), {"mode": "last", "explicit": True})
assert papers_of(last, "computer-organization")[-1] == "computer-2026-midterm"
# 找不到锚点时退化成追加，不报错
nope, _ = pub.patch_entries(text, "computer-2026-midterm", meta_for("computer-organization", "计算机组成（软国际）"), {"mode": "before", "value": "不存在"})
assert papers_of(nope, "computer-organization")[-1] == "computer-2026-midterm"
print("[5] 位置：first / after / last / 锚点不存在 都正确")

# ⑥ 幂等：把同一份试卷再挂一次（已在最后一张）→ 不改动
same, detail_same = pub.patch_entries(last, "computer-2026-midterm", meta_for("computer-organization", "计算机组成（软国际）"), {"mode": "last"})
assert same == last, "同位置重挂不该产生改动"
assert "无改动" in detail_same, detail_same
print("[6] 幂等：同位置重挂 →", detail_same)

# ⑦ 一份试卷只能属于一个入口：挂到新入口时要从旧入口摘掉
moved, _ = pub.patch_entries(updated, "computer-2026-midterm", meta_for("english-cet4", "英语四级"), {"mode": "last"})
assert "computer-2026-midterm" not in papers_of(moved, "computer-organization"), papers_of(moved, "computer-organization")
assert papers_of(moved, "english-cet4") == ["computer-2026-midterm"]
print("[7] 跨入口移动：旧入口已摘掉")

print("\n全部通过：7 组断言")
