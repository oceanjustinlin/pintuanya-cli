import csv
import io
import os
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import orders
import pintuanya


def make_order(uid, status=2, items=1, ctime="2026-07-01T10:00:00+08:00", **extra):
    item_list = [{"item_uuid": f"{uid}-{i}", "goods_uuid": f"g{i}", "goods_title": f"商品{i}",
                  "sku_uuid": f"s{i}", "sku_title": "", "quantity": 1, "price": 31500,
                  "discount": 0, "paid_amount": 31500, "shipped_quantity": 0,
                  "refunded_quantity": 0, "refunded_amount": 0} for i in range(items)]
    order = {"order_uuid": uid, "sequence": 1, "create_time": ctime, "status": status,
             "payment_method": 1, "transaction_status": 0, "refund_request_status": 0,
             "user": {"nickname": "买家", "remark": ""},
             "user_address": {"name": "张三丰", "tel": "13812345678", "province": "四川省",
                              "city": "成都市", "area": "武侯区", "address": "某路1号"},
             "number_of_product_types": items, "order_item_count": items,
             "order_item_list": item_list or None, "amount": 31500 * items, "shipping_amount": 0,
             "full_reduction": 0, "coupon_amount": 0, "membership_amount": 0, "refund_amount": 0,
             "transaction_amount": 0, "comments": "", "deleted_status": 0}
    order.update(extra)
    return order


class FakeClient:
    """按 order_status 和 page 返回订单；可模拟总数变化、商品截断。"""

    def __init__(self, by_status, items=None, totals=None):
        self.by_status, self.items, self.totals = by_status, items or {}, totals or {}
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/order/batch":
            data = self.by_status.get(body["order_status"], [])
            size, page = body["size"], body["page"]
            total = self.totals.get((body["order_status"], len(self.calls)), len(data))
            return {"code": 0, "data": {"list": data[(page - 1) * size: page * size], "total": total}}
        if path == "/order/item/batch":
            full = self.items[params["order_uuid"]]
            size, page = params["size"], params["page"]
            chunk = full[(page - 1) * size: page * size]
            return {"code": 0, "data": {"list": chunk, "next": page * size < len(full)}}
        raise AssertionError(path)


class RangeTest(unittest.TestCase):
    def test_default_is_180_days_ending_today(self):
        self.assertEqual(orders.parse_range(None, None, now=date(2026, 11, 3)),
                         (date(2026, 5, 8), date(2026, 11, 3)))

    def test_rejects_times_and_reversed_ranges(self):
        with self.assertRaises(ValueError):
            orders.parse_range("2026-07-01 10:00", None)
        with self.assertRaises(ValueError):
            orders.parse_range("2026-07-02", "2026-07-01")

    def test_body_uses_whole_days_and_ascending_sort(self):
        body = orders.query_body("g", 0, date(2026, 7, 1), date(2026, 7, 31), "", 1)
        self.assertEqual((body["start_time"], body["end_time"]), ("2026-07-01 00:00:00", "2026-07-31 23:59:59"))
        self.assertEqual((body["sort_order"], body["size"]), (0, 30))


class FetchTest(unittest.TestCase):
    def fetch(self, client, statuses=(0,)):
        return orders.fetch_orders(client, "g", list(statuses), date(2026, 1, 1), date(2026, 12, 31), pause=0)

    def test_paginates_and_rechecks_total(self):
        data = [make_order(f"o{i}") for i in range(65)]
        client = FakeClient({0: data})
        self.assertEqual(len(self.fetch(client)), 65)
        pages = [c[3]["page"] for c in client.calls if c[1] == "/order/batch"]
        self.assertEqual(pages, [1, 2, 3, 1])

    def test_any_merges_canceled(self):
        client = FakeClient({0: [make_order("a")], 100: [make_order("b", status=100)]})
        self.assertEqual({o["order_uuid"] for o in self.fetch(client, (0, 100))}, {"a", "b"})

    def test_total_change_fails(self):
        data = [make_order(f"o{i}") for i in range(31)]
        client = FakeClient({0: data}, totals={(0, 2): 32})
        with self.assertRaises(ValueError):
            self.fetch(client)

    def test_final_recheck_detects_change(self):
        data = [make_order("a")]
        client = FakeClient({0: data}, totals={(0, 2): 2})
        with self.assertRaisesRegex(ValueError, "总数发生变化"):
            self.fetch(client)

    def test_duplicate_and_missing_id_fail(self):
        with self.assertRaises(ValueError):
            self.fetch(FakeClient({0: [make_order("a"), make_order("a")]}))
        with self.assertRaises(ValueError):
            self.fetch(FakeClient({0: [make_order("")]}))

    def test_early_empty_page_fails(self):
        client = FakeClient({0: [make_order("a")]}, totals={(0, 1): 5})
        with self.assertRaises(ValueError):
            self.fetch(client)

    def test_truncated_items_are_completed(self):
        order = make_order("big", items=12)
        full = order["order_item_list"]
        order["order_item_list"] = full[:10]
        client = FakeClient({0: [order]}, items={"big": full})
        result = self.fetch(client)
        self.assertEqual(len(result[0]["order_item_list"]), 12)

    def test_incomplete_item_detail_fails(self):
        order = make_order("big", items=12)
        order["order_item_list"] = order["order_item_list"][:10]
        client = FakeClient({0: [order]}, items={"big": order["order_item_list"][:11]})
        with self.assertRaises(ValueError):
            self.fetch(client)

    def test_format_change_fails(self):
        class Broken:
            def request(self, *a, **k):
                return {"code": 0, "data": {"list": None, "total": "3"}}
        with self.assertRaises(ValueError):
            self.fetch(Broken())


