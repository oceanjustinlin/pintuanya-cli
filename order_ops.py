"""订单写操作：修改状态、添加/删除标记。plan → apply，逐单写入并回查。

只使用网页单笔操作的同步接口（/order/status、/order/tag、/order/tag/delete），
不使用“操作可能需要几分钟”的异步批量任务，以便每项写入后都能立即回查。
"""
import csv
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import orders

ACTIONS = {"status", "tag", "untag"}
STATUS_TARGETS = {"unpaid": 1, "paid": 2, "shipped": 3, "canceled": 100}  # 与网页“修改订单状态”选项一致
TAG_TYPES = {"public": 1, "private": 2}
TAG_TYPE_TEXT = {1: "普通标记", 2: "隐私标记"}
LOOKUP_STATUSES = (0, 100, 101)  # 后台 0 只含未取消订单，需另查已取消和售后
STATUSES_ANY = [0, 100, 101]  # 计划时需找到所有状态的订单
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def fail(message):
    raise ValueError(message)


# ---------- 读取后台状态 ----------

def tags(client, order_uuid):
    data = client.request("GET", "/order/tag", params={"order_uuid": order_uuid}).get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        fail("订单标记格式已变化，停止操作")
    result = []
    for tag in data:
        if (not isinstance(tag, dict) or tag.get("tag_id") in (None, "")
                or not isinstance(tag.get("name"), str) or tag.get("type") not in TAG_TYPE_TEXT):
            fail("订单标记格式已变化，停止操作")
        result.append({"tag_id": str(tag["tag_id"]), "name": tag["name"], "type": tag["type"]})
    return result


def find_order(client, group, op):
    """按订单号在下单当天精确查找，再按 order_uuid 匹配。"""
    day = date.fromisoformat(op["day"])
    found = []
    for status in LOOKUP_STATUSES:
        body = orders.query_body(group, status, day, day, str(op["sequence"]), 1)
        body["query_field"] = 1  # 网页搜索框“单号”
        batch, _ = orders.fetch_page(client, body)
        found += [o for o in batch if str(o.get("order_uuid")) == op["order_uuid"]]
    if len(found) > 1:
        fail(f"订单在多个状态中重复出现: {op['order_uuid']}")
    return found[0] if found else None


def state(client, group, op):
    if op["action"] == "status":
        order = find_order(client, group, op)
        if order is None:
            fail(f"找不到订单，停止操作: {op['sequence']}")
        return order.get("status")
    current = tags(client, op["order_uuid"])
    if op["action"] == "tag":
        return any(t["name"] == op["name"] and t["type"] == op["type"] for t in current)
    return any(t["tag_id"] == op["tag_id"] for t in current)


def write(client, op):
    if op["action"] == "status":
        client.request("PUT", "/order/status", body={"order_uuid": op["order_uuid"], "order_status": op["after"]})
    elif op["action"] == "tag":
        client.request("POST", "/order/tag", body={"order_uuid": op["order_uuid"], "type": op["type"], "tag": op["name"]})
    else:
        client.request("PUT", "/order/tag/delete", body={"order_uuid": op["order_uuid"], "tag_id": op["tag_id"]})


# ---------- 计划 ----------

def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"action", "order", "value"}.issubset(reader.fieldnames):
            fail("CSV 至少需要 action,order,value 三列（tag_type 可选）")
        rows = []
        for line, raw in enumerate(reader, 2):
            row = {key: (value or "").strip() for key, value in raw.items() if key}
            if not any(row.values()):
                continue
            if row["action"] not in ACTIONS:
                fail(f"第 {line} 行操作无效（status/tag/untag）: {row['action']}")
            if not row["order"] or not row["value"]:
                fail(f"第 {line} 行缺少 order 或 value")
            row["line"] = line
            rows.append(row)
    if not rows:
        fail("CSV 没有操作行")
    return rows


def index_orders(all_orders):
    by_uuid, by_sequence = {}, {}
    for order in all_orders:
        by_uuid[str(order["order_uuid"])] = order
        by_sequence.setdefault(str(order.get("sequence", "")), []).append(order)
    return by_uuid, by_sequence


