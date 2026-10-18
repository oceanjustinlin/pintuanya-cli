import copy
import csv
import importlib.util
import io
import json
from contextlib import redirect_stdout
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

spec = importlib.util.spec_from_file_location("pintuanya", Path(__file__).with_name("pintuanya.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

GROUP = "group-1"


def goods():
    return {"goods_uuid": "goods-1", "groupon_uuid": GROUP, "title": "测试黑胶",
            "category_uuid": "category-1", "cover": "cover.jpg", "image_list": ["cover.jpg"],
            "unit": "张", "is_offline": 1, "is_must_buy": 0, "is_free_shipping": 0,
            "sku_list": [{"sku_uuid": "sku-1", "price": 12000, "cost_price": 9000,
                          "stock": 3, "sku_number": "A", "is_freeze": 0}]}


class FakeClient:
    def __init__(self, item):
        self.items = {item["goods_uuid"]: copy.deepcopy(item)}
        self.calls = []

    def inventory(self, group, query=""):
        return copy.deepcopy({uid: item for uid, item in self.items.items() if query in item["title"]})

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        item = self.items.get(body.get("goods_uuid"))
        if path.endswith("/batch/price"):
            for sku in self.items["goods-1"]["sku_list"]:
                if sku["sku_uuid"] == body["sku_list"][0]["sku_uuid"]:
                    sku["price"] = body["sku_list"][0]["price"]
        elif path.endswith("/offline"):
            item["is_offline"] = body["is_offline"]
        elif path.endswith("/delete"):
            del self.items[body["goods_uuid"]]
        else:
            item = self.items[body["goods_uuid"]]
            item["sku_list"] = body["sku_list"]


def plan(operations):
    result = {"groupon_uuid": GROUP, "operations": operations}
    result["sha256"] = app.digest(result)
    return result


class CliTest(unittest.TestCase):
    def test_targeted_verification_request_count_is_independent_of_inventory_size(self):
        class PagedClient(app.Client):
            def __init__(self, count):
                self.items, self.reads, self.calls = {}, [], []
                for index in range(count):
                    item = goods()
                    item.update(goods_uuid=f"goods-{index}", title=f"黑胶 {index:04d}")
                    self.items[item["goods_uuid"]] = item
            def request(self, method, path, *, params=None, body=None):
                if method == "GET":
                    self.reads.append((len(self.calls), params["query"]))
                    items = [item for item in self.items.values()
                             if item["is_offline"] == params["is_offline"] and params["query"] in item["title"]]
                    start = (params["page"] - 1) * params["size"]
                    return {"data": {"list": copy.deepcopy(items[start:start + params["size"]]), "total": len(items)}}
                return FakeClient.request(self, method, path, body=body)
        counts = []
        for size in (3, 350):
            client = PagedClient(size)
            document = plan(app.prepare([{"action": "lock", "goods_uuid": "goods-0"},
                                         {"action": "lock", "goods_uuid": "goods-1"}], client.items))
            app.apply(client, document, yes=True)
            counts.append(len([read for read in client.reads if read[0] > 0]))
            self.assertEqual(len(client.calls), 2)
            self.assertTrue(all(read[1] for read in client.reads if read[0] > 0))
        self.assertEqual(counts, [4, 4])

    def test_delete_falls_back_to_full_read_when_title_query_misses(self):
        class MissingTitleClient(FakeClient):
            def inventory(self, group, query=""):
                queries.append(query)
                return {} if query else super().inventory(group)
        client, queries = MissingTitleClient(goods()), []
        document = plan(app.prepare([{"action": "delete", "goods_uuid": "goods-1"}], client.items))
        with patch("builtins.input", return_value="DELETE"):
            app.apply(client, document, yes=True)
        self.assertEqual(queries, ["", "测试黑胶", ""])

    def test_delete_fallback_does_not_mistake_query_miss_for_success(self):
        class NoDeleteClient(FakeClient):
            def inventory(self, group, query=""):
                return {} if query else super().inventory(group)
            def request(self, method, path, *, body=None):
                self.calls.append((method, path, body))
        client = NoDeleteClient(goods())
        document = plan(app.prepare([{"action": "delete", "goods_uuid": "goods-1"}], client.items))
        with patch("builtins.input", return_value="DELETE"), self.assertRaisesRegex(ValueError, "回查不一致"):
            app.apply(client, document, yes=True)

    def test_check_success_uses_one_small_request(self):
        class CheckClient(app.Client):
            def request(self, method, path, *, params=None, body=None):
                self_test.assertEqual((method, path, params),
                                      ("GET", "/groupon/batch", {"page": 1, "size": 1}))
                return {"success": True, "data": {"list": [], "total": 7}}
        self_test = self
        output = io.StringIO()
        with patch.object(app, "Client", CheckClient), patch.dict(app.os.environ, {"PINTUANYA_COOKIE": "test"}), redirect_stdout(output):
            self.assertEqual(app.cli(["check"]), 0)
        self.assertIn("登录有效", output.getvalue())
        self.assertIn("7", output.getvalue())

    def test_http_auth_errors_have_exit_code_two_for_all_commands(self):
        for command in (["check"], ["groups"]):
            for code in (401, 403):
                with self.subTest(command=command, code=code):
                    error = app.urllib.error.HTTPError("https://example.invalid", code, "denied", {}, None)
                    with patch.object(app.urllib.request, "urlopen", side_effect=error), patch.dict(app.os.environ, {"PINTUANYA_COOKIE": "test"}), patch("sys.stderr", io.StringIO()):
                        self.assertEqual(app.cli(command), 2)

    def test_business_login_failures_have_exit_code_two(self):
        for payload in ({"success": False, "message": "请先登录"},
                        {"success": False, "code": 401},
                        {"success": False, "message": "登录已过期"},
                        {"success": False, "message": "Unauthorized"}):
            with self.subTest(payload=payload):
                with patch.object(app.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())), patch.dict(app.os.environ, {"PINTUANYA_COOKIE": "test"}), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(app.cli(["check"]), 2)

    def test_parameter_and_ordinary_api_errors_have_exit_code_one(self):
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(app.cli(["unknown"]), 1)
            payload = {"success": False, "message": "商品不存在"}
            with patch.object(app.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())), patch.dict(app.os.environ, {"PINTUANYA_COOKIE": "test"}):
                self.assertEqual(app.cli(["check"]), 1)

    def test_apply_preserves_auth_error_type(self):
        client = FakeClient(goods())
        document = plan(app.prepare([{"action": "publish", "goods_uuid": "goods-1"}], client.inventory(GROUP)))
        with patch.object(client, "request", side_effect=app.LoginError("登录失效")):
            with self.assertRaises(app.LoginError):
                app.apply(client, document, yes=True)

    def run_read_command(self, arguments, items=None):
        items = [goods()] if items is None else items
        class SelectionClient:
            def __init__(self, cookie):
                pass
            def groups(self):
                return [{"groupon_uuid": GROUP, "title": "黑胶"}]
            def inventory(self, group, query=""):
                self_test.assertEqual(group, GROUP)
                queries.append(query)
                return {item["goods_uuid"]: copy.deepcopy(item) for item in items
                        if query in item["title"]}
        self_test, queries = self, []
        output = io.StringIO()
        with patch.object(app, "Client", SelectionClient), redirect_stdout(output):
            app.main(["--group", GROUP] + arguments)
        return output.getvalue(), queries

    def test_list_filters_status_without_changing_raw_json(self):
        offline = goods()
        online = copy.deepcopy(offline)
        online.update(goods_uuid="online", is_offline=0)
        frozen = copy.deepcopy(online)
        frozen["goods_uuid"] = "frozen"
        frozen["sku_list"].append(dict(frozen["sku_list"][0], sku_uuid="sku-2", is_freeze=1))
        items = [offline, online, frozen]
        for status, expected in (("all", items), ("online", [online, frozen]),
                                 ("offline", [offline]), ("frozen", [frozen])):
            with self.subTest(status=status):
                output, _ = self.run_read_command(["list", "--status", status, "--format", "json"], items)
                self.assertEqual(json.loads(output), expected)

    def test_list_defaults_to_readable_table_and_all_statuses(self):
        output, queries = self.run_read_command(["list"])
        self.assertIn("goods_uuid", output)
        self.assertIn("goods-1", output)
        self.assertIn("测试黑胶", output)
        self.assertIn("下架", output)
        self.assertEqual(queries, [""])

    def test_list_query_and_empty_json(self):
        output, queries = self.run_read_command(["list", "--query", "不存在", "--format", "json"])
        self.assertEqual(json.loads(output), [])
        self.assertEqual(queries, ["不存在"])

    def test_export_and_list_csv_share_columns_and_query(self):
        item = goods()
        item["title"] = '测试,黑胶\n"限定"'
        item["sku_list"].append({"sku_uuid": "sku-2", "price": 100, "is_freeze": 1})
        item["sku_list"].append({"sku_uuid": "sku-3", "price": 100, "is_freeze": 0,
                                 "stock": 0, "cost_price": 0, "sku_number": ""})
        expected_header = ["goods_uuid", "title", "is_offline", "sku_uuid", "sku_title", "price",
                           "is_freeze", "category_uuid", "stock", "cost_price", "sku_number"]
        listing, _ = self.run_read_command(["list", "--format", "csv"], [item])
        rows = list(csv.reader(io.StringIO(listing)))
        self.assertEqual(rows[0], expected_header)
        self.assertEqual(rows[1][1], item["title"])
        self.assertEqual(rows[1][7:], ["category-1", "3", "90.00", "A"])
        self.assertEqual(rows[2][7:], ["category-1", "", "", ""])
        self.assertEqual(rows[3][7:], ["category-1", "0", "0.00", ""])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            _, queries = self.run_read_command(["export", str(path), "--query", "测试"], [item])
            self.assertEqual(queries, ["测试"])
            with path.open(newline="", encoding="utf-8-sig") as handle:
                self.assertEqual(list(csv.reader(handle)), rows)

    def test_list_csv_empty_inventory_has_header(self):
        output, _ = self.run_read_command(["list", "--format", "csv"], [])
        self.assertEqual(len(list(csv.reader(io.StringIO(output)))), 1)

    def test_list_requires_group_before_cookie(self):
        with patch.dict(app.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "选择拼团"):
                app.main(["list"])

    def test_get_outputs_original_json_with_chinese_and_indentation(self):
        output, queries = self.run_read_command(["get", "goods-1"])
        self.assertEqual(output, json.dumps(goods(), ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(queries, [""])

    def test_get_reports_missing_exact_uuid(self):
        for uid in ("missing", "goods", "goods-10"):
            with self.subTest(uid=uid):
                with self.assertRaisesRegex(ValueError, "找不到商品"):
                    self.run_read_command(["get", uid])

    def test_get_query_limits_inventory_before_uuid_lookup(self):
        output, queries = self.run_read_command(["get", "goods-1", "--query", "黑胶"])
        self.assertEqual(json.loads(output), goods())
        self.assertEqual(queries, ["黑胶"])
        with self.assertRaisesRegex(ValueError, "找不到商品"):
            self.run_read_command(["get", "goods-1", "--query", "不存在"])

    def test_get_requires_group_before_cookie(self):
        with patch.dict(app.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "选择拼团"):
                app.main(["get", "goods-1"])

    def test_inventory_sends_title_query_to_backend(self):
        class ListingClient(app.Client):
            def __init__(self):
                self.queries = []
            def request(self, method, path, *, params=None, body=None):
                self.queries.append(params["query"])
                return {"data": {"list": [], "total": 0}}
        client = ListingClient()
        client.inventory(GROUP, query="Example Album Deluxe Edition")
        self.assertEqual(client.queries, ["Example Album Deluxe Edition"] * 2)

    def test_plan_saves_query_for_targeted_apply(self):
        class SelectionClient:
            queries = []
            def __init__(self, cookie):
                pass
            def groups(self):
                return [{"groupon_uuid": GROUP, "title": "黑胶"}]
            def inventory(self, group, query=""):
                self.queries.append(query)
                return {"goods-1": goods()}
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "delete.csv"
            plan_path = Path(directory) / "plan.json"
            csv_path.write_text("action,goods_uuid\ndelete,goods-1\n")
            with patch.object(app, "Client", SelectionClient), patch.dict(app.os.environ, {"PINTUANYA_COOKIE": "test"}):
                app.main(["--group", GROUP, "plan", "--query", "测试黑胶", str(csv_path), str(plan_path)])
            self.assertEqual(SelectionClient.queries, ["测试黑胶"])
            self.assertEqual(__import__("json").loads(plan_path.read_text())["query"], "测试黑胶")

    def test_apply_uses_saved_query_for_every_inventory_read(self):
        class TargetedClient(FakeClient):
            def __init__(self, item):
                super().__init__(item)
                self.queries = []
            def inventory(self, group, query=""):
                self.queries.append(query)
                return super().inventory(group)
        client = TargetedClient(goods())
        document = plan(app.prepare([{"action": "delete", "goods_uuid": "goods-1"}], client.inventory(GROUP)))
        document["query"] = "测试黑胶"
        document["sha256"] = app.digest({k: v for k, v in document.items() if k != "sha256"})
        with patch("builtins.input", return_value="DELETE"):
            app.apply(client, document, yes=True)
        self.assertEqual(client.queries[1:], ["测试黑胶", "测试黑胶"])

    def test_client_lists_groups_from_backend(self):
        class ListingClient(app.Client):
            def request(self, method, path, *, params=None, body=None):
                self.called = (method, path, params)
                return {"data": {"list": [{"groupon_uuid": "a", "title": "黑胶"}], "total": 1}}
        client = ListingClient("cookie=test")
        self.assertTrue(hasattr(client, "groups"), "group listing API missing")
        self.assertEqual(client.groups()[0]["groupon_uuid"], "a")
        self.assertEqual(client.called[1], "/groupon/batch")

    def test_export_refuses_implicit_group(self):
        class SelectionClient:
            def __init__(self, cookie):
                pass
            def groups(self):
                return [{"groupon_uuid": "a", "title": "黑胶"}]
            def inventory(self, group):
                return {"goods-1": goods()}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "Client", SelectionClient), patch.dict(app.os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "选择拼团"):
                    app.main(["export", str(Path(directory) / "out.csv")])

    def test_missing_group_reported_before_missing_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(app.os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "选择拼团"):
                    app.main(["export", str(Path(directory) / "out.csv")])

    def test_group_selection_requires_explicit_value(self):
        self.assertTrue(hasattr(app, "resolve_group"), "group selection API missing")
        with self.assertRaisesRegex(ValueError, "选择拼团"):
            app.resolve_group(None, "")

    def test_group_name_must_resolve_uniquely(self):
        self.assertTrue(hasattr(app, "resolve_group"), "group selection API missing")
        class Groups:
            def groups(self):
                return [{"groupon_uuid": "a", "title": "黑胶"},
                        {"groupon_uuid": "b", "title": "黑胶"}]
        with self.assertRaisesRegex(ValueError, "重名"):
            app.resolve_group(Groups(), "黑胶")

    def test_group_uuid_resolves_from_list(self):
        self.assertTrue(hasattr(app, "resolve_group"), "group selection API missing")
        class Groups:
            def groups(self):
                return [{"groupon_uuid": "a", "title": "黑胶"}]
        self.assertEqual(app.resolve_group(Groups(), "a"), "a")

    def test_price_uses_integer_cents_and_verifies(self):
        client = FakeClient(goods())
        ops = app.prepare([{"action": "price", "goods_uuid": "goods-1",
                            "sku_uuid": "sku-1", "price": "125.50"}], client.inventory(GROUP))
        self.assertEqual(ops[0]["after"], 12550)
        app.apply(client, plan(ops), yes=True)
        self.assertEqual(client.calls[0][2]["sku_list"], [{"sku_uuid": "sku-1", "price": 12550}])

    def test_changed_state_prevents_all_writes(self):
        client = FakeClient(goods())
        ops = app.prepare([{"action": "publish", "goods_uuid": "goods-1"}], client.inventory(GROUP))
        client.items["goods-1"]["is_offline"] = 0
        with self.assertRaisesRegex(ValueError, "后台状态已变化"):
            app.apply(client, plan(ops), yes=True)
        self.assertEqual(client.calls, [])

    def test_lock_preserves_other_fields(self):
        item = goods()
        payload = app.lock_payload(item, 1, GROUP)
        self.assertEqual(payload["sku_list"][0]["is_freeze"], 1)
        self.assertEqual(payload["sku_list"][0]["price"], 12000)
        self.assertEqual(item["sku_list"][0]["is_freeze"], 0)

    def test_bad_price_rejected(self):
        for price in ("0", "12.345", "NaN", "-3"):
            with self.assertRaises(ValueError):
                app.cents(price)


if __name__ == "__main__":
    unittest.main()
