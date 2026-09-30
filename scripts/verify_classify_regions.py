#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【纯本地】地区档可配置回归: 默认行为不变 + 校验 + 分类规则 API

需求(2026-09-30): 后端分类的地区要能手动选择 —— 归属可改、可新增地区档(如单独 Tw)。
实现: config.regions = {order, items} 由 lib.classify 读取; 「管理 → 分类规则」页可编辑;
分类键 <地区档键>Movie/Show 随档生成, 白名单跟着变。

本脚本覆盖:
  A) 默认配置下 17 条判定与旧硬编码版一字不差(改引擎绝不能动老库行为)
  B) normalize_regions 的宽松清洗
  C) validate_regions 的拒绝清单
  D) 分类键生成 / region_info 与 classify 同口径
  E) GET/PUT /api/organize/categories: 行随地区档变、regions 落库、非法配置 400
     (get_config/save_config/CD2 列目录全打桩, 不碰真实库、不联网)

用法: ./venv/bin/python scripts/verify_classify_regions.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def media(**kw):
    base = {"title": "", "filename": "", "content_type": "movie",
            "genres": [], "language": "", "countries": []}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# A) 默认配置: 17 条判定与旧硬编码版一致
# ---------------------------------------------------------------------------
DEFAULT_CASES = [
    ("台产中文片 → 港台", dict(language="cn", countries=["TW"], title="比悲伤更悲伤的故事"), "HkMovie"),
    ("台产英文片 → 欧美(TW 的语言守卫)", dict(language="en", countries=["TW"]), "EnMovie"),
    ("港产日语片 → 港台(HK 优先)", dict(language="ja", countries=["HK"]), "HkMovie"),
    ("中文 + CN → 中国", dict(language="cn", countries=["CN"], title="长津湖"), "CnMovie"),
    ("英文 + US → 欧美", dict(language="en", countries=["US"], title="Inception"), "EnMovie"),
    ("粤语无国家 → 港台", dict(language="yue"), "HkMovie"),
    ("日语 + JP → 日韩", dict(language="ja", countries=["JP"], title="花样男子"), "JpKrMovie"),
    ("泰语 + TH → 东南亚", dict(language="th", countries=["TH"]), "SeaMovie"),
    ("判不出 → Ot", dict(language="", countries=[]), "OtMovie"),
    ("动画类型 → Dm", dict(genres=[16], title="鬼灭之刃"), "DmMovie"),
    ("纪录类型(电影) → JlShow", dict(genres=[99], title="地球脉动"), "JlShow"),
    ("标题带「港」→ 港台(关键词最先)", dict(title="港囧", language="cn", countries=["CN"]), "HkMovie"),
    ("标题带「台」→ 港台", dict(title="那些年，我们一起追的女孩", language="zh", countries=["TW"]), "HkMovie"),
    ("剧集 + 综艺 → XrShow", dict(content_type="tv", title="快乐大本营 综艺"), "XrShow"),
    ("剧集 + 体育 → SpShow", dict(content_type="tv", title="NBA 赛事直播"), "SpShow"),
    ("剧集 + 音乐 → MuShow", dict(content_type="tv", title="某歌手 演唱会 现场"), "MuShow"),
    ("剧集 + 动画 → DmShow", dict(content_type="tv", genres=[16], title="间谍过家家"), "DmShow"),
]


def t_default():
    from lib.classify import classify
    for name, kw, want in DEFAULT_CASES:
        got = classify(media(**kw), {})
        check(f"A 默认: {name}", got["category_key"] == want,
              f"got={got['category_key']} want={want} reasons={got['reasons']}")
    # 目录名映射照旧
    got = classify(media(title="长津湖", language="cn", countries=["CN"]),
                   {"categories": {"CnMovie": "华语片"}})
    check("A 默认: categories 目录名映射仍生效", got["folder"] == "华语片", got)
    # 剧集地区键
    got = classify(media(content_type="tv", language="ja", countries=["JP"]), {})
    check("A 默认: 剧集走 <键>Show", got["category_key"] == "JpKrShow", got)


