"""Append explicit variants using the web client's full goods update protocol."""
from copy import deepcopy
import unicodedata


FIELDS = ('goods_uuid', 'category_uuid', 'cover', 'image_list', 'title', 'unit',
          'is_must_buy', 'is_free_shipping', 'starting_purchase', 'limit_purchase', 'tiered_pricing')
SKU_FIELDS = ('sku_uuid', 'cover', 'price', 'cost_price', 'stock', 'sku_number',
              'is_freeze', 'sku_info_list')


def build(group, before, spec):
    from catalog import create_body
    before = deepcopy(before)
    attrs = before.get('sku_attr_list') or []
    skus = before.get('sku_list') or []
    if not attrs and len(skus) == 1 and not skus[0].get('sku_info_list'):
        name = spec.get('existing_variant_name')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('旧单SKU转规格需要明确 existing_variant_name，不推测原版本')
        name = name.strip()
        attrs = [{'attr_name': '默认规格', 'attr_value_list': [name]}]
        skus[0]['sku_info_list'] = [{'name': '默认规格', 'value': name}]
    if (len(attrs) != 1 or attrs[0].get('attr_name') != '默认规格' or not skus
            or not all(s.get('sku_uuid') for s in skus)
            or len({s['sku_uuid'] for s in skus}) != len(skus)):
        raise ValueError('追加仅支持已有单维默认规格结构，需先核实原规格')
    names = attrs[0].get('attr_value_list') or []
    if names != [(s.get('sku_info_list') or [{}])[0].get('value') for s in skus]:
        raise ValueError('原规格列表不一致')
    new = create_body(group, {'title': before['title'], 'category_uuid': before['category_uuid'],
                             'images': spec.get('images', []), 'variants': spec.get('variants', [])})
    normalize = lambda name: ' '.join(unicodedata.normalize('NFKC', name).casefold().split())
    seen = {normalize(name) for name in names}
    for name in new['sku_attr_list'][0]['attr_value_list']:
        key = normalize(name)
        if key in seen:
            raise ValueError('规格已存在，不重复追加或覆盖')
        seen.add(key)
    body = {k: deepcopy(before[k]) for k in FIELDS if k in before}
    body.update(groupon_uuid=group, source_category_uuid=before['category_uuid'])
    body['cover'] = new['sku_list'][0]['cover']
    images = [body['cover']] + new['image_list']
    for variant in spec['variants']:
        images.extend(variant.get('images', []))
    images += ([before['cover']] if before.get('cover') else []) + (before.get('image_list') or [])
    body['image_list'] = list(dict.fromkeys(images))
    body['sku_attr_list'] = deepcopy(attrs)
    body['sku_attr_list'][0]['attr_value_list'] += new['sku_attr_list'][0]['attr_value_list']
    body['sku_list'] = [{k: deepcopy(s[k]) for k in SKU_FIELDS if k in s} for s in skus] + new['sku_list']
    return body


def matches(item, before, body):
    if (any(item.get(k) != body.get(k) for k in FIELDS)
            or item.get('is_offline') != before.get('is_offline')
            or item.get('sku_attr_list') != body['sku_attr_list']):
        return False
    actual = item.get('sku_list') or []
    by_id = {s.get('sku_uuid'): s for s in actual}
    if len(actual) != len(body['sku_list']) or len(by_id) != len(actual) or not all(by_id):
        return False
    old_ids = {s['sku_uuid'] for s in before['sku_list']}
    expected_old = {s['sku_uuid']: s for s in body['sku_list'] if s.get('sku_uuid')}
    for old in before['sku_list']:
        if any(by_id.get(old['sku_uuid'], {}).get(k) !=
               (expected_old[old['sku_uuid']].get(k) if k == 'sku_info_list' else v)
               for k, v in old.items()):
            return False
    added = [s for s in actual if s['sku_uuid'] not in old_ids]
    expected = body['sku_list'][len(old_ids):]
    def key(s):
        return tuple((i.get('name'), i.get('value')) for i in s.get('sku_info_list', []))
    by_spec = {key(s): s for s in added}
    return (len(by_spec) == len(expected)
            and all(all(by_spec.get(key(s), {}).get(k) == v for k, v in s.items() if k != 'sku_uuid')
                    for s in expected))


def execute(client, group, op, state, save):
    before, body = op['before'], op['body']
    def current():
        return next((g for g in client.category_goods(group, before['category_uuid'])
                     if g['goods_uuid'] == before['goods_uuid']), None)
    item = current()
    if state.get('status'):
        if not item or not matches(item, before, body):
            raise ValueError('追加结果不确定或已变化；仅回查，不重复 PUT')
        state['status'] = 'done'; save(); return
    if item != before:
        raise ValueError('商品或销售状态与追加计划不符，停止写入')
    state['status'] = 'sending'; save()
    client.request('PUT', '/groupon/goods', body=body)
    item = current()
    if not item or not matches(item, before, body):
        raise ValueError('追加后回查不一致；保留状态，禁止重复提交')
    state['status'] = 'done'; save()
