# 订单查询与导出方案

状态：设计草案，等待用户确认。订单接口尚未核实：本次会话所在云端环境的网络策略拒绝访问 `www.pintuanya.com`（代理对 CONNECT 返回 403），无法登录或读取前端。下文接口细节均为待核实假设，阶段 0 完成后再定稿字段和参数。本文不含登录凭据和订单数据。

## 目标与范围

在 CLI 中只读查询指定拼团的订单，并导出为 CSV，用于对账、统计和备货。

- 本期只做读取：列表、单个订单详情、CSV 导出。
- 发货、退款、改备注等写操作不在本期范围。以后如需实现，沿用 `plan` → `apply`：先生成计划，执行前核对状态，写入后回查，并记录可续跑的日志。
- 与商品命令一样必须明确选择拼团（`--group` 或 `PINTUANYA_GROUP`）。
- 遵守项目约束：接口调研和核对只通过命令行完成，不操作浏览器。

## 阶段 0：接口调研（仅命令行，只读）

1. 用 curl 下载网页后台的公开 HTML 和 JS bundle，搜索 `order`、`订单`、`refund`、`express` 等关键词，找到订单列表和详情接口的路径、HTTP 方法和查询参数，例如 `groupon_uuid`、`page`、`size`、状态、时间范围和关键词。
2. 加载已有 `.env` 后，用 `size=1~5` 发几次只读请求，记录以下内容：
   - 响应结构（`data.list`、`data.total` 或其他）和每页上限；
   - 状态枚举值；
   - 时间字段格式（秒、毫秒或字符串）和时区；
   - 金额单位（预计和商品一样为分）；
   - 订单与商品行的嵌套方式；
   - 页码能翻多深，时间范围有无限制。
3. 写入文档的只有字段名、类型和枚举值。手机号、姓名、地址等个人信息的样例一律脱敏，原始响应不入库。
4. 确认网页请求的 `Referer` 是否必须是订单页；如果需要，`Client.request` 增加可选 `referer` 参数，默认值保持不变。

## 命令设计

```bash
python3 pintuanya.py --group '拼团UUID' orders
python3 pintuanya.py --group '拼团UUID' orders --from 2026-10-01 --to 2026-10-31 --status paid
python3 pintuanya.py --group '拼团UUID' orders --query '手机号尾号或商品标题' --format json
python3 pintuanya.py --group '拼团UUID' order-get '订单号'
python3 pintuanya.py --group '拼团UUID' orders-export orders-2026-10.csv --from 2026-10-01 --to 2026-10-31
```

| 参数 | 说明 |
| --- | --- |
| `--from` / `--to` | 日期 `YYYY-MM-DD` 或带时区的 ISO 时间，不带时区时按 Asia/Shanghai 解释，`--to` 当天包含在内。省略 `--to` 时，固定为命令开始时刻，保证一次翻页期间时间范围不变。 |
| `--status` | 使用 CLI 自己的稳定名称（如 `all`、`unpaid`、`paid`、`shipped`、`completed`、`refunding`、`refunded`、`closed`），映射到后台枚举。具体取值以阶段 0 为准，默认 `all`。 |
| `--query` | 透传后台的关键词搜索（如果有）。和商品一样，可能漏查，只用于缩小范围；精确查单用 `order-get`。 |
| `--format` | `orders` 支持 `table`（默认，个人信息脱敏）、`json`（后台原始对象数组）和 `csv`（输出到 stdout）。 |
| `--limit N` | 仅 `table` 模式下，翻完全部页并核对后只显示前 N 条，默认 50。 |
| `--pii mask\|full` | 导出时个人信息的处理方式，见下文“个人信息”。 |

`table` 列：订单号、下单时间、状态、买家（脱敏）、件数、实付金额（元）。

## 翻页与一致性

沿用 `inventory` / `category_goods` 的严格规则，宁可报错也不导出不完整的数据：

- 每页大小取后台允许的最大值，例如 100，页间等 1 秒；最多翻 1000 页，超过就报错停止。
- 以下情况都报错停止：`list` 或 `total` 格式变化，订单缺少唯一 ID，订单 ID 重复，未达到 `total` 时出现空页。
- 新订单会让按页偏移的结果错位。对策：固定 `--to` 上界；如果后台支持，按下单时间稳定排序；翻页结束后再请求一次第一页，`total` 变化就报错，提示缩小时间范围重试，不静默去重。
- 如果后台限制翻页深度或时间跨度，就按天（必要时按小时）拆分时间窗口依次查询，再合并、去重并核对每个窗口的 `total`。
- 列表接口如果不返回商品行或收货信息，导出时逐单调用详情接口，限速并显示进度；任何一单失败都终止，不写出不完整的文件。

