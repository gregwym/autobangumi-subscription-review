# 统一上游 API 提案

订阅审核与媒体库整理将一起提出 AB 正式扩展接口。目前只记录方案，尚未提交上游 PR。

完整设计、兼容层边界、验收与提交顺序集中维护于：
[媒体库整理仓库 docs/UPSTREAM_API.md](https://github.com/gregwym/autobangumi-library-organizer/blob/main/docs/UPSTREAM_API.md)。

订阅审核所需接口包括：所有下载入口统一准入、持久候选批次、过滤变化重评、稳定任务关联、精确且可解释的跨季匹配、历史清理事务、映射变化事件、维护锁与更新服务。

当前 `review_gate/bridge40.py` 对下载门面、批次过滤和 RSS 匹配的私有适配，在上述接口落地后逐项移除。既有下载去重/不确定提交保护、审核状态和历史数据应保留迁移，不通过清空数据库重建。
