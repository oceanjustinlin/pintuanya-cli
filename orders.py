"""订单只读查询与 CSV 导出。接口与字段见 docs/orders.md。"""
import csv
import json
import os
import re
import sys
import tempfile
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PAGE_SIZE = 30          # 后台上限；超过 30 返回 HTTP 400
ITEM_PAGE_SIZE = 100
MAX_PAGES = 1000
DEFAULT_DAYS = 180      # 与网页默认“近180天”一致
CHINA = timezone(timedelta(hours=8))

# CLI 稳定名称 → 后台 order_status。后台 0 表示“全部未取消订单”，不含已取消。
STATUSES = {"all": [0], "unpaid": [1], "paid": [2], "shipped": [3], "canceled": [100],
            "after-sales": [101], "any": [0, 100]}
STATUS_TEXT = {1: "未付款", 2: "已付款", 3: "已发货", 100: "已取消", 101: "售后"}
PAYMENT_TEXT = {1: "二维码收款", 2: "在线付款"}
TRANSACTION_TEXT = {0: "", 1: "已入账", 10: "已退款", 11: "退款中", 12: "部分退款"}
REFUND_REQUEST_TEXT = {0: "", 1: "新申请退款", 2: "已同意退款", 10: "已拒绝退款", 11: "退款失败"}

ORDER_COLUMNS = [
    "order_uuid", "sequence", "create_time", "status", "status_text", "payment_method",
    "transaction_status", "refund_request_status", "buyer_nickname", "buyer_remark",
    "receiver_name", "receiver_tel", "receiver_address", "product_types", "item_count",
    "amount", "shipping_amount", "full_reduction", "coupon_amount", "membership_amount",
    "refund_amount", "transaction_amount", "comments", "deleted_status",
]
ITEM_COLUMNS = ORDER_COLUMNS[:15] + [
    "item_index", "goods_uuid", "goods_title", "sku_uuid", "sku_title", "quantity", "price",
    "discount", "paid_amount", "shipped_quantity", "refunded_quantity", "refunded_amount",
] + ORDER_COLUMNS[15:]


def fail(message):
    raise ValueError(message)


def today():
    return datetime.now(CHINA).date()


def parse_range(start, end, now=None):
    """后台只按日期过滤（起止日均包含，时分秒被忽略），因此只接受 YYYY-MM-DD。"""
    now = now or today()

    def parse(value, name):
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            fail(f"{name} 必须是 YYYY-MM-DD 日期（按北京时间整天计算）: {value}")
    end_day = parse(end, "--to") if end else now
    start_day = parse(start, "--from") if start else end_day - timedelta(days=DEFAULT_DAYS - 1)
    if start_day > end_day:
        fail("--from 不能晚于 --to")
    return start_day, end_day


def query_body(group, status, start_day, end_day, query, page):
    return {"groupon_uuid": group, "page": page, "size": PAGE_SIZE, "order_status": status,
            "sort_order": 0, "query_field": 0, "query_value": query,
            "start_time": f"{start_day.isoformat()} 00:00:00",
            "end_time": f"{end_day.isoformat()} 23:59:59"}


def fetch_page(client, body):
    data = client.request("POST", "/order/batch", body=body).get("data") or {}
    batch, total = data.get("list"), data.get("total")
    if batch is None and total == 0:
        batch = []
    if not isinstance(batch, list) or type(total) is not int or total < 0:
        fail("订单列表格式已变化，停止操作")
    return batch, total


def fetch_status(client, group, status, start_day, end_day, query, pause):
    """按下单时间升序翻页：新订单只会追加到末尾。结束后复核总数，变化即报错。"""
    orders, seen, expected = [], set(), None
    for page in range(1, MAX_PAGES + 1):
        batch, total = fetch_page(client, query_body(group, status, start_day, end_day, query, page))
        if expected is None:
            expected = total
        elif total != expected:
            fail("翻页期间订单总数发生变化，结果可能不完整；请稍后重试或缩小日期范围")
        for order in batch:
            uid = str(order.get("order_uuid") or "")
            if not uid:
                fail("订单缺少 order_uuid，停止操作")
            if uid in seen:
                fail(f"订单重复出现，翻页结果不可靠: {uid}")
            seen.add(uid)
            orders.append(order)
        if len(orders) >= expected:
            break
        if not batch:
            fail("订单分页提前结束，停止操作")
        if pause:
            time.sleep(pause)
    else:
        fail("订单分页超过上限，请缩小日期范围")
    if len(orders) != expected:
        fail(f"订单数量与后台总数不一致（{len(orders)}/{expected}），停止操作")
    _, final_total = fetch_page(client, dict(query_body(group, status, start_day, end_day, query, 1), size=1))
    if final_total != expected:
        fail("翻页期间订单总数发生变化，结果可能不完整；请稍后重试或缩小日期范围")
    return orders


