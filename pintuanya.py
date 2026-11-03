#!/usr/bin/env python3
"""拼团呀商品批量操作 CLI。仅使用后台网页已使用的接口。"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE = "https://www.pintuanya.com/api/v1"
BACKUP_DIR = Path(__file__).resolve().parent / "backups"
ACTIONS = {"publish", "offline", "price", "lock", "unlock", "delete"}
CSV_COLUMNS = ["goods_uuid", "title", "is_offline", "sku_uuid", "sku_title", "price", "is_freeze",
               "category_uuid", "stock", "cost_price", "sku_number"]


def fail(message):
    raise ValueError(message)


class LoginError(ValueError):
    pass


def api_urlopen(request, timeout=30):
    from http_transport import NoRedirect
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        fail(f"参数错误: {message}")


def cents(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        fail(f"无效价格: {value}")
    if not number.is_finite() or number <= 0 or number.as_tuple().exponent < -2:
        fail(f"价格必须是大于零且最多两位小数: {value}")
    return int(number * 100)


def money(value):
    return f"{Decimal(int(value)) / 100:.2f}"


def digest(data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def resolve_group(client, selector):
    if not selector:
        fail("请先用 groups 查看并选择拼团；通过 --group 传入 UUID 或完整名称")
    matches = [group for group in client.groups()
               if selector in (str(group.get("groupon_uuid", "")), group.get("title", ""))]
    if len(matches) > 1:
        fail(f"拼团名称重名，请改用 UUID: {selector}")
    if not matches:
        fail(f"找不到拼团: {selector}")
    return str(matches[0]["groupon_uuid"])


class Client:
    def __init__(self, cookie):
        if not cookie:
            fail("未配置 PINTUANYA_COOKIE；请运行 auth-login，或通过 auth-save 导入；不要把 Cookie 写入计划或 Git")
        self.cookie = cookie
        self.device_id = os.getenv("PINTUANYA_DEVICE_ID") or str(uuid.uuid4())
        if any(c in self.cookie + self.device_id for c in '\r\n\x00'):
            fail('登录凭据包含非法换行或控制字符（内容已隐藏）')
        self.cookies = None
        self.persist_cookie = None
        self.timeout = 30

    def receive_cookies(self, url, headers):
        if headers is not None and hasattr(headers, 'get_all') and headers.get_all('Set-Cookie'):
            if self.cookies is None:
                from login import CookieState
                self.cookies = CookieState(self.cookie)
            self.cookies.receive(url, headers)
            self.cookie = self.cookies.header()

    def request(self, method, path, *, params=None, body=None):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        before_cookie = self.cookie
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Cookie": self.cookies.header(url) if self.cookies is not None else self.cookie, "Origin": "https://www.pintuanya.com",
            "Referer": "https://www.pintuanya.com/groupon/list",
            "User-Agent": "pintuanya-cli/0.1",
            "X-Device-Id": self.device_id, "X-Nonce": str(uuid.uuid4()),
            "X-Timestamp": str(int(time.time())),
        })
        try:
            transport = os.getenv("PINTUANYA_TRANSPORT", "urllib")
            if transport == "curl":
                from http_transport import api_request
                payload = api_request(url, method, dict(request.header_items()), body,
                                      os.getenv("PINTUANYA_RESOLVE_IP"),
                                      on_headers=lambda headers: self.receive_cookies(url, headers), timeout=self.timeout)
            elif transport == "urllib":
                with api_urlopen(request, timeout=self.timeout) as response:
                    self.receive_cookies(url, getattr(response, 'headers', None))
                    payload = json.load(response)
            else:
                fail("PINTUANYA_TRANSPORT 仅支持 urllib 或 curl")
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise LoginError(f"登录被拒绝（HTTP {error.code}），请用 auth-status 检查或 auth-login 重新登录") from None
            fail(f"HTTP {error.code} {path}；请检查登录状态或接口是否变化")
        except urllib.error.URLError as error:
            fail(f"网络请求失败: {error.reason}")
        if isinstance(payload, dict):
            message = str(payload.get("message", "")).lower()
            login_message = any(word in message for word in (
                "未登录", "未登陆", "请先登录", "请登录", "重新登录", "登录失效", "登录过期",
                "登录已过期", "登录已失效", "unauthorized", "not logged in", "login expired"))
            if (str(payload.get("code")) in ("401", "403")
                    or (payload.get("success") is not True and login_message)):
                raise LoginError("登录失效，请运行 auth-login；写入请按原计划回查后续跑")
        if (not isinstance(payload, dict) or payload.get("success") in (False, 0)
                or str(payload.get("code", 0)) not in ("0", "200", "None")):
            fail(f"接口返回失败 {path}（响应内容已隐藏）")
        if self.cookie != before_cookie and self.persist_cookie:
            try:
                self.persist_cookie(self.cookie)
            except (ValueError, OSError):
                print('警告: 服务器请求已成功，但新 Cookie 未保存；请检查配置是否被修改，并运行 auth-status。', file=sys.stderr)
        return payload

    def categories(self, group):
        data = self.request("GET", "/groupon/category/batch", params={"groupon_uuid": group}).get("data")
        if not isinstance(data, list):
            fail("分类列表格式已变化")
        return data

    def category_goods(self, group, category):
        """按分类分页，避开后台标题搜索漏掉新商品的问题。"""
        result = []
        for offline in (0, 1):
            collected = 0
            for page in range(1, 1001):
                data = self.request("GET", "/groupon/goods/batch", params={
                    "groupon_uuid": group, "category_uuid": category, "page": page,
                    "size": 1000, "is_offline": offline}).get("data") or {}
                batch = data.get("list")
                total = data.get("total")
                if not isinstance(batch, list) or type(total) is not int or total < 0:
                    fail("商品列表格式已变化")
                if not batch and collected < total:
                    fail("商品分页提前结束，停止操作")
                result.extend(batch)
                collected += len(batch)
                if collected >= total:
                    break
                time.sleep(1)
            else:
                fail("商品分页异常")
        return result

    def check(self):
        response = self.request("GET", "/groupon/batch", params={"page": 1, "size": 1})
        data = response.get("data") or {}
        total = data.get("total")
        if not isinstance(data.get("list"), list) or not isinstance(total, int) or total < 0:
            fail("拼团列表格式已变化，无法确认登录状态")
        return total

    def groups(self):
        groups = []
        page = 1
        while True:
            response = self.request("GET", "/groupon/batch", params={"page": page, "size": 100})
            data = response.get("data") or {}
            batch = data.get("list")
            if not isinstance(batch, list):
                fail("拼团列表格式已变化，停止操作")
            groups.extend(batch)
            total = data.get("total")
            if not batch or (total is not None and page * 100 >= int(total)) or len(batch) < 100:
                break
            page += 1
            if page > 1000:
                fail("拼团分页异常，停止操作")
        return groups

    def inventory(self, group, query=""):
        goods = {}
        for offline in (0, 1):
            page = 1
            while True:
                response = self.request("GET", "/groupon/goods/batch", params={
                    "category_uuid": "", "groupon_uuid": group, "query": query,
                    "page": page, "size": 100, "is_offline": offline,
                })
                data = response.get("data") or {}
                batch = data.get("list")
                if not isinstance(batch, list):
                    fail("商品列表格式已变化，停止操作")
                for item in batch:
                    uid = str(item.get("goods_uuid", ""))
                    if not uid:
                        fail("商品缺少 goods_uuid，停止操作")
                    if uid in goods:
                        fail(f"商品重复: {uid}")
                    goods[uid] = item
                total = data.get("total")
                if not batch or (total is not None and page * 100 >= int(total)) or len(batch) < 100:
                    break
                page += 1
                if page > 1000:
                    fail("商品分页异常，停止操作")
        return goods


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"action", "goods_uuid"}.issubset(reader.fieldnames):
            fail("CSV 至少需要 action,goods_uuid 两列")
        rows = []
        for line, raw in enumerate(reader, 2):
            row = {key: (value or "").strip() for key, value in raw.items() if key}
            if not any(row.values()):
                continue
            if row["action"] not in ACTIONS:
                fail(f"第 {line} 行操作无效: {row['action']}")
            if not row["goods_uuid"]:
                fail(f"第 {line} 行缺少 goods_uuid")
            rows.append(row)
    if not rows:
        fail("CSV 没有操作行")
    keys = [(r["action"], r["goods_uuid"], r.get("sku_uuid", "")) for r in rows]
    if len(keys) != len(set(keys)):
        fail("CSV 存在重复操作行")
    return rows


def sku_items(goods):
    skus = goods.get("sku_list")
    if not isinstance(skus, list) or not skus:
        fail(f"商品 {goods.get('goods_uuid')} 没有可验证的 SKU")
    return skus


def write_csv(items, handle):
    writer = csv.writer(handle)
    writer.writerow(CSV_COLUMNS)
    for goods in items:
        for sku in sku_items(goods):
            sku_info = sku.get("sku_info_list") or []
            sku_title = sku_info[0].get("value", "") if sku_info else ""
            cost = sku.get("cost_price")
            writer.writerow([goods["goods_uuid"], goods.get("title", ""), goods.get("is_offline", ""),
                             sku.get("sku_uuid", ""), sku_title, money(sku.get("price", 0)),
                             sku.get("is_freeze", ""), goods.get("category_uuid", ""),
                             sku.get("stock", ""), money(cost) if cost not in (None, "") else "",
                             sku.get("sku_number", "")])


def filter_goods(items, status):
    if status == "all":
        return list(items)
    if status == "frozen":
        return [item for item in items if any(int(sku.get("is_freeze", 0)) == 1
                                             for sku in sku_items(item))]
    offline = 0 if status == "online" else 1
    return [item for item in items if int(item.get("is_offline", -1)) == offline]


def show_table(items):
    rows = [["goods_uuid", "title", "状态", "冻结"]]
    for item in items:
        frozen = any(int(sku.get("is_freeze", 0)) == 1 for sku in sku_items(item))
        rows.append([str(item["goods_uuid"]), str(item.get("title", "")),
                     {0: "上架", 1: "下架"}.get(int(item.get("is_offline", -1)), "未知"),
                     "是" if frozen else "否"])
    rows = [[cell.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
             for cell in row] for row in rows]
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def prepare(rows, inventory):
    operations = []
    seen = set()
    goods_actions, priced_goods = set(), set()
    for row in rows:
        uid, action = row["goods_uuid"], row["action"]
        goods = inventory.get(uid)
        if not goods:
            fail(f"找不到商品 {uid}")
        if action == "price":
            if uid in goods_actions:
                fail(f"商品 {uid} 有冲突的操作；分批执行")
            sku_id = row.get("sku_uuid", "")
            if not sku_id:
                fail(f"改价必须填写 sku_uuid: {uid}")
            matches = [s for s in sku_items(goods) if str(s.get("sku_uuid")) == sku_id]
            if len(matches) != 1:
                fail(f"找不到唯一 SKU: {uid}/{sku_id}")
            target = cents(row.get("price", ""))
            old = int(matches[0]["price"])
            if target == old:
                fail(f"价格没有变化: {uid}/{sku_id}")
            key = (uid, sku_id)
            if key in seen:
                fail(f"同一 SKU 有重复改价: {uid}/{sku_id}")
            seen.add(key)
            priced_goods.add(uid)
            operations.append({"action": action, "goods_uuid": uid, "sku_uuid": sku_id,
                               "title": goods.get("title", ""), "before": old, "after": target})
        else:
            if uid in goods_actions or uid in priced_goods:
                fail(f"商品 {uid} 有冲突的操作；分批执行")
            goods_actions.add(uid)
            if action in ("publish", "offline"):
                before = int(goods.get("is_offline", -1))
                after = 0 if action == "publish" else 1
                if before not in (0, 1) or before == after:
                    fail(f"商品状态不适用 {action}: {uid}")
            elif action in ("lock", "unlock"):
                before = {str(s.get("sku_uuid")): int(s.get("is_freeze", -1)) for s in sku_items(goods)}
                if "" in before or any(v not in (0, 1) for v in before.values()):
                    fail(f"SKU 冻结字段异常: {uid}")
                after = 1 if action == "lock" else 0
                if all(v == after for v in before.values()):
                    fail(f"商品已处于目标锁定状态: {uid}")
            else:
                before, after = True, False
            operations.append({"action": action, "goods_uuid": uid, "title": goods.get("title", ""),
                               "before": before, "after": after})
    return operations


def snapshot(goods, action, sku_uuid=""):
    if action == "price":
        matches = [s for s in sku_items(goods) if str(s.get("sku_uuid")) == sku_uuid]
        return int(matches[0]["price"]) if len(matches) == 1 else None
    if action in ("publish", "offline"):
        return int(goods.get("is_offline", -1))
    if action in ("lock", "unlock"):
        return {str(s.get("sku_uuid")): int(s.get("is_freeze", -1)) for s in sku_items(goods)}
    return True


def lock_payload(goods, freeze, group):
    """与网页 transferGoods 的字段保持一致，避免覆盖无关设置。"""
    allowed = ("goods_uuid", "category_uuid", "cover", "image_list", "title", "unit",
               "is_must_buy", "is_free_shipping", "sku_attr_list", "tiered_pricing",
               "starting_purchase", "limit_purchase")
    payload = {key: deepcopy(goods[key]) for key in allowed if key in goods}
    payload["groupon_uuid"] = group
    skus = deepcopy(sku_items(goods))
    for sku in skus:
        sku["is_freeze"] = freeze
    payload["sku_list"] = skus
    if not payload.get("goods_uuid") or not payload.get("title"):
        fail("冻结所需商品字段不完整，停止操作")
    return payload


def show(operations):
    for index, op in enumerate(operations, 1):
        suffix = f" / {op['sku_uuid']}" if op["action"] == "price" else ""
        before = money(op["before"]) if op["action"] == "price" else op["before"]
        after = money(op["after"]) if op["action"] == "price" else op["after"]
        print(f"{index:>3}. {op['action']:<7} {op['goods_uuid']}{suffix}  {op['title']}  {before} → {after}")


def backup_deleted(plan, operations, current):
    ids = list(dict.fromkeys(op["goods_uuid"] for op in operations if op["action"] == "delete"))
    if not ids:
        return
    now = datetime.now(timezone.utc)
    document = {"version": 1, "groupon_uuid": plan["groupon_uuid"], "created_at": now.isoformat(),
                "plan_sha256": plan["sha256"], "goods": [current[uid] for uid in ids]}
    name = f"{plan['groupon_uuid']}-{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    path = BACKUP_DIR / name
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        fail("删除前备份写入失败，未执行任何操作；请检查 backups 目录权限和磁盘空间")
    print(f"删除前备份: {path}")


def read_execution_log(path, plan, resume):
    if not path.exists():
        if resume:
            fail("续跑日志不存在，无法确认已完成项")
        return set()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        fail("执行日志读取失败，停止操作")
    if raw and not raw.endswith("\n"):
        fail("执行日志末行不完整，停止操作；请先人工核对后台状态")
    lines = raw.splitlines()
    done = set()
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            fail("执行日志损坏，停止操作；请先人工核对后台状态")
        if not isinstance(record, dict) or record.get("plan_sha256") != plan["sha256"]:
            fail("执行日志与计划不一致，停止操作")
        index = record.get("index")
        if type(index) is not int or not 1 <= index <= len(plan["operations"]):
            fail("执行日志序号无效，停止操作")
        op = plan["operations"][index - 1]
        if (record.get("action") != op["action"] or record.get("goods_uuid") != op["goods_uuid"]
                or record.get("sku_uuid") != op.get("sku_uuid", "")
                or record.get("status") not in ("done", "failed")
                or not isinstance(record.get("error"), str)
                or not isinstance(record.get("at"), str) or not record["at"]):
            fail("执行日志记录与计划不一致或格式无效")
        if index != len(done) + 1:
            fail("执行日志顺序异常，停止操作")
        if record["status"] == "done":
            done.add(index)
    if lines and not resume:
        fail("日志已有执行记录，请使用 apply --resume 续跑")
    return done


def safe_error(error):
    message = str(error) or "用户中断"
    cookie = os.getenv("PINTUANYA_COOKIE")
    if cookie:
        message = message.replace(cookie, "[已隐藏]")
        for part in cookie.split(';'):
            key, sep, value = part.strip().partition('=')
            if sep and value:
                message = message.replace(value, '[已隐藏]')
    import re
    return re.sub(r'([?&](?:code|state|uuid|token)=)[^\s&\"\x27]+', r'\1[已隐藏]', message)


def append_execution_log(handle, plan, index, op, status, error="", *, reconciled=False):
    record = {"plan_sha256": plan["sha256"], "index": index, "action": op["action"],
              "goods_uuid": op["goods_uuid"], "sku_uuid": op.get("sku_uuid", ""),
              "status": status, "error": error, "at": datetime.now(timezone.utc).isoformat()}
    if reconciled:
        record["reconciled"] = True
    try:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    except OSError:
        fail("执行日志写入失败；停止后续操作，请人工核对本项后台状态")


def matches_after(item, op):
    if op["action"] == "delete":
        return item is None
    if item is None:
        return False
    actual = snapshot(item, op["action"], op.get("sku_uuid", ""))
    if op["action"] in ("lock", "unlock"):
        return actual == {uid: op["after"] for uid in op["before"]}
    return actual == op["after"]


def write_operation(client, group, op, current):
    uid, action = op["goods_uuid"], op["action"]
    if action == "price":
        client.request("PUT", "/groupon/goods/batch/price", body={"groupon_uuid": group,
                       "sku_list": [{"sku_uuid": op["sku_uuid"], "price": op["after"]}]})
    elif action in ("publish", "offline"):
        client.request("PUT", "/groupon/goods/offline", body={"groupon_uuid": group,
                       "goods_uuid": uid, "is_offline": op["after"]})
    elif action in ("lock", "unlock"):
        client.request("PUT", "/groupon/goods", body=lock_payload(current[uid], op["after"], group))
    else:
        client.request("PUT", "/groupon/goods/delete", body={"groupon_uuid": group, "goods_uuid": uid})


def execute_operations(client, plan, pending, current, handle, done):
    group, query = plan["groupon_uuid"], plan.get("query", "")
    total = len(plan["operations"])
    for index, op in pending:
        uid, action = op["goods_uuid"], op["action"]
        try:
            verify_query = query or op.get("title", "")
            if action == "delete" and not query and verify_query:
                matched = client.inventory(group, query=verify_query)
                if uid not in matched:
                    verify_query = ""
            write_operation(client, group, op, current)
            fresh = client.inventory(group, query=verify_query) if verify_query else client.inventory(group)
            item = fresh.get(uid)
            if not matches_after(item, op):
                fail(f"写入后回查不一致: {uid}")
            if item is None:
                current.pop(uid, None)
            else:
                current[uid] = item
        except (Exception, KeyboardInterrupt) as error:
            message = safe_error(error)
            try:
                append_execution_log(handle, plan, index, op, "failed", message)
            except ValueError as log_error:
                message += f"；{log_error}"
            error_type = LoginError if isinstance(error, LoginError) else ValueError
            raise error_type(f"已完成 {done}/{total} 项；在 {action} {uid} 停止: {message}") from None
        append_execution_log(handle, plan, index, op, "done")
        done += 1
        print(f"完成 {done}/{total}: {action} {uid}")


def apply(client, plan, yes=False, *, plan_path, resume=False):
    operations = plan["operations"]
    if (not isinstance(operations, list) or not operations
            or any(not isinstance(op, dict) or op.get("action") not in ACTIONS for op in operations)):
        fail("计划操作列表无效")
    if digest({k: v for k, v in plan.items() if k != "sha256"}) != plan.get("sha256"):
        fail("计划文件校验失败")
    log_path = Path(str(plan_path) + ".log.jsonl")
    done = read_execution_log(log_path, plan, resume)
    pending = [(index, op) for index, op in enumerate(operations, 1) if index not in done]
    print(f"拼团: {plan['groupon_uuid']}；共 {len(operations)} 项；剩余 {len(pending)} 项")
    show([op for _, op in pending])
    if pending and not yes:
        expected = f"APPLY {len(pending)}"
        if input(f"输入 {expected} 执行: ").strip() != expected:
            fail("已取消")
    if any(op["action"] == "delete" for _, op in pending):
        if input("包含永久删除。输入 DELETE 确认: ").strip() != "DELETE":
            fail("已取消删除")
    group, query = plan["groupon_uuid"], plan.get("query", "")
    current = client.inventory(group, query=query) if query else client.inventory(group)
    first_pending = pending[0][0] if pending else None
    reconciled = None
    for index, op in enumerate(operations, 1):
        item = current.get(op["goods_uuid"])
        if index in done:
            if not matches_after(item, op):
                fail(f"已完成项后台状态与 after 不一致，停止续跑: {op['goods_uuid']}")
        elif resume and index == first_pending and matches_after(item, op):
            reconciled = (index, op)
        elif not item or snapshot(item, op["action"], op.get("sku_uuid", "")) != op["before"]:
            fail(f"后台状态已变化，未执行任何操作；请人工核对: {op['goods_uuid']}")
    completed_deletes = [op for index, op in enumerate(operations, 1)
                         if op["action"] == "delete" and (index in done or
                            (reconciled and index == reconciled[0]))]
    if query and completed_deletes:
        full = client.inventory(group)
        for op in completed_deletes:
            if op["goods_uuid"] in full:
                fail(f"删除项后台状态不符，完整列表中仍有商品: {op['goods_uuid']}")
    if reconciled:
        pending = pending[1:]
    if not pending and not reconciled:
        print("全部项目已完成并回查通过")
        return
    backup_deleted(plan, [op for _, op in pending], current)
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
            if reconciled:
                index, op = reconciled
                append_execution_log(handle, plan, index, op, "done", reconciled=True)
                done.add(index)
                print(f"第 {index} 项续跑时确认已生效，记为完成")
            if not pending:
                print("全部项目已完成并回查通过")
                return
            execute_operations(client, plan, pending, current, handle, len(done))
    except OSError:
        fail("执行日志无法打开或持久化，停止操作；请检查权限、磁盘并核对后台状态")


def main(argv=None):
    import auth_config
    bootstrap = ArgumentParser(add_help=False)
    source = bootstrap.add_mutually_exclusive_group()
    source.add_argument('--env-file', type=Path, default=auth_config.DEFAULT_ENV, help='配置文件，默认脚本目录 .env')
    source.add_argument('--no-env-file', action='store_true', help='仅使用外部环境变量，不读取或保存配置文件')
    options, remaining = bootstrap.parse_known_args(argv)
    config = auth_config.AuthConfig(None if options.no_env_file else options.env_file)
    config.load()
    parser = ArgumentParser(description="拼团呀商品与订单管理", parents=[bootstrap])
    parser.add_argument("--group", default=os.getenv("PINTUANYA_GROUP"), help="拼团 UUID 或完整名称；list/get/export/plan/orders 必填")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("groups", help="列出可选拼团")
    commands.add_parser("check", help="检查登录状态和拼团总数")
    commands.add_parser('auth-status', help='查看凭据来源和只读登录检查（不显示凭据）')
    auth_login = commands.add_parser('auth-login', help='微信扫码登录并验证保存至 .env，不打开浏览器')
    auth_login.add_argument('--timeout', type=int, default=180, help='登录总超时秒数，默认180，最多600')
    auth_login.add_argument('--qr-output', type=Path, help='二维码图片输出路径，文件必须不存在；省略则结束时自动清理')
    auth_save = commands.add_parser("auth-save", help="验证 Cookie 并保存至 .env，不使用钥匙串")
    auth_save.add_argument("--clipboard", action="store_true", help="读取系统剪贴板文本；默认从标准输入读取")
    commands.add_parser("categories", help="列出商品分类")
    catalog_list = commands.add_parser("catalog-list", help="按后台原顺序回查全拼团或指定分类的上架商品")
    catalog_list.add_argument("--category", default="", help="分类 UUID；省略表示全拼团")
    catalog_list.add_argument("--limit", type=int, default=10, help="完整分页核对后输出前 N 件，默认10")
    upload = commands.add_parser("upload-image", help="上传本地 PNG/JPEG/WebP 商品图片并等待审核")
    upload.add_argument("image", type=Path)
    catalog_plan = commands.add_parser("catalog-plan", help="从 JSON 生成创建/追加规格/分类/置顶计划")
    catalog_plan.add_argument("input", type=Path)
    catalog_plan.add_argument("output", type=Path)
    catalog_apply = commands.add_parser("catalog-apply", help="执行目录计划，支持中断后重新运行核对")
    catalog_apply.add_argument("plan", type=Path)
    catalog_apply.add_argument("--yes", action="store_true")
    listing = commands.add_parser("list", help="查询商品列表")
    listing.add_argument("--query", default="", help="仅查询匹配标题的商品")
    listing.add_argument("--status", choices=("online", "offline", "frozen", "all"), default="all")
    listing.add_argument("--format", choices=("table", "json", "csv"), default="table")
    get = commands.add_parser("get", help="按 UUID 查看商品原始 JSON")
    get.add_argument("goods_uuid", help="商品 UUID，精确匹配")
    get.add_argument("--query", default="", help="先按标题缩小查询范围")
    export = commands.add_parser("export", help="导出商品和 SKU CSV")
    export.add_argument("--query", default="", help="仅查询匹配标题的商品")
    export.add_argument("output", type=Path)
    plan = commands.add_parser("plan", help="校验 CSV 并保存计划")
    plan.add_argument("--query", default="", help="仅查询匹配标题的商品，适合少量操作")
    plan.add_argument("input", type=Path)
    plan.add_argument("output", type=Path)
    run = commands.add_parser("apply", help="执行已保存计划")
    run.add_argument("plan", type=Path)
    run.add_argument("--yes", action="store_true", help="跳过普通确认；删除仍需二次输入")
    run.add_argument("--resume", action="store_true", help="校验执行日志和已完成项后续跑")
    for name, help_text in (("orders", "只读查询订单（默认近180天、不含已取消，个人信息脱敏）"),
                            ("orders-export", "导出订单 CSV（默认每单一行、个人信息脱敏）")):
        sub = commands.add_parser(name, help=help_text)
        if name == "orders-export":
            sub.add_argument("output", type=Path)
            sub.add_argument("--force", action="store_true", help="允许覆盖已存在的文件")
        sub.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD（北京时间，含当天）；默认为 --to 前179天")
        sub.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD（含当天）；默认今天")
        sub.add_argument("--status", choices=("all", "unpaid", "paid", "shipped", "canceled", "after-sales", "any"),
                         default="all", help="all=全部未取消（网页默认），any=含已取消")
        sub.add_argument("--query", default="", help="后台搜索：单号、昵称、收件人、商品名等")
        sub.add_argument("--level", choices=("order", "item"), default="order", help="CSV 每单一行或每个商品一行")
        sub.add_argument("--pii", choices=("mask", "full"), default="mask", help="收件人姓名/电话/地址是否脱敏，默认 mask")
        if name == "orders":
            sub.add_argument("--format", choices=("table", "json", "csv"), default="table")
            sub.add_argument("--limit", type=int, default=50, help="表格显示前 N 单，默认50")
    args = parser.parse_args(remaining)
    if args.command in ('auth-save', 'auth-status', 'auth-login'):
        import auth
        auth.run_command(args, config, Client)
        return
    if args.command in ("list", "get", "export", "plan", "categories", "catalog-plan", "catalog-list",
                        "orders", "orders-export") and not args.group:
        fail("请先用 groups 查看并选择拼团；通过 --group 传入 UUID 或完整名称")
    if args.command in ("catalog-list", "orders") and getattr(args, "limit", 1) <= 0:
        fail("--limit 必须大于零")
    cookie = os.getenv("PINTUANYA_COOKIE")
    client = Client(cookie)
    if config.source == '配置文件':
        client.persist_cookie = lambda updated: config.save(updated, client.device_id)
    if args.command in ("orders", "orders-export"):
        import orders
        orders.run(client, resolve_group(client, args.group), args)
    elif args.command == "upload-image":
        import catalog
        print(catalog.upload_image(client, args.image))
    elif args.command in ("categories", "catalog-plan", "catalog-apply", "catalog-list"):
        import catalog
        if args.command == "catalog-apply":
            document = json.loads(args.plan.read_text(encoding="utf-8"))
            if args.group and resolve_group(client, args.group) != document.get("groupon_uuid"):
                fail("计划拼团与 --group 不一致")
            catalog.run(client, document, args.plan, args.yes)
        else:
            group = resolve_group(client, args.group)
            if args.command == "categories":
                print(json.dumps(client.categories(group), ensure_ascii=False, indent=2))
            elif args.command == "catalog-list":
                items = filter_goods(client.category_goods(group, args.category), "online")
                print(json.dumps(items[:args.limit], ensure_ascii=False, indent=2))
            else:
                specs = json.loads(args.input.read_text(encoding="utf-8"))
                if not isinstance(specs, list):
                    fail("目录输入必须是 JSON 数组")
                document = {"version": 1, "kind": "catalog", "groupon_uuid": group,
                            "operations": catalog.prepare(client, group, specs)}
                document["sha256"] = digest(document)
                args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(document, ensure_ascii=False, indent=2))
    elif args.command == "check":
        print(f"登录有效；拼团总数: {client.check()}")
    elif args.command == "groups":
        for group in client.groups():
            print(f"{group.get('groupon_uuid', '')}\t{group.get('title', '')}")
    elif args.command in ("list", "get", "export"):
        group = resolve_group(client, args.group)
        inventory = client.inventory(group, query=args.query) if args.query else client.inventory(group)
        if args.command == "get":
            item = inventory.get(args.goods_uuid)
            if item is None:
                fail(f"找不到商品 {args.goods_uuid}")
            print(json.dumps(item, ensure_ascii=False, indent=2))
        elif args.command == "export":
            with args.output.open("w", newline="", encoding="utf-8-sig") as handle:
                write_csv(inventory.values(), handle)
            print(f"已导出 {len(inventory)} 件商品: {args.output}")
        else:
            items = filter_goods(inventory.values(), args.status)
            if args.format == "json":
                print(json.dumps(items, ensure_ascii=False, indent=2))
            elif args.format == "csv":
                write_csv(items, sys.stdout)
            else:
                show_table(items)
    elif args.command == "plan":
        group = resolve_group(client, args.group)
        inventory = client.inventory(group, query=args.query) if args.query else client.inventory(group)
        operations = prepare(read_rows(args.input), inventory)
        document = {"version": 1, "groupon_uuid": group,
                    "created_at": datetime.now(timezone.utc).isoformat(), "operations": operations}
        if args.query:
            document["query"] = args.query
        document["sha256"] = digest(document)
        args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        show(operations)
        print(f"计划已保存: {args.output}")
    else:
        document = json.loads(args.plan.read_text(encoding="utf-8"))
        if args.group and document.get("groupon_uuid") != resolve_group(client, args.group):
            fail("计划中的拼团 UUID 与 --group 不一致")
        apply(client, document, args.yes, plan_path=args.plan, resume=args.resume)


def cli(argv=None):
    try:
        main(argv)
        return 0
    except LoginError as error:
        print(f"错误: {safe_error(error)}", file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyboardInterrupt) as error:
        print(f"错误: {safe_error(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