# ---------------------------------------------------------------------------
# B) normalize_regions 的宽松清洗(脏值不炸, 直接用)
# ---------------------------------------------------------------------------
def t_normalize():
    from lib.classify import DEFAULT_REGIONS, normalize_regions
    n = normalize_regions(None)
    check("B 出厂默认 order", n["order"] == ["Cn", "En", "JpKr", "Hk", "Sea", "Ot"], n["order"])
    check("B 出厂默认键数", len(n["items"]) == 6, len(n["items"]))
    check("B 出厂默认 Hk 的 prio", n["items"]["Hk"]["prio"] == {"HK": [], "TW": ["cn", "zh", "yue"]},
          n["items"]["Hk"]["prio"])

    # 只给了 Tw/Ot: 其它档按用户给的来(order 定义全部), 兜底档永远补上
    n2 = normalize_regions({"order": ["Tw"], "items": {
        "Tw": {"label": "台湾", "languages": ["zh"], "countries": ["tw"],
               "keywords": ["台片"], "prio": {"tw": "zh cn"}}}})
    check("B 缺 Ot 自动补到末尾", n2["order"] == ["Tw", "Ot"], n2["order"])
    check("B 国家码转大写", n2["items"]["Tw"]["countries"] == ["TW"], n2["items"]["Tw"])
    check("B prio 国家键转大写、语言转小写且字符串会拆",
          n2["items"]["Tw"]["prio"] == {"TW": ["zh", "cn"]}, n2["items"]["Tw"]["prio"])
    check("B 脏值被丢掉(非法国家/语言码)",
          normalize_regions({"order": ["X1"], "items": {
              "X1": {"label": "x", "countries": ["CN", "1", "TOOLONG"],
                     "languages": ["zh", "中文", "toolong"]}}})["items"]["X1"]["countries"] == ["CN"]
          and normalize_regions({"order": ["X1"], "items": {
              "X1": {"label": "x", "countries": ["CN"],
                     "languages": ["zh", "中文", "toolong"]}}})["items"]["X1"]["languages"] == ["zh"])

    # 出厂默认没被改过 → 与 DEFAULT_REGIONS 完全等价
    check("B 未配置 == DEFAULT_REGIONS", n["items"] == DEFAULT_REGIONS["items"])


# ---------------------------------------------------------------------------
# C) validate_regions 拒绝清单
# ---------------------------------------------------------------------------
def t_validate():
    from lib.classify import validate_regions
    def bad(name, regions, must_contain):
        try:
            validate_regions(regions)
            check(f"C 拒绝: {name}", False, "没抛错")
        except ValueError as e:
            check(f"C 拒绝: {name}", must_contain in str(e), f"msg={e}")

    def good(name, regions):
        try:
            validate_regions(regions)
            check(f"C 通过: {name}", True)
        except ValueError as e:
            check(f"C 通过: {name}", False, str(e))

    base = {"order": ["Tw", "Ot"], "items": {
        "Tw": {"label": "台湾", "languages": ["zh"], "countries": ["TW"], "keywords": [], "prio": {}},
        "Ot": {"label": "其他", "languages": [], "countries": [], "keywords": [], "prio": {}}}}

    good("合法的 Tw 档", copy.deepcopy(base))
    bad("键不合法", {"order": ["1tw", "Ot"], "items": {
        "1tw": {"label": "x"}, "Ot": {"label": "其他"}}}, "不合法")
    bad("与特殊类型撞车", {"order": ["Dm", "Ot"], "items": {
        "Dm": {"label": "撞车"}, "Ot": {"label": "其他"}}}, "冲突")
    bad("缺 Ot 兜底档", {"order": ["Tw"], "items": {k: v for k, v in base["items"].items() if k == "Tw"}},
        "必须保留")
    b = copy.deepcopy(base)
    b["order"].append("Hk")
    b["items"]["Hk"] = {"label": "港台", "languages": [], "countries": ["TW"], "keywords": [], "prio": {}}
    bad("国家跨档重复", b, "只能属于一个档")
    b = copy.deepcopy(base)
    b["items"]["Ot"]["languages"] = ["zh"]
    bad("语言跨档重复", b, "只能属于一个档")
    b = copy.deepcopy(base)
    b["items"]["Tw"]["label"] = ""
    bad("档名为空", b, "档名不能为空")
    b = copy.deepcopy(base)
    b["items"]["Tw"]["countries"] = ["TWN"]
    bad("国家码不是两位", b, "两位字母")
    b = copy.deepcopy(base)
    b["order"].append("Tw")
    bad("order 有重复键", b, "重复")
    b = copy.deepcopy(base)
    b["items"]["Tw"]["keywords"] = ["k" * 41]
    bad("关键词超长", b, "关键词不合法")
    # 同档内 countries ∩ prio 允许(优先=先于语言, 归属=兜底)
    b = copy.deepcopy(base)
    b["items"]["Tw"]["prio"] = {"TW": ["zh"]}
    good("同档国家既在归属又在优先(允许)", b)
    b = copy.deepcopy(base)
    b["items"]["Tw"]["prio"] = {"HK": ["zh"]}
    b["items"]["Hk2"] = {"label": "港", "languages": [], "countries": ["HK"], "keywords": [], "prio": {}}
    b["order"].insert(1, "Hk2")
    bad("优先国家也跨档重复", b, "只能属于一个档")


