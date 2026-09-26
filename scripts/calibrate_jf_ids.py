#!/usr/bin/env python3
"""
media-auto / calibrate_jf_ids —— 校准 Jellyfin 侧的 TMDB ID(用 TMDB 接口, 不按剧名)
================================================================================
背景: 系统"是否在库"只认 TMDB ID(用户规定不允许按剧名兜底)。如果 Jellyfin 里
某作品的 NFO/媒体库刮削挂错了 TMDb ID(TMM 刮错、同名撞号、跨类型冲突), 系统就会
误判"Jellyfin 有但显示缺失"。本脚本把这类错 ID 用 TMDB 官方接口反查修正。

匹配策略(严格按用户要求: 必须用 TMDB 匹配, 不使用剧名):
  唯一自动修正路径 = **IMDB ID 精确匹配**:
    Jellyfin 条目的 imdb_id → TMDB /find/ttXXXX?external_source=imdb_id
    → 权威 tmdb_id + media_type。IMDB 是全局唯一标识, 无歧义。
  仅当 TMDB 返回的 media_type 与 Jellyfin 条目类型一致(Movie↔movie / Series↔tv)
  且 ID 不同时才改; 类型不一致只报告不改(类型是媒体库结构决定的, 单改 ID 会错位,
  需在 Jellyfin 里重建该条目)。无 IMDB ID 的条目不自动改(没有唯一标识不敢动)。

修改动作:
  主修复 = **本地 jellyfin_item 表**(inLibrary 判定读的就是它, 改这里立即生效):
    scripts/sync_jellyfin 的 upsert 会用 Jellyfin 的 ProviderIds 覆盖 tmdb_id,
    所以本地库平时是"Jellyfin 的镜像" —— 校准就是把这个镜像里挂错的 ID 用 TMDB
    接口修正。
  可选 = **Jellyfin 媒体库侧写回**(加 --jellyfin): 本环境 Jellyfin 写 API 受限
    (PUT /Items/{id} → 405, 单条 GET ProviderIds → 400, 无法安全保留 Tvdb), 默认不动。
  默认 dry-run(只报告, 不写), --apply 才真正改本地 DB。

用法:
  python3 scripts/calibrate_jf_ids.py                 # dry-run, 打印将修正的清单
  python3 scripts/calibrate_jf_ids.py --apply         # 修正本地 DB(inLibrary 立即生效)
  python3 scripts/calibrate_jf_ids.py --apply --jellyfin  # 连 Jellyfin 媒体库一起写
  python3 scripts/calibrate_jf_ids.py --limit 50      # 调试: 只处理前 50 条
"""
import argparse
import asyncio
import difflib
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import load_config
from clients.jellyfin import client as jf
from clients.tmdb import client as tmdb
from db.database import SessionLocal
from db import repositories as repo
from db.models import JellyfinItem, TmdbMedia

CONCURRENCY = 8            # TMDB 限速 ~10 req/s, 8 并发安全
_TYPE_MAP = {"Movie": "movie", "Series": "tv"}   # Jellyfin type → TMDB media_type


async def _find_by_imdb(cfg, imdb_id, sem):
    """TMDB /3/find 反查。返回 (tmdb_id, media_type) 或 (None, None)。

    ⚠️ 路径必须带 /3 前缀(/find 会 404);响应键是 movie_results / tv_results
    (不是 movie/tv)—— 用错键会永远拿到空列表。
    """
    async with sem:
        try:
            data = await tmdb._tmdb_get(cfg, "/3/find/" + imdb_id, {
                "external_source": "imdb_id", "language": "zh-CN"})
        except Exception:
            return None, None
    for key, mt in (("movie_results", "movie"), ("tv_results", "tv")):
        for x in ((data or {}).get(key) or []):
            if x.get("id"):
                return x["id"], mt
    return None, None


