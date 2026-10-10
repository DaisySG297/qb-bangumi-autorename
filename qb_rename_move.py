r"""
qb_rename_move.py — qBittorrent 番剧下载完成 → 自动改名 → 移动入库 Emby 媒体库

流程:
  1. qBittorrent "下载完成后运行外部程序" 触发本脚本(仅指定分类的任务, 其他种子一律跳过)
  2. 将任务媒体文件转移到独立工作区, 方式可选(TRANSFER_MODE):
       "no_seed" 不做种: 直接移动(暂存不留副本, 种子任务按 SEED_ACTION 处理)
       "seed"    做种:   硬链接(跨盘回退复制), 下载目录原文件保留, qB 继续做种
  3. 在工作区调用外部改名工具(自动喂入 y 确认), 校验输出;
     工具解析不了原始名(如英文点分名 Tokyo.Revengers.S04E02...mkv)时, 按 qB 任务名
     预改写为「[组] 标题 - NN」形态自动重试一次(仍失败则保留现场 + failures.jsonl + 告警)
  4. 搜索匹配目标文件夹(全部库根, 含 NSFW 库根):
       缓存 → 库内已有目录(忽略年份/繁简/标点) → Bangumi 规范名对齐 → ani-rss 目录名回退
     规则 A: 库内已有同一部番剧时, 新一季**对齐既有目录**, 不另建分叉目录
     规则 B: 目录年份**必须对齐 TMDB 首播年**(经 Emby RemoteSearch 反查, 无需 TMDB API Key)。
             年份不符时自动重命名既有目录并同步 tvshow.nfo。
             ⚠ 这是 Emby 能匹配到 TMDB 元数据的前提: 年份错(如第二季年 2026 而首播年 2022)
               会让 Emby 检索 TMDB 得到 0 条结果, 条目退化为无元数据(ProviderIds 为空)。
     规则 C: TMDB 把多 cours 合成单季时(如 2024 版乱马½「本篇」36 集连续编号),
             ani-rss 拆出的多余 Season 自动归并到 TMDB 实际存在的季(集号不变)
     规则 D: ani-rss 用跨季连续集号时(如 S1 共 13 集, S2 第一集被标 S02E14),
             按 TMDB 每季重排编号自动改写为 S02E01
     规则 E: 配置级兜底 SEASON_OVERRIDES = 季归属手工覆写(show/file 双命中 -> 强制归季)
             + ep_offset 绝对集号续排(续作并入 TMDB 合并单季时 E -> E+N),
             用于"ani-rss 季号 / 文件名季号 / TMDB 季结构"三方打架且自动裁决不动时
  5. 季号以 ani-rss(TMDB) 为准, 同步修正文件名 Sxx
  6. 匹配成功 → 移入 <库根>\<文件夹>\Season N\;
     未匹配   → 不入库, 改名后文件移入 <工具目录>\待归档\<任务名>\, 可 --retry 重试
  7. 同集冲突裁决顺序: ① 订阅主/备组角色 → ② 全局组优劣表(优选/未评级/次选)
     → ③ 同组新版(v2 修复版)整套替换 → ④ 同大小去重 / 不同大小旧版移冲突备份。
     主组已在库→备组新文件直接丢弃; 新到主组、库内为备组→整套替换(旧移冲突备份)。
  8. 处理完成后清理暂存中变空的目录; 无法识别残留 → _残留待处理;
     日志按天分段(logs/rename_move_YYYY-MM-DD.log), 文件内再按"一次运行"分段(段头关键词+
     参数, 段尾退出码/用时/事件数); 超 2MB 滚动 .1/.2, 超 30 天自动清理; 失败另记 failures.jsonl
  9. 每次运行收尾可向钉钉群机器人推送**一条汇总卡片**(成功/跳过/失败/重试 + WARN/ERROR 明细),
     由 DINGTALK_* 配置控制; 推送失败只写日志, 不影响入库。自检: python qb_rename_move.py --test-notify

qBittorrent 设置(下载完成后运行外部程序):
  pythonw.exe qb_rename_move.py "%N" "%F" "%D" "%L" "%G" "%I"
  (%L=分类, %G=标签; 注意 %C 是文件数不是分类, 不要用)
用法:
  python qb_rename_move.py            # qB autorun 正常入口
  python qb_rename_move.py --retry    # 重扫 待归档\ 目录补归档
  python qb_rename_move.py --dry-run  # 演练(工作区用硬链接, 不动真实文件)
  python qb_rename_move.py --seed     # 本次运行强制做种(硬链接/复制, 保留源文件)
  python qb_rename_move.py --no-seed  # 本次运行强制不做种(直接移动)
  python qb_rename_move.py --test-notify  # 钉钉通知链路自检
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

# ============================== 配置 ==============================
# 全部个人化配置均可通过环境变量覆盖(见 README), 下方为带示例的默认值。


def _env(name, default=""):
    """环境变量取值; 未设置或空串时用 default"""
    v = os.environ.get(name)
    return v if v else default


def _env_list(name, default=""):
    """按 | 分隔的环境变量列表(去空白、去空项)"""
    return [p.strip() for p in _env(name, default).split("|") if p.strip()]


def _env_flag(name, default=False):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# 工具目录: 脚本 / 改名 exe / 日志 / 工作区 / 依赖 集中于此, 与下载暂存区**严格分开**。
# 为什么必须分开: STAGE_DIR 是 ani-rss 的下载落点, 处理干净时应当只剩下载内容;
# 工具文件混在里面会让「暂存是否清空」无法判断, 也容易被当成残留或误加种子。
TOOLS_DIR = _env("QBR_TOOLS_DIR", os.path.dirname(os.path.abspath(__file__)))
_TOOLS_PARENT = os.path.dirname(TOOLS_DIR.rstrip("\\/")) or TOOLS_DIR
# 改名工具(第三方 exe, 需自备; 只扫描其工作目录, 执行前从 stdin 读入 y 确认)
RENAME_EXE = _env("QBR_RENAME_EXE", os.path.join(TOOLS_DIR, "番剧批量重命名(字幕版).exe"))
# 媒体库根目录列表(多个根用 | 分隔): 匹配既有番剧目录时全部搜索; 新建目录落在第一个根
LIBRARY_DIRS = _env_list("QBR_LIBRARY_DIRS", r"D:\Media\Bangumi")
DEFAULT_LIBRARY_DIR = LIBRARY_DIRS[0]
# NSFW 番剧库根(多个根用 | 分隔): **只参与既有目录匹配**, 新建目录仍落 DEFAULT_LIBRARY_DIR。
# 为什么单独列: 这类内容在 Emby 是独立媒体库, 与普通番剧库分开; 不纳入搜索时,
# 订阅更新的新集会误建进普通番剧库, 同一部番出现两个 Emby 条目
# (实测 2026-10-08: 黑暗召唤师在地下恋爱中 —— NSFW 库 Erai-raws 版 + 番剧库 LoliHouse 版)。
NSFW_LIBRARY_DIRS = _env_list("QBR_NSFW_LIBRARY_DIRS", "")
# 目录匹配范围 = 全部库根(番剧 + NSFW); 新建目录只落 DEFAULT_LIBRARY_DIR
MATCH_LIBRARY_DIRS = LIBRARY_DIRS + NSFW_LIBRARY_DIRS
# 冲突备份(勿放在 Emby 库根内, 否则会被 Emby 索引成幽灵条目)
CONFLICT_DIR = _env("QBR_CONFLICT_DIR", os.path.join(_TOOLS_PARENT, "MediaConflictBackup"))
# 下载暂存根目录(ani-rss 下载落点), 处理完应只剩下载内容
STAGE_DIR = _env("QBR_STAGE_DIR", os.path.join(_TOOLS_PARENT, "下载暂存"))
WORK_DIR = os.path.join(TOOLS_DIR, "_work")
LEFTOVER_DIR = os.path.join(TOOLS_DIR, "_残留待处理")
UNMATCHED_DIR = os.path.join(TOOLS_DIR, "待归档")   # 搜索不到目标文件夹时, 改名后的文件暂存于此
LOG_DIR = os.path.join(TOOLS_DIR, "logs")
CACHE_FILE = os.path.join(LOG_DIR, "bgm_cache.json")
# 运行日志**按天分段**: logs\rename_move_YYYY-MM-DD.log
#   为什么分段: 单文件会跨天持续膨胀(几十天混在一起, 排查时无从下手)。
#   分段规则: ① 每天一个文件; ② 文件内按「一次运行」再分段(段头=分隔线+参数, 段尾=退出码/用时);
#             ③ 单日超过 LOG_MAX_BYTES 自动滚动为 .1/.2; ④ 超期历史自动清理。
LOG_RETAIN_DAYS = 30                # 历史日志保留天数(<=0 表示不自动清理)
LOG_MAX_BYTES = 2 * 1024 * 1024     # 单日日志超过该大小则滚动为 .1 / .2
LOG_ROTATE_KEEP = 2                 # 每天最多保留的分卷数(不含当日正卷)
VENDOR_DIR = os.path.join(TOOLS_DIR, "_vendor")     # 本地依赖(zhconv 繁简转换), 不污染系统环境

# 仅当任务分类属于该集合时才自动处理(其他种子一律跳过, 含手动加标签的)
BANGUMI_CATEGORIES = {_env("QBR_CATEGORY", "ani-rss")}
BANGUMI_TAGS = set()

# 入库后对 qB 中种子任务的处理(仅不做种模式生效):
#   "delete"=删除种子任务(默认) | "pause"=暂停 | "keep"=不动
# 做种模式(TRANSFER_MODE="seed")下本项被忽略, 种子任务一律保留继续上传
SEED_ACTION = _env("QBR_SEED_ACTION", "delete")
# 转移方式可选(做种开关):
#   "no_seed" 不做种: 媒体文件从暂存直接移动到工作区, 不留副本; 入库后种子按 SEED_ACTION 处理
#   "seed"    做种:   工作区用硬链接(同盘瞬时、不占额外空间), 跨盘自动回退为复制;
#                     下载目录原文件保留, qB 继续做种, 入库后不动 qB 种子任务
# 命令行可用 --seed / --no-seed 临时覆盖本配置
TRANSFER_MODE = _env("QBR_TRANSFER_MODE", "no_seed").lower()   # "no_seed" | "seed"
QB_HOST = _env("QBR_QB_HOST", "http://127.0.0.1:8080")
QB_API_KEY = _env("QBR_QB_APIKEY", "")            # qB WebUI API Key(删除种子需要; 留空则跳过)

# ani-rss API(主/备组角色识别): 同集冲突时主组替换备组、备组遇已入库主组直接丢弃。
# 留空 QBR_ANIRSS_HOST/KEY 则跳过角色识别, 退回通用冲突规则(去重/备份)。
# 注意: ani-rss 的 API Key 即 WebUI 的 apiKey(设置页可见)。
ANIRSS_API_BASE = _env("QBR_ANIRSS_HOST", "")
ANIRSS_API_KEY = _env("QBR_ANIRSS_KEY", "")
ANIRSS_ROLES_TTL = 600   # 订阅角色缓存秒数

# --- 钉钉通知(群自定义机器人 Webhook, 可选) ---
# 群里「添加机器人 → 自定义」后取得 Webhook 地址; 安全设置三选一:
#   加签       → 填写 QBR_DINGTALK_SECRET(SEC 开头), 脚本自动附带 timestamp+sign
#   自定义关键词 → 把关键词设为"番剧入库"(消息标题恒含该词, 可与之配套)
#   IP 白名单   → 无需额外配置
# 通知形态: **每次脚本运行汇总为一条卡片**(非逐条刷屏), 运行中的 WARN/ERROR 明细一并附上;
# 发送失败只写日志, 绝不中断或影响入库主流程
DINGTALK_WEBHOOK = _env("QBR_DINGTALK_WEBHOOK", "")
DINGTALK_SECRET = _env("QBR_DINGTALK_SECRET", "")
DINGTALK_ENABLED = _env_flag("QBR_DINGTALK_ENABLED", bool(DINGTALK_WEBHOOK))
DINGTALK_KEYWORD = _env("QBR_DINGTALK_KEYWORD", "番剧入库")   # 消息标题恒含该词
DINGTALK_AT_ALL = _env_flag("QBR_DINGTALK_AT_ALL", False)     # 是否 @所有人
DINGTALK_TIMEOUT = 8           # 单次请求超时(秒); 超时即放弃推送, 不阻塞入库
DINGTALK_INCLUDE_WARN = True   # 卡片附带本次运行的全部 WARN/ERROR 明细
# 推送的事件类型(全部开启): start=开始处理 skip=跳过非番剧 result=入库完成
#                            fail=失败/异常 retry=待归档重试
DINGTALK_EVENTS = {"start", "skip", "result", "fail", "retry"}

USE_BGM_API = True          # 通过 Bangumi API 获取 中文名; 失败时回退原名
BGM_UA = _env("QBR_BGM_UA", "qb-bangumi-autorename/1.0")

# --- TMDB 首播年权威来源(经 Emby 服务端的 RemoteSearch 反查, 无需 TMDB API Key) ---
# 为什么必须用 TMDB: Emby 剧集匹配完全依赖 TMDB 的 "首播年"。若目录/ nfo 里的年份
# 写成"第二季播出的年份"(如 青之芦苇 (2026)), Emby 按该年检索 TMDB 会得到 0 条结果,
# 条目退化为无元数据(ProviderIds 为空, 无封面/简介/剧集信息)。
# 因此目录年份必须对齐 TMDB 上该剧 first_air_date 的年份, 与第几季无关。
USE_TMDB_YEAR = True
EMBY_HOST = _env("QBR_EMBY_HOST", "")
EMBY_API_KEY = _env("QBR_EMBY_APIKEY", "")
TMDB_YEAR_TOLERANCE = 1     # 库内目录年份与 TMDB 首播年相差 > 该值时视为需校正
# 同名不同版作品(如 乱马½ 1989 版 / 2024 重制版)在 TMDB 是独立条目。
# ani-rss 目录年份=该季播出年, 与既有目录(第一季/旧版)年份差超过该值时不视为同一部,
# 不并入既有目录(否则重制版内容会错进旧版目录, Emby 无法匹配分集元数据)。
YEAR_SPLIT_GAP = 5

VIDEO_EXTS = {".mp4", ".mkv", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".rmvb", ".ts", ".m2ts"}
SUB_EXTS = {".ass", ".srt", ".ssa", ".vtt", ".sup", ".mks"}
MEDIA_EXTS = VIDEO_EXTS | SUB_EXTS

FINAL_NAME_RE = re.compile(
    r"^(?P<title>.+?)\s+-\s+S(?P<season>\d{1,2})E(?P<ep>\d{1,4})(?:\s+-\s+(?P<group>.+))?$",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(19|20)\d{2}")
CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十",
          11: "十一", 12: "十二"}
TZ8 = timezone(timedelta(hours=8))
# ==================================================================
# ==================================================================


def now():
    return datetime.now(TZ8)


# ---------------------------- 日志(按天分段) ----------------------------
# 两级分段: ①文件级——每天一份 rename_move_YYYY-MM-DD.log;
#           ②段内——每次运行以分隔线包夹(段头: 参数/模式; 段尾: 退出码/用时/事件数),
#             排查时一次运行自成一段, 不会与其他运行的行混在一起。
_LOG_STATE = {"day": None}
_DAY_LOG_RE = re.compile(r"^rename_move_(\d{4}-\d{2}-\d{2})\.log(?:\.\d+)?$")


def _day_log_path(when=None):
    """当天的分段日志路径(分段的最小单位=天)"""
    return os.path.join(LOG_DIR, f"rename_move_{(when or now()).strftime('%Y-%m-%d')}.log")


def _rotate_day_log(path):
    """单日日志超过 LOG_MAX_BYTES 时滚动为 .1/.2; 只动本脚本自己的日志文件"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= LOG_MAX_BYTES:
            return
    except OSError:
        return
    for i in range(LOG_ROTATE_KEEP, 1, -1):
        src, dst = f"{path}.{i - 1}", f"{path}.{i}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, path + ".1")
    except OSError:
        pass


