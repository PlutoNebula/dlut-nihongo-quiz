"""一次性手工撤销发布：把「马克思主义原理」（图标 马）入口卡连同它下面的试卷从站点删掉。

删掉的（站点侧 + 发布器产物）：
  * src/config/entries.ts        —— 整个入口块
  * src/config/courseTree.ts     —— 分组「马克思主义原理」及其叶子
  * src/types/question.ts        —— Category 联合类型里的 key
  * src/config/categories.ts     —— 分类块
  * src/config/categories.test.ts—— 期望 key 列表
  * scripts/generate-meta.mjs    —— 题库清单
  * scripts/audit-banks.mjs      —— 审计清单
  * public/_meta.json            —— 元数据条目
  * public/Principles-of-Marxism-question-bank.json  —— 题库（卡片内容）
  * data/processed/Principles-of-Marxism-check.json + -validation-report.json

**保留**（这些是花过 token 的原始材料，随时能重新发布）：
  data/raw/Principles-of-Marxism/ 与 pdf-ocr/work/Principles-of-Marxism/
  想连它们一起删，跑完这个脚本后手动删这两个目录即可。
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def edit(rel: str, pattern: str, repl: str = "") -> None:
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, repl, text, count=1)
    if count == 0:
        print(f"  ⚠ 没匹配上：{rel}")
        return
    path.write_text(new, encoding="utf-8", newline="")
    print(f"  ✓ 已删：{rel}")


TAIL = r"(?:.*\r?\n)*?  \},\r?\n"
edit("src/config/entries.ts", r"  \{\r?\n    key: 'principles-of-marxism',\r?\n" + TAIL)
edit(
    "src/config/courseTree.ts",
    r"  \{\r?\n    type: 'group',\r?\n    key: 'Principles-of-Marxism-group',\r?\n" + TAIL,
)
edit("src/types/question.ts", r"\r?\n  \| 'Principles-of-Marxism'")
edit("src/config/categories.ts", r"  \{\r?\n    key: 'Principles-of-Marxism',\r?\n" + TAIL)
edit("src/config/categories.test.ts", r"      'Principles-of-Marxism',\r?\n")
edit("scripts/generate-meta.mjs", r"\['Principles-of-Marxism', ", "[")
edit("scripts/audit-banks.mjs", r"  'Principles-of-Marxism-question-bank\.json',\r?\n")
edit("public/_meta.json", r'  "Principles-of-Marxism": \{\r?\n' + TAIL)

for rel in (
    "public/Principles-of-Marxism-question-bank.json",
    "data/processed/Principles-of-Marxism-check.json",
    "data/processed/Principles-of-Marxism-validation-report.json",
):
    path = ROOT / rel
    if path.exists():
        path.unlink()
        print(f"  ✓ 已删除文件：{rel}")
    else:
        print(f"  （本来就不存在）{rel}")

print("保留（需要时可手动删）：data/raw/Principles-of-Marxism/ 、 pdf-ocr/work/Principles-of-Marxism/")
