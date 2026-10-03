"""SQLAlchemy ORM 模型

本地 SQLite 落地以下数据:
  - Jellyfin: 媒体库(VirtualFolders) + 媒体项(电影/剧集) + 分集明细
  - Media/Season: 【可用性】镜像(Jellyfin 扫描写入, 见下)
  - TmdbMedia/TmdbSeason: TMDB 元数据本地缓存(浏览/缺失/整理反查的主源)
  - SyncLog: 每次同步的运行记录(便于前端展示最近同步状态)
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base


# ---------------------------------------------------------------------------
# 媒体状态 / 类型 —— 数字状态码贯穿 media/season/sync 全链路
# ---------------------------------------------------------------------------
class MediaStatus:
    UNKNOWN = 1
    PENDING = 2
    PROCESSING = 3
    PARTIALLY_AVAILABLE = 4
    AVAILABLE = 5
    BLOCKLISTED = 6
    DELETED = 7
    # 库内但完整性未知: Jellyfin 实有集, 但 TMDB 无季结构(404/挂错 ID)或季号错位,
    # 无法确认是否齐全。介于"在库"与"明确不完整"之间 —— 不谎报完整也不错报缺失。
    OWNED_UNVERIFIED = 8


class MediaType:
    MOVIE = "movie"
    TV = "tv"


class Media(Base):
    """【可用性】镜像 —— 这个作品在媒体库里是否可得。

    与 tmdb_media(元数据缓存)不同, 这张表只记录"这个作品在媒体库里是否可得",
    由 Jellyfin 扫描写入, **从不在用户查看时写**。一个作品(tmdb_id, media_type)
    只有一行 —— 去重靠扫描侧"先查再写", 不是唯一约束。

    可用性列说明:
      - status / status4k        : MediaStatus(作品级可用性)
      - jellyfin_media_id        : Jellyfin 项 Id
      - media_added_at           : Jellyfin DateCreated(作品入库时间)
      - last_season_change       : 最后一次季可用性变化(触发通知/对账用)
      - service_id               : 预留(暂空)
    """
    __tablename__ = "media"
    __table_args__ = (UniqueConstraint("tmdb_id", "media_type", name="uq_media_tmdb_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_type: Mapped[str] = mapped_column(String(16), default=MediaType.MOVIE, index=True)  # movie | tv
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    tvdb_id: Mapped[int] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str] = mapped_column(String(32), nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=MediaStatus.UNKNOWN, index=True)
    status_4k: Mapped[int] = mapped_column(Integer, default=MediaStatus.UNKNOWN, index=True)
    # 展示信息(扫描时从 Jellyfin 条目带过来, 免 API): Jellyfin 的 name 就是干净标题,
    # ProductionYear 是年份。本表保留 title/year 两列是为了浏览列表 0 API 出标题/年份;
    # 海报/演员等重字段仍按需实时查 TMDB。
    title: Mapped[str] = mapped_column(String(512), default="", index=True)
    year: Mapped[str] = mapped_column(String(16), default="")
    jellyfin_media_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    media_added_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_season_change: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    service_id: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    seasons: Mapped[list["Season"]] = relationship(
        back_populates="media", cascade="all, delete-orphan"
    )


class Season(Base):
    """【季可用性】—— 每季一行, 记录该季在媒体库里的可得状态。

    (AVAILABLE/PARTIALLY_AVAILABLE/DELETED/...)。
    分集级精确缺失(缺哪一集)仍由 jf_episode vs tmdb_season 计算; 这里的 status
    是"季整体是否可得", 由 availability-sync 对账维护。
    """
    __tablename__ = "season"
    __table_args__ = (UniqueConstraint("media_id", "season_number", name="uq_season_media_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_number: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[int] = mapped_column(Integer, default=MediaStatus.UNKNOWN, index=True)
    status_4k: Mapped[int] = mapped_column(Integer, default=MediaStatus.UNKNOWN, index=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    media: Mapped["Media"] = relationship(back_populates="seasons")


class TmdbMedia(Base):
    """TMDB 作品本地缓存(详情/演员/类型/剧情)。

    架构(2026-09): Web 浏览/缺失/屏蔽/隐藏 全部基于本地 TMDB 缓存。
    缓存以 Jellyfin 库里的 tmdb_id 为种子增量填充(organize 反查也会顺带补)。
    """
    __tablename__ = "tmdb_media"
    __table_args__ = (UniqueConstraint("tmdb_id", "kind", name="uq_tmdb_media"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="movie")  # movie | tv
    title: Mapped[str] = mapped_column(String(512), default="")
    original_title: Mapped[str] = mapped_column(String(512), default="")
    # 英文名(TMDB ?language=en 的 title/name)。中文片的 original_title 仍是中文,
    # 磁力双查需要真正的英文标题才能命中英文命名的发布组资源。
    english_title: Mapped[str] = mapped_column(String(512), default="")
    # 中文标题手动覆盖(详情页「中文标题」填的)。非空时**永远优先于 title**:
    #   - upsert_tmdb_media 每次同步都会把 title 顶回 TMDB 值 → 这里在写入后强制还原;
    #   - 目录名 / 文件名 / NFO <title> 全部读 title, 所以保存时同时写 title。
    custom_title: Mapped[str] = mapped_column(String(512), default="")
    # 中文标题解析是否已跑完(1 = TMDB 译名 + 豆瓣都试过, 哪怕没查到也不必再查)。
    # 豆瓣网络失败时保持 0 → 下次同步重试, 不会把"暂时查不到"固化成"永远没有"。
    title_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    year: Mapped[str] = mapped_column(String(16), default="")
    overview: Mapped[str] = mapped_column(Text, default="")
    poster: Mapped[str] = mapped_column(String(512), default="")
    backdrop: Mapped[str] = mapped_column(String(512), default="")
    vote: Mapped[float] = mapped_column(default=0.0)
    imdb_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    tvdb_id: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="")  # TMDB status(Ended/Returning Series/Released...)
    in_production: Mapped[bool] = mapped_column(default=False)
    original_language: Mapped[str] = mapped_column(String(8), default="")
    # 产地国家(逗号分隔 ISO 3166-1 码, 如 "HK" / "CN,HK")。⚠️ 分类必须用它:
    # 港台片的 original_language 常是 "cn"(国语配音版), 只看语言会把《鼠胆龙威》这类
    # 港片判成 CnMovie; 而 TMDB 直连路径带 production_countries 会判成 HkMovie ——
    # 两条反查路径结果不一致(2026-09-22 用户实测同一部片两次整理进了不同目录)。
    # 本列补上后本地缓存路径也能走 classify._region 的 "HK/TW 优先" 分支, 两路恒一致。
    countries: Mapped[str] = mapped_column(String(64), default="")
    genres: Mapped[str] = mapped_column(Text, default="")  # 逗号分隔 genre_id
    genre_names: Mapped[str] = mapped_column(Text, default="")  # 逗号分隔中文名(展示用)
    cast_json: Mapped[str] = mapped_column(Text, default="")  # JSON: [{name,role,tmdbid,thumb}]
    directors_json: Mapped[str] = mapped_column(Text, default="")  # JSON
    studios_json: Mapped[str] = mapped_column(Text, default="")  # JSON
    keywords_json: Mapped[str] = mapped_column(Text, default="")  # JSON
    certification: Mapped[str] = mapped_column(String(64), default="")
    runtime: Mapped[int] = mapped_column(Integer, default=0)
    premiered: Mapped[str] = mapped_column(String(32), default="")
    end_date: Mapped[str] = mapped_column(String(32), default="")
    number_of_seasons: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class TmdbSeason(Base):
    """TMDB 剧集分集结构缓存(每季一行)。本地「分集级缺失」的"应有集"基准。"""
    __tablename__ = "tmdb_season"
    __table_args__ = (UniqueConstraint("tmdb_id", "season_number", name="uq_tmdb_season"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    season_number: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(256), default="")
    episode_count: Mapped[int] = mapped_column(Integer, default=0)
    air_date: Mapped[str] = mapped_column(String(32), default="")
    in_production: Mapped[bool] = mapped_column(default=False)
    # 该季应有的集号列表(JSON, 如 [0,1,2,3])。默认空;点进分集明细时按需从 TMDB 拉取并缓存,
    # 用于"精确到缺哪一集"。无 TMDB key 时回退 1..episode_count 估算。
    episode_numbers: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class TmdbBlocklist(Base):
    """本地屏蔽列表(用户手动屏蔽某部作品, 浏览/缺失页不再显示)。"""
    __tablename__ = "tmdb_blocklist"
    __table_args__ = (UniqueConstraint("tmdb_id", "kind", name="uq_tmdb_block"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="movie")
    title: Mapped[str] = mapped_column(String(512), default="")
    reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class TmdbIdCorrection(Base):
    """Jellyfin 条目 TMDB ID 校准映射(本地权威修正层)。

    背景: 系统"是否在库"只认 TMDB ID。Jellyfin 侧可能把某作品的 NFO 刮出**错误
    的 TMDB ID**(指向另一部片)。`calibrate_jf_ids` 的 IMDB 精确匹配路径覆盖不到
    一种隐蔽情况: 该条目的 IMDb 号在 TMDB 里也绑在错误片上(IMDB 本身数据错),
    反查只会返回错 ID。此时只能靠**片名+年份搜 TMDB** 找到正确 ID。

    本表记录 {Jellyfin item_id → 正确 tmdb_id} 的人工/审计确认映射。
    scanner 取 ID 时**先查本表**(本地校准优先于 Jellyfin ProviderIds),
    使修正在每次全量/增量扫描后仍然生效(Jellyfin 侧 ID 未动也不回退)。
    一行一个 item; 修正被撤销时删除该行即可。
    """
    __tablename__ = "tmdb_id_correction"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Jellyfin item_id
    kind: Mapped[str] = mapped_column(String(16), default="movie")      # movie | tv
    old_tmdb_id: Mapped[str] = mapped_column(String(32), default="")    # Jellyfin 原挂 ID
    new_tmdb_id: Mapped[str] = mapped_column(String(32), default="")    # 校准后正确 ID
    name: Mapped[str] = mapped_column(String(512), default="")          # 条目名(展示/审计用)
    year: Mapped[str] = mapped_column(String(16), default="")
    method: Mapped[str] = mapped_column(String(32), default="")         # imdb | title | manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class TmdbSetting(Base):
    """本地设置(k-v)。如 hide_complete=true 隐藏已完整作品。"""
    __tablename__ = "tmdb_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class JellyfinLibrary(Base):
    """Jellyfin 媒体库(VirtualFolder)。"""
    __tablename__ = "jellyfin_library"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    library_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    type: Mapped[str] = mapped_column(String(32), default="")  # movies | tvshows | music | ...
    locations: Mapped[str] = mapped_column(Text, default="")  # 路径,多行
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class JellyfinItem(Base):
    """Jellyfin 媒体项(电影/剧集/季/集等)。"""
    __tablename__ = "jellyfin_item"
    __table_args__ = (UniqueConstraint("item_id", name="uq_jf_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    library_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    name: Mapped[str] = mapped_column(String(512), default="")
    type: Mapped[str] = mapped_column(String(32), default="")  # Movie | Series | Season | Episode
    year: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[str] = mapped_column(Text, default="")
    tmdb_id: Mapped[str] = mapped_column(String(32), default="")
    imdb_id: Mapped[str] = mapped_column(String(32), default="")
    overview: Mapped[str] = mapped_column(Text, default="")
    date_created: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # Jellyfin DateCreated
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class JfEpisode(Base):
    """Jellyfin 分集明细(供「分集级缺失」: 本地实有 SxxExx 集合)。

    series_id = Jellyfin Series 项的 item_id(→ jellyfin_item.item_id 查 tmdb_id)。
    两条写入路径: ① 全量重建(每日 03:00, 一次拉到 ~7.7 万条, 约 1 分钟);
    ② 增量补(5 分钟扫描): 只重拉「新入库/有新分集/上轮疑似残缺」的剧。
    ⚠️ 增量路径有"残缺结果保护"(拉到的集数比已有的少 → 不覆盖, 记入可疑队列下轮重试),
    否则 Jellyfin 库扫描期间返回的残缺列表会把整部剧的分集踩少(2026-09-22 修)。
    """
    __tablename__ = "jf_episode"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    season: Mapped[int] = mapped_column(Integer, default=0)   # ParentIndexNumber
    episode: Mapped[int] = mapped_column(Integer, default=0)  # IndexNumber
    name: Mapped[str] = mapped_column(String(512), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class SyncState(Base):
    """同步游标/状态(k-v)。如 jellyfin_items_since = 上次全量/增量同步的 UTC 时间戳,
    用于增量拉取(按 DateCreated 倒序, 拉到游标为止), 避免每次都拉全库。"""
    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class SyncLog(Base):
    """同步运行记录。"""
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # tmdb | jellyfin
    scope: Mapped[str] = mapped_column(String(32), default="")  # all | libraries | items | episodes | jf-scan:* | availability
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | error
    items_synced: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class OrganizeLog(Base):
    """整理(清广告/改名/归位/补季/写NFO)运行记录 —— Web 与 CLI 共用, 重启不丢。

    ⚠️ 此前 Web 整理任务只存在进程内 _JOBS 字典: 服务重启即丢, 且失败明细只进
    console.warn 用户看不到, 表现为"老是报错但查不到原因"。本表落库后:
    - results 是每条目 {name,status,ok,error,skipped,...} 的 JSON 数组
    - 前端「文件整理」页展示最近 20 次运行 + 可展开失败明细
    """
    __tablename__ = "organize_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), index=True, default="web")  # web | cli
    scope: Mapped[str] = mapped_column(String(32), default="")  # 如 "only:巴比伦柏林" / "all"
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | error
    total: Mapped[int] = mapped_column(Integer, default=0)    # 选中条目数
    done: Mapped[int] = mapped_column(Integer, default=0)     # 完成条目数(成功+失败+跳过)
    ok: Mapped[int] = mapped_column(Integer, default=0)       # 成功数
    failed: Mapped[int] = mapped_column(Integer, default=0)   # 失败数
    skipped: Mapped[int] = mapped_column(Integer, default=0)  # 跳过数
    results: Mapped[str] = mapped_column(Text, default="[]")  # JSON: 每条目结果明细
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class Track(Base):
    """追踪: 演员新作 / 剧集新季 —— 检测到更新后可按设置自动推磁力到 CD2 离线下载。

    两种追踪(kind):
      - person : ref_id = TMDB person id。baseline_items 存"首次追踪时已存在的作品
                 (tmdb_id 集合)", 之后 person_credits 里不在集合内的即"新作"。
      - show   : ref_id = TMDB tv id。baseline_seasons 存"首次追踪时已有的季号集合",
                 之后 all_seasons 里不在集合内的即"新季"。

    基线(baseline)的意义: 只追踪"追踪之后新出现的", 不把历史作品全推一遍。
    auto_push: 该条的自动推送覆盖位(None=跟随全局设置, True=强制开, False=强制关)。
    全局开关与大小范围在 settings 表(track_auto_push / track_size_min_gb /
    track_size_max_gb), 见 server/routers/track.py。
    """
    __tablename__ = "track"
    __table_args__ = (UniqueConstraint("kind", "ref_id", name="uq_track_kind_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True, default="person")  # person | show
    ref_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    name: Mapped[str] = mapped_column(String(512), default="")
    # 基线: JSON 数组。person → tmdb_id 列表; show → 季号列表
    baseline_items: Mapped[str] = mapped_column(Text, default="[]")
    # 上次检查时间(手动/自动), 前端展示 + 排重
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # 上次检查结果摘要(如 "新作 2 部, 推送 1, 跳过 1")
    last_result: Mapped[str] = mapped_column(String(512), default="")
    # 自动推送覆盖: None=跟随全局 / True=开 / False=关
    auto_push: Mapped[bool] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class PushRecord(Base):
    """推送标记: 记录某磁力(以 info_hash 唯一标识)是否已推送到 CD2 / Qbit, 避免重复推送。

    两个目标独立标记(一个种子可以先推 CD2、过阵子再推 Qbit, 互不影响):
      - pushed_cd2  : 已推送到 CloudDrive2 离线下载
      - pushed_qbit : 已推送到 qBittorrent 下载器
    info_hash 是磁力唯一键(magnet:?xt=urn:btih:<40位哈希> 的小写哈希), 与搜索接口
    返回的 infoHash、与 state/queue.json 的 info_hash 同一口径, 保证标记能用 hash 对上。
    历史兼容: 本表建立前的 CD2 推送记录在 state/queue.json 里, 查询时一并合并(见 repositories.get_push_states)。
    """
    __tablename__ = "push_record"
    __table_args__ = (UniqueConstraint("info_hash", name="uq_push_info_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    info_hash: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    magnet: Mapped[str] = mapped_column(Text, default="")       # 该 hash 最近一次推送用的磁力链(便于排错)
    title: Mapped[str] = mapped_column(String(512), default="")
    pushed_cd2: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    pushed_qbit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cd2_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)   # 最近一次推 CD2 的时间
    qbit_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # 最近一次推 Qbit 的时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AppConfig(Base):
    """应用配置(JSON blob)—— 2026-09-26 起的配置唯一真相源。

    运行期只读写本表, 不依赖任何配置文件。首次启动若本表为空, 仅用
    config.example.json 播种默认值(setup_done=0 → 进初始化引导); 之后 Web「通用」
    页与首次引导的读写都在这里, 不再有并发写文件的竞态, 也不再有 JSON 语法错
    导致 load_config 静默返回 {} 的坑。(config.json 已彻底移除, 见 2026-09-26。)

    key='config'        整份配置 JSON(indent=2, 与导出文件同格式)
    key='setup_done'    首次引导是否完成("1"/"0"; 从已有配置文件导入 = 1)
    updated_at          unix 秒, 作为热加载依据(server/config.get_config 比对)
    """
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[int] = mapped_column(Integer, default=0)