def resolve(row, by_uuid, by_sequence):
    key = row["order"]
    if key in by_uuid:
        return by_uuid[key]
    matches = by_sequence.get(key, [])
    if len(matches) > 1:
        fail(f"第 {row['line']} 行订单号不唯一，请改用 order_uuid: {key}")
    if not matches:
        fail(f"第 {row['line']} 行在查询日期范围内找不到订单（可用 --from 扩大范围）: {key}")
    return matches[0]


def tag_type(row, default=None):
    value = row.get("tag_type", "")
    if not value:
        return default
    if value not in TAG_TYPES:
        fail(f"第 {row['line']} 行 tag_type 只能是 public 或 private: {value}")
    return TAG_TYPES[value]


def prepare(client, rows, all_orders):
    by_uuid, by_sequence = index_orders(all_orders)
    operations, seen = [], set()
    tag_cache = {}
    for row in rows:
        order = resolve(row, by_uuid, by_sequence)
        uid = str(order["order_uuid"])
        base = {"action": row["action"], "order_uuid": uid, "sequence": order.get("sequence"),
                "day": str(order.get("create_time", ""))[:10]}
        try:
            date.fromisoformat(base["day"])
        except ValueError:
            fail(f"订单下单时间格式已变化: {uid}")
        if row["action"] == "status":
            if row["value"] not in STATUS_TARGETS:
                fail(f"第 {row['line']} 行目标状态只能是 {'/'.join(STATUS_TARGETS)}: {row['value']}")
            before, after = order.get("status"), STATUS_TARGETS[row["value"]]
            if before not in orders.STATUS_TEXT or before == 101:
                fail(f"第 {row['line']} 行订单处于{orders.text(orders.STATUS_TEXT, before)}，CLI 不修改；请在网页处理")
            if order.get("refund_request_status") == 1:
                fail(f"第 {row['line']} 行订单有待处理的退款申请，CLI 不修改状态；请在网页处理")
            if before == after:
                fail(f"第 {row['line']} 行订单已是目标状态: {order.get('sequence')}")
            key = ("status", uid)
            op = dict(base, before=before, after=after)
        else:
            name = row["value"]
            if CONTROL.search(name):
                fail(f"第 {row['line']} 行标记包含换行或控制字符")
            if uid not in tag_cache:
                tag_cache[uid] = tags(client, uid)
            current = tag_cache[uid]
            if row["action"] == "tag":
                kind = tag_type(row, TAG_TYPES["public"])
                if any(t["name"] == name and t["type"] == kind for t in current):
                    fail(f"第 {row['line']} 行订单已有该标记: {order.get('sequence')} {name}")
                key = ("tag", uid, name, kind)
                op = dict(base, name=name, type=kind, before=False, after=True)
            else:
                kind = tag_type(row)
                matches = [t for t in current if t["name"] == name and kind in (None, t["type"])]
                if len(matches) != 1:
                    fail(f"第 {row['line']} 行要删除的标记{'不存在' if not matches else '不唯一，请填写 tag_type'}: "
                         f"{order.get('sequence')} {name}")
                key = ("tag", uid, name, matches[0]["type"])
                op = dict(base, name=name, type=matches[0]["type"], tag_id=matches[0]["tag_id"],
                          before=True, after=False)
        # 添加要求标记不存在、删除要求存在，同一标记的增删必然有一项在上面被拒绝；这里只拦重复行。
        if key in seen:
            fail(f"第 {row['line']} 行与前面的操作重复")
        seen.add(key)
        operations.append(op)
    return operations


def describe(op):
    if op["action"] == "status":
        return (f"status  订单 {op['sequence']}  {orders.text(orders.STATUS_TEXT, op['before'])}"
                f" → {orders.text(orders.STATUS_TEXT, op['after'])}")
    verb = "添加" if op["action"] == "tag" else "删除"
    return f"{op['action']:<7} 订单 {op['sequence']}  {verb}{TAG_TYPE_TEXT[op['type']]}【{op['name']}】"


def show(operations):
    for index, op in enumerate(operations, 1):
        print(f"{index:>3}. {describe(op)}")


def plan_document(client, group, args):
    from pintuanya import digest
    start_day, end_day = orders.parse_range(args.date_from, args.date_to)
    rows = read_rows(args.input)
    all_orders = orders.fetch_orders(client, group, STATUSES_ANY, start_day, end_day)
    operations = prepare(client, rows, all_orders)
    document = {"version": 1, "kind": "orders", "groupon_uuid": group,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "range": [start_day.isoformat(), end_day.isoformat()], "operations": operations}
    document["sha256"] = digest(document)
    return document


