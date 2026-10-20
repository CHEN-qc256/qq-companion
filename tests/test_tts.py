"""语音条测试（不联网、不连 QQ、不花一分钱）。

七组：
  A 触发判断    —— 什么时候发、什么时候闭嘴（词表 + 开关 + 概率）
  B 文本清洗    —— 表情、占位符、动作描写、URL、换行、超长截断
  C 情绪推断    —— 本地规则，且只认 refs 里真有的情绪
  D 参考音频    —— 三级回退必须和 panel/voice.py 一致
  E 发送        —— OneBot record 段的形状、群/私聊分别走哪个接口、retcode 处理
  F 合成        —— 真起一个假 HTTP 服务，校验请求体；失败要抛错不能静默
  G 集成        —— Orchestrator._try_voice / _send_humanized 的三条分支

跑法：python tests/test_tts.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import TtsConfig  # noqa: E402
from app.onebot import IncomingMessage, OneBotAdapter  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.tts import (  # noqa: E402
    VoiceClient,
    guess_emotion,
    load_emotions,
    record_segment,
    reply_denies_voice,
    resolve_ref,
    service_alive,
    should_speak,
    speech_refused,
    speech_requested,
    speech_text,
    to_qq_audio,
)

FAILED = []
PASSED = 0


def check(name: str, cond, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  —— {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


TMP = ROOT / "data" / "_test_tts"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
REFS = TMP / "refs"
REFS.mkdir(parents=True, exist_ok=True)

# 一份"够用"的假 wav：resolve_ref 只查存在性，合成走下面那个假服务
FAKE_WAV = b"RIFF" + b"\x00" * 64
(REFS / "default.wav").write_bytes(FAKE_WAV)
(REFS / "default.txt").write_text("这是默认参考音频的原文", encoding="utf-8")
(REFS / "happy.wav").write_bytes(FAKE_WAV)
(REFS / "happy.txt").write_text("今天心情不错", encoding="utf-8")


def cfg_for(**kw) -> TtsConfig:
    base = dict(enabled=True, install_dir=str(TMP), api_base="http://127.0.0.1:9",
                max_chars=120, timeout=3.0, speed=1.0, default_emotion="neutral")
    base.update(kw)
    return TtsConfig(**base)


# ══════════════════════ A 触发判断 ══════════════════════
section("A 触发判断")

check("「发条语音」算要求", speech_requested("发条语音给我听听"))
check("「来一段语音」算要求", speech_requested("来一段语音"))
check("「说句话给我听」算要求", speech_requested("说句话给我听嘛"))
check("「用语音回复」算要求", speech_requested("用语音回复我"))
check("「念一下」算要求", speech_requested("念一下这句"))
check("技术讨论不算要求", not speech_requested("语音合成是怎么做的"))
check("单纯提到语音不算要求", not speech_requested("语音条挺好玩的"))
check("「别发语音」被识别为拒绝", speech_refused("别发语音了"))
check("「打字就行」被识别为拒绝", speech_refused("打字就行"))
check("拒绝优先于要求", not speech_requested("别发语音，打字就行"))

# 线上真实踩过的漏判：量词不是只有「条/段/个」一种说法。
# 这两句是用户实际发的，旧词表全都没认出来，导致明明要语音却只回文字。
check("「发句语音」算要求（线上原句）",
      speech_requested("给我发句语音"))
check("「发一下语音」算要求（线上原句）",
      speech_requested("我已经配置好参数了啊，给我发一下语音"))
check("「念一段」算要求", speech_requested("念一段来听听"))
check("「语音条来一个」算要求", speech_requested("语音条来一个"))
check("「再说一遍给我听」算要求", speech_requested("再说一遍给我听"))
check("「给我来一句」算要求", speech_requested("给我来一句"))

# 反向：聊语音、抱怨语音不好用，都不该被当成「要语音」
check("「发语音失败了」不算要求", not speech_requested("我发语音失败了"))
check("「语音发不出去」不算要求", not speech_requested("语音发不出去"))
check("「发语音了吗」不算要求", not speech_requested("你昨天发语音了吗"))
check("「发现语音不好用」不算要求", not speech_requested("后来我发现语音不好用"))
check("「将来这个语音会更好」不算要求", not speech_requested("将来这个语音会更好"))

# 最自然的一类：正文说完了，末尾补一句指令。
# 用户原话就是这种 ——「一句话末尾让她发语音」，前面聊什么不影响。
check("句尾「，发语音」算要求", speech_requested("今天天气怎么样，发语音"))
check("句尾「，用语音」算要求", speech_requested("讲个笑话吧，用语音"))
check("句尾「，语音吧」算要求", speech_requested("今天好热啊，语音吧"))
check("句尾「语音条来一个」算要求", speech_requested("说说你今天干啥了，语音条来一个"))
check("句尾「说句话吧」算要求", speech_requested("你在吗，说句话吧"))
check("句尾「说两句」算要求", speech_requested("在干嘛呢，说两句"))
check("「说给我听」算要求", speech_requested("说给我听"))
check("「念给我听」算要求", speech_requested("念给我听"))
check("「唱一段」算要求", speech_requested("唱一段"))
check("「语音告诉我」算要求", speech_requested("语音告诉我"))
check("「别打字了」算要求", speech_requested("我不看屏幕了，别打字了"))

# 反向：夸声音、聊功能、说自己的动作，都别误判
check("夸声音不算要求", not speech_requested("我超喜欢你的声音"))
check("「我发了语音」不算要求", not speech_requested("我发了语音你没听"))
check("「让我说两句」不算要求", not speech_requested("让我说两句"))
check("「我说一句话」不算要求", not speech_requested("我说一句话你别生气"))
check("「声音有点小」不算要求", not speech_requested("声音有点小"))

c_off = cfg_for(enabled=False)
check("开关关着永远不发", not should_speak(c_off, "发条语音", "好啊"))

c_req = cfg_for(trigger="request")
check("request 模式：被要求才发", should_speak(c_req, "发条语音", "好呀我念给你听"))
check("request 模式：没被要求不发", not should_speak(c_req, "今天吃啥", "吃面吧"))
check("request 模式：被拒绝不发", not should_speak(c_req, "别发语音", "好"))

c_auto = cfg_for(trigger="auto", chance=1.0)
check("auto + 概率 1：没被要求也发", should_speak(c_auto, "今天吃啥", "吃面吧"))
c_auto0 = cfg_for(trigger="auto", chance=0.0)
check("auto + 概率 0：不发", not should_speak(c_auto0, "今天吃啥", "吃面吧"))
# 概率只该管「主动开口」。用户点名要的语音必须发，不能靠骰子。
check("auto + 概率 0：被点名仍然发",
      should_speak(c_auto0, "给我发句语音", "好呀，这就说"))
check("auto + 概率 0：被点名仍然发（发一下语音）",
      should_speak(c_auto0, "我已经配置好参数了啊，给我发一下语音", "好呀，这就说"))

class _Rng:
    def __init__(self, v: float):
        self.v = v

    def random(self) -> float:
        return self.v

c_half = cfg_for(trigger="auto", chance=0.5)
check("概率 0.5：掷 0.3 命中", should_speak(c_half, "嗯", "随便说说", rng=_Rng(0.3)))
check("概率 0.5：掷 0.7 不中", not should_speak(c_half, "嗯", "随便说说", rng=_Rng(0.7)))

# ── 模型自己说「我不会发语音」──
# 线上事故：用户「发个语音给我问个好」，模型回「我不会发语音，张嘴也发不出声」，
# 而这句被合成成了语音条 —— 收到一条语音，内容是「我不会发语音」。荒谬且暴露。
check("「我不会发语音」被识别", reply_denies_voice("老大我不会发语音，张嘴也发不出声啊"))
check("「只是没声那种」被识别", reply_denies_voice("在的老大，只是没声那种"))
check("「真说不了语音啦」被识别", reply_denies_voice("真说不了语音啦"))
check("「我没有麦克风」被识别", reply_denies_voice("我没有麦克风啊"))
check("「只能打字」被识别", reply_denies_voice("我只能打字陪你"))
check("正常回复不被误判", not reply_denies_voice("好啊，早啊老大，今儿起得挺早"))
check("吐槽打字不算否认", not reply_denies_voice("打字好，我都怀疑每条语音得花你半条命"))
check("聊语音功能不算否认", not reply_denies_voice("找不到就过，语音这东西又不是必须听"))
check("否认的回复不转语音（被要求也不转）",
      not should_speak(c_req, "发个语音给我问个好", "老大我不会发语音，张嘴也发不出声啊"))

long_reply = "这句话" * 100
check("auto 模式：超长回复不发语音", not should_speak(c_auto, "说说", long_reply))
check("被要求时：超长放宽到 2 倍上限",
      should_speak(cfg_for(trigger="request", max_chars=20), "发条语音", "这句话" * 12))
check("被要求时：超级长仍然拒绝",
      not should_speak(cfg_for(trigger="request", max_chars=20), "发条语音", "这句话" * 40))
check("整条都是表情时不发", not should_speak(c_req, "发条语音", "😂😂😂"))
check("单字不发（auto 模式）", not should_speak(c_auto, "嗯", "嗯"))

# ── 全语音模式（面板上那个按钮）──
# 它是一个**覆盖开关**：开了每条都发，关掉立刻回到原来的 trigger/chance。
# 关键是不能覆盖「别发语音」和「我不会发语音」这两条否决权。
c_all = cfg_for(always_voice=True, trigger="request", chance=0.0)
check("全语音：没被要求、概率 0 也发", should_speak(c_all, "今天吃啥", "吃面吧"))
check("全语音：超长也发", should_speak(c_all, "说说", long_reply))
check("全语音：单字也发", should_speak(c_all, "嗯", "嗯"))
check("全语音：仍然尊重「别发语音」", not should_speak(c_all, "别发语音", "好"))
check("全语音：仍然不念「我不会发语音」",
      not should_speak(c_all, "嗯", "老大我不会发语音"))
check("全语音：总开关关着时依然不发",
      not should_speak(cfg_for(always_voice=True, enabled=False), "今天吃啥", "吃面吧"))
check("全语音：没东西可念（纯表情）也不发",
      not should_speak(c_all, "哈", "[表情]"))
# 关掉就是关掉 —— 不能留下任何后遗症（用户的原始设置还在）
c_off_all = cfg_for(always_voice=False, trigger="request", chance=0.0)
check("关掉全语音后回到 request 模式", not should_speak(c_off_all, "今天吃啥", "吃面吧"))
check("关掉全语音后被要求仍然发",
      should_speak(c_off_all, "发条语音", "好呀我念给你听"))# ══════════════════════ B 文本清洗 ══════════════════════
section("B 文本清洗")

check("去掉 [图片] 占位符", speech_text("看看这个 [图片] 好看吧") == "看看这个 好看吧")
check("去掉 [表情]", "[表情]" not in speech_text("哈哈[表情]真逗"))
check("去掉 emoji", speech_text("好耶🎉🎉") == "好耶")
check("去掉颜文字", speech_text("好开心(≧▽≦)") == "好开心")
check("去掉动作描写", speech_text("好呀（笑）这就去") == "好呀这就去")
check("保留正常括号里的字", "明天" in speech_text("（明天）见"))
check("去掉 @号码", speech_text("@1234567 你看").strip() == "你看")
check("去掉 URL", speech_text("看这个 https://example.com/a?b=1 很好笑") == "看这个 很好笑")
check("换行变逗号", speech_text("第一句\n第二句") == "第一句，第二句")
check("连续感叹号收敛", speech_text("真的吗！！！") == "真的吗！")
check("连续句号收敛", speech_text("好。。。") == "好。")
check("去掉书名号外壳", speech_text("「你好」") == "你好")
check("空输入返回空", speech_text("") == "")
check("纯表情返回空", speech_text("😂😂😂") == "")
check("开头标点被清掉", not speech_text("，你好").startswith("，"))

cut = speech_text("第一句话。第二句话。第三句话。", max_chars=8)
check("超长在句末截断", cut.endswith("。"), f"实际：{cut!r}")
check("截断后不超长", len(cut) <= 8, f"实际 {len(cut)} 字：{cut!r}")
check("短文本不截断", speech_text("你好", max_chars=10) == "你好")


# ══════════════════════ C 情绪推断 ══════════════════════
section("C 情绪推断")

check("哈哈哈 → happy", guess_emotion("笑死我了哈哈哈哈哈") == "happy")
check("呜呜 → sad", guess_emotion("呜呜我好难过") == "sad")
check("卧槽 → excited", guess_emotion("卧槽这也太强了") == "excited")
check("生气 → angry", guess_emotion("烦死我了") == "angry")
check("平淡 → 空（回退默认）", guess_emotion("今天星期三") == "")
check("情绪不在 refs 里就不猜",
      guess_emotion("哈哈哈哈", available={"neutral", "sad"}) == "")
check("情绪在 refs 里正常返回",
      guess_emotion("哈哈哈哈", available={"happy"}) == "happy")


# ══════════════════════ D 参考音频 ══════════════════════
section("D 参考音频")

p, txt = resolve_ref(REFS, "neutral")
check("没配情绪时回退 default.wav", p.name == "default.wav")
check("参考文本跟着 wav 一起取", txt == "这是默认参考音频的原文")

p2, txt2 = resolve_ref(REFS, "happy")
check("情绪同名文件优先", p2.name == "happy.wav" and txt2 == "今天心情不错")

p3, _ = resolve_ref(REFS, "", filename="happy.wav")
check("指定文件名时直接用", p3.name == "happy.wav")

try:
    resolve_ref(REFS, "sad")  # 没有 sad.wav，但有 default.wav
    check("情绪缺失回退 default", True)
except Exception as e:
    check("情绪缺失回退 default", False, str(e))

try:
    resolve_ref(REFS, "", filename="../../evil.wav")
    check("路径穿越被拦住", False, "居然没报错")
except Exception:
    check("路径穿越被拦住", True)

(REFS / "emotions.json").write_text(
    json.dumps({"gentle": "happy.wav"}, ensure_ascii=False), encoding="utf-8")
em = load_emotions(REFS)
check("情绪表读取正确", em.get("gentle") == "happy.wav")
p4, _ = resolve_ref(REFS, "gentle")
check("情绪表映射生效", p4.name == "happy.wav")

(REFS / "emotions.json").write_text("{坏掉的 json", encoding="utf-8")
check("情绪表坏掉不抛异常", load_emotions(REFS) == {})
(REFS / "emotions.json").unlink()


# ══════════════════════ E 发送 ══════════════════════
section("E 发送")

seg = record_segment(b"RIFFfake")
check("段类型是 record", seg[0]["type"] == "record")
check("file 走 base64:// 前缀", seg[0]["data"]["file"].startswith("base64://"))
check("base64 能还原原始字节",
      base64.b64decode(seg[0]["data"]["file"][9:]) == b"RIFFfake")

PRIVATE = IncomingMessage(message_type="private", user_id=1001, group_id=None,
                          self_id=1, text="发条语音")
# 同一会话、但没提语音的普通消息（判断时看的是 msg.text，必须分开造）
PRIVATE_PLAIN = IncomingMessage(message_type="private", user_id=1001, group_id=None,
                                self_id=1, text="今天吃啥")
GROUP = IncomingMessage(message_type="group", user_id=1001, group_id=2002,
                        self_id=1, text="发条语音")

adapter = OneBotAdapter(cfg=SimpleNamespace(onebot=SimpleNamespace(
    mode="reverse", host="127.0.0.1", port=0, path="/", url="")),
    on_message=lambda m: None)
calls = []


async def _fake_call(action, params, timeout=20.0):
    calls.append((action, params))
    return {"retcode": 0, "status": "ok"}


adapter.call_api = _fake_call  # type: ignore[assignment]

asyncio.run(adapter.send_record(PRIVATE, b"RIFFprivate"))
check("私聊走 send_private_msg", calls[-1][0] == "send_private_msg")
check("私聊带 user_id", calls[-1][1].get("user_id") == 1001)
check("message 是数组（段格式）", isinstance(calls[-1][1].get("message"), list))

asyncio.run(adapter.send_record(GROUP, b"RIFFgroup"))
check("群聊走 send_group_msg", calls[-1][0] == "send_group_msg")
check("群聊带 group_id", calls[-1][1].get("group_id") == 2002)


async def _reject(action, params, timeout=20.0):
    return {"retcode": 1200, "message": "格式不支持"}


adapter.call_api = _reject  # type: ignore[assignment]
try:
    asyncio.run(adapter.send_record(PRIVATE, b"RIFFx"))
    check("retcode 非 0 要抛异常", False, "居然当成功了")
except RuntimeError as e:
    check("retcode 非 0 要抛异常", "1200" in str(e))

adapter.call_api = _fake_call  # type: ignore[assignment]
try:
    asyncio.run(adapter.send_record(PRIVATE, b""))
    check("空音频要拒绝", False)
except RuntimeError:
    check("空音频要拒绝", True)


# ══════════════════════ F 合成 ══════════════════════
section("F 合成")

seen_payloads = []


class _FakeTTS(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            seen_payloads.append(json.loads(body.decode("utf-8")))
        except Exception:
            seen_payloads.append({})
        if self.path.startswith("/tts"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.end_headers()
            self.wfile.write(FAKE_WAV * 4)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTTS)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.2)

c_ok = cfg_for(api_base=f"http://127.0.0.1:{port}", speed=1.1)
client = VoiceClient(c_ok)
wav = asyncio.run(client.synth_async("你好呀", "happy"))
check("合成返回字节", isinstance(wav, bytes) and wav[:4] == b"RIFF")
check("返回的是服务端那份音频", len(wav) == len(FAKE_WAV) * 4)

p = seen_payloads[-1]
check("请求带 text", p.get("text") == "你好呀")
check("请求带参考音频路径", str(p.get("ref_audio_path", "")).endswith("happy.wav"))
check("请求带参考文本", p.get("prompt_text") == "今天心情不错")
check("语速用 speed_factor", abs(float(p.get("speed_factor", 0)) - 1.1) < 1e-6)
check("media_type 是 wav", p.get("media_type") == "wav")
check("text_lang 是 zh", p.get("text_lang") == "zh")

b4 = len(seen_payloads)
try:
    VoiceClient(cfg_for(api_base=f"http://127.0.0.1:{port}")).synth("😂😂😂")
    check("没内容可念要抛错", False)
except ValueError:
    check("没内容可念要抛错", True)
check("没内容时不该发请求", len(seen_payloads) == b4)

srv.shutdown()
srv.server_close()

dead = cfg_for(api_base="http://127.0.0.1:9", timeout=1.0)
try:
    VoiceClient(dead).synth("你好")
    check("服务没开要抛错", False, "居然成功了")
except RuntimeError as e:
    check("服务没开要抛错", "连不上" in str(e), str(e))


# ══════════════════════ G 集成 ══════════════════════
section("G 编排集成")

sent_records = []
sent_texts = []


class _FakeAdapter:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def send_record(self, msg, audio, ext="mp3", timeout=60.0):
        if self.fail:
            raise RuntimeError("平台拒了")
        sent_records.append((msg.session_id, len(audio), ext))

    async def send_text(self, msg, text):
        sent_texts.append((msg.session_id, text))


def fake_orch(tts_cfg, adapter):
    """只用 _try_voice / _send_humanized 需要的两个属性 + 人类化配置。

    这里不构造真的 Orchestrator：那会拖进 store/llm/ctx/budget/life/memory
    一整套。要测的只是"发送前那道判断"，把两个方法绑到轻量对象上就够了。
    """
    cfg = SimpleNamespace(
        tts=tts_cfg,
        group=SimpleNamespace(max_segments=1),
        humanize=SimpleNamespace(
            read_delay_min=0.0, read_delay_max=0.0,
            typing_per_char_min=0.0, typing_per_char_max=0.0,
            typing_min=0.0, typing_max=0.0,
            segment_gap_min=0.0, segment_gap_max=0.0,
            segment_max_len=200, segment_max_count=3,
        ),
    )
    orch = SimpleNamespace(cfg=cfg, adapter=adapter)
    orch._voice_down_until = 0.0
    orch._try_voice = types.MethodType(Orchestrator._try_voice, orch)
    orch._send_humanized = types.MethodType(Orchestrator._send_humanized, orch)
    return orch


# 把 speak 打桩成"成功"，避免这里还要起 HTTP 服务
import app.orchestrator as orch_mod  # noqa: E402

_real_speak = orch_mod.speak
_real_alive = orch_mod.service_alive

# 端口探测也打桩成"通"。真去连 127.0.0.1 会白白等 0.8 秒，而且 CI 上没有服务。
orch_mod.service_alive = lambda *a, **kw: True


async def _ok_speak(cfg, adapter, msg, reply, **kw):
    await adapter.send_record(msg, b"RIFFok", ext="mp3")
    return 7


async def _bad_speak(cfg, adapter, msg, reply, **kw):
    raise RuntimeError("合成挂了")


# 私聊 + 被要求 → 发语音，不发文字
orch_mod.speak = _ok_speak
sent_records.clear()
sent_texts.clear()
o = fake_orch(cfg_for(trigger="request"), _FakeAdapter())
asyncio.run(Orchestrator._send_humanized(o, PRIVATE, "好呀我念给你听"))
check("被要求时发语音", len(sent_records) == 1)
check("发语音时不重复发文字", len(sent_texts) == 0)
check("语音按私聊会话发出", sent_records and sent_records[0][0] == "private:1001")

# 私聊 + 没被要求 + request 模式 → 发文字
sent_records.clear()
sent_texts.clear()
asyncio.run(Orchestrator._send_humanized(o, PRIVATE_PLAIN, "吃面吧"))
check("没被要求时发文字", len(sent_texts) == 1 and not sent_records)

# 合成失败 → 回退文字，不能让消息消失
orch_mod.speak = _bad_speak
sent_records.clear()
sent_texts.clear()
asyncio.run(Orchestrator._send_humanized(o, PRIVATE, "好呀我念给你听"))
check("合成失败回退文字", len(sent_texts) == 1, f"实际 {len(sent_texts)} 条")
check("回退后不允许重复发语音", not sent_records)

# 群里 group=False → 即使被要求也要发（点名优先），没被要求则闭嘴
orch_mod.speak = _ok_speak
g_cfg = cfg_for(trigger="request", group=False)
o2 = fake_orch(g_cfg, _FakeAdapter())
sent_records.clear()
sent_texts.clear()
GRP_PLAIN = IncomingMessage(message_type="group", user_id=1001, group_id=2002,
                            self_id=1, text="今天吃啥")
asyncio.run(Orchestrator._send_humanized(o2, GRP_PLAIN, "吃面吧"))
check("群里默认不发语音", not sent_records and len(sent_texts) == 1)

sent_records.clear()
sent_texts.clear()
asyncio.run(Orchestrator._send_humanized(o2, GROUP, "好呀我念给你听"))
check("群里被点名要语音时照发", len(sent_records) == 1)

# 全语音模式越过 tts.group —— 用户按的是「全都要」那个按钮
o_all = fake_orch(cfg_for(trigger="request", group=False, always_voice=True),
                  _FakeAdapter())
sent_records.clear()
sent_texts.clear()
asyncio.run(Orchestrator._send_humanized(o_all, GRP_PLAIN, "吃面吧"))
check("全语音模式下群里也发语音", len(sent_records) == 1 and not sent_texts)

# 开关关着 → 即使被要求也不发
o3 = fake_orch(cfg_for(enabled=False, trigger="auto", chance=1.0), _FakeAdapter())
sent_records.clear()
sent_texts.clear()
asyncio.run(Orchestrator._send_humanized(o3, PRIVATE, "好呀"))
check("开关关着时不发语音", not sent_records and len(sent_texts) == 1)

# 模型自己在说「我不会发语音」→ 就算被要求，也只发文字
sent_records.clear()
sent_texts.clear()
o4 = fake_orch(cfg_for(trigger="auto", chance=1.0), _FakeAdapter())
asyncio.run(Orchestrator._send_humanized(
    o4, PRIVATE, "老大我不会发语音，张嘴也发不出声啊"))
check("否认语音的回复只发文字", not sent_records and len(sent_texts) == 1)

# 语音服务端口不通 → 立刻降级文字，而且不能卡住（之前是等满 timeout 才放弃）
orch_mod.service_alive = _real_alive          # 这一条要真探端口
sent_records.clear()
sent_texts.clear()
o5 = fake_orch(cfg_for(trigger="request", timeout=30.0), _FakeAdapter())
t0 = time.perf_counter()
asyncio.run(Orchestrator._send_humanized(o5, PRIVATE, "好呀我念给你听"))
cost = time.perf_counter() - t0
check("服务没开时改发文字", not sent_records and len(sent_texts) == 1)
check("服务没开时立刻降级（<2 秒，不等 timeout）", cost < 2.0, f"实际 {cost:.2f}s")

# 失败后进入冷却：紧接着的下一条不再去试（否则每条都白等一次探测）
sent_records.clear()
sent_texts.clear()
t0 = time.perf_counter()
asyncio.run(Orchestrator._send_humanized(o5, PRIVATE, "再说一句给我听"))
check("冷却期内不再尝试合成", not sent_records and len(sent_texts) == 1)
check("冷却期内几乎不花时间", time.perf_counter() - t0 < 0.5)

check("service_alive 对没开的端口返回 False",
      not service_alive("http://127.0.0.1:9", 0.3))

# 活着的端口：随手开一个只听不接的 socket，握手成功就算通
import socket as _socket  # noqa: E402

_live = _socket.socket()
_live.bind(("127.0.0.1", 0))
_live.listen(1)
_live_port = _live.getsockname()[1]
check("service_alive 对活着的端口返回 True",
      service_alive(f"http://127.0.0.1:{_live_port}", 0.5))
check("service_alive 端口写错也不抛异常",
      not service_alive("不是个网址", 0.3))
_live.close()

# ── 提示词：得先告诉模型它能出声，否则它会当面拒绝 ──
from app.context import ContextBuilder, VOICE_RULES  # noqa: E402


def _ctx(tts_on: bool):
    fake = SimpleNamespace(cfg=SimpleNamespace(
        tts=SimpleNamespace(enabled=tts_on),
        whitelist=SimpleNamespace(masters=[]),
        persona_text="人设", group_persona_text="",
    ))
    fake._speaking_rules = types.MethodType(ContextBuilder._speaking_rules, fake)
    return fake


_pv_on = ContextBuilder.static_prefix(_ctx(True), group=False)
_pv_off = ContextBuilder.static_prefix(_ctx(False), group=False)
_gv_on = ContextBuilder.static_prefix(_ctx(True), group=True)
check("开了语音：私聊提示词里说「你能出声」",
      any("你能出声" in m["content"] for m in _pv_on))
check("关了语音：提示词里不提语音（省 token）",
      not any("你能出声" in m["content"] for m in _pv_off))
check("开了语音：群里也要说「你能出声」",
      any("你能出声" in m["content"] for m in _gv_on))
check("提示词里明确禁止「我不会发语音」",
      "不许出现「我不会发语音」" in VOICE_RULES)
check("system 条数没变（不动缓存结构）",
      len(_pv_on) == 2 and len(_gv_on) == 3)

# ── 随机晚回：对方在等语音时不许「装忙」──
# 踩过：要语音之后抽中 8% 的随机晚回，安静了 2.8 分钟，用户以为机器人卡死了。
def _due(text: str) -> float:
    fake = SimpleNamespace(
        cfg=SimpleNamespace(humanize=SimpleNamespace(
            late_reply_chance=1.0, late_reply_min=60.0, late_reply_max=1500.0)),
        life=SimpleNamespace(quiet_delay=lambda now: 0),
    )
    return Orchestrator._reply_due_at(fake, IncomingMessage(
        message_type="group", user_id=1, group_id=2, self_id=3, text=text))[0]


check("被要语音时不随机晚回", _due("发个语音给我问个好") == 0.0)
check("普通闲聊仍可能晚回（拟人还在）", _due("今天吃啥") > 0.0)

orch_mod.speak = _real_speak
orch_mod.service_alive = _real_alive

# ══════════════════════ 收尾 ══════════════════════
print()
print("=" * 70)
if FAILED:
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print(f"通过 {PASSED} 项，全部通过")
