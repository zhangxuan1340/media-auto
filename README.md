# media-auto — 影音自动化流水线

从搜磁力到刷新媒体库的一条链路:**磁力搜索(双源)→ CloudDrive2 离线下载 → 完成轮询 → 分类归位 → 整理(清广告 / 剥推广 / 改名 / 写 NFO)→ 刷新 Jellyfin**。

日常操作基本都在 Web 控制台(1.2 · PWA)里完成:搜种子、推离线、看缺失、跑整理、改配置,不必每次开终端。控制台同时把 Jellyfin 的媒体库/分集与 TMDB 元数据同步到本地 SQLite,「本地库」「浏览」「缺失」都走本地缓存,不反复请求远端服务。

**三条设计基线**

| 基线 | 说明 |
| --- | --- |
| 配置单一真相源 | 只存数据库(SQLite `app_config` 表),运行期不读任何配置文件;入口只有首次初始化引导与「管理 → 通用」,点「保存配置」即热加载,不用重启、不用改文件 |
| `/Cloud` 只进不删 | 代码层拦截(`delete_files()` 命中保护路径抛 `PermissionError`),只允许移入;写 NFO 需覆盖时自动改走「临时区 → `MoveFile` Overwrite」 |
| 默认 dry-run | 整理永远先预览再执行;`/Temp`(离线工作区)下的删除走 CD2 回收站,可恢复 |

## 目录

