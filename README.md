# kumo

115 云下载自动化:

界面分两块: **115** (下载队列、目录同步、授权) 与 **爬取** (新作、订阅、论坛).

1. **番号** —— 网页上手动添加, 从「新作」里勾选, 或订阅 RSS (条目带磁力直接下载, 只有番号则去刮磁力)
2. **115 目录同步** —— 定期列出指定的 115 目录, 已有的番号直接跳过
3. **磁力刮削** —— sukebei.nyaa.si、JavBus、JavDB, 以及本地已爬取的论坛资源; 遇到 Cloudflare 时可走 FlareSolverr
   - **论坛爬取** —— 定时爬取 sehuatang 指定板块的新帖 (磁力、大小、分类) 入库; 每个板块可单独开启「新帖自动下载」并按分类 / 关键词过滤
4. **磁力选择** —— 番号匹配、体积范围、排除词等硬性条件过滤后按偏好打分 (中文字幕、1080p、避开 H265、做种数); 没有达标的等人工选择
5. **115 云下载** —— 只用 115 开放平台的官方接口 (扫码授权, 不用 Cookie), 跟踪任务进度
6. **输出 RSS** —— `/rss.xml` 列出已提交的番号, 供 Amane「远程发现」订阅后自动刮削元数据

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

## 接入 Amane

在 Amane「远程发现」里添加订阅 `http://<NAS>:8115/rss.xml` (设了密码则加 `?key=<密码>`), 打开自动入队.
条目标题就是番号, Amane 会按番号刮削; 文件下载完成后由 okstrm 生成 strm, Amane 的目录监控再关联到文件.

### 订阅女优 / 系列 / 片商的新作

在「爬取 → 订阅」粘贴资料站页面的地址:

- JavBus: 女优、系列、片商、发行商、导演、类别 (无码用 `/uncensored/` 开头的地址), 如 `https://www.javbus.com/star/1248`
- JavDB: 演员、系列、片商、发行商、导演、番号前缀, 如 `https://javdb.com/actors/B85e9`、`https://javdb.com/video_codes/MNGS`;
  JavDB 列表含已公布未发行的作品, 新作比 JavBus 出现得早

同一番号只记一次, 同一女优在两个站都订阅也不会重复.
kumo 定期检查这些页面, 新作进入「新作」列表; 在那里勾选后「去搜磁力」, 才加入下载队列.
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

例: `http://<NAS>:8115/rss/sehuatang.xml?fid=36&typeid=369` 只输出 HEYZO. 「爬取」页面会显示当前筛选对应的地址.
要输出某个分类, 先在设置的爬取目标里加上它 (如 `36:369`), 或爬整个板块.

## 开发

```bash
uv sync
uv run pytest
uv run python -m kumo      # 默认 0.0.0.0:8115, 数据在 ./data
```
