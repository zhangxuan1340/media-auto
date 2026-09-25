# media-auto — 影音自动化流水线

一条流水线完成「找片 → 离线下载 → 分类落库 → 刮削 → 刷新」：

```
Bitmagnet(GraphQL 搜磁力) → CloudDrive2(gRPC 离线下载)
   → 轮询完成 + 分类引擎决定目录 + MoveFile 归位
   → TinyMediaManager(刮削重命名) → Jellyfin(刷新媒体库)
```

另外,本项目的 **Web 控制台** 还能把 **Seerr** 与 **Jellyfin** 的数据同步到本地 **SQLite**,
在「本地库」页签里离线浏览、核对,而不必每次都请求远端服务。

完整说明见 `SKILL.md`。

## 目录结构
```
MediaAuto/
├── SKILL.md              # 完整文档(触发词/分类规则/配置/子命令/避坑)
├── README.md            # 本文件
├── requirements.txt     # Python 依赖
├── config.example.json   # 配置模板(复制为 config.json 填真实值)
├── lib/                  # 公共逻辑
│   ├── config.py         # 共享配置加载(脚本 & server 共用)
│   ├── classify.py       # 类型优先于地区的分类引擎 → 决定 /Cloud/<分类>
│   ├── naming.py         # 推广块剥离/广告识别/标题年份抽取/命名模板/匹配度校验
│   ├── mediainfo.py      # 媒体探测(mediainfo / WebDAV+ffprobe) → TMM 质量标记 + <streamdetails>
│   ├── nfo.py            # tinyMediaManager 5.2.12 兼容 NFO 生成(电影 + tvshow)
│   └── state.py          # 队列状态 state/queue.json
├── clients/              # 外部服务客户端(脚本 & server 共用)
│   ├── clouddrive/       # CloudDrive2 客户端 + clouddrive.proto(官方原样拷贝)
│   ├── seerr/            # Seerr 客户端(详情/搜索/反查元数据,同步数据规整)
│   └── jellyfin/         # Jellyfin 客户端
├── db/                   # 本地 SQLite 存储层
│   ├── database.py       # 引擎 / 会话 / 建表
│   ├── models.py         # SQLAlchemy ORM 模型
│   └── repositories.py   # upsert / 查询封装
├── scripts/              # CLI 子命令 + 同步器
│   ├── search.py         # Bitmagnet 搜索(原生 GraphQL)
│   ├── diao_search.py    # Bitmagnet-Next-Web 站点搜索(如 your-site.example.com, REST)
│   ├── push.py           # 推 CD2 离线下载
│   ├── check.py          # 轮询完成 + 分类 + 移动
│   ├── organize.py       # 整理离线目录: 清广告 + 改名 + 写 NFO + 按分类归位 /Cloud/<分类>
│   ├── finish.py         # tMM 刮削 + Jellyfin 刷新
│   ├── pipeline.py       # 一键全链路
│   ├── sync_seerr.py     # 同步 Seerr → 本地 SQLite
│   └── sync_jellyfin.py  # 同步 Jellyfin → 本地 SQLite
├── server/               # FastAPI 网页控制台(单端口)
│   ├── main.py           # 入口: 登录/鉴权 + 托管 index.html + 挂载 API
│   ├── config.py         # 加载 config.json, 暴露项目根目录
│   ├── auth.py           # 用户名/密码 + HttpOnly Cookie 会话
│   ├── routers/          # seerr / search / cd2 / sync 路由(cd2 路由对应 clients/clouddrive)
│   └── static/index.html # 单页前端
├── state/                # 运行时队列目录(gitignored)
└── data/                 # 本地 SQLite(data/media_auto.db, gitignored)
```

## 快速开始(命令行)
```bash
pip install -r requirements.txt
cp config.example.json config.json      # 填入 Bitmagnet / CD2 / Seerr / WebDAV 真实地址与令牌
brew install grpcurl                   # 调 CloudDrive2 必需
brew install mediainfo                 # 可选: 探测媒体信息写 NFO 的 <fileinfo>(有 ffprobe 也行)

python3 scripts/search.py --query "盗梦空间 2010"
python3 scripts/push.py --magnet "magnet:?xt=urn:btih:XXXX" --title "..." --content-type movie --language ja
python3 scripts/check.py --loop --interval 120
python3 scripts/organize.py                    # 预览整理计划(不动数据)
python3 scripts/organize.py --apply            # 执行: 清广告 → 改名 → 写 NFO → 归位 /Cloud/<分类>
python3 scripts/finish.py --all
# 或一键: python3 scripts/pipeline.py --query "盗梦空间 2010" --auto --wait
```