async def _classify(cfg, it, sem):
    """单条分类(并发)。返回 (action, new_id, why)。"""
    jf_type = it["type"]
    cur = str(it.get("tmdb_id") or "")
    imdb = str(it.get("imdb_id") or "")
    if jf_type not in _TYPE_MAP:
        return "skip", None, "非 Movie/Series"
    if not imdb.startswith("tt") or len(imdb) < 9:
        return "skip", None, "无 IMDB ID(不自动改)"
    true_id, true_type = await _find_by_imdb(cfg, imdb, sem)
    if true_id is None:
        return "skip", None, f"TMDB /find 未收录 {imdb}"
    if str(true_id) == cur:
        return "ok", None, "一致"
    if true_type != _TYPE_MAP[jf_type]:
        return "type_mismatch", None, (
            f"{imdb} → TMDB {true_id} 但类型不符(Jellyfin={jf_type}, TMDB={true_type})")
    return "fix", str(true_id), f"IMDB 精确匹配 {imdb}"


async def _apply(cfg, it, new_id, with_jf):
    """修正一条错 ID。

    **主修复 = 本地 jellyfin_item 表**(inLibrary 判定读的就是它, 改这里立即生效)。
    Jellyfin 媒体库侧是【可选】(with_jf): 本环境 Jellyfin 写 API 受限
    (PUT /Items/{id} → 405, 且单条 GET 取 ProviderIds → 400, 无法安全保留
    Tvdb 等其它 provider id), 故默认不动 Jellyfin; 加了 --jellyfin 才尝试写回。
    返回 (本地成功, 说明)。
    """
    item_id = it["item_id"]
    local_ok = True
    note = "本地已修正"
    # 本地 DB: 权威修正(inLibrary 的判定源)
    s = SessionLocal()
    try:
        s.query(JellyfinItem).filter(JellyfinItem.item_id == item_id).update(
            {"tmdb_id": new_id}, synchronize_session=False)
        s.commit()
    except Exception as e:  # noqa: BLE001
        local_ok = False
        note = f"本地修正失败 {e}"
        print(f"    ⚠️ 本地修正失败 {it['name']}: {e}")
    finally:
        s.close()
    # Jellyfin 侧: 可选
    if local_ok and with_jf:
        try:
            await jf.update_provider_ids(
                cfg, item_id,
                {"TMDb": new_id, "Imdb": it.get("imdb_id") or ""})
            note = "本地+Jellyfin 已修正"
        except Exception as e:  # noqa: BLE001
            note = f"本地已修正 / Jellyfin 写失败({e.__class__.__name__})"
            print(f"    ⚠️ Jellyfin 更新失败 {it['name']}: {e}")
    elif local_ok and not with_jf:
        note = "本地已修正(Jellyfin 未动, 可加 --jellyfin 同步到媒体库)"
    return local_ok, note


def _db_items():
    """从本地 jellyfin_item 表取 Media 项, 规整成与 list_items 相同的结构。

    校准只需 item_id/name/type/tmdb_id/imdb_id, 读本地即可 —— 不必再走
    Jellyfin 网络拉取(更快, 也不受其写/读 API 限制影响)。
    """
    s = SessionLocal()
    try:
        rows = s.query(JellyfinItem).filter(
            JellyfinItem.type.in_(("Movie", "Series"))).all()
        return [{
            "item_id": r.item_id, "name": r.name, "type": r.type,
            "tmdb_id": r.tmdb_id or "", "imdb_id": r.imdb_id or "",
        } for r in rows]
    finally:
        s.close()


