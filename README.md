# pintuanya-cli

用 Python 批量管理拼团呀商品，支持查询、CSV 导出、上下架、改价、冻结和删除。写入前先生成可预览的计划，执行时核对后台状态，并在每项操作后回查结果。

需要 Python 3.9 或更高版本，仅使用标准库，无须安装依赖。本项目使用拼团呀网页使用的内部接口；接口可能随网页更新而变化，项目与拼团呀官方无关联。

## 快速开始

```bash
git clone https://github.com/oceanjustinlin/pintuanya-cli.git
cd pintuanya-cli
python3 pintuanya.py --help
```

### 设置登录信息

在自己已登录的拼团呀网页中，打开开发者工具的 Network 面板，从商品列表请求中获取 `Cookie` 和 `X-Device-Id`，然后在当前终端设置环境变量。以下命令适用于 Bash 和 Zsh：

```bash
printf 'Cookie: '
IFS= read -r -s PINTUANYA_COOKIE
printf '\n'
export PINTUANYA_COOKIE

printf 'X-Device-Id: '
IFS= read -r -s PINTUANYA_DEVICE_ID
printf '\n'
export PINTUANYA_DEVICE_ID

python3 pintuanya.py check
```

`PINTUANYA_COOKIE` 必填；建议同时设置 `PINTUANYA_DEVICE_ID`，未设置时程序会生成随机值。Cookie 只从环境变量读取，不要将它粘贴到命令参数、Issue、CSV 或 Git 文件中。登录失效后，重新从浏览器获取。

### 选择拼团

```bash
python3 pintuanya.py groups
python3 pintuanya.py --group '拼团UUID' list
```

`--group` 接受 UUID 或完整名称。同名拼团必须使用 UUID。也可以通过 `PINTUANYA_GROUP` 设置默认拼团；`list`、`get`、`export` 和 `plan` 都需要明确的拼团选择。

## 查询与导出

```bash
python3 pintuanya.py --group '拼团UUID' list --query '商品标题'
python3 pintuanya.py --group '拼团UUID' list --status frozen
python3 pintuanya.py --group '拼团UUID' list --format json
python3 pintuanya.py --group '拼团UUID' list --status online --format csv > online.csv
python3 pintuanya.py --group '拼团UUID' get '商品UUID' --query '商品标题'
python3 pintuanya.py --group '拼团UUID' export inventory.csv --query '商品标题'
```

`list` 默认输出表格，`--status` 默认为 `all`：

| 状态 | 含义 |
| --- | --- |
| `all` | 全部商品 |
| `online` | 已上架商品 |
| `offline` | 已下架商品 |
| `frozen` | 任意 SKU 已冻结的商品，包含上架和下架商品 |

`--format json` 输出后台原始商品对象组成的数组，`get` 输出单个原始商品对象。`get` 按 UUID 精确匹配；使用 `--query` 后，目标必须在标题查询范围内。

`list --format csv` 和 `export` 使用相同列，每个 SKU 一行：

```text
goods_uuid,title,is_offline,sku_uuid,sku_title,price,is_freeze,category_uuid,stock,cost_price,sku_number
```

CSV 中 `price` 和 `cost_price` 的单位为人民币元；JSON 保留后台原始分值。后台未返回的新增字段留空，零值保留。`export` 文件使用 UTF-8 BOM 编码；`list --format csv` 向标准输出写入 CSV，不附加说明文字。

## 批量修改

先导出商品和 SKU 标识，再创建 UTF-8 CSV，例如 `changes.csv`：

```csv
action,goods_uuid,sku_uuid,price
publish,待上架商品UUID,,
offline,待下架商品UUID,,
price,待改价商品UUID,SKU_UUID,128.00
lock,待冻结商品UUID,,
unlock,待解冻商品UUID,,
delete,待删除商品UUID,,
```

将示例中的标识替换为实际 UUID，只保留需要执行的行。`price` 针对单个 SKU，金额必须大于零且最多两位小数；其他动作针对整件商品。`lock` 和 `unlock` 会修改全部 SKU 的冻结状态。一个计划内，同一商品允许多个不同 SKU 改价，或一个商品级动作；混合操作需分批执行。

```bash
python3 pintuanya.py --group '拼团UUID' plan changes.csv plan.json
# 查看预览并核对计划文件后执行：
python3 pintuanya.py apply plan.json
```

针对少量商品，可以在生成计划时加上 `--query '商品标题'`。程序仍按 UUID 匹配，并把查询词保存在计划中。

计划包含拼团 UUID、变更前后状态和 SHA-256 校验。执行时使用计划内的拼团；也可再传 `--group` 做一致性检查。执行前会统一核对当前状态与 `before`，每项写入后检查 `after`，发现不一致就停止。

执行时输入 `APPLY 数量` 确认。计划包含删除时还要输入 `DELETE`；`--yes` 只跳过普通确认。删除可能影响未付款订单中的商品，请在执行前确认对应商品和订单影响，并先用少量商品验证操作。

### 删除备份

计划包含删除时，程序会在任何写入之前，将待删商品的当前原始对象保存到脚本目录下的 `backups/<拼团UUID>-<UTC时间戳>.json`。备份写入失败会阻止整批操作。

备份包含版本、拼团 UUID、UTC 创建时间、计划摘要和商品列表。当前尚未提供自动恢复命令。备份和运行日志可能包含经营数据，请妥善保管。

### 中断后续跑

```bash
python3 pintuanya.py apply plan.json --resume
python3 pintuanya.py apply plan.json --resume --yes
```

程序将执行日志保存在计划旁边，例如 `plan.json.log.jsonl`。写入并回查成功后记录 `done`；操作或回查失败时记录 `failed` 并停止。每条记录包含计划摘要、原始操作序号、商品及 SKU 标识、状态、错误和 UTC 时间。

续跑会确认日志属于当前计划，回查已完成项仍符合 `after`，并核对剩余项符合 `before`，然后只执行剩余项。确认数量使用剩余项数；剩余项包含删除时仍需输入 `DELETE`，并会重新备份待删商品。日志已有完成项时，普通 `apply` 会提示改用 `--resume`。

请保留原计划和日志。日志缺失、损坏或状态不符时，程序会停止。`failed` 不代表后台一定未发生变化：写入成功后回查断网也会记录失败。这种情况下应先人工核对后台，再生成新计划。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功 |
| `1` | 参数、校验、文件或普通接口错误 |
| `2` | 登录失效，例如 HTTP 401/403 或明确的业务登录失败 |

## 开发与贡献

```bash
python3 -m unittest test_pintuanya.py
```

测试使用模拟客户端，不访问真实接口。欢迎提交 Issue 和 Pull Request。报告问题时请提供 Python 版本、脱敏后的命令与错误信息，不要附上 Cookie、真实商品数据、执行计划或备份。

新增写动作应继续使用计划预览、执行前状态校验和写入后回查；修改接口适配时请补充离线测试。

## 许可证

[MIT](LICENSE) © 2026 oceanjustinlin
