# AutoBangumi 订阅审核

为 **AutoBangumi 4.0.0 + qBittorrent** 提供按番审核：聚合 RSS 发现新番后先保留候选，选好版本再下载，后续自动追更。适合用一个 Mikan 个人 RSS 管理多部动画。

[下载安装包](https://github.com/gregwym/autobangumi-subscription-review/releases/latest) · [维护与回滚](https://github.com/gregwym/autobangumi-subscription-review/blob/main/docs/MAINTENANCE.md) · [AB API 提案](https://github.com/gregwym/autobangumi-subscription-review/blob/main/docs/UPSTREAM_API.md)

![下载版本多选（示例数据）](https://raw.githubusercontent.com/gregwym/autobangumi-subscription-review/main/docs/images/download-versions.png)

## 功能

- 海报显示「待选版本／追番中」，番剧详情增加「下载版本」和「下载历史」。
- 按字幕组、分辨率、来源、字幕和编码展示版本，可多选，也可选择「不过滤版本，沿用 AB 规则」。
- 默认只追后续发布；可补下已发现的缺集。同一番、季度、集数只下载一次，多选版本不会重复下载同集。
- 同批候选存在 v2/v3 时跳过低修订；已下载的 v1 不自动替换。
- 候选和去重状态持久保存；修改季集偏移后同步记录，相关规则重新审核。
- 下载历史可预览、备份后清理，不删除媒体或 qB 任务。
- 自动检查新版，点击一次更新；不会自动安装。

插件沿用 AB 的下载目录及后续整理流程；可搭配[媒体库整理插件](https://github.com/gregwym/autobangumi-library-organizer)。不需要修改 AB 源文件或替换镜像，但使用了针对 4.0.0 的内部接口兼容层，升级 AB 前需核对兼容性。

## 安装与迁移

首次启用时，已有番剧也需审核。现有文件、qB 任务和下载历史保留。

1. 暂停 RSS，保持「番剧补全」关闭，等待当前刷新结束；备份 AB 的 `config` 和 `data`。
2. 下载 Release 中的 `subscription-review-<version>.zip`，解压到 `config/plugins/local/subscription-review/`，使该目录直接包含 `plugin.toml`。
3. 在 AB 中允许未签名本地插件，启用插件并重启 AB。
4. 到「设置 → 订阅审核」点击「启用并刷新」。先保存其他修改；插件会备份配置，将所有 qB 实例切换到受控下载器类型，保留实例 ID、连接信息和路径。
5. 看到「已启用」后开启聚合 RSS，旧的独立 RSS 先停用保留。
6. 点击番剧海报，进入「下载版本」，选择版本并点击「开始追番」。需要补集时选择「补下已发现的缺集」。

**所有使用中的 qB 实例都必须切换到 `subscription-review-qbittorrent`。** AB 普通过滤钩子异常时会放行，受控下载器负责阻止未审核添加。插件未加载或被停用时不会回退到普通下载器；已有 qB 下载和做种继续，AB 对这些任务的查询／整理可能暂停。

## 版本与过滤

选择多个版本表示允许任一匹配版本，仍按集去重。自定义「必须包含」逐项 AND，「排除」任一命中即拒绝；按字面、忽略大小写。AB 的全局／单番过滤和其他过滤插件仍然生效，「不过滤版本」不会绕过它们。

Mikan 个人 RSS 只提供近期条目；补集范围是插件已收集的候选，不是站点全部历史。AB 更早排除或完全无法匹配的条目可能不进入候选；已保存的未匹配记录会重新检查。取消过滤不会自动补下旧资源，新关联条目仍需审核。

## 限制

- 严格支持 AB **4.0.0**。只自动处理可明确识别集数的单集发布；电影、整季合集和歧义发布保持等待。
- 去重依赖 AB 下载历史及插件记录，不扫描磁盘重建。投递结果不确定时保留原候选占用，重试原候选。
- 不自动换版本、不取消 Mikan 订阅。暂停追番只停止新投递，不取消已开始的下载。
- 手动在 qB 添加、qB 自身 RSS 和其他应用的下载不受审核控制。
- 修改季集偏移只同步候选与去重记录，不重命名媒体文件或修复已有硬链接。

## 更新与数据

插件启动约一分钟后、此后每六小时检查本仓库稳定 Release。在设置中点击更新，校验 ZIP 身份和 SHA-256，备份数据库及旧代码后重载。仍需允许未签名插件；校验和不等于官方签名。

历史清理、恢复普通下载器、更新中断处理见[维护说明](https://github.com/gregwym/autobangumi-subscription-review/blob/main/docs/MAINTENANCE.md)。数据库和备份可能含站点令牌；提交问题前去掉日志中的私人路径和凭据，不要上传数据库或配置文件。

<details>
<summary>开发与测试</summary>

Python 3.14；CI 固定验证 AB 4.0.0 的 `bc23984ae4a6d388c0b8381e48f6aa9b60e338ba`。运行时无需额外安装依赖。

```sh
AB_SOURCE=/path/to/Auto_Bangumi /path/to/Auto_Bangumi/backend/.venv/bin/python -m pytest -q
/path/to/Auto_Bangumi/backend/.venv/bin/ruff check .
/path/to/Auto_Bangumi/backend/.venv/bin/ruff format --check .
PYTHONPATH=/path/to/Auto_Bangumi/backend/src /path/to/Auto_Bangumi/backend/.venv/bin/ab-plugin validate .
python3 tools/build.py
```

Release 安装包按允许列表构建，不包含运行数据。发布 `vX.Y.Z` 标签，CI 通过后生成 ZIP 和 SHA-256。

</details>
