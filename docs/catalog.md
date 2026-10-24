# 商品目录功能与命令

| 功能 | 命令 | 状态 |
| --- | --- | --- |
| 查询拼团、登录和商品 | `groups/check/list/get/export` | 已有 |
| 上下架、改价、冻结、删除 | `plan` + `apply` | 已有 |
| 读取分类 UUID | `categories` | 新增 |
| 上传本地商品图片 | `upload-image IMAGE` | 已实测；回查审核状态 |
| 新建商品（单 SKU / 多规格） | `catalog-plan` + `catalog-apply`，`action=create` | 新增 |
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

输入 UTF-8 JSON 数组，售价单位为人民币元；`stock: 0` 表示不限，默认单位为“张”。每个 create 元素创建一个商品；不含 variants 时为单 SKU，含 variants 时为同一商品的多个规格。

```json
[
  {
    "action": "create",
    "title": "Carly Rae Jepsen - Day and Night (Opaque Apple Red Vinyl)",
    "category_uuid": "实际分类UUID",
    "price": "315",
    "stock": 0,
    "images": ["https://img.pintuanya.com/groupon-goods/实际文件.png"]
  }
]
```

```bash
python3 pintuanya.py --group '拼团UUID' catalog-plan specs.json plans/create.json
# 查看计划后执行；已获得用户授权时可加 --yes
python3 pintuanya.py catalog-apply plans/create.json --yes
```

命令校验分类、价格、图片 URL 和分类内同名商品，创建默认上架、不冻结。读取完整分类分页核对标题、售价、库存、图片和上架状态，避免后台搜索索引延迟漏掉新商品。

创建或修改前将 sending 状态落盘到计划旁的 `.state.json`。中断后可重新运行同一个 catalog-apply：创建结果不明确时只回查，不重新 POST。发现同名、结果不符或商品状态变化时停止，需人工核对。不要删除执行记录后重跑。

## 分类及置顶

创建完成取得商品 UUID 后，另做分类或排序计划。一个计划内同一已有商品只允许一个动作；创建和随后置顶分开执行。TOP/BOTTOM 是分类内的绝对排序；两件商品依次置顶后，后操作的一件排在更前面。

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