# ---------------------------------------------------------------------------
# D) 分类键 / region_info 与 classify 同口径
# ---------------------------------------------------------------------------
def t_keys():
    from lib.classify import classify, region_category_keys, region_info
    nkeys = region_category_keys(None)
    check("D 默认生成 12 个地区键", len(nkeys) == 12 and "HkMovie" in nkeys and "OtShow" in nkeys,
          nkeys)
    tw = {"order": ["Cn", "Tw", "Hk", "Ot"], "items": {
        "Cn": {"label": "中国大陆", "languages": ["zh"], "countries": ["CN"], "keywords": [], "prio": {}},
        "Tw": {"label": "台湾", "display": "台片", "languages": [], "countries": ["TW"],
               "keywords": [], "prio": {"TW": ["zh", "cn"]}},
        "Hk": {"label": "港台", "display": "港片", "languages": ["yue"], "countries": ["HK"],
               "keywords": ["港"], "prio": {"HK": []}},
        "Ot": {"label": "其他", "languages": [], "countries": [], "keywords": [], "prio": {}}}}
    keys = region_category_keys(tw)
    check("D 新增 Tw 档 → TwMovie/TwShow", "TwMovie" in keys and "TwShow" in keys, keys)
    check("D 键数 = 档数 × 2", len(keys) == 8, keys)
    from lib.classify import validate_regions
    try:
        validate_regions(tw)
        check("D 这份 Tw 配置能通过严格校验", True)
    except ValueError as e:
        check("D 这份 Tw 配置能通过严格校验", False, str(e))

    cfg = {"regions": tw}
    got = classify(media(title="台片", language="zh", countries=["TW"]), cfg)
    check("D TW+中文 → TwMovie(新档生效)", got["category_key"] == "TwMovie", got)
    got = classify(media(title="某片", language="cn", countries=["CN"]), cfg)
    check("D CN 仍 → CnMovie", got["category_key"] == "CnMovie", got)
    got = classify(media(title="某港片", language="yue", countries=["HK"]), cfg)
    check("D HK → HkMovie", got["category_key"] == "HkMovie", got)

    ri = region_info(media(title="某台片", language="zh", countries=["TW"]), cfg)
    check("D region_info 用档的 display", ri["key"] == "Tw" and ri["label"] == "台片", ri)
    ri2 = region_info(media(title="长津湖", language="cn", countries=["CN"]), {})
    check("D region_info 默认档", ri2["key"] == "Cn" and ri2["label"] == "国片", ri2)

    # 详情页与归类同口径
    from server.routers.browse import _region_fields
    f = _region_fields("某台片", "", "zh", ["TW"], cfg)
    check("D 详情 regionKey/regionLabel 与归类一致",
          f["regionKey"] == "Tw" and f["regionLabel"] == "台片", f)
    f2 = _region_fields("长津湖", "", "cn", ["CN"], None)
    check("D 详情默认口径", f2["regionKey"] == "Cn", f2)


