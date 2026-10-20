"""本地语音（GPT-SoVITS）：决定发不发、把文字洗成能念的话、合成、发出去。

三条纪律：
  1. **默认不发**。语音条比文字慢，也更容易被认出是合成的 —— 它是「显式打开」
     的能力，不是默认行为。
  2. **判断全在本地做**，一次模型都不多调（词表 + 标点，够用了）。
  3. **合成失败一定退回文字**。语音是锦上添花，不能让一条回复直接消失。

和 panel/voice.py 的关系：那边是「管理面」（试听、克隆、换音色），这边是
「运行面」（聊天时真的发出去）。参考音频的查找顺序两边必须一致 —— 都是
「文件名 → 情绪表 → default.wav」，否则面板里试听好的音色，聊天时却不是那个，
这种 bug 极难发现。改动任一边都要同步另一边的 tests。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from .config import TtsConfig

log = logging.getLogger("companion.tts")

_EMOTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")

# ────────────────────── 什么时候发 ──────────────────────

# 明确要求「来条语音」。宁可漏判也别误判 —— 把「语音合成怎么弄」这种技术讨论
# 当成指令去发语音，比不发更尴尬。所以都要求动词搭配，光出现「语音」不算。
# 真人怎么开口要语音，比「发条语音」这句话花样多得多：
#   发一下语音 / 发句语音 / 来段语音 / 语音条来一个 / 念一段 / 说句话给我听 / 整条语音
#   说给我听 / 念给我听 / 唱一段 / 语音告诉我 / 别打字了用说的 / ……，语音吧
# 所以这里不用「动词+固定量词+语音」这种死板组合，而是：
#   1. 动词和宾语之间允许塞任意短修饰（最多 8 个字）
#   2. 补上「说/念/读/唱 + 给我听」这类不带「语音」二字的说法
#   3. 句子末尾单独甩一句「语音」「说句话」也算（真人常这么补一句）
# 再统一排除「发语音失败了」「你昨天发语音了吗」这类陈述句。
_VOICE_NOUN = r"(?:语音|语音条|语音消息|音频|声音|录音)"

_REQ_PATTERNS = [
    # ── 出现「语音/声音」这类名词 ──────────────────────────
    # 动词在前：发一下语音 / 来段语音 / 整条语音 / 录句声音 / 用语音 / 给我来一段
    # 「发」要排除「发现语音」「发言语音」这类词；「来」要排除「后来/将来/原来」
    r"(?:(?<![后将原本起出去回进])来|发(?!现|言|挥|布|展|送|出|生|火|育|泄|行|起)"
    r"|整|录|放|播|要|给|用|换|开)"
    rf"[^，。！？\n]{{0,8}}{_VOICE_NOUN}",
    # 宾语在前：语音条来一个 / 语音回复我 / 语音告诉我 / 语音念一遍
    rf"{_VOICE_NOUN}(?:消息)?"
    r"[^，。！？\n]{0,6}(来|发|回|给|说|讲|整|录|听|告诉|念|读|唱|一个|一条)",
    # ── 不带「语音」二字，但意思一样 ──────────────────────
    # 说给我听 / 念给我听 / 读给我听 / 唱给我听
    r"(说|念|读|唱|讲)(给|让)(我|俺)(听|听听)",
    # 说句话给我听 / 念一段来听 / 唱首给我听（量词 + 给我/听）
    r"(说|讲|念|读|唱)(一|两|几)?[句话段遍首][^，。！？\n]{0,6}(听|给我)",
    # 念一下 / 念一段 / 读一句 / 背一遍 / 唱一首
    r"(念|读|背|唱|播)(一|两|几)?[句段条遍篇下首]",
    # 给我来一句 / 帮我说段 / 替我唱首
    r"(给我|帮我|替我)(来|说|念|读|唱)(一)?[句段条首]?",
    # 摆明了嫌打字：别打字了 / 打字太累 / 打字说不清 / 用说的
    r"(别|不要|不想|懒得)(再)?打字|打字(太|好)?(累|麻烦|说不清|不方便)|用说的",
    # 指定嗓音：用萝莉的声音说 / 换个音色念
    r"(用|换)[^，。！？\n]{0,8}(声音|音色|嗓子)[^，。！？\n]{0,6}(说|讲|念|读|回|唱)",
    # 想听你说话 / 听听你的声音
    r"(想|要)听(你|您)?[^，。！？\n]{0,6}(说话|声音|念|说|唱)",
    r"听听(你|您)(说话|的声音|唱)",
    # 光杆一句「来一句」「来段」
    r"^来(一)?[句段条首]$",
    # ── 句尾补一句指令：……，语音吧 / ……，发语音 / ……，说句话 ──
    # 必须以标点或空格隔开才算，否则「我超喜欢你的声音」会被误判。
    r"(?:^|[,，。.！!？?；;\s]+)"
    rf"(?:(?:用|发|来|整|录|换成?)?{_VOICE_NOUN}|(?<![我让])(?:说|念|读|唱)(?:一|两|几)?(?:句话|句|话|段|首))"
    r"[吧呗啊呀哦嘛哈~]*$",
    # 英文
    r"\bvoice\s*(me|reply|message|one)?\b",
    r"\bsend\s+(me\s+)?(a\s+)?voice\b",
]

# 「发语音失败」「语音发不出去」「你昨天发语音了吗」—— 这些是在聊语音，
# 不是在要语音。命中就直接判定为「没要求」。
_NOT_A_REQUEST_RE = re.compile(
    r"(语音|音频|声音|语音条|录音)"
    r"[^，。！？\n]{0,6}(失败|不了|不出|不行|打不开|报错|坏了|没成功|了|吗|过)"
)

# 「我发了语音」「你昨天开过语音」—— 已经发生的事，不是在要。「发个语音」不在此列。
_ALREADY_DONE_RE = re.compile(
    r"(发|用|开|录|听)(了|过)[^，。！？\n]{0,4}(语音|音频|声音|语音条|录音)"
)

# 明确拒绝 —— 优先级高于上面（「别发语音」里也含「发语音」）
_NO_PATTERNS = [
    r"(别|不要|不用|不许|不准|停止|少)(再)?(发|用|说|整)?(语音|音频)",
    r"打字(就|还是)?(行|好|可以|吧)",
    r"(别|不要)(念|说话了?给我听)",
]

_REQ_RE = [re.compile(p, re.I) for p in _REQ_PATTERNS]
_NO_RE = [re.compile(p, re.I) for p in _NO_PATTERNS]


def speech_refused(user_text: str) -> bool:
    """用户明确说了「别发语音 / 打字就行」。"""
    return any(p.search(user_text or "") for p in _NO_RE)


def speech_requested(user_text: str) -> bool:
    """用户明确要一条语音。"""
    t = user_text or ""
    if speech_refused(t):
        return False
    if _NOT_A_REQUEST_RE.search(t):
        return False
    if _ALREADY_DONE_RE.search(t):
        return False
    return any(p.search(t) for p in _REQ_RE)


# 模型自己说「我不会发语音 / 我这边没声 / 只能打字」。
#
# 真踩过：用户说「发个语音给我问个好」，模型回「老大我不会发语音，张嘴也发不出声啊」，
# 而这一层照样把这句话合成成语音条发了出去 —— 收到的是「一条语音告诉你我不会发语音」。
# 原因是提示词里从没告诉过模型它能出声（见 context.VOICE_RULES），模型只能按
# 「我是个打字的助手」去答。提示词已经补上了，这里再留一道闸：
# **带自我否认的回复一律不转语音**，哪怕合成成功听着也荒谬。
_DENY_VOICE_RE = re.compile(
    r"(不会|不能|没法|发不|说不了|缺少?|没有|没)"
    r"[^，。！？\n]{0,4}(发)?(语音|声音|声|嘴|麦克风|麦|嗓子)"
    r"|(说不了|开不了|张不开|不能|没法)[^，。！？\n]{0,2}说话"
    r"|只能打字|打字(就)?行(吧)?|打字算了|听不着|听不见我"
)


def reply_denies_voice(reply: str) -> bool:
    """这条回复是不是在说「我发不了语音」。"""
    return bool(_DENY_VOICE_RE.search(reply or ""))


def should_speak(cfg: TtsConfig, user_text: str, reply: str,
                 *, rng=None) -> bool:
    """这条回复该不该用语音发。纯本地判断，零 token。"""
    if not cfg.enabled:
        return False
    if speech_refused(user_text):
        return False

    tts_text = speech_text(reply)
    if not tts_text:
        return False  # 整条都是表情/占位符，没东西可念
    if reply_denies_voice(tts_text):
        return False  # 「我不会发语音」不该被念成语音条

    # 全语音模式（面板上那个按钮）：每条回复都发。
    # 放在这里而不是最前面 —— 「别发语音」「我不会发语音」「这条没东西可念」
    # 这三条否决权比按钮大。按钮只是把「概率」和「字数」这两关免掉。
    if getattr(cfg, "always_voice", False):
        return True

    # 被明确要求：放宽字数上限（要求了就别只念半句），但仍受硬上限约束
    if speech_requested(user_text):
        return len(tts_text) <= max(cfg.max_chars * 2, 40)

    if (cfg.trigger or "request").lower() != "auto":
        return False

    # 主动发：太长的念不完，太短的没必要（「嗯」发语音反而刻意）
    if len(tts_text) > cfg.max_chars:
        return False
    if len(tts_text) < 2:
        return False
    return (rng or random).random() < max(0.0, min(1.0, float(cfg.chance or 0.0)))


# ────────────────────── 把文字洗成能念的话 ──────────────────────

# [图片] [表情] 这类占位符念出来毫无意义
_PLACEHOLDER_RE = re.compile(r"[\[【](图片|表情|语音|视频|文件|红包|动画表情|戳一戳|位置|链接)[\]】]")
# @123456 里的号码念出来很怪
_AT_RE = re.compile(r"@\d{5,}")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
# emoji / 符号区（含变体选择符、零宽连接符）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\uFE0F\u200D\u20E3]"
)
# 颜文字：括号里全是符号
_KAOMOJI_RE = re.compile(r"[（(][^\u4e00-\u9fa5A-Za-z0-9]{1,12}[）)]")
# 动作描写：（笑）（叹气）（小声说）—— 念出来像在念剧本。
# 只认常见的那几个词，不用「括号里是中文就删」那种粗规则 —— 那会把
# 「（明天）见」里的正经内容也吃掉。
_ACTION_WORDS = (
    "笑|大笑|偷笑|坏笑|干笑|笑死|叹气|叹了口气|哭|抽泣|哽咽|小声|大声|低声"
    "|嘟囔|嘀咕|无奈|认真|沉默|想了想|顿了顿|点头|摇头|耸肩|捂脸|害羞|尴尬"
    "|得意|生气|咳嗽|咳|哈欠|伸懒腰|眨眼|歪头|撇嘴|翻白眼|顿了顿"
)
_ACTION_RE = re.compile(rf"[（(]\s*(?:{_ACTION_WORDS})[^）)]{{0,6}}[）)]")


def speech_text(reply: str, *, max_chars: int = 0) -> str:
    """把聊天文本洗成适合 TTS 念的句子。纯本地。"""
    t = (reply or "").strip()
    if not t:
        return ""

    t = _PLACEHOLDER_RE.sub("", t)
    t = _AT_RE.sub("", t)
    t = _URL_RE.sub("", t)
    t = _KAOMOJI_RE.sub("", t)
    t = _ACTION_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)

    # 换行在 TTS 里会被当成奇怪的停顿，统一变成逗号
    t = re.sub(r"\s*\n+\s*", "，", t)
    # 连续标点收敛：模型偶尔会吐「！！！！」「。。。」
    t = re.sub(r"([!！?？。，、~～])\1+", r"\1", t)
    # 引号书名号可以念，但成对的「」在语音里会突兀，直接去掉外壳
    t = t.replace("「", "").replace("」", "")
    t = re.sub(r"[ \t]+", " ", t)
    # 开头结尾残留的标点
    t = t.strip(" ，、,.;；:：")

    if max_chars and len(t) > max_chars:
        t = _cut_at_sentence(t, max_chars)
    return t.strip()


def _cut_at_sentence(text: str, limit: int) -> str:
    """截到 limit 字以内，尽量断在句末，别把词切一半。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    # 从后往前找最后一个句末标点（至少留 1/3，不然会切得太碎）
    floor = max(1, limit // 3)
    for i in range(len(head) - 1, floor - 1, -1):
        if head[i] in "。！？!?，、…":
            return head[: i + 1] if head[i] != "，" else head[:i]
    return head


# ────────────────────── 情绪推断 ──────────────────────

_EMOTION_RULES: list = [
    ("happy", re.compile(r"[哈哈嘿嘻]{2,}|笑死|好笑|逗死|开心|高兴")),
    ("sad", re.compile(r"呜呜|难过|伤心|想哭|委屈|难受|叹气")),
    ("excited", re.compile(r"卧槽|绝了|太棒|牛[啊逼]|天哪|天啊|[!！]{2,}")),
    ("angry", re.compile(r"生气|烦死|讨厌|别闹|滚[啊吧]")),
]


def guess_emotion(text: str, available: Optional[set] = None) -> str:
    """从文本里猜一个情绪名。猜不到返回 ""（调用方回退到默认情绪）。

    available 传 refs 里真实存在的情绪名 —— 猜出一个没录过的情绪没有意义，
    只会让 resolve 白跑一次再回退。这里先筛掉。
    """
    t = text or ""
    for name, pat in _EMOTION_RULES:
        if pat.search(t):
            return name if (available is None or name in available) else ""
    return ""


# ────────────────────── 参考音频 ──────────────────────

def emotions_path(refs: Path) -> Path:
    return refs / "emotions.json"


def load_emotions(refs: Path) -> dict:
    """{情绪名: 文件名}。文件坏了就当空的 —— 不因为一个 json 让语音功能挂掉。"""
    p = emotions_path(refs)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if _EMOTION_RE.match(str(k))}
    except (OSError, ValueError):
        pass
    return {}


