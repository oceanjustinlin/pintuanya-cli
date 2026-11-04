import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import order_ops
from pintuanya import digest
from test_orders import make_order


class Backend:
    """有状态的假后台：订单按日期和单号可搜索，标记可增删，可模拟写入失败。"""

    def __init__(self, orders_list, tags=None):
        self.orders = {o["order_uuid"]: o for o in orders_list}
        self.tags = tags or {}
        self.writes = []
        self.next_id = 100
        self.fail_write = None   # 写入前抛错
        self.lose_reply = None   # 写入生效但响应丢失
        self.ignore_write = False

    def request(self, method, path, params=None, body=None):
        if path == "/order/batch":
            day = body["start_time"][:10]
            hits = [o for o in self.orders.values()
                    if o["create_time"][:10] == day and str(o["sequence"]) == body["query_value"]
                    and (o["status"] in (1, 2, 3) if body["order_status"] == 0 else o["status"] == body["order_status"])]
            return {"data": {"list": hits, "total": len(hits)}}
        if path == "/order/tag" and method == "GET":
            return {"data": list(self.tags.get(params["order_uuid"], []))}
        self.writes.append((method, path, body))
        if self.fail_write:
            raise ValueError(self.fail_write)
        if not self.ignore_write:
            uid = body["order_uuid"]
            if path == "/order/status":
                self.orders[uid]["status"] = body["order_status"]
            elif path == "/order/tag":
                self.next_id += 1
                self.tags.setdefault(uid, []).append({"tag_id": self.next_id, "name": body["tag"], "type": body["type"]})
            elif path == "/order/tag/delete":
                self.tags[uid] = [t for t in self.tags.get(uid, []) if str(t["tag_id"]) != body["tag_id"]]
        if self.lose_reply:
            message, self.lose_reply = self.lose_reply, None
            raise ValueError(message)
        return {"code": 0}


def rows(*items):
    return [dict({"line": i + 2, "tag_type": ""}, **r) for i, r in enumerate(items)]


def plan_for(backend, *items):
    ops = order_ops.prepare(backend, rows(*items), list(backend.orders.values()))
    plan = {"version": 1, "kind": "orders", "groupon_uuid": "g", "operations": ops}
    plan["sha256"] = digest(plan)
    return plan


def orders_fixture():
    return [make_order("u1", status=2, sequence=11), make_order("u2", status=1, sequence=12),
            make_order("u3", status=101, sequence=13), make_order("u4", status=2, sequence=14, refund_request_status=1)]