# ---------------------------------------------------------------------------
# E) 分类规则 API(打桩: get_config/save_config/CD2 列目录)
# ---------------------------------------------------------------------------
def t_api():
    from pydantic import BaseModel  # noqa: F401
    from server.routers import cd2 as api
    from clients.clouddrive import client as cd2c

    store = {"cfg": {"categories": {"CnMovie": "华语片"}}}
    orig_get, orig_save, orig_sub = api.get_config, api.save_config, cd2c.get_subfiles
    api.get_config = lambda: store["cfg"]
    api.save_config = lambda d: store.update({"cfg": d})
    cd2c.get_subfiles = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CD2 不可达(打桩)"))

    async def run():
        try:
            # GET: 默认 6 特殊 + 6 地区档 × 2 = 24 行
            r = await api.api_organize_categories_get(cfg=store["cfg"])
            rows = r["rows"]
            check("E GET 行数 = 18(6 特殊 + 6 档 × 2)", len(rows) == 18, len(rows))
            check("E GET 带 regions", isinstance(r.get("regions"), dict)
                  and r["regions"]["order"] == ["Cn", "En", "JpKr", "Hk", "Sea", "Ot"],
                  r.get("regions"))
            check("E GET 保留自定义目录名",
                  any(x["key"] == "CnMovie" and x["folder"] == "华语片" for x in rows), rows[:3])
            check("E GET CD2 不可达不报错", r["existing_checkable"] is False, r)
            cn_row = [x for x in rows if x["key"] == "CnMovie"][0]
            check("E GET 说明列跟着地区档生成", "国家" in cn_row["desc"] and "语言" in cn_row["desc"],
                  cn_row["desc"])
            check("E GET 特殊键仍在", {x["key"] for x in rows} >= {"DmMovie", "MuShow", "JlShow"})

            # PUT: 只改目录名(老前端行为, 不带 regions)
            r2 = await api.api_organize_categories_put(
                api.CategoriesBody(categories={"CnMovie": "华语"}), cfg=store["cfg"])
            check("E PUT 只改目录名", r2["ok"] and store["cfg"]["categories"]["CnMovie"] == "华语", r2)
            check("E PUT 不带 regions 时 region_changed=False", r2["region_changed"] is False, r2)

            # PUT: 新增 Tw 档(把 TW 从 Hk 拆出来; 语言 zh 留在 Cn,
            # 台湾靠「优先国家 TW=zh/cn」抢在语言之前 —— 这正是优先国家存在的意义)
            regions = {"order": ["Cn", "En", "JpKr", "Hk", "Tw", "Sea", "Ot"], "items": {
                "Cn": {"label": "中国大陆", "display": "国片", "languages": ["zh", "cn"],
                       "countries": ["CN"], "keywords": [], "prio": {}},
                "En": {"label": "欧美", "display": "欧美", "languages": ["en"], "countries": ["US"],
                       "keywords": [], "prio": {}},
                "JpKr": {"label": "日韩", "display": "日韩", "languages": ["ja", "ko"],
                         "countries": ["JP", "KR"], "keywords": [], "prio": {}},
                "Hk": {"label": "香港", "display": "港片", "languages": ["yue"], "countries": ["HK"],
                       "keywords": ["港"], "prio": {"HK": []}},
                "Tw": {"label": "台湾", "display": "台片", "languages": [], "countries": ["TW"],
                       "keywords": ["台"], "prio": {"TW": ["zh", "cn"]}},
                "Sea": {"label": "东南亚", "display": "东南亚", "languages": ["th"],
                        "countries": ["TH"], "keywords": [], "prio": {}},
                "Ot": {"label": "其他", "display": "其他", "languages": [], "countries": [],
                       "keywords": [], "prio": {}}}}
            from lib.classify import classify as _clf
            check("E 拆档预演: TW+中文 → TwMovie",
                  _clf(media(title="台片", language="zh", countries=["TW"]),
                       {"regions": regions})["category_key"] == "TwMovie")
            check("E 拆档预演: CN+中文仍 → CnMovie",
                  _clf(media(title="长津湖", language="zh", countries=["CN"]),
                       {"regions": regions})["category_key"] == "CnMovie")
            check("E 拆档预演: HK+粤语 → HkMovie",
                  _clf(media(title="某港片", language="yue", countries=["HK"]),
                       {"regions": regions})["category_key"] == "HkMovie")
            r3 = await api.api_organize_categories_put(
                api.CategoriesBody(regions=regions), cfg=store["cfg"])
            check("E PUT 新增地区档成功", r3["ok"] and r3["region_changed"] is True, r3)
            check("E PUT regions 落库", store["cfg"]["regions"]["order"] == regions["order"],
                  store["cfg"].get("regions"))
            check("E PUT 回执写了档数", "7 档" in r3["msg"], r3["msg"])

            r4 = await api.api_organize_categories_get(cfg=store["cfg"])
            keys = [x["key"] for x in r4["rows"]]
            check("E GET 行数变 20(7 档)", len(keys) == 20 and "TwMovie" in keys and "TwShow" in keys,
                  keys)
            check("E Hk 档名改了标签跟着变", any(x["key"] == "HkMovie" and x["label"] == "香港电影"
                                              for x in r4["rows"]), r4["rows"][:2])
            check("E 目录名映射没丢", any(x["key"] == "CnMovie" and x["folder"] == "华语"
                                       for x in r4["rows"]), r4["rows"][:2])

            # 新键可以直接给目录名
            await api.api_organize_categories_put(
                api.CategoriesBody(categories={"TwMovie": "台湾电影"}), cfg=store["cfg"])
            check("E 新地区键可设目录名",
                  store["cfg"]["categories"].get("TwMovie") == "台湾电影", store["cfg"]["categories"])

            # 非法配置 → 400
            try:
                await api.api_organize_categories_put(
                    api.CategoriesBody(regions={"order": ["Dm", "Ot"], "items": {
                        "Dm": {"label": "撞车"}, "Ot": {"label": "其他"}}}), cfg=store["cfg"])
                check("E PUT 撞特殊键 → 400", False, "没抛")
            except Exception as e:
                check("E PUT 撞特殊键 → 400", getattr(e, "status_code", None) == 400
                      and "冲突" in str(e.detail), str(e)[:160])
            try:
                await api.api_organize_categories_put(
                    api.CategoriesBody(regions={"order": ["Tw"], "items": {
                        "Tw": {"label": "台湾"}}}), cfg=store["cfg"])
                check("E PUT 缺 Ot → 400", False, "没抛")
            except Exception as e:
                check("E PUT 缺 Ot → 400", getattr(e, "status_code", None) == 400
                      and "必须保留" in str(e.detail), str(e)[:160])
            try:
                await api.api_organize_categories_put(
                    api.CategoriesBody(categories={"NotAKey": "x"}), cfg=store["cfg"])
                check("E PUT 未知分类键 → 400", False, "没抛")
            except Exception as e:
                check("E PUT 未知分类键 → 400", getattr(e, "status_code", None) == 400
                      and "未知分类键" in str(e.detail), str(e)[:160])
            check("E 非法提交没污染配置",
                  store["cfg"]["regions"]["order"] == regions["order"]
                  and "NotAKey" not in store["cfg"].get("categories", {}),
                  store["cfg"].get("categories"))

            # 删档 → 对应键的目录名一并清掉
            regions2 = copy.deepcopy(regions)
            regions2["order"].remove("Tw")
            del regions2["items"]["Tw"]
            await api.api_organize_categories_put(
                api.CategoriesBody(regions=regions2, categories={}), cfg=store["cfg"])
            check("E 删档后 Tw 目录名清理掉",
                  "TwMovie" not in store["cfg"].get("categories", {}),
                  store["cfg"].get("categories"))
            r5 = await api.api_organize_categories_get(cfg=store["cfg"])
            check("E 删档后行数回到 18", len(r5["rows"]) == 18, len(r5["rows"]))
        finally:
            api.get_config, api.save_config, cd2c.get_subfiles = orig_get, orig_save, orig_sub

    asyncio.run(run())


def main():
    print("-- A 默认行为(不能变) --")
    t_default()
    print("-- B normalize(宽松清洗) --")
    t_normalize()
    print("-- C validate(严格拒绝) --")
    t_validate()
    print("-- D 分类键 / 详情同口径 --")
    t_keys()
    print("-- E 分类规则 API --")
    t_api()

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
