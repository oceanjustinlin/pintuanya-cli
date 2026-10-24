"""Catalogue creation, category changes and top/bottom sorting plans."""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit


def create_body(group, spec):
    from pintuanya import cents
    if 'variants' in spec:
        variants = spec['variants']
        if not isinstance(variants, list) or not variants:
            raise ValueError('variants 必须是非空规格数组')
        names = [v.get('name', '').strip() if isinstance(v, dict) else '' for v in variants]
        if not all(names) or len(set(names)) != len(names):
            raise ValueError('规格名称不能为空或重复')
        common = {k:v for k,v in spec.items() if k != 'variants'}
        if any('price' not in v for v in variants):
            raise ValueError('每个规格需要 price（人民币元）')
        common['price'] = variants[0]['price']
        body = create_body(group, common)
        body['sku_list'] = []
        for name, variant in zip(names, variants):
            one = create_body(group, {**common, **{k:v for k,v in variant.items() if k != 'name'}})
            sku = one['sku_list'][0]
            sku['sku_info_list'] = [{'name':'默认规格', 'value':name}]
            body['sku_list'].append(sku)
        body['sku_attr_list'] = [{'attr_name':'默认规格', 'attr_value_list':names}]
        return body
    title = spec.get('title', '').strip()
    if 'price' not in spec:
        raise ValueError('创建需要 price（人民币元）')
    images = spec.get('images', [])
    if not title or not spec.get('category_uuid'):
        raise ValueError('创建需要 title 和 category_uuid')
    if not isinstance(images, list) or not images:
        raise ValueError('创建至少需要一张 HTTPS 图片')
    for url in images:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('图片必须是无凭据的 HTTPS URL')
    stock = spec.get('stock', 0)
    if type(stock) is not int or not 0 <= stock <= 100000:
        raise ValueError('stock 必须为 0 至 100000 的整数；0 表示不限')
    if len(images[0]) > 128:
        raise ValueError('SKU 封面 URL 超过后台 128 字符限制，请使用短地址')
    return {'groupon_uuid': group, 'goods_uuid': '', 'category_uuid': spec['category_uuid'],
            'title': title, 'unit': spec.get('unit', '张'), 'cover': images[0], 'image_list': images,
            'is_must_buy': 0, 'is_free_shipping': 0,
            'sku_list': [{'sku_uuid': '', 'cover': images[0], 'price': cents(str(spec['price'])),
                          'cost_price': 0, 'stock': stock, 'sku_number': '', 'is_freeze': 0}]}


def matches(item, body):
    return (all(item.get(k) == body.get(k) for k in ('title','category_uuid','cover','image_list','unit'))
            and item.get('is_offline') == 0
            and item.get('sku_attr_list') == body.get('sku_attr_list')
            and len(item.get('sku_list') or []) == len(body['sku_list'])
            and all(all(actual.get(k) == expected.get(k)
                        for k in ('price','stock','is_freeze','cover','sku_info_list'))
                    for actual, expected in zip(item['sku_list'],body['sku_list'])))


def execute(client, group, op, state, save):
    action = op['action']
    if action == 'create':
        body = op['body']
        items = client.category_goods(group, body['category_uuid'])
        same = [g for g in items if g['title'] == body['title']]
        if state.get('status') in ('sending','done'):
            if len(same) != 1 or not matches(same[0], body):
                raise ValueError('创建结果不确定或已变化；停止且不重复创建，请人工核对')
            state.update(status='done', goods_uuid=same[0]['goods_uuid']); save(); return
        if same:
            raise ValueError('同名商品已存在；停止创建')
        state['status'] = 'sending'; save()
        client.request('POST', '/groupon/goods', body=body)
        items = client.category_goods(group, body['category_uuid'])
        same = [g for g in items if g['title'] == body['title']]
        if len(same) != 1 or not matches(same[0], body):
            raise ValueError('创建请求已发送但回查不一致；禁止重复创建')
        state.update(status='done', goods_uuid=same[0]['goods_uuid']); save()
        return
    uid = op['goods_uuid']
    before = op['before']
    source = client.category_goods(group, before['category_uuid'])
    target = client.category_goods(group, op['category_uuid']) if action == 'category' else source
    current = next((g for g in source + target if g['goods_uuid'] == uid), None)
    if not current:
        raise ValueError('找不到计划中的商品')
    if action == 'category':
        if state.get('status') and current['category_uuid'] == op['category_uuid']:
            state['status'] = 'done'; save(); return
        if current != before:
            raise ValueError('商品状态与计划不符')
        body = {'groupon_uuid':group, 'goods_uuid':uid, 'source_category_uuid':before['category_uuid'],
                'target_category_uuid':op['category_uuid']}
        state['status'] = 'sending'; save()
        client.request('PUT','/groupon/goods/category',body=body)
        after = client.category_goods(group,op['category_uuid'])
        if not any(g['goods_uuid'] == uid for g in after):
            raise ValueError('分类回查失败')
    else:
        # TOP/BOTTOM are absolute moves, so an interrupted request can be reconciled.
        if current.get('is_offline') != 0:
            raise ValueError('排序只支持上架中商品')
        def content(item):
            return {k:v for k,v in item.items() if k != 'sorting'}
        if content(current) != content(before):
            raise ValueError('商品状态与计划不符')
        # A later TOP in this plan may have moved this already completed item.
        if state.get('status') == 'done':
            return
        source = [g for g in source if g.get('is_offline', 0) == 0]
        position = 0 if op['direction'] == 'TOP' else -1
        if state.get('status') and source and source[position]['goods_uuid'] == uid:
            state['status'] = 'done'; save(); return
        state['status'] = 'sending'; save()
        client.request('PUT','/groupon/goods/sorting',body={'groupon_uuid':group,
            'category_uuid':before['category_uuid'],'goods_uuid':uid,
            'direction':3 if op['direction'] == 'TOP' else 4})
        after = [g for g in client.category_goods(group,before['category_uuid']) if g.get('is_offline', 0) == 0]
        if not after or after[position]['goods_uuid'] != uid:
            raise ValueError('排序回查失败')
    state['status'] = 'done'; save()