def run_calibration(cfg, items=None, apply=True, with_jf=False, limit=0, verbose=True):
    """可复用入口: 对一批条目做 TMDB 反查 + (可选)修正。

    items: 与 list_items 同结构的条目列表; 不传则从本地 DB 取。
    apply=False → 只分类不改; apply=True → 修正本地 DB(with_jf 时连 Jellyfin)。
    返回 (results: Counter, fixes: list, mismatches: list)。
    sync_jellyfin 全量同步后用它自动回校, 防止 Jellyfin 里的错 ID 再次覆盖本地。
    """
    if items is None:
        items = _db_items()
    if limit:
        items = items[:limit]
    if verbose:
        with_imdb = sum(1 for it in items if str(it.get("imdb_id") or "").startswith("tt"))
        print(f"[calibrate] 共 {len(items)} 条 (有 IMDB ID: {with_imdb}), 逐条 TMDB 校验…")

    state = {"n": 0}

    def done_cb():
        state["n"] += 1
        if verbose and state["n"] % 500 == 0:
            print(f"  进度 {state['n']}/{len(items)}")

    return asyncio.run(_run(cfg, items, apply, with_jf, done_cb))


async def _run(cfg, items, apply, with_jf, done_cb):
    sem = asyncio.Semaphore(CONCURRENCY)
    results = Counter()
    fixes, mismatches = [], []

    async def one(it):
        action, new_id, why = await _classify(cfg, it, sem)
        results[action] += 1
        if action == "fix":
            if apply:
                ok, note = await _apply(cfg, it, new_id, with_jf)
                if ok:
                    fixes.append((it["name"], it["type"], it.get("tmdb_id"), new_id, note))
            else:
                fixes.append((it["name"], it["type"], it.get("tmdb_id"), new_id,
                              f"IMDB 精确匹配 {it.get('imdb_id') or ''} (dry-run, 未改)"))
        elif action == "type_mismatch":
            mismatches.append((it["name"], it["type"], it.get("tmdb_id"), why))
        done_cb()

    await asyncio.gather(*(one(it) for it in items))
    return results, fixes, mismatches


# ---------------------------------------------------------------------------
# --audit 模式: 片名+年份搜 TMDB, 修复"IMDB 反查覆盖不到"的挂错
# ---------------------------------------------------------------------------
def _norm_title(t):
    t = re.sub(r"\s+", " ", (t or "").strip().lower())
    t = t.replace("：", ":").replace("—", "-").replace("——", "-")
    return t


def _is_cjk(t):
    """是否含 CJK 字符(中日韩) —— 用于判断片名/原始语言的文字体系。委托 lib.naming(唯一实现)。"""
    from lib import naming
    return naming.is_cjk(t)


def _is_name_mismatch(jf_name, tmdb_title, ratio=0.4):
    """Jellyfin 名称与 ID 指向的 TMDB 标题是否"明显对不上"(候选挂错)。
    中英名差异(阿索卡/Ahsoka)相似度低但**不是挂错** —— 这只是初筛候选,
    最终由 TMDB 片名搜索确认(搜回同一 ID = 同一部片的不同译名, 安全)。"""
    a, b = _norm_title(jf_name), _norm_title(tmdb_title)
    if not a or not b:
        return False
    if a[:4] in b or b[:4] in a:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() < ratio


