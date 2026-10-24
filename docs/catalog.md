# 商品目录功能与命令

## 项目操作约束

所有查询、来源核对、接口调研、状态复核和商品写操作均通过 CLI/命令行完成。未经用户另行明确授权，不操作浏览器，包括只读查看。CLI 缺少所需动作时，先补齐命令和必要测试，再使用；写入仍使用 plan → apply 并由 CLI 回查。登录前先加载本地已有的 .env，不能仅凭环境变量未设置就认定没有 Cookie。

黑胶默认上架规则及逐项验收见 [上架约束与验收](listing-workflow.md)：必须有对应版本图片，标题使用 Artist - Title，默认显式规格、无库存上限、NEW 分组，并验证 NEW 与店铺首页两处置顶。以下通用接口示例不是默认业务规则；首页置顶机制尚未验证，不可将分类排序成功报告为双位置顶完成。

| 功能 | 命令 | 状态 |
| --- | --- | --- |
| 查询拼团、登录和商品 | `groups/check/list/get/export` | 已有 |
| 上下架、改价、冻结、删除 | `plan` + `apply` | 已有 |
| 读取分类 UUID | `categories` | 新增 |
| 上传本地商品图片 | `upload-image IMAGE` | 已实测；回查审核状态 |
| 新建商品（单 SKU / 多规格） | `catalog-plan` + `catalog-apply`，`action=create` | 新增 |
| 在原商品追加规格 | 同上，`action=append-variants` | 支持单维“默认规格”；已实测 |
| 修改分类 | 同上，`action=category` | 新增 |
| 置顶、置底 | 同上，`action=sort` | 新增 |
| 相对上移/下移 | 尚未实现 | 后续扩展 |

## 登录与图片

继续使用环境变量 `PINTUANYA_COOKIE` 和可选的 `PINTUANYA_DEVICE_ID`。macOS 也支持 `auth-save --clipboard` 保存至钥匙串；环境变量未设置时自动读取。临时脚本和剪贴板不是持久登录配置。不要把凭据写入计划或 Git。

```bash
python3 pintuanya.py --group '拼团UUID' categories
python3 pintuanya.py upload-image /absolute/path/red.png
```

