# AutoBangumi Subscription Review / 订阅审核

适配 **AutoBangumi 4.0.0 + qBittorrent** 的纯插件：Mikan 聚合 RSS 发现新番后，先保存候选，用户选择每部番的版本条件并批准，再自动下载后续匹配的集数。

无需修改或替换 AB 的源文件和镜像。为补足 4.0 的 SDK，本插件在进程内包装下载入口，并注册一个受控 qBittorrent 下载器；内部接口集中在 `review_gate/bridge40.py`。这是针对官方 4.0.0 的兼容实现，不是只依赖稳定 SDK 的插件。

## 功能

- 首页审核面板、番剧详情里的“版本审核”标签。
- 新规则默认待审核；候选独立持久化，RSS 翻页、AB 记录未匹配种子、重启均不丢队列。
- 从一条发布填入字幕组、分辨率、来源、字幕、编码条件；允许手工调整。
- “必须包含”是逐项 AND，“排除”是任意命中即拒绝；均按字面、忽略大小写，不使用正则。
- 先预览、后批准。候选或规则在两步之间变化时，要求重新预览。
- 可选“只追预览后的新发布”或“处理当前候选”；支持起始集数。
- 导入 AB 已有下载记录；同一番、内容类型、映射季度和集数只接受一个版本，不自动升级 v2 或改下其他编码。
- 下载失败可重试。投递结果不确定时保留该集的原候选占用，阻止另一个版本抢先下载。
- 批准后每 30 秒重试队列，沿用 AB 原有下载路径、`Bangumi` 分类、`ab:<id>` 标签及后续重命名、硬链接、媒体库刷新。

## 为什么要切换下载器类型

AB 4.0 的普通 `torrent.filter` 钩子异常时会放行。仅靠该钩子无法保证“未审核绝不下载”。本插件同时提供 `subscription-review-qbittorrent` 下载器：只有同一调用任务中的审核许可才能向真实 qB 发起添加。

插件未加载时，这个 provider 不存在，AB 无法创建下载器；禁用后已经创建的受控客户端也拒绝新添加。它不会回退到普通 qBittorrent。已有 qB 下载和做种任务不受插件阻断新添加的行为影响；插件停用期间 AB 的查询整理可能暂停。

**必须把所有使用中的 qB 下载器实例切换到此类型，保持实例 ID 不变。** 手动在 qB 添加、qB 自己的 RSS 规则、其他应用的下载不受本插件控制。

## 安装与迁移

首次接入会让已有番剧规则也进入待审核；已经下载的文件、qB 任务及 AB 历史不删除。

1. 在 AB 停用所有 RSS（尤其是尚未审核的聚合源），保持“番剧补全”关闭。等待当前刷新结束，停止 AB 容器，备份挂载的 `config` 和 `data`。
2. 把安装包解压到 AB 的 **`config/plugins/local/subscription-review/`**。该目录直接包含 `plugin.toml`、`review_gate/` 和 `web/`，不要多套一级文件夹。
3. 启动 AB，在设置中开启“允许未签名插件”，启用“订阅审核”。确认它显示运行中。此时 RSS 仍应停用。
4. 保持下载器的实例 ID、地址、密码和下载路径不变，将 provider 改为 `subscription-review-qbittorrent`。**AB 4.0 的界面在切换类型时会清空连接选项**，不要直接切换后盲目保存。推荐停止 AB，再用下方离线辅助脚本仅更改 provider。
5. 启动 AB，首页审核面板应显示“下载器保护已生效”。点击“同步已有下载记录”。已有番剧可根据历史发布选择条件，先预览并批准；通常选“只追预览后的新发布”。
6. 启用 Mikan 聚合 RSS。无需把 Mikan 订阅删到只剩一部：所有新发现的规则都等待审核，已批准的规则也必须满足版本条件。
7. 对新番逐部预览并批准，需要当前已有集数时选择“也处理当前候选”，设置起始集数。旧独立 RSS 先停用保留，确认新源匹配正常后再决定清理。

### 离线切换 provider

在 **AB 已停止**时，对实际挂载的配置文件操作。脚本只输出实例 ID 和备份路径，不输出凭据。

```sh
# 只预览，不写入
python3 tools/configure.py /path/to/config/config.json --instance default
# 备份后写入；其他设置和凭据原样保留
python3 tools/configure.py /path/to/config/config.json --instance default --apply
```

多个 qB 实例可重复指定 `--instance`。脚本拒绝 aria2 等其他类型。

### 卸载 / 回滚

先停用 RSS 并停止 AB。使用相同脚本追加 `--restore --apply` 恢复原 qB provider，然后再禁用或移除插件。恢复普通 qB 类型会恢复 AB 原本的自动下载行为，重新启用宽泛聚合源前先确认过滤。

插件队列位于 `config/plugin-data/subscription-review/review.sqlite3`（含 WAL 文件）。备份时停止 AB，或使用 SQLite 在线备份；不要在运行时只复制主文件。队列含种子 URL，可能含站点令牌，不要提交到 GitHub。保留该目录可以恢复审批及去重状态。

## 范围与限制

- 严格支持官方 **4.0.0**，版本不匹配拒绝加载。测试基线 commit：`bc23984ae4a6d388c0b8381e48f6aa9b60e338ba`。未来升级 AB 前需先验证兼容层。
- 首版支持可明确识别集数的单集发布。电影、整季合集、无法安全解析的发布保持等待，不会冒险自动下载。
- 去重依赖 AB 历史和插件队列；不是扫描磁盘重建媒体库。只有媒体文件而没有下载记录时，需要明确设置起始集数。AB 既有错误历史也需要先核实。
- 全局排除、单番排除和其他过滤插件仍然生效。被 AB 更早排除、或完全无法匹配任何规则的条目不会进入审核候选。
- 规则名称、季数、组名或偏移等身份字段变化会重新进入审核，避免旧批准被套用到另一部番。
- 不删除媒体、不自动换版本、不自动取消 Mikan 订阅。暂停一部番只停止新投递，已开始的下载不会被取消。
- 不确定的下载失败只重试原候选，不自动切换同集其他版本。当前没有强制覆盖 / 清除单集占用按钮。
- 兼容测试使用 AB 的真实解析、数据库、RSS 引擎、插件加载器和下载门面；外部 qB 网络添加被替身替换。实际 NAS 上的下载和后续整理仍需试用验收。

## 开发与测试

运行时不需要额外 pip 包。开发测试使用官方 AB 的依赖和 Python 3.14。

```sh
git clone --branch 4.0.0 --depth 1 https://github.com/EstrellaXD/Auto_Bangumi.git /tmp/ab-4.0
uv sync --project /tmp/ab-4.0/backend --python 3.14
AB_SOURCE=/tmp/ab-4.0 /tmp/ab-4.0/backend/.venv/bin/python -m pytest -q
PYTHONPATH=/tmp/ab-4.0/backend/src /tmp/ab-4.0/backend/.venv/bin/ab-plugin validate .
python3 tools/build.py
```

`dist/subscription-review-0.1.0.zip` 是允许列表打包的安装包，不含测试状态、凭据、缓存或源码 checkout。CI 针对同一个官方 commit 验证。

## 后续上游方向

插件验证需求后，再向 AB 提议正式的 `defer` 状态、审核后重新投递接口及可选择 fail-closed 的强制关卡，逐步移除对内部下载门面的运行时包装。本仓库不修改 AB 数据库 schema。
