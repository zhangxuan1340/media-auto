"""全局同步互斥锁 —— 让所有"写本地媒体库"的同步任务串行执行。

为什么需要: 调度器每 5 分钟自动跑(Jellyfin 条目全库比对 + 可用性扫描), 而 UI 上的
"同步 Jellyfin / 分集明细" 按钮、每日 03:00 的全量扫描、05:00 的可用性对账都可能
**同时**在跑。两个任务同时写 `jellyfin_item` / `media` / `season` 会:
  · 互相覆盖状态(后写的把先算出来的结果盖掉, 例如刚置 AVAILABLE 又被算回 UNKNOWN);
  · 争 SQLite 写锁 —— 默认 busy timeout 只有 5s, 超时抛 "database is locked" 会让
    整轮同步白跑(并可能连带把分集刷新回滚)。

用 RLock(可重入): 调度器要把"条目同步 + 可用性扫描"当成一个整体持锁, 而这两步内部
各自也会取同一把锁 —— 必须可重入, 否则自己把自己锁死。
注意: 只在**同一进程内**生效(FastAPI 内调度器线程与请求线程共用), 跨进程的 CLI
手工执行不在保护范围内。
"""
import threading

SYNC_LOCK = threading.RLock()
