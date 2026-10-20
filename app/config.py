"""配置加载 + 计价。

设计原则：所有"能调的数字"都放 config.yaml，代码里不出现魔法数字。
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

CST = timezone(timedelta(hours=8))  # DeepSeek 的高峰/空闲按北京时间算


def _mk(cls: type, d: Any):
    """用 dict 构造 dataclass，忽略多余字段（方便配置文件比代码新）。"""
    if not isinstance(d, dict):
        return cls()
    allowed = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class OneBotConfig:
    mode: str = "reverse"
    host: str = "0.0.0.0"
    port: int = 3001
    path: str = "/onebot/v11/ws"
    url: str = "ws://127.0.0.1:3000"
    access_token: str = ""


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    thinking: bool = False
    max_tokens: int = 200
    temperature: float = 1.3
    timeout: float = 60.0
    mock: bool = False
    # ★ 联网搜索（P5）。给模型一个 web_search / fetch_page 工具，**由它自己决定要不要用**。
    # 工具定义是常量、挂在缓存前缀上，平时一次不调 ≈ 不花钱；
    # 真搜一次多一轮请求，约 0.001~0.003 元。关掉就完全不发 tools 字段。
    web_search: bool = True
    # 最多来回几轮工具调用。2 够用：够它「搜一次 → 觉得不够 → 再打开一个网页」。
    # 调大只会让话变慢变贵，不会变准。
    max_tool_rounds: int = 2


@dataclass
class BudgetConfig:
    daily_yuan: float = 2.0
    monthly_yuan: float = 45.0
    min_interval_seconds: float = 1.0
    saver_history_tokens: int = 600
    saver_max_tokens: int = 80
    hard_stop_multiplier: float = 3.0


@dataclass
class ContextConfig:
    history_tokens: int = 1500
    compress_ratio: float = 0.6
    summary_tokens: int = 300
    profile_tokens: int = 200


@dataclass
class MemoryConfig:
    """长期记忆（P3）。检索全在本地做，不花 token —— 见 app/memory.py 顶部说明。"""

    enabled: bool = True
    extract_every_turns: int = 20     # 每聊这么多轮，抽一次事实
    extract_min_turns: int = 6        # 少于这么多轮不值得抽
    extract_max_facts: int = 4
    extract_max_tokens: int = 220
    top_k: int = 3                    # 每轮最多召回几条
    max_chars: int = 30               # 单条记忆注入时的字数上限
    half_life_days: float = 30.0      # 时间衰减半衰期（只影响排序，不影响要不要想起）
    min_sim: float = 0.45             # 查询的信息量至少要有这么多落在这条记忆里，否则不注入
    dedup_threshold: float = 0.72     # 相似度超过这个就当同一条，更新而不是新增


@dataclass
class ProactiveConfig:
    """主动搭话（P2）。默认开着但很克制，避免像推销号。"""

    enabled: bool = True
    min_idle_hours: float = 6.0       # 多久没聊才主动找他
    max_per_day: int = 2              # 一天最多主动几次
    active_hours: List[int] = field(default_factory=lambda: [10, 23])
    check_interval_minutes: float = 15.0
    topics_from_memory: int = 3
    # 只主动搭话给这几个 QQ。留空 = 只看 whitelist.masters；
    # masters 也是空的，才退回老行为（聊过的私聊对象都可能被搭话）。
    targets: List[int] = field(default_factory=list)
    # 同一个人两次主动之间至少隔这么久，防止「把一整天的话都攒给一个人」。
    min_hours_between: float = 2.0


@dataclass
class LifeConfig:
    """作息（P4 的一部分，P2 主动搭话要依赖它）。"""

    enabled: bool = True
    sleeping: List[int] = field(default_factory=lambda: [1, 8])
    working: List[List[int]] = field(default_factory=lambda: [[9, 12], [14, 18]])
    weekday_only_work: bool = True
    moods: Dict[str, str] = field(default_factory=lambda: {
        "sleeping": "你已经睡了，不该回消息",
        "working": "你在上班，有点忙，回得短、偶尔敷衍",
        "winding_down": "晚上有点困，说话懒懒的、慢半拍",
        "free": "你有空，可以多聊两句",
    })
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)


@dataclass
class HumanizeConfig:
    read_delay_min: float = 0.4
    read_delay_max: float = 1.8
    typing_per_char_min: float = 0.09
    typing_per_char_max: float = 0.22
    typing_min: float = 1.2
    typing_max: float = 12.0
    segment_gap_min: float = 0.6
    segment_gap_max: float = 2.2
    segment_max_len: int = 60
    segment_max_count: int = 3
    debounce_seconds: float = 1.2
    late_reply_chance: float = 0.08
    late_reply_min: float = 60.0
    late_reply_max: float = 1500.0
    # 安静时段不在这里配了 —— 归 app/life.py 管，见 config.yaml 的 life.sleeping
    trivial_replies: List[List[str]] = field(default_factory=list)


@dataclass
class PersonaConfig:
    file: str = "persona/default.md"
    name: str = "遥遥"
    # 群聊用另一套人设。群里说话跟私聊不一样：要更短、更能忍得住、
    # 不能像一对一那样什么都接。文件不存在就退回上面那套。
    group_file: str = "persona/group.md"
    # 群里被叫到哪个名字才回。留空 = 用 name。
    group_name: str = ""


@dataclass
class WhitelistConfig:
    private: List[int] = field(default_factory=list)
    groups: List[int] = field(default_factory=list)
    # ★ 谁是「主人」——和 private（谁能跟它说话）是两件不同的事，别再混为一谈。
    # private 里可以既有主人也有朋友；这个列表才是「谁被当成主人」的真源。
    # 留空 = 退回老行为：能私聊它的就一律当主人（单人自用最省事）。
    # 一旦填了，不在列表里的私聊对象会被当成「主人允许来聊天的朋友」：
    # 照常聊天，但不会被叫「老大」，也不会被当成主人本人。
    masters: List[int] = field(default_factory=list)
    # ★ 群里不点名时的插话概率。默认 0：只理 @ 和叫名字的。
    # 真人插话是偶尔，机器插话是刷屏 —— 调高之前想清楚能不能接受被踢。
    group_reply_rate: float = 0.0
    # 前 N 个私聊你的人自动进白名单（存数据库，日志里会明确打出来）。
    # 默认 1：你拿自己主号给机器人发一条「在吗」，就绑定好了，不用手抄 QQ 号。
    # 设成 0 就关掉，只认 private 里手写的那几个。
    auto_accept_first: int = 1


@dataclass
class GroupConfig:
    """群聊行为。默认值一律往「少说话」那边靠。"""

    enabled: bool = True
    # 只有被 @ 或被叫名字才回。设 False 就退回老的「按 group_reply_rate 随机插话」。
    address_required: bool = True
    # 同一个人在群里连续 @ 你，两次回复之间至少隔这么久（秒）。
    per_user_cooldown_seconds: float = 20.0
    # 不管谁说话，整个群两次回复之间至少隔这么久（秒）。防刷屏。
    reply_cooldown_seconds: float = 8.0
    # 硬上限：这个群每小时最多回几条。到量就闭嘴，到下一小时才恢复。
    max_replies_per_hour: int = 30
    # 群聊单独的上下文预算。0 = 跟私聊共用 context.history_tokens。
    history_tokens: int = 900
    # 群里一次最多发几条。默认 1：连发三条在群里太扎眼。
    max_segments: int = 1


@dataclass
class DatabaseConfig:
    path: str = "data/companion.db"


@dataclass
class TtsConfig:
    """本地语音（GPT-SoVITS）。

    默认关闭：没人明确要语音时不许自己发。语音条比文字贵得多（本地推理不花钱，
    但多一次几秒的等待、还容易被认出是合成的），所以它是个「显式打开」的能力。
    """

    enabled: bool = False
    # GPT-SoVITS 的 api_v2.py 地址。默认端口 9880，只绑本机。
    api_base: str = "http://127.0.0.1:9880"
    # 部署根目录：下面应该有 refs\ 和 app\GPT-SoVITS-*\ 以及 api_v2.log
    install_dir: str = "D:/GPT-SoVITS-deploy"
    # 一条语音最多多少字。太长的语音没人听完，还拖慢回复。
    max_chars: int = 120
    # 合成超时（秒）。冷启动第一句可能要 7~8 秒，别设太小。
    timeout: float = 60.0
    # 语速。0.9 温柔一点，1.1 活泼一点，超过 1.3 音质会劣化。
    speed: float = 1.0
    # 没指定情绪时用哪套参考音频（refs/<emotion>.wav）。
    default_emotion: str = "neutral"
    # 什么时候发：request 只在被明确要求时发；auto 还会按 chance 主动发。
    # 两种模式下「来条语音」这类要求都生效。
    trigger: str = "request"
    # auto 模式下每条回复转语音的概率。别调高 —— 语音条很扎眼。
    chance: float = 0.15
    # 群里也允许主动发语音吗。群消息一响所有人都在看，默认只在被要求时发。
    group: bool = False
    # 全语音模式：面板「声音」页上的一个按钮。开了之后**每条回复都发语音**，
    # 不看 trigger、不看 chance、不看字数上限（超长仍会截断，但这条一定发）。
    #
    # 为什么不做成「把 trigger 改成 auto + chance 改成 1」：那样会覆盖用户原来
    # 调好的参数，关掉之后没法还原（用户明确要求「关闭后仍然保留原来的设置」）。
    # 所以它是一个独立的覆盖开关，上面那些参数一个都不动。
    always_voice: bool = False


@dataclass
class VideoPushConfig:
    """B 站视频推荐推送。

    链路：按关键词搜 B 站 → 用思考模式让模型筛一遍（宁缺毋滥）→ 逐条丢给
    BiliNote 生成笔记 → 拼成一条消息私聊发给主人。

    注意 thinking 跟 llm.thinking 是**两件事**：聊天那条路必须关（每轮都烧
    思维链太贵），视频筛选一天只跑两三次，开 high 换更准的筛选是划算的。
    """

    enabled: bool = True
    # 感兴趣的关键词。一条一个，写成「关键词|筛选要求」，竖线后面可以省略。
    # 筛选要求会原样进提示词，例如「只看工程实践，不要卖课」。
    topics: List[str] = field(default_factory=list)
    # 每天几点跑一轮。错过的点在 catchup_hours 内会补跑一次。
    times: List[str] = field(default_factory=lambda: ["09:00", "21:00"])
    # 推给谁（私聊）。空 = 用 whitelist.masters（没设主人就谁也不发，只写日志）。
    targets: List[int] = field(default_factory=list)
    # 还要推到哪些群。在群里发「视频推送 开」会自动把那个群加到这里；
    # 私聊的 targets 照常推，两边的收件人互不影响。
    groups: List[int] = field(default_factory=list)
    # 一天最多推几条（含补跑）。推多了会烦人，默认 3。
    max_per_day: int = 3
    # 一个关键词一轮最多选中几条。防止一个关键词把名额吃光。
    max_per_topic: int = 2
    # 每个关键词取多少条候选交给模型筛。太大烧 token，太小容易没好货。
    candidates: int = 24
    # 模型评分低于这个数就不要。70 是「值得看」，80 以上是「非看不可」。
    min_score: int = 70
    # 只要最近多少天发布的。老视频多半早看过了。
    max_age_days: int = 30
    # 时长过滤（秒）。太短没内容，太长看不完 —— 默认 2 分钟到 90 分钟。
    min_duration: int = 120
    max_duration: int = 5400
    # 播放量下限。太低的多半是自娱自乐，但不是硬指标，0 = 不过滤。
    min_play: int = 300
    # 标题里出现这些词就直接扔掉，连模型都不用看（省钱的第一道闸）。
    block_words: List[str] = field(default_factory=lambda: [
        "广告", "带货", "招生", "训练营", "抽奖", "福利", "加群", "领资料"])
    # 筛选时开思考模式。关掉能省钱但更容易收垃圾。
    thinking: bool = True
    # 思考档位：low / high / max。high 是用户要的默认值——筛得慢一点，但垃圾少。
    # 随便填会被 API 拒（400），面板那边是下拉框，只有这三个值。
    filter_effort: str = "high"
    # 筛选那一次调用的输出上限。思考模式的思维链也算在输出里。
    filter_max_tokens: int = 3000
    # 停机太久（超过这么多小时）就不补跑了，等下一个点，免得一开机连推几轮。
    catchup_hours: float = 12.0
    # 多久看一次时间/待发队列（秒）。不影响检索频率，只影响「该不该跑了」。
    check_interval: int = 60

    # ---- BiliNote ----
    # 笔记风格，对应 BiliNote 的 9 种：minimal/detailed/academic/tutorial/
    # xiaohongshu/life_journal/task_oriented/business/meeting_minutes
    note_style: str = "detailed"
    # 下载质量：fast 最快（720P 以下），medium/slow 更清晰但更慢。
    note_quality: str = "fast"
    # 笔记里插视频截图。开了会慢一点，且每张截图都要跑一次 ffmpeg。
    note_screenshot: bool = False
    # 单条笔记最多等多久（秒）。长视频 + tiny 模型转写，十分钟很正常。
    note_timeout: int = 900
    # 推送里最多带多少字笔记。QQ 单条消息有长度上限，太长会被截断甚至拒收。
    note_max_chars: int = 1200
    # BiliNote 后端地址和 backend 目录。
    bilinote_url: str = "http://127.0.0.1:8483"
    bilinote_dir: str = "D:/BiliNote/backend"
    # 指定解释器。空 = <bilinote_dir>/.venv/Scripts/python.exe。
    bilinote_python: str = ""
    # 后端没起时自动替它拉起来（无窗口）。关掉就要求你先手动启动。
    bilinote_autostart: bool = True
    # 用哪个供应商/模型。留空 = 问 BiliNote 当前启用的那个。
    provider_id: str = ""
    model_name: str = ""
    # 可选登录 cookie（SESSDATA=...; bili_jct=...）。被 -412 风控时才需要。
    cookie: str = ""


@dataclass
class BootConfig:
    """开机自启（严格说是「登录时自启」）。

    注意这里的字段**机器人本体一个都不读** —— 它们由 boot.py（引导器）和
    面板消费。所以整组都是热更新：改完下次登录才生效，不需要重启谁。
    """

    # 登录时自动启动。实际生效的是注册表 HKCU\...\Run 那一行，
    # 这个布尔值只是「意图记录」，两处由面板/命令行同时写，不会打架。
    enabled: bool = False
    # 登录后先等多少秒。桌面、网络、代理都还没就位时抢跑会连不上 NapCat。
    delay: int = 10
    # 顺带把管理面板也拉起来（隐藏窗口，8899）。
    panel: bool = True
    # 面板起来后自动打开浏览器。这是「登录后唯一看得见的东西」——
    # 全隐藏模式下如果连页面都不弹，用户会以为自启失败了。
    open_panel: bool = True
    # 顺带把 GPT-SoVITS 语音服务拉起来。它加载模型要 60 秒，所以第一个点火。
    tts: bool = False
    # 代管 NapCat（自动找目录 + 静默拉起）。关掉就只起机器人和面板。
    napcat: bool = True
    # NapCat 的启动脚本或 exe。填了就按它来，不再自动找。
    napcat_path: str = ""
    # 快速登录用的 QQ 号。留空 = 从 NapCat 的 start-napcat.bat 里读 NAPCAT_QUICK_QQ。
    napcat_qq: str = ""
    # 隐藏所有子进程的窗口。关掉的话每个程序都会弹一个控制台（只建议排查时用）。
    headless: bool = True
    # 顺带把 BiliNote 后端拉起来（视频推送要用）。加载 whisper 要十几秒，
    # 但它不挡机器人启动，所以只起不等。
    bilinote: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "data/companion.log"


@dataclass
class PricingConfig:
    currency_per_million: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    peak_weekdays: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    peak_ranges: List[List[int]] = field(default_factory=lambda: [[9, 12], [14, 18]])
    holidays: List[str] = field(default_factory=list)

    def is_peak(self, when: Optional[datetime] = None) -> bool:
        """高峰时段判定：北京时间 周一至周五（非节假日）9:00-12:00 / 14:00-18:00。"""
        when = when or datetime.now(CST)
        if when.date().isoformat() in set(self.holidays or []):
            return False
        if when.weekday() not in (self.peak_weekdays or []):
            return False
        for lo, hi in self.peak_ranges or []:
            if lo <= when.hour < hi:
                return True
        return False

    def cost(self, model: str, cache_hit: int, cache_miss: int, output: int,
             when: Optional[datetime] = None) -> float:
        """算这一笔请求花了多少钱（元）。"""
        table = (self.currency_per_million or {}).get(model)
        if not table:
            return 0.0
        band = "peak" if self.is_peak(when) else "offpeak"
        per_m = 1_000_000.0
        total = 0.0
        total += cache_hit / per_m * float(table.get("cache_hit", {}).get(band, 0.0))
        total += cache_miss / per_m * float(table.get("cache_miss", {}).get(band, 0.0))
        total += output / per_m * float(table.get("output", {}).get(band, 0.0))
        return total


@dataclass
class Config:
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    humanize: HumanizeConfig = field(default_factory=HumanizeConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    whitelist: WhitelistConfig = field(default_factory=WhitelistConfig)
    life: LifeConfig = field(default_factory=LifeConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    group: GroupConfig = field(default_factory=GroupConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    videopush: VideoPushConfig = field(default_factory=VideoPushConfig)
    boot: BootConfig = field(default_factory=BootConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)

    # 运行时派生字段
    persona_text: str = ""
    # 群聊人设；读不到群文件就等于 persona_text
    group_persona_text: str = ""
    # 这份配置是从哪个文件读出来的。热更新要靠它重新读一遍同一个文件。
    source_path: str = ""

    def persona_path(self) -> Path:
        p = Path(self.persona.file)
        return p if p.is_absolute() else (ROOT / p)

    def group_persona_path(self) -> Path:
        p = Path(self.persona.group_file or self.persona.file)
        return p if p.is_absolute() else (ROOT / p)

    def talk_name(self, *, group: bool = False) -> str:
        """群里被叫到哪个名字才回。"""
        if group:
            return (self.persona.group_name or self.persona.name or "").strip()
        return (self.persona.name or "").strip()

    def is_master(self, uid: int) -> bool:
        """这个 QQ 号是不是「主人」。

        whitelist.masters 没配就退回老行为（能私聊它的就是主人），
        这样单人自用的旧配置不用改一个字。配了就以它为准：
        不在名单里的私聊对象是「主人允许来聊天的朋友」，不叫老大、不当主人本人。
        """
        masters = [int(x) for x in (self.whitelist.masters or [])]
        if not masters:
            return True
        return int(uid) in masters

    def proactive_targets(self) -> List[int]:
        """主动搭话可以找哪些 QQ 号。空列表 = 谁都可以（调用方自己决定怎么筛）。

        优先级：life.proactive.targets > whitelist.masters > 不限制。
        单独拎出来是因为「该主动找谁」和「谁能跟它说话」是两件事：
        朋友可以聊，但**不该被它主动打扰**。
        """
        t = [int(x) for x in (self.life.proactive.targets or [])]
        if t:
            return t
        return [int(x) for x in (self.whitelist.masters or [])]

    def db_path(self) -> Path:
        p = Path(self.database.path)
        return p if p.is_absolute() else (ROOT / p)

    def log_path(self) -> Path:
        p = Path(self.logging.file)
        return p if p.is_absolute() else (ROOT / p)


# ──────────────────────────────────────────────────────────────────────
#  热更新 vs 需重启（管理面板靠这个决定「保存」要不要顺便重启服务）
# ──────────────────────────────────────────────────────────────────────
# 下面这些参数在**进程启动时**就被固化了，光改配置文件对正在跑的进程没有影响：
#   - socket 在 serve_forever 之前就 bind 好了           → onebot.*
#   - httpx 客户端的 base_url 和 Authorization 头在 __init__ 里固定 → llm.api_key / llm.base_url
#   - sqlite 连接在 Store.__init__ 里建立                → database.path
#   - logging 的 handler 在 setup_logging 里挂上          → logging.*
# 其余参数都是每次用到时现读 `self.cfg.xxx`，可以直接把新值写进那个 Config 对象。
#
# ★ 这份名单是**单一真源**：panel/schema.py 的 restart 标记直接读它，
#   所以面板上显示的「需重启」徽章和这里永远不会不一致。
HOT_EXCLUDE = {
    "onebot.mode", "onebot.host", "onebot.port", "onebot.path",
    "onebot.url", "onebot.access_token",
    "llm.api_key", "llm.base_url",
    "database.path",
    "logging.level", "logging.file",
}


def _merge_hot(old: Any, new: Any, prefix: str) -> List[str]:
    """逐层把 new 里**可热更新**的字段搬到 old 上，返回实际变化了的路径。"""
    changed: List[str] = []
    for f in dataclasses.fields(old):
        path = f"{prefix}.{f.name}" if prefix else f.name
        cur = getattr(old, f.name)
        nv = getattr(new, f.name)
        if dataclasses.is_dataclass(cur) and dataclasses.is_dataclass(nv):
            changed.extend(_merge_hot(cur, nv, path))
        elif path not in HOT_EXCLUDE:
            if cur != nv:
                changed.append(path)
            setattr(old, f.name, nv)
    return changed


def apply_hot(old: "Config", new: "Config") -> List[str]:
    """把 new 里可热更新的字段**就地**写进 old（正在跑的那个 Config 对象）。

    调用方的各个组件都持有同一个 cfg 引用，所以改完立刻生效，不用重启。
    返回实际变化的参数路径列表（没变的不算）。
    """
    return _merge_hot(old, new, "")


def load_config(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else (ROOT / "config.yaml")
    if not path.exists():
        example = ROOT / "config.example.yaml"
        if example.exists():
            raise FileNotFoundError(
                f"没找到 {path}。\n先复制一份：copy config.example.yaml config.yaml"
            )
        raise FileNotFoundError(f"没找到配置文件 {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if (raw.get("humanize") or {}).get("quiet_hours") is not None:
        print("提示：humanize.quiet_hours 已挪到 life.sleeping，旧配置里这项会被忽略。")

    life_raw = dict(raw.get("life") or {})
    proactive_raw = life_raw.pop("proactive", None)
    life = _mk(LifeConfig, life_raw)
    if proactive_raw is not None:
        life.proactive = _mk(ProactiveConfig, proactive_raw)

    cfg = Config(
        onebot=_mk(OneBotConfig, raw.get("onebot")),
        llm=_mk(LLMConfig, raw.get("llm")),
        budget=_mk(BudgetConfig, raw.get("budget")),
        context=_mk(ContextConfig, raw.get("context")),
        humanize=_mk(HumanizeConfig, raw.get("humanize")),
        persona=_mk(PersonaConfig, raw.get("persona")),
        whitelist=_mk(WhitelistConfig, raw.get("whitelist")),
        life=life,
        memory=_mk(MemoryConfig, raw.get("memory")),
        group=_mk(GroupConfig, raw.get("group")),
        database=_mk(DatabaseConfig, raw.get("database")),
        tts=_mk(TtsConfig, raw.get("tts")),
        videopush=_mk(VideoPushConfig, raw.get("videopush")),
        boot=_mk(BootConfig, raw.get("boot")),
        logging=_mk(LoggingConfig, raw.get("logging")),
        pricing=_mk(PricingConfig, raw.get("pricing")),
    )

    # 环境变量优先，避免 API key 写进文件
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        cfg.llm.api_key = env_key
    if os.environ.get("COMPANION_LLM_MOCK") == "1":
        cfg.llm.mock = True

    # tts.install_dir 允许相对路径，按项目根解析。
    # 发布包里语音运行时摆在项目隔壁（../GPT-SoVITS-deploy），写死绝对路径
    # 会导致别人解压到别的盘就找不到；相对路径则整个文件夹搬走也不失效。
    if cfg.tts.install_dir and not Path(cfg.tts.install_dir).is_absolute():
        cfg.tts.install_dir = str((ROOT / cfg.tts.install_dir).resolve())

    cfg.persona_text = _load_persona(cfg)
    cfg.group_persona_text = _load_group_persona(cfg)
    cfg.source_path = str(path)
    return cfg


def _load_persona(cfg: Config) -> str:
    p = cfg.persona_path()
    if not p.exists():
        return f"你是{cfg.persona.name}，一个在 QQ 上陪人聊天的普通人。说话短、口语化、不用书面语。"
    return p.read_text(encoding="utf-8").strip()


def _load_group_persona(cfg: Config) -> str:
    """群聊人设。读不到就退回私聊那套 —— 宁可语气不对，也不能没有。"""
    p = cfg.group_persona_path()
    if not p.exists():
        return cfg.persona_text
    return p.read_text(encoding="utf-8").strip()
