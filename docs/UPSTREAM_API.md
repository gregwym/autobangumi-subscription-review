# AB 插件 API 需求

两个插件的共同提案集中维护在[媒体库整理仓库](https://github.com/gregwym/autobangumi-library-organizer/blob/main/docs/UPSTREAM_API.md)，避免分别维护两套契约。宿主服务已在 [AB #1164](https://github.com/EstrellaXD/Auto_Bangumi/pull/1164) 实现并提交，等待 review，尚未合并。当前插件发行版仍保留 4.0 兼容层。

独立修复：[定时任务作用域 #1162](https://github.com/EstrellaXD/Auto_Bangumi/pull/1162)、[弹窗焦点 #1163](https://github.com/EstrellaXD/Auto_Bangumi/pull/1163)。实际接口见 [SDK 0.7 服务文档](https://github.com/gregwym/Auto_Bangumi/blob/d55882392ae967b09b8d8c02cf4435b353800ca4/docs/dev/plugins/services.md)。

订阅审核的主要需求：

| 优先级 | 接口缺口 | 移除的兼容逻辑 |
| --- | --- | --- |
| P0 | 所有添加入口统一准入，支持持久 hold 与插件不可用时阻断 | 下载门面包装、受控 qB provider |
| P0 | 稳定下载关联、幂等提交及结果对账 | 私有下载客户端和数据库查询 |
| P0 | 批次候选、过滤后重评、带证据的规则匹配 | RSS 批次过滤和匹配包装 |
| P1 | 带修订号的元数据、历史清理及维护协调 | 偏移同步、跨库维护和后台任务暂停 |
| P1 | 用户选择的未签名来源更新服务 | 插件自带更新器 |

版本多选、v2 策略、审核 UI 和去重规则继续由插件维护。迁移保留候选、审批、历史及不确定投递状态，不以清空数据库替代兼容。

新增的站内通知回执与操作追踪需求尚未包含在 #1164，见[共同提案](https://github.com/gregwym/autobangumi-library-organizer/blob/main/docs/UPSTREAM_API.md#p1站内通知确认与操作追踪新增待议)。订阅审核继续复用 `ctx.log`；整理成功通知由媒体库整理插件负责。
