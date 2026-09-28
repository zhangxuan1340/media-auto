# media-auto — 影音自动化流水线

从搜磁力到刷新媒体库的一条链路:Bitmagnet 搜磁力 → CloudDrive2 离线下载 → 轮询完成后由分类引擎
决定目录并归位 → 整理(清广告、改名、写 NFO)→ 刷新 Jellyfin。

配套的 Web 控制台把 Jellyfin 的媒体库/分集与 TMDB 元数据同步到本地 SQLite,「本地库」页签里可以
离线浏览、核对,不必每次请求远端服务。

## 目录结构
```
MediaAuto/
├── README.md            # 本文件
├── LICENSE              # GPL-3.0(见文末「开源协议」)
├── requirements.txt     # Python 依赖
├── config.example.json  # 配置模板(仅首启播种默认值, 之后配置只在数据库)
├── Dockerfile / .dockerignore / docker-compose.yml   # 容器化部署
├── doc/                 # 官方文档存档(CloudDrive2 gRPC API 指南等)
├── lib/                 # 公共逻辑
│   ├── config.py        # 共享配置加载(脚本 & server 共用)
│   ├── classify.py      # 类型优先于地区的分类引擎, 决定 /Cloud/<分类>
│   ├── naming.py        # 推广块剥离 / 广告识别 / 标题年份抽取 / 命名模板 / 匹配度校验
│   ├── mediainfo.py     # 媒体探测(mediainfo 或 WebDAV+ffprobe) → TMM 质量标记 + <streamdetails>
│   ├── nfo.py           # tinyMediaManager 5.2.12 兼容的 NFO 生成(电影 + tvshow)
│   └── state.py         # 队列状态 state/queue.json
├── clients/             # 外部服务客户端(脚本 & server 共用)
│   ├── clouddrive/      # CloudDrive2 客户端 + clouddrive.proto(官方原样拷贝)
│   ├── jellyfin/        # Jellyfin 客户端
│   ├── tmdb/            # TMDB 客户端(元数据主源)
│   └── qbit/            # qBittorrent 客户端
├── db/                  # 本地 SQLite 存储层
│   ├── database.py      # 引擎 / 会话 / 建表
│   ├── models.py        # SQLAlchemy ORM 模型
│   └── repositories.py  # upsert / 查询封装
├── scripts/             # CLI 子命令 + 同步器
│   ├── search.py        # Bitmagnet 搜索(原生 GraphQL)
│   ├── diao_search.py   # Bitmagnet-Next-Web 站点搜索(改版站, REST)
│   ├── push.py          # 推 CD2 离线下载
│   ├── check.py         # 轮询完成 + 分类 + 移动
│   ├── organize.py      # 整理离线目录: 清广告 → 改名 → 写 NFO → 按分类归位
│   ├── finish.py        # Jellyfin 刷新(tMM 已停用)
│   ├── pipeline.py      # 一键全链路
│   ├── sync_jellyfin.py # 同步 Jellyfin → 本地 SQLite
│   ├── sync_jf_scanner.py # 可用性扫描(写 media / season)
│   ├── sync_tmdb.py     # 同步 TMDB 元数据 → 本地 SQLite
│   ├── track_check.py   # 追踪检查: 演员新作 / 剧集新季 → 搜磁力 → 筛选 → 推 CD2
│   └── verify_*.py      # 回归门禁(见文末「回归验证」)
├── server/              # FastAPI 网页控制台(单端口)
│   ├── main.py          # 入口: 登录 / 鉴权 + 托管 index.html + 挂载 API
│   ├── config.py        # 配置读写(真相源 DB app_config), 暴露项目根目录
│   ├── auth.py          # 用户名/密码 + HttpOnly Cookie 会话
│   ├── routers/         # search / browse / sync / jobs / cd2 等路由
│   └── static/index.html # 单页前端
├── state/               # 运行时队列目录(gitignored)
└── data/                # 本地 SQLite(data/media_auto.db, gitignored)
```

