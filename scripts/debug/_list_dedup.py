import json
import pathlib
lines = pathlib.Path('data/processed/ai-dedup-hist-d.jsonl').read_text(encoding='utf-8').strip().split('\n')
judged = [json.loads(l) for l in lines]
yes = [x for x in judged if x['ai_same_kp'] is True]
out = []
out.append(f'同知识点 {len(yes)} 对:')
for y in yes:
    out.append(f"  {y['a_id']} <-> {y['b_id']}  score={y['score']:.2f}  {y['ai_reason']}")
pathlib.Path('_dedup_list.txt').write_text('\n'.join(out), encoding='utf-8')
