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


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # 115 开放平台
    app_id: str = ""
    download_path: str = ""
    """云下载保存目录 (115 内路径, 如 ``/云下载``); 为空时不提交任务."""
    sync_paths: list[str] = Field(default_factory=list)
    """用于判断「已拥有」的 115 目录."""
    sync_interval_hours: float = Field(default=6.0, ge=0.5)

    # 磁力刮削
    sites: list[str] = Field(default_factory=lambda: ["sukebei", "javbus"])
    scrape_proxy: str = ""
    """刮削磁力站点用的代理, 如 ``http://user:pass@host:port``; 为空则直连."""
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