def _prune_day_logs():
    """清理超期历史日志。严格只匹配 rename_move_日期.log[.n], 不碰 cache/failures 等其他文件"""
    if LOG_RETAIN_DAYS <= 0:
        return []
    cutoff = (now() - timedelta(days=LOG_RETAIN_DAYS)).date()
    removed = []
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return []
    for name in names:
        mm = _DAY_LOG_RE.match(name)
        if not mm:
            continue
        try:
            d = datetime.strptime(mm.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                os.remove(os.path.join(LOG_DIR, name))
                removed.append(name)
            except OSError:
                pass
    return removed


def _prepare_log_file():
    """返回本次写入的日志文件; 首次落盘时做跨天检查(清理超期历史)与超限滚动"""
    os.makedirs(LOG_DIR, exist_ok=True)
    today = now().strftime("%Y-%m-%d")
    if _LOG_STATE.get("day") != today:
        _LOG_STATE["day"] = today
        _prune_day_logs()
    path = _day_log_path()
    _rotate_day_log(path)
    return path


def log(msg, level="INFO"):
    line = f"[{now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line)
    try:
        if _RUN["active"] and level in ("WARN", "ERROR"):
            # 运行期告警入缓冲区, 收尾时随汇总卡片一起推送(避免逐条刷屏)
            _RUN["warns"].append((level, msg))
    except Exception:
        pass
    try:
        with open(_prepare_log_file(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_segment_start(argv=None):
    """一次运行的段头: 分隔线 + 参数/模式(与上一次运行在日志里明确切开)"""
    args = list(sys.argv[1:] if argv is None else argv)
    mode = "做种" if TRANSFER_MODE == "seed" else "不做种"
    tail = " DRY-RUN" if "--dry-run" in args else ""
    log("=" * 72)
    log(f"运行开始 | 模式={mode}{tail} | 参数: {' '.join(args) if args else '(无)'}")


def log_segment_end(rc=None):
    """一次运行的段尾: 退出码/用时/事件与告警数, 便于判断本次运行是否干净"""
    t0 = _RUN.get("t0")
    dur = (time.time() - t0) if t0 else 0.0
    log(f"运行结束 | 退出码={rc} | 用时={dur:.2f}s | "
        f"事件={len(_RUN['events'])} 告警={len(_RUN['warns'])}")
    log("-" * 72)


def log_failure(torrent, reason):
    log(f"处理失败: {torrent} | 原因: {reason}", "ERROR")
    try:
        with open(os.path.join(LOG_DIR, "failures.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": now().isoformat(), "torrent": torrent, "reason": reason},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------- 钉钉通知 ----------------------------
# 设计: 单次脚本运行只发一条汇总卡片(含本次全部事件 + WARN/ERROR 明细)。
# 所有网络异常都在 dingtalk_send 内部消化, 任何失败都不会影响改名/入库主流程。
_RUN = {"active": False, "events": [], "warns": [], "meta": {}, "t0": None}
_DT_ICON = {"start": "🚀", "skip": "⏭️", "result": "✅", "fail": "❌", "retry": "🔁"}


def run_reset():
    """每次运行开始前重置通知缓冲"""
    _RUN.update(active=True, events=[], warns=[], meta={}, t0=time.time())


def notify(event, msg, level="INFO"):
    """登记一条待推送事件(真正发送在 main() 收尾统一 flush)"""
    if not DINGTALK_ENABLED:
        return
    _RUN["events"].append((event, level, msg))


def _dt_signed_url():
    """启用加签时在 Webhook 后附加 timestamp + sign(钉钉自定义机器人安全设置)"""
    if not DINGTALK_SECRET:
        return DINGTALK_WEBHOOK
    ts = str(round(time.time() * 1000))
    digest = hmac.new(DINGTALK_SECRET.encode("utf-8"),
                      f"{ts}\n{DINGTALK_SECRET}".encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in DINGTALK_WEBHOOK else "?"
    return f"{DINGTALK_WEBHOOK}{sep}timestamp={ts}&sign={sign}"


def dingtalk_send(title, text, at_all=None):
    """POST markdown 到钉钉群机器人。返回 (ok, detail); 任何异常只返回失败, 绝不抛出"""
    if not DINGTALK_ENABLED:
        return False, "通知未启用"
    if not DINGTALK_WEBHOOK:
        return False, "未配置 DINGTALK_WEBHOOK"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title[:64], "text": text},
        "at": {"isAtAll": DINGTALK_AT_ALL if at_all is None else bool(at_all)},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _dt_signed_url(), data=data, method="POST",
        headers={"Content-Type": "application/json;charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=DINGTALK_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, f"请求异常: {e}"
    if resp.get("errcode") == 0:
        return True, "ok"
    return False, f"errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}"


def _compose_card():
    """把本次运行的事件/元信息组装成 markdown; 无事件或全被过滤时返回 None"""
    evs = [e for e in _RUN["events"] if e[0] in DINGTALK_EVENTS]
    if not evs:
        return None
    kinds = {e[0] for e in evs}
    if "fail" in kinds:
        head, icon = "失败", "❌"
    elif "result" in kinds:
        head, icon = "完成", "✅"
    elif "retry" in kinds:
        head, icon = "待归档重试", "🔁"
    elif "skip" in kinds:
        head, icon = "跳过", "⏭️"
    else:
        head, icon = "开始", "🚀"
    title = f"{DINGTALK_KEYWORD} · {head}"
    m = _RUN["meta"]
    lines = [f"## {icon} {title}"]
    if m.get("torrent"):
        lines.append(f"**任务**: {m['torrent']}")
    if "category" in m:
        lines.append(f"**分类**: {m.get('category') or '无'}")
    if m.get("target"):
        lines.append(f"**目标**: `{m['target']}`")
    if m.get("counts"):
        lines.append(f"**结果**: {m['counts']}")
    if _RUN["t0"]:
        lines.append(f"**耗时**: {time.time() - _RUN['t0']:.0f}s"
                     + ("　(dry-run 演练)" if m.get("dry_run") else ""))
    lines.append("")
    lines += [f"- {_DT_ICON.get(e, '•')} {msg}" for e, _l, msg in evs]
    if DINGTALK_INCLUDE_WARN and _RUN["warns"]:
        lines += ["", f"**明细 ({len(_RUN['warns'])})**"]
        lines += [f"- {'❌' if lv == 'ERROR' else '⚠️'} {w}" for lv, w in _RUN["warns"][:12]]
    return title, "\n".join(lines)


def notify_flush():
    """运行收尾: 组装并发送汇总卡片"""
    if not DINGTALK_ENABLED:
        return
    if not DINGTALK_WEBHOOK:
        log("钉钉通知: 未配置 DINGTALK_WEBHOOK, 本次跳过推送", "WARN")
        return
    card = _compose_card()
    if not card:
        return
    ok, detail = dingtalk_send(card[0], card[1])
    log("钉钉通知已发送" if ok else f"钉钉通知发送失败: {detail}", "INFO" if ok else "WARN")


# ---------------------------- 并发锁 ----------------------------
def force_rmtree(path):
    """直接用系统调用删除目录树, 不经 shutil.rmtree(本机 sitecustomize 会将其
    猴补丁为回收站删除并可能阻塞), 确保无弹窗、无回收站、不挂起"""
    for root, dirs, files in os.walk(path, topdown=False):
        for fn in files:
            try:
                os.remove(os.path.join(root, fn))
            except OSError:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def acquire_lock(timeout_sec=1800):
    os.makedirs(WORK_DIR, exist_ok=True)
    lock = os.path.join(WORK_DIR, ".lock")
    deadline = time.time() + timeout_sec
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 1800:  # 陈旧锁
                    os.remove(lock)
                    log("移除陈旧锁文件", "WARN")
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise RuntimeError("等待全局锁超时(30分钟)")
            time.sleep(2)


def release_lock(lock):
    try:
        os.remove(lock)
    except OSError:
        pass


# ---------------------------- 任务文件收集 ----------------------------
def wait_content_ready(content_path, max_wait=600):
    """等待文件就绪: 无 .!qB 临时文件, 且大小趋于稳定"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        unstable = False
        for root, _dirs, files in os.walk(content_path):
            for fn in files:
                if fn.endswith(".!qB"):
                    unstable = True
                    break
            if unstable:
                break
        if not unstable:
            time.sleep(5)  # settle
            return True
        time.sleep(10)
    return False


def collect_media_files(content_path):
    """收集本任务的媒体文件(视频+字幕), 返回 [(绝对路径, 相对路径)]"""
    result = []
    if os.path.isfile(content_path):
        if os.path.splitext(content_path)[1].lower() in MEDIA_EXTS:
            result.append((content_path, os.path.basename(content_path)))
        return result
    for root, _dirs, files in os.walk(content_path):
        for fn in files:
            full = os.path.join(root, fn)
            if os.path.splitext(fn)[1].lower() in MEDIA_EXTS and not fn.startswith("._"):
                result.append((full, os.path.relpath(full, content_path)))
    return result


def move_to_workdir(media_files, workdir, dry_run=False, keep_source=False):
    """把任务媒体文件转移到工作区, 返回是否使用了复制(跨盘)。

    keep_source=False(不做种): 直接移动——同盘瞬时 rename, 暂存不留副本
    keep_source=True(做种/演练): 先尝试硬链接(同盘不占额外空间, 下载目录原文件
        保留给 qB 继续做种), 跨盘时回退为 copy2 复制, 绝不动暂存源文件"""
    os.makedirs(workdir, exist_ok=True)
    used_copy = False
    seen = set()
    for full, rel in media_files:
        dst = os.path.join(workdir, os.path.basename(full))
        base, ext = os.path.splitext(dst)
        i = 2
        while dst.lower() in seen or os.path.exists(dst):
            dst = f"{base} ({i}){ext}"
            i += 1
        seen.add(dst.lower())
        try:
            if keep_source:
                try:
                    os.link(full, dst)  # 做种/演练: 硬链接, 不动源文件
                except OSError:
                    shutil.copy2(full, dst)  # 跨盘无法硬链接, 回退复制
                    used_copy = True
            else:
                shutil.move(full, dst)  # 不做种: 直接移动
        except OSError:
            shutil.copy2(full, dst)
            used_copy = True
    return used_copy


def _prune_empty_tree(d):
    """后序遍历删除 d 下所有"只含空目录"的子树, d 自身为空则一并删除。
    返回删除的目录个数。绝不删除任何含文件的目录。"""
    removed = 0
    try:
        entries = os.listdir(d)
    except OSError:
        return 0
    for name in entries:
        p = os.path.join(d, name)
        if os.path.isdir(p) and not os.path.islink(p):
            removed += _prune_empty_tree(p)
    try:
        if not os.listdir(d):
            os.rmdir(d)
            removed += 1
    except OSError:
        pass
    return removed


def prune_empty_dirs(start_dirs):
    """把因文件移走而变空的暂存子目录(含其内部空子目录树)逐级向上删除,
    不越过 STAGE_DIR 本身。"""
    removed = []
    seen = set()
    stage = os.path.abspath(STAGE_DIR)
    for d in start_dirs:
        d = os.path.abspath(d)
        while d and d not in seen:
            seen.add(d)
            if not d.startswith(stage + os.sep):
                break
            if os.path.isdir(d):
                n = _prune_empty_tree(d)
                if n:
                    removed.append(d)
                if os.path.isdir(d):
                    break           # d 自身仍非空, 不再向上
            else:
                break
            d = os.path.dirname(d)
    return removed


def sweep_stage_empty():
    """兜底清扫: STAGE_DIR 直属子目录中的空目录树(含历史遗留)。
    只删真正空(递归无任何文件)的目录; 下载中的任务目录必然非空, 不受影响。"""
    removed = []
    try:
        names = os.listdir(STAGE_DIR)
    except OSError:
        return removed
    for n in sorted(names):
        p = os.path.join(STAGE_DIR, n)
        if os.path.isdir(p) and not os.path.islink(p):
            if _prune_empty_tree(p) and not os.path.exists(p):
                removed.append(p)
    return removed


# ---------------------------- 调用改名工具 ----------------------------
# 改名工具(PyInstaller 固化)以 GBK 打印预览输出, 文件名含 ½/♪ 等超集字符时
# 会 UnicodeEncodeError 崩溃(实测 PYTHONUTF8/PYTHONIOENCODING 均无效);
# 而文件操作本身走 Windows Unicode API 是安全的。
# 因此调用工具前先把超集字符替换为 ASCII 占位符 ~UXXXX~, 成功后还原。
# 占位符刻意不含 '%': 工具内部做 %-风格格式化, '%%' 会被吞成 '%'。
_GBK_PLACEHOLDER_RE = re.compile(r"~U([0-9A-Fa-f]{4,6})~")


def sanitize_gbk_unsafe(name):
    """名字中 GBK 无法编码的字符替换为 ASCII 占位符。返回 (新名, 是否替换)"""
    try:
        name.encode("gbk")
        return name, False
    except UnicodeEncodeError:
        pass
    out = []
    for ch in name:
        try:
            ch.encode("gbk")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("~U%04X~" % ord(ch))
    return "".join(out), True


def restore_gbk_placeholders(name):
    """占位符还原为原字符"""
    return _GBK_PLACEHOLDER_RE.sub(lambda m: chr(int(m.group(1), 16)), name)


def _rename_with_suffix_guard(old, new):
    """带重名保护的 rename, 撞名时追加序号"""
    if not os.path.exists(new):
        os.rename(old, new)
        return new
    base, ext = os.path.splitext(new)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    dst = f"{base} ({i}){ext}"
    os.rename(old, dst)
    return dst


def sanitize_workdir_for_tool(workdir):
    """工作区内所有文件/子目录名做 GBK 占位符替换。文件先改, 目录自底向上。
    返回替换的名字个数"""
    count = 0
    paths = []
    for root, dirs, files in os.walk(workdir):
        for fn in files:
            paths.append(("f", os.path.join(root, fn)))
        for d in dirs:
            paths.append(("d", os.path.join(root, d)))
    # 文件先行(父目录名未动, 路径有效); 目录按深度降序(叶子优先)
    for kind, path in [p for p in paths if p[0] == "f"] + \
                      sorted([p for p in paths if p[0] == "d"], key=lambda p: len(p[1]), reverse=True):
        parent, name = os.path.split(path)
        new, changed = sanitize_gbk_unsafe(name)
        if changed:
            _rename_with_suffix_guard(path, os.path.join(parent, new))
            count += 1
    return count


def restore_workdir_names(workdir):
    """工作区内所有名字中的占位符还原为原字符。目录自底向上。返回还原个数"""
    count = 0
    paths = []
    for root, dirs, files in os.walk(workdir):
        for fn in files:
            paths.append(("f", os.path.join(root, fn)))
        for d in dirs:
            paths.append(("d", os.path.join(root, d)))
    for kind, path in [p for p in paths if p[0] == "f"] + \
                      sorted([p for p in paths if p[0] == "d"], key=lambda p: len(p[1]), reverse=True):
        parent, name = os.path.split(path)
        new = restore_gbk_placeholders(name)
        if new != name:
            _rename_with_suffix_guard(path, os.path.join(parent, new))
            count += 1
    return count


def run_renamer(workdir, timeout=600):
    """在 workdir 下调用改名 exe, 自动确认 y, 返回 (成功?, stdout)"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(
            [RENAME_EXE], cwd=workdir, input=b"y\n",
            capture_output=True, timeout=timeout, env=env,
        )
        raw = (p.stdout or b"") + b"\n" + (p.stderr or b"")
        try:
            out = raw.decode("utf-8")
            if "\ufffd" in out:  # 工具实际按 GBK 输出
                out = raw.decode("gbk", errors="replace")
        except UnicodeDecodeError:
            out = raw.decode("gbk", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"改名工具执行超时({timeout}s)"
    except Exception as e:
        return False, f"改名工具启动失败: {e}"

    if "已取消重命名操作" in out:
        return False, "改名工具未获确认(自动 y 注入失败)"
    if "完成！成功重命名" in out or "完成!成功重命名" in out:
        return True, out
    if "未在当前目录及子文件夹中找到可识别的文件" in out:
        # 无可识别文件: 若文件已是最终命名格式(重复触发), 视为成功; 否则失败
        has_final = any(
            FINAL_NAME_RE.match(os.path.splitext(f)[0])
            for _r, _d, fs in os.walk(workdir) for f in fs
        )
        if has_final:
            return True, out + "\n(文件已是最终命名, 跳过改名)"
        return False, "改名工具无法识别工作区中的任何文件"
    return False, "改名工具输出中未找到成功标志 | 输出: " + out[:500]


# ---------------------------- 目标目录解析 ----------------------------
def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---------------------------- 繁简转换 ----------------------------
try:
    sys.path.insert(0, VENDOR_DIR)
    from zhconv import convert as _zh_convert

    def t2s(text):
        """繁体->简体(用于库目录名比对); 失败时原样返回"""
        try:
            return _zh_convert(text or "", "zh-cn")
        except Exception:
            return text or ""
except ImportError:
    def t2s(text):
        return text or ""


def match_existing_folder(title, hint_year=None, season=None):
    """在全部库根中查找同名番剧目录(忽略年份/大小写/繁简/标点空白)。
    hint_year: 参考年份(通常为 ani-rss 目录的该季播出年)。既有目录年份与之相差
    超过 YEAR_SPLIT_GAP 时视为同名不同版(如乱马½ 1989/2024, TMDB 独立条目)。
    season: 任务季号。**季号 > 1 且年份差超限时, 先经 TMDB 仲裁**: 若 TMDB 首播年
    与既有目录年份一致(同一条目), 说明是"长间隔正统续作"(实测: 黑色五叶草 2017 →
    2026 第二季, 相差 9 年), 仍应对齐既有目录; TMDB 不可用或年份不一致才拒配。
    返回 (库根, 目录名) 或 None。"""
    want = _norm_text(t2s(title))
    if not want:
        return None
    for root in MATCH_LIBRARY_DIRS:
        try:
            names = os.listdir(root)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        for name in names:
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            base, dir_year = split_folder_year(name)
            if _norm_text(t2s(base)) == want or _norm_text(t2s(name)) == want:
                if (hint_year and dir_year
                        and abs(int(dir_year) - int(hint_year)) > YEAR_SPLIT_GAP):
                    # 长间隔续作仲裁(仅季号>1, 第一季维持乱马½ 的拆分行为不变):
                    # 既有条目已存在第 N 季、且该季首播年与本次任务参考年一致
                    # => 同一条目的正统续作, 对齐(TMDB 可能另建重复条目, 以库内
                    #    Emby 条目的实际季结构为准; 实测黑色五叶草 2017 条目的
                    #    S2 首播 2026-10-03, 与 ani-rss 参考年 2026 一致)
                    if season is not None and season > 1:
                        sy = _existing_season_year(root, name, season)
                        if sy and abs(sy - int(hint_year)) <= TMDB_YEAR_TOLERANCE:
                            log(f"长间隔续作仲裁对齐: {name} (条目第 {season} 季首播 "
                                f"{sy} 与参考年 {hint_year} 一致, 属同一条目的新一季)")
                            return root, name
                    log(f"跳过同名不同版目录: {name} (年份 {dir_year} 与参考 {hint_year} "
                        f"相差超 {YEAR_SPLIT_GAP} 年, TMDB 视为不同条目)", "WARN")
                    continue
                return root, name
    return None


FRANCHISE_MIN_LEN = 4    # 归一化后公共名最短长度(字符), 低于此不做包含式匹配
FRANCHISE_YEAR_GAP = 30  # 系列包含匹配的年份容忍(长播系列如 JOJO 2012 起播、2026 仍有新季)


def match_franchise_folder(title, hint_year=None, min_len=FRANCHISE_MIN_LEN):
    r"""系列名包含匹配(直接同名匹配失败后的二级匹配)。

    场景: 库内是系列主条目「JOJO的奇妙冒险（2012）」，而新番标题是
    「飙马野郎 JOJO的奇妙冒险 第一赛段」——名字不等, 但库内目录名(去季标记)
    是候选标题的子串, 属同一系列(该系列在 TMDB 是同一剧集的不同季)。此时按
    同一目录归档, 由季归并逻辑(按 TMDB 季名)决定落入哪一季。

    仅在 ①同名匹配/②ani-rss 名匹配/③Bangumi 规范名匹配 均失败后调用。
    年份容忍放宽到 FRANCHISE_YEAR_GAP(长播系列首播年到新季播出年可能相差十年以上),
    多个候选时取公共名最长者, 同长取年份最接近者。返回 (库根, 目录名) 或 None。"""
    want = _norm_text(t2s(title))
    if not want:
        return None
    best = None
    for root in MATCH_LIBRARY_DIRS:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            base, dir_year = split_folder_year(name)
            nb = _norm_text(t2s(base))
            if len(nb) < min_len or nb == want or nb not in want:
                continue
            gap = abs(int(dir_year) - int(hint_year)) if (hint_year and dir_year) else 0
            if gap > FRANCHISE_YEAR_GAP:
                log(f"跳过系列包含匹配: {name} (年份 {dir_year} 与参考 {hint_year} "
                    f"相差 {gap} 年, 判为不同作品)", "WARN")
                continue
            score = (len(nb), -gap)
            if best is None or score > best[2]:
                best = (root, name, score)
    if best:
        if hint_year:
            _b, _y = split_folder_year(best[1])
            if _y and abs(int(_y) - int(hint_year)) > YEAR_SPLIT_GAP:
                log(f"系列包含匹配跨年份(首播 {_y} / 本季参考 {hint_year}): {best[1]}", "WARN")
        return best[0], best[1]
    return None


def resolved_name_hint(title, season):
    """读取解析阶段缓存的规范基础名(Bangumi name_cn 等), 供季名匹配使用。
    优先精确匹配该季, 其次取该标题任意一季的规范名(季号可能已被 ani-rss 校正过)"""
    try:
        cache = load_cache()
    except Exception:
        return None
    t = (title or "").casefold()
    exact = cache.get(f"{t}|S{season}|base")
    if exact:
        return exact
    for k, v in cache.items():
        if k.endswith("|base") and k.startswith(f"{t}|S"):
            return v
    return None


def split_folder_year(folder_name):
    """拆分目录名为 (基础名, 年份或'')。年份分隔支持半角/全角括号
    (库内既有目录实测混用, 如「JOJO的奇妙冒险（2012）」)"""
    m = re.match(r"^(?P<base>.+?)\s*[（(]\s*(?P<year>(?:19|20)\d{2})\s*[)）]\s*$",
                 folder_name or "")
    if m:
        return m.group("base").strip(), m.group("year")
    return (folder_name or "").strip(), ""


def align_folder_year(library_root, folder_name, tmdb_year, tmdb_name=None):
    r"""确保目录名年份 = TMDB 首播年。不一致时**重命名既有目录**并同步 tvshow.nfo。
    返回最终使用的目录名(可能已改名)。这是防止 Emby 因年份不符而匹配不到 TMDB 的关键。
    年份相差超过 YEAR_SPLIT_GAP 时视为同名不同版(改目录会毁掉另一版条目), 不改名仅告警。"""
    base, cur_year = split_folder_year(folder_name)
    if not _valid_year(str(tmdb_year)):
        return folder_name
    tmdb_year = str(tmdb_year)
    if cur_year == tmdb_year:
        return folder_name
    if cur_year and abs(int(cur_year) - int(tmdb_year)) <= TMDB_YEAR_TOLERANCE:
        return folder_name  # 年份接近, 视为同一部, 不折腾
    if cur_year and abs(int(cur_year) - int(tmdb_year)) > YEAR_SPLIT_GAP:
        # 同名不同版(如 乱马½ (1989) vs TMDB 2024 重制版): 目录归属另一个 TMDB 条目, 不动
        log(f"既有目录 {folder_name} 与 TMDB 首播年 {tmdb_year} 相差超 {YEAR_SPLIT_GAP} 年, "
            f"判定为同名不同版条目, 不改名; 内容应另建目录", "WARN")
        return folder_name

    new_folder = f"{base} ({tmdb_year})"
    src = os.path.join(library_root, folder_name)
    dst = os.path.join(library_root, new_folder)
    if not os.path.isdir(src):
        return folder_name
    if os.path.exists(dst):
        log(f"目标目录已存在, 不合并(需人工确认): {dst}", "WARN")
        return folder_name
    try:
        os.rename(src, dst)
        log(f"目录年份按 TMDB 校正: {folder_name} -> {new_folder}"
            + (f" (TMDB名={tmdb_name})" if tmdb_name else ""), "WARN")
        _rewrite_tvshow_year(dst, base, tmdb_year)
        return new_folder
    except OSError as e:
        log(f"目录重命名失败, 保持原名: {folder_name} | {e}", "WARN")
        return folder_name


def _rewrite_tvshow_year(show_dir, title, year):
    """把 tvshow.nfo 的 <year>/<title> 校正为 TMDB 首播年, 避免 Emby 以错误年份检索 TMDB"""
    nfo = os.path.join(show_dir, "tvshow.nfo")
    if not os.path.exists(nfo):
        return
    try:
        with open(nfo, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    original = text
    text = re.sub(r"<year>\s*\d{4}\s*</year>", f"<year>{year}</year>", text)
    if "<year>" not in text:
        text = text.replace("</tvshow>", f"  <year>{year}</year>\n</tvshow>")
    if title:
        text = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", text, count=1)
        text = re.sub(r"<sorttitle>.*?</sorttitle>", f"<sorttitle>{title}</sorttitle>", text, count=1)
    if text != original:
        try:
            with open(nfo, "w", encoding="utf-8") as f:
                f.write(text)
            log(f"tvshow.nfo 年份已校正为 {year}: {nfo}")
        except OSError as e:
            log(f"tvshow.nfo 写入失败: {e}", "WARN")


def bgm_search(keyword):
    url = "https://api.bgm.tv/v0/search/subjects?limit=5"
    body = json.dumps({"keyword": keyword, "filter": {"type": [2]}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": BGM_UA, "Content-Type": "application/json", "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8")).get("data", [])


# ---------------------------- TMDB 权威年份 / 剧集 ID ----------------------------
_tmdb_year_cache = {}


def tmdb_lookup(title, year_hint=None, timeout=25):
    """经 Emby 服务端的 TMDB 远程搜索反查剧集, 返回 (首播年, TMDBId, 规范名) 或 (None, None, None)。

    说明:
      * Emby 内置 TMDB 凭据, 调用 /emby/Items/RemoteSearch/Series 即可免 key 拿到 TMDB 结果;
      * 返回的 ProductionYear 即 TMDB 的 first_air_date 年份, 这正是 Emby 匹配剧集所用的年份;
      * 搜索时**不要**传入错误的 year 提示(如第二季年份), 否则 TMDB 返回 0 条。仅在无结果时
        才带上 year_hint 重试, 用于消歧同名作品;
      * 同名多版本作品(如 乱马½ 1989 版 / 2024 重制版): year_hint 传"该季播出年"(ani-rss
        目录年份), 多结果按与 hint 的年份距离排序, 取时间上最近的版本。"""
    key = title.strip()
    if key in _tmdb_year_cache:
        return _tmdb_year_cache[key]
    if not USE_TMDB_YEAR or not title or not title.strip():
        return None, None, None
    if not EMBY_HOST:
        return None, None, None

    def _search(with_year):
        info = {"Name": title.strip(), "ProviderIds": {}}
        if with_year and year_hint:
            info["Year"] = int(year_hint)
        payload = {"SearchInfo": info, "IncludeDisabledProviders": False}
        req = urllib.request.Request(
            EMBY_HOST.rstrip("/") + "/emby/Items/RemoteSearch/Series",
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-Emby-Token": EMBY_API_KEY, "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")) or []

    def _year_of(it):
        y = it.get("ProductionYear")
        if y:
            return int(y)
        pd = (it.get("PremiereDate") or "")[:4]
        return int(pd) if _valid_year(pd) else None

    try:
        results = _search(with_year=False)
        if not results and year_hint:
            results = _search(with_year=True)
        if not results:
            _tmdb_year_cache[key] = (None, None, None)
            log(f"TMDB 远程搜索无结果: {title}", "WARN")
            return None, None, None

        cands = [(idx, _year_of(it), (it.get("ProviderIds") or {}).get("Tmdb"),
                  it.get("Name") or "", it.get("OriginalTitle") or "")
                 for idx, it in enumerate(results)]
        if year_hint:
            try:
                hint = int(year_hint)
            except (TypeError, ValueError):
                hint = None
            if hint:
                # 年份接近优先; 同距按 TMDB 相关度顺序
                cands.sort(key=lambda c: (abs(c[1] - hint) if c[1] else 10 ** 6, c[0]))
        best = None
        for idx, y, tid, nm, orig in cands:
            if not y:
                continue
            best = (y, tid, nm)
            if textually_confident(title, [nm, orig]):
                break
        if best is None:
            idx, y, tid, nm, _orig = cands[0]
            best = (y, tid, nm)
        _tmdb_year_cache[key] = best
        log(f"TMDB 反查: {title} -> 首播年={best[0]} tmdbId={best[1]} 规范名={best[2]}")
        return best
    except Exception as e:
        log(f"TMDB 远程搜索失败({title}): {type(e).__name__} {e}", "WARN")
        _tmdb_year_cache[key] = (None, None, None)
        return None, None, None


def cn_season_marker(n):
    return f"第{CN_NUM.get(n, n)}季"


def bgm_pick(data, season):
    """按季数匹配度挑选条目"""
    best, best_score = None, -1
    roman = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"}
    for item in data:
        name_cn = (item.get("name_cn") or "").strip()
        name = (item.get("name") or "").strip()
        score = 0
        marker = cn_season_marker(season)
        if season == 1:
            if name_cn and not re.search(r"第[一二三四五六七八九十\d]+[季期]", name_cn):
                score += 2
            if not re.search(rf"\s{roman.get(season, 'I')}\b", name):
                score += 1
        else:
            if marker in name_cn or f"第{season}季" in name_cn:
                score += 3
            if re.search(rf"\s{roman.get(season, '')}\b", name):
                score += 2
        if name_cn:
            score += 1
        if score > best_score:
            best, best_score = item, score
    return best, best_score


def bgm_subject_aliases(subject_id):
    """获取条目 infobox 中的别名列表(常含罗马音/英文名)"""
    url = f"https://api.bgm.tv/v0/subjects/{subject_id}"
    req = urllib.request.Request(url, headers={"User-Agent": BGM_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    texts = [data.get("name", ""), data.get("name_cn", "")]
    for item in data.get("infobox") or []:
        v = item.get("value")
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, list):
            texts.extend(str(x.get("v", "")) for x in v if isinstance(x, dict))
    return [t for t in texts if t]


def _norm_text(s):
    return re.sub(r"[\s\W_]+", "", s or "", flags=re.UNICODE).casefold()


def _latin_tokens(s):
    """显著的拉丁词元(>=3 字符), 用于英文标题的词元重合判定"""
    return {w.casefold() for w in re.findall(r"[A-Za-z0-9]+", s or "") if len(w) >= 3}


def textually_confident(keyword, texts):
    """标题与候选名称的文本相似度校验: 包含关系或相似度达阈值视为可信。

    ⚠ 拉丁文标题必须单独收紧判定(2026-10-08 实测教训): "Black Clover" 与
    "BLACK LAGOON" 仅因共享 "black" 一个词, 序列相似度即达 ~0.64, 旧阈值 0.55
    会让 Bangumi 误配成《黑礁》并把文件错入别人目录。规则:
      * 双方均为拉丁文 -> 相似度 >= 0.82, 或共同显著词元(>=3字符) >= 2 个
        且覆盖率 >= 0.8;
      * 其余(含 CJK)   -> 维持 0.55(CJK 字符密度高, 短串误配风险低)。
    收紧方向的代价是"宁可未匹配进待归档, 也不错归"。"""
    k = _norm_text(keyword)
    if not k:
        return False
    k_latin = k.isascii()
    k_toks = _latin_tokens(keyword)
    for t in texts:
        n = _norm_text(t)
        if not n:
            continue
        if k in n or n in k:
            return True
        ratio = SequenceMatcher(None, k, n).ratio()
        if k_latin and n.isascii():
            if ratio >= 0.82:
                return True
            # 词元重合: 共同显著词元须 >= 2 个且覆盖率 >= 0.8。
            # 只要求 1 个词元会把 "Akame ga Kill" / "Kill la Kill"(共享 kill)
            # 这类不同作品误判为同名(实测教训, 与 Black Clover/BLACK LAGOON 同因)
            common = k_toks & _latin_tokens(t)
            if len(common) >= 2 and \
                    len(common) / min(len(k_toks), len(_latin_tokens(t))) >= 0.8:
                return True
        elif ratio >= 0.55:
            return True
    return False


ANI_RSS_DIR_RE = re.compile(r"^(?P<name>.+?)\s*\((?:19|20)\d{2}\)$")


def ani_rss_layout(content_path):
    r"""解析 ani-rss 建立的结构 "<名称> (年份)\Season N\", 返回 (目录名, 季号);
    非该结构返回 (None, None)。
    例: <暂存目录>\异人旅馆 (2026)\Season 2\a.mp4 -> ("异人旅馆 (2026)", 2)"""
    if not content_path:
        return None, None
    parts = os.path.normpath(content_path).split(os.sep)
    for i, seg in enumerate(parts):
        if i + 1 < len(parts) and ANI_RSS_DIR_RE.match(seg.strip()):
            sm = re.fullmatch(r"Season\s*(\d+)", parts[i + 1].strip(), re.I)
            if sm:
                return seg.strip(), int(sm.group(1))
    return None, None


def rename_season_in_filename(fn, season):
    r"""把 "<标题> - SxxEyy - <发布组>.ext" 中的季号改写为 season, 使目录与文件名一致"""
    base, ext = os.path.splitext(fn)
    m = FINAL_NAME_RE.match(base)
    if not m:
        return fn
    new = f"{m.group('title').strip()} - S{season:02d}E{m.group('ep')}"
    if m.group("group"):
        new += f" - {m.group('group').strip()}"
    return new + ext


_TMDB_SEASON_CACHE = {}


def _emby_series_id(library_root, folder_name):
    """按目录路径查找 Emby 剧集条目, 返回 Id 或 None(带缓存)"""
    ckey = os.path.normcase(os.path.normpath(os.path.join(library_root, folder_name)))
    cache = _TMDB_SEASON_CACHE.setdefault("#series", {})
    if ckey in cache:
        return cache[ckey]
    sid = None
    if USE_TMDB_YEAR and EMBY_HOST and EMBY_API_KEY:
        try:
            want = ckey
            req = urllib.request.Request(
                EMBY_HOST.rstrip("/") + "/emby/Items"
                + "?Recursive=true&IncludeItemTypes=Series&Fields=Path"
                + "&api_key=" + EMBY_API_KEY)
            with urllib.request.urlopen(req, timeout=20) as r:
                items = json.loads(r.read().decode("utf-8")).get("Items", [])
            for it in items:
                p = os.path.normcase(os.path.normpath(it.get("Path") or ""))
                if p and p == want:
                    sid = it.get("Id")
                    break
        except Exception as e:
            log(f"查找 Emby 剧集条目失败({folder_name}): {type(e).__name__} {e}", "WARN")
    cache[ckey] = sid
    return sid


def _existing_season_year(library_root, folder_name, season):
    """查询库内既有目录对应 Emby 条目中第 season 季的首播年份; 查不到返回 None。

    用途: 长间隔续作仲裁——既有条目若已存在第 N 季且其首播年与本次任务的
    参考年(ani-rss 该季播出年)一致, 说明这季就属于该条目(实测: 黑色五叶草
    2017 条目的 Season 2 首播 2026-10-03, 与 ani-rss 的 2026 一致 → 对齐);
    而"同名不同版"(乱马½ 1989 条目的 S2 首播 1989 ≠ 2024 重制版)不一致 → 拒配。"""
    try:
        sid = _emby_series_id(library_root, folder_name)
        if not sid:
            return None
        req = urllib.request.Request(
            EMBY_HOST.rstrip("/") + "/emby/Shows/" + sid + "/Seasons"
            + "?Fields=PremiereDate,IndexNumber&api_key=" + EMBY_API_KEY)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        for s in data.get("Items") or []:
            if s.get("IndexNumber") == int(season):
                pd = (s.get("PremiereDate") or "")[:4]
                return int(pd) if _valid_year(pd) else None
    except Exception as e:
        log(f"查询既有季首播年失败({folder_name} S{season}): {type(e).__name__} {e}", "WARN")
    return None


def tmdb_season_layout(library_root, folder_name):
    """查询 Emby 中该剧的 TMDB 季号列表(反映 TMDB 真实结构)。
    返回季号 set; 条目不存在或查询失败返回 None。结果按目录缓存。"""
    ckey = os.path.normcase(os.path.normpath(os.path.join(library_root, folder_name)))
    if ckey in _TMDB_SEASON_CACHE:
        return _TMDB_SEASON_CACHE[ckey]
    result = None
    sid = _emby_series_id(library_root, folder_name)
    if sid:
        try:
            req = urllib.request.Request(
                EMBY_HOST.rstrip("/") + "/emby/Items"
                + f"?ParentId={sid}&Recursive=true&IncludeItemTypes=Season"
                + "&api_key=" + EMBY_API_KEY)
            with urllib.request.urlopen(req, timeout=20) as r:
                seasons = json.loads(r.read().decode("utf-8")).get("Items", [])
            result = {s.get("IndexNumber") for s in seasons
                      if isinstance(s.get("IndexNumber"), int)}
            log(f"TMDB 季结构(经 Emby): {folder_name} -> {sorted(result)}")
        except Exception as e:
            log(f"查询 TMDB 季结构失败({folder_name}): {type(e).__name__} {e}", "WARN")
    _TMDB_SEASON_CACHE[ckey] = result
    return result


def tmdb_season_episode_counts(library_root, folder_name):
    """查询 Emby 中该剧各 TMDB 季的已收录集数, 返回 {季号: 集数}; 失败返回 None。
    Emby 的分集来自 TMDB, 集号为 TMDB 的"每季重排编号"(S2 从 E1 重新计数)。"""
    ckey = os.path.normcase(os.path.normpath(os.path.join(library_root, folder_name)))
    cache = _TMDB_SEASON_CACHE.setdefault("#epcounts", {})
    if ckey in cache:
        return cache[ckey]
    result = None
    sid = _emby_series_id(library_root, folder_name)
    if sid:
        try:
            req = urllib.request.Request(
                EMBY_HOST.rstrip("/") + "/emby/Items"
                + f"?ParentId={sid}&Recursive=true&IncludeItemTypes=Episode"
                + "&Fields=ParentIndexNumber,IndexNumber&api_key=" + EMBY_API_KEY)
            with urllib.request.urlopen(req, timeout=20) as r:
                eps = json.loads(r.read().decode("utf-8")).get("Items", [])
            counts = {}
            for e in eps:
                sn, en = e.get("ParentIndexNumber"), e.get("IndexNumber")
                if isinstance(sn, int) and isinstance(en, int) and sn >= 1:
                    counts[sn] = max(counts.get(sn, 0), en)
            result = counts or None
            if result:
                log(f"TMDB 各季集数(经 Emby): {folder_name} -> "
                    + str({k: result[k] for k in sorted(result)}))
        except Exception as e:
            log(f"查询 TMDB 分集失败({folder_name}): {type(e).__name__} {e}", "WARN")
    cache[ckey] = result
    return result


def _cjk_runs(s):
    """提取字符串中的连续中日韩文字片段(长度>=2), 用于季名的中文片段比对"""
    return [r for r in re.findall(r"[\u3040-\u30ff\u3400-\u9fff]{2,}", s or "")]


def tmdb_season_names(library_root, folder_name):
    """查询 Emby 中该剧各 TMDB 季的季名, 返回 {季号: 季名}; 失败返回 None(带缓存)。
    季名是判定"这个新季属于 TMDB 哪一季"的关键(如 JOJO 的 Season 6 名为「飙马野郎篇」)。"""
    ckey = os.path.normcase(os.path.normpath(os.path.join(library_root, folder_name)))
    cache = _TMDB_SEASON_CACHE.setdefault("#seasonnames", {})
    if ckey in cache:
        return cache[ckey]
    result = None
    sid = _emby_series_id(library_root, folder_name)
    if sid:
        try:
            req = urllib.request.Request(
                EMBY_HOST.rstrip("/") + "/emby/Items"
                + f"?ParentId={sid}&Recursive=true&IncludeItemTypes=Season"
                + "&Fields=IndexNumber&api_key=" + EMBY_API_KEY)
            with urllib.request.urlopen(req, timeout=20) as r:
                seasons = json.loads(r.read().decode("utf-8")).get("Items", [])
            result = {s.get("IndexNumber"): (s.get("Name") or "")
                      for s in seasons if isinstance(s.get("IndexNumber"), int)}
            if result:
                log(f"TMDB 各季名称(经 Emby): {folder_name} -> "
                    + str({k: result[k] for k in sorted(result)}))
        except Exception as e:
            log(f"查询 TMDB 季名失败({folder_name}): {type(e).__name__} {e}", "WARN")
    cache[ckey] = result
    return result


def pick_season_by_name(library_root, folder_name, hints, exclude=None):
    r"""按季名文本匹配确定"这一季属于 TMDB 哪一季"。

    hints 为候选标题集合(改名标题/ani-rss 名/Bangumi 规范名)。评分规则:
      * CJK 片段包含(取最长命中片段长度, 如「飙马野郎」⊂「飙马野郎篇」= 4 分)
      * 归一化整体相似度 >= 0.6 记 3 分
    仅当最高分 >= 3 且**唯一最高**时返回该季号, 否则返回 None(不猜)。"""
    names = tmdb_season_names(library_root, folder_name)
    if not names:
        return None
    hints = [h for h in (hints or []) if h]
    if not hints:
        return None
    best, best_score, second = None, 0, 0
    for sn, nm in sorted(names.items()):
        if sn == exclude:
            continue
        nn = _norm_text(nm)
        score = 0
        for h in hints:
            for r in _cjk_runs(h):
                if r in (nm or ""):
                    score = max(score, len(r))
            for r in _cjk_runs(nm):
                if r in (h or ""):
                    score = max(score, len(r))
            if score == 0 and nn and SequenceMatcher(None, _norm_text(h), nn).ratio() >= 0.6:
                score = 3
        if score > best_score:
            best, second, best_score = sn, best_score, score
        elif score > second:
            second = score
    if best is not None and best_score >= 3 and best_score > second:
        log(f"季归属按 TMDB 季名匹配: Season {best} (季名「{names[best]}」, 得分 {best_score})")
        return best
    return None


def align_season_to_tmdb(library_root, folder_name, season, fn, src_size=None, abs_ep=None,
                         name_hints=None):
    r"""按 TMDB 季结构对齐 ani-rss 的 cours 拆季。

    背景: ani-rss 按播出 cours 拆季(Season 1/2/3、乃至「第七季」), 而 TMDB 的季划分
    未必一致。判定目标季的优先级:
      ① 单季剧(该剧在 TMDB 只有 Season 1) -> 多 cours 合并到 Season 1;
      ② **按 TMDB 季名匹配**(如库内 JOJO 主条目 Season 6 名为「飙马野郎篇」, 而新番
         标题是「飙马野郎 JOJO的奇妙冒险 第一赛段」-> 归入 Season 6);
      ③ 兜底: ani-rss 季号超出 TMDB 最大季且该季集数能容纳本集 -> 取最大季。

    集号改写: 若原始文件名带有绝对集号提示 abs_ep(如 [01_49] 的 49, 对应
    药屋少女的呢喃 这类 TMDB 单季绝对编号剧), 归并时把 cours 相对集号
    (S03E01)改写为绝对集号(S01E49); 无提示时集号不变(乱马½ 语义)。

    冲突保护: 归并目标已有其他版本(大小不同)时, 照常归并并交由同集号冲突
    检查路由(组优先/同组新版/去重/冲突备份), 不丢数据、不产生幽灵季。
    返回 (season, fn)。"""
    if season is None or season <= 1:
        return season, fn
    idx = tmdb_season_layout(library_root, folder_name)
    if not idx or season in idx:
        return season, fn

    m = FINAL_NAME_RE.match(os.path.splitext(fn)[0])
    cur_ep = int(m.group("ep")) if m and m.group("ep").isdigit() else None

    target, reason = None, ""
    if len(idx) == 1 and 1 in idx:
        target, reason = 1, "该剧在 TMDB 仅一季, 多 cours 合并"
    else:
        picked = pick_season_by_name(
            library_root, folder_name,
            list(name_hints or []) + [folder_name, strip_season_marker(folder_name)],
            exclude=season)
        if picked is not None:
            target, reason = picked, "按 TMDB 季名匹配"
        else:
            mx = max(idx)
            counts = tmdb_season_episode_counts(library_root, folder_name) or {}
            cand_ep = abs_ep if abs_ep is not None else cur_ep
            if season > mx and cand_ep is not None and counts.get(mx, 0) >= cand_ep:
                target, reason = mx, f"ani-rss 季号超出 TMDB 最大季 {mx}, 归入最大季"

    if target is None:
        log(f"TMDB 季结构 {sorted(idx)} 中无 Season {season}, 且无法确定归属季, "
            f"保持原季号 ({folder_name})", "WARN")
        return season, fn

    new_fn = rename_season_in_filename(fn, target)
    if abs_ep is not None and cur_ep is not None and abs_ep != cur_ep \
            and 1 <= abs_ep <= 999:
        old_fn = fn
        new_fn = rename_season_in_filename(rename_episode_in_filename(fn, abs_ep), target)
        log(f"季归并集号改写: {old_fn} -> {new_fn} (原始文件名含绝对集号 {abs_ep})")
    target_path = os.path.join(library_root, folder_name, f"Season {target}", new_fn)
    if os.path.exists(target_path) and os.path.isfile(target_path) \
            and src_size is not None and os.path.getsize(target_path) != src_size:
        # 归并目标已有其他版本: 一律照常归并, 交由同集号冲突检查路由
        # (组优先/同组新版/去重/冲突备份, 不会丢数据)。不再"保持原季号"——
        # 那会在库根产生 TMDB 不存在的幽灵季(实测 乱马½ (2024) Season 3 事故)。
        # 集号本身已由集号守卫(任务名权威)校正, 误配风险由冲突路由兜底。
        log(f"TMDB 无 Season {season}, Season {target} 中 {new_fn} 已存在且大小不同"
            f"(其他版本), 归并后交由同集号冲突检查处理", "WARN")
        return target, new_fn
    log(f"季号按 TMDB 季结构归并: S{season:02d} -> S{target:02d} "
        f"({folder_name} 在 TMDB 无 Season {season}; {reason})")
    return target, new_fn


ABS_HINT_TWO_RE = re.compile(r"[\[(](\d{1,2})[_](\d{1,2})[\])]")
ABS_HINT_ONE_RE = re.compile(r"[\[(](\d{1,2})[\])]")


def extract_abs_episode_hint(basename):
    r"""从 ani-rss 原始文件名提取"绝对集号"提示(可能为 None)。

    实测: [BeanSub][Kusuriya no Hitorigoto S3][01_49][CHS].mp4 -> 49
          (cours 相对集号 01 + 绝对全集号 49, TMDB 单季连续编号剧需要 49)
          [Nekomoe kissaten][Kusuriya no Hitorigoto][49][1080p].mp4 -> 49
    过滤: 仅认纯数字 token(≤99), 排除 [1080p]/[x264_AAC] 等"""
    m = ABS_HINT_TWO_RE.search(basename)
    if m:
        return int(m.group(2))
    vals = {int(x) for x in ABS_HINT_ONE_RE.findall(basename)}
    if len(vals) == 1:
        return vals.pop()
    return None


def rename_episode_in_filename(fn, new_ep):
    r"""把 "<标题> - SxxEyy - <发布组>.ext" 中的集号改写为 new_ep, 其余不动"""
    base, ext = os.path.splitext(fn)
    m = FINAL_NAME_RE.match(base)
    if not m:
        return fn
    new = f"{m.group('title').strip()} - S{m.group('season')}E{new_ep:02d}"
    if m.group("group"):
        new += f" - {m.group('group').strip()}"
    return new + ext


def align_episode_to_tmdb(library_root, folder_name, season, fn, src_size=None):
    r"""集号按 TMDB 每季重排编号对齐(处理 ani-rss 的跨季连续编号)。

    背景: ani-rss 有时给第二季文件用**跨季连续集号**(实测异人旅馆: TMDB S1 共
    13 集, 其 S2 第一集被标为 S02E14=13+1), 而 TMDB/Emby 的季内集号从 1 重排
    (应为 S02E01)。文件名不改, Emby 会按 E14 找分集元数据。

    规则: offset = TMDB 中第 1..N-1 季已收录集数之和。
      * 文件集号 E <= offset            -> 本就是每季重排编号, 不动
      * E > TMDB 第 N 季集数(C_N) 且
        E-offset <= C_N                 -> 判定为连续编号, 改写为 E-offset
      * 其余(介于两者之间无法判定)       -> 保持原样
    附带冲突保护: 目标文件已存在且大小不同时不改写, 告警等人工。
    返回 (season, fn)。"""
    if season is None or season < 1:
        return season, fn
    m = FINAL_NAME_RE.match(os.path.splitext(fn)[0])
    if not m or not m.group("ep").isdigit():
        return season, fn
    ep = int(m.group("ep"))
    counts = tmdb_season_episode_counts(library_root, folder_name)
    if not counts or season not in counts:
        return season, fn
    c_n = counts[season]
    offset = sum(c for s, c in counts.items() if isinstance(s, int) and 1 <= s < season)
    if ep <= offset:
        return season, fn          # 每季重排编号, 无需处理
    if ep <= c_n:
        return season, fn          # E 在 TMDB 该季范围内, 按每季编号理解
    new_ep = ep - offset
    if new_ep < 1:
        log(f"集号无法对齐 TMDB({folder_name} S{season}: E{ep}, offset={offset}), 保持原集号", "WARN")
        return season, fn
    if new_ep > c_n:
        # TMDB 分集按周更新, 下一集到达时 TMDB 可能尚未收录。用本地连续性校验:
        # 上一集(new_ep-1)已以每季编号存在于同季目录 -> 确信是连续编号链, 放行
        sdir = os.path.join(library_root, folder_name, f"Season {season}")
        prev_ok = False
        try:
            for name in os.listdir(sdir):
                mm = FINAL_NAME_RE.match(os.path.splitext(name)[0])
                if mm and mm.group("season") == m.group("season") \
                        and mm.group("ep").isdigit() \
                        and int(mm.group("ep")) == new_ep - 1:
                    prev_ok = True
                    break
        except OSError:
            pass
        if not prev_ok:
            log(f"集号无法对齐 TMDB({folder_name} S{season}: E{ep}, offset={offset}, "
                f"该季 TMDB 集数={c_n}, 且本地无 E{new_ep - 1:02d} 前集), 保持原集号", "WARN")
            return season, fn
    new_fn = rename_episode_in_filename(fn, new_ep)
    if new_fn == fn:
        return season, fn
    target = os.path.join(library_root, folder_name, f"Season {season}", new_fn)
    if os.path.exists(target) and os.path.isfile(target) \
            and src_size is not None and os.path.getsize(target) != src_size:
        log(f"集号对齐目标 {new_fn} 已存在且大小不同, 保持原集号 E{ep}", "WARN")
        return season, fn
    log(f"集号按 TMDB 每季重排编号对齐: S{season:02d}E{ep:02d} -> "
        f"S{season:02d}E{new_ep:02d} (offset={offset}, {folder_name})")
    return season, new_fn


def strip_season_marker(name):
    """剥掉条目标题中的季标记: 杀手旅店 第二季 -> 杀手旅店; アオのハコ Season 2 -> アオのハコ"""
    s = (name or "").strip()
    s = re.sub(r"\s*第[一二三四五六七八九十\d]+[季期]\s*", "", s)
    s = re.sub(r"\s+Season\s*\d+\s*", " ", s, flags=re.I)
    s = re.sub(r"\s+[IVX]{1,5}$", "", s.strip())
    s = s.strip(" -—·：:，,")
    return s.strip() or (name or "").strip()


def has_season_marker(name):
    return bool(re.search(r"第[一二三四五六七八九十\d]+[季期]|Season\s*\d+|\s[IVX]{1,5}$", name or "", re.I))


def _valid_year(y):
    return bool(re.fullmatch(r"(19|20)\d{2}", y or ""))


def bgm_clean_base(name, fallback=""):
    """Bangumi 候选名 -> 目录基础名: 去季标记 + 去**末尾年份括号** + 繁转简。

    年份由 TMDB 首播年统一决定, 故基础名必须剥掉自带的年份, 否则会拼出
    「魔法骑士雷阿斯 (1994) (1994)」这类双年份目录(Bangumi 用 (年份) 区分
    同名重制版, 如 1994 原版 / 2026 重制版)。"""
    s = strip_season_marker((name or "").strip())
    s = t2s(s) or (fallback or "").strip()
    s = re.sub(r"[\s\-_]*[（(]\s*\d{4}\s*[)）]\s*$", "", s).strip()
    return s or (fallback or "").strip()


def bgm_prefer_version(scored, ar_year):
    """同名不同版消歧: 候选规范名(去季标记/去年份括号)相同时, 把年份最接近参考年
    者前置。参考年 = 该季播出年(ani-rss 目录年份), 用于在「1994 原版 / 2026 重制版」
    这类同条目名不同年份的候选中选中正在下载的那一版。无参考年或无同名多版时保持原序。"""
    if not scored or not _valid_year(str(ar_year)):
        return scored

    def _yr(c):
        y = (c.get("date") or "")[:4]
        return int(y) if _valid_year(y) else 0

    groups = {}
    for c in scored:
        nm = re.sub(r"[\s\-_]*[（(]\s*\d{4}\s*[)）]\s*$", "",
                    t2s((c.get("name_cn") or c.get("name") or "").strip()))
        groups.setdefault(_norm_text(strip_season_marker(nm)), []).append(c)
    out = []
    for g in groups.values():
        if len(g) > 1 and len({_yr(c) for c in g}) > 1:
            g = sorted(g, key=lambda c: abs(_yr(c) - int(ar_year)))
        out.extend(g)
    return out


def resolve_show_folder(title, season, torrent_name, content_path=None):
    """搜索匹配目标文件夹, 返回 ((库根, 文件夹名) 或 None, 来源)。

    流程:
      1) 内部解析(resolve_show_folder_raw)得到候选目录;
      2) **统一做 TMDB 首播年校正**: 目录年份必须等于 TMDB 上该剧的首播年,
         否则重命名既有目录并同步 tvshow.nfo;
      3) 返回校正后的目录。

    为什么必须校正年份: Emby 用目录/nfo 中的年份检索 TMDB 匹配剧集。若写成"第二季
    播出年"(如 青之芦苇 (2026) 而 TMDB 首播年为 2022), Emby 检索 TMDB 返回 0 条,
    条目退化为无元数据(ProviderIds 为空, 无封面/简介/剧集信息)。"""
    hit, source = resolve_show_folder_raw(title, season, torrent_name, content_path)
    if hit is None:
        return hit, source

    library_root, folder_name = hit
    if not USE_TMDB_YEAR:
        return hit, source

    # --- TMDB 首播年校正 ---
    base, cur_year = split_folder_year(folder_name)
    tmdb_year, tmdb_id, tmdb_name = None, None, None
    for kw in list(dict.fromkeys([k for k in (base, title, t2s(base), t2s(title)) if k and k.strip()])):
        tmdb_year, tmdb_id, tmdb_name = tmdb_lookup(kw, year_hint=cur_year or None)
        if tmdb_year:
            break
    if tmdb_year:
        new_folder = align_folder_year(library_root, folder_name, tmdb_year, tmdb_name)
        if new_folder != folder_name:
            # 目录已改名: 缓存与命中结果同步更新
            cache = load_cache()
            key = f"{title.casefold()}|S{season}"
            cache[key] = new_folder
            save_cache(cache)
            hit = (library_root, new_folder)
        else:
            # 年份一致, 仍确保 tvshow.nfo 正确(可能曾被写错)
            _rewrite_tvshow_year(os.path.join(library_root, folder_name), base, str(tmdb_year))
        if tmdb_id:
            cache = load_cache()
            cache[f"{title.casefold()}|S{season}|tmdb"] = tmdb_id
            save_cache(cache)
    else:
        log(f"未能从 TMDB 取得首播年, 沿用目录年份: {folder_name}", "WARN")

    return hit, source


def resolve_show_folder_raw(title, season, torrent_name, content_path=None):
    """搜索匹配目标文件夹(未做 TMDB 年份校正), 返回 ((库根, 文件夹名) 或 None, 来源)。
    顺序: 缓存 → 库内已有目录(全部库根, 繁简归一) → Bangumi 规范名对齐/新建
          → ani-rss 目录名回退。均未命中则返回 None(不入库)。
    规则: 库内已有同一部番剧时, 新一季对齐既有目录; 但**同名不同版**(如乱马½
          1989 版 / 2024 重制版, TMDB 独立条目)不并入——以 ani-rss 目录的该季
          播出年为参考, 与既有目录年份差超 YEAR_SPLIT_GAP 即跳过。"""
    cache = load_cache()
    key = f"{title.casefold()}|S{season}"

    # ani-rss 目录年份 = 该季播出年, 作为跨版本判别的参考年份
    ar_folder, ar_season = ani_rss_layout(content_path)
    ar_year = None
    if ar_folder:
        _ar_name, ar_year = split_folder_year(ar_folder)
        ar_year = int(ar_year) if ar_year else None

    if key in cache:
        folder = cache[key]
        hit = match_existing_folder(strip_season_marker(folder), hint_year=ar_year, season=season) or \
              match_existing_folder(split_folder_year(folder)[0], hint_year=ar_year, season=season)
        if hit:
            # 缓存值经重解析指向了不同目录(如污染/目录已改名) -> 回写校正
            if hit[1] != folder:
                cache[key] = hit[1]
                save_cache(cache)
                log(f"缓存条目已校正: {folder} -> {hit[1]}")
            return hit, "缓存"
        # 缓存指向的目录已不存在(被人工改名/删除): 忽略缓存重新解析
        log(f"缓存目录已失效, 重新解析: {folder}", "WARN")

    # ① 库内已有目录: 改名标题直接匹配(全部库根, 繁简/标点归一, 跨版本年份差拒配)
    hit = match_existing_folder(title, hint_year=ar_year, season=season)
    if hit:
        cache[key] = hit[1]
        save_cache(cache)
        log(f"库内已有目录直接匹配: {title} -> {hit[0]}\\{hit[1]}")
        return hit, "库内已有目录"

    # ② ani-rss 目录名直接匹配库内(TMDB 名与发布组译名可能不同)
    ar_title = None
    if ar_folder:
        ar_title = ANI_RSS_DIR_RE.match(ar_folder).group("name").strip()
        hit = match_existing_folder(ar_title, hint_year=ar_year, season=season)
        if hit:
            cache[key] = hit[1]
            save_cache(cache)
            log(f"库内已有目录(经 ani-rss 名 {ar_title})匹配: -> {hit[0]}\\{hit[1]}")
            return hit, "库内已有目录(ani-rss名)"

    # ②c 系列名包含匹配: 库内目录名(去季标记)是候选标题的子串 -> 同系列的季
    #    (如库内「JOJO的奇妙冒险（2012）」vs 候选「飙马野郎 JOJO的奇妙冒险 第一赛段」)
    for _cand in (title, ar_title):
        if not _cand:
            continue
        hit = match_franchise_folder(_cand, hint_year=ar_year)
        if hit:
            cache[key] = hit[1]
            cache[f"{key}|base"] = strip_season_marker(_cand)
            save_cache(cache)
            log(f"库内已有目录(系列名包含匹配): {_cand} -> {hit[0]}\\{hit[1]}")
            return hit, "库内已有目录(系列包含)"

    # ③ Bangumi 解析(原标题失败则繁->简重试) → 规范基础名 → 对齐库内 / 新建
    if USE_BGM_API:
        try:
            data, via = [], None
            for kw in list(dict.fromkeys([k for k in (title, t2s(title)) if k and k.strip()])):
                d = bgm_search(kw)
                if d:
                    data, via = d, kw
                    break
            if not data and season > 1:
                for kw in [f"{title} {cn_season_marker(season)}", f"{t2s(title)} {cn_season_marker(season)}"]:
                    d = bgm_search(kw)
                    if d:
                        data, via = d, kw
                        break
            if not data:
                log(f"Bangumi API 无搜索结果: {title} (S{season})", "WARN")
            else:
                scored = sorted(data, key=lambda it: bgm_pick([it], season)[1], reverse=True)
                # 同名不同版消歧(1994 原版 / 2026 重制版): 参考年为该季播出年
                scored = bgm_prefer_version(scored, ar_year)
                item = None
                for cand in scored:
                    names = [cand.get("name", ""), cand.get("name_cn", "")]
                    if textually_confident(title, names) or textually_confident(t2s(title), names):
                        item = cand
                        break
                if item is None:  # 名称不相似: 查前2个候选的别名(罗马音/英文)再确认
                    for cand in scored[:2]:
                        try:
                            aliases = bgm_subject_aliases(cand.get("id"))
                            if textually_confident(title, aliases) or textually_confident(t2s(title), aliases):
                                item = cand
                                break
                        except Exception:
                            pass
                if item is None:
                    best = scored[0]
                    log(f"Bangumi 搜索有结果但置信度不足, 视为未匹配 | 标题: {title} | "
                        f"最相关候选: {best.get('name_cn') or best.get('name')} (id={best.get('id')})", "WARN")
                else:
                    base = bgm_clean_base(item.get("name_cn") or "", item.get("name") or "")
                    # ③a 对齐库内既有目录(同一部番; 跨版本年份差拒配, 参考该季播出年)
                    hit = match_existing_folder(base, hint_year=ar_year, season=season)
                    if not hit:
                        # 规范名不等但库内目录名是其子串(如「飙马野郎 JOJO的奇妙冒险
                        # 第一赛段」⊃「JOJO的奇妙冒险」) -> 同系列的季, 归入主条目
                        hit = match_franchise_folder(base, hint_year=ar_year)
                        if hit:
                            log(f"系列名包含匹配(规范名): {base} -> {hit[0]}\\{hit[1]}")
                    if hit:
                        cache[key] = hit[1]
                        cache[f"{key}|id"] = item.get("id")
                        cache[f"{key}|base"] = base
                        save_cache(cache)
                        log(f"对齐库内既有目录: 基础名={base} -> {hit[0]}\\{hit[1]} "
                            f"(Bangumi id={item.get('id')}, 查询词={via})")
                        return hit, f"库内对齐(Bangumi:{via})"
                    # ③b 新建目录: 年份优先取 TMDB 首播年(Emby 匹配剧集所用), 其次 Bangumi
                    # hint 用该季播出年(ani-rss): 同名多版本时选时间上最近的 TMDB 条目
                    _hint = ar_year or (item.get("date") or "")[:4]
                    tmdb_y, tmdb_id, tmdb_nm = tmdb_lookup(base or title, year_hint=_hint)
                    year = ""
                    if _valid_year(str(tmdb_y)):
                        year = str(tmdb_y)
                        log(f"新建目录年份取自 TMDB 首播年: {year} (tmdbId={tmdb_id}, 名={tmdb_nm})")
                    if not _valid_year(year) and (season > 1 or has_season_marker(item.get("name_cn") or item.get("name"))):
                        for cand in data:
                            cn, nm = cand.get("name_cn") or "", cand.get("name") or ""
                            if not has_season_marker(cn) and \
                               _norm_text(strip_season_marker(cn) or strip_season_marker(nm)) == _norm_text(base):
                                y = (cand.get("date") or "")[:4]
                                if _valid_year(y):
                                    year, s1_id = y, cand.get("id")
                                    log(f"第一季条目: id={s1_id} date={cand.get('date')} -> 年份 {year}")
                                    break
                    if not _valid_year(year):
                        year = (item.get("date") or "")[:4]
                    if not _valid_year(year):
                        year = str(now().year)
                        log("无法确定首播年, 年份回退为当前年份", "WARN")
                    folder = f"{base} ({year})"
                    cache[key] = folder
                    cache[f"{key}|id"] = item.get("id")
                    cache[f"{key}|base"] = base
                    if tmdb_id:
                        cache[f"{key}|tmdb"] = tmdb_id
                    save_cache(cache)
                    log(f"Bangumi 匹配(高置信): id={item.get('id')} name={item.get('name')} "
                        f"基础名={base} 年份={year} -> {folder}")
                    return (DEFAULT_LIBRARY_DIR, folder), f"Bangumi API({via})"
        except Exception as e:
            log(f"Bangumi API 查询失败: {e}", "WARN")

    # ④ 回退: 沿用 ani-rss 已建立的目录名。注意其年份取自 TMDB 的"该季条目",
    #    同一部番的多季在 TMDB 是独立条目, 年份会漂移(如第二季 2026), 故此处
    #    仅作名称来源, 年份最终由外层 TMDB 首播年校正统一修正。
    if ar_folder:
        cache[key] = ar_folder
        save_cache(cache)
        log(f"Bangumi 未解析成功, 沿用 ani-rss 目录名(年份待 TMDB 校正): {ar_folder}", "WARN")
        return (DEFAULT_LIBRARY_DIR, ar_folder), "ani-rss 目录名(回退)"

    return None, "未匹配"


# ---------------------------- 移动入库 ----------------------------
def safe_subdir(name):
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())[:80] or "unnamed"


def archive_one(src_full, library_root, folder_name, season, fn, dry_run=False):
    r"""移动单个文件到 库\<folder>\Season N\, 带冲突保护。返回 moved/dup/conflict"""
    season_dir = os.path.join(library_root, folder_name, f"Season {season}")
    target = os.path.join(season_dir, fn)
    if os.path.exists(target):
        if os.path.getsize(target) == os.path.getsize(src_full):
            log(f"目标已存在且同大小, 丢弃新文件(去重): {fn}")
            if not dry_run:
                os.remove(src_full)
            return "dup"
        # 大小不同: 旧文件先备份, 再入库新文件, 绝不覆盖
        bdir = os.path.join(CONFLICT_DIR, now().strftime("%Y%m%d_%H%M%S"))
        log(f"同名但大小不同, 旧文件将备份到 {bdir}: {fn}", "WARN")
        if not dry_run:
            os.makedirs(bdir, exist_ok=True)
            shutil.move(target, os.path.join(bdir, fn))
            os.makedirs(season_dir, exist_ok=True)
            shutil.move(src_full, target)
        return "conflict"
    log(f"入库: {library_root}\\{folder_name}/Season {season}/{fn}")
    if not dry_run:
        os.makedirs(season_dir, exist_ok=True)
        shutil.move(src_full, target)
    return "moved"


# ------------------------ 主/备组角色识别(ani-rss) ------------------------
_SUB_ROLES_CACHE = {"ts": 0.0, "by_key": {}}

# --- 字幕组全局优劣(同集冲突的兜底裁决) ---
# ani-rss 的主/备角色是"订阅级"意图, 优先采用; 订阅给不出裁决时(角色判不出, 或
# 两侧同为备组/同为主组——如订阅备组同时挂着 Nix-Raws 与 ANi, 分不出先后),
# 用这张全局表决定同集冲突中谁更优:
#   GROUP_PREFERRED     : 优选组。层级 2, 新到的优选组文件可替换库内任何**非优选**版本
#   (未评级组)           : 层级 1, 高于次选、低于优选(如 Nix-Raws > ANi, 但与绿茶等平级)
#   GROUP_DEPRIORITIZED : 次选组。层级 0, 库内已有任何**非次选**版本时, 新到的次选文件让位
# 两侧同层时不裁决, 回退通用去重规则。
# 当前需求: LoliHouse 优先; Nix-Raws 大于 ANi(实测 2026-10-08 FX戦士久留美 S01E02:
# 订阅主=绿茶, 备=[Nix-Raws, ANi], 两侧均判为"备组"导致兜底被跳过, Nix-Raws 反进备份)。
GROUP_PREFERRED = ["lolihouse"]
GROUP_DEPRIORITIZED = ["ani"]

# --- 季归属手工覆写(cours/季结构错位时的权威兜底, v18) ---
# 背景: ani-rss 以"订阅条目"为单位编季(它的 Season 1 未必是 TMDB 的 Season 1);
# 而 Emby 侧的 TMDB 季数据可能滞后(新季尚未收录, 实测 2026-10-09 网球王子:
# TMDB 官网已有 Season 3「決勝メンバー決定戦」, Emby 查到的季结构只有 {1,2}),
# TMDB 季名匹配也可能无命中 -> 只好用显式配置定夺。
# 命中规则(同时满足才生效):
#   * show 列表任一子串出现在目标库目录名中(t2s+去标点归一化后);
#   * file 列表任一子串出现在该文件的**原始文件名**或 qB 任务名中(同样归一化;
#     原始文件名在改名工具运行前截获, 如 Nix-Raws 的 "...The Prince of Tennis II S03E01...")。
# 命中后本集强制归入 season 季(覆盖 ani-rss 校正与 TMDB 对齐的结果)。
# 未命中任何条目 -> 完全不影响原有流程。
SEASON_OVERRIDES = [
    {
        # 新网球王子 U-17 世界杯系列: 2026 决赛成员决定战 = TMDB Season 3
        # (Season 1=2022 世界杯本体, Season 2=2024 半决赛, 库内既有)
        "show": ["新网球王子 U-17世界杯篇"],
        "file": ["世界杯决赛成员决定战", "决赛成员决定", "決勝メンバー決定戦",
                 "final member selection", "prince of tennis ii s03"],
        "season": 3,
    },
    {
        # 桃源暗鬼: 2026 日光·華嚴瀑布篇续作 = TMDB Season 2 (Season 1=2025 本体)
        # Bangumi 视作独立条目(617948)故 ani-rss 按 S01 编号; TMDB/Emby 以 S2 归档
        "show": ["桃源暗鬼"],
        "file": ["日光华严瀑布篇", "日光华严", "日光華嚴瀑布篇", "日光華嚴",
                 "日光・華厳の滝", "日光華厳", "nikko kegon"],
        "season": 2,
    },
    {
        # 东京复仇者 三天战争篇: 2026-10 第4期(全13话)。TMDB(105009) 是**合并单季**
        # 结构: Season 1 共 55 集, 第4期 E01/E02 续排为 S01E51/E52(库内 orion origin
        # 版已排至 S01E50, 与 TMDB 逐集对齐)。ani-rss/Bangumi 视作独立条目按 S01
        # 相对编号 -> 绝对集号需 +50 续排; 季号保持 1。
        # 实测 2026-10-10: orion E01 被错标 S01E01 撞老番第1集, ToonsHub E02
        # (英文点分名)改名工具识别失败。
        "show": ["东京复仇者"],
        "file": ["三天战争篇", "三天戰爭篇", "santen sensou", "three titans"],
        "season": 1,
        "ep_offset": 50,
    },
]


def _season_override_entry(folder_name, orig_texts):
    r"""按 SEASON_OVERRIDES 找到命中的覆写条目; 未命中返回 None。
    orig_texts: 原始文件名/qB 任务名等可携带季信号的文本集合。"""
    if not SEASON_OVERRIDES or not folder_name:
        return None
    key = _norm_text(t2s(folder_name))
    if not key:
        return None
    texts = [_norm_text(t2s(x)) for x in (orig_texts or []) if x]
    for ov in SEASON_OVERRIDES:
        if not any(_norm_text(t2s(s)) in key for s in ov.get("show", []) if s):
            continue
        for f in ov.get("file", []):
            fn_key = _norm_text(t2s(f))
            if fn_key and any(fn_key in t for t in texts if t):
                return ov
    return None


def season_override_target(folder_name, orig_texts):
    r"""按 SEASON_OVERRIDES 判定本集应强制归入的季号; 未命中返回 None。"""
    ov = _season_override_entry(folder_name, orig_texts)
    return int(ov["season"]) if ov else None


def season_override_ep_offset(folder_name, orig_texts):
    r"""命中 SEASON_OVERRIDES 条目时的集号偏移(续作并入合并单季时把 cours
    相对集号续排为绝对集号); 未命中或未配置返回 0。"""
    ov = _season_override_entry(folder_name, orig_texts)
    return int(ov.get("ep_offset") or 0) if ov else 0


# qB 任务名(ani-rss 生成)中的集号: "... - S01E01 - <组>"。用于校验改名工具的
# 集号解析(实测 "[ANi] 新網球王子 U-17 WORLD CUP...- 01" 的 "U-17" 被当成集 17)。
_TASK_EP_RE = re.compile(r"(?:^|[ \-\[])(?:S\d{1,2})?E(\d{1,3})(?:[ \-\]]|$)", re.IGNORECASE)


def task_episode_hint(torrent_name):
    """从 qB 任务名提取集号提示; 无法可靠提取时返回 None"""
    if not torrent_name:
        return None
    ms = _TASK_EP_RE.findall(torrent_name)
    return int(ms[-1]) if len(ms) == 1 else None


_TITLE_JUNK_RE = re.compile(r"\s+\d{1,4}v\d+[A-Za-z0-9]*(?= - S\d{1,2}E\d{1,4})")


def strip_title_junk(fn):
    r"""剔除标题尾部紧贴 SxxExx 的发布残留碎片(如 ' 01v2AVC')。
    改名工具会把原始文件名里的 [01v2][AVC-8bit] 等标签拼进标题,
    产生 'Seitokai ni mo Ana wa Aru！ 01v2AVC - S01E01 - Sakurato.mp4'。
    仅清理直接位于 ' - SxxExx' 之前的 '数字v数字(+编码字母)' 碎片, 其余不动。"""
    return _TITLE_JUNK_RE.sub("", fn, count=1)


def apply_task_episode_guard(full, fn, torrent_name, dry_run=False):
    r"""集号守卫: 单文件任务的 qB 任务名集号(ani-rss 按 TMDB 生成)权威,
    与改名结果不一致时以任务名为准改写集号(只动 Exx, 季号交给后面的流程)。
    返回 (full, fn)。"""
    hint = task_episode_hint(torrent_name)
    if hint is None:
        return full, fn
    m = FINAL_NAME_RE.match(os.path.splitext(fn)[0])
    if not m or not m.group("ep").isdigit() or int(m.group("ep")) == hint:
        return full, fn
    old_fn = fn
    fn = rename_episode_in_filename(fn, hint)
    log(f"集号按 qB 任务名(E{hint:02d}) 校正: {old_fn} -> {fn} "
        f"(任务名集号权威, 防标题内数字如 U-17 被误当集号)", "WARN")
    if not dry_run and fn != old_fn:
        try:
            new_full = os.path.join(os.path.dirname(full), fn)
            os.rename(full, new_full)
            full = new_full
        except OSError as e:
            log(f"集号校正改名失败, 保持原名: {e}", "WARN")
            return full, old_fn
    return full, fn


# --- 改名工具兜底救援(v23): 工具解析不了的原始名, 用 qB 任务名预改写 ---
# 实测 2026-10-10 东京复仇者 三天战争篇: 原始文件为英文点分名
# "Tokyo.Revengers.S04E02.Be.Left.Behind.the.Times...-ToonsHub.mkv",
# 改名 exe 报"未找到可识别的文件"整体失败。而同任务的 qB 任务名
# "[ToonsHub] 东京复仇者 三天战争篇 - 02 [...]" 是 exe 能解析的形态
# (orion origin 同构任务名此前一直成功) -> 按任务名把文件预改写为
# "[组] 标题 - NN.ext" 再让 exe 重试一次。
_TASK_TRAILING_EP_RES = [
    # "... - S01E02" / "... - 02" / "... - E02"(结尾)
    re.compile(r"(?:^|\s)-\s*(?:S\d{1,2})?E?(\d{1,3})\s*$", re.IGNORECASE),
    # "... [02]" / "... (02)"(结尾)
    re.compile(r"\s[\[(](\d{1,3})[\])]\s*$"),
]


def parse_torrent_title_ep(torrent_name):
    r"""从 qB 任务名解析 (标题, 集号, 字幕组); 无法可靠解析返回 None。
    集号信号口径(多个候选时不猜):
      * 结尾 " - NN" / " - SxxExx" / "[NN]"(纯数字括号标签, 如 orion 的 [01]);
      * 任务名中段唯一的 "SxxExx"(如 ani-rss 名 "乱马½ - S03E25 - 组");
    其余括号标签([1080p]/[H265 AAC] 等)先剔除, 避免截断标题或干扰匹配。"""
    if not torrent_name:
        return None
    base = torrent_name.strip()
    b, ext = os.path.splitext(base)
    if ext.lower() in VIDEO_EXTS or ext.lower() in SUB_EXTS:
        base = b
    m0 = re.match(r"^[\[(]([^\])]+)[\])]\s*", base)
    group = m0.group(1).strip() if m0 else ""
    s = base[m0.end():] if m0 else base
    # 剔除非纯数字的括号标签(保留可能是集号的 [01])
    for t in {t.strip() for t in re.findall(r"[\[(]([^\])]+)[\])]", s)}:
        if not re.fullmatch(r"\d{1,3}", t):
            s = re.sub(r"\s*[\[(]" + re.escape(t) + r"[\])]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # ① 结尾集号信号
    for pat in _TASK_TRAILING_EP_RES:
        ms = list(pat.finditer(s))
        if len(ms) == 1:
            ep = int(ms[0].group(1))
            title = s[:ms[0].start()].strip(" -–—")
            if title and 1 <= ep <= 999:
                return title, ep, group
            break
    # ② 中段唯一的 SxxExx
    ms = list(re.finditer(r"(?:^|\s)S\d{1,2}E(\d{1,3})(?=\s|$)", s, re.IGNORECASE))
    if len(ms) == 1:
        ep = int(ms[0].group(1))
        title = s[:ms[0].start()].strip(" -–—")
        if title and 1 <= ep <= 999:
            return title, ep, group
    return None


def rescue_rename_from_task(workdir, torrent_name):
    r"""改名工具兜底: 工作区内**唯一**视频文件解析不了时, 按 qB 任务名
    构造工具可识别的 '[组] 标题 - NN.ext' 友好名预改写(返回 1)。
    多文件任务无法逐集对号, 不动(返回 0); 任务名解析失败返回 0。"""
    try:
        videos = [f for f in os.listdir(workdir)
                  if os.path.isfile(os.path.join(workdir, f))
                  and os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    except OSError:
        return 0
    if len(videos) != 1:
        return 0
    parsed = parse_torrent_title_ep(torrent_name)
    if not parsed:
        return 0
    title, ep, group = parsed
    ext = os.path.splitext(videos[0])[1] or ".mkv"
    name = f"{title} - {ep:02d}{ext}" if not group else f"[{group}] {title} - {ep:02d}{ext}"
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    dst = os.path.join(workdir, name)
    if os.path.abspath(dst) == os.path.abspath(os.path.join(workdir, videos[0])):
        return 0
    if os.path.exists(dst):
        return 0
    try:
        os.rename(os.path.join(workdir, videos[0]), dst)
    except OSError as e:
        log(f"兜底预改写失败({videos[0]} -> {name}): {e}", "WARN")
        return 0
    log(f"改名工具无法识别原始名, 已按任务名预改写: {videos[0]} -> {name}")
    return 1

# 字幕组"发布标签" ↔ "ani-rss 显示名"对照表。
# ani-rss 的 subgroup/label 用的是**显示名**(如 三明治摆烂组), 而入库文件名里是
# **发布标签**(如 smzase), 二者文本不同, 不加对照表就无法判定主/备角色。
# 键为归一化后的任一侧写法, 值统一为归一化后的规范名; 两侧都注册即可互通。
GROUP_ALIASES = {
    "smzase": "三明治摆烂组",
    "三明治": "三明治摆烂组",
    "nekomoe kissaten": "喵萌奶茶屋",
    "nekomoekissaten": "喵萌奶茶屋",
    "sakurato": "桜都字幕组",
    "sakurato.sub": "桜都字幕组",
    "lolihouse": "lolihouse",
    "ani": "ani",
    "orion origin": "orion origin",
    "orionorigin": "orion origin",
    "green tea": "绿茶字幕组",
    "sweetsub": "sweetsub",
    "nix-raws": "nix-raws",
    "nixraws": "nix-raws",
}
# 规范名 -> 归一化规范名(反向补全, 使显示名也能映射到同一规范名)
_GROUP_CANON = {}
for _a, _b in GROUP_ALIASES.items():
    _na, _nb = _norm_text(_a), _norm_text(_b)
    _GROUP_CANON[_na] = _nb
    _GROUP_CANON[_nb] = _nb


def _group_key(tag):
    """字幕组标签統一化: 别名折叠 + 归一化(繁简/标点/大小写)。无法识别时返回 None"""
    if not tag:
        return None
    t = _norm_text(t2s(str(tag)))
    if not t:
        return None
    return _GROUP_CANON.get(t, t)


# 全局优劣表的归一化键(供 _group_tier 判定; 与 _group_key 同一套归一化口径)
_GROUP_PREF_KEYS = {k for k in (_group_key(g) for g in GROUP_PREFERRED) if k}
_GROUP_LOW_KEYS = {k for k in (_group_key(g) for g in GROUP_DEPRIORITIZED) if k}
GROUP_TIER_PREFERRED, GROUP_TIER_NORMAL, GROUP_TIER_LOW = 2, 1, 0


def _group_tier(tag):
    """字幕组全局优劣层: 2=优选 / 1=未评级 / 0=次选; 标签为空或无法识别时返回 None。
    在 ani-rss 主/备角色**给不出裁决**(判不出, 或两侧同为主/备)时作为兜底;
    订阅明确分出主备时以订阅为准, 不被本表覆盖。"""
    k = _group_key(tag)
    if not k:
        return None
    if k in _GROUP_PREF_KEYS:
        return GROUP_TIER_PREFERRED
    if k in _GROUP_LOW_KEYS:
        return GROUP_TIER_LOW
    return GROUP_TIER_NORMAL


# Emby 生成的侧车文件沿用媒体文件主名再加后缀(如 '... - S01E01 - ANi-thumb.jpg'),
# 直接取末段会得到 'ANi-thumb' 这种**伪组名**: 既认不出该文件的主/备角色, 又会在
# "库内同集是否全为备组"的判定里投出 None, 使整套替换条件失败 —— 实测表现为
# 主组 LoliHouse 的新文件被当成普通冲突丢进备份, 库内备组 ANi 反被保留。
# 这里统一剥离侧车后缀(-thumb/-poster/...)与语言后缀(.chs/.jpn/...)。
_SIDECAR_SUFFIX_RE = re.compile(
    r"(?:[-_.](?:thumb|poster|fanart|backdrop|banner|landscape|clearlogo|logo|disc|menu|theme|"
    r"chs|cht|sc|tc|zh|zho|jpn|jp|eng|en))+$", re.IGNORECASE)


def _fn_group_tag(fn):
    """最终命名文件名中的字幕组标签: '<标题> - SxxEyy - <标签>.ext' -> <标签>
    侧车后缀(-thumb/-poster/.nfo 等)与语言后缀(.chs/.jpn 等)会被剥离, 避免得到伪组名。"""
    m = FINAL_NAME_RE.match(os.path.splitext(fn)[0])
    if not (m and m.group("group")):
        return ""
    return _SIDECAR_SUFFIX_RE.sub("", m.group("group")).strip()


def _group_role(tag, roles):
    """标签在订阅的主/备组中的角色: "main" / "standby" / None(无法识别)"""
    if not roles or not tag:
        return None
    t = _group_key(tag)
    if not t:
        return None
    if roles.get("main") and _group_key(roles["main"]) == t:
        return "main"
    for s in roles.get("standby") or []:
        if s and _group_key(s) == t:
            return "standby"
    return None


def torrent_group_label(torrent_name):
    """ani-rss 命名的种子名 '标题 - SxxEyy - 组名' -> 组名(识别失败返回 None)"""
    m = re.search(r"-\s*S\d{1,2}E\d{1,4}\s*-\s*(?P<g>[^-[\]]+?)\s*$", torrent_name or "")
    return m.group("g").strip() if m else None


def sub_roles(*names):
    """按候选名(标题/TMDB名等)查订阅的主/备组角色; 缓存 ANIRSS_ROLES_TTL 秒。
    返回 {"main": 组名, "standby": [标签...]} 或 None(未订阅/查询失败)。

    匹配策略(逐级放宽): 归一化全等 -> 一方包含另一方(ani-rss 常存全名, 文件名多为简称
    如 '乱世千金' vs '乱世千金倪亚·利斯顿转生为娇弱千金的弑神武人华丽无双录')。
    多候选命中不同订阅时, 以命中数多/名称更长者为佳。"""
    keys = [k for k in (_norm_text(t2s(n)) for n in names if n) if k]
    if not keys:
        return None
    now_ts = time.time()
    if now_ts - _SUB_ROLES_CACHE["ts"] > ANIRSS_ROLES_TTL:
        idx = {}
        try:
            req = urllib.request.Request(
                ANIRSS_API_BASE.rstrip("/") + "/api/listAni", data=b"{}",
                method="POST",
                headers={"X-Api-Key": ANIRSS_API_KEY,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            items = [it for wk in (data.get("data") or {}).get("weekList") or []
                     for it in (wk.get("items") or [])]
            for it in items:
                entry = {"main": (it.get("subgroup") or "").strip() or None,
                         "standby": [s.get("label") for s in (it.get("standbyRssList") or [])
                                     if s.get("label")]}
                for k in (_norm_text(t2s(it.get("title"))),
                          _norm_text(t2s(it.get("themoviedbName"))),
                          _norm_text(t2s(it.get("mikanTitle")))):
                    if k and k not in idx:
                        idx[k] = entry
        except Exception as exc:  # noqa: BLE001
            log(f"ani-rss 订阅角色获取失败(按无角色处理): {exc}", "WARN")
        _SUB_ROLES_CACHE.update({"ts": now_ts, "by_key": idx})
    by_key = _SUB_ROLES_CACHE["by_key"]
    # 1) 全等
    for k in keys:
        if k in by_key:
            return by_key[k]
    # 2) 包含匹配(取最长命中, 避免短名误撞到多部番)
    best, best_score = None, 0
    for k in keys:
        for ik, entry in by_key.items():
            if not ik:
                continue
            if k in ik or ik in k:
                score = min(len(k), len(ik))
                if score > best_score:
                    best, best_score = entry, score
    return best


def route_episode_collision(src_full, library_root, folder_name, season, fn,
                            dry_run=False, roles=None, fallback_group=None):
    r"""同集号冲突检查: 目标季目录中该集已有**其他命名版本**(其他字幕组等)时,
    按主/备组角色路由(可从 ani-rss 订阅识别; 订阅给不出裁决时回退全局组优劣表):
      - 新文件=更优(主组/优选组), 库内同集=让位(备组/次选组) => 整套替换, 旧移冲突备份
      - 新文件=让位, 库内同集=更优                            => 新文件让位(丢弃/入冲突备份)
      - 其余(同级/角色不明)                                   => 通用规则: 同大小去重, 不同大小入冲突备份
    绝不让同一集出现两个并存条目污染 Emby。返回 True 表示已处理(调用方跳过 archive_one)。"""
    season_dir = os.path.join(library_root, folder_name, f"Season {season}")
    if not os.path.isdir(season_dir):
        return False
    m = FINAL_NAME_RE.match(os.path.splitext(fn)[0])
    if not m or not m.group("ep").isdigit():
        return False
    ep = int(m.group("ep"))
    try:
        src_size = os.path.getsize(src_full)
    except OSError:
        return False
    olds = []                       # (文件名, 完整路径, 大小, 角色, 是否视频)
    stem_new = os.path.splitext(fn)[0]
    for old in os.listdir(season_dir):
        om = FINAL_NAME_RE.match(os.path.splitext(old)[0])
        if not om or not om.group("ep").isdigit():
            continue
        if int(om.group("season")) != season or int(om.group("ep")) != ep:
            continue
        # 同名文件(及其侧车 -thumb/-poster/.nfo)交由 archive_one 处理: 它按体积去重或备份旧文件,
        # 语义比"冲突路由"更准确。若把同名侧车当成"其他版本", 会把本该正常入库的新文件误判成冲突。
        stem_old = os.path.splitext(old)[0]
        if os.path.normcase(stem_old) == os.path.normcase(stem_new) or \
                (os.path.normcase(_SIDECAR_SUFFIX_RE.sub("", stem_old)) ==
                 os.path.normcase(stem_new) and _SIDECAR_SUFFIX_RE.search(stem_old)):
            continue
        old_full = os.path.join(season_dir, old)
        if not os.path.isfile(old_full):
            continue
        olds.append((old, old_full, os.path.getsize(old_full),
                     _group_role(_fn_group_tag(old), roles),
                     os.path.splitext(old)[1].lower() in VIDEO_EXTS))
    if not olds:
        return False
    # 侧车文件(nfo/thumb)与该集视频同属一个版本, 但他们的体积不代表该集、组名也可能带伪后缀。
    # 因此"库内版本"的角色与体积一律以**视频**为准(该集目录内没有视频时才退回全部文件)。
    vids = [t for t in olds if t[4]]
    rep_pool = vids or olds
    rep_name, _rep_full, old_size, _rep_role, _rep_isv = rep_pool[0]
    old_roles = [t[3] for t in rep_pool]
    new_role = _group_role(_fn_group_tag(fn), roles) \
        or _group_role(fallback_group, roles)

    # 裁决一: ani-rss 主/备角色(订阅级意图, 优先)
    replace_old = bool(new_role == "main" and all(r == "standby" for r in old_roles))
    yield_new = bool(new_role == "standby" and any(r == "main" for r in old_roles))
    why = "订阅主/备组"
    # 裁决二: 兜底 —— 订阅给不出裁决(角色不明, 或两侧同为备组/同为主组)时, 按全局
    # 组优劣层定夺(优选 > 未评级 > 次选; 实测需求 Nix-Raws[未评级] > ANi[次选])
    if not replace_old and not yield_new:
        new_t = _group_tier(_fn_group_tag(fn))
        if new_t is None:
            new_t = _group_tier(fallback_group)
        old_ts = [t for t in (_group_tier(_fn_group_tag(x[0])) for x in rep_pool)
                  if t is not None]
        old_t = max(old_ts) if old_ts else None
        if new_t is not None and old_t is not None and new_t != old_t:
            if new_t > old_t:
                replace_old, why = True, "全局组优先(新文件组更优)"
            else:
                yield_new, why = True, "全局组优先(库内组更优)"

    # 裁决三: 同组新版 —— 新文件与库内同集文件为**同一字幕组**且大小不同,
    # 视为该组的 v2/修复版重发布, 新版直接替换旧版(实测 2026-10-10 FX战士久留美
    # S01E01: 主组绿茶发布 01v2WebRipHEVC 修复版, 旧逻辑走通用规则被送进冲突备份)。
    # 注: 同大小已在通用规则去重, 走不到这里。
    if not replace_old and not yield_new:
        new_g = (_fn_group_tag(fn) or fallback_group or "").strip().casefold()
        if new_g and any(
                (_fn_group_tag(x[0]) or "").strip().casefold() == new_g
                for x in rep_pool):
            replace_old, why = True, "同组新版(同一字幕组重发布)"

    # 更优的新文件替换让位的库内版本: 旧整套(视频+nfo+thumb等)移入冲突备份
    if replace_old:
        action = "同组新版替换" if why.startswith("同组新版") else "主组替换备组"
        bdir = os.path.join(CONFLICT_DIR, now().strftime("%Y%m%d_%H%M%S") + "_" + action)
        log(f"{action}[{why}]: 库内同集 {', '.join(n for n, _p, _s, _r, _v in olds)} 旧版"
            f" 整套移入冲突备份 {bdir}; 新文件 {fn} 继续入库", "WARN")
        if not dry_run:
            os.makedirs(bdir, exist_ok=True)
            for _n, old_full, _s, _r, _v in olds:
                base_old = os.path.splitext(os.path.basename(old_full))[0]
                for x in os.listdir(season_dir):
                    if x == os.path.basename(old_full) or \
                            os.path.splitext(x)[0].startswith(base_old):
                        try:
                            shutil.move(os.path.join(season_dir, x),
                                        os.path.join(bdir, x))
                        except OSError as exc:  # noqa: BLE001
                            log(f"{action}: 移动 {x} 失败: {exc}", "WARN")
        return False   # 交给调用方继续 archive_one

    # 让位的新文件: 库内已是更优版本, 备组副本无价值 -> 丢弃(连同工作区同前缀配套文件)
    if yield_new:
        keeper = next((n for n, _p, _s, r, _v in rep_pool if r == "main"), rep_name)
        log(f"主组已在库[{why}]({keeper}), 备组文件直接丢弃: {fn}", "WARN")
        if not dry_run:
            base_new = os.path.splitext(os.path.basename(src_full))[0]
            wdir = os.path.dirname(src_full)
            for x in os.listdir(wdir):
                if x == os.path.basename(src_full) or \
                        os.path.splitext(x)[0].startswith(base_new):
                    try:
                        os.remove(os.path.join(wdir, x))
                    except OSError as exc:  # noqa: BLE001
                        log(f"备组丢弃: 删除 {x} 失败: {exc}", "WARN")
        return True

    # 通用规则(同级/角色不明): 同大小去重, 不同大小移入冲突备份
    if old_size == src_size:
        log(f"同集号同大小(其他命名版本), 丢弃新文件(去重): {fn} ≈ {rep_name}")
        if not dry_run:
            os.remove(src_full)
        return True
    bdir = os.path.join(CONFLICT_DIR, now().strftime("%Y%m%d_%H%M%S"))
    log(f"同集号已有其他版本(大小不同), 新文件移入冲突备份 {bdir}: {fn} (库内保留 {rep_name})", "WARN")
    if not dry_run:
        os.makedirs(bdir, exist_ok=True)
        shutil.move(src_full, os.path.join(bdir, fn))
    return True


def park_unmatched(files, torrent_name, dry_run=False):
    r"""未匹配到目标文件夹: 文件移入 <工具目录>\待归档\<任务名>\, 等待重试"""
    udir = os.path.join(UNMATCHED_DIR, safe_subdir(torrent_name))
    log(f"未搜索到匹配的目标文件夹, {len(files)} 个文件保留待归档: {udir}", "WARN")
    if not dry_run:
        os.makedirs(udir, exist_ok=True)
        for full, _fn in files:
            dst = os.path.join(udir, _fn)
            i = 2
            while os.path.exists(dst):
                base, ext = os.path.splitext(dst)
                dst = f"{base} ({i}){ext}"
                i += 1
            shutil.move(full, dst)


def move_into_library(workdir, torrent_name, dry_run=False, content_path=None, abs_hint_map=None,
                      orig_name_map=None):
    """解析工作区改名后的文件: 匹配到目标文件夹则入库, 否则全部移入待归档。
    abs_hint_map: {(大小, mtime): 绝对集号}, 由原始文件名提取。
    orig_name_map: {(大小, mtime): 原始文件名}, 供季归属覆写匹配。
    返回 (archived, pending)"""
    entries = []
    for root, _dirs, files in os.walk(workdir):
        for fn in files:
            base, ext = os.path.splitext(fn)
            m = FINAL_NAME_RE.match(base)
            if m and ext.lower() in MEDIA_EXTS:
                entries.append((os.path.join(root, fn), m.group("title").strip(),
                                int(m.group("season")), fn))
            else:
                entries.append((os.path.join(root, fn), None, None, fn))

    recognized = [e for e in entries if e[1] is not None]
    leftovers = [e for e in entries if e[1] is None]

    if not recognized:
        raise RuntimeError("工作区中没有符合最终命名格式的媒体文件")

    titles = {}
    for _full, title, _s, _fn in recognized:
        titles[title] = titles.get(title, 0) + 1
    show_title = max(titles, key=titles.get)
    for t, c in titles.items():
        if t != show_title:
            log(f"标题不一致({c}个文件): {t} -> 按多数取 '{show_title}'", "WARN")

    hit = resolve_show_folder(show_title, recognized[0][2], torrent_name, content_path)
    if hit[0] is None:
        # 未匹配: 不入库。改名后的文件(含无法识别的)统一进待归档目录
        park_unmatched([(full, fn) for full, _t, _s, fn in entries], torrent_name, dry_run)
        return 0, len(entries)

    library_root, folder_name, source = hit[0][0], hit[0][1], hit[1]
    log(f"目标文件夹匹配来源: {source} -> {library_root}\\{folder_name}")
    _RUN["meta"]["target"] = f"{library_root}\\{folder_name}"
    _RUN["meta"]["show_title"] = show_title
    # 主/备组角色(ani-rss 订阅): 同集冲突时主组可替换备组, 备组遇主组让位
    _roles = sub_roles(show_title, folder_name)
    _fallback_group = torrent_group_label(torrent_name)

    # ani-rss 依据 TMDB 元数据划分的季号, 优先于改名工具从文件名推断的结果。
    # 季号是"内容属于第几季"的属性, 与最终归入哪个目录无关(对齐既有目录时目录名/年份可能不同)
    _ar_folder, ar_season = ani_rss_layout(content_path)

    archived = 0
    for full, title, season, fn in recognized:
        if title != show_title:
            leftovers.append((full, None, None, fn))
            continue
        _hint = (abs_hint_map or {}).get(
            (os.path.getsize(full), int(os.path.getmtime(full)))) \
            if os.path.isfile(full) else None
        _orig = (orig_name_map or {}).get(
            (os.path.getsize(full), int(os.path.getmtime(full)))) \
            if os.path.isfile(full) else None
        # 标题残留清理(先于集号守卫): 改名工具会把原始名里的 [01v2][AVC-8bit]
        # 等标签拼进标题(如 "... 01v2AVC - S01E01 - Sakurato.mp4"), 剔除该碎片
        clean_fn = strip_title_junk(fn)
        if clean_fn != fn:
            if not dry_run:
                try:
                    new_full = os.path.join(os.path.dirname(full), clean_fn)
                    os.rename(full, new_full)
                    full = new_full
                except OSError as e:
                    log(f"标题清理改名失败, 保持原名: {e}", "WARN")
                    clean_fn = fn
            if clean_fn != fn:
                log(f"标题清理: {fn} -> {clean_fn}")
                fn = clean_fn
        # 集号守卫: 单文件任务时 qB 任务名(ani-rss 按 TMDB 生成)的集号权威。
        # 实测 "[ANi] 新網球王子 U-17 WORLD CUP...- 01" 的 "U-17" 被改名工具
        # 误当集号(S01E17), 任务名说是 E01 -> 以任务名为准。
        if len(recognized) == 1:
            full, fn = apply_task_episode_guard(full, fn, torrent_name, dry_run=dry_run)
        if ar_season is not None and ar_season != season:
            new_fn = rename_season_in_filename(fn, ar_season)
            if new_fn != fn:
                if not dry_run:
                    new_full = os.path.join(os.path.dirname(full), new_fn)
                    os.rename(full, new_full)
                    full = new_full
                log(f"季号按 ani-rss(Season {ar_season}) 校正: {fn} -> {new_fn}")
            fn = new_fn
            season = ar_season
        # TMDB 单季合并多 cours 时(如 2024 版乱马½), 归并到 TMDB 实际存在的季
        season, fn = align_season_to_tmdb(
            library_root, folder_name, season, fn,
            src_size=None if dry_run else os.path.getsize(full), abs_ep=_hint,
            name_hints=[resolved_name_hint(show_title, season), show_title, torrent_name])
        # ani-rss 跨季连续集号时(如异人旅馆 S02E14), 按 TMDB 每季重排编号对齐
        season, fn = align_episode_to_tmdb(
            library_root, folder_name, season, fn,
            src_size=None if dry_run else os.path.getsize(full))
        # 季归属手工覆写(SEASON_OVERRIDES): 权威级, 覆盖前面一切季推断。
        # 适用于 Emby 侧 TMDB 季数据滞后/ani-rss 相对编号等情况(实测网球王子 S3)
        ov = season_override_target(folder_name, [_orig, torrent_name])
        if ov is not None and ov != season:
            new_fn = rename_season_in_filename(fn, ov)
            log(f"季归属手工覆写: S{season:02d} -> S{ov:02d} ({new_fn})")
            if not dry_run and new_fn != fn:
                try:
                    new_full = os.path.join(os.path.dirname(full), new_fn)
                    os.rename(full, new_full)
                    full = new_full
                except OSError as e:
                    log(f"季覆写改名失败, 保持原名: {e}", "WARN")
            fn = new_fn
            season = ov
        # 绝对集号续排(SEASON_OVERRIDES 的 ep_offset): 续作并入 TMDB 合并单季时,
        # 把 cours 相对集号改写为绝对集号(实测东京复仇者 三天战争篇: 第4期
        # E01/E02 -> S01E51/E52, TMDB Season 1 共 55 集, 库内已排至 E50)
        _ov_off = season_override_ep_offset(folder_name, [_orig, torrent_name])
        if _ov_off:
            m_off = FINAL_NAME_RE.match(os.path.splitext(fn)[0])
            if m_off and m_off.group("ep").isdigit():
                _old_ep = int(m_off.group("ep"))
                new_fn = rename_episode_in_filename(fn, _old_ep + _ov_off)
                if new_fn != fn:
                    log(f"绝对集号续排: E{_old_ep:02d} -> E{_old_ep + _ov_off:02d}"
                        f"(+{_ov_off}, 续作并入合并单季) | {new_fn}")
                    if not dry_run:
                        try:
                            new_full = os.path.join(os.path.dirname(full), new_fn)
                            os.rename(full, new_full)
                            full = new_full
                        except OSError as e:
                            log(f"集号续排改名失败, 保持原名: {e}", "WARN")
                            new_fn = fn
                    fn = new_fn
        # 同集号已有其他版本时按主/备角色路由(主组替换备组/备组让位/去重/冲突备份)
        if not dry_run and route_episode_collision(
                full, library_root, folder_name, season, fn,
                roles=_roles, fallback_group=_fallback_group):
            archived += 1  # 已去重/备份, 不再入库
            continue
        result = archive_one(full, library_root, folder_name, season, fn, dry_run=dry_run)
        if result in ("moved", "conflict", "dup"):
            archived += 1

    for full, _t, _s, fn in leftovers:
        ldir = os.path.join(LEFTOVER_DIR, safe_subdir(torrent_name))
        if not dry_run:
            os.makedirs(ldir, exist_ok=True)
            shutil.move(full, os.path.join(ldir, fn))
        log(f"未识别/非主流名文件移至残留目录: {ldir}/{fn}", "WARN")
    return archived, len(leftovers)


def retry_pending(dry_run=False):
    """重试待归档目录: 重新搜索目标文件夹, 匹配成功则归档"""
    if not os.path.isdir(UNMATCHED_DIR):
        log("待归档目录不存在, 无需重试")
        notify("retry", "待归档目录不存在, 无需重试")
        return
    n_scan = n_ok = 0
    for sub in sorted(os.listdir(UNMATCHED_DIR)):
        sub_dir = os.path.join(UNMATCHED_DIR, sub)
        if not os.path.isdir(sub_dir) or sub.startswith("_"):
            continue
        entries = []
        for fn in sorted(os.listdir(sub_dir)):
            full = os.path.join(sub_dir, fn)
            if not os.path.isfile(full):
                continue
            base, ext = os.path.splitext(fn)
            m = FINAL_NAME_RE.match(base)
            if m and ext.lower() in MEDIA_EXTS:
                entries.append((full, m.group("title").strip(), int(m.group("season")), fn))
            else:
                log(f"[重试] 文件不符合最终命名, 保留: {sub}/{fn}", "WARN")
        if not entries:
            continue
        n_scan += 1
        titles = {}
        for _full, title, _s, _fn in entries:
            titles[title] = titles.get(title, 0) + 1
        show_title = max(titles, key=titles.get)
        hit = resolve_show_folder(show_title, entries[0][2], sub)
        if hit[0] is None:
            log(f"[重试] 仍未匹配 '{show_title}' ({sub}), 继续保留", "WARN")
            continue
        library_root, folder_name = hit[0]
        log(f"[重试] '{show_title}' 匹配成功({hit[1]}) -> {library_root}\\{folder_name}")
        n_ok += 1
        _RUN["meta"].setdefault("target", f"{library_root}\\{folder_name}")
        _roles = sub_roles(show_title, folder_name)
        for full, title, season, fn in entries:
            if title != show_title:
                log(f"[重试] 标题不一致, 保留待人工处理: {fn}", "WARN")
                continue
            season, fn = align_season_to_tmdb(
                library_root, folder_name, season, fn,
                src_size=None if dry_run else os.path.getsize(full),
                name_hints=[resolved_name_hint(show_title, season), show_title, sub])
            season, fn = align_episode_to_tmdb(
                library_root, folder_name, season, fn,
                src_size=None if dry_run else os.path.getsize(full))
            if not dry_run and route_episode_collision(
                    full, library_root, folder_name, season, fn, roles=_roles):
                continue
            archive_one(full, library_root, folder_name, season, fn, dry_run=dry_run)
        if not dry_run and not os.listdir(sub_dir):
            os.rmdir(sub_dir)
            log(f"[重试] 待归档子目录已清空并移除: {sub}")
    notify("retry", f"待归档重试: 扫描 {n_scan} 个任务, 匹配成功 {n_ok} 个"
                    + (" [DRY-RUN]" if dry_run else ""))
    _RUN["meta"]["counts"] = f"扫描 {n_scan} · 匹配 {n_ok}"


# ---------------------------- qBittorrent API ----------------------------
def qb_api(path, data=None):
    url = QB_HOST + path
    req = urllib.request.Request(url, method="POST" if data is not None else "GET",
                                 headers={"Authorization": "Bearer " + QB_API_KEY})
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, body, timeout=15) as r:
        raw = r.read()
    return json.loads(raw.decode()) if raw.strip() else {}


def seed_action(hash_id, torrent_name):
    if TRANSFER_MODE == "seed":
        log(f"做种模式: 保留种子任务继续上传, 不动 qB | {torrent_name}")
        return
    if SEED_ACTION not in ("pause", "delete") or not hash_id:
        return
    try:
        if SEED_ACTION == "pause":
            qb_api("/api/v2/torrents/pause", {"hashes": hash_id})
            log(f"已暂停种子: {torrent_name}")
        else:
            # 文件已被移走, deleteFiles 仅清理种子任务及其残留
            qb_api("/api/v2/torrents/delete", {"hashes": hash_id, "deleteFiles": "true"})
            log(f"已删除种子任务(文件已移入媒体库): {torrent_name}")
    except Exception as e:
        log(f"种子动作({SEED_ACTION})失败: {e}", "WARN")


# ---------------------------- 主流程 ----------------------------
def process(torrent_name, content_path, save_path, category, tags, hash_id, dry_run=False):
    tags = set((tags or "").split(",")) - {""}
    if category not in BANGUMI_CATEGORIES and not (tags & BANGUMI_TAGS):
        log(f"跳过(非番剧任务): 分类={category or '无'} 标签={sorted(tags) or '无'} | {torrent_name}")
        notify("skip", f"非 ani-rss 任务, 已跳过: `{torrent_name}`")
        return

    log(f"开始处理: {torrent_name} | 分类={category} | 路径={content_path}")
    _RUN["meta"].update(torrent=torrent_name, category=category, dry_run=dry_run)
    notify("start", f"开始处理 `{torrent_name}`")
    if not os.path.exists(content_path):
        raise RuntimeError(f"内容路径不存在: {content_path}")

    if not wait_content_ready(content_path):
        raise RuntimeError("等待文件就绪超时(仍有 .!qB 临时文件)")

    media_files = collect_media_files(content_path)
    if not media_files:
        log("任务中无媒体文件(视频/字幕), 仅跳过入库")
        notify("skip", "任务中无媒体文件(视频/字幕), 仅跳过入库")
        return

    # 改名会丢失原始文件名, 而绝对集号提示([01_49] 的 49)与季归属信号恰在里面。
    # 以 (大小, mtime) 为键建立映射, 改名后仍可反查(改名不改动大小/mtime)
    abs_hint_map = {}
    orig_name_map = {}
    for full, _rel in media_files:
        try:
            h = extract_abs_episode_hint(os.path.basename(full))
        except Exception:
            h = None
        if h is not None:
            try:
                abs_hint_map[(os.path.getsize(full), int(os.path.getmtime(full)))] = h
            except OSError:
                pass
        try:
            orig_name_map[(os.path.getsize(full), int(os.path.getmtime(full)))] = \
                os.path.basename(full)
        except OSError:
            pass

    workdir = os.path.join(WORK_DIR, f"{(hash_id or 'manual')[:12]}_{now().strftime('%H%M%S')}")
    keep_source = dry_run or TRANSFER_MODE == "seed"
    used_copy = move_to_workdir(media_files, workdir, dry_run=dry_run, keep_source=keep_source)
    if used_copy:
        log("存在跨盘文件, 无法硬链接, 使用复制(暂存源文件保留)", "WARN")
    if TRANSFER_MODE == "seed" and not dry_run:
        log("做种模式: 下载目录原文件保留, qB 继续做种; 工作区为硬链接副本")
    staged_dirs = list({os.path.dirname(full) for full, _rel in media_files})

    try:
        # 改名工具以 GBK 打印输出, 文件名含超集字符(½/♪/☆ 等)会使其崩溃:
        # 先替换为占位符, 工具跑完(无论成败)立即还原
        n_sub = sanitize_workdir_for_tool(workdir)
        if n_sub:
            log(f"已将 {n_sub} 个名称中的 GBK 超集字符替换为占位符(改名工具编码限制)")
        try:
            ok, out = run_renamer(workdir)
        finally:
            n_ret = restore_workdir_names(workdir)
            if n_ret:
                log(f"已还原 {n_ret} 个名称中的占位符")
        if not ok and "无法识别工作区中的任何文件" in out:
            # 兜底救援: 按任务名预改写为工具可识别的形态, 再让工具重试一次
            if rescue_rename_from_task(workdir, torrent_name):
                n_sub = sanitize_workdir_for_tool(workdir)
                try:
                    ok, out = run_renamer(workdir)
                finally:
                    restore_workdir_names(workdir)
        if not ok:
            faildir = os.path.join(LEFTOVER_DIR, "_改名失败_" + re.sub(r'[\\/:*?"<>|]', "_", torrent_name)[:60]
                                   + "_" + now().strftime("%H%M%S"))
            os.makedirs(os.path.dirname(faildir), exist_ok=True)
            if os.path.exists(faildir):
                force_rmtree(faildir)
            shutil.move(workdir, faildir)
            workdir = None  # 已移走并保留现场, finally 不再清理
            raise RuntimeError("改名工具执行失败, 工作区已保留至: " + faildir + " | " + out[:300].replace("\n", " "))
        log("改名工具执行成功")

        archived, pending = move_into_library(workdir, torrent_name, dry_run=dry_run,
                                              content_path=content_path, abs_hint_map=abs_hint_map,
                                              orig_name_map=orig_name_map)
        _RUN["meta"]["counts"] = f"入库 {archived} · 残留 {pending}"
        if archived == 0 and pending > 0:
            log(f"未归档: {pending} 个文件保留在 待归档 目录, 可稍后运行 --retry 重试"
                + (" [DRY-RUN 未实际移动]" if dry_run else ""), "WARN")
            notify("result", f"未匹配到目标目录, {pending} 个文件进入待归档(可 --retry 重试)")
        else:
            log(f"完成: 入库 {archived} 个文件, 残留 {pending} 个"
                + (" [DRY-RUN 未实际移动]" if dry_run else ""))
            notify("result", f"入库 {archived} 个文件" + (f", 残留 {pending} 个" if pending else ""))
            if not dry_run:
                seed_action(hash_id, torrent_name)
        if not dry_run:
            # 不做种模式下暂存中已移出的目录若已为空则逐级清理;
            # 做种模式下原文件仍在, 目录非空, 此处自然不会删除
            removed = prune_empty_dirs(staged_dirs)
            if removed:
                log(f"已清理空的暂存目录 {len(removed)} 个: " + "; ".join(
                    os.path.relpath(d, STAGE_DIR) for d in removed[-5:]))
    finally:
        if workdir and os.path.exists(workdir):
            force_rmtree(workdir)


def _main():
    global TRANSFER_MODE
    argv = list(sys.argv[1:])
    dry_run = "--dry-run" in argv
    if "--seed" in argv:
        TRANSFER_MODE = "seed"
    elif "--no-seed" in argv:
        TRANSFER_MODE = "no_seed"
    argv = [a for a in argv if a not in ("--dry-run", "--seed", "--no-seed")]

    run_reset()
    log_segment_start()   # 日志分段: 本次运行的段头(参数以原始 sys.argv 为准)

    if "--test-notify" in argv:
        # 钉钉链路自检: 只发一条测试消息, 不触碰任何媒体文件
        ok, detail = dingtalk_send(
            f"{DINGTALK_KEYWORD} · 链路自检",
            f"## 🔔 {DINGTALK_KEYWORD} · 链路自检\n"
            f"**时间**: {now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**加签**: {'已启用' if DINGTALK_SECRET else '未启用'}\n"
            f"**@所有人**: {'是' if DINGTALK_AT_ALL else '否'}")
        log(f"钉钉链路自检: {'成功' if ok else '失败'} | {detail}", "INFO" if ok else "ERROR")
        return 0 if ok else 2

    try:
        if "--retry" in argv:
            # 重试待归档目录(无需 qB 参数)
            argv = [a for a in argv if a != "--retry"]
            lock = acquire_lock()
            try:
                retry_pending(dry_run=dry_run)
                return 0
            except Exception as e:
                log_failure("(retry)", f"{e}\n{traceback.format_exc(limit=3)}")
                notify("fail", f"待归档重试异常: {e}")
                return 2
            finally:
                for _p in sweep_stage_empty():
                    log(f"清扫空的暂存目录: {os.path.relpath(_p, STAGE_DIR)}")
                release_lock(lock)

        if len(argv) < 6:
            log(f"参数不足, 期望: <名称> <内容路径> <保存路径> <分类> <标签> <hash>, 实际: {argv}", "ERROR")
            notify("fail", f"参数不足, 收到 {len(argv)} 个参数")
            return 1
        name, content, save, cat, tags, h = argv[0], argv[1], argv[2], argv[3], argv[4], argv[5]
        lock = acquire_lock()
        try:
            process(name, content, save, cat, tags, h, dry_run=dry_run)
            return 0
        except Exception as e:
            log_failure(name, f"{e}\n{traceback.format_exc(limit=3)}")
            notify("fail", f"处理异常: {e}")
            return 2
        finally:
            # 兜底清扫历史遗留的空目录树(含 qB deleteFiles 残留), 与主流程互斥由锁保证
            for _p in sweep_stage_empty():
                log(f"清扫空的暂存目录: {os.path.relpath(_p, STAGE_DIR)}")
            release_lock(lock)
    finally:
        # 收尾统一推送(此时全局锁已释放, 网络耗时不影响并发)
        notify_flush()


def main():
    """薄包装: 无论正常返回还是异常退出, 都以「运行结束」段尾收口日志(保证分段完整)"""
    rc = 0
    try:
        rc = _main()
        return rc
    except BaseException:
        rc = 2
        try:
            log_failure("(main)", traceback.format_exc(limit=3))
        except Exception:
            pass
        raise
    finally:
        try:
            log_segment_end(rc)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
