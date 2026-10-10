# 贡献指南

## 提交规范

遵循 [Conventional Commits 1.0](https://www.conventionalcommits.org/zh-hans/v1.0.0/), type 用英文, 描述用中文. 历史提交 (纯中文一句话) 不改写, 新提交按本规范.

### 格式

```
<type>(<scope>): <subject>

<body>

<footer>
```

- 正文 (可选): 说明为什么改, 有什么影响; 每行建议不超过 72 字符
- 脚注 (可选): `BREAKING CHANGE: ...`, `Refs #12`, `Co-Authored-By: ...` 等 trailer
- 首行与正文, 正文与脚注之间各空一行

### type

| type | 说明 |
|---|---|
| feat | 新功能 |
| fix | 修复缺陷 |
| refactor | 重构, 不改变外部行为 |
| perf | 性能优化 |
| style | 格式调整, 不影响逻辑 (空白, 缩进等) |
| test | 增加或修改测试 |
| docs | 文档变更 |
| build | 构建系统与依赖 (pyproject, uv.lock) |
| ci | 持续集成配置 |
| chore | 其它不改源码和测试的杂项 |
| revert | 回退之前的提交 |

### scope (可选)

小写英文 / 数字 / 连字符. 建议值 (对应 `src/kumo/` 模块):

| scope | 范围 |
|---|---|
| pan115 | 115 开放平台客户端 (授权, 云下载, 目录同步) |
| amane | Amane 集成 (镜像, 海报, 刮削请求) |
| sehuatang | 98堂板块爬虫 |
| magnets | sukebei / JavBus / JavDB 等磁力站点刮削 |
| feeds | RSS 输入 |
| numbers | 番号与标签识别 |
| select | 磁力过滤与打分 |
| subscriptions | 订阅与入池条件 |
| web | Web 界面与路由 (含 templates) |
| db | SQLite 与数据模型 (含 `models.py`) |
| images | 封面与头像的代理和磁盘缓存 |
| service | 调度与业务流程 (任务循环, 番号生命周期) |
| settings | 设置项 |
| docker | Dockerfile 与部署 |

跨多个模块或不属于以上范围时省略 scope.

### subject

- 中文, 直接说做了什么, 不超过 50 个字符 (钩子硬限制: 首行总长 72 字符, 中文算 1)
- 结尾不加句号
- 破坏性变更在冒号前加 `!`, 并在脚注写 `BREAKING CHANGE: ...`
- 一个提交只做一件事

### 示例

正例:

```
fix(subscriptions): 被入池条件挡掉的条目一直被跳过
feat(magnets): sukebei 磁力入池时取详情页里的封面图
feat(web)!: 去掉 RSS 输出

BREAKING CHANGE: /rss 路径不再可用, 改为直接让 Amane 刮削.
```

反例:

```
修正入池条件              # 缺 type
Feat: 订阅页支持批量删除   # type 必须小写
feat:订阅页支持批量删除    # 冒号后缺空格
fix(web): 修复了一个问题。 # 结尾有句号, 描述空泛
```

## 分支约定

开发阶段在 `dev` 提交并推送, `main` 暂不接受提交 (pre-commit 钩子会拒绝).

## 启用钩子和模板

克隆后执行一次 (钩子在 `.githooks/`, 模板为 `.gitmessage`):

```bash
git config core.hooksPath .githooks
git config commit.template .gitmessage
```

`commit-msg` 钩子会校验首行格式, 长度, 句号和空行; `Merge `, `Revert "`, `fixup! `, `squash! `, `amend! ` 开头的信息直接放行.

pre-commit 只拦截 main 分支, detached HEAD 下不拦截.