## 快速开始(命令行)
```bash
pip install -r requirements.txt
# 配置只有两个入口: 首次初始化引导 + Web「管理 → 通用」页, 全部存在数据库里
# 首启按 config.example.json 播种默认值, 然后进初始化引导填真实值
# (可选) brew install mediainfo     # 探测媒体信息写 NFO 的 <fileinfo>, 有 ffprobe 也行

python3 scripts/search.py --query "盗梦空间 2010"
python3 scripts/push.py --magnet "magnet:?xt=urn:btih:XXXX" --title "..." --content-type movie --language ja
python3 scripts/check.py --loop --interval 120
python3 scripts/organize.py            # 预览整理计划(不动数据)
python3 scripts/organize.py --apply    # 执行: 清广告 → 改名 → 写 NFO → 归位 /Cloud/<分类>
python3 scripts/finish.py              # 刷新 Jellyfin
# 或一键: python3 scripts/pipeline.py --query "盗梦空间 2010" --auto --wait
```

## 整理离线目录

离线下载完成后,目录名往往带着高清站的推广前缀,里面还塞着几百 KB 的广告文件,例如:

```
【高清影视之家发布 www.HDBTHD.com】误杀2[60帧率版本][高码版][国语配音+中文字幕].Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD/
├── Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD.mkv   ← 正片,保留
├── 【更多无水印蓝光原盘请访问 www.HDBTHD.com】【…】.MP4                              ← 广告,删
├── 【更多无水印蓝光电影请访问 www.HDBTHD.com】【…】.DOC                              ← 广告,删
└── 【更多无水印高清电影请访问 www.HDBTHD.com】【…】.MKV                              ← 广告,删
```

`scripts/organize.py` 会把它整理成下面这样,顺带写好 NFO,TMM 和 Jellyfin 就不用再刮:

```
/Cloud/CnMovie/误杀2 (2021)/
├── 误杀2 (2021) 2160p h265 EAC3.mkv    ← 原片,推广前缀已剥掉, 技术标记保留
└── 误杀2 (2021) 2160p h265 EAC3.nfo    ← tinyMediaManager 5.2.12 兼容的完整 NFO
```

规则:

- 归位目的地是 `/Cloud/<分类>`(`organize.cloud_root`,默认 `/Cloud`)。分类走 `lib/classify.py` 的级联
  (动画 > 纪录片 > 综艺 > 体育 > 音乐 > 地区),落到 `/Cloud/CnMovie`、`/Cloud/Jp&KrShow` 这类现成目录。
  `/Cloud` 下只允许移动进来,禁止删除,客户端代码层会拦(`delete_files()` 抛 `PermissionError`)。
- 命名默认用 TMM 风格:目录 `标题 (年份)`,电影文件 `标题 (年份) 质量`
  (如 `保持沉默 (2019) 2160p h265 EAC3.mp4`),和你库里现有条目一致。
  想换成 `标题.年份.ttIMDB`,把 `organize.folder_template` 改成 `{title}.{year}.{imdb}` 即可。
- 自带刮削:直接生成完整 NFO(电影 `<视频名>.nfo`、剧集 `tvshow.nfo`),Jellyfin 读取即可,
  字段含 `ratings`、`uniqueid`(tmdb/imdb/wikidata)、`genre`、`actor`、`crew`、`producer`、`trailer`、
  `fileinfo.streamdetails` 等。元数据来自 TMDB 反查:用目录名 + 主媒体文件名去搜,中英文名都能查
  (`Fireflies in the Sun` → 误杀2,`Gannibal` → 噬亡村)。磁力链本身不带 TMDB/IMDB,关联就靠这一步。
- `<fileinfo>`(编码/分辨率/音轨)按顺序探测:本地 `local_root` 用 mediainfo;不行就走 `config.webdav`
  (账号根 URL + 账号内路径 + 凭据)用 ffprobe;两条都不通才退回从文件名推断(NFO 不含该段)。
  WebDAV 账号只授到 `/Temp`,所以探测在搬运之前完成。探测失败时,整理日志会写明断在哪一环
  (本地路径不存在 / mediainfo 没装 / WebDAV 没配凭据 / 路径超出 `account_root` / ffprobe 报错),
  NFO 里的 `<fileinfo/>` 才留空,不会静默失败。
- 广告文件按特征判定:去掉含域名的括号块之后没有实际片名就算广告,不做域名白名单。
  高清站的域名变体很多(HDBTHD / BBEBBB / BBQDDQ / BPHDTV …),形态却一致。正片名里带推广前缀不算广告。
- 非视频杂项(`.txt` / `.url` / `.doc` / `.pdf` 等)一并删除;`.nfo` 和海报类资产
  (`poster.jpg` / `fanart.jpg` …)保留,字幕默认保留。