def fetch_items(client, order_uuid, pause):
    items = []
    for page in range(1, MAX_PAGES + 1):
        data = client.request("GET", "/order/item/batch", params={
            "order_uuid": order_uuid, "page": page, "size": ITEM_PAGE_SIZE}).get("data") or {}
        batch = data.get("list")
        if not isinstance(batch, list):
            fail("订单商品列表格式已变化，停止操作")
        items.extend(batch)
        if not data.get("next"):
            break
        if not batch:
            fail("订单商品分页提前结束，停止操作")
        if pause:
            time.sleep(pause)
    else:
        fail("订单商品分页异常")
    return items


def complete_items(client, order, pause):
    """列表接口每单最多返回 10 种商品，不足时用商品明细接口补齐。"""
    items = order.get("order_item_list") or []
    types = order.get("number_of_product_types") or 0
    if type(types) is not int or types < 0:
        fail(f"订单商品种类字段异常: {order.get('order_uuid')}")
    if len(items) < types:
        items = fetch_items(client, order["order_uuid"], pause)
        if len(items) < types:
            fail(f"订单商品明细不完整: {order.get('order_uuid')}")
    order["order_item_list"] = items
    return order


def fetch_orders(client, group, statuses, start_day, end_day, query="", pause=0.5, progress=None):
    result, seen = [], set()
    for status in statuses:
        for order in fetch_status(client, group, status, start_day, end_day, query, pause):
            uid = str(order["order_uuid"])
            if uid in seen:
                fail(f"订单在多个状态中重复出现: {uid}")
            seen.add(uid)
            result.append(order)
    fetched = 0
    for order in result:
        before = len(order.get("order_item_list") or [])
        complete_items(client, order, pause)
        if len(order["order_item_list"]) != before:
            fetched += 1
    if progress and fetched:
        progress(f"已从商品明细接口补齐 {fetched} 单")
    result.sort(key=lambda o: (str(o.get("create_time", "")), str(o["order_uuid"])))
    return result


# ---------- 个人信息脱敏 ----------

PHONE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")


def mask_tel(value):
    value = str(value or "")
    if len(value) >= 7:
        return value[:3] + "****" + value[-4:]
    return "*" * len(value)


def mask_name(value):
    value = str(value or "")
    return value[:1] + "*" * (len(value) - 1) if value else ""


def mask_text(value):
    return PHONE.sub(r"\1****\2", str(value or ""))


def address_text(address, pii):
    if not isinstance(address, dict):
        return ""
    parts = [address.get("province"), address.get("city")]
    if pii == "full":
        parts += [address.get("area"), address.get("address")]
    return " ".join(str(p) for p in parts if p)


def mask_order(order):
    """返回脱敏副本，用于 JSON 输出；不修改原对象。"""
    masked = deepcopy(order)
    address = masked.get("user_address")
    if isinstance(address, dict):
        address["name"] = mask_name(address.get("name"))
        address["tel"] = mask_tel(address.get("tel"))
        address["area"] = ""
        address["address"] = ""
    station = masked.get("pickup_station")
    if isinstance(station, dict):
        for key, value in list(station.items()):
            if "tel" in key or "phone" in key:
                station[key] = mask_tel(value)
    if "comments" in masked:
        masked["comments"] = mask_text(masked.get("comments"))
    return masked


# ---------- 输出 ----------

def money(value):
    if value in (None, ""):
        return ""
    from decimal import Decimal
    return f"{Decimal(int(value)) / 100:.2f}"


def text(mapping, value):
    if value in (None, ""):
        return ""
    return mapping.get(value, f"未知({value})")


def order_fields(order, pii):
    user = order.get("user") or {}
    address = order.get("user_address") or {}
    full = pii == "full"
    return {
        "order_uuid": order.get("order_uuid", ""), "sequence": order.get("sequence", ""),
        "create_time": order.get("create_time", ""), "status": order.get("status", ""),
        "status_text": text(STATUS_TEXT, order.get("status")),
        "payment_method": text(PAYMENT_TEXT, order.get("payment_method")),
        "transaction_status": text(TRANSACTION_TEXT, order.get("transaction_status")),
        "refund_request_status": text(REFUND_REQUEST_TEXT, order.get("refund_request_status")),
        "buyer_nickname": user.get("nickname", ""), "buyer_remark": user.get("remark", ""),
        "receiver_name": address.get("name", "") if full else mask_name(address.get("name")),
        "receiver_tel": address.get("tel", "") if full else mask_tel(address.get("tel")),
        "receiver_address": address_text(address, pii),
        "product_types": order.get("number_of_product_types", ""),
        "item_count": order.get("order_item_count", ""),
        "amount": money(order.get("amount")), "shipping_amount": money(order.get("shipping_amount")),
        "full_reduction": money(order.get("full_reduction")), "coupon_amount": money(order.get("coupon_amount")),
        "membership_amount": money(order.get("membership_amount")),
        "refund_amount": money(order.get("refund_amount")),
        "transaction_amount": money(order.get("transaction_amount")),
        "comments": order.get("comments", "") if full else mask_text(order.get("comments")),
        "deleted_status": order.get("deleted_status", ""),
    }


