"""短命事件循环的 httpx 客户端清理。

背景(2026-10 「Too many open files」事故):
    项目里许多地方用 `asyncio.run(...)` 跑一段**临时事件循环**(定时作业、browse、
    TMDB 同步…)。每个 loop 各自建一份 httpx 客户端并按 loop 缓存; loop 结束时
    httpx **不会**自动关连接池 → keep-alive 套接字变孤儿 → fd 累积 → 打满。

为什么不用 `loop.add_closed_callback`(上一版 3efe601 的教训):
    Python 3.12 / 3.13 / 3.14 的 asyncio loop **都没有**这个方法(实测 AttributeError)。
    旧写法 `try: loop.add_closed_callback(...) except Exception: pass` 把它吞了,
    回调**从没注册**, 于是"修了却没修好"。这里改为**显式 finally 清理**, 不依赖
    任何不存在的 API。

另一个必须处理的坑(自引用):
    客户端按 loop 存在 `WeakKeyDictionary`(key=loop)里, 但 client 经 httpx
    transport **强引用 loop** → 自引用使表项**永不回收**(GC 也无效, 因从模块级
    dict 可达)。所以清理时**必须把 client 从字典 `pop` 掉**, 打破自引用。
    (实测: 不清理时 500 个短 loop → fd +1000, `gc.collect()` 后仍不降。)

用法:
    把运行期的 `asyncio.run(coro())` 换成 `run_coro(coro())`。
    ⚠️ 只替换**短命 loop**的入口; 常驻的 server 主 loop 不在此列(它的客户端
    需跨请求长期复用, 只应在进程退出时回收)。
"""
import asyncio
import logging
import sys

log = logging.getLogger(__name__)

# 需要在 loop 结束前关闭其 httpx 客户端的模块(模块名)。
# 只处理**已加载**的模块(sys.modules), 不主动 import, 避免副作用。
_CLIENT_MODULES = (
    "clients.jellyfin.client",
    "clients.tmdb.client",
    "server.imgproxy",
)


async def aclose_all_clients():
    """关闭【当前事件循环】名下所有客户端模块的 httpx 连接池。逐模块容错。"""
    for name in _CLIENT_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        closer = getattr(mod, "aclose_loop_clients", None)
        if closer is None:
            continue
        try:
            await closer()
        except Exception:  # noqa: BLE001
            log.warning("关闭 %s 的 httpx 客户端失败", name, exc_info=True)


def run_coro(main):
    """等价于 `asyncio.run(main)`, 但保证在 loop 关闭前清掉本 loop 的 httpx 客户端。

    `main` 必须是**已创建的协程**: 调用 `run_coro(coro())`, 不要 `run_coro(coro)`。
    """
    async def _wrapped():
        try:
            return await main
        finally:
            await aclose_all_clients()

    return asyncio.run(_wrapped())
