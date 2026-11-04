# 订单查询、导出与修改

`orders` 和 `orders-export` 只读，用于对账和统计。修改订单状态和标记使用 `order-plan` → `order-apply`，见下文“修改状态与标记”。所有请求都在命令行完成，不打开浏览器。

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

## 修改状态与标记

先写 UTF-8 CSV，再生成计划、核对、执行，与商品批量修改的流程相同：

```csv
action,order,value,tag_type
status,6492,shipped,
tag,6492,已对账,public
untag,6493,已对账,
```

```bash
python3 pintuanya.py --group '拼团UUID' order-plan changes.csv plan.json --from 2026-10-01
python3 pintuanya.py order-apply plan.json
python3 pintuanya.py order-apply plan.json --resume
```

| 列 | 说明 |
| --- | --- |
| `action` | `status` 改状态，`tag` 加标记，`untag` 删除标记 |
| `order` | 网页显示的订单号（`sequence`），或 `order_uuid` |
| `value` | 改状态时填目标状态 `unpaid` / `paid` / `shipped` / `canceled`，与网页“修改订单状态”的四个选项一致；加删标记时填标记文字 |
| `tag_type` | 仅标记操作使用：`public` 普通标记（默认）或 `private` 隐私标记。删除标记时，只有同名标记存在两种类型才需要填写 |

### 生成计划时的检查

`order-plan` 会查找日期范围内所有状态的订单，日期范围默认为近 180 天，更早的订单用 `--from` 扩大范围。以下情况直接拒绝：

- 订单号找不到，或在拼团内不唯一（这时改用 `order_uuid`）；
- 订单已经是目标状态；
- 订单处于售后，或有待处理的退款申请（请在网页处理）；
- 要添加的标记已存在；要删除的标记不存在；
- 标记文字包含换行或控制字符；
- 同一订单出现重复操作。

计划文件已存在时不会覆盖。计划包含 SHA-256 校验，修改后执行会被拒绝。

### 执行

`order-apply` 的执行流程：

1. 输入 `APPLY 数量` 确认。如果计划里有取消订单或恢复已取消订单的操作，还要再输入 `CANCEL`；`--yes` 只跳过第一步确认。
2. 写入前统一核对每一项当前是否仍符合计划中的 `before`，任何一项不符合都停止，不写入任何内容。
3. 逐单调用网页单笔操作使用的同步接口，每项写入后立即回查；回查不一致就停止。

本工具没有使用网页批量操作调用的异步任务接口，因为该接口需要“几分钟”才生效，无法立即回查。

执行日志保存在 `plan.json.log.jsonl`，规则与商品 `apply` 相同：

- 中断后用 `--resume` 续跑。
- 已完成项必须仍然符合 `after`。
- 第一个未完成项如果已经生效（例如写入成功但响应丢失），会记为完成，不重复发送写请求。

修改状态可能触发后台的其他处理，例如库存变化或通知买家，前端代码无法确认具体有哪些。首次使用时，请先用一两个订单验证效果。

## 接口依据（2026-11-03 命令行调研）

依据来自网页前端代码 `umi` 及订单页 chunk 的静态分析，以及只读实测。

- `POST /api/v1/order/batch`：订单列表，请求体字段为 `groupon_uuid`、`page`、`size`、`order_status`、`sort_order`（0 时间升序 / 1 降序 / 2 金额升序 / 3 降序）、`query_field`、`query_value`、`start_time`、`end_time`，以及网页的其他筛选项。响应 `data` 包含 `list`、`total`、`page`、`size`。
  - `size` 大于 30 时返回 HTTP 400；缺少时间范围时也返回 HTTP 400。
  - 时间只按日期生效：起始日的 23:59:59 仍包含当天订单，结束日的 00:00:00 也包含当天订单。
- `GET /api/v1/order/item/batch?order_uuid=&page=&size=`：订单商品明细，响应 `data` 包含 `list`、`next`。
- 实测一个拼团：`all` 返回 827 单，正好等于未付款 13、已付款 113、已发货 701 之和；已取消 152 单不在其中。全量翻页 979 单，无重复。974 单满足“订单金额 = 商品实付合计 + 运费”，其余为无商品的已取消订单等特殊情况。
- 写接口（单笔、同步）：
  - `PUT /order/status`：请求体 `{order_uuid, order_status}`；
  - `POST /order/tag`：请求体 `{order_uuid, type, tag}`，`type` 为 1 普通、2 隐私；
  - `PUT /order/tag/delete`：请求体 `{order_uuid, tag_id}`；
  - `GET /order/tag?order_uuid=`：返回 `[{tag_id, name, type}]`。
- 回查时，在 `/order/batch` 中设置 `query_field=1`（单号）、`query_value` 为订单号、日期为下单当天，分别查询状态 0、100、101，再按 `order_uuid` 匹配。2026-11-04 实测：单号搜索为精确匹配，部分单号命中 0 条；抽样 14 单（含已取消）全部找到且状态正确；拼团内 979 个订单号无重复。
- `tag_id` 是整数，删除时必须按整数提交；转成字符串会返回 HTTP 400。程序会拒绝标记 ID 类型不正确的计划。
- 2026-11-04 线上验收（标记）：在一笔 2024 年已取消、无标记、无备注、无退款的订单上，通过 CLI 添加隐私标记 `CLI测试`，再删除。
  - 添加和删除都回查通过，列表中能看到该标记。
  - 以下情况均按预期被拒绝：重复添加、删除不存在的标记、不带 `--resume` 重复执行、使用旧版计划。
  - 测试结束后，该订单已恢复为无标记，状态未变。
- **修改状态尚未在线上验收**：只完成了离线测试和只读回查验证。首次使用时，请先用一单确认后台没有副作用。
- 批量退款、改备注、改地址等接口已存在，本工具未接入；异步批量任务 `/order/async-bulk-order-management-task` 也未使用。