- 剧集只改剧总目录名,季目录与集文件名不动,留给 NAS 上的刮削工具处理。
- 目标是库里已存在的同名条目时判为 `duplicate`:不重复归位,但仍会清掉源目录里的广告并单列报告。
  其他冲突按 `organize.on_conflict`(`skip` 默认 / `merge` 同剧补季)。
- 默认 dry-run,`--apply` 才执行;`/Temp` 下的删除走 CD2 回收站,可恢复。
- 反查结果做匹配度校验(`lib/naming.py:title_match`),防止模糊匹配错条目
  (例如目录名「国安…」被搜成《国土安全》),匹配度不足就跳过改名。

Web 控制台的「文件整理」页签走同样流程:先预览计划(要删的广告、新目录名、目标库),再勾选执行。

## 同步 Jellyfin / TMDB 到本地 SQLite
```bash
# 把 Jellyfin 的媒体库 + 媒体项 + 分集拉到本地库
python3 scripts/sync_jellyfin.py --scope all
python3 scripts/sync_jellyfin.py --scope libraries   # 仅媒体库
python3 scripts/sync_jellyfin.py --scope items       # 仅媒体项

# 可用性扫描(在库/完整/缺失 → 写 media / season)
python3 scripts/sync_jf_scanner.py --mode full       # 全量
python3 scripts/sync_jf_scanner.py --mode recent     # 增量窗口

# TMDB 元数据(标题/海报/分集)缓存
python3 scripts/sync_tmdb.py

# 可用性对账(唯一会把状态改成 DELETED 的作业)
python3 scripts/availability_sync.py --dry           # 先只报告
python3 scripts/availability_sync.py                 # 真写库
```
同步结果落在 `data/media_auto.db`(可用环境变量 `MEDIA_AUTO_DB` 改路径)。
本地库表:`jellyfin_library` / `jellyfin_item` / `jf_episode` / `media` / `season` /
`tmdb_media` / `tmdb_season` / `sync_log`。

## Web 控制台

一个 FastAPI + 单页 HTML 控制台,单端口同时提供 API 与界面:

- 登录,账号存在数据库 `config.web.auth`,可用 `WEB_USER` / `WEB_PASS` 覆盖。
- 缺失 / 热门 / 演员作品:基于本地 TMDB 缓存和可用性表判定,点开看详情与 Bitmagnet 磁力,一键推 CD2。
  分集缺失只按 TMDB 真实集号报缺,拿不到集号就显示「编号未同步」,不做估算;缺失页与浏览页
  滚动加载分页,不会一次铺开全库。
- 追踪:演员出新电影或剧集出新季时,后台搜磁力,按片源、分辨率、发布组、大小筛选,命中才自动推 CD2
  (全局开关默认关,单条追踪可以覆盖),详见下一节。
- 文件整理:预览整理计划(要删的广告/杂项、新目录名、目标 `/Cloud/<分类>`、匹配度),勾选或全部执行,
  执行时顺带写 NFO。库里已有的条目判为 `duplicate`,不重复归位但会清广告。
- 本地库:一键把 Jellyfin / TMDB 同步到本地 SQLite,离线浏览已同步的媒体库、媒体项、分集,
  并展示每次同步的运行状态。
- 作业与缓存:定时同步(近增 / 全量 / 可用性对账)的手动触发与周期编辑,缓存统计与清理。

启动:
```bash
python -m server.main   # 首启用 config.example.json 播种默认值, 端口见配置 web.port(默认 8787)
# 浏览器打开 http://<host>:8787, 登录, 首次初始化引导里改密码并填 Jellyfin / TMDB / CD2 / 库根
```

## 追踪(演员新作、剧集新季)

在演员页点「追踪新作」、剧集详情页点「追踪新季」登记目标。定时作业 `track-check` 默认每 6 小时跑一次
(管理 → 作业可改),也可以手动触发:

```bash
python3 scripts/track_check.py    # 检查全部追踪; 自动推送关着时是干跑, 只记录会推什么
```

流程是:TMDB 对比基线拿新增 → Bitmagnet 搜磁力 → 筛选 → 推 CD2。基线只记录开始追踪时已有的作品/季,
所以只会推之后新出现的,不会把历史作品和旧季重推一遍。

筛选有四个维度,全部命中才推;某个维度的名字里认不出来就不推,交给人工判断:

| 维度 | 选项与规则 | settings 键 |
| --- | --- | --- |
| 分辨率 | `2160p` / `1080p` / `720p` / `480p`;从未设置过时默认前两档;认不出不推 | `track_resolutions` |
| 片源 | `REMUX` / `BluRay` / `WEB-DL` / `WEBRip` / `HDTV` / `DVD`,WEBRip 与 WEB-DL 分开统计;留空为不限 | `track_sources` |
| 发布组 | 名字结尾的 `-GROUP`(如 `SPARK`、`NTb`),大小写不敏感;留空为不限 | `track_groups` |
| 大小 | `2160p` 与 `1080p` 各有独立区间(GB),其他分辨率不设区间;留空为不限 | `track_size_{4k,1080}_{min,max}_gb` |

自动推送默认关,不会在没人看着的时候往离线盘推;单条追踪可以在「跟随全局 / 强制开 / 强制关」之间覆盖。
筛选在结果侧做,不改磁力搜索关键词(改写关键词会掉召回)。设置入口是管理 → 追踪页的
「推送筛选」「磁力大小范围」两张卡片,对应 API `GET|POST /api/track/settings`。

## 回归验证

```bash
venv/bin/python scripts/verify_sync_guard.py       # 同步/可用性守卫 13 用例
venv/bin/python scripts/verify_episode_guard.py    # 分集列表守卫 7 用例
venv/bin/python scripts/verify_numbering_guard.py  # 分集缺失「无真实集号不猜」
venv/bin/python scripts/verify_track_filter.py     # 追踪筛选 69 用例
venv/bin/python scripts/verify_organize.py         # 整理沙箱, 在 /Temp 内跑, 不碰 /Cloud
node --check server/static/js/*.js                 # 前端语法
```

## 安装 / 启用
1. 装依赖:`pip install -r requirements.txt`。
2. 起服务后登录,按首次初始化引导填必填项(配置只存数据库,没有配置文件)。
3. 可选装 `mediainfo`(`brew install mediainfo`),用来探测编码/分辨率写 NFO 的 `<fileinfo>`;
   没装也能正常跑,探测链会降级到 WebDAV + ffprobe,再不行退回文件名推断,
   NFO 其余字段照写,只是不含该段。
4. 起服务:`python -m server.main`,见下一节。

## 部署与使用

配置存在数据库里(SQLite 的 `app_config` 表),运行期不读任何配置文件。配置入口只有两个:
首次初始化引导(第一次)和「管理 → 通用」页(日常),分组表单保存即热加载,不用重启、不用改文件。

### 方式一:本地直接跑
```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -m server.main        # 端口取 config.web.port(默认 8787)
```
浏览器打开 `http://<host>:8787` 登录,默认 `admin` / `change_me`,首次登录进引导改密码;
也可以用 `WEB_USER` / `WEB_PASS` 覆盖账号。

### 方式二:Docker(推荐 NAS)
```bash
docker compose up -d --build
docker compose logs -f
curl -fsS http://localhost:8787/ >/dev/null && echo OK      # GET / 返回 200 即正常
```

旧的 `config.json` 不参与运行(可自行删掉或归档)。首次启动会播种默认值并进入初始化引导,重新填一遍;
想省事,可以先在旧机器上「管理 → 通用 → 导出备份」留档。

| 项 | 值 |
| --- | --- |
| 镜像 | `python:3.12-slim` + `mediainfo` / `ffmpeg`(探测 `<fileinfo>` 用,可缺省) |
| 端口 | `8787:8787`(容器内监听 `0.0.0.0:8787`) |
| 配置 | 数据库(`app_config` 表),容器不内置配置、不挂配置文件 |
| 数据库 | 卷 `./data:/app/data`,SQLite 走 WAL,必须放本地盘,不要放网络盘/对象存储 |
| 图片缓存 | `./data/img_cache`(随 `data` 卷一起持久化) |
| 队列状态 | 卷 `./state:/app/state`(`state/queue.json`) |
| 探活 | `GET /`,本项目没有 `/api/health` |

可用环境变量只有这几个:

| 变量 | 作用 |
| --- | --- |
| `WEB_USER` / `WEB_PASS` | 覆盖数据库里的 `config.web.auth` 登录账号 |
| `MEDIA_AUTO_DIR` | 项目根目录(容器内默认 `/app`,一般不用改) |
| `MEDIA_AUTO_DB` | SQLite 路径(默认 `/app/data/media_auto.db`) |