## CSV 导出

每个订单商品行输出一行（订单 × SKU），订单级字段在每行重复，方便透视统计。列表为暂定，阶段 0 后按实际字段确定：

```text
order_id,order_no,created_at,paid_at,status,status_code,buyer_nickname,receiver_name,receiver_phone,receiver_address,goods_uuid,goods_title,sku_uuid,sku_title,quantity,unit_price,item_amount,order_amount,freight,discount,paid_amount,refund_status,buyer_remark,seller_remark,express_company,express_no
```

- 金额单位为人民币元，保留两位小数；JSON 保留后台原始分值。规则与 `money()` 一致。
- 时间统一输出带 `+08:00` 的 ISO 8601。
- 同时保留原始状态码 `status_code`；遇到未知状态显示 `未知(代码)`，不报错。
- 后台缺少的字段留空，零值保留。
- 文件使用 UTF-8 BOM 编码，Excel 可直接打开。
- 写出方式：先写同目录临时文件，完成后原子改名；文件权限 0600。目标文件已存在时拒绝写入，除非加 `--force`。导出数量与核对后的 `total` 不一致时不生成文件。
- 完成后打印订单数、商品行数、时间范围和金额合计，方便与后台页面核对。

## 个人信息

订单含收货人姓名、手机号和地址，泄露风险比商品数据高：

- `table` 输出始终脱敏：手机号显示 `138****1234`，姓名只保留首字，地址只保留省市。
- `--pii` 默认值待用户确认（见“待确认问题”）。建议导出默认 `mask`，发货等场景需要完整信息时显式传 `--pii full`。`full` 会在 stderr 提醒文件含个人信息、需妥善保管。
- `.gitignore` 已忽略根目录 `*.csv`、`*.json` 和 `/exports/`。文档建议把导出文件放在 `exports/`。
- 错误信息和日志不输出订单内容，与现有“响应内容已隐藏”的做法一致。

## 代码结构

- 新模块 `orders.py`，与 `catalog.py` 并列：
  - `fetch_orders(client, group, filters)`：翻页、核对、拆分时间窗口；
  - `fetch_order(client, group, order_id)`：查询单个订单；
  - `flatten(order)`：把一个订单展开为 CSV 行；
  - `write_orders_csv(rows, path, force)`：写出 CSV；
  - `mask(...)`：个人信息脱敏；
  - `parse_range(from, to, now)`：解析时间范围；
  - 状态映射表。
- `pintuanya.py` 只负责注册子命令和分派，复用 `Client.request`、`resolve_group`、登录失效时退出码 2、`Set-Cookie` 持久化等现有机制。
- 只用标准库，时区用 `zoneinfo`（Python 3.9 起可用），并在 Python 3.9 上测试。

## 测试与验收

离线测试 `test_orders.py`，使用假 Client，覆盖：

- 多页拼接；
- `total` 变化、空页提前结束、订单 ID 重复、缺少 ID、翻页超上限；
- 时间窗口拆分与合并；
- 时间范围解析（日期、时区、`--to` 包含当天）；
- 状态映射及未知状态；
- 金额换算、零值保留与缺失字段留空；
- 一单多商品行展开；
- 脱敏规则；
- CSV 列顺序和 BOM；
- 文件 0600 权限、文件已存在时拒绝、失败时不留下半成品文件；
- 登录失效时退出码为 2。

运行全部现有测试。

线上验收只做只读请求：选一个订单量少的拼团和一个较短的时间范围，核对订单数、金额合计和几笔抽样订单的字段。CLI 不操作浏览器，需要用户自己在网页后台目视核对。验收记录写进本文，不包含个人信息。

## 待确认问题

1. 导出主要用途是什么（对账/统计、发货、备货汇总）？这决定 `--pii` 的默认值，以及是否另外提供按商品汇总件数的视图。
2. 导出时个人信息默认脱敏，还是默认完整？
3. 大概订单量和常用时间范围？这决定是否一开始就实现按天拆分时间窗口。
4. 后续是否需要发货、退款等写操作？需要的话，阶段 0 调研时一并记录这些接口，但本期不实现。
