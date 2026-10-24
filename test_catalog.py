import unittest
from unittest.mock import Mock
import pintuanya
import catalog

class CatalogTest(unittest.TestCase):
    def test_create_uses_yuan_and_validates_images(self):
        body = catalog.create_body('g', {'title':'中文红胶','category_uuid':'new','price':'315','images':['https://img.example/red.png']})
        self.assertEqual(body['sku_list'][0]['price'],31500)
        self.assertEqual(body['sku_list'][0]['stock'],0)
        self.assertEqual(body['title'],'中文红胶')
        with self.assertRaises(ValueError):
            catalog.create_body('g',{'title':'a','category_uuid':'new','price':'315','images':['file:///secret']})

    def test_uncertain_create_is_never_repeated(self):
        client=Mock()
        client.request.side_effect=ValueError('timeout')
        client.category_goods.return_value=[]
        state={}
        operation={'action':'create','body':catalog.create_body('g',{'title':'a','category_uuid':'new','price':'315','images':['https://example.com/a.png']})}
        save=Mock()
        with self.assertRaises(ValueError):catalog.execute(client,'g',operation,state,save)
        self.assertEqual(state['status'],'sending')
        client.request.reset_mock()
        with self.assertRaises(ValueError):catalog.execute(client,'g',operation,state,save)
        client.request.assert_not_called()

    def test_resume_recovers_matching_created_goods(self):
        body=catalog.create_body('g',{'title':'a','category_uuid':'new','price':'315','images':['https://example.com/a.png']})
        item=dict(body,goods_uuid='id',is_offline=0)
        client=Mock();client.category_goods.return_value=[item]
        state={'status':'sending'}
        catalog.execute(client,'g',{'action':'create','body':body},state,Mock())
        self.assertEqual(state['status'],'done')
        self.assertEqual(state['goods_uuid'],'id')
        client.request.assert_not_called()

    def test_nonzero_business_error_is_rejected(self):
        import io,json
        from unittest.mock import patch
        response=io.BytesIO(json.dumps({'code':4020,'msg':'操作频繁'}).encode())
        with patch('pintuanya.urllib.request.urlopen',return_value=response):
            with self.assertRaises(ValueError):pintuanya.Client('cookie=x').request('POST','/groupon/goods',body={})


