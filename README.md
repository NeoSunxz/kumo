# kumo

115 云下载自动化:

1. **番号** —— 网页上手动添加, 或订阅 RSS (条目带磁力直接下载, 只有番号则去刮磁力)
2. **115 目录同步** —— 定期列出指定的 115 目录, 已有的番号直接跳过
3. **磁力刮削** —— sukebei.nyaa.si、JavBus; 遇到 Cloudflare 时可走 FlareSolverr
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

## 开发

```bash
uv sync
uv run pytest
uv run python -m kumo      # 默认 0.0.0.0:8115, 数据在 ./data
```
