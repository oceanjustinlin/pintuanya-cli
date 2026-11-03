# 订单查询与导出

只读功能，不修改任何订单。用于对账和统计。所有请求都在命令行完成，不打开浏览器。

```bash
python3 pintuanya.py --group '拼团UUID' orders
python3 pintuanya.py --group '拼团UUID' orders --from 2026-10-01 --to 2026-10-31 --status shipped
python3 pintuanya.py --group '拼团UUID' orders --query '订单号或昵称' --format json
python3 pintuanya.py --group '拼团UUID' orders-export exports/orders-2026-10.csv --from 2026-10-01 --to 2026-10-31
python3 pintuanya.py --group '拼团UUID' orders-export exports/items-2026-10.csv --from 2026-10-01 --to 2026-10-31 --level item
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `--from` / `--to` | 日期 `YYYY-MM-DD`，按北京时间整天计算，起止两天都包含。默认查询截至今天的近 180 天，与网页一致。后台只按日期过滤，所以不接受时分秒。 |
| `--status` | 默认 `all`。取值见下表。 |
| `--query` | 透传网页的“搜索”框（全部字段），可搜单号、买家昵称、收件人、电话、地址、商品名称、物流单号等。 |
| `--pii` | 默认 `mask`；`full` 输出完整收件信息，见下文“个人信息”。 |
| `--level` | 仅 CSV：`order`（默认，每单一行）或 `item`（每个商品一行）。 |
| `--format` | 仅 `orders`：`table`（默认）、`json`（后台原始订单对象，金额为分）、`csv`（输出到标准输出）。 |
| `--limit` | 仅表格输出：显示前 N 单，默认 50。统计摘要始终按全部结果计算。 |
| `--force` | 仅 `orders-export`：允许覆盖已存在的文件。 |

| `--status` | 后台值 | 含义 |
| --- | --- | --- |
| `all` | 0 | 全部**未取消**订单（网页默认的“全部”） |
| `unpaid` | 1 | 未付款 |
| `paid` | 2 | 已付款，未发货 |
| `shipped` | 3 | 已发货 |
| `canceled` | 100 | 已取消 |
| `after-sales` | 101 | 售后 |
| `any` | 0 + 100 | 未取消和已取消订单合并，完整对账用 |

表格和导出完成后都会打印摘要：订单数、各状态数量、未取消订单金额合计、退款合计。已取消订单不计入金额合计。

## CSV 列

导出文件采用 UTF-8 BOM 编码，Excel 可直接打开。金额单位为元；时间为后台原始的 ISO 8601 格式（`+08:00`）；状态列给出中文说明，未知状态显示为 `未知(代码)`。

**每单一行（默认）**

```text
order_uuid,sequence,create_time,status,status_text,payment_method,transaction_status,refund_request_status,buyer_nickname,buyer_remark,receiver_name,receiver_tel,receiver_address,product_types,item_count,amount,shipping_amount,full_reduction,coupon_amount,membership_amount,refund_amount,transaction_amount,comments,deleted_status
```

- `sequence`：网页显示的订单号。
- `amount`：订单金额，等于各商品实付合计加运费。
- `membership_amount`：会员优惠。
- `transaction_amount`：在线支付的入账金额；二维码收款的订单为 0。
- `deleted_status`：后台原始值，网页未展示，暂未确认含义，原样保留。

**每个商品一行（`--level item`）**

在 `item_count` 之后插入商品列：

```text
item_index,goods_uuid,goods_title,sku_uuid,sku_title,quantity,price,discount,paid_amount,shipped_quantity,refunded_quantity,refunded_amount
```

订单级的金额列在该订单的每一行都会重复。合计订单金额时，只统计 `item_index` 为 1 的行；统计商品金额时，用 `paid_amount`。没有商品的订单（个别已取消订单）也输出一行，商品列为空。

## 个人信息

默认 `mask` 模式下，表格、CSV 和 JSON 输出一律脱敏：

- 收件人姓名只保留首字；
- 电话显示为 `138****5678`；
- 地址只保留省和市；
- 备注中的手机号同样打码。

买家昵称和团长给买家的备注照常输出，因为二维码收款对账需要按昵称核对。

需要完整收件信息时加 `--pii full`，程序会在标准错误输出中提醒。导出文件权限为 0600。目标文件已存在时默认拒绝写入，在开始查询前就会检查。导出时先写临时文件再原子替换，失败不会留下半成品。建议放在 Git 已忽略的 `exports/` 目录。

## 可靠性

- 按下单时间升序翻页，每页 30 单（后台上限），页间间隔 0.5 秒。
- 每页都核对后台总数，并检查订单 ID 是否重复或缺失。翻页结束后再取一次总数，有变化就报错退出，不输出可能不完整的结果；这时稍后重试，或缩小日期范围。
- 列表接口每单最多返回 10 种商品，不足时自动从商品明细接口补齐；补齐后仍不完整就报错。
- 登录失效时退出码为 2，运行 `auth-login` 后重试即可。

## 接口依据（2026-11-03 命令行调研）

依据来自网页前端代码 `umi` 及订单页 chunk 的静态分析，以及只读实测。

- `POST /api/v1/order/batch`：订单列表，请求体字段为 `groupon_uuid`、`page`、`size`、`order_status`、`sort_order`（0 时间升序 / 1 降序 / 2 金额升序 / 3 降序）、`query_field`、`query_value`、`start_time`、`end_time`，以及网页的其他筛选项。响应 `data` 包含 `list`、`total`、`page`、`size`。
  - `size` 大于 30 时返回 HTTP 400；缺少时间范围时也返回 HTTP 400。
  - 时间只按日期生效：起始日的 23:59:59 仍包含当天订单，结束日的 00:00:00 也包含当天订单。
- `GET /api/v1/order/item/batch?order_uuid=&page=&size=`：订单商品明细，响应 `data` 包含 `list`、`next`。
- 实测一个拼团：`all` 返回 827 单，正好等于未付款 13、已付款 113、已发货 701 之和；已取消 152 单不在其中。全量翻页 979 单，无重复。974 单满足“订单金额 = 商品实付合计 + 运费”，其余为无商品的已取消订单等特殊情况。
- 发货、改状态、打标签、退款等写接口（`/order/status`、`/order/async-bulk-order-management-task` 等）已经存在，但本工具未接入。