class UploadTest(unittest.TestCase):
    def test_deduplicated_upload_never_requests_token(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.png';p.write_bytes(b'\x89PNG\r\n\x1a\nabc')
            client=Mock();client.request.return_value={'data':{'url':'https://img.pintuanya.com/a.png'}}
            self.assertEqual(catalog.upload_image(client,p),'https://img.pintuanya.com/a.png')
            self.assertEqual(client.request.call_count,1)

    def test_upload_does_not_send_session_cookie_to_storage(self):
        import tempfile,json,io
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.png';p.write_bytes(b'\x89PNG\r\n\x1a\nabc')
            client=Mock();client.request.side_effect=[{'data':{}},{'data':{'token':'scoped-token','key':'groupon-goods/a.png'}},{'data':{'status':2}}]
            with patch('pintuanya.urllib.request.urlopen',return_value=io.BytesIO(json.dumps({'key':'groupon-goods/a.png'}).encode())) as send:
                self.assertEqual(catalog.upload_image(client,p),'https://img.pintuanya.com/groupon-goods/a.png')
            request=send.call_args.args[0]
            self.assertFalse(request.has_header('Cookie'))
            self.assertIn(b'scoped-token',request.data)

class SortingTest(unittest.TestCase):
    def test_absolute_sort_accepts_only_order_change_from_previous_move(self):
        from copy import deepcopy
        before={'goods_uuid':'id','category_uuid':'new','title':'red','is_offline':0,'sorting':3}
        current=dict(before,sorting=4)
        client=Mock();client.category_goods.side_effect=[[{'goods_uuid':'other'},current],[current,{'goods_uuid':'other'}]]
        state={}
        catalog.execute(client,'g',{'action':'sort','goods_uuid':'id','before':before,'direction':'TOP'},state,Mock())
        self.assertEqual(state['status'],'done')
        self.assertEqual(client.request.call_args.kwargs['body']['direction'],3)

    def test_sort_refuses_changed_product(self):
        before={'goods_uuid':'id','category_uuid':'new','title':'red','is_offline':0,'sorting':3}
        client=Mock();client.category_goods.return_value=[dict(before,title='edited')]
        with self.assertRaises(ValueError):
            catalog.execute(client,'g',{'action':'sort','goods_uuid':'id','before':before,'direction':'TOP'},{},Mock())
        client.request.assert_not_called()

    def test_finished_sort_does_not_move_again_after_later_top(self):
        before={'goods_uuid':'id','category_uuid':'new','title':'red','is_offline':0,'sorting':3}
        client=Mock();client.category_goods.return_value=[{'goods_uuid':'other'},dict(before,sorting=4)]
        catalog.execute(client,'g',{'action':'sort','goods_uuid':'id','before':before,'direction':'TOP'},{'status':'done'},Mock())
        client.request.assert_not_called()

class UploadCallbackTest(unittest.TestCase):
    def test_null_storage_callback_still_requires_successful_audit(self):
        import tempfile,io
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.png';p.write_bytes(b'\x89PNG\r\n\x1a\nabc')
            c=Mock();c.request.side_effect=[{'data':{}},{'data':{'token':'scoped-token','key':'groupon-goods/a.png'}},{'data':{'status':2}}]
            with patch('pintuanya.urllib.request.urlopen',return_value=io.BytesIO(b'null')):
                self.assertEqual(catalog.upload_image(c,p),'https://img.pintuanya.com/groupon-goods/a.png')

class CategoryPaginationTest(unittest.TestCase):
    def test_category_scan_handles_server_page_cap(self):
        from unittest.mock import patch
        c=pintuanya.Client('cookie=x')
        c.request=Mock(side_effect=[{'data':{'total':3,'list':[{'goods_uuid':'1'},{'goods_uuid':'2'}]}}, {'data':{'total':3,'list':[{'goods_uuid':'3'}]}}, {'data':{'total':0,'list':[]}}])
        with patch('pintuanya.time.sleep'):
            self.assertEqual([g['goods_uuid'] for g in c.category_goods('g','cat')],['1','2','3'])
        self.assertEqual([call.kwargs['params']['page'] for call in c.request.call_args_list],[1,2,1])

    def test_truncated_scan_is_not_used_for_duplicate_check(self):
        from unittest.mock import patch
        c=pintuanya.Client('cookie=x')
        c.request=Mock(side_effect=[{'data':{'total':2,'list':[{'goods_uuid':'1'}]}}, {'data':{'total':2,'list':[]}}])
        with patch('pintuanya.time.sleep'):
            with self.assertRaises(ValueError):c.category_goods('g','cat')

if __name__=='__main__':
    unittest.main()

class VariantTest(unittest.TestCase):
    def test_create_two_variants_in_one_goods(self):
        body=catalog.create_body('g',{'title':'Day and Night','category_uuid':'new',
          'images':['https://example.com/red.png','https://example.com/green.png'],
          'variants':[{'name':'Apple Red','price':'315','images':['https://example.com/red.png']},
                      {'name':'Forest Green','price':'315','images':['https://example.com/green.png']}]})
        self.assertEqual(len(body['sku_list']),2)
        self.assertEqual(body['sku_attr_list'],[{'attr_name':'默认规格','attr_value_list':['Apple Red','Forest Green']}])
        self.assertEqual([s['price'] for s in body['sku_list']],[31500,31500])
        item=dict(body,is_offline=0)
        self.assertTrue(catalog.matches(item,body))
        item['sku_list']=[dict(s) for s in body['sku_list']]
        item['sku_list'][1]['price']=31000
        self.assertFalse(catalog.matches(item,body))

    def test_duplicate_variant_names_are_rejected(self):
        with self.assertRaises(ValueError):
            catalog.create_body('g',{'title':'a','category_uuid':'new','images':['https://example.com/a.png'],
             'variants':[{'name':'red','price':'315'},{'name':'red','price':'315'}]})