配置备份走「管理 → 通用 → 导出备份」(含令牌,注意保管),或直接备 `data/media_auto.db`。
恢复配置就是恢复数据库文件,配置没有第二份存储。

容器只承载 Web 控制台和同步作业。整理/搬运要访问的 `/Cloud`、`/Temp` 等媒体目录与 NAS 上的
Jellyfin 是另一条链路:要么在 `docker-compose.yml` 里自己加只读卷挂载,要么按「方式一」在 NAS 上
以进程方式跑流水线脚本。

数据库备份可以停服务直接拷 `data/media_auto.db*`(含 `-wal` / `-shm`),恢复时放回 `data/`:
```bash
# 热备, 不用停服, WAL 会被一并合并
venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/media_auto.db'); d=sqlite3.connect('data/backup.db'); s.backup(d); d.close(); s.close()"
```

## CloudDrive2:多个链接用换行分隔

CD2 的 `AddOfflineFiles.urls` 是单个字符串字段,多个链接靠换行 `\n` 拆分。
用逗号、空格、分号分隔会被当成一个链接,符号混进磁力链,任务直接失败。

本项目在这里做了限制:`push.py` 默认每个链接单独调用一次,结构上不会合并;另外支持 `--magnet`
重复传、`--magnets-file`(每行一个)、`--stdin` 整段混排自动拆开逐个推。只有显式 `--batch`
才用换行一次推多个,且内部逐个校验合法性。

## CloudDrive2:连接方式

`clouddrive2.host` 支持逗号分隔的多个候选地址加自动回退,每个地址可带协议头,协议头决定是否走 TLS:

```jsonc
"clouddrive2": {
  "host": "https://nas.example.com:5002, http://192.168.1.100:19798, http://localhost:19798",
  "insecure": true,     // 自签证书跳过校验
  "token": "…",
  "offline_root": "/Temp/Offline",
  "no_delete_paths": ["/Cloud"],
  "staging_dir": "/Temp/.media_auto_stage"
}
```
- 三种写法都支持:`https://nas.example.com:5002`(外网)、`http://192.168.1.100:19798`(内网)、
  `http://localhost:19798`(同机),按顺序试到通为止。
- 代理环境变量:客户端不再全局清空 `http_proxy`(会连带影响 Jellyfin/TMDB 等 httpx 客户端),
  grpcio 实测在有代理变量的进程里也能连通。手动排查时建议先
  `unset http_proxy https_proxy all_proxy`,以免把代理造成的失败误判成连不上。
  早期「5002 是 gRPC-web 连不上」的结论已作废,原因是代理。
- `/Cloud` 禁止删除:`delete_files()` 命中保护路径会抛 `PermissionError`。
  写 NFO 需要覆盖时,`write_file()` 自动改走「临时区 → `MoveFile` Overwrite」,不删库内文件。

离线任务相关接口(`ListAllOfflineFiles` / `GetOfflineQuotaInfo`)必须带云盘名和账号 ID,
缺任何一个会报 `cloud account xxx not found`;没带的话会自动从 `GetSubFiles("/")` 发现。

查任务状态不要用 `ListOfflineFilesByPath`:它一次性返回目录下全部任务,任务多时单次可达数十秒
(实测 4769 条约 49s),轮询必然超时。项目改用账户级分页接口,单页约 0.4s。
其余接口细节见 `doc/clouddrive2/CloudDrive2_gRPC_API_Guide_zh-CN-2.md`(官方 API 指南)。

## 开源协议

本项目采用 [GPL-3.0](LICENSE) 开源协议。你可以自由使用、修改和分发,但衍生作品必须以同样的
GPL-3.0 协议开源,并保留版权声明。

## 致谢

- [CloudDrive2](https://www.clouddrive2.com/):离线下载与文件管理依靠它的 gRPC 接口,
  本项目的客户端参照官方 API 指南实现,proto 为其原始文件。相关文档存放在 `doc/clouddrive2/`。
- [TMDB](https://www.themoviedb.org/):本项目的元数据(标题、海报、分集、分级)来自 TMDB API,
  没有它就做不了刮削与分集核对。

  > This product uses the TMDB API but is not endorsed or certified by TMDB.