图片上传使用网页相同的 MD5 去重、`/file/token`、七牛表单上传及审核流程；只上传 PNG/JPEG/WebP，最大 20 MB。只向存储服务发送限于该文件的 token，不发送拼团呀 Cookie。[七牛表单上传协议](https://developer.qiniu.com/kodo/1272/form-upload)。

也可直接使用公开 HTTPS 图片地址。SKU 封面 URL 有 128 字符限制；新建单 SKU 使用第一张图作为封面。

## 创建计划

输入 UTF-8 JSON 数组，售价单位为人民币元；当前协议适配以 `stock: 0` 编码不限，后台实际语义的验证要求见上架规范，默认单位为“张”。每个 create 元素创建一个商品。新建计划必须使用 `Artist - Title` 标题及非空 variants；只有一个版本也显式填写一个规格。底层构造器保留旧单 SKU 适配，但新建计划入口不再接受无 variants 的创建。

```json
[
  {
    "action": "create",
    "title": "Carly Rae Jepsen - Day and Night",
    "category_uuid": "实际分类UUID",
    "images": ["https://img.pintuanya.com/groupon-goods/实际文件.png"],
    "variants": [
      {"name": "Opaque Apple Red - Vinyl", "price": "315"}
    ]
  }
]
```

```bash
python3 pintuanya.py --group '拼团UUID' catalog-plan specs.json plans/create.json
# 查看计划后执行；已获得用户授权时可加 --yes
python3 pintuanya.py catalog-apply plans/create.json --yes
```

命令校验分类、价格和图片 URL；生成创建计划时对全拼团完整分页查重，Artist 和 Title 分别匹配，忽略大小写及末尾括号日期。同一作品或疑似候选会阻止新建；应核实原商品并追加不同规格，相同规格不重复添加。已提供 append-variants 追加单维默认规格，不得另建商品替代。新计划在首次 POST 前再次全量复核，旧计划应重新生成。创建默认上架、不冻结；创建后读取完整分类分页核对标题、售价、库存、图片和上架状态，避免后台搜索索引延迟漏掉新商品。

创建或修改前将 sending 状态落盘到计划旁的 `.state.json`。中断后可重新运行同一个 catalog-apply：创建结果不明确时只回查，不重新 POST。发现同名、结果不符或商品状态变化时停止，需人工核对。不要删除执行记录后重跑。

## 分类及置顶

创建或更新商品/规格回查通过后，商品必须移入 NEW：若原分类不是 NEW，先另做 category 计划执行并回查，再生成排序计划置顶 NEW。不能因复用旧商品而保留原分类。一个计划内同一已有商品只允许一个动作；创建和随后置顶分开执行。TOP/BOTTOM 是分类内的绝对排序；两件商品依次置顶后，后操作的一件排在更前面。

```json
[
  {
    "action": "sort",
    "goods_uuid": "实际商品UUID",
    "source_category_uuid": "当前分类UUID",
    "direction": "TOP"
  },
  {
    "action": "category",
    "goods_uuid": "另一商品UUID",
    "source_category_uuid": "当前分类UUID",
    "category_uuid": "目标分类UUID"
  }
]
```

执行前校验商品快照；执行后核对目标分类或首尾位置。目录计划与原 CSV 批量计划分别使用 catalog-apply 和 apply，不可混用。

## 验证

```bash
python3 -m unittest test_pintuanya.py test_catalog.py test_http_transport.py test_auth.py
```

离线测试不证明线上接口可用。首次线上使用应保留计划和执行记录，并确认后台回查成功。

## 命令行 TLS 与系统代理

默认使用 Python urllib；当 Python 的证书信任链与系统不同，可选择系统 curl（仍验证 TLS）：

```bash
export PINTUANYA_TRANSPORT=curl
```

若系统代理的虚拟 DNS 地址导致 HTTPS 握手失败，可在核实公开 DNS 后，为本次运行指定真实 IP：

```bash
export PINTUANYA_RESOLVE_IP='经核实的实际IP'
```

只对 `www.pintuanya.com` 生效，保留原域名、SNI 和证书校验；不要把临时 IP 当成长期配置。curl 请求头和 JSON 请求体通过标准输入传递，不将 Cookie 写入命令参数或临时文件。图片存储上传也可使用 curl，不向存储服务发送账号 Cookie。

系统剪贴板读取须检查 `pbpaste` 退出码；受限进程读取失败不能据此认定用户没有复制 Cookie。

排序输入中的 `TOP` / `BOTTOM` 会按网页协议转为接口数字 `3` / `4`。curl 入口禁用默认 `.curlrc` 加载，避免本地配置改变 TLS 校验行为。

## 一个商品的两个规格

在单个 create 对象中使用 variants，避免误建成两个独立商品。顶层 images 是商品相册；每个规格的 images 第一张作为该规格的封面。

```json
[
  {
    "action": "create",
    "title": "Carly Rae Jepsen - Day and Night",
    "category_uuid": "实际分类UUID",
    "images": ["https://example.com/red.png", "https://example.com/green.png"],
    "variants": [
      {"name": "Opaque Apple Red Vinyl", "price": "315", "images": ["https://example.com/red.png"]},
      {"name": "Indie Translucent Forest Green Vinyl", "price": "315", "images": ["https://example.com/green.png"]}
    ]
  }
]
```

规格名称必须非空且唯一。创建后逐个回查规格名称、价格、库存和图片。

## 故障与经验

执行前约定见项目 `AGENTS.md`；本次已验证的故障现象、原因和处理顺序见 [2026-10-23 上架流程复盘](lessons/2026-10-23-listing.md)。通用商品修改/合并尚未封装，不要将多规格创建能力误认为通用合并能力。

## 按顺序回查

`catalog-list` 保留后台返回顺序，完整分页核对后输出前 N 件上架商品：

```bash
python3 pintuanya.py --group 拼团UUID catalog-list --limit 3
python3 pintuanya.py --group 拼团UUID catalog-list --category 分类UUID --limit 3
```

省略 category 时读取全拼团；不能直接把这个管理接口的全局顺序等同于顾客小程序首页，二者须有数据源依据。命令只读，不发送商品写请求。

## 在原商品追加规格

先通过 CLI 核实 Artist 和 Title、原 goods_uuid、分类及全部 SKU，再使用以下输入生成 `catalog-plan` 并执行 `catalog-apply`：

```json
[
  {
    "action": "append-variants",
    "goods_uuid": "原商品UUID",
    "source_category_uuid": "原分类UUID",
    "images": ["https://img.pintuanya.com/groupon-goods/已上传图片.png"],
    "variants": [{"name": "Yellow - 2LP", "price": "318"}]
  }
]
```

只支持原商品已有单维“默认规格”且属性列表与 SKU 一致的情况；不支持跨商品合并。旧单 SKU 可在明确提供 existing_variant_name 时转为显式规格并追加：只有一个原 SKU 且原来没有规格属性时允许；保留原 SKU UUID、价格、库存、成本、冻结与销售记录，只增加该 SKU 的规格名称。缺少明确原版本名时停止，不自动猜测。追加请求保留原标题、当前分类及购买设置；默认以第一个新增规格封面作为商品主图和相册首图，其余本次新图与历史图片保留在相册并去重，旧SKU封面保持原样；追加回查后必须另做 category 计划移入 NEW，再置顶。旧 SKU 保留 UUID、价格、库存、成本及冻结配置；后端销售记录不作为请求字段提交，执行后逐字段核对旧 SKU 记录是否保持。

同名规格（忽略 Unicode、大小写、首尾和重复空格）拒绝追加；颜色或签名的语义同义仍需人工核实。执行前核对完整快照，销售状态变化也会阻止写入。PUT 前保存 sending；任何已发送但结果不确定的追加只回查，不自动重发。新增 SKU 按规格身份回查并验证新的 UUID；原 SKU 必须保持原记录。商品主图必须等于计划中的新图，相册首图及其余图片列表也必须与计划一致，否则不记为完成。排序另做计划。

追加规格及旧单 SKU 转显式规格已通过 CLI 实测；具体商品、售价、库存和销售记录保存在本地忽略的 plans/ 验收记录中，不纳入公共文档。