## 整理离线目录(清广告 → 改名 → 归位)

离线下载完成后,目录名往往带着高清站的推广前缀、里面还塞着几百 KB 的广告文件,例如:

```
【高清影视之家发布 www.HDBTHD.com】误杀2[60帧率版本][高码版][国语配音+中文字幕].Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD/
├── Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD.mkv   ← 正片,保留
├── 【更多无水印蓝光原盘请访问 www.HDBTHD.com】【…】.MP4                              ← 广告,删
├── 【更多无水印蓝光电影请访问 www.HDBTHD.com】【…】.DOC                              ← 广告,删
└── 【更多无水印高清电影请访问 www.HDBTHD.com】【…】.MKV                              ← 广告,删
```

`scripts/organize.py` 会把它整理成(顺带写好 NFO,让 TMM/Jellyfin 不用再刮):

```
/Cloud/CnMovie/误杀2 (2021)/
├── 误杀2 (2021) 2160p h265 EAC3.mkv          ← 原片,文件名里的推广前缀已剥掉、技术标记保留
└── 误杀2 (2021) 2160p h265 EAC3.nfo          ← tinyMediaManager 5.2.12 兼容的完整 NFO
```

规则要点:
- **归位目的地** = **`/Cloud/<分类>`**(`organize.cloud_root`,默认 `/Cloud`)。分类走 `lib/classify.py` 的级联
  (`动画 > 纪录片 > 综艺 > 体育 > 音乐 > 地区`),落到 `/Cloud/CnMovie`、`/Cloud/Jp&KrShow` 这类现成目录。
  **`/Cloud` 下只允许移动进来,禁止删除** —— 客户端代码层已拦截。
- **命名**默认 **TMM 风格**:目录 `标题 (年份)`、电影文件 `标题 (年份) 质量`
  (如 `保持沉默 (2019) 2160p h265 EAC3.mp4`),与你库里现有条目一致。
  想用 `标题.年份.ttIMDB` 只需改 `organize.folder_template` 为 `{title}.{year}.{imdb}`。
- **自带刮削**:生成 TMM 5.2.12(JELLYFIN profile)兼容的完整 NFO(电影 `<视频名>.nfo`、剧集 `tvshow.nfo`),
  字段含 `ratings/uniqueid(tmdb,imdb,wikidata)/genre/actor/crew/producer/trailer/fileinfo.streamdetails` 等,
  末尾写 `<tmm_locked/>` 让 TMM 不再改写。**元数据来自 Seerr 反查**:用目录名 + 主媒体文件名去搜,
  中英文名都能查(`Fireflies in the Sun` → 误杀2;`Gannibal` → 噬亡村)。磁力链本身不带 TMDB/IMDB,关联就靠这一步。
- **`<fileinfo>`(编码/分辨率/音轨)探测**按顺序试:① 本地 `local_root` → `mediainfo`;
  ② `config.webdav`(账号根 URL + 账号内路径 + 凭据)→ `ffprobe`;③ 都不通就退回从文件名推断(NFO 不含该段)。
  因为 WebDAV 账号只授到 `/Temp`,**探测在搬运前完成**。
- **广告文件**判定靠"去掉含域名的括号块后没有实际片名"这一特征,不做域名白名单 —— 高清站的域名变体很多
  (HDBTHD / BBEBBB / BBQDDQ / BPHDTV …),但形态一致。正片名里带推广前缀不算广告。
- **非视频杂项**(`.txt/.url/.doc/.pdf` 等)一并删除;但 **`.nfo` 与海报类资产(`poster.jpg`/`fanart.jpg`/…)一律保留**。
  **字幕默认保留**。
- **剧集只改剧总目录名**,季目录与集文件名不动(留给 NAS 上的刮削工具处理)。
- 目标是库里**已存在**的同名条目时判为 `duplicate`:默认不重复归位,但**仍会清掉源目录里的广告**并单列报告。
  其他冲突按 `organize.on_conflict`(`skip` 默认 / `merge` 同剧补季)。
- **默认 dry-run**,`--apply` 才执行;`/Temp` 下删除走 CD2 回收站,可恢复。
- 反查结果会做**匹配度校验**(`lib/naming.py:title_match`),挡住 Seerr 的模糊匹配错条目
  (实例:目录名「国安…」被搜成《国土安全》),匹配度不足则跳过不重命名。

Web 控制台「文件整理」页签提供同样流程: 先预览计划(含要删的广告、新目录名、目标库),再勾选执行。