def item_fields(item, index):
    return {
        "item_index": index, "goods_uuid": item.get("goods_uuid", ""),
        "goods_title": item.get("goods_title", ""), "sku_uuid": item.get("sku_uuid", ""),
        "sku_title": item.get("sku_title", ""), "quantity": item.get("quantity", ""),
        "price": money(item.get("price")), "discount": money(item.get("discount")),
        "paid_amount": money(item.get("paid_amount")),
        "shipped_quantity": item.get("shipped_quantity", ""),
        "refunded_quantity": item.get("refunded_quantity", ""),
        "refunded_amount": money(item.get("refunded_amount")),
    }


def rows(orders, level, pii):
    if level == "order":
        for order in orders:
            fields = order_fields(order, pii)
            yield [fields[c] for c in ORDER_COLUMNS]
        return
    for order in orders:
        base = order_fields(order, pii)
        items = order.get("order_item_list") or [{}]
        for index, item in enumerate(items, 1):
            fields = dict(base, **item_fields(item, index if item else ""))
            yield [fields[c] for c in ITEM_COLUMNS]


def write_csv(orders, handle, level, pii):
    writer = csv.writer(handle)
    writer.writerow(ORDER_COLUMNS if level == "order" else ITEM_COLUMNS)
    count = 0
    for row in rows(orders, level, pii):
        writer.writerow(row)
        count += 1
    return count


def check_target(path, force):
    path = Path(path)
    if path.is_symlink():
        fail(f"目标是符号链接，未写入: {path}")
    if path.exists() and not force:
        fail(f"目标文件已存在，未写入；如需覆盖请加 --force: {path}")
    if not (path.parent if str(path.parent) else Path(".")).is_dir():
        fail(f"目标目录不存在: {path.parent}")


def export(orders, path, level, pii, force=False):
    """先写同目录临时文件，完成后原子替换；权限 0600；默认拒绝覆盖。"""
    path = Path(path)
    check_target(path, force)
    directory = path.parent if str(path.parent) else Path(".")
    fd, temp = tempfile.mkstemp(prefix=".orders-", suffix=".csv.tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as handle:
            count = write_csv(orders, handle, level, pii)
            handle.flush()
            os.fsync(handle.fileno())
        if not force and path.exists():
            fail(f"目标文件已存在，未写入: {path}")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    return count


def summary(orders):
    valid = [o for o in orders if o.get("status") != 100]
    total = sum(int(o.get("amount") or 0) for o in valid)
    refund = sum(int(o.get("refund_amount") or 0) for o in valid)
    counts = {}
    for order in orders:
        label = text(STATUS_TEXT, order.get("status")) or "未知"
        counts[label] = counts.get(label, 0) + 1
    parts = "，".join(f"{k} {v}" for k, v in counts.items())
    return f"订单 {len(orders)} 单（{parts or '无'}）；未取消订单金额合计 ¥{money(total)}；退款合计 ¥{money(refund)}"


def show_table(orders, limit, pii):
    table = [["订单号", "下单时间", "状态", "买家", "收件人", "件数", "订单金额"]]
    for order in orders[:limit]:
        f = order_fields(order, pii)
        table.append([str(f["sequence"]), str(f["create_time"])[:19].replace("T", " "), f["status_text"],
                      str(f["buyer_nickname"]), str(f["receiver_name"]), str(f["item_count"]), f["amount"]])
    table = [[re.sub(r"[\r\n\t]", " ", cell) for cell in row] for row in table]
    widths = [max(display_width(row[i]) for row in table) for i in range(len(table[0]))]
    for row in table:
        print("  ".join(cell + " " * (w - display_width(cell)) for cell, w in zip(row, widths)))
    if len(orders) > limit:
        print(f"…… 仅显示前 {limit} 单，共 {len(orders)} 单；使用 --limit 或 orders-export 查看全部")


def display_width(value):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)


def run(client, group, args):
    start_day, end_day = parse_range(args.date_from, args.date_to)
    if args.command == "orders-export":
        check_target(args.output, args.force)
    progress = lambda message: print(message, file=sys.stderr)
    progress(f"查询范围: {start_day} 至 {end_day}（北京时间，含首尾两天）；状态: {args.status}")
    orders = fetch_orders(client, group, STATUSES[args.status], start_day, end_day,
                          args.query, progress=progress)
    if args.pii == "full":
        progress("提醒: 输出包含收件人姓名、电话和完整地址，请妥善保管，勿提交到 Git 或公开分享")
    if args.command == "orders-export":
        count = export(orders, args.output, args.level, args.pii, args.force)
        print(f"已导出 {len(orders)} 单、{count} 行: {args.output}")
        print(summary(orders))
    elif args.format == "json":
        data = orders if args.pii == "full" else [mask_order(o) for o in orders]
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        write_csv(orders, sys.stdout, args.level, args.pii)
    else:
        show_table(orders, args.limit, args.pii)
        print(summary(orders))