async def _search_tmdb_id(cfg, kind, name, year, sem):
    """按片名搜 TMDB, 返回 (best_id, best_title) 或 (None, None)。

    判定规则(宁漏勿错, 避免"中文译名撞同名老片"误修):
      - 标题归一化后与 JF 名**完全一致**
      - 且(若 JF 有年份)该条目 **detail 的年份 == JF 年份** ← 硬校验
    ⚠️ 年份必须拉 detail 取: TMDB search 结果的 release_year/first_air_date
    经常为 None(实测 奇门遁甲2/冥婚 均 None), 直接拿它校验会形同虚设,
    导致 黑寡妇(2021)误匹配到 黑寡妇(1987)、分裂(2016)误匹配 分裂(2017) 等。"""
    async with sem:
        try:
            if kind == "movie":
                data = await tmdb._tmdb_get(cfg, "/3/search/movie", {
                    "query": name, "language": "zh-CN", "include_adult": "false"})
                results = data.get("results") or []
            else:
                data = await tmdb._tmdb_get(cfg, "/3/search/tv", {
                    "query": name, "language": "zh-CN", "include_adult": "false"})
                results = data.get("results") or []
        except Exception:
            return None, None
    name_cjk = _is_cjk(name)
    for x in results[:8]:
        title = x.get("title") or x.get("name") or ""
        if _norm_title(title) != _norm_title(name):
            continue
        if not x.get("id"):
            continue
        # 拉 detail 取准确年份 + 原始语言(search 结果年份常缺失, 不可靠)
        try:
            d = await tmdb.detail(cfg, kind, x["id"])
        except Exception:
            continue
        dy = str((d or {}).get("year") or "")
        orig = (d or {}).get("originalTitle") or ""
        if year and dy and dy != str(year):
            continue  # 年份不符 → 不是同一部
        # 语言一致: 候选的原始语言必须与 JF 片名同文字体系(中文↔中文/拉丁↔拉丁)。
        # 挡住"中文片名撞了同名外文片"(复仇女神→Nemesis)或"外文片撞同名中文片"。
        if orig and _is_cjk(orig) != name_cjk:
            continue
        return (x["id"], title)  # 标题一致 + 年份相符 + 语言一致 → 采信
    return None, None


async def _audit_run(cfg, apply=True, verbose=True):
    """审计并(可选)写入 TMDB ID 校准映射。

    步骤:
      1. 取本地 jellyfin_item(Movie/Series) 全集
      2. 初筛: 名称与 其 tmdb_id 在 tmdb_media 缓存里指向的标题"明显不符"的条目
         (缓存没有的跳过 —— 没有参照无从判断)
      3. 对每个候选: 按 片名(+年份) 搜 TMDB → 标题完全一致 且 ID≠现挂 → 判定挂错
      4. apply 时写 tmdb_id_correction 映射(不直接改 jellyfin_item.tmdb_id,
         由 scanner 取 ID 时查映射生效; jellyfin_item 保持 Jellyfin 镜像原样)
    返回 (candidates, confirmed, applied)。
    """
    s = SessionLocal()
    try:
        items = [{
            "item_id": r.item_id, "name": r.name, "type": r.type,
            "tmdb_id": r.tmdb_id or "", "imdb_id": r.imdb_id or "",
            "year": str(r.year or ""),
        } for r in s.query(JellyfinItem).filter(
            JellyfinItem.type.in_(("Movie", "Series")))
            if r.tmdb_id and str(r.tmdb_id).strip()]
        cache = {}
        for r in s.query(TmdbMedia).all():
            cache[(r.kind, str(r.tmdb_id))] = r
    finally:
        s.close()

    # 初筛候选
    candidates = []
    for it in items:
        kind = "movie" if it["type"] == "Movie" else "tv"
        m = cache.get((kind, str(it["tmdb_id"])))
        if not m or not m.title:
            continue
        if _is_name_mismatch(it["name"], m.title):
            candidates.append((it, m.title))
    if verbose:
        print(f"[audit] 候选(名称与 ID 指向标题明显不符): {len(candidates)} 条")

    sem = asyncio.Semaphore(CONCURRENCY)
    confirmed = []   # (it, new_id, new_title)
    async def check(it, cur_title):
        new = await _search_tmdb_id(cfg, "movie" if it["type"] == "Movie" else "tv",
                                    it["name"], it.get("year", ""), sem)
        # ⚠️ _search_tmdb_id 返回 (id,title) 或 (None,None); 元组 (None,None) 是 truthy,
        # 必须显式判 new[0] is not None, 否则"搜索无命中"会被当成"确认挂错"。
        if new and new[0] is not None and str(new[0]) != str(it["tmdb_id"]):
            confirmed.append((it, str(new[0]), new[1]))
    await asyncio.gather(*(check(it, ct) for it, ct in candidates))

    if verbose:
        print(f"[audit] 确认挂错(TMDB 片名搜索命中不同 ID 且标题一致): {len(confirmed)} 条")
        for it, new_id, new_title in confirmed:
            tag = "APPLY" if apply else "DRY "
            print(f"  [{tag}] {it['name']!r} [{it['type']}] {it['tmdb_id']} → {new_id} "
                  f"(TMDB 标题 {new_title!r})")
    if apply:
        s = SessionLocal()
        try:
            for it, new_id, _t in confirmed:
                kind = "movie" if it["type"] == "Movie" else "tv"
                repo.set_tmdb_id_correction(
                    s, it["item_id"], kind=kind, old_tmdb_id=it["tmdb_id"],
                    new_tmdb_id=new_id, name=it["name"], year=it.get("year", ""),
                    method="title")
            s.commit()
        finally:
            s.close()
    return candidates, confirmed