def prepare(client, group, specs):
    categories = {c['category_uuid'] for c in client.categories(group)}
    operations = []
    seen_titles, seen_ids = set(), set()
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError('目录操作必须是 JSON 对象')
        action = spec.get('action','create')
        if action == 'create':
            body = create_body(group,spec)
            if body['category_uuid'] not in categories or body['title'] in seen_titles:
                raise ValueError('分类不存在或创建标题重复')
            seen_titles.add(body['title'])
            if any(g['title'] == body['title'] for g in client.category_goods(group,body['category_uuid'])):
                raise ValueError('同名商品已存在')
            operations.append({'action':action,'body':body})
        elif action in ('category','sort'):
            required = ('goods_uuid', 'source_category_uuid',
                        'category_uuid' if action == 'category' else 'direction')
            if any(not spec.get(key) for key in required):
                raise ValueError('分类/排序缺少必要字段')
            uid = spec['goods_uuid']
            if uid in seen_ids:
                raise ValueError('同一计划内商品只能有一个修改动作')
            seen_ids.add(uid)
            items = client.category_goods(group,spec['source_category_uuid'])
            before = next((g for g in items if g['goods_uuid'] == uid),None)
            if not before: raise ValueError('找不到商品')
            op = {'action':action,'goods_uuid':uid,'before':before}
            if action == 'category':
                if spec['category_uuid'] not in categories: raise ValueError('目标分类不存在')
                op['category_uuid'] = spec['category_uuid']
            else:
                if spec['direction'] not in ('TOP','BOTTOM'): raise ValueError('仅支持 TOP/BOTTOM 绝对排序')
                op['direction'] = spec['direction']
            operations.append(op)
        else: raise ValueError('不支持的目录动作')
    if not operations: raise ValueError('计划不能为空')
    return operations


def run(client, document, path, yes=False):
    from pintuanya import digest
    checked = deepcopy(document)
    checksum = checked.pop('sha256',None)
    if (checksum != digest(checked) or document.get('kind') != 'catalog'
            or document.get('version') != 1 or not document.get('operations')):
        raise ValueError('目录计划校验失败')
    group = document['groupon_uuid']
    if not yes and input(f"输入 APPLY {len(document['operations'])} 执行: ") != f"APPLY {len(document['operations'])}":
        raise ValueError('已取消')
    state_path = Path(str(path)+'.state.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {'sha256':checksum,'items':{}}
    if state.get('sha256') != checksum: raise ValueError('执行记录不属于本计划')
    def save():
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(state_path)
    for index, op in enumerate(document['operations']):
        item = state['items'].setdefault(str(index),{})
        execute(client,group,op,item,save)
        print(json.dumps({'index':index,**item},ensure_ascii=False))


def upload_image(client, path):
    """Use the same MD5/token/audit flow as the web client (Qiniu region z0)."""
    import hashlib
    import time
    import uuid
    import urllib.request
    import os
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('图片超过 20 MB')
    data = path.read_bytes()
    suffix = path.suffix.lower().lstrip('.')
    signatures = {'png': data.startswith(b'\x89PNG\r\n\x1a\n'),
                  'jpg': data.startswith(b'\xff\xd8\xff'),
                  'jpeg': data.startswith(b'\xff\xd8\xff'),
                  'webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP'}
    if not signatures.get(suffix):
        raise ValueError('仅支持内容与扩展名一致的 PNG/JPEG/WebP')
    md5 = hashlib.md5(data).hexdigest()
    existing = client.request('GET','/file',params={'dir':'groupon-goods','digest':md5}).get('data') or {}
    if existing.get('url'):
        return existing['url']
    token = client.request('POST','/file/token',body={'dir':'groupon-goods','digest':md5,'suffix':suffix}).get('data') or {}
    if not token.get('token') or not token.get('key'):
        raise ValueError('未获取到上传凭据')
    key = token['key']
    if not key.startswith('groupon-goods/') or any(c in key for c in '\r\n"'):
        raise ValueError('上传路径异常')
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in [('key',key),('token',token['token'])]:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="image.{suffix}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    chunks.extend([data,f'\r\n--{boundary}--\r\n'.encode()])
    request = urllib.request.Request('https://upload.qiniup.com/',data=b''.join(chunks),method='POST',
        headers={'Content-Type':f'multipart/form-data; boundary={boundary}'})
    try:
        opener = urllib.request.urlopen
        if os.getenv('PINTUANYA_TRANSPORT') == 'curl':
            from http_transport import upload
            opener = upload
        with opener(request,timeout=60) as response:
            result = json.load(response)
    except (OSError,ValueError):
        raise ValueError('图片上传失败；未输出上传凭据，请先回查再重试') from None
    if isinstance(result, dict) and result.get('key', key) != key:
        raise ValueError('上传返回的文件标识不一致')
    for _ in range(10):
        status = client.request('GET','/file/audit/status',params={'digest':md5}).get('data') or {}
        if status.get('status') == 2:
            return 'https://img.pintuanya.com/' + key
        if status.get('status') == 3:
            raise ValueError('图片审核未通过')
        time.sleep(1)
    raise ValueError('图片审核尚未完成；请稍后回查，勿直接使用')
