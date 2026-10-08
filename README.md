# kumo

115 云下载自动化. 两种订阅在「挑选作品」汇合, 之后走同一条路:

```
片源订阅 (女优 / 系列 / 片商 / 标签) ─┐
                                      ├→ 识别作品 → 挑选作品 → 磁力选择 → 115 云下载
磁力订阅 (98堂板块 / sukebei / RSS) ──┘
```

- **片源订阅** —— 订阅 JavBus / JavDB 的女优、系列、片商、标签, 用来发现有什么作品
- **磁力订阅** —— 按条件爬取 98堂 (sehuatang) 板块、sukebei、RSS 的优质磁力进入磁力池, 识别番号与标签 (中字、破解、无码、欧美、FC2…) 和片头 `[...]`;
  每条订阅有自己的入池条件, 可单独启用、开「自动」(跳过挑选直接下载)、立即爬取, 或「预览」第一页逐条看是否入池与原因
- **挑选作品** —— 两边的作品按番号合并, 按来源、订阅、磁力标签、时间筛选; 勾选后加入下载队列, 不想要的忽略
- **磁力选择** —— 磁力池与各站 (sukebei、JavBus、JavDB) 按番号搜到的磁力, 按硬性条件 (番号、体积、排除词) 过滤后打分
  (中字、清晰度、避开 H265、做种数), 自动选最高分; 没有达标的等人工选择. 遇到 Cloudflare 可走 FlareSolverr
- **115** —— 只用 115 开放平台的官方接口 (扫码授权, 不用 Cookie) 提交云下载、跟踪进度, 定期同步目录, 已有的番号跳过
- **Amane 内容填充** —— 按番号读取 Amane 的标题、海报、封面、演员, 按名字读取演员头像与资料 (只读, 不改 Amane);
  98堂帖子的图片也会显示
- **输出 RSS** —— `/rss.xml` 列出已提交的番号, 供 Amane「远程发现」订阅后自动刮削元数据

## 部署

```yaml
services:
  kumo:
    build: /path/to/kumo        # 或使用自己构建好的镜像
    container_name: kumo
    restart: unless-stopped
    ports:
      - "8115:8115"
    volumes:
      - ./data:/data
    environment:
      - TZ=Asia/Shanghai
      # - KUMO_PASSWORD=xxx     # 设置后网页需要登录 (任意用户名), RSS 用 /rss.xml?key=xxx
```

启动后打开 `http://<NAS>:8115`:

1. **设置**: 填 115 开放平台 AppID、云下载保存目录、同步目录
2. **115 授权**: 生成二维码, 用 115 App 扫码确认
3. **番号**: 添加番号, 之后全自动

没有 AppID 时可以把 AppID 填成 `fake`, 用内存模拟的 115 走通整个流程 (不会真正下载).

### 热加载 (改代码不用重新构建镜像)

把源码挂载进容器并设 `KUMO_RELOAD=1`: `.py` 改动后 kumo 自动重启, 模板改动直接生效 (不重启).
只有改了 `pyproject.toml` 的依赖才需要重新构建. 重启会中断正在运行的任务 (爬取、同步等), 下一轮调度会重新执行.

```yaml
    volumes:
      - ./data:/data
      - /path/to/kumo/src:/app/src:ro
    environment:
      - KUMO_RELOAD=1
```

## 接入 Amane

在 Amane「远程发现」里添加订阅 `http://<NAS>:8115/rss.xml` (设了密码则加 `?key=<密码>`), 打开自动入队.
条目标题就是番号, Amane 会按番号刮削; 文件下载完成后由 okstrm 生成 strm, Amane 的目录监控再关联到文件.

### 读取 Amane 的海报与演员信息

在设置的「Amane」里填 Amane 地址与 API token (Amane 未设 token 时留空). kumo 定时按番号查 Amane 的 `/api/metadata`,
按订阅的女优名查 `/api/actors`, 结果缓存在 kumo 自己的库里 (已查过的番号默认 7 天后重查); 图片经 kumo 带 token 去 Amane 取并缓存.
资料站封面优先, 没有时用 Amane 的; 女优头像同理.

Amane 里还没有的番号, 让 Amane「远程发现」订阅 kumo 的 RSS (`/rss.xml`、`/rss/releases.xml`、`/rss/sehuatang.xml`) 去刮削, 之后 kumo 自动补上.

### 磁力订阅与入池条件

在「磁力订阅」页添加98堂 (sehuatang) 板块、sukebei 搜索或 RSS, 每条可设入池条件 (只看标题):

- 必须有 / 不要的标签: 中字、破解、无码、欧美、FC2、4K…
- 片头 `[...]` 含某些字 (如「破解」匹配 `[新模型无码破解]`)、98堂分类、标题包含 / 排除词、只要识别出番号的

例: 亚洲无码板块 (36) 只要破解 → 「必须有: 破解」. 98堂按列表标题先判断, 不符合的帖子不打开.
改了条件先点「预览」看第一页哪些会入池、为什么不入, 再保存.
98堂地址、翻页数、各类爬取间隔也在这一页.

### 订阅女优 / 系列 / 片商的新作

在「片源订阅」粘贴资料站页面的地址:

- JavBus: 女优、系列、片商、发行商、导演、类别 (无码用 `/uncensored/` 开头的地址), 如 `https://www.javbus.com/star/1248`
- JavDB: 演员、系列、片商、发行商、导演、番号前缀, 如 `https://javdb.com/actors/B85e9`、`https://javdb.com/video_codes/MNGS`;
  JavDB 列表含已公布未发行的作品, 新作比 JavBus 出现得早
- JavDB 标签 (可组合): 在 JavDB 标签页勾选标签后复制地址, 如 `https://javdb.com/tags?c2=67&c4=57`;
  标签页需要登录, 先在设置里填 JavDB Cookie

同一番号只记一次, 同一女优在两个站都订阅也不会重复.
kumo 定期检查这些页面, 新作进入「挑选作品」; 在那里勾选后才加入下载队列.
订阅可单独打开「新作自动搜磁力」, 新作直接进入下载队列.

`/rss/releases.xml` 输出新作供 Amane 刮削元数据, 参数: `sub` (订阅编号, 逗号分隔), `kind` (star / series / studio / label / director / genre / code),
`q`, `days` (只要最近 N 天发行的), `limit`. 订阅页面列出了每个订阅的 RSS 地址.

### 按板块 / 分类订阅 98堂资源

`/rss/sehuatang.xml` 输出已爬取的 98堂资源, 用查询参数组合筛选, 每组参数可在 Amane 里单独订阅:

| 参数 | 说明 |
|------|------|
| `fid` | 板块编号, 多个用逗号分隔 (103 高清中文字幕, 36 亚洲无码原创, 37 亚洲有码原创) |
| `typeid` | 板块内分类编号 (如 36 板块的 369 HEYZO, 103 板块的 480 有码高清) |
| `subtitle=1` | 只要字幕资源 |
| `q` | 标题关键词 |
| `numbered=0` | 也输出识别不出番号的帖子 (默认不输出) |
| `limit` | 条数, 默认 100, 最多 500 |

例: `http://<NAS>:8115/rss/sehuatang.xml?fid=36&typeid=369` 只输出 HEYZO. 「全部磁力」页面会显示当前筛选对应的地址.
要输出某个分类, 先在「磁力订阅」里添加它 (板块 36、分类 369), 或爬整个板块.

## 开发

```bash
uv sync
uv run pytest
uv run python -m kumo      # 默认 0.0.0.0:8115, 数据在 ./data
```
