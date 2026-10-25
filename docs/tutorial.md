# 从零跑通教程：qBittorrent 下载完成自动改名入库 Emby

> 本文是**手把手分步教程**：从一台全新的 Windows 机器，到「ani-rss 下载完成 → 自动改名 → 入库 Emby → 钉钉通知」全链路跑通。
> 各配置项的完整含义与进阶用法请看 [README](../README.md)；本文只讲「怎么一步步搭起来、怎么验证每一步、卡住了去哪看」。

## 0. 前置条件与架构总览

开始前请确认你已有：

| 组件 | 说明 |
|---|---|
| Windows 常驻机器 | 脚本在 Windows 上运行（改名工具为 Windows exe） |
| [qBittorrent](https://www.qbittorrent.org/) 4.x/5.x | 下载器，需开启 WebUI |
| [ani-rss](https://github.com/wushuo894/ani-rss) | RSS 追番，负责把种子推给 qB 并打上分类 |
| [Emby](https://emby.media/) | 媒体库（服务器端需能访问） |
| 改名工具 exe | 姊妹仓库 [bangumi-rename-for-emby](https://github.com/DaisySG297/bangumi-rename-for-emby) 的成品，见其 [Releases](https://github.com/DaisySG297/bangumi-rename-for-emby/releases/latest)，**建议 v1.3+** |
| Python 3.8+ | 仅运行脚本用（改名工具本体不需要） |

整条链路的分工：

```
ani-rss ──推送种子──> qBittorrent ──下载完成事件──> 本脚本
                                                     │
                                    ① 转移到工作区(移动或硬链接)
                                    ② 调用改名工具(自动喂 y 确认)
                                    ③ 搜索匹配目标目录(缓存→库内→Bangumi→TMDB→ani-rss)
                                    ④ 年份/季/集三重 TMDB 对齐
                                    ⑤ 移动入库 / 进待归档
                                    ⑥ 钉钉通知(可选)
```

**设计底线**：匹配不到目标目录就绝不入库——文件留在「待归档」目录等你处理，而不是猜个名字建个错目录。

## 1. 目录规划（先想清楚再动手）

脚本对目录有严格要求，规划错是后面一切诡异问题的根源。四个区域**必须分开**：

```
F:\BangumiTools\              ← 工具目录(脚本/exe/日志/工作区/依赖)
F:\下载暂存\                  ← 暂存区(ani-rss 的下载落点, 只允许有下载内容)
E:\Emby Media\Bangumi\        ← 媒体库根(Emby 已索引)
F:\MediaConflictBackup\       ← 冲突备份(绝不能放在媒体库根内!)
```

| 区域 | 存放什么 | 为什么必须分开 |
|---|---|---|
| 工具目录 | 脚本、改名 exe、日志、`_work` 工作区、`_vendor` 依赖 | 工作区在这里构建，与下载无关 |
| 暂存区 | ani-rss 下载的原始文件 | 脚本靠「暂存目录是否清空」判断处理干净与否；混入杂物会误判残留 |
| 媒体库根 | 入库后的成片 | 只允许脚本写入 |
| 冲突备份 | 被替换/去重的旧文件 | 放进媒体库会被 Emby 索引成幽灵条目 |

> ani-rss 的下载目录模板应配置为 `<暂存区>\<番剧名> (年份)\Season N\`，脚本会自动解析这个结构。

## 2. 安装脚本与依赖

```bat
:: 1) 克隆或直接下载脚本
git clone https://github.com/DaisySG297/qb-bangumi-autorename.git F:\BangumiTools\repo
:: 也可以只把 qb_rename_move.py 单文件放到 F:\BangumiTools\

:: 2) 安装繁简转换依赖(二选一)
pip install zhconv
:: 或不污染系统环境, 放到脚本同目录:
::   pip install --target F:\BangumiTools\_vendor zhconv

:: 3) 放入改名工具
:: 从 bangumi-rename-for-emby 的 Releases 下载 bangumi-rename-v1.3.exe,
:: 命名为 番剧批量重命名(字幕版).exe 放进 F:\BangumiTools\
:: (文件名不重要, 路径配到 QBR_RENAME_EXE 即可)
```

验证：

```bat
python F:\BangumiTools\repo\qb_rename_move.py --help
```

不报 `ModuleNotFoundError: zhconv` 即依赖就绪。

## 3. 配置环境变量

脚本**所有配置走环境变量**（前缀 `QBR_`），不改代码。Windows 设置方法（推荐用户级，无需管理员）：

```bat
:: 最小可跑配置(按你的实际路径修改)
setx QBR_LIBRARY_DIRS "E:\Emby Media\Bangumi|F:\Emby Media Part2\Bangumi"
setx QBR_STAGE_DIR "F:\下载暂存"
setx QBR_CONFLICT_DIR "F:\MediaConflictBackup"
setx QBR_RENAME_EXE "F:\BangumiTools\番剧批量重命名(字幕版).exe"
:: 多个库根用 | 分隔, 第一个根用于新建目录
```

再按需补齐下面这些（**都是可选，但建议配**）：

```bat
:: --- qB(删种子任务需要; 不配则种子任务留在 qB 里不删, 功能不受影响) ---
setx QBR_QB_HOST "http://127.0.0.1:8080"
setx QBR_QB_APIKEY "你的qB_WebUI_API_Key"        :: qB: 工具→选项→WebUI→API Key

:: --- Emby(强推荐: 免 TMDB Key 反查首播年/季结构全靠它) ---
setx QBR_EMBY_HOST "http://192.168.1.10:8096"
setx QBR_EMBY_APIKEY "你的Emby_API_Key"          :: Emby: 控制台→API密钥

:: --- ani-rss(可选: 主/备字幕组角色识别) ---
setx QBR_ANIRSS_HOST "http://192.168.1.10:7789"
setx QBR_ANIRSS_KEY "ani-rss的apiKey"

:: --- 钉钉通知(可选) ---
setx QBR_DINGTALK_WEBHOOK "https://oapi.dingtalk.com/robot/send?access_token=xxx"
setx QBR_DINGTALK_SECRET "SECxxx"                :: 安全设置选「加签」时填
```

⚠️ **关键坑：环境变量改完必须重启 qBittorrent**。qB 的 autorun 子进程继承的是 **qB 启动那一刻**的环境变量，`setx` 只对之后新启动的进程生效——改完配置不重启 qB，脚本会拿旧值跑很久。

### 最小可跑判断

只有两个变量是**必须**想的：

- `QBR_LIBRARY_DIRS`——不配就用默认 `D:\Media\Bangumi`，库不在这个路径必然匹配不到；
- `QBR_STAGE_DIR`——必须等于 ani-rss 实际的下载目录，否则脚本找不到文件。

其余（qB/Emby/ani-rss/钉钉/TMDB）都有无伤大雅的降级行为，可以后补。

## 4. 配置 qBittorrent 完成触发

qB：工具 → 选项 → 下载 → 勾选「Torrent 完成时运行外部程序」，填：

```
pythonw.exe C:\path\to\qb_rename_move.py "%N" "%F" "%D" "%L" "%G" "%I"
```

三个必知坑（都来自真实翻车）：

1. **占位符**：`%L` 是**分类**、`%G` 是标签；网上误传把 `%C` 当分类（实为文件数）——传错后脚本收到分类 `"1"`，判定非番剧任务**静默跳过、什么都不发生**。
2. **qB 5.x 的 ini 段层级**：直接手改 `qBittorrent.ini` 时，`[AutoRun]` 必须是**顶层段**。写进 `[Preferences]` 下面会被 Qt 解析成 `Preferences\AutoRun\program`，永远匹配不上，且不报任何错。**建议直接在 GUI 里填**，或用 WebUI API 写入并回读验证。
3. **验证触发别只信配置**：配置好后来一次端到端验证（见下一步）。

### WebUI API 写入法（推荐，可回读验证）

```powershell
$h = @{ 'Authorization' = 'Bearer qbt_你的APIKey' }
$json = '{"autorun_enabled":true,"autorun_program":"C:\\Python312\\pythonw.exe \"C:\\path\\qb_rename_move.py\" \"%N\" \"%F\" \"%D\" \"%L\" \"%G\" \"%I\""}'
$body = 'json=' + [Uri]::EscapeDataString($json)   # 必须 form 形式, raw JSON 无效!
Invoke-RestMethod 'http://127.0.0.1:8080/api/v2/app/setPreferences' -Method Post `
  -Body ([Text.Encoding]::ASCII.GetBytes($body)) -ContentType 'application/x-www-form-urlencoded' -Headers $h
# 回读验证:
(Invoke-RestMethod 'http://127.0.0.1:8080/api/v2/app/preferences' -Headers $h).autorun_program
```

## 5. 首跑验证（别等真番下载完）

### 5.1 演练模式

```bat
python qb_rename_move.py --dry-run
```

演练模式用**硬链接仿真**，不动真实文件、不动 qB。可以先随便喂一个参数跑通流程、看日志格式。

### 5.2 端到端验证（造一个「已完成」种子）

最快的三端验证方法——造一个文件已就位的种子：

1. 在暂存区按 ani-rss 目录结构放一个真实视频：`F:\下载暂存\测试番 (2024)\Season 1\[测试组] 测试番 - 01 [1080P].mp4`
2. 用 qB「添加种子链接/文件」加载一个指向该文件的外部种子，qB 校验后立即 100%；
3. 分类填 `ani-rss`（与 `QBR_CATEGORY` 一致）；
4. 观察**三处**：
   - **qB**：不做种模式下任务在处理开始即被删除（这是 v32 起的正常行为——先释放再搬移，避免 I/O 错误弹窗）；
   - **日志**：`logs\rename_move_YYYY-MM-DD.log`，应有「运行开始 → 开始处理 → 改名工具执行成功 → TMDB 反查 → 入库 → 运行结束 退出码=0」完整段落；
   - **Emby**：媒体库出现改名后的文件（如 `测试番 - S01E01 - 测试组.mp4`），Emby 扫描后元数据齐全。

### 5.3 验证钉钉通知

```bat
python qb_rename_move.py --test-notify
```

收到卡片即通知链路 OK。之后每次运行只推**一条**汇总卡片（成功/跳过/失败 + WARN 明细）。

## 6. 日常运维

### 6.1 待归档补处理

搜索匹配不到目标目录的文件会进 `<工具目录>\待归档\<任务名>\`。常见于：番剧还没在库里建目录、英文发布名 TMDB 反查失败等。**目标目录出现后**补跑：

```bat
python qb_rename_move.py --retry
```

建议挂一个每日计划任务自动重试（任务计划程序 → 每天 1 次 → 上述命令）。

### 6.2 看日志排障

| 位置 | 内容 |
|---|---|
| `logs\rename_move_YYYY-MM-DD.log` | 全流程日志，按天分文件、单日超 2MB 滚动、超 30 天清理 |
| `logs\failures.jsonl` | 失败任务的结构化记录（含改名工具原始输出） |
| `_残留待处理\` | 改名工具失败时保留的完整现场，供人工排查 |
| `MediaConflictBackup\` | 被冲突裁决替换/去重的旧文件（带时间戳和原因后缀） |

排障顺序：先看日志段尾「运行结束 退出码」，退出码非 0 往上找第一个 ERROR；改名失败去看 `_残留待处理` 里的现场文件名是什么形态。

### 6.3 升级脚本

```bat
git -C F:\BangumiTools\repo pull
:: 计划任务/autorun 里指向的脚本路径不变即无缝升级
```

升级后建议翻一遍 README 的「更新记录」，新版可能新增了需要配套的环境变量。

## 7. 运行方式速查

| 命令 | 用途 |
|---|---|
| `python qb_rename_move.py` | qB autorun 正常入口（qB 自动带参数调用，手动裸跑会直接退出） |
| `python qb_rename_move.py --retry` | 重扫 `待归档\` 补归档 |
| `python qb_rename_move.py --dry-run` | 演练：硬链接仿真，不动真实文件、不动 qB |
| `python qb_rename_move.py --seed` | 本次强制做种（硬链接/复制，保留下载目录原文件） |
| `python qb_rename_move.py --no-seed` | 本次强制不做种（直接移动不留副本） |
| `python qb_rename_move.py --test-notify` | 钉钉通知链路自检 |

## 8. 卡住了？对照检查

| 症状 | 最可能原因 | 去哪确认 |
|---|---|---|
| 配了 autorun 但什么都不发生 | 占位符传错（`%C`≠分类）/ ini 段层级错 / 环境变量改了没重启 qB | 日志里有没有「运行开始」段 |
| 日志报「内容路径不存在」 | `QBR_STAGE_DIR` 与 ani-rss 实际下载目录不一致 | 对照 ani-rss 下载设置 |
| 番剧进了 `待归档\` | 库根没配对 / 新番库内无目录且匹配失败 | 日志「搜索匹配」段落 + README「命名规则总览」 |
| qB 弹 I/O 错误(file_open) | 脚本版本 < v32（旧版先搬移后删种的时序问题） | 升级到 v32+ |
| Emby 条目没封面没简介 | 目录年份 ≠ TMDB 首播年 | README「TMDB 对齐规则」+ FAQ 第一条 |
| 改名工具报 `UnicodeEncodeError` | 改名工具版本 < v1.1 | 升级改名工具（脚本也有占位符双保险） |

更多边角案例（幽灵季、本地回声、跨季集号、同名重制版……）见 [README 常见问题](../README.md#常见问题)。
