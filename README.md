# kumo

115 云下载自动化. 找片有两条路径, 汇合到同一个下载队列:

| 路径 | 用途 | 流程 |
|------|------|------|
| **片源** (女优 / 作品 / 订阅) | 找有什么片子 | 订阅资料站 → 挑选作品 → 按番号搜磁力 → 选择磁力 → 115 |
| **磁力** (磁力池) | 找优质磁力 | 论坛 / sukebei 按规则爬取 → 识别番号 → 确认影片 → 115 |

两边按番号共享数据: 磁力池同时是搜磁力的来源; 作品页显示磁力池里已有几条磁力, 磁力池显示作品库与 Amane 里的作品信息;
两边都能看到 115 目录里是否已有、下载队列里的状态.

1. **片源** —— 订阅 JavBus / JavDB 的女优、系列、片商、标签, 新作进入「作品」; 勾选后加入下载队列, 也可以手动添加番号或订阅输入 RSS
2. **磁力** —— 定时爬取 sehuatang 指定板块与 sukebei 规则, 新磁力进入磁力池; 识别出番号后在「确认影片」里按番号合成卡片,
   默认选中按磁力选择规则得分最高的一条. 板块与规则可单独开「自动」, 符合条件的跳过确认直接提交
3. **磁力刮削** —— 按番号搜 sukebei.nyaa.si、JavBus、JavDB 与本地磁力池; 遇到 Cloudflare 时可走 FlareSolverr
4. **磁力选择** —— 番号匹配、体积范围、排除词等硬性条件过滤后按偏好打分 (中文字幕、1080p、避开 H265、做种数); 没有达标的等人工选择
5. **115** —— 只用 115 开放平台的官方接口 (扫码授权, 不用 Cookie) 提交云下载、跟踪进度, 定期同步目录, 已有的番号跳过
6. **Amane 内容填充** —— 按番号读取 Amane 的标题、海报、封面、演员, 按名字读取演员头像与资料, 填充各页面 (只读, 不改 Amane)
7. **输出 RSS** —— `/rss.xml` 列出已提交的番号, 供 Amane「远程发现」订阅后自动刮削元数据

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

### sukebei 爬取规则

设置 → 磁力 → sukebei, 每行一条: `名称 | 搜索词 | 选项`. 选项用空格分隔, 都可省略:
`自动` (符合磁力选择规则的直接提交)、`可信` (只要可信上传者)、`做种>=5`、`上传者:名字`、`分类:2_2`、`+包含词`、`-排除词`.

```
中字 | 中文字幕 | 可信 做种>=3 -4K
某上传者 |  | 上传者:someone 自动
```

### 订阅女优 / 系列 / 片商的新作

在「片源 → 订阅」粘贴资料站页面的地址:

- JavBus: 女优、系列、片商、发行商、导演、类别 (无码用 `/uncensored/` 开头的地址), 如 `https://www.javbus.com/star/1248`
- JavDB: 演员、系列、片商、发行商、导演、番号前缀, 如 `https://javdb.com/actors/B85e9`、`https://javdb.com/video_codes/MNGS`;
  JavDB 列表含已公布未发行的作品, 新作比 JavBus 出现得早
- JavDB 标签 (可组合): 在 JavDB 标签页勾选标签后复制地址, 如 `https://javdb.com/tags?c2=67&c4=57`;
  标签页需要登录, 先在设置里填 JavDB Cookie

同一番号只记一次, 同一女优在两个站都订阅也不会重复.
kumo 定期检查这些页面, 新作进入「作品」列表; 在那里勾选后「去搜磁力」, 才加入下载队列.
订阅可单独打开「新作自动搜磁力」, 新作直接进入下载队列.

`/rss/releases.xml` 输出新作供 Amane 刮削元数据, 参数: `sub` (订阅编号, 逗号分隔), `kind` (star / series / studio / label / director / genre / code),
`q`, `days` (只要最近 N 天发行的), `limit`. 订阅页面列出了每个订阅的 RSS 地址.

### 按板块 / 分类订阅论坛资源

`/rss/sehuatang.xml` 输出已爬取的论坛资源, 用查询参数组合筛选, 每组参数可在 Amane 里单独订阅:

| 参数 | 说明 |
|------|------|
| `fid` | 板块编号, 多个用逗号分隔 (103 高清中文字幕, 36 亚洲无码原创, 37 亚洲有码原创) |
| `typeid` | 板块内分类编号 (如 36 板块的 369 HEYZO, 103 板块的 480 有码高清) |
| `subtitle=1` | 只要字幕资源 |
| `q` | 标题关键词 |
| `numbered=0` | 也输出识别不出番号的帖子 (默认不输出) |
| `limit` | 条数, 默认 100, 最多 500 |

例: `http://<NAS>:8115/rss/sehuatang.xml?fid=36&typeid=369` 只输出 HEYZO. 「磁力池 → 全部磁力」页面会显示当前筛选对应的地址.
要输出某个分类, 先在设置的爬取目标里加上它 (如 `36:369`), 或爬整个板块.

## 开发

```bash
uv sync
uv run pytest
uv run python -m kumo      # 默认 0.0.0.0:8115, 数据在 ./data
```
