"""Offline acceptance of existing listing primitives, not live API semantics."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

import catalog


class ListingWorkflowTest(unittest.TestCase):
    def spec(self):
        return {
            'title': 'Artist - Album (2026-11-20)',
            'category_uuid': 'new',
            'images': ['https://example.com/red.png'],
            'variants': [{
                'name': 'Artist Store Exclusive - Opaque Red - 2LP - Signed Card (/1000)',
                'price': '315',
            }],
        }

    def test_single_version_keeps_explicit_specification_and_unlimited_encoding(self):
        body = catalog.create_body('group', self.spec())
        self.assertEqual(len(body['sku_list']), 1)
        self.assertEqual(body['sku_attr_list'][0]['attr_value_list'],
                         [self.spec()['variants'][0]['name']])
        self.assertEqual(body['sku_list'][0]['stock'], 0)
        self.assertEqual(body['sku_list'][0]['cover'], body['image_list'][0])

    def test_explicit_stock_is_not_replaced_by_edition_limit(self):
        spec = self.spec()
        spec['variants'][0]['stock'] = 7
        self.assertEqual(catalog.create_body('group', spec)['sku_list'][0]['stock'], 7)

    def test_missing_images_prevents_creation(self):
        spec = self.spec()
        spec.pop('images')
        with self.assertRaisesRegex(ValueError, '图片'):
            catalog.create_body('group', spec)

    def test_plan_rejects_title_without_artist_or_title(self):
        for title in ['Album', ' - Album', 'Artist - ', 'Artist-Album']:
            with self.subTest(title=title):
                spec = self.spec()
                spec['title'] = title
                client = Mock()
                client.categories.return_value = [{'category_uuid': 'new'}]
                client.category_goods.return_value = []
                with self.assertRaisesRegex(ValueError, 'Artist - Title'):
                    catalog.prepare(client, 'group', [spec])
                client.request.assert_not_called()

    def test_plan_requires_explicit_variant_even_for_one_version(self):
        spec = self.spec()
        spec.pop('variants')
        spec['price'] = '315'
        client = Mock()
        client.categories.return_value = [{'category_uuid': 'new'}]
        client.category_goods.return_value = []
        with self.assertRaisesRegex(ValueError, 'variants'):
            catalog.prepare(client, 'group', [spec])
        client.request.assert_not_called()

    def test_compliant_plan_retains_single_explicit_variant(self):
        client = Mock()
        client.categories.return_value = [{'category_uuid': 'new'}]
        client.category_goods.return_value = []
        operations = catalog.prepare(client, 'group', [self.spec()])
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]['body']['sku_attr_list'][0]['attr_value_list'],
                         [self.spec()['variants'][0]['name']])
        client.request.assert_not_called()

    def test_duplicate_scan_uses_full_group_including_unclassified_goods(self):
        client = Mock()
        client.categories.return_value = [{'category_uuid': 'new'}]
        client.category_goods.side_effect = lambda group, category: (
            [{'goods_uuid': 'unclassified', 'title': 'Artist - Album'}] if category == '' else [])
        with self.assertRaisesRegex(ValueError, '追加规格'):
            catalog.prepare(client, 'group', [self.spec()])
        client.request.assert_not_called()

    def test_existing_work_in_another_category_blocks_new_product(self):
        client = Mock()
        client.categories.return_value = [
            {'category_uuid': 'new'}, {'category_uuid': 'old'},
        ]
        client.category_goods.side_effect = lambda group, category: (
            [{'goods_uuid': 'existing', 'title': 'ARTIST - Album',
              'category_uuid': 'old', 'is_offline': 1}] if category in ('old', '') else [])
        with self.assertRaisesRegex(ValueError, '追加规格'):
            catalog.prepare(client, 'group', [self.spec()])
        client.request.assert_not_called()

    def test_artist_and_title_must_both_match(self):
        for title in ['Other Artist - Album', 'Artist - Completely Different Work']:
            with self.subTest(title=title):
                client = Mock()
                client.categories.return_value = [{'category_uuid': 'new'}]
                client.category_goods.return_value = [{'goods_uuid': 'unrelated', 'title': title}]
                self.assertEqual(len(catalog.prepare(client, 'group', [self.spec()])), 1)

    def test_fuzzy_work_match_requires_review_and_blocks_create(self):
        client = Mock()
        client.categories.return_value = [{'category_uuid': 'new'}]
        client.category_goods.return_value = [
            {'goods_uuid': 'existing', 'title': 'Artist - Albumm'},
        ]
        with self.assertRaisesRegex(ValueError, '疑似'):
            catalog.prepare(client, 'group', [self.spec()])
        client.request.assert_not_called()

    def test_legacy_version_suffix_is_a_candidate_not_silently_ignored(self):
        for title in ['Artist - Album (Opaque Apple Red Vinyl)', 'Artist - Album (Deluxe Edition)']:
            with self.subTest(title=title):
                client = Mock()
                client.categories.return_value = [{'category_uuid': 'new'}]
                client.category_goods.return_value = [{'goods_uuid': 'legacy', 'title': title}]
                with self.assertRaisesRegex(ValueError, '疑似'):
                    catalog.prepare(client, 'group', [self.spec()])
                client.request.assert_not_called()

    def test_same_work_twice_in_one_plan_is_rejected(self):
        client = Mock()
        client.categories.return_value = [{'category_uuid': 'new'}]
        client.category_goods.return_value = []
        second = self.spec()
        second['title'] = 'artist - album'
        second['variants'][0]['name'] = 'Blue - Vinyl'
        with self.assertRaisesRegex(ValueError, '同一作品'):
            catalog.prepare(client, 'group', [self.spec(), second])
        client.request.assert_not_called()

    def test_new_cross_category_match_at_apply_blocks_post(self):
        body = catalog.create_body('group', self.spec())
        client = Mock()
        client.categories.return_value = [{'category_uuid': 'new'}, {'category_uuid': 'old'}]
        client.category_goods.side_effect = lambda group, category: (
            [{'goods_uuid': 'existing', 'title': 'Artist - Album'}] if category in ('old', '') else [])
        with self.assertRaisesRegex(ValueError, '追加规格'):
            catalog.execute(client, 'group', {
                'action': 'create', 'body': body, 'identity_check': True,
            }, {}, Mock())
        client.request.assert_not_called()

    def test_missing_new_uuid_prevents_plan_without_writes(self):
        client = Mock()
        client.categories.return_value = [{'category_uuid': 'other'}]
        with self.assertRaisesRegex(ValueError, '分类不存在'):
            catalog.prepare(client, 'group', [self.spec()])
        client.request.assert_not_called()

    def test_created_product_verification_rejects_each_material_difference(self):
        body = catalog.create_body('group', self.spec())
        expected = dict(deepcopy(body), goods_uuid='created', is_offline=0)
        self.assertTrue(catalog.matches(expected, body))
        changes = [
            ('title', 'Other - Album'), ('category_uuid', 'other'),
            ('cover', 'https://example.com/wrong.png'),
            ('image_list', ['https://example.com/wrong.png']), ('is_offline', 1),
            ('sku_attr_list', []),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                changed = deepcopy(expected)
                changed[field] = value
                self.assertFalse(catalog.matches(changed, body))
        for field, value in [
            ('price', 1), ('stock', 1000), ('is_freeze', 1),
            ('cover', 'https://example.com/wrong.png'),
            ('sku_info_list', [{'name': '默认规格', 'value': 'Wrong version'}]),
        ]:
            with self.subTest(sku_field=field):
                changed = deepcopy(expected)
                changed['sku_list'][0][field] = value
                self.assertFalse(catalog.matches(changed, body))

    def test_success_response_without_top_position_does_not_complete_sort(self):
        before = {'goods_uuid': 'created', 'category_uuid': 'new', 'is_offline': 0}
        client = Mock()
        client.category_goods.return_value = [{'goods_uuid': 'other'}, before]
        client.request.return_value = {'code': 0}
        state = {}
        with self.assertRaisesRegex(ValueError, '排序回查失败'):
            catalog.execute(client, 'group', {
                'action': 'sort', 'goods_uuid': 'created',
                'before': before, 'direction': 'TOP',
            }, state, Mock())
        self.assertEqual(state['status'], 'sending')

    def test_interrupted_sort_at_top_is_reconciled_without_another_write(self):
        before = {'goods_uuid': 'created', 'category_uuid': 'new', 'is_offline': 0}
        client = Mock()
        client.category_goods.return_value = [before, {'goods_uuid': 'other'}]
        state = {'status': 'sending'}
        catalog.execute(client, 'group', {
            'action': 'sort', 'goods_uuid': 'created',
            'before': before, 'direction': 'TOP',
        }, state, Mock())
        self.assertEqual(state['status'], 'done')
        client.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