- [功能特性](#功能特性)
- [架构与工作流程](#架构与工作流程)
- [目录结构](#目录结构)
- [部署](#部署)
- [首次使用](#首次使用操作顺序)
- [Web 控制台](#web-控制台)
- [磁力搜索](#磁力搜索)
- [整理离线目录](#整理离线目录)
- [同步 Jellyfin / TMDB](#同步-jellyfin--tmdb-到本地-sqlite)
- [追踪](#追踪演员新作剧集新季)
- [命令行脚本](#命令行脚本可选)
- [回归验证](#回归验证)
- [CloudDrive2](#clouddrive2)
- [开源协议](#开源协议)
- [致谢](#致谢)

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 磁力搜索 | 双源(原生 Bitmagnet GraphQL + Bitmagnet-Next-Web REST);五种排序;分段窗口分页;前排发布组 + 金标自压组;质量分两条硬约束 |
| 离线下载 | CloudDrive2 `AddOfflineFiles`,多链接换行分隔;完成轮询 |
| 分类引擎 | 类型优先于地区级联(动画 > 纪录 > 综艺 > 体育 > 音乐 > 地区),决定 `/Cloud/<分类>` |
| 整理 | 清广告 / 剥推广前缀 / 改名 / 写 NFO / 按分类归位;TMM 风格命名;自带刮削 |
| 元数据同步 | Jellyfin 媒体库/分集 + TMDB 标题/海报/分集 落到本地 SQLite |
| 缺失检测 | 剧集分集缺失 / 电影未拥有;只按 TMDB 真实集号报缺,不估算 |
| 追踪 | 演员新作 / 剧集新季 → 对比基线 → 搜磁力 → 四维筛选 → 推 CD2 |
| Web 控制台 | FastAPI 单端口 + 单页 PWA;4 个主页签 + 9 个管理子页签 |
| 配置管理 | 数据库单一真相源,分组表单热加载,导出/备份 |
| 安全 | `/Cloud` 只进不删;删除走回收站;默认 dry-run |

## 架构与工作流程

一条单向流水线,外加几条由「作业」触发的同步 / 对账链路,把远端状态镜像到本地库:

```
  磁力搜索(双源) → 推 CD2 离线 → 完成轮询 → 分类归位 → 整理 → 刷新 Jellyfin
       │              push.py     check.py              │    organize.py
       │                                                └─ 清广告 / 改名 / NFO / 归位
       └── search.py / diao_search.py

  同步 / 对账(管理 → 作业 触发):
    sync_jellyfin     全量/增量 upsert 媒体项 + 分集  → jellyfin_item / jf_episode
    sync_jf_scanner   窗口式增量, 可用性(在库/完整/缺失) → media / season
    sync_tmdb         TMDB 元数据(标题/海报/分集)      → tmdb_media / tmdb_season
    availability_sync 唯一会把状态改成 DELETED 的对账作业
```

- **写入永远可加(upsert/merge),删除只由 fail-safe 的专职作业 `availability_sync` 做**;失败方向永远倒向「保留」,结构上不会把完整数据洗成缺失(详见「同步」一节)。
- 本地库是单一 SQLite(`data/media_auto.db`),控制台 / 浏览 / 缺失都读它,避免反复打远端。

## 目录结构

```
MediaAuto/
├── README.md            # 本文件
├── CHANGELOG.md         # 更新记录(带日期的口径变更/报障修复历史)
├── LICENSE              # GPL-3.0(见文末「开源协议」)
├── requirements.txt     # Python 依赖
├── config.example.json  # 配置模板(仅首启播种默认值, 之后配置只在数据库)
├── Dockerfile / .dockerignore / docker-compose.yml   # 容器化部署
├── doc/clouddrive2/     # CloudDrive2 官方 gRPC API 指南(中文)
├── lib/                 # 公共逻辑
│   ├── config.py        # 配置读写(真相源 DB app_config; 脚本与 server 共用)
│   ├── classify.py      # 分类引擎: 类型优先于地区, 决定 /Cloud/<分类>
│   ├── naming.py        # 推广块剥离 / 广告识别 / 标题年份抽取 / 命名模板 / 匹配度校验
│   ├── titles.py        # 标题优先级(手动 > TMDB > 豆瓣 > TMDB 台/港)+ 豆瓣联想短缓存
│   ├── episode_numbers.py # 分集集号口径: 无 TMDB 真实集号就不猜
│   ├── mediainfo.py     # 媒体探测(WebDAV+ffprobe / CD2 下载链接) → 质量标记 + <streamdetails>
│   ├── nfo.py           # tinyMediaManager 5.2.12 兼容的 NFO(电影 + tvshow)
│   ├── jobs.py          # 6 个定时作业的定义与排期
│   ├── sync_guard.py    # 同步/可用性状态守卫
│   ├── cache_stats.py   # 缓存统计(作业页展示)
│   └── state.py         # 队列状态 state/queue.json
├── clients/             # 外部服务客户端(脚本 & server 共用)
│   ├── clouddrive/      # CloudDrive2 客户端 + clouddrive.proto(官方原样拷贝)
│   ├── jellyfin/        # Jellyfin 客户端
│   ├── tmdb/            # TMDB 客户端(元数据主源)
│   ├── qbit/            # qBittorrent 客户端
│   └── douban/          # 豆瓣联想(中文标题反查)
├── db/                  # 本地 SQLite 存储层(database / models / repositories)
├── scripts/             # CLI 子命令 + 同步器 + 回归门禁(verify_*.py, 见文末)
│   ├── search.py        # 磁力搜索(原生 Bitmagnet GraphQL)
│   ├── diao_search.py   # Bitmagnet-Next-Web 站点搜索(改版站, REST)
│   ├── push.py          # 推 CD2 离线下载
│   ├── check.py         # 轮询完成 + 分类 + 移动
│   ├── organize.py      # 整理离线目录: 清广告 → 改名 → 写 NFO → 按分类归位
│   ├── finish.py        # 刷新 Jellyfin(tMM 已停用)
│   ├── pipeline.py      # 一键全链路
│   ├── sync_jellyfin.py / sync_jf_scanner.py / sync_tmdb.py / availability_sync.py
│   ├── track_check.py   # 追踪: 演员新作 / 剧集新季 → 搜磁力 → 筛选 → 推 CD2
│   └── verify_*.py      # 回归门禁(见文末「回归验证」)
├── server/              # FastAPI 网页控制台(单端口)
│   ├── main.py          # 入口: 登录 / 鉴权 + 托管静态资源 + 挂载 API
│   ├── config.py        # 配置读写(真相源 DB app_config), 暴露项目根目录
│   ├── auth.py          # 用户名/密码 + HttpOnly Cookie 会话
│   ├── routers/         # search browse sync jobs cd2 track qbit nfo media configapi
│   └── static/          # index.html + js/(14 个模块) + css/app.css
│                        # + PWA: manifest.webmanifest / sw.js / icons/
├── state/               # 运行时队列目录(gitignored)
└── data/                # SQLite(data/media_auto.db)+ 图片缓存(gitignored)
```

## 部署

配置存在数据库里(SQLite 的 `app_config` 表),容器/进程都不内置配置文件,也没有第二份存储。首次启动会用 `config.example.json` 播种默认值并进入初始化引导,重新填一遍。想省事,先在旧机器上「管理 → 通用 → 导出备份」留档。

### 本地直接跑(venv)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -m server.main        # 端口取 config.web.port(默认 8787)

# 可选: 探测媒体信息写 NFO 的 <fileinfo>
#   brew install mediainfo     # 或用 ffmpeg(ffprobe)
```

浏览器打开 `http://<host>:8787`,默认账号 `admin` / `change_me`(首次登录进引导改密码),也可以用环境变量 `WEB_USER` / `WEB_PASS` 覆盖。

### Docker(推荐 NAS)

```bash
docker compose up -d --build
docker compose logs -f
curl -fsS http://localhost:8787/ >/dev/null && echo OK      # GET / 返回 200 即正常
```

| 项 | 值 |
| --- | --- |
| 镜像 | `python:3.12-slim` + `mediainfo` / `ffmpeg`(探测 `<fileinfo>` 用, 可缺省) |
| 端口 | `8787:8787`(容器内监听 `0.0.0.0:8787`) |
| 配置 | 数据库(`app_config` 表), 容器不内置配置、不挂配置文件 |
| 数据库 | 卷 `./data:/app/data`,SQLite 走 WAL,必须放本地盘,不要放网络盘/对象存储 |
| 图片缓存 | `./data/img_cache`(随 `data` 卷一起持久化) |
| 队列状态 | 卷 `./state:/app/state`(`state/queue.json`) |
| 探活 | `GET /`,本项目没有 `/api/health` |

可用环境变量:

| 变量 | 作用 |
| --- | --- |
| `WEB_USER` / `WEB_PASS` | 覆盖数据库里的 `config.web.auth` 登录账号 |
| `MEDIA_AUTO_DIR` | 项目根目录(容器内默认 `/app`,一般不用改) |
| `MEDIA_AUTO_DB` | SQLite 路径(默认 `/app/data/media_auto.db`) |

容器只承载 Web 控制台和同步作业。整理/搬运要访问的 `/Cloud`、`/Temp` 等媒体目录与 NAS 上的 Jellyfin 是另一条链路:要么在 `docker-compose.yml` 里自己加只读卷挂载,要么按「本地直接跑」在 NAS 上以进程方式跑流水线脚本。

### 数据备份与恢复

配置备份走「管理 → 通用 → 导出备份」(含令牌,注意保管),或直接备 `data/media_auto.db`。恢复配置就是恢复数据库文件,配置没有第二份存储。

```bash
# 热备, 不用停服, WAL 会被一并合并
venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/media_auto.db'); d=sqlite3.connect('data/backup.db'); s.backup(d); d.close(); s.close()"
```

停服务直接拷 `data/media_auto.db*`(含 `-wal` / `-shm`)也可以,恢复时放回 `data/`。

## 首次使用(操作顺序)

1. 登录 → 进**首次初始化引导**:改密码,填 Jellyfin / TMDB / CloudDrive2 地址与令牌、媒体库根目录。
2. **管理 → 通用**:日常改配置。上方页签分组(一次只看一组),改完点底部「保存配置」立即热加载;密钥显示 `••••••` 表示未修改;切页签不会丢改动,保存是一起提交的;「导出备份」导出整份 JSON。同页还有:夜间模式(跟随系统 19:00–07:00 / 手动浅色·深色)、隐藏已完整作品、图片本地缓存、S0 特别篇是否计入缺失检测。
3. **管理 → 分类规则**:上方是**地区档**表(每档生成 `键Movie` / `键Show` 两个分类键,可增删改归属),下方是分类键 → `/Cloud` 下真实目录名的映射,以及库根路径。特殊类型键(动画/纪录片/综艺/体育/音乐共 6 个)**不可增删**,只能改目录名;改名不迁移已有内容(新目录下次整理时自动创建,已归位的自行移动)。
4. **管理 → 下载**:填 qBittorrent 地址/账号,测试连接,看任务进度与「下到第几集」聚合。
5. **整理**页签:先「预览」看计划(要删的广告、新目录名、目标库、匹配度),再勾选或「执行全部」。
6. 需要定时动作的去 **管理 → 作业** 改排期(手动运行不改变排期)。

## Web 控制台

单端口 FastAPI + 单页前端,底部 4 个主页签:

| 主页签 | 内容 |
| --- | --- |
| 电影 / 剧集 | TMDB 热门榜(滚动分页)+ 高级筛选:类型 / 国家 / 日期范围 / 流媒体平台(可切地区)/ 电影分级 / 剧集完结状态 —— 不带高级筛选走榜单,带任意一项切 TMDB discover;工具栏值始终按当前筛选回显 + 详情弹窗(导航栈、侧滑返回):演员页、磁力搜索与推送、分集扫种子、追踪新作/新季、中文标题覆盖与改名、手动重建 NFO |
| 整理 | 离线目录整理计划 → 勾选执行 → 整理记录(详见「整理离线目录」) |
| 管理 | 9 个子页签(下表);支持深链,如 `?tab=manage:missing` 直达缺失页 |

管理子页签:

| 子页签 | 内容 |
| --- | --- |
| 缺失 | 剧集分集缺失 / 电影未拥有,分页 + 滚动加载(整表由后端 30s 缓存切片);分集只按 TMDB 真实集号报缺,拿不到集号显示「编号未同步」,不估算 |
| 通用 | 分组配置表单 + 夜间模式 / 隐藏完整 / 图片缓存 / S0 特别篇开关 |
| 分类规则 | 地区档增删改(每档 → `键Movie`/`键Show`)+ 分类键 → 目录名映射 + 库根 + 库内实际目录是否存在 |
| 下载 | qBittorrent 配置与连接测试、任务进度(有下载中任务时 5s 轮询)、按作品聚合 |
| 追踪 | 演员新作 / 剧集新季追踪 + 推送筛选(片源/分辨率/发布组/大小) |
| 本地库 | Jellyfin / TMDB 同步状态与浏览;同步统一由「作业」触发 |
| 作业 | 定时作业(改排期 / 手动运行)+ 缓存统计与清理 |
| 屏蔽 | 按 TMDB ID 屏蔽作品(热门榜/浏览/缺失都会过滤) |
| 浏览 | 本地 TMDB 缓存筛选浏览:类型 / 库内状态 / 片名 / 年份范围 / 完结状态 / 分级(**剧集分级只能在这页筛** —— TMDB `discover/tv` 没有 certification 参数,页签里会置灰并注明) |

PWA:可安装到桌面(`manifest.webmanifest`),Service Worker 走网络优先、失败回退缓存,离线也能打开;`/api/qbit/*` 实时数据永不缓存。

## 磁力搜索

三个磁力源,**各自独立开关**(`enabled`),想开哪个就勾哪个——可单开,也可多开。多开时系统并行查所有启用的源,结果按 InfoHash 去重后自动合并;某源查询失败(如未配地址)不影响其它源,全部失败才报错。管理页「磁力搜索源」里每个源一张独立卡片(启用开关 + 专属字段 + 协议「检测」)。

| 源 | 配置段 | 协议 | 特点 |
| --- | --- | --- | --- |
| Bitmagnet(原生) | `bitmagnet` | GraphQL | 自带 seeders/leechers 与 TMDB 元数据,适合自托管 |
| Bitmagnet-Next-Web | `bitmagnet_next_web` | REST(改版站) | 通常更快,无 seeders |
| Jackett | `jackett` | Torznab | 种子聚合引擎,一次聚合 Jackett 里配置的全部站点(`indexer` 默认 `all`) |

- 地址支持 http/https 切换与自动探测(「管理 → 通用 → 磁力搜索源」里有「检测」按钮,后端 `GET /api/search/probe` 逐协议试,收到 HTTP 响应即算通)。
- 排序:`relevance`(引擎原序)、`quality`(质量优先)、`size_desc` / `size_asc`、`seeders_desc`。非 `relevance` 的模式按**分段窗口**抓取排序后分页:首屏只抓 `max(需要+30, 60)` 条,「加载更多」要更多时再重抓更大的窗口(200 条封顶),同查询+排序结果缓存 120s。站点单页只有 10 条却要 2~3.5s,所以翻页在 `scripts/diao_search.py::collect` 里按 6 路并行——老实现每个请求都拉满 200 条(串行翻 20 页 = 12~20s)是「详情页磁力列表很慢」的根因。
- **前排发布组**(管理 → 通用 → 种子抓取规则,`search.group_priority`):一行一个组名,行序 = 优先级,大小写不敏感;命中要求组名与标题其余部分分开(`-Beitai`、`[FRDS]`、` HHD` 算,`CHDRip` 不算 `CHD`)。详情页「质量优先」搜索与追踪自动推送都按这个顺序整批排最前;留空 = 关闭前排。同处的**金标自压组**(`search.golden_groups`)给命中的种子默认金标,质量分 +20。
- **质量分的两条硬约束**:
  1. **分辨率写实**:名字出现 `2160P`/`3840x2160` 才算 4K;只写 `4K`/`UHD` 但同时写了 `1080`(如《…【4K.SDR1080p】》)按 1080p 算——否则拿满 48 分,把真 1080p 蓝光压在下面,界面标签也照样显示 2160p。
  2. **体积合理性**:按分辨率要求「该有的体积」,名不副实扣分(4K:1~4G −45、100M~1G −60、<100M −75;≥10G +6、≥30G +10;1080p <400M −35)。扣到 −35 就回传 `sizeSuspect`,界面出**「体积可疑」**标签。体积 <1MiB 视为站点占位/未知,不奖不罚。

## 整理离线目录

离线下载完成后,目录名往往带着高清站的推广前缀,里面还塞着几百 KB 的广告文件,例如:

```
【高清影视之家发布 www.HDBTHD.com】误杀2[60帧率版本][高码版][国语配音+中文字幕].Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD/
├── Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD.mkv   ← 正片,保留
├── 【更多无水印蓝光原盘请访问 www.HDBTHD.com】【…】.MP4                              ← 广告,删
├── 【更多无水印蓝光电影请访问 www.HDBTHD.com】【…】.DOC                              ← 广告,删
└── 【更多无水印高清电影请访问 www.HDBTHD.com】【…】.MKV                              ← 广告,删
```

整理之后(顺带写好 NFO,Jellyfin 读取即可,不用再刮):

```
/Cloud/CnMovie/误杀2 (2021)/
├── 误杀2 (2021) 2160p h265 EAC3.mkv    ← 原片,推广前缀已剥掉, 技术标记保留
└── 误杀2 (2021) 2160p h265 EAC3.nfo    ← tinyMediaManager 5.2.12 兼容的完整 NFO
```

执行顺序(不可乱):

```
搬前探测 → 清文件名推广 → 删广告/杂项 → 冲突判定 → 替换源 NFO → 改名
→ 在 /Temp 工作区按季整理 → 归位 /Cloud/<分类> → 写 NFO
```

规则与口径:

- **分类**:归位目的地是 `/Cloud/<分类>`(`organize.cloud_root`,默认 `/Cloud`)。分类走 `lib/classify.py` 的级联:**动画(Dm) > 纪录片(Jl) > 综艺(Xr) > 体育(Sp) > 音乐(Mu) > 地区档**,类型优先于地区。地区档默认 **中国大陆(Cn)/ 欧美(En)/ 日韩(JpKr)/ 港台(Hk)/ 东南亚(Sea)/ 其他(Ot)**,每档生成 `键Movie` / `键Show` 两个分类键;档与归属在「管理 → 分类规则 → 地区档」**可增删改**(想把台湾单拆成 Tw 档就是在这里加一行)。判定顺序:**关键词 → 优先国家 → 语言 → 国家 → 兜底 Ot**,所以出厂默认下台湾与香港同属港台(`HkMovie` / `HkShow`,没有单独的 Tw 目录)。同一个国家/语言只能属于一个档,拆档时先把 TW 从港台档的「国家/优先」里删掉。目录名同样在「管理 → 分类规则」改;改归属/删档**不迁移已归位内容**,旧目录留原地自行合并。
- **命名**:默认 TMM 风格,目录 `标题 (年份)`,电影文件 `标题 (年份) 质量`(如 `保持沉默 (2019) 2160p h265 EAC3.mp4`)。想换成 `标题.年份.ttIMDB`,把 `organize.folder_template` 改成 `{title}.{year}.{imdb}` 即可。
- **自带刮削**:直接生成完整 NFO(电影 `<视频名>.nfo`、剧集 `tvshow.nfo`),字段含 `ratings`、`uniqueid`(tmdb/imdb/wikidata)、`genre`、`actor`、`crew`、`producer`、`trailer`、`fileinfo.streamdetails` 等。元数据来自 TMDB 反查:用目录名 + 主媒体文件名去搜,中英文名都能查(`Fireflies in the Sun` → 误杀2,`Gannibal` → 噬亡村)。磁力链本身不带 TMDB/IMDB,关联就靠这一步。
- **`<fileinfo>` 探测**(编码/分辨率/音轨):按顺序 ①`config.webdav`(账号根 URL + 账号内路径 + 凭据)用 ffprobe 读(Range 只取需要的片段),ffprobe 缺失时 mediainfo CLI 兜底 → ②CD2 下载链接(`GetDownloadUrlPath`,只要 gRPC token,不受 `account_root` 限制);两条都不通才退回从文件名推断(NFO 不含该段)。WebDAV 账号只授到 `/Temp`,所以探测在搬运之前完成;探测失败时整理日志会写明断在哪一环,`<fileinfo/>` 才留空,不会静默失败。
- **NFO 更新**:「更新 NFO」只刷新 TMDB 元数据;`<fileinfo>` / `<original_filename>` / `<source>` / 观看状态这些**随文件走的字段原样沿用** —— 更新前先读现有 NFO,读取通道依次是 ①WebDAV(受 `webdav.account_root` 范围限制,账号只开 `/Temp` 时读不到 `/Cloud`)→ ②CD2 下载链接(只要 gRPC token,不受账号范围限制)。文件在目录里但两条都读不到时**拒绝覆盖**(502 逐条列出原因),免得把上面那些字段抹成空。
- **广告判定**:去掉含域名的括号块之后没有实际片名就算广告,不做域名白名单(高清站域名变体多,形态却一致)。正片名里带推广前缀不算广告。非视频杂项(`.txt` / `.url` / `.doc` / `.pdf` 等)一并删除;`.nfo` 与海报类资产保留,字幕默认保留。
- **剧集归位**:按季归位 + 集文件改标准名(字幕跟随)。季号取自 `SxxExx` / 「第N集」/ 子目录名,提取不到季的文件留原地;目标名已存在则跳过,绝不覆盖。文件名与子目录都没有季号时,预览里该条会标 **`季号不明 · 保留原名/原结构`** —— 不替你猜,文件留在剧目录根、不改名。想让它们进季目录,二选一:①把文件改出季号(如 `…E01…` → `…S01E01…`)或移进 `Season 01/` 子目录(下一条规则会从子目录名读出季号)后重跑整理;②用整理页的「重新匹配」确认条目后再执行。**单季剧也不会自动补 S01**(要的是可预期,不是聪明)。
- **状态口径**:`ok` 改名归位;`merge` 剧集补季(逐文件并入库内季目录,已有内容不动);`duplicate` 库里已有同名:不重复归位,只清源目录里的广告;`upgrade` 库里已有但这条规格更高(升级版):同样只清广告 —— `/Cloud` 只进不删,不会自动换上;其他冲突按 `organize.on_conflict`(`skip` 默认 / `merge` 并入已有目录)。
- **源 NFO 只在「确认要搬」之后才替换**:冲突跳过、搬不动、TMDB 取不到元数据时,旧 NFO 一个字节不动。
- **默认 dry-run**:CLI 用 `--apply` 才执行;Web 端先预览再点执行,单次默认 100 条、硬上限 200 条(不传 `limit` 不会再像旧版那样静默截断成 20 条,超出会在完成提示里说明)。删除走 CD2 回收站。
- **匹配度校验**:反查结果做匹配度校验(`lib/naming.py:title_match`),防止模糊匹配错条目(例如目录名「国安…」被搜成《国土安全》),匹配度不足就跳过改名;预览是**只读**的,不会写标题、不消耗豆瓣核对标记。

Web 控制台的「整理」页签与 CLI 走同一套流程,另有整理记录可查。

## 同步 Jellyfin / TMDB 到本地 SQLite

```bash
# 把 Jellyfin 的媒体库 + 媒体项 + 分集拉到本地库
venv/bin/python scripts/sync_jellyfin.py --scope all
venv/bin/python scripts/sync_jellyfin.py --scope libraries   # 仅媒体库
venv/bin/python scripts/sync_jellyfin.py --scope items       # 仅媒体项

# 可用性扫描(在库/完整/缺失 → 写 media / season)
venv/bin/python scripts/sync_jf_scanner.py --mode full       # 全量
venv/bin/python scripts/sync_jf_scanner.py --mode recent     # 增量窗口

# TMDB 元数据(标题/海报/分集)缓存
venv/bin/python scripts/sync_tmdb.py --full                  # 默认只刷缺失/过期条目
venv/bin/python scripts/sync_tmdb.py --limit 50              # 只刷 N 条(调试)

# 可用性对账(唯一会把状态改成 DELETED 的作业)
venv/bin/python scripts/availability_sync.py --dry           # 先只报告
venv/bin/python scripts/availability_sync.py                 # 真写库
```
(`sync_jellyfin.py` 的 `--scope` 还可取 `episodes`;`--full` 一律表示全量重刷。)

同步结果落在 `data/media_auto.db`(可用环境变量 `MEDIA_AUTO_DB` 改路径)。本地库表:`jellyfin_library` / `jellyfin_item` / `jf_episode` / `media` / `season` / `tmdb_media` / `tmdb_season` / `sync_log`。界面上的同步入口在 **管理 → 作业**(近增扫描 / 全量扫描 / 可用性对账 / TMDB 同步),手动运行不会改变它的排期。

写入模型:**全量/增量都是纯 upsert,全类无 delete/clear**;删除只由 `availability_sync` 做,且只有确认消失(404)才写 DELETED,非 404/500 一律当「还在」,失败方向永远倒向保留。

## 追踪(演员新作、剧集新季)

在演员页点「追踪新作」、剧集详情页点「追踪新季」登记目标。定时作业 `track-check` 默认每 6 小时跑一次(管理 → 作业可改),也可以手动触发:

```bash
venv/bin/python scripts/track_check.py    # 检查全部追踪; 自动推送关着时是干跑, 只记录会推什么
```

流程是:TMDB 对比基线拿新增 → 磁力搜索 → 筛选 → 推 CD2。基线只记录开始追踪时已有的作品/季,所以只会推之后新出现的,不会把历史作品和旧季重推一遍。

筛选有四个维度,全部命中才推;某个维度的名字里认不出来就不推,交给人工判断:

| 维度 | 选项与规则 | settings 键 |
| --- | --- | --- |
| 分辨率 | `2160p` / `1080p` / `720p` / `480p`;从未设置过时默认前两档;认不出不推 | `track_resolutions` |
| 片源 | `REMUX` / `BluRay` / `WEB-DL` / `WEBRip` / `HDTV` / `DVD`,WEBRip 与 WEB-DL 分开统计;留空为不限 | `track_sources` |
| 发布组 | 名字结尾的 `-GROUP`(如 `SPARK`、`NTb`),大小写不敏感;留空为不限 | `track_groups` |
| 大小 | `2160p` 与 `1080p` 各有独立区间(GB),其他分辨率不设区间;留空为不限 | `track_size_{4k,1080}_{min,max}_gb` |

自动推送默认关,不会在没人看着的时候往离线盘推;单条追踪可以在「跟随全局 / 强制开 / 强制关」之间覆盖。筛选在结果侧做,不改磁力搜索关键词(改写关键词会掉召回)。设置入口是 管理 → 追踪页的「推送筛选」「磁力大小范围」两张卡片,对应 API `GET|POST /api/track/settings`。推上去的种子是否开下载,由 **管理 → 下载** 的 qBittorrent 配置决定。

## 命令行脚本(可选)

Web 控制台覆盖日常操作,脚本留给自动化/排障:

```bash
venv/bin/python scripts/search.py --query "盗梦空间 2010"          # 搜磁力(--json 出完整结果)
venv/bin/python scripts/push.py --magnet "magnet:?xt=urn:btih:XXXX" \
    --title "..." --content-type movie --language ja               # 推离线
venv/bin/python scripts/check.py --loop --interval 120             # 轮询完成 + 分类 + 移动
venv/bin/python scripts/organize.py                                # 预览整理计划(只读)
venv/bin/python scripts/organize.py --apply                        # 执行整理
venv/bin/python scripts/organize.py --apply --only "误杀" --limit 5   # 只处理部分
venv/bin/python scripts/finish.py                                  # 刷新 Jellyfin
venv/bin/python scripts/pipeline.py --query "盗梦空间 2010" --auto --wait   # 一键全链路
venv/bin/python scripts/diao_search.py --query "..." --json         # Next-Web 源独立搜索
```

## 回归验证

```bash
venv/bin/python scripts/verify_sync_guard.py        # 同步/可用性守卫 13 用例(只读/临时库)
venv/bin/python scripts/verify_episode_guard.py     # 分集列表守卫 7 用例
venv/bin/python scripts/verify_numbering_guard.py   # 分集缺失「无真实集号不猜」
venv/bin/python scripts/verify_track_filter.py      # 追踪筛选 69 用例
venv/bin/python scripts/verify_group_rank.py        # 前排发布组 27 用例(列表/排序/前后端同序)
venv/bin/python scripts/verify_organize.py          # 整理沙箱, 在 /Temp 内跑, 不碰 /Cloud
venv/bin/python scripts/verify_organize_order.py    # 整理顺序 31 用例(全打桩不连网)
venv/bin/python scripts/verify_settings_save.py     # 设置页保存按钮 14 用例(起临时服务 + 系统 Chrome)
venv/bin/python scripts/verify_classify_regions.py  # 地区档 74 用例(默认行为不变/校验/分类规则 API)
venv/bin/python scripts/verify_region_rules_ui.py   # 分类规则页地区档 UI 30 用例(起临时服务 + 系统 Chrome)
venv/bin/python scripts/verify_nfo_read.py          # 现有 NFO 两条读取通道 + 502 文案 16 用例(连真实 CD2)
venv/bin/python scripts/verify_probe_channels.py   # <fileinfo> 探测两条通道(WebDAV/下载链接)7 用例(连真实 CD2)
venv/bin/python scripts/verify_search_pages.py     # 磁力翻页: 并行抓取/分段窗口/加载更多 31 用例(打桩站点)
venv/bin/python scripts/verify_quality_score.py    # 质量分: 分辨率写实 + 体积合理性 49 用例(打桩, 不连网)
venv/bin/python scripts/verify_trend_filters.py    # 页签/浏览两页高级筛选 104 用例(discover 参数映射、400 校验、缓存键、本地筛选、筛选回显, 全打桩不连网)
venv/bin/python scripts/verify_trend_filters_ui.py # 筛选回显 UI 44 用例(起临时服务 + 系统 Chrome; /api 全走浏览器侧桩, 不连网)
venv/bin/python scripts/audit_jellyfin_sync.py      # 同步审计 8 项(要连 Jellyfin, 约 3min)
node --check server/static/js/*.js                  # 前端语法
```

另有一批按需跑的脚本:`verify_qbit.py` / `verify_qbit_dl.py` / `verify_downloads_error.py`(要 qBittorrent)、`verify_race.py`、`verify_region_detail.py`、`verify_pwa_safearea.py`、`verify_prompt_more.py`、`nfo_ui_verify.py`、`ui_verify*.py`(playwright 截图 + JS 错误检查,需要系统 Chrome,且要先起服务)、`theme_verify.py`、`nav_check*.py`、`trend_toolbar_verify.py`。

## CloudDrive2

### 多个链接用换行分隔

CD2 的 `AddOfflineFiles.urls` 是单个字符串字段,多个链接靠换行 `\n` 拆分。用逗号、空格、分号分隔会被当成一个链接,符号混进磁力链,任务直接失败。

本项目在这里做了限制:`push.py` 默认每个链接单独调用一次,结构上不会合并;另外支持 `--magnet` 重复传、`--magnets-file`(每行一个)、`--stdin` 整段混排自动拆开逐个推。只有显式 `--batch` 才用换行一次推多个,且内部逐个校验合法性。

### 连接方式

候选地址用 `clouddrive2.hosts`(数组,优先)或 `clouddrive2.host`(逗号分隔字符串),按顺序试到通为止;每个地址可带协议头,协议头决定是否走 TLS:

```jsonc
"clouddrive2": {
  "hosts": ["https://nas.example.com:5002", "http://192.168.1.100:19798", "http://localhost:19798"],
  "insecure": true,     // 自签证书跳过校验
  "token": "…",
  "offline_root": "/Temp/Offline",
  "no_delete_paths": ["/Cloud"],
  "staging_dir": "/Temp/.media_auto_stage"
}
```

- 三种写法都支持:`https://nas.example.com:5002`(外网)、`http://192.168.1.100:19798`(内网)、`http://localhost:19798`(同机)。`use_tls` 不写时按协议头自动推断;**一旦显式写了,所有候选都按它处理**,所以「外网 https + 内网明文」混用时不要写。
- 代理环境变量:客户端不再全局清空 `http_proxy`(会连带影响 Jellyfin/TMDB 等 httpx 客户端),grpcio 实测在有代理变量的进程里也能连通。手动排查时建议先 `unset http_proxy https_proxy all_proxy`,以免把代理造成的失败误判成连不上。
- `/Cloud` 只进不删:`delete_files()` 命中保护路径会抛 `PermissionError`。写 NFO 需要覆盖时,`write_file()` 自动改走「临时区 → `MoveFile` Overwrite」,不删库内文件。

离线任务相关接口(`ListAllOfflineFiles` / `GetOfflineQuotaInfo`)必须带云盘名和账号 ID,缺任何一个会报 `cloud account xxx not found`;没带的话会自动从 `GetSubFiles("/")` 发现。

查任务状态不要用 `ListOfflineFilesByPath`:它一次性返回目录下全部任务,任务多时单次可达数十秒(实测 4769 条约 49s),轮询必然超时。项目改用账户级分页接口,单页约 0.4s。其余接口细节见 `doc/clouddrive2/CloudDrive2_gRPC_API_Guide_zh-CN-2.md`(官方 API 指南)。

## 开源协议

本项目采用 [GPL-3.0](LICENSE) 开源协议。你可以自由使用、修改和分发,但衍生作品必须以同样的 GPL-3.0 协议开源,并保留版权声明。

## 致谢

- [CloudDrive2](https://www.clouddrive2.com/):离线下载与文件管理依靠它的 gRPC 接口,本项目的客户端参照官方 API 指南实现,proto 为其原始文件。相关文档存放在 `doc/clouddrive2/`。
- [TMDB](https://www.themoviedb.org/):本项目的元数据(标题、海报、分集、分级)来自 TMDB API,没有它就做不了刮削与分集核对。

  > This product uses the TMDB API but is not endorsed or certified by TMDB.
