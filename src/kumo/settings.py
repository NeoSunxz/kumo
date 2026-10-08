"""运行配置: 整体以 JSON 存在数据库里, 网页上修改后立即生效 (下一轮调度读取新值)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class InputFeed(BaseModel):
    """订阅的输入 RSS: 条目带磁力时直接下载, 只有番号时去刮磁力."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class CrawlForum(BaseModel):
    """一个爬取目标: 论坛板块, 可限定分类; 新帖默认只入库, ``auto_download`` 打开后才自动提交下载."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fid: int = Field(ge=1)
    typeid: int | None = Field(default=None, ge=1)
    """板块内的分类 (Discuz ``typeid``); 为空爬整个板块."""
    auto_download: bool = False
    types: list[str] = Field(default_factory=list)
    """只自动下载这些分类 (如「有码高清」); 为空不限."""
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class SukebeiRule(BaseModel):
    """一条 sukebei 爬取规则: 按搜索条件定时读取 RSS, 新磁力进入磁力池; ``auto_download`` 打开后符合条件的直接提交."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str
    query: str = ""
    """sukebei 的搜索词, 支持站点自身的语法 (如 ``"中文字幕" | 中字``)."""
    user: str = ""
    """只看这个上传者."""
    category: str = "2_2"
    """分类: 2_2 Real Life - Videos, 2_0 Real Life, 1_0 Art, 0_0 全部."""
    trusted_only: bool = False
    min_seeders: int = Field(default=0, ge=0)
    auto_download: bool = False
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # 115 开放平台
    app_id: str = ""
    download_path: str = ""
    """云下载保存目录 (115 内路径, 如 ``/云下载``); 为空时不提交任务."""
    sync_paths: list[str] = Field(default_factory=list)
    """用于判断「已拥有」的 115 目录."""
    sync_interval_hours: float = Field(default=6.0, ge=0.5)

    # 磁力刮削; sehuatang 指本地已爬取的资源库, 不发网络请求
    sites: list[str] = Field(default_factory=lambda: ["sukebei", "javbus", "javdb", "sehuatang"])
    scrape_proxy: str = ""
    """刮削磁力站点用的代理, 如 ``http://user:pass@host:port``; 为空则直连."""
    javdb_cookie: str = ""
    """登录 JavDB 后从浏览器复制的 Cookie (至少含 ``_jdb_session``); 标签订阅需要."""
    flaresolverr_url: str = ""
    """遇到 Cloudflare 验证时改用 FlareSolverr, 如 ``http://192.168.100.110:8191``."""
    no_magnet_retry_hours: float = Field(default=24.0, ge=1)

    # 磁力选择
    size_min_gb: float = Field(default=1.0, ge=0)
    size_max_gb: float = Field(default=12.0, ge=0)
    prefer_subtitle: bool = True
    require_subtitle: bool = False
    allow_4k: bool = False
    avoid_h265: bool = True
    exclude_keywords: list[str] = Field(default_factory=list)

    # 提交与跟踪
    submit_batch: int = Field(default=5, ge=1, le=50)
    """每轮最多提交的任务数."""
    task_poll_minutes: float = Field(default=5.0, ge=1)

    # 论坛爬取 (sehuatang)
    crawl_enabled: bool = False
    crawl_base_url: str = "https://www.sehuatang.net"
    """站点常换域名, 换了在这里改."""
    crawl_forums: list[CrawlForum] = Field(default_factory=lambda: [CrawlForum(fid=103), CrawlForum(fid=36)])
    crawl_pages: int = Field(default=2, ge=1, le=20)
    """每轮每个板块最多翻几页; 遇到已爬过的帖子即停止翻页."""
    crawl_interval_minutes: float = Field(default=60.0, ge=10)
    crawl_request_delay: float = Field(default=1.5, ge=0.5, le=30)
    """相邻两次请求的间隔 (秒)."""

    # sukebei 爬取: 按规则读取 RSS, 新磁力进入磁力池
    sukebei_rules: list[SukebeiRule] = Field(default_factory=list)
    sukebei_interval_minutes: float = Field(default=60.0, ge=10)

    # Amane: 按番号读取海报、封面、演员等作为页面内容; 只读, 不改 Amane
    amane_url: str = ""
    """Amane 的地址, 如 ``http://192.168.100.110:8000``; 为空不接入."""
    amane_token: str = ""
    """Amane 的 API token (Amane 未设 token 时留空)."""
    amane_interval_minutes: float = Field(default=30.0, ge=5)
    amane_refresh_days: float = Field(default=7.0, ge=1)
    """已查过的番号多久后再查一次 (Amane 里可能刚刮削完)."""

    # 订阅 (按女优 / 系列 / 片商等获取新作)
    subs_interval_hours: float = Field(default=12.0, ge=1)
    subs_pages: int = Field(default=1, ge=1, le=10)
    """之后每次检查最多翻几页; 遇到已见过的作品即停止."""
    subs_initial_pages: int = Field(default=3, ge=1, le=30)
    """新订阅第一次检查时回填的页数 (每页 30 部)."""

    # 输入 / 输出 RSS
    input_feeds: list[InputFeed] = Field(default_factory=list)
    feed_interval_minutes: float = Field(default=30.0, ge=5)
    rss_statuses: list[str] = Field(default_factory=lambda: ["submitted", "downloading", "done"])
    """出现在输出 RSS 里的状态."""


class Env(BaseModel):
    """进程级配置, 来自环境变量; 运行中不变."""

    data_dir: Path
    password: str
    """为空时网页不设密码 (只应在局域网内使用)."""

    @classmethod
    def load(cls) -> Env:
        return cls(
            data_dir=Path(os.environ.get("KUMO_DATA_DIR", "data")),
            password=os.environ.get("KUMO_PASSWORD", ""),
        )