def run_audit(cfg, apply=True, verbose=True):
    return asyncio.run(_audit_run(cfg, apply=apply, verbose=verbose))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="实际执行: 修正本地 DB(inLibrary 判定源)。默认 dry-run 只报告。")
    ap.add_argument("--jellyfin", action="store_true",
                    help="连同 Jellyfin 媒体库侧一起写回(可选; 本环境写 API 受限, 默认关)")
    ap.add_argument("--limit", type=int, default=0, help="调试用: 只处理前 N 条(0=全部)")
    ap.add_argument("--audit", action="store_true",
                    help="片名+年份搜 TMDB 审计: 修 IMDB 反查覆盖不到的挂错"
                         "(如 IMDb 号在 TMDB 绑错片)。结果写 tmdb_id_correction 映射, "
                         "scanner 取 ID 时优先查映射。")
    args = ap.parse_args()

    if args.audit:
        cfg = load_config()
        if not (cfg.get("tmdb", {}).get("api_key")):
            print("缺 tmdb.api_key(管理 → 通用 → TMDB), 无法片名搜索")
            sys.exit(1)
        print(f"[audit] {'APPLY' if args.apply else 'DRY-RUN'} 片名审计…")
        cands, confirmed = run_audit(cfg, apply=args.apply)
        print(f"\n[audit] 候选 {len(cands)} → 确认 {len(confirmed)}"
              f"{' 已写入映射' if args.apply else ' (dry-run, 加 --apply 写入)'}")
        return


    cfg = load_config()
    if not (cfg.get("tmdb", {}).get("api_key")):
        print("缺 tmdb.api_key(管理 → 通用 → TMDB), 无法用 TMDB 反查")
        sys.exit(1)

    print(f"[calibrate] {'APPLY' if args.apply else 'DRY-RUN'} 从本地库取媒体项…")
    results, fixes, mismatches = run_calibration(
        cfg, items=None, apply=args.apply, with_jf=args.jellyfin,
        limit=args.limit)

    items = _db_items()
    no_imdb = sum(1 for it in items
                  if str(it.get("imdb_id") or "")[:2] != "tt")
    print("\n[calibrate] 汇总:")
    print(f"  ID 一致(无需改):        {results['ok']}")
    print(f"  需修正{'并已完成' if args.apply else '(待 --apply)'}: {results['fix']} (成功 {len(fixes)})")
    print(f"  类型不符(需重建条目):    {results['type_mismatch']}")
    print(f"  无 IMDB ID(未动):       {no_imdb}")
    if fixes:
        print("\n修正清单:")
        for name, typ, old, new, note in fixes:
            print(f"  {name} [{typ}] {old} → {new}  ({note})")
    if mismatches:
        print("\n类型不符明细(该条目需在 Jellyfin 删除重建, 刮削才能落到正确类型):")
        for name, typ, old, why in mismatches:
            print(f"  {name} [{typ}] 当前={old}  {why}")


if __name__ == "__main__":
    main()
