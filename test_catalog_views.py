import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

import pintuanya


class CatalogViewsTest(unittest.TestCase):
    def test_cli_reads_whole_group_order_without_resorting(self):
        client = Mock()
        client.groups.return_value = [{'groupon_uuid': 'g', 'title': 'Shop'}]
        client.category_goods.return_value = [
            {'goods_uuid': 'second-id', 'is_offline': 0, 'sorting': 4},
            {'goods_uuid': 'first-id', 'is_offline': 0, 'sorting': 5},
            {'goods_uuid': 'offline', 'is_offline': 1, 'sorting': 0},
        ]
        output = io.StringIO()
        with patch.object(pintuanya, 'Client', return_value=client), patch.dict(
                pintuanya.os.environ, {'PINTUANYA_COOKIE': 'test'}), contextlib.redirect_stdout(output):
            pintuanya.main(['--group', 'g', 'catalog-list', '--limit', '2'])
        self.assertEqual([g['goods_uuid'] for g in json.loads(output.getvalue())],
                         ['second-id', 'first-id'])
        client.category_goods.assert_called_once_with('g', '')
        client.request.assert_not_called()

    def test_cli_reads_selected_category_order(self):
        client = Mock()
        client.groups.return_value = [{'groupon_uuid': 'g', 'title': 'Shop'}]
        client.category_goods.return_value = [{'goods_uuid': 'new-first', 'is_offline': 0}]
        with patch.object(pintuanya, 'Client', return_value=client), patch.dict(
                pintuanya.os.environ, {'PINTUANYA_COOKIE': 'test'}), contextlib.redirect_stdout(io.StringIO()):
            pintuanya.main(['--group', 'g', 'catalog-list', '--category', 'new'])
        client.category_goods.assert_called_once_with('g', 'new')


if __name__ == '__main__':
    unittest.main()