## 同步 Seerr / Jellyfin 到本地 SQLite
```bash
# 把 Seerr 的缺失剧集 + 热门榜拉到本地库
python3 scripts/sync_seerr.py --scope all
python3 scripts/sync_seerr.py --scope missing      # 仅缺失剧集
python3 scripts/sync_seerr.py --scope popular      # 仅热门

# 把 Jellyfin 的媒体库 + 媒体项拉到本地库
python3 scripts/sync_jellyfin.py --scope all
python3 scripts/sync_jellyfin.py --scope libraries # 仅媒体库
python3 scripts/sync_jellyfin.py --scope items     # 仅媒体项
```
同步结果落在 `data/media_auto.db`(可用环境变量 `MEDIA_AUTO_DB` 覆盖路径)。
本地库表:`seerr_request` / `seerr_episode` / `seerr_popular` / `jellyfin_library` / `jellyfin_item` / `sync_log`。

## Web 控制台(可视化界面)
一个 FastAPI + 单页 HTML 控制台,单端口同时提供 API 与界面：

- 登录(账号来自 `config.json` 的 `web.auth`,可用 `WEB_USER` / `WEB_PASS` 覆盖)
- **缺失剧集 / 热门剧集 / 热门电影**:从 Seerr 拉取,点开看详情与 Bitmagnet 磁力,一键推 CD2
- **文件整理**:预览整理计划(要删的广告/杂项、新目录名、目标 `/Cloud/<分类>`、匹配度),勾选或全部执行;
  执行时会顺带写 NFO。库里已有的条目判为 `duplicate`,不重复归位但会清广告。
- **本地库(新增)**:一键把 Seerr / Jellyfin 同步到本地 SQLite,离线浏览已同步的请求 / 热门 / 媒体库 / 媒体项,
  并展示每次同步的运行状态

启动：
```bash
python -m server.main                 # 读 config.json,端口见 config.web.port(默认 8787)
MEDIA_AUTO_CONFIG=/path/config.json python -m server.main   # 指定配置
# 浏览器打开 http://<host>:8787 ,用 web.auth 的账号登录
```

## 安装 / 启用
1. 安装依赖:`pip install -r requirements.txt`
2. 复制配置:`cp config.example.json config.json`,填入真实地址与令牌
3. 安装 `grpcurl`:`brew install grpcurl`
4. (可选)安装 `mediainfo`:`brew install mediainfo` —— 探测媒体编码/分辨率以写 NFO 的 `<fileinfo>`;
   没装且没配 WebDAV 时,NFO 其余字段照写,只是不含该段。

## ⚠️ 最重要的一条规则(CloudDrive2 多链接分隔)
CD2 的 `AddOfflineFiles.urls` 是**单个字符串字段**,多个链接靠**换行 `\n`** 拆分;
**绝不能用逗号/空格/分号**,否则多个链接被当成一个、符号混进磁力链 → 任务失败。

本项目已堵死此坑:`push.py` 默认**每个链接单独一次调用**(结构上不可能合并),
并支持 `--magnet` 重复传、`--magnets-file`(每行一个)、`--stdin` 整段混排自动拆开逐个推。
只有显式 `--batch` 才用换行一次推多个,且内部逐个校验合法性。详见 `SKILL.md`。

## ⚠️ CloudDrive2 怎么连(第二重要)
`clouddrive2.host` **支持逗号分隔的多个候选地址 + 自动回退**,每个地址可带协议头,**协议头决定是否走 TLS**:

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
  `http://localhost:19798`(同机)。按顺序试到通为止。
- ⚠️ **代理环境变量会干扰 grpcurl** —— 客户端已统一清空 `http_proxy` 等再调用。
  手动用命令行排查时也要先 `unset http_proxy https_proxy all_proxy`,否则会误报 `context deadline exceeded`。
  (早期"5002 是 gRPC-web 连不上"的结论已作废,真凶就是代理。)
- **`/Cloud` 禁止删除**:`delete_files()` 命中保护路径会抛 `PermissionError`。
  写 NFO 需要覆盖时,`write_file()` 自动改走「临时区 → `MoveFile` Overwrite」,不删库内文件。

离线任务相关接口(`ListAllOfflineFiles` / `GetOfflineQuotaInfo`)**必须带云盘名 + 账号 ID**
(缺任一报 `cloud account xxx not found`),否则自动从 `GetSubFiles("/")` 发现。

> 查任务状态**不要**用 `ListOfflineFilesByPath`:它一次性返回目录下全部任务,任务多时单次可达数十秒
> (实测 4769 条约 49s),轮询必超时。项目已改用账户级分页接口(单页约 0.4s)。
> 其余坑位见 `SKILL.md` 的「CloudDrive2 连接与接口避坑」。
