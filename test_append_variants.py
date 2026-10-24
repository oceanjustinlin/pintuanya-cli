from copy import deepcopy
import unittest
from unittest.mock import Mock

import catalog


def existing():
    return {'goods_uuid': 'old', 'category_uuid': 'new', 'title': 'Artist - Album',
            'cover': 'https://example.com/old.jpg', 'image_list': ['https://example.com/old.jpg'],
            'unit': '', 'is_offline': 0, 'is_must_buy': 0, 'is_free_shipping': 0,
            'starting_purchase': 0, 'limit_purchase': 0,
            'sku_attr_list': [{'attr_name': '默认规格', 'attr_value_list': ['Signed Red']}],
            'sku_list': [{'sku_uuid': 'original-sku', 'cover': 'https://example.com/old.jpg',
                          'price': 41800, 'stock': 15, 'sale': 15, 'cost_price': 0,
                          'sku_number': '', 'is_freeze': 0,
                          'sku_info_list': [{'name': '默认规格', 'value': 'Signed Red'}]}]}


def spec():
    return {'action': 'append-variants', 'goods_uuid': 'old', 'source_category_uuid': 'new',
            'images': ['https://example.com/yellow.jpg'],
            'variants': [{'name': 'Yellow - 2LP', 'price': '318'}]}


class AppendVariantsTest(unittest.TestCase):
    def client(self, item):
        c = Mock()
        c.categories.return_value = [{'category_uuid': 'new'}]
        c.category_goods.return_value = [item]
        return c

    def operation(self):
        before = existing()
        return catalog.prepare(self.client(before), 'g', [spec()])[0]

    def after(self, op):
        item = deepcopy(op['before'])
        item['cover'] = op['body']['cover']
        item['image_list'] = op['body']['image_list']
        item['sku_attr_list'] = op['body']['sku_attr_list']
        item['sku_list'].append(dict(deepcopy(op['body']['sku_list'][-1]), sku_uuid='new-sku', sale=0))
        return item

    def test_plan_preserves_original_sku_and_adds_unlimited_318_variant(self):
        op = self.operation()
        self.assertEqual(op['body']['sku_list'][0]['sku_uuid'], 'original-sku')
        self.assertEqual(op['body']['sku_list'][0]['stock'], 15)
        self.assertEqual(op['body']['sku_list'][0]['price'], 41800)
        self.assertEqual(op['body']['sku_list'][-1]['price'], 31800)
        self.assertEqual(op['body']['sku_list'][-1]['stock'], 0)
        self.assertEqual(op['body']['cover'], 'https://example.com/yellow.jpg')
        self.assertEqual(op['body']['image_list'],
                         ['https://example.com/yellow.jpg', 'https://example.com/old.jpg'])
        self.assertEqual(op['body']['sku_list'][0]['cover'], existing()['sku_list'][0]['cover'])

    def test_primary_image_uses_first_new_variant_image(self):
        s = spec()
        s['variants'][0]['images'] = ['https://example.com/variant-yellow.jpg']
        s['variants'].append({'name': 'Blue - 2LP', 'price': '330',
                              'images': ['https://example.com/blue.jpg']})
        op = catalog.prepare(self.client(existing()), 'g', [s])[0]
        self.assertEqual(op['body']['cover'], 'https://example.com/variant-yellow.jpg')
        self.assertEqual(op['body']['image_list'][0], op['body']['cover'])
        self.assertIn('https://example.com/old.jpg', op['body']['image_list'])
        self.assertIn('https://example.com/blue.jpg', op['body']['image_list'])

    def test_unchanged_main_image_fails_append_verification(self):
        op = self.operation(); after = self.after(op)
        after['cover'] = existing()['cover']
        c = self.client(existing()); c.category_goods.side_effect = [[existing()], [after]]
        state = {}
        with self.assertRaises(ValueError): catalog.execute(c, 'g', op, state, Mock())
        self.assertEqual(state['status'], 'sending')

    def test_same_variant_is_rejected(self):
        s = spec(); s['variants'][0]['name'] = 'Signed Red'
        with self.assertRaisesRegex(ValueError, '规格已存在'):
            catalog.prepare(self.client(existing()), 'g', [s])

    def test_apply_uses_put_and_checks_preserved_sales(self):
        op = self.operation(); after = self.after(op)
        c = self.client(existing()); c.category_goods.side_effect = [[existing()], [after]]
        state = {}; catalog.execute(c, 'g', op, state, Mock())
        self.assertEqual(state['status'], 'done')
        self.assertEqual(c.request.call_args.args, ('PUT', '/groupon/goods'))

    def test_changed_sale_prevents_write(self):
        op = self.operation(); changed = existing(); changed['sku_list'][0]['sale'] = 16
        c = self.client(changed)
        with self.assertRaises(ValueError): catalog.execute(c, 'g', op, {}, Mock())
        c.request.assert_not_called()

    def test_resume_after_response_loss_never_sends_again(self):
        op = self.operation(); c = self.client(self.after(op)); state = {'status': 'sending'}
        catalog.execute(c, 'g', op, state, Mock())
        self.assertEqual(state['status'], 'done'); c.request.assert_not_called()

    def test_uncertain_append_in_before_state_is_not_repeated(self):
        op = self.operation(); c = self.client(existing())
        with self.assertRaises(ValueError): catalog.execute(c, 'g', op, {'status': 'sending'}, Mock())
        c.request.assert_not_called()

    def test_old_sku_changes_fail_post_verification(self):
        op = self.operation(); after = self.after(op); after['sku_list'][0]['price'] = 31800
        c = self.client(existing()); c.category_goods.side_effect = [[existing()], [after]]
        state = {}
        with self.assertRaises(ValueError): catalog.execute(c, 'g', op, state, Mock())
        self.assertEqual(state['status'], 'sending')

    def test_implicit_single_sku_requires_explicit_original_name(self):
        item = existing(); item['sku_attr_list'] = None; item['sku_list'][0]['sku_info_list'] = None
        with self.assertRaises(ValueError): catalog.prepare(self.client(item), 'g', [spec()])
        s = spec(); s['existing_variant_name'] = 'VMP'
        op = catalog.prepare(self.client(item), 'g', [s])[0]
        self.assertEqual(op['body']['sku_list'][0]['sku_uuid'], 'original-sku')
        self.assertEqual(op['body']['sku_attr_list'][0]['attr_value_list'], ['VMP', 'Yellow - 2LP'])

    def test_conversion_verifies_old_sale_and_freeze_while_adding_name(self):
        item = existing(); item['sku_attr_list'] = None; item['sku_list'][0]['sku_info_list'] = None
        item['sku_list'][0]['is_freeze'] = 1
        s = spec(); s['existing_variant_name'] = 'VMP'
        op = catalog.prepare(self.client(item), 'g', [s])[0]
        after = deepcopy(item)
        after['cover'] = op['body']['cover']
        after['sku_attr_list'] = op['body']['sku_attr_list']; after['image_list'] = op['body']['image_list']
        after['sku_list'][0]['sku_info_list'] = op['body']['sku_list'][0]['sku_info_list']
        after['sku_list'].append(dict(op['body']['sku_list'][1],sku_uuid='new-sku',sale=0))
        c = self.client(item); c.category_goods.side_effect = [[item], [after]]
        state = {}; catalog.execute(c,'g',op,state,Mock())
        self.assertEqual(state['status'],'done')
        self.assertEqual(c.request.call_args.kwargs['body']['sku_list'][0]['is_freeze'],1)


if __name__ == '__main__': unittest.main()
