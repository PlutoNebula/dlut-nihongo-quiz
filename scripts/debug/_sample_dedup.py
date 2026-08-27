import json
import pathlib

data = json.loads(pathlib.Path('public/history-question-bank.json').read_text(encoding='utf-8'))
by_id = {q['id']: q for q in data}

sample_pairs = [
    ('hist-d-q00122', 'hist-d-q00140'),  # 第一部资产阶级宪法
    ('hist-d-q00211', 'hist-d-q00212'),  # 毛泽东农村中心思想
    ('hist-d-q00085', 'hist-d-q00086'),  # 正反考查
    ('hist-d-q00143', 'hist-d-q00146'),  # 五四运动开端
    ('hist-d-q00026', 'hist-d-q00030'),  # 师夷长技
    ('hist-d-q00150', 'hist-d-q00154'),  # 最早党组织上海
    ('hist-d-q00274', 'hist-d-q00277'),  # 七届二中全会
]

out = []
for a_id, b_id in sample_pairs:
    a = by_id.get(a_id)
    b = by_id.get(b_id)
    if not a or not b:
        out.append(f'缺题: {a_id} / {b_id}')
        continue
    out.append(f'\n=== {a_id} ↔ {b_id} ===')
    out.append(f'A [{a["questionType"]}] {a["stem"]}')
    for o in a['options']:
        out.append(f'  {o["key"]}. {o["text"]}')
    out.append(f'  答案: {a["answerKey"]}')
    out.append(f'B [{b["questionType"]}] {b["stem"]}')
    for o in b['options']:
        out.append(f'  {o["key"]}. {o["text"]}')
    out.append(f'  答案: {b["answerKey"]}')

pathlib.Path('_sample_dedup.txt').write_text('\n'.join(out), encoding='utf-8')