def resolve_ref(refs: Path, emotion: str = "",
                filename: str = "") -> Tuple[Path, str]:
    """按「文件名 → 情绪表 → 情绪同名文件 → default.wav」找参考音频。

    顺序必须和 panel/voice.py 的 resolve_ref 完全一致，否则面板试听和聊天
    实际用的是两条不同的音频。返回 (wav, 参考文本)。
    """
    if filename:
        p = refs / Path(filename).name
        if not p.is_file():
            raise FileNotFoundError(f"参考音频不存在：{filename}")
    elif emotion:
        name = load_emotions(refs).get(emotion)
        p = refs / name if name else refs / f"{emotion}.wav"
        if not p.is_file():
            # 情绪没录就退回默认音色 —— 聊天场景更需要「有声音」，而不是报错
            p = refs / "default.wav"
            if not p.is_file():
                raise FileNotFoundError(f"没有 {emotion} 的参考音频，也没有 default.wav")
    else:
        p = refs / "default.wav"
        if not p.is_file():
            raise FileNotFoundError("refs 里没有 default.wav")

    txt = p.with_suffix(".txt")
    prompt = txt.read_text(encoding="utf-8").strip() if txt.is_file() else ""
    return p, prompt


# ────────────────────── 合成 ──────────────────────

def service_alive(api_base: str, timeout: float = 0.8) -> bool:
    """api_v2 的端口通不通。只做 TCP 握手，不发任何请求。

    为什么需要这一下：服务没开（或刚崩）时，urllib 的 connect 一直等到
    `tts.timeout`（默认 60 秒）才报错，而这 60 秒是**握着会话锁**在等的 ——
    用户看到的现象就是「机器人在群里卡住了，后来一句话都不回」。
    先花 0.8 秒探一下，连不上就立刻降级发文字。

    注意不能用 urllib 探：国内环境常开着 HTTP_PROXY，127.0.0.1 也会被丢给代理。
    socket 直连不受代理影响。
    """
    try:
        u = urllib.parse.urlsplit(api_base or "")
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _request(url: str, payload: dict, timeout: float) -> Tuple[int, bytes]:
    """POST JSON，返回 (状态码, 响应体)。0 表示连不上。

    必须绕代理：不少国内环境开着 HTTP_PROXY，urllib 会把
    127.0.0.1 的请求也丢给代理，结果是 502 —— 服务明明活着却调不通。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # 连接失败 / 超时
        return 0, str(e).encode("utf-8", "ignore")


class VoiceClient:
    """一次聊天回复的语音合成。失败一律抛异常，由调用方降级成文字。"""

    def __init__(self, cfg: TtsConfig):
        self.cfg = cfg
        self.refs = Path(cfg.install_dir) / "refs"

    def synth(self, text: str, emotion: str = "") -> bytes:
        """合成一句，返回 wav 字节。"""
        text = speech_text(text, max_chars=self.cfg.max_chars)
        if not text:
            raise ValueError("没有可念的内容")

        emotion = (emotion or self.cfg.default_emotion or "").strip()
        ref, prompt = resolve_ref(self.refs, emotion)

        payload = {
            "text": text,
            "text_lang": "zh",
            "ref_audio_path": str(ref),
            "prompt_text": prompt,
            "prompt_lang": "zh",
            "text_split_method": "cut5",
            "speed_factor": float(self.cfg.speed or 1.0),
            "media_type": "wav",
            "streaming_mode": False,
        }
        code, raw = _request(self.cfg.api_base.rstrip("/") + "/tts", payload,
                             float(self.cfg.timeout or 60.0))
        if code == 0:
            raise RuntimeError(f"连不上语音服务（{self.cfg.api_base}）："
                               + raw.decode("utf-8", "ignore")[:120])
        if code != 200 or raw[:4] != b"RIFF":
            detail = raw.decode("utf-8", "ignore")[:200]
            raise RuntimeError(f"合成失败（HTTP {code}）：{detail}")

        log.info("语音合成完成：%d 字 → %d KB，情绪 %s",
                 len(text), len(raw) // 1024, emotion or "默认")
        return raw

    async def synth_async(self, text: str, emotion: str = "") -> bytes:
        """合成是阻塞 IO（2~3 秒），必须扔线程池，不然整个 bot 卡住。"""
        return await asyncio.to_thread(self.synth, text, emotion)


def find_ffmpeg(install_dir: str) -> Optional[str]:
    """找 ffmpeg：优先用 GPT-SoVITS 目录里的那份，其次 PATH。"""
    for cand in (Path(install_dir) / "bin" / "ffmpeg.exe",
                 Path(install_dir) / "bin" / "ffmpeg",
                 Path(install_dir) / "runtime" / "ffmpeg.exe"):
        if cand.is_file():
            return str(cand)
    return shutil.which("ffmpeg")


def to_qq_audio(wav: bytes, ffmpeg: Optional[str],
                *, bitrate: str = "48k") -> Tuple[bytes, str]:
    """wav → mp3（QQ 语音更认这个，体积也小十几倍）。

    转不了就原样返回 wav —— 宁可发个大的，也别因为转码失败整条语音没了。
    """
    if not ffmpeg:
        return wav, "wav"

    tmpdir = tempfile.mkdtemp(prefix="qqvoice_")
    src = Path(tmpdir) / "in.wav"
    dst = Path(tmpdir) / "out.mp3"
    try:
        src.write_bytes(wav)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
               "-ar", "24000", "-ac", "1", "-b:a", bitrate, "-f", "mp3", str(dst)]
        kwargs = {}
        if os.name == "nt":
            # 不弹黑框：bot 是后台进程，一闪一闪的窗口很吓人
            kwargs["creationflags"] = 0x08000000
        subprocess.run(cmd, check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kwargs)
        mp3 = dst.read_bytes()
        if mp3:
            return mp3, "mp3"
    except Exception as e:
        log.warning("转 mp3 失败（%s），改发 wav", e)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return wav, "wav"


def record_segment(audio: bytes) -> list:
    """OneBot 11 的语音消息段。base64:// 是各家实现（NapCat/LLOneBot）都认的写法。"""
    return [{"type": "record",
             "data": {"file": "base64://" + base64.b64encode(audio).decode("ascii")}}]


async def speak(cfg: TtsConfig, adapter, msg, reply: str,
                *, client: Optional[VoiceClient] = None,
                emotion: str = "") -> Optional[int]:
    """合成并发送一条语音。成功返回音频字节数，失败抛异常（调用方降级成文字）。"""
    client = client or VoiceClient(cfg)
    wav = await client.synth_async(reply, emotion)

    audio, ext = to_qq_audio(wav, find_ffmpeg(cfg.install_dir))
    await adapter.send_record(msg, audio, ext=ext)
    return len(audio)
