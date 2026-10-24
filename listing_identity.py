"""Conservative candidate detection; never authorizes automatic SKU merging."""
from difflib import SequenceMatcher
import re
import unicodedata


def work_key(title):
    if not isinstance(title, str):
        return None
    title = unicodedata.normalize('NFKC', title).strip()
    parts = re.split(r'\s+[-–—]\s+', title, maxsplit=1)
    if len(parts) != 2 and len(re.findall(r'[-–—]', title)) == 1:
        parts = re.split(r'[-–—]', title, maxsplit=1)
    if len(parts) != 2:
        return None
    parts[1] = re.sub(r'\s*\(\d{4}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?\)\s*$', '', parts[1])
    key = tuple(' '.join(re.findall(r'[^\W_]+', part.casefold())) for part in parts)
    return key if all(key) else None


def candidate_kind(left, right):
    a, b = work_key(left), work_key(right)
    if not a or not b:
        return None
    if a == b:
        return 'same'
    # Both fields must independently be similar. A candidate only blocks creation.
    if all(x == y or SequenceMatcher(None, x, y).ratio() >= 0.85
           or (index == 1 and (x.startswith(y + ' ') or y.startswith(x + ' ')))
           for index, (x, y) in enumerate(zip(a, b))):
        return 'fuzzy'
    return None


def reject_existing_work(title, goods):
    candidates = [(item, candidate_kind(title, item.get('title'))) for item in goods]
    candidates = [(item, kind) for item, kind in candidates if kind]
    if not candidates:
        return
    ids = ', '.join(str(item.get('goods_uuid', '未知UUID')) for item, _ in candidates)
    if len(candidates) > 1 or any(kind == 'fuzzy' for _, kind in candidates):
        raise ValueError(f'发现疑似或多个同一作品候选（{ids}）；核实 Artist 和 Title 后追加规格，禁止新建')
    raise ValueError(f'同一作品已存在（{ids}）；须核对原规格并用 append-variants 追加规格，相同规格不重复添加，禁止新建替代')


def catalog_goods(client, group):
    """Empty category selects the whole group, including unclassified/offline goods."""
    items = {}
    for item in client.category_goods(group, ''):
        uid = item.get('goods_uuid')
        if not uid or (uid in items and items[uid] != item):
            raise ValueError('全拼团商品快照不完整或不一致，停止查重')
        items[uid] = item
    return list(items.values())
