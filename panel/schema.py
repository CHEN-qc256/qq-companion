"""管理面板的**参数元数据（唯一真源）**。

这个文件回答三个问题，且只在这里回答一次：
  1. config.yaml 里有哪些参数（一个都不能漏）
  2. 每个参数该用什么控件、什么范围、什么默认值
  3. 每个参数的三处中文说明：面板 tooltip / 代码注释 / 生成的配置文件行内注释

前端是「哑」的：它不认识任何一个具体参数名，只按 type 渲染控件。
想加参数只改这个文件，前端不用动。

关于 `restart`（改完要不要重启）：
  - True  = 需重启。这类参数在**进程启动时**就被固化了（socket 绑定的端口、
            httpx 客户端的 base_url 和 Authorization 头、sqlite 连接、
            日志 handler），光改文件不重启不会生效。
  - False = 热更新。这类参数每次用到时都现读 `self.cfg.xxx`，面板可以直接
            把新值写进正在跑的那个 Config 对象里，立刻生效。
分类依据是逐个核对 app/*.py 里读取该字段的位置得出的，不是猜的。

注意：本文里凡是中文引号一律用「」，不要用直引号 —— 直引号会提前闭合
Python 字符串（这个坑已经踩过一次）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 「需重启」名单的单一真源在 app/config.py（机器人本体那边）。
# 面板这里只读它，保证徽章显示的和实际行为不会分家。
try:
    from app.config import HOT_EXCLUDE
except Exception:                                    # 单独跑本文件时不至于炸
    HOT_EXCLUDE = {
        "onebot.mode", "onebot.host", "onebot.port", "onebot.path",
        "onebot.url", "onebot.access_token",
        "llm.api_key", "llm.base_url",
        "database.path", "logging.level", "logging.file",
    }


# ──────────────────────────────────────────────────────────────────────
#  分组
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Group:
    id: str
    title: str
    desc: str
    advanced: bool = False


GROUPS: List[Group] = [
    Group("llm", "模型与花销",
          "用哪个模型、开不开思考模式。这一组是花钱的大头，改之前先看说明。"),
    Group("onebot", "接入 NapCat",
          "和 QQ 协议端怎么连接。这一组的端口改完必须重启，因为 socket 在启动时就绑好了。"),
    Group("budget", "花钱闸门",
          "日预算、月预算、省电模式和硬停线。超过预算会自动降级，不会突然烧光。"),
    Group("context", "上下文与压缩",
          "历史攒到多长就压成摘要。这是省 token 的核心，压缩比调太大会丢细节。"),
    Group("humanize", "说话节奏",
          "像不像人的关键：读多久、打多久、分几条发、什么时候晚点回。最值得慢慢调的一组。"),
    Group("tts", "语音（TTS）",
          "本地 GPT-SoVITS 的接入参数。默认关闭 —— 打开后它才会发语音条。"
          "音色、试听、克隆都在面板的「声音」页签里做，不在这里填。"),
    Group("boot", "开机自启",
          "登录 Windows 后自动把这一套拉起来，全程不弹黑框。"
          "真正生效的是用户级注册表（HKCU\\...\\Run），这一组只是「要拉起什么」。"),
    Group("videopush", "B 站视频推送",
          "按你关心的关键词去 B 站找视频，用思考模式筛掉垃圾，再用 BiliNote 生成笔记，最后私聊发给你。"
          "关键词写成「关键词|筛选要求」，例如「AI Agent|只看工程实践，不要卖课」。"),
    Group("life", "作息与情绪",
          "真人不 24 小时待命。睡着时不会丢消息，会挂成任务等醒了再回。"),
    Group("memory", "长期记忆",
          "全部在本地跑：分词 + IDF + 时间衰减，0 token、0 延迟。"),
    Group("persona", "人设",
          "它扮演谁、在群里叫什么名字。改人设文件会让缓存失效一次，之后重新累积。"),
    Group("whitelist", "谁能跟它说话",
          "私聊白名单、群白名单、要不要自动认主人。"),
    Group("group", "群聊",
          "群里默认只在被 @ 或叫名字时说话，而且有三层限流，防止被踢。"),
    Group("runtime", "数据与日志",
          "数据库和日志文件的位置。这一组改完必须重启。"),
    Group("pricing", "计价表",
          "元 / 百万 token。高峰 = 工作日 9-12 点、14-18 点，其余走空闲价（正好一半）。"),
]


# ──────────────────────────────────────────────────────────────────────
#  参数
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Param:
    path: str
    label: str
    type: str
    default: Any
    desc: str
    note: str = ""
    group: str = ""
    restart: bool = True
    choices: Optional[List[Dict[str, str]]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: str = ""
    placeholder: str = ""
    keys: Optional[List[str]] = None
    key_labels: Optional[Dict[str, str]] = None
    pair_labels: Optional[List[str]] = None
    advanced: bool = False
    hint: str = ""


def P(path: str, label: str, type: str, default: Any, desc: str, *,
      note: str = "", group: str = "", restart: bool = True,
      choices=None, min=None, max=None, step=None, unit: str = "",
      placeholder: str = "", keys=None, key_labels=None, pair_labels=None,
      advanced: bool = False, hint: str = "") -> Param:
    # restart 不从调用处传的值来，而是查 app/config.py 里的 HOT_EXCLUDE —— 单一真源。
    return Param(path=path, label=label, type=type, default=default, desc=desc,
                 note=note, group=group, restart=(path in HOT_EXCLUDE),
                 choices=choices,
                 min=min, max=max, step=step, unit=unit, placeholder=placeholder,
                 keys=keys, key_labels=key_labels, pair_labels=pair_labels,
                 advanced=advanced, hint=hint)


_ALL: List[Param] = [
    # ══════════════ llm ══════════════
    P("llm.api_key", "API Key", "password", "",
      "调用 DeepSeek 接口用的密钥，在 platform.deepseek.com 申请。",
      note="留空则读环境变量 DEEPSEEK_API_KEY。这个值会明文写进 config.yaml，别外传。",
      group="llm", restart=True,
      placeholder="sk-...",
      hint="改 Key 需要重启：Authorization 头在进程启动时就固定了"),
    P("llm.base_url", "接口地址", "str", "https://api.deepseek.com",
      "DeepSeek 的 API 根地址。除非用代理，否则不要改。",
      note="改完需重启：httpx 客户端的 base_url 在初始化时固定。",
      group="llm", restart=True, advanced=True),
    P("llm.model", "模型", "enum", "deepseek-flash",
      "用哪个模型。flash 便宜 6 倍，陪聊完全够用。",
      note="deepseek-flash 支持看图；deepseek-v4-pro 不支持。价格差在计价表那一组里。",
      group="llm", restart=False,
      choices=[{"value": "deepseek-flash", "label": "deepseek-flash（便宜 6 倍，推荐）"},
               {"value": "deepseek-v4-pro", "label": "deepseek-v4-pro（更强，贵 4~6 倍）"}],
      hint="热更新：模型名每轮请求现读"),
    P("llm.thinking", "思考模式", "bool", False,
      "开启后模型会先「想」再回答，思维链按输出 token 计费（4~8 元/百万）。",
      note="★ 头号省钱开关。陪聊场景每轮白烧几百 token，开一天就是十几块。关掉对闲聊质量几乎没影响。",
      group="llm", restart=False,
      hint="这是最省钱的一个开关，建议保持关闭"),
    P("llm.max_tokens", "回复长度上限", "int", 200,
      "单条回复最多生成多少 token。陪聊 200 足够。",
      note="★ 输出按 4~8 元/百万计费，调大直接多花钱。1 个汉字约 0.6~0.7 token。",
      group="llm", restart=False, min=20, max=4000, step=10, unit="tok"),
    P("llm.temperature", "随机度", "float", 1.3,
      "越大越放飞。只在思考模式关闭时有效。",
      note="陪聊建议 1.1~1.4：太低像客服，太高会胡言乱语。",
      group="llm", restart=False, min=0.0, max=2.0, step=0.05),
    P("llm.timeout", "超时", "float", 60.0,
      "单次请求等多久算失败。",
      note="单位秒。网络差可以调大；调太大会让回复卡很久。",
      group="llm", restart=False, min=5, max=300, step=5, unit="秒",
      advanced=True),
    P("llm.mock", "离线模拟", "bool", False,
      "打开后完全不联网，用假的固定回复跑通流程，不花一分钱。",
      note="自测用。真实使用时必须是 false，否则它永远只会回同一句话。",
      group="llm", restart=False,
      hint="热更新：每轮请求现读，可以随时开关"),
    P("llm.web_search", "联网搜索", "bool", True,
      "是否把联网搜索当作工具交给模型，由它自己决定什么时候去搜。",
      note="搜索工具的定义跟着缓存前缀走，平时一次都不调、几乎不花钱；真搜一次约 0.001~0.003 元。关掉 = 完全不发工具，它也永远不会联网。",
      group="llm", restart=False,
      hint="热更新：每轮请求现读"),
    P("llm.max_tool_rounds", "搜索最多几轮", "int", 2,
      "一轮回复里最多来回调用几次工具。",
      note="2 就够（搜一次、觉得不够再打开一个网页）。调大只会让话变慢变贵，不会变准。",
      group="llm", restart=False, min=0, max=5, step=1, unit="轮",
      advanced=True, hint="热更新"),

    # ══════════════ onebot ══════════════
    P("onebot.mode", "连接方式", "enum", "reverse",
      "reverse = 本程序监听端口，NapCat 连过来；forward = 本程序主动连 NapCat。",
      note="推荐 reverse：NapCat 重启不会把程序带断。改完必须重启。",
      group="onebot", restart=True,
      choices=[{"value": "reverse", "label": "reverse（程序监听，推荐）"},
               {"value": "forward", "label": "forward（程序主动连）"}]),
    P("onebot.host", "监听地址", "str", "0.0.0.0",
      "reverse 模式下绑定哪个网卡。本机用 0.0.0.0 或 127.0.0.1 都行。",
      note="改完必须重启：socket 在启动时绑定。",
      group="onebot", restart=True, advanced=True),
    P("onebot.port", "监听端口", "int", 3001,
      "reverse 模式下监听的端口。要和 NapCat 网络配置里填的端口一致。",
      note="改完必须重启。如果提示端口被占用，多半是上一个 main.py 没退干净。",
      group="onebot", restart=True, min=1, max=65535, step=1),
    P("onebot.path", "WebSocket 路径", "str", "/onebot/v11/ws",
      "reverse 模式下的 URL 路径。要和 NapCat 里填的一致。",
      note="改完必须重启。",
      group="onebot", restart=True, advanced=True),
    P("onebot.url", "NapCat 地址", "str", "ws://127.0.0.1:3000",
      "forward 模式下主动去连的地址。",
      note="只有 mode = forward 时才用到。改完必须重启。",
      group="onebot", restart=True, advanced=True),
    P("onebot.access_token", "连接令牌", "password", "",
      "和 NapCat 网络配置里填的 token 保持一致。",
      note="不填就两边都留空。改完必须重启。",
      group="onebot", restart=True, advanced=True),

    # ══════════════ budget ══════════════
    P("budget.daily_yuan", "日预算", "float", 6.0,
      "一天最多花多少钱，超了自动转省电模式（上下文变短、回复变短）。",
      note="单位元。省电模式不会不回消息，只是回得更省。",
      group="budget", restart=False, min=0.1, max=1000, step=0.5, unit="元"),
    P("budget.monthly_yuan", "月预算", "float", 45.0,
      "一个月最多花多少钱，同样触发省电模式。",
      note="单位元。",
      group="budget", restart=False, min=1, max=10000, step=5, unit="元"),
    P("budget.min_interval_seconds", "最小回复间隔", "float", 1.0,
      "两条回复之间至少隔这么久，防止连续刷屏被风控盯上。",
      note="单位秒。设 0 就是不限。",
      group="budget", restart=False, min=0, max=60, step=0.5, unit="秒"),
    P("budget.saver_history_tokens", "省电模式上下文", "int", 600,
      "进入省电模式后，历史预算从 context.history_tokens 降到这个值。",
      note="单位 token。调小更省，但记性会变差。",
      group="budget", restart=False, min=100, max=8000, step=100, unit="tok"),
    P("budget.saver_max_tokens", "省电模式回复上限", "int", 80,
      "进入省电模式后，单条回复长度上限。",
      note="单位 token。",
      group="budget", restart=False, min=20, max=1000, step=10, unit="tok"),
    P("budget.hard_stop_multiplier", "硬停倍数", "float", 3.0,
      "花到日预算的几倍就彻底停止回复，必须人工改配置才能恢复。",
      note="最后一道闸门。到了这一步它完全不回消息，日志里会写明原因。",
      group="budget", restart=False, min=1.5, max=20, step=0.5),

    # ══════════════ context ══════════════
    P("context.history_tokens", "历史预算", "int", 1500,
      "累积的历史超过这个长度就压成摘要。★ 是压缩，不是丢弃。",
      note="单位 token。调大记性更好但每轮都更贵；调小省钱。1500 是陪聊的甜点值。",
      group="context", restart=False, min=200, max=12000, step=100, unit="tok"),
    P("context.compress_ratio", "压缩比例", "float", 0.6,
      "每次压缩吃掉最老的多大比例。",
      note="取值范围 0.1~0.9。太大会一次丢太多细节，太小会压缩得太频繁（每次都花一次摘要的钱）。",
      group="context", restart=False, min=0.1, max=0.9, step=0.05),
    P("context.summary_tokens", "摘要输出上限", "int", 300,
      "生成摘要和画像时最多输出多少 token。",
      note="只有触发压缩时才花这笔钱。",
      group="context", restart=False, min=50, max=2000, step=50, unit="tok",
      advanced=True),
    P("context.profile_tokens", "画像输出上限", "int", 200,
      "生成用户画像（称呼/工作/喜好/雷区）时最多输出多少 token。",
      note="和摘要同一次调用产出，不额外花钱。",
      group="context", restart=False, min=50, max=2000, step=50, unit="tok",
      advanced=True),

    # ══════════════ humanize ══════════════
    P("humanize.read_delay_min", "读延迟下限", "float", 0.4,
      "收到消息后先「愣」多久才开始打字，模拟人看到消息需要时间。",
      note="单位秒。这一段完全不输出内容，是纯粹的拟人成本（不影响 token）。",
      group="humanize", restart=False, min=0, max=30, step=0.1, unit="秒"),
    P("humanize.read_delay_max", "读延迟上限", "float", 1.8,
      "读延迟的上限，实际值在这个区间里随机取。",
      note="单位秒。",
      group="humanize", restart=False, min=0, max=60, step=0.1, unit="秒"),
    P("humanize.typing_per_char_min", "打字速度下限", "float", 0.09,
      "每个字打多久，用来算「正在输入」的时长。",
      note="单位秒/字。越小回得越快。",
      group="humanize", restart=False, min=0.01, max=2, step=0.01, unit="秒/字"),
    P("humanize.typing_per_char_max", "打字速度上限", "float", 0.22,
      "每个字打多久的上限。",
      note="单位秒/字。",
      group="humanize", restart=False, min=0.01, max=3, step=0.01, unit="秒/字"),
    P("humanize.typing_min", "打字时长下限", "float", 1.2,
      "不管多短的一句话，至少「打」这么久。",
      note="单位秒。防止秒回显得太假。",
      group="humanize", restart=False, min=0, max=60, step=0.1, unit="秒"),
    P("humanize.typing_max", "打字时长上限", "float", 12.0,
      "打字时长的上限，超过就按上限算。",
      note="单位秒。长回复不会真的等几分钟。",
      group="humanize", restart=False, min=0, max=120, step=0.5, unit="秒"),
    P("humanize.segment_gap_min", "分条间隔下限", "float", 0.6,
      "拆成多条时，两条之间隔多久。",
      note="单位秒。真人连发两条不会瞬间发出。",
      group="humanize", restart=False, min=0, max=30, step=0.1, unit="秒"),
    P("humanize.segment_gap_max", "分条间隔上限", "float", 2.2,
      "分条间隔的上限。",
      note="单位秒。",
      group="humanize", restart=False, min=0, max=60, step=0.1, unit="秒"),
    P("humanize.segment_max_len", "单条长度上限", "int", 60,
      "回复超过这个字数就拆成两条。",
      note="单位字。调小会拆更多条，显得话多且碎。",
      group="humanize", restart=False, min=10, max=500, step=5, unit="字"),
    P("humanize.segment_max_count", "最多拆几条", "int", 3,
      "拆条的上限，超出的部分合并回最后一条。",
      note="防止一次甩出七八条刷屏（那是最容易被当成机器人的行为）。",
      group="humanize", restart=False, min=1, max=10, step=1, unit="条"),
    P("humanize.debounce_seconds", "合并等待", "float", 1.2,
      "他连发三条时，等这么久把三条合并成一次回复。",
      note="★ 既省 token 又像人（真人不会你每发一句就秒回一句）。设 0 就是每条都单独回。",
      group="humanize", restart=False, min=0, max=30, step=0.1, unit="秒"),
    P("humanize.late_reply_chance", "晚回概率", "float", 0.08,
      "有多大比例的消息会故意拖一会儿再回，模拟人在忙别的事。",
      note="取值 0~1。0.08 = 8%。设 0 就永远秒回（会显得太闲）。",
      group="humanize", restart=False, min=0, max=1, step=0.01),
    P("humanize.late_reply_min", "晚回最短", "float", 60.0,
      "触发晚回时，至少拖这么久。",
      note="单位秒。默认 60 秒。",
      group="humanize", restart=False, min=1, max=86400, step=60, unit="秒",
      advanced=True),
    P("humanize.late_reply_max", "晚回最长", "float", 1500.0,
      "触发晚回时，最多拖这么久。",
      note="单位秒。默认 1500 秒 = 25 分钟。",
      group="humanize", restart=False, min=1, max=86400, step=60, unit="秒",
      advanced=True),
    P("humanize.trivial_replies", "本地规则回复", "pair_list", [
        ["^(在|在吗|在不在|在么)[？?！!。.]*$", "在"],
        ["^(嗯|恩|哦|噢|好吧|行吧)[。.！!]*$", "嗯"],
        ["^(晚安|睡了|睡觉了|困了)[。.！!]*$", "晚安"],
        ["^(哈哈|哈哈哈|hhhh)+[。.！!]*$", "笑死"],
      ],
      "命中这些正则的消息直接用固定话术回复，完全不调模型，0 token。",
      note="这是最便宜的一项优化。但别加太多，否则会显得很机械 —— 真人不会每句都回得一模一样。",
      group="humanize", restart=False,
      pair_labels=["正则表达式", "回复内容"],
      hint="左边是正则（要能匹配整句话），右边是固定回复"),

    # ══════════════ life ══════════════
    P("life.enabled", "启用作息", "bool", True,
      "关掉的话它就 24 小时在线，任何时候都秒回 —— 这本身就很不像人。",
      note="关掉后下面所有作息设置失效，晚上的消息也会立刻回。",
      group="life", restart=False),
    P("life.sleeping", "睡觉时段", "range", [1, 8],
      "这段时间内完全不回复。消息不会丢，会挂成任务等醒了再回。",
      note="支持跨零点（比如 [23, 8] 表示晚上 11 点到早上 8 点）。",
      group="life", restart=False, pair_labels=["从", "到"], unit="点",
      hint="跨零点写 [23, 8] 就行"),
    P("life.working", "上班时段", "range_list", [[9, 12], [14, 18]],
      "这些时间段里回得短、有点敷衍（因为在上班）。",
      note="可以加多段。只有 weekday_only_work 为 true 时才只在工作日生效。",
      group="life", restart=False, pair_labels=["从", "到"], unit="点"),
    P("life.weekday_only_work", "只在工作日上班", "bool", True,
      "关掉的话周末也会按上班时段的语气说话。",
      note="",
      group="life", restart=False),
    P("life.moods", "各时段心情", "map", {
        "sleeping": "刚睡醒，还在迷糊",
        "working": "在上班，偷偷摸鱼回消息",
        "winding_down": "晚上在家瘫着，懒得动",
        "free": "闲着，有空聊天",
      },
      "每种状态下它会带什么情绪。这段文字会进当轮动态块，影响说话语气。",
      note="不会破坏缓存（动态块本来就每轮都变）。可以随便写得具体一点。",
      group="life", restart=False,
      keys=["sleeping", "working", "winding_down", "free"],
      key_labels={"sleeping": "睡觉中", "working": "上班中",
                  "winding_down": "晚上歇着", "free": "有空"}),
    P("life.proactive.enabled", "启用主动搭话", "bool", True,
      "他不说话的时候，偶尔主动找他一次。",
      note="关掉的话它永远不会主动开口。",
      group="life", restart=False),
    P("life.proactive.min_idle_hours", "最少安静多久", "float", 6.0,
      "至少安静这么久才可能主动开口。",
      note="单位小时。调小会显得黏人。",
      group="life", restart=False, min=0, max=240, step=0.5, unit="小时"),
    P("life.proactive.max_per_day", "每天最多几次", "int", 2,
      "★ 每天最多主动找几次。",
      note="调高会非常像推销号。真人没事不会一天问你八遍「在干嘛」。",
      group="life", restart=False, min=0, max=20, step=1, unit="次"),
    P("life.proactive.active_hours", "允许主动的时段", "range", [10, 23],
      "只在一天中的这段时间内主动开口。",
      note="支持跨零点。凌晨主动发消息会吓到人。",
      group="life", restart=False, pair_labels=["从", "到"], unit="点"),
    P("life.proactive.check_interval_minutes", "检查间隔", "float", 15.0,
      "每隔多久看一次要不要开口。",
      note="单位分钟。程序内部强制不低于 1 分钟。",
      group="life", restart=False, min=1, max=1440, step=1, unit="分钟",
      advanced=True),
    P("life.proactive.topics_from_memory", "从记忆里挑几个由头", "int", 3,
      "主动搭话时从长期记忆里挑几条旧事当话题。",
      note="没有由头它就不会开口（干巴巴的「在吗」比不发更减分）。设 0 就永远不会主动说话。",
      group="life", restart=False, min=0, max=10, step=1, unit="条",
      advanced=True),
    P("life.proactive.targets", "只主动搭话给谁", "int_list", [],
      "主动开口时只找这几个 QQ 号。",
      note="留空 = 只找「谁能跟它说话」里的主人；主人也没配，才退回「聊过的私聊对象都可能被搭话」。"
           "朋友可以跟它聊天，但默认不该被它主动打扰 —— 这是两件事。群聊永远不主动开腔。",
      group="life", restart=False, unit="QQ 号"),
    P("life.proactive.min_hours_between", "两次之间至少隔", "float", 2.0,
      "同一个人两次主动之间至少隔这么久。",
      note="防止把一整天的话都攒给同一个人。设 0 关掉这道闸。",
      group="life", restart=False, min=0, max=72, step=0.5, unit="小时",
      advanced=True),

    # ══════════════ memory ══════════════
    P("memory.enabled", "启用长期记忆", "bool", True,
      "关掉后它只会记得当前对话窗口内的内容，跨天的旧事全忘。",
      note="检索本身 0 token，只有「抽取」会花一点钱。",
      group="memory", restart=False),
    P("memory.extract_every_turns", "每几轮抽一次", "int", 20,
      "每聊这么多轮，把对话里值得记的事提取一次。",
      note="这是记忆功能里唯一花 token 的地方。调大更省，但记性更新得慢。",
      group="memory", restart=False, min=2, max=200, step=1, unit="轮"),
    P("memory.extract_min_turns", "首次最少几轮", "int", 6,
      "第一次抽取至少要有这么多轮对话。",
      note="刚聊两句就抽，抽不出有意义的长期信息。所以新装的时候聊几轮还没记忆是正常的。",
      group="memory", restart=False, min=1, max=100, step=1, unit="轮"),
    P("memory.extract_max_facts", "一次最多记几条", "int", 4,
      "单次抽取最多产出几条长期记忆。",
      note="调大会让抽取的输出变长（花更多输出 token）。",
      group="memory", restart=False, min=1, max=20, step=1, unit="条"),
    P("memory.extract_max_tokens", "抽取输出上限", "int", 220,
      "抽取那一次调用最多输出多少 token。",
      note="每条事实约 30~40 token，4 条的话 220 够用。",
      group="memory", restart=False, min=50, max=2000, step=10, unit="tok",
      advanced=True),
    P("memory.top_k", "每轮注入几条", "int", 3,
      "每轮对话最多把几条旧记忆塞进上下文。",
      note="每条都会增加当轮输入 token。3 条是个平衡点。",
      group="memory", restart=False, min=0, max=20, step=1, unit="条"),
    P("memory.max_chars", "每条截断到多少字", "int", 30,
      "注入时每条记忆最多保留多少字。",
      note="单位字。太长会挤占历史预算。",
      group="memory", restart=False, min=5, max=200, step=5, unit="字"),
    P("memory.min_sim", "相似度门槛", "float", 0.45,
      "★ 他这句话的信息量，至少要有这么多比例被某条记忆接住，才注入。",
      note="只卡「像不像」，不卡新旧 —— 半年前但完全对得上的事仍然会被想起，只是排序靠后。调高 = 更少但更准。",
      group="memory", restart=False, min=0, max=1, step=0.05),
    P("memory.half_life_days", "时间衰减半衰期", "float", 30.0,
      "记忆的新鲜度过多少天减半。",
      note="只影响排序（先想起哪几条），不影响要不要想起。",
      group="memory", restart=False, min=1, max=3650, step=1, unit="天",
      advanced=True),
    P("memory.dedup_threshold", "去重阈值", "float", 0.72,
      "新的记忆和已有的多像就算同一条，走合并而不是新增。",
      note="调低会合并得太狠（把不同的事并成一条），调高会堆一堆重复条目。",
      group="memory", restart=False, min=0.1, max=1, step=0.02,
      advanced=True),

    # ══════════════ persona ══════════════
    P("persona.file", "私聊人设文件", "path", "persona/default.md",
      "私聊时它扮演谁。这是最能影响体验的一个文件。",
      note="相对路径按项目根目录解析。文件不存在会用一个极简的兜底人设。",
      group="persona", restart=False,
      hint="改完重启服务才会生效（人设是启动时读进内存的）"),
    P("persona.name", "名字", "str", "遥遥",
      "它在群里被叫到这个名字就会回复。",
      note="setup.py 里的人设名改的就是这一项。",
      group="persona", restart=False),
    P("persona.group_file", "群聊人设文件", "path", "persona/group.md",
      "群聊时用另一套人设。群里的分寸跟私聊不一样。",
      note="文件不存在会自动退回上面那份私聊人设，不会崩。",
      group="persona", restart=False),
    P("persona.group_name", "群里的名字", "str", "",
      "群聊时被叫到哪个名字才回。留空就用上面的「名字」。",
      note="比如私聊叫遥遥、群里叫遥神，就填这里。",
      group="persona", restart=False),

    # ══════════════ whitelist ══════════════
    P("whitelist.private", "私聊白名单", "int_list", [],
      "只理这些 QQ 号。留空 = 靠下面的自动认主人来决定。",
      note="手写就是 [123456, 789012] 这种格式。填了之后，不在这里面的人发消息会被无视。",
      group="whitelist", restart=False,
      placeholder="输入 QQ 号后回车"),
    P("whitelist.groups", "群白名单", "int_list", [],
      "★ 只在这些群里活动。留空 = 任何群都理。",
      note="留空的风险：别人把你小号拉进陌生群，它就会在里面说话。第一次收到某个群的消息时，日志会打出群号，抄过来填这里。",
      group="whitelist", restart=False,
      placeholder="输入群号后回车",
      hint="强烈建议填上，留空等于对任何群开放"),
    P("whitelist.group_reply_rate", "群内随机插话概率", "float", 0.0,
      "★ 群里没 @ 到它时，按这个概率主动插话。",
      note="0 = 完全不插话（只在被叫到时回）。在活跃群里随机插话是会被踢和举报的，调之前想好。",
      group="whitelist", restart=False, min=0, max=1, step=0.01,
      hint="要让它随机插话，还得把群聊那组的 address_required 关掉"),
    P("whitelist.auto_accept_first", "自动认主人", "int", 1,
      "前 N 个私聊它的人自动进白名单（写进本地数据库，不改配置文件）。",
      note="默认 1：你拿主号发一条「在吗」就绑定好了，不用手抄 QQ 号。设 0 = 关掉，此时变成谁都能聊。",
      group="whitelist", restart=False, min=0, max=10, step=1, unit="个"),
    P("whitelist.masters", "谁是主人", "int_list", [],
      "★ 填你自己的 QQ 号。这是「谁被当成主人」，跟上面「谁能跟我说话」是两件事。",
      note="留空 = 老行为，能私聊它的就一律当主人（单人自用最省事）。一旦填了，不在名单里的私聊对象会被当成「主人允许来聊天的朋友」：照常聊天，但不会被叫老大、也不会被当成主人本人，更不会把你的私事讲给他听。",
      group="whitelist", restart=False,
      placeholder="输入你自己的 QQ 号后回车",
      hint="热更新；改完下一轮就生效（会话缓存前缀会重建一次）"),

    # ══════════════ group ══════════════
    P("group.enabled", "启用群聊", "bool", True,
      "关掉 = 完全不理群消息（私聊不受影响）。",
      note="",
      group="group", restart=False),
    P("group.address_required", "只在被叫到时回", "bool", True,
      "★ true = 只理 @ 它和叫它名字的；false = 按上面的随机插话概率。",
      note="强烈建议保持 true。改成 false 之前先想清楚会不会被踢。",
      group="group", restart=False),
    P("group.per_user_cooldown_seconds", "同一个人冷却", "float", 20.0,
      "同一个人在群里连着 @ 它，两次回复至少隔这么久。",
      note="单位秒。防止一个人连着问就把它变成复读机。",
      group="group", restart=False, min=0, max=3600, step=1, unit="秒"),
    P("group.reply_cooldown_seconds", "整个群冷却", "float", 8.0,
      "★ 不管谁说话，整个群两次回复至少隔这么久。",
      note="单位秒。这是防刷屏的第一道闸门。",
      group="group", restart=False, min=0, max=3600, step=1, unit="秒"),
    P("group.max_replies_per_hour", "每小时上限", "int", 30,
      "硬上限：这个群每小时最多回几条，到量就闭嘴，下一小时恢复。",
      note="单位条。活跃群里这是保命的数字。",
      group="group", restart=False, min=1, max=1000, step=1, unit="条"),
    P("group.history_tokens", "群聊上下文预算", "int", 900,
      "群聊单独的历史预算，一般比私聊更紧。",
      note="单位 token。群里人多、话题散，记得越多越贵而且越容易串味。0 = 跟私聊共用。",
      group="group", restart=False, min=0, max=12000, step=100, unit="tok"),
    P("group.max_segments", "群里最多发几条", "int", 1,
      "群里一次最多发几条消息。",
      note="默认 1。连发三条在几个人的小群里非常扎眼。",
      group="group", restart=False, min=1, max=10, step=1, unit="条"),

    # ══════════════ runtime ══════════════
    P("database.path", "数据库文件", "path", "data/companion.db",
      "聊天记录、长期记忆、花费流水都存在这里。只存本地，不外传。",
      note="改完必须重启：sqlite 连接在启动时建立。改路径等于换一套记忆。",
      group="runtime", restart=True),
    P("logging.level", "日志级别", "enum", "INFO",
      "写进日志文件的详细程度。",
      note="DEBUG 会多记录很多细节（排查问题用），日常用 INFO。改完必须重启：日志 handler 在启动时建立。",
      group="runtime", restart=True,
      choices=[{"value": "DEBUG", "label": "DEBUG（最详细，排查用）"},
               {"value": "INFO", "label": "INFO（推荐）"},
               {"value": "WARNING", "label": "WARNING（只记问题）"},
               {"value": "ERROR", "label": "ERROR（只记错误）"}]),
    P("logging.file", "日志文件", "path", "data/companion.log",
      "运行日志写到哪个文件。面板的实时日志读的就是这个文件。",
      note="改完必须重启。",
      group="runtime", restart=True),

    # ══════════════ pricing ══════════════
    P("pricing.currency_per_million", "每百万 token 单价", "price_table", {
        "deepseek-flash": {
          "cache_hit": {"peak": 0.04, "offpeak": 0.02},
          "cache_miss": {"peak": 2.0, "offpeak": 1.0},
          "output": {"peak": 8.0, "offpeak": 4.0},
        },
        "deepseek-v4-pro": {
          "cache_hit": {"peak": 0.30, "offpeak": 0.15},
          "cache_miss": {"peak": 9.0, "offpeak": 4.5},
          "output": {"peak": 27.0, "offpeak": 13.5},
        },
      },
      "各模型在高峰/空闲时段的价格，用来算每轮花了多少钱。",
      note="单位：元 / 百万 token。★ 未命中缓存比命中贵 50 倍（flash），所以省钱的本质是提高缓存命中率。价格若官方调整，改这里即可。",
      group="pricing", restart=False,
      keys=["deepseek-flash", "deepseek-v4-pro"],
      key_labels={"cache_hit": "缓存命中（输入）", "cache_miss": "未命中（输入）",
                  "output": "输出"},
      pair_labels=["高峰", "空闲"],
      advanced=True),
    P("pricing.peak_weekdays", "高峰是星期几", "int_list", [0, 1, 2, 3, 4],
      "哪几天算高峰（0 = 周一，6 = 周日）。",
      note="高峰时段价格是空闲的两倍。默认工作日是高峰。",
      group="pricing", restart=False, advanced=True),
    P("pricing.peak_ranges", "高峰时段", "range_list", [[9, 12], [14, 18]],
      "一天里哪几段算高峰。",
      note="默认北京时间 9-12 点、14-18 点。其余时间（含周末和法定节假日）走空闲价，正好一半。",
      group="pricing", restart=False, pair_labels=["从", "到"], unit="点",
      advanced=True),
    P("pricing.holidays", "法定节假日", "str_list", [],
      "这些日期按空闲价算（因为不上班，API 也半价）。",
      note="按年作废。用 python tools/holidays.py --write 拉当年的日历自动填，别手写。留空会让节假日被当成高峰。",
      group="pricing", restart=False,
      placeholder="2026-01-01",
      advanced=True),

    # ══════════════ tts ══════════════
    P("tts.enabled", "允许发语音", "bool", False,
      "打开后它才能在聊天里发语音条。默认关闭：语音比文字慢，也更容易露馅。",
      note="关着的时候「声音」页签照样能用（试听、克隆都不受影响），只是机器人不会主动发。",
      group="tts", restart=False,
      hint="热更新：每次要发语音时现读"),
    P("tts.always_voice", "全语音模式（每条回复都发语音）", "bool", False,
      "打开后每条回复都会变成语音条，不看下面的「什么时候发」「概率」「字数上限」。",
      note="这是个覆盖开关，不改动其它参数 —— 关掉之后立刻回到你原来的设置。"
           "「别发语音」这类明确拒绝仍然有效。群里也会每条都发，慎开。",
      group="tts", restart=False,
      hint="面板「声音」页右上角有同一个按钮，点一下就行"),
    P("tts.trigger", "什么时候发语音", "enum", "request",
      "request 只在你说「来条语音」这类话时才发；auto 还会按概率主动发。",
      note="改 auto 前先把下面的概率调小（0.05~0.1）。语音条在群里很扎眼，每条还多等两三秒。",
      group="tts", restart=False,
      choices=[{"value": "request", "label": "request（只在被要求时）"},
               {"value": "auto", "label": "auto（偶尔主动发）"}]),
    P("tts.chance", "主动发语音的概率", "float", 0.15,
      "auto 模式下，每条回复变成语音的概率。",
      note="0.15 大约是每六七条回复一条语音。设 0 就等于退回「只在被要求时」。",
      group="tts", restart=False, min=0.0, max=1.0, step=0.05),
    P("tts.group", "群里也主动发语音", "bool", False,
      "打开后群里也会按概率发语音。",
      note="默认关闭：群里一条语音弹出来所有人都得听，还容易被管理员盯上。被点名要语音时不受这个开关限制。",
      group="tts", restart=False),
    P("tts.api_base", "语音服务地址", "str", "http://127.0.0.1:9880",
      "GPT-SoVITS 的 api_v2.py 地址。默认端口 9880。",
      note="只绑本机时不要改成 0.0.0.0。改了之后先在「声音」页签点「刷新」看能不能连上。",
      group="tts", restart=False,
      placeholder="http://127.0.0.1:9880"),
    P("tts.install_dir", "GPT-SoVITS 目录", "path", "D:/GPT-SoVITS-deploy",
      "部署根目录，下面应该有 refs 参考音频、app 程序和 api_v2.log。",
      note="换机器只改这一项，音色库和参考音频会跟着走。填错的话「声音」页签会直接告诉你哪个没找到。",
      group="tts", restart=False,
      placeholder="D:/GPT-SoVITS-deploy"),
    P("tts.max_chars", "单条语音字数上限", "int", 120,
      "超过这个字数就只念前面这么多。",
      note="实测 120 字约 30 秒语音，再长没人听完，合成也更慢。想让它「说话简短」就把这个调小。",
      group="tts", restart=False, min=20, max=600, step=10, unit="字"),
    P("tts.timeout", "合成超时", "float", 60.0,
      "等一条语音最多等多久（秒）。",
      note="服务冷启动第一次合成要 7~8 秒，别设太小。超时后这条语音会降级成文字发出去。",
      group="tts", restart=False, min=5, max=300, step=5, unit="秒"),
    P("tts.speed", "语速", "float", 1.0,
      "1.0 正常。0.9 温柔，1.1 活泼。",
      note="超过 1.3 音质会明显劣化（尤其 v2ProPlus）。想让它说话快一点，优先改人设而不是这里。",
      group="tts", restart=False, min=0.6, max=1.5, step=0.05),
    P("tts.default_emotion", "默认情绪", "enum", "neutral",
      "没指定情绪时用 refs 里的哪套参考音频。",
      note="GPT-SoVITS 没有「语气指令」，情绪完全由参考音频决定。refs 里缺这个文件会回退到 default.wav。",
      group="tts", restart=False,
      choices=[{"value": "neutral", "label": "neutral（平静）"},
               {"value": "happy", "label": "happy（开心）"},
               {"value": "sad", "label": "sad（低落）"},
               {"value": "angry", "label": "angry（不耐烦）"},
               {"value": "gentle", "label": "gentle（温柔）"}]),

    # ══════════════ boot ══════════════
    P("boot.enabled", "开机自启（登录时）", "bool", False,
      "打开后，每次登录 Windows 都会自动把机器人拉起来，看不到任何黑框。",
      note="触发时机是「用户登录」而不是「开机」—— QQ 和 NapCat 都是用户态程序，"
           "做成系统服务会跑在 Session 0（没有桌面），QQ 根本登不上。"
           "实际生效的是注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，"
           "这个开关和面板顶栏的按钮是同一件事。",
      group="boot", restart=False,
      hint="改完不用重启：下次登录才生效"),
    P("boot.delay", "登录后等几秒再启动", "int", 10,
      "登录后先等这么久，再开始拉程序。",
      note="刚登录时桌面、网卡、代理设置都还没就位，抢跑会连不上 NapCat，"
           "还会把一堆假错误写进日志。10 秒够机械硬盘的机器把网卡唤醒。",
      group="boot", restart=False, min=0, max=300, step=5, unit="秒"),
    P("boot.napcat", "顺带启动 NapCat（QQ 登录）", "bool", True,
      "登录后自动把 NapCat 拉起来，QQ 才会处于在线状态。",
      note="NapCat 要往 QQ 进程里注入 DLL，这件事必须有管理员权限；"
           "所以走的是「最高权限的计划任务」，用 schtasks /run 触发，**不弹 UAC**。"
           "任务只用装一次：双击项目根目录的 install_napcat_task.bat。"
           "没装任务时会尝试普通权限拉起，但注入会被系统拒绝（Error Code: 5）。"
           "真正拉起走的是 NapCat 自带的 start-napcat.bat（它会部署 loadNapCat.js、"
           "自己找 QQ.exe、按 NAPCAT_QUICK_QQ 决定快速登录），不是直接调注入器。",
      group="boot", restart=False),
    P("boot.napcat_qq", "快速登录的 QQ 号", "str", "",
      "NapCat 用哪个号登录。留空就从 NapCat 的 start-napcat.bat 里读。",
      note="填了就不用扫码。首次登录或 QQ 让会话失效时才需要扫码 —— "
           "扫码必须看得见窗口，用 python boot.py --now --show 跑一次。",
      group="boot", restart=False, placeholder="123456789"),
    P("boot.napcat_path", "NapCat 启动脚本（手动指定）", "str", "",
      "一般不用填。填了就不再自动找目录，改成按这个脚本/exe 启动。",
      note="自动找的顺序：这个字段 → 环境变量 NAPCAT_SHELL → 项目旁边的 "
           "NapCatQQ\\NapCat.Shell → 各盘根目录下的 NapCat.Shell。"
           "手动填 .bat 时要注意：它会自己提权，每次登录都会弹 UAC。",
      group="boot", restart=False,
      placeholder="D:/QQBot/NapCatQQ/NapCat.Shell/start-napcat.bat"),
    P("boot.bilinote", "顺带启动 BiliNote（视频笔记）", "bool", True,
      "登录后自动拉起 BiliNote 后端，视频推送要用它生成笔记。",
      note="它起得比较慢（加载 whisper 模型约 10 秒），但和机器人互不依赖，"
           "所以排在机器人后面点火。视频推送关着时这一步会自动跳过。",
      group="boot", restart=False),
    P("boot.tts", "顺带启动语音服务", "bool", False,
      "登录后自动拉起 GPT-SoVITS（加载约 1 分钟）。",
      note="它是最慢的一环，所以会在机器人之前先点火，加载的同时别的程序在起。"
           "不打开发语音时会被自动降级成文字（不会卡住，只是没声音）。",
      group="boot", restart=False),
    P("boot.panel", "顺带启动管理面板", "bool", True,
      "登录后自动把面板挂起来（隐藏窗口，http://127.0.0.1:8899）。",
      note="面板只是个本地网页服务，不占资源。关掉的话以后要双击 panel.bat 才有页面。",
      group="boot", restart=False),
    P("boot.open_panel", "启动后自动打开浏览器", "bool", True,
      "面板起来后顺手用默认浏览器打开它的地址。",
      note="全隐藏模式下这是登录后**唯一看得见**的东西。"
           "不想要就关掉 —— 服务照常在跑，只是不弹页面。",
      group="boot", restart=False),
    P("boot.headless", "隐藏所有窗口", "bool", True,
      "启动机器人、面板、NapCat、语音服务时都不弹控制台，QQ 自己的窗口也一并藏起来。",
      note="关掉之后每个程序都会弹一个黑框（排查时有用）。"
           "隐藏是用 DETACHED_PROCESS 实现的 —— 子进程压根不分配控制台，"
           "不是「先弹出来再藏起来」，所以连闪一下都没有。"
           "QQ 的登录窗/主窗和 NapCat 注入器那个黑框是另一套办法（EnumWindows 找窗口再 ShowWindow 藏），"
           "由 data/napcat-nohide.flag 临时关掉。",
      group="boot", restart=False),

    # ══════════════ videopush ══════════════
    P("videopush.enabled", "启用视频推送", "bool", True,
      "打开后，每天到点去 B 站搜视频、生成笔记、私聊发给你。",
      note="关掉只是不主动推；已经生成好的笔记还会留着（data/notes/），"
           "想立刻跑一次可以在面板上点「立即跑一次」。",
      group="videopush", restart=False, hint="热更新：下一轮调度就按新值来"),
    P("videopush.topics", "感兴趣的关键词", "str_list", [],
      "★ 想让它帮你盯着什么，一条一个。格式：关键词|筛选要求。",
      note="竖线后面的筛选要求会原样写进提示词，用来告诉 AI 你要什么、不要什么。"
           "例如「AI Agent|只看工程实践和踩坑，不要课程广告」；"
           "「算法竞赛|ACM/蓝桥杯 题解，C++ 为主」。不写竖线就只按关键词搜。",
      group="videopush", restart=False,
      placeholder="AI Agent|只看工程实践，不要卖课"),
    P("videopush.times", "每天几点推", "str_list", ["09:00", "21:00"],
      "每天在这些时间点各跑一轮（24 小时制 HH:MM）。",
      note="停机错过的点，在 catchup_hours 之内会补跑一次；超过就不补了，"
           "免得一开机连推好几轮。想加一次就再加一行，比如 12:30。",
      group="videopush", restart=False, placeholder="09:00"),
    P("videopush.targets", "推给谁", "int_list", [],
      "收推送的 QQ 号。留空 = 用「谁是主人」那一栏（whitelist.masters）。",
      note="两个都空的话，视频照样会去搜、笔记也会生成，只是没人收，日志里会提醒。",
      group="videopush", restart=False,
      placeholder="输入 QQ 号后回车"),
    P("videopush.groups", "推到哪些群", "int_list", [],
      "也要收推送的群号。在群里发「视频推送 开」会自动把那个群加到这里，"
      "发「视频推送 关」就摘掉。",
      note="群推送和私聊推送互不影响，同一条总结两边都发。",
      group="videopush", restart=False,
      placeholder="输入群号后回车"),
    P("videopush.max_per_day", "每天最多推几条", "int", 3,
      "一天最多推几条视频。推多了会从「惊喜」变成「骚扰」。",
      note="包含补跑。已经推满就只更新上次运行时间，不会再生成笔记。",
      group="videopush", restart=False, min=1, max=20, step=1, unit="条"),
    P("videopush.max_per_topic", "每个关键词最多几条", "int", 2,
      "一个关键词一轮最多选中几条，防止一个话题把名额吃光。",
      note="两个关键词各 2 条，一天就是 4 条 —— 但 max_per_day 会先卡住。",
      group="videopush", restart=False, min=1, max=10, step=1, unit="条"),
    P("videopush.candidates", "每个关键词看多少候选", "int", 24,
      "搜出来多少条交给 AI 筛。太少容易没好货，太多烧 token。",
      note="这一项乘上话题数，就是每轮送进模型的条数。"
           "每条大约 60~100 token，24 条约 2000 token，很便宜。",
      group="videopush", restart=False, min=6, max=50, step=1, unit="条"),
    P("videopush.min_score", "AI 评分门槛", "int", 70,
      "模型给每条视频打 0~100 分，低于这个分数就不要。",
      note="70 = 值得看；80 以上 = 非看不可。调高会明显变少，调低会混进凑数的。"
           "注意：就算分数够，模型也可能一条都不选 —— 这是故意的，宁缺毋滥。",
      group="videopush", restart=False, min=0, max=100, step=5, unit="分"),
    P("videopush.max_age_days", "只要最近几天的", "int", 30,
      "发布时间早于这么多天的直接跳过，老视频多半看过了。",
      note="0 = 不限。B 站搜索按综合排序，会混进几年前的「经典」视频。",
      group="videopush", restart=False, min=0, max=3650, step=1, unit="天"),
    P("videopush.min_duration", "最短时长", "int", 120,
      "短于这个时长的不要（秒）。太短的通常没内容。",
      note="默认 2 分钟。注意 B 站返回的时长是「分:秒」字符串，已经换算成秒。",
      group="videopush", restart=False, min=0, max=7200, step=10, unit="秒"),
    P("videopush.max_duration", "最长时长", "int", 5400,
      "长于这个时长的不要（秒）。默认 90 分钟。",
      note="这条很重要：B 站搜索结果里混着几百上千分钟的合集，不拦会占满候选。"
           "90 分钟也够长了，再长的笔记也看不完。",
      group="videopush", restart=False, min=60, max=86400, step=60, unit="秒"),
    P("videopush.min_play", "播放量下限", "int", 300,
      "播放量低于这个数的不要。0 = 不过滤。",
      note="只是粗略的防呆，不是硬指标 —— 发布不满 3 天的新视频不看这一项"
           "（刚发的题解播放量往往还是 1），过了宽限期才按这个数挡人。",
      group="videopush", restart=False, min=0, max=10000000, step=100, unit="播放"),
    P("videopush.block_words", "标题黑名单词", "str_list",
      ["广告", "带货", "招生", "训练营", "抽奖", "福利", "加群", "领资料"],
      "标题里出现这些词就直接扔掉，连模型都不用看。",
      note="这是最便宜的一道闸：本地匹配，0 token。别把「课程」也加进去 —— "
           "大学公开课和正经教程的标题里经常有这两个字。",
      group="videopush", restart=False, placeholder="加一个词后回车"),
    P("videopush.thinking", "筛选用思考模式", "bool", True,
      "★ 让模型在筛选时先想一遍再打分。质量明显更高，也更不容易收垃圾。",
      note="跟上面「模型与花销」里的思考模式是两件事：聊天那条路必须关（每轮都烧思维链太贵），"
           "视频筛选一天只跑两三次，开 high 换更准的判断是划算的。",
      group="videopush", restart=False),
    P("videopush.filter_effort", "筛选思考档位", "enum", "high",
      "思考模式开哪一档：low 快、high 默认、max 最慢但最挑。",
      note="只在「筛选用思考模式」打开时生效。按你的要求默认 high：宁可少推两条，也别推垃圾。",
      group="videopush", restart=False,
      choices=[{"value": "low", "label": "low（快，略省）"},
               {"value": "high", "label": "high（默认，推荐）"},
               {"value": "max", "label": "max（最慢，最挑）"}]),
    P("videopush.filter_max_tokens", "筛选输出上限", "int", 3000,
      "筛选那一次调用的输出上限（思维链也算在里面）。",
      note="思考模式下 3000 够用；要是日志里经常出现返回被截断、JSON 解析失败，就往上加。",
      group="videopush", restart=False, min=500, max=20000, step=100, unit="tok"),
    P("videopush.catchup_hours", "错过多久还补跑", "float", 12.0,
      "停机错过时间点后，多少小时内还补跑一次。",
      note="默认 12 小时：早上 9 点那次错过了，晚上 9 点前起来还能补。"
           "超过就不补了，免得关机一周后一开机连推好几轮。",
      group="videopush", restart=False, min=0, max=168, step=1, unit="小时"),
    P("videopush.check_interval", "调度检查间隔", "int", 60,
      "每隔多少秒看一眼「到点了吗、有没有待发的」。",
      note="只影响调度灵敏度，不影响检索频率。60 秒足够，改小了也不会更准时。",
      group="videopush", restart=False, min=15, max=3600, step=5, unit="秒"),
    P("videopush.note_style", "笔记风格", "enum", "detailed",
      "BiliNote 生成笔记时用的风格。",
      note="detailed 是「详尽」；minimal 更短；academic 像论文笔记；"
           "tutorial 偏步骤；task_oriented 偏待办。选 detailed 最稳。",
      group="videopush", restart=False,
      choices=[{"value": "detailed", "label": "detailed（详尽，推荐）"},
               {"value": "minimal", "label": "minimal（极简）"},
               {"value": "academic", "label": "academic（学术）"},
               {"value": "tutorial", "label": "tutorial（教程）"},
               {"value": "task_oriented", "label": "task_oriented（任务导向）"},
               {"value": "business", "label": "business（商业）"},
               {"value": "meeting_minutes", "label": "meeting_minutes（会议纪要）"},
               {"value": "xiaohongshu", "label": "xiaohongshu（小红书风）"},
               {"value": "life_journal", "label": "life_journal（生活记录）"}]),
    P("videopush.note_quality", "下载质量", "enum", "fast",
      "BiliNote 下载视频时用的质量档。",
      note="fast 最快（只取音轨转写），笔记内容一样。"
           "medium/slow 会下更高清的视频，只有在需要截图时才值得。",
      group="videopush", restart=False,
      choices=[{"value": "fast", "label": "fast（最快，推荐）"},
               {"value": "medium", "label": "medium"},
               {"value": "slow", "label": "slow（最清晰，最慢）"}]),
    P("videopush.note_screenshot", "笔记里插视频截图", "bool", False,
      "生成笔记时顺带截几张视频画面插进去。",
      note="开了会明显变慢（每张截图跑一次 ffmpeg），而且 QQ 里看不到图。一般不用开。",
      group="videopush", restart=False),
    P("videopush.note_timeout", "单条笔记最多等多久", "int", 900,
      "一条笔记最多等多少秒，超时就判失败。",
      note="长视频 + whisper tiny 转写，十分钟很正常。超时的任务在 BiliNote 那边可能还在跑，"
           "但这边会先放弃，避免整轮卡死。",
      group="videopush", restart=False, min=60, max=7200, step=30, unit="秒"),
    P("videopush.note_max_chars", "推送里带多少字笔记", "int", 1200,
      "笔记太长 QQ 会截断甚至拒收，这里先砍一刀。",
      note="只影响推送文本；完整笔记存在 data/notes/<BV号>.md，随时能翻。",
      group="videopush", restart=False, min=0, max=4000, step=100, unit="字"),
    P("videopush.bilinote_url", "BiliNote 地址", "str", "http://127.0.0.1:8483",
      "BiliNote 后端的地址。改端口的话这里也要跟着改。",
      note="用 /api/sys_health 探活；连不上时如果开了下面的自动拉起，会替你把后端拉起来。",
      group="videopush", restart=False, placeholder="http://127.0.0.1:8483"),
    P("videopush.bilinote_dir", "BiliNote backend 目录", "path",
      "D:/BiliNote/backend",
      "BiliNote 的 backend 目录（里面要有 main.py 和 .venv）。",
      note="注意 cwd 必须是这个目录：BiliNote 的笔记产物目录是相对 cwd 解析的，"
           "从别处启动会把 note_results 落到奇怪的地方。",
      group="videopush", restart=False),
    P("videopush.bilinote_python", "BiliNote 解释器（可选）", "str", "",
      "默认用 <backend>/.venv/Scripts/python.exe。",
      note="只有把虚拟环境放在别处时才需要填。",
      group="videopush", restart=False,
      placeholder="D:/BiliNote/backend/.venv/Scripts/python.exe"),
    P("videopush.bilinote_autostart", "BiliNote 没起就自动拉起", "bool", True,
      "生成笔记前发现后端没在跑，就无窗口地把它拉起来。",
      note="拉起来的是后台进程，日志写到 data/bilinote.log。"
           "关掉的话，BiliNote 没启动时这一轮笔记会直接失败（视频条目会留着，等下次补）。",
      group="videopush", restart=False),
    P("videopush.provider_id", "BiliNote 供应商", "str", "",
      "用哪个供应商生成笔记。留空 = 问 BiliNote 当前启用的那个。",
      note="BiliNote 的 LLM key 存在它自己的库里（providers 表），不在这里填。",
      group="videopush", restart=False, placeholder="deepseek"),
    P("videopush.model_name", "BiliNote 模型", "str", "",
      "用哪个模型生成笔记。留空 = 取该供应商的第一个模型。",
      note="当前 BiliNote 里配的是 deepseek-flash / deepseek-v4-pro。"
           "笔记质量要求高就用 pro，但贵一些。",
      group="videopush", restart=False, placeholder="deepseek-flash"),
    P("videopush.cookie", "B 站登录 Cookie（可选）", "password", "",
      "被 B 站风控（-412）时才需要。格式：SESSDATA=...; bili_jct=...",
      note="不填也能搜（会自动取 buvid 防 -412）。只有天天被拦时才去浏览器复制一份，"
           "注意这是登录凭证，别外传。填完重启才生效。",
      group="videopush", restart=True, advanced=True),
]

PARAMS_BY_PATH: Dict[str, Param] = {p.path: p for p in _ALL}
ALL_PARAMS: List[Param] = _ALL


def get(path: str) -> Optional[Param]:
    return PARAMS_BY_PATH.get(path)


def param_to_dict(p: Param) -> Dict[str, Any]:
    return {
        "path": p.path,
        "label": p.label,
        "type": p.type,
        "default": p.default,
        "desc": p.desc,
        "note": p.note,
        "restart": p.restart,
        "choices": p.choices,
        "min": p.min,
        "max": p.max,
        "step": p.step,
        "unit": p.unit,
        "placeholder": p.placeholder,
        "keys": p.keys,
        "keyLabels": p.key_labels,
        "pairLabels": p.pair_labels,
        "advanced": p.advanced,
        "hint": p.hint,
    }


def groups_with_params() -> List[Dict[str, Any]]:
    """给前端用的分组结构（含每组的参数）。顺序跟 GROUPS 一致。"""
    out: List[Dict[str, Any]] = []
    for g in GROUPS:
        items = [p for p in ALL_PARAMS if p.group == g.id]
        if not items:
            continue
        out.append({
            "id": g.id,
            "title": g.title,
            "desc": g.desc,
            "params": [param_to_dict(p) for p in items],
        })
    return out


def defaults() -> Dict[str, Any]:
    """把 default 摊成嵌套 dict（面板的重置为默认值用）。"""
    out: Dict[str, Any] = {}
    for p in ALL_PARAMS:
        cur = out
        parts = p.path.split(".")
        for seg in parts[:-1]:
            cur = cur.setdefault(seg, {})
        cur[parts[-1]] = p.default
    return out