# ---------- 执行 ----------

def needs_cancel_confirm(op):
    return op["action"] == "status" and 100 in (op["before"], op["after"])


def validate_plan(plan):
    from pintuanya import digest
    operations = plan.get("operations")
    if (plan.get("kind") != "orders" or not isinstance(operations, list) or not operations
            or any(not isinstance(op, dict) or op.get("action") not in ACTIONS for op in operations)):
        fail("订单计划无效")
    if digest({k: v for k, v in plan.items() if k != "sha256"}) != plan.get("sha256"):
        fail("计划文件校验失败")


def read_log(path, plan, resume):
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
    done = set()
    lines = raw.splitlines()
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
        if (record.get("action") != op["action"] or record.get("order_uuid") != op["order_uuid"]
                or record.get("status") not in ("done", "failed")):
            fail("执行日志记录与计划不一致或格式无效")
        if index != len(done) + 1:
            fail("执行日志顺序异常，停止操作")
        if record["status"] == "done":
            done.add(index)
    if lines and not resume:
        fail("日志已有执行记录，请使用 order-apply --resume 续跑")
    return done


def append_log(handle, plan, index, op, status, error="", reconciled=False):
    record = {"plan_sha256": plan["sha256"], "index": index, "action": op["action"],
              "order_uuid": op["order_uuid"], "status": status, "error": error,
              "at": datetime.now(timezone.utc).isoformat()}
    if reconciled:
        record["reconciled"] = True
    try:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    except OSError:
        fail("执行日志写入失败；停止后续操作，请人工核对本项后台状态")


def apply(client, plan, plan_path, yes=False, resume=False, ask=input):
    from pintuanya import LoginError, safe_error
    validate_plan(plan)
    group, operations = plan["groupon_uuid"], plan["operations"]
    log_path = Path(str(plan_path) + ".log.jsonl")
    done = read_log(log_path, plan, resume)
    pending = [(i, op) for i, op in enumerate(operations, 1) if i not in done]
    print(f"拼团: {group}；共 {len(operations)} 项；剩余 {len(pending)} 项")
    show([op for _, op in pending])
    if not pending:
        print("全部项目已完成")
        return
    if not yes:
        expected = f"APPLY {len(pending)}"
        if ask(f"输入 {expected} 执行: ").strip() != expected:
            fail("已取消")
    if any(needs_cancel_confirm(op) for _, op in pending):
        if ask("包含取消订单或恢复已取消订单，可能影响库存和买家。输入 CANCEL 确认: ").strip() != "CANCEL":
            fail("已取消")
    # 执行前统一核对：已完成项须符合 after；续跑时第一个未完成项可已符合 after；其余须符合 before。
    first = pending[0][0]
    reconciled = None
    for index, op in enumerate(operations, 1):
        current = state(client, group, op)
        if index in done:
            if current != op["after"]:
                fail(f"已完成项后台状态与 after 不一致，停止续跑: 订单 {op['sequence']}")
        elif resume and index == first and current == op["after"]:
            reconciled = index
        elif current != op["before"]:
            fail(f"后台状态已变化，未执行任何操作；请人工核对: 订单 {op['sequence']}")
    try:
        handle = log_path.open("a", encoding="utf-8")
    except OSError:
        fail("执行日志无法打开，停止操作")
    with handle:
        count = len(done)
        for index, op in pending:
            if index == reconciled:
                append_log(handle, plan, index, op, "done", reconciled=True)
                count += 1
                print(f"第 {index} 项续跑时确认已生效，记为完成")
                continue
            try:
                write(client, op)
                if state(client, group, op) != op["after"]:
                    fail(f"写入后回查不一致: 订单 {op['sequence']}")
            except (Exception, KeyboardInterrupt) as error:
                message = safe_error(error)
                try:
                    append_log(handle, plan, index, op, "failed", message)
                except ValueError as log_error:
                    message += f"；{log_error}"
                error_type = LoginError if isinstance(error, LoginError) else ValueError
                raise error_type(f"已完成 {count}/{len(operations)} 项；在第 {index} 项停止: {message}") from None
            append_log(handle, plan, index, op, "done")
            count += 1
            print(f"完成 {count}/{len(operations)}: {describe(op)}")
    print("全部项目已完成并回查通过")