class OutputTest(unittest.TestCase):
    def test_mask_rules(self):
        self.assertEqual(orders.mask_tel("13812345678"), "138****5678")
        self.assertEqual(orders.mask_name("张三丰"), "张**")
        self.assertEqual(orders.mask_text("电话13812345678请联系"), "电话138****5678请联系")
        masked = orders.mask_order(make_order("a"))
        self.assertEqual(masked["user_address"]["address"], "")
        self.assertEqual(masked["user_address"]["tel"], "138****5678")

    def test_mask_does_not_modify_original(self):
        order = make_order("a")
        orders.mask_order(order)
        self.assertEqual(order["user_address"]["tel"], "13812345678")

    def test_order_level_csv_masks_by_default(self):
        out = io.StringIO()
        orders.write_csv([make_order("a", comments="13812345678")], out, "order", "mask")
        row = list(csv.DictReader(io.StringIO(out.getvalue())))[0]
        self.assertEqual(row["receiver_tel"], "138****5678")
        self.assertEqual(row["receiver_address"], "四川省 成都市")
        self.assertEqual(row["comments"], "138****5678")
        self.assertEqual(row["amount"], "315.00")
        self.assertEqual(row["status_text"], "已付款")

    def test_full_pii_keeps_address(self):
        out = io.StringIO()
        orders.write_csv([make_order("a")], out, "order", "full")
        row = list(csv.DictReader(io.StringIO(out.getvalue())))[0]
        self.assertEqual(row["receiver_address"], "四川省 成都市 武侯区 某路1号")
        self.assertEqual(row["receiver_tel"], "13812345678")

    def test_item_level_rows_and_empty_orders(self):
        out = io.StringIO()
        count = orders.write_csv([make_order("a", items=2), make_order("b", status=100, items=0)],
                                 out, "item", "mask")
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertEqual(count, 3)
        self.assertEqual([r["item_index"] for r in rows], ["1", "2", ""])
        self.assertEqual(rows[0]["paid_amount"], "315.00")
        self.assertEqual(rows[2]["status_text"], "已取消")

    def test_unknown_status_and_zero_values(self):
        fields = orders.order_fields(make_order("a", status=7, shipping_amount=0), "mask")
        self.assertEqual(fields["status_text"], "未知(7)")
        self.assertEqual(fields["shipping_amount"], "0.00")

    def test_summary_excludes_canceled_amount(self):
        text = orders.summary([make_order("a"), make_order("b", status=100)])
        self.assertIn("订单 2 单", text)
        self.assertIn("¥315.00", text)


class ExportFileTest(unittest.TestCase):
    def test_writes_bom_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "o.csv"
            orders.export([make_order("a")], path, "order", "mask")
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(os.listdir(d), ["o.csv"])

    def test_refuses_existing_unless_forced(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "o.csv"
            path.write_text("old")
            with self.assertRaises(ValueError):
                orders.export([make_order("a")], path, "order", "mask")
            self.assertEqual(path.read_text(), "old")
            orders.export([make_order("a")], path, "order", "mask", force=True)
            self.assertIn("order_uuid", path.read_text(encoding="utf-8-sig"))

    def test_failure_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "o.csv"
            with patch("orders.write_csv", side_effect=ValueError("boom")):
                with self.assertRaises(ValueError):
                    orders.export([make_order("a")], path, "order", "mask")
            self.assertEqual(os.listdir(d), [])


class CommandTest(unittest.TestCase):
    def test_orders_requires_group(self):
        with patch.dict(os.environ, {"PINTUANYA_COOKIE": "a=b"}, clear=False):
            os.environ.pop("PINTUANYA_GROUP", None)
            with self.assertRaisesRegex(ValueError, "选择拼团"):
                pintuanya.main(["--no-env-file", "orders"])

    def test_export_checks_target_before_querying(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "o.csv"
            path.write_text("old")
            client = FakeClient({0: [make_order("a")]})

            class Args:
                command, output, force, date_from, date_to = "orders-export", path, False, None, None
            with self.assertRaises(ValueError):
                orders.run(client, "g", Args)
            self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