class PrepareTest(unittest.TestCase):
    def test_status_by_sequence_or_uuid(self):
        backend = Backend(orders_fixture())
        ops = order_ops.prepare(backend, rows({"action": "status", "order": "11", "value": "shipped"},
                                               {"action": "status", "order": "u2", "value": "paid"}),
                                list(backend.orders.values()))
        self.assertEqual([(o["order_uuid"], o["before"], o["after"]) for o in ops], [("u1", 2, 3), ("u2", 1, 2)])
        self.assertEqual(ops[0]["day"], "2026-07-01")

    def test_rejects_unsafe_or_noop_status(self):
        backend = Backend(orders_fixture())
        for row in ({"action": "status", "order": "11", "value": "paid"},       # 无变化
                    {"action": "status", "order": "11", "value": "after-sales"},  # 不支持的目标
                    {"action": "status", "order": "13", "value": "paid"},       # 售后中
                    {"action": "status", "order": "14", "value": "shipped"},    # 有退款申请
                    {"action": "status", "order": "99", "value": "paid"}):      # 找不到
            with self.assertRaises(ValueError, msg=row):
                order_ops.prepare(backend, rows(row), list(backend.orders.values()))

    def test_duplicate_rows_rejected(self):
        backend = Backend(orders_fixture())
        with self.assertRaises(ValueError):
            order_ops.prepare(backend, rows({"action": "status", "order": "11", "value": "shipped"},
                                            {"action": "status", "order": "u1", "value": "canceled"}),
                              list(backend.orders.values()))
        with self.assertRaises(ValueError):
            order_ops.prepare(backend, rows({"action": "tag", "order": "11", "value": "已对账"},
                                            {"action": "tag", "order": "11", "value": "已对账"}),
                              list(backend.orders.values()))

    def test_ambiguous_sequence_requires_uuid(self):
        backend = Backend([make_order("a", sequence=1), make_order("b", sequence=1)])
        with self.assertRaisesRegex(ValueError, "不唯一"):
            order_ops.prepare(backend, rows({"action": "tag", "order": "1", "value": "x"}), list(backend.orders.values()))

    def test_tag_rules(self):
        backend = Backend(orders_fixture(), tags={"u1": [{"tag_id": 7, "name": "已对账", "type": 1}]})
        with self.assertRaisesRegex(ValueError, "已有该标记"):
            order_ops.prepare(backend, rows({"action": "tag", "order": "11", "value": "已对账"}), list(backend.orders.values()))
        ops = order_ops.prepare(backend, rows({"action": "tag", "order": "11", "value": "已对账", "tag_type": "private"},
                                               {"action": "untag", "order": "11", "value": "已对账"}),
                                list(backend.orders.values()))
        self.assertEqual((ops[0]["type"], ops[1]["tag_id"]), (2, "7"))
        for row in ({"action": "untag", "order": "11", "value": "不存在"},
                    {"action": "tag", "order": "11", "value": "a\nb"},
                    {"action": "tag", "order": "11", "value": "x", "tag_type": "vip"}):
            with self.assertRaises(ValueError, msg=row):
                order_ops.prepare(backend, rows(row), list(backend.orders.values()))


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "plan.json"

    def tearDown(self):
        self.dir.cleanup()

    def run_apply(self, backend, plan, answers=("APPLY",), **kw):
        answers = list(answers)

        def ask(prompt):
            answer = answers.pop(0)
            return answer if answer != "APPLY" else prompt.split("输入 ")[1].split(" 执行")[0]
        with redirect_stdout(io.StringIO()) as out:
            order_ops.apply(backend, plan, self.path, ask=ask, **kw)
        return out.getvalue()

    def log(self):
        return [json.loads(l) for l in Path(str(self.path) + ".log.jsonl").read_text().splitlines()]

    def test_applies_status_and_tags_with_readback(self):
        backend = Backend(orders_fixture(), tags={"u2": [{"tag_id": 9, "name": "旧", "type": 2}]})
        plan = plan_for(backend, {"action": "status", "order": "11", "value": "shipped"},
                        {"action": "tag", "order": "11", "value": "已对账"},
                        {"action": "untag", "order": "12", "value": "旧"})
        self.run_apply(backend, plan)
        self.assertEqual(backend.orders["u1"]["status"], 3)
        self.assertEqual([t["name"] for t in backend.tags["u1"]], ["已对账"])
        self.assertEqual(backend.tags["u2"], [])
        self.assertEqual([r["status"] for r in self.log()], ["done"] * 3)
        self.assertEqual(backend.writes[0], ("PUT", "/order/status", {"order_uuid": "u1", "order_status": 3}))

    def test_wrong_confirmation_writes_nothing(self):
        backend = Backend(orders_fixture())
        plan = plan_for(backend, {"action": "tag", "order": "11", "value": "x"})
        with self.assertRaises(ValueError):
            self.run_apply(backend, plan, answers=("no",))
        self.assertEqual(backend.writes, [])

    def test_cancel_requires_extra_confirmation(self):
        backend = Backend(orders_fixture())
        plan = plan_for(backend, {"action": "status", "order": "11", "value": "canceled"})
        with self.assertRaises(ValueError):
            self.run_apply(backend, plan, answers=("x",), yes=True)
        self.assertEqual(backend.writes, [])
        self.run_apply(backend, plan, answers=("CANCEL",), yes=True)
        self.assertEqual(backend.orders["u1"]["status"], 100)

    def test_changed_state_stops_before_any_write(self):
        backend = Backend(orders_fixture())
        plan = plan_for(backend, {"action": "tag", "order": "11", "value": "x"},
                        {"action": "status", "order": "12", "value": "paid"})
        backend.orders["u2"]["status"] = 3
        with self.assertRaisesRegex(ValueError, "未执行任何操作"):
            self.run_apply(backend, plan)
        self.assertEqual(backend.writes, [])

    def test_readback_mismatch_is_logged_as_failed(self):
        backend = Backend(orders_fixture())
        backend.ignore_write = True
        plan = plan_for(backend, {"action": "status", "order": "11", "value": "shipped"})
        with self.assertRaisesRegex(ValueError, "回查不一致"):
            self.run_apply(backend, plan)
        self.assertEqual(self.log()[0]["status"], "failed")

    def test_plain_apply_refuses_existing_log(self):
        backend = Backend(orders_fixture())
        backend.fail_write = "timeout"
        plan = plan_for(backend, {"action": "tag", "order": "11", "value": "x"})
        with self.assertRaises(ValueError):
            self.run_apply(backend, plan)
        with self.assertRaisesRegex(ValueError, "--resume"):
            self.run_apply(backend, plan)

    def test_resume_reconciles_write_whose_reply_was_lost(self):
        backend = Backend(orders_fixture())
        plan = plan_for(backend, {"action": "tag", "order": "11", "value": "x"},
                        {"action": "status", "order": "11", "value": "shipped"})
        backend.lose_reply = "connection reset"
        with self.assertRaises(ValueError):
            self.run_apply(backend, plan)
        writes = len(backend.writes)
        out = self.run_apply(backend, plan, resume=True)
        self.assertIn("确认已生效", out)
        self.assertEqual(len(backend.writes), writes + 1)  # 只执行第 2 项，不重复添加标记
        self.assertEqual(len(backend.tags["u1"]), 1)
        self.assertEqual([r["status"] for r in self.log()], ["failed", "done", "done"])
        self.assertTrue(self.log()[1].get("reconciled"))

    def test_resume_checks_completed_items(self):
        backend = Backend(orders_fixture())
        plan = plan_for(backend, {"action": "tag", "order": "11", "value": "x"},
                        {"action": "status", "order": "12", "value": "paid"})
        log = Path(str(self.path) + ".log.jsonl")
        log.write_text(json.dumps({"plan_sha256": plan["sha256"], "index": 1, "action": "tag",
                                   "order_uuid": "u1", "status": "done"}) + "\n")
        with self.assertRaisesRegex(ValueError, "已完成项"):
            self.run_apply(backend, plan, resume=True)  # 日志称已完成，但后台没有该标记
        self.assertEqual(backend.writes, [])

    def test_tampered_plan_rejected(self):
        backend = Backend(orders_fixture())
        plan = plan_for(backend, {"action": "status", "order": "11", "value": "shipped"})
        plan["operations"][0]["after"] = 100
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.run_apply(backend, plan)


if __name__ == "__main__":
    unittest.main()
