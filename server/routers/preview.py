"""种子内容预览路由: `GET /api/magnet/preview?hash=<40位infoHash|磁力链>`

前端**点开大图弹窗时**才调它 —— 不在列表渲染时预取。原因:
  · 一次搜索几十行, 逐条预取会给每行都加上 1~1.7s 的额外等待;
  · 大部分条目本来就没有截图(whatslink 只给"含视频文件"的种子出图);
  · 用户真正想看内容预览的, 是**他准备点开看的那一个**。

结果带磁盘缓存(成功 24h / 无图 6h / 失败 5min, 见 lib/whatslink.py),
同一行第二次点开是秒出。
"""
from fastapi import APIRouter, Depends, Query

from server.auth import require_auth
from lib import whatslink

router = APIRouter(prefix="/api/magnet", tags=["magnet"],
                   dependencies=[Depends(require_auth)])


@router.get("/preview")
async def preview(hash: str = Query("", alias="hash")):
    """取某种子的内容预览截图。

    返回: `{ok, reason, type, file_type, size, count, name, screenshots[]}`
      · `ok=false` + `reason=no_screenshots` → 接口通了, 但这个种子没有可用截图;
      · `reason=upstream_error` → 上游不可达/超时(前端提示"稍后重试"即可);
      · `reason=bad_hash` → 前端传了不合法的 hash(正常不该出现)。
    """
    return await whatslink.fetch(hash)
