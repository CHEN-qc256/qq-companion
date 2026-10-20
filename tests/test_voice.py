"""声音页测试（不联网、不调真模型、不起 QQ）。

六组：
  A 目录探测      —— 能按 api_v2.py 找到程序目录、能列出权重并按音色配对
  B 参考音频      —— 文件名净化、wav 校验、写入、时长
  C 情绪库        —— 绑定/解绑、一个文件只属于一个情绪、删除时自动解绑
  D 路径安全      —— 越界路径、路径穿越、试听文件名的白名单
  E 服务连不上时  —— 一律给中文原因，不许抛裸异常
  F HTTP 接口     —— 真起一个面板服务，把 /api/voice/* 打一遍

跑法：python tests/test_voice.py
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import sys
import threading
import time
import wave
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from panel import configio, voice  # noqa: E402
from panel.server import Ctx, Handler  # noqa: E402

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


# ──────────────────────────────────────────────────────────────────────
#  造一个假的部署目录（只造目录结构和文件，不跑模型）
# ──────────────────────────────────────────────────────────────────────
TMP = ROOT / "data" / "_test_voice"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

INSTALL = TMP / "install"
REFS = INSTALL / "refs"
APP = INSTALL / "app" / "GPT-SoVITS-v2pro-test"
GW = APP / "GPT_weights_v2Pro"
SW = APP / "SoVITS_weights_v2Pro"
for d in (REFS, GW, SW):
    d.mkdir(parents=True, exist_ok=True)
(APP / "api_v2.py").write_text("# fake entry\n", encoding="utf-8")

(GW / "遥遥-e10.ckpt").write_bytes(b"\0" * 1024)
(GW / "旁白-e3.ckpt").write_bytes(b"\0" * 1024)
(SW / "遥遥_e5_s200.pth").write_bytes(b"\0" * 1024)

(INSTALL / "api_v2.log").write_text(
    "INFO: 开始加载\n"
    "Loading Text2Semantic weights from GPT_weights_v2Pro\\遥遥-e10.ckpt\n"
    "Loading VITS weights from SoVITS_weights_v2Pro\\遥遥_e5_s200.pth. <All keys matched successfully>\n"
    "INFO: Uvicorn running on http://127.0.0.1:9880\n",
    encoding="utf-8")


def make_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * frames)
    return path.read_bytes()


WAV_BYTES = make_wav(REFS / "default.wav", 1.0)
make_wav(REFS / "happy.wav", 1.5)
make_wav(REFS / "sad.wav", 0.8)
(REFS / "default.txt").write_text("默认参考音频的原话", encoding="utf-8")
(REFS / "happy.txt").write_text("开心的原话", encoding="utf-8")

# 一个能读的配置（tts.install_dir 指向假部署目录）
CFG_PATH = TMP / "config.yaml"
shutil.copy2(ROOT / "config.example.yaml", CFG_PATH)
_raw = configio.load(CFG_PATH)
_raw["database"]["path"] = "data/_test_voice/voice.db"
_raw["logging"]["file"] = "data/_test_voice/voice.log"
_raw["tts"]["install_dir"] = str(INSTALL).replace("\\", "/")
_raw["tts"]["api_base"] = "http://127.0.0.1:1"      # 故意指一个没人听的端口
configio.save(CFG_PATH, _raw, backup=False)

CFG = SimpleNamespace(tts=SimpleNamespace(
    enabled=False, api_base="http://127.0.0.1:1",
    install_dir=str(INSTALL), max_chars=120, timeout=2.0,
    speed=1.0, default_emotion="neutral"))


def main() -> int:
    # ────────────────────────────────────────────────────────────
    section("A 目录探测与权重配对")
    # ────────────────────────────────────────────────────────────
    app_dir = voice.detect_app_dir(INSTALL)
    check("能按 api_v2.py 找到程序目录", app_dir == APP, f"实际 {app_dir}")
    check("找不到时返回 None，不抛异常",
          voice.detect_app_dir(TMP / "不存在") is None)
    check("refs_dir 是按部署目录拼的", voice.refs_dir(INSTALL) == REFS)

    w = voice.list_weights(app_dir)
    check("GPT 权重扫到 2 个", len(w["gpt"]) == 2, f"实际 {len(w['gpt'])}")
    check("SoVITS 权重扫到 1 个", len(w["sovits"]) == 1)
    names = {g["name"] for g in w["groups"]}
    check("按音色名配对出 2 组", len(w["groups"]) == 2, f"实际 {sorted(names)}")
    check("同一个人的 ckpt 和 pth 被归到一组",
          any(g["gpt"] and g["sovits"] and g["name"].endswith("遥遥") for g in w["groups"]),
          f"{[(g['name'], bool(g['gpt']), bool(g['sovits'])) for g in w['groups']]}")
    check("只有一半权重的组也列出来（旁白缺 SoVITS）",
          any(g["name"].endswith("旁白") and g["gpt"] and not g["sovits"] for g in w["groups"]))

    cur = voice.current_weights(INSTALL / "api_v2.log")
    check("从日志尾巴解析出当前加载的 GPT 权重",
          cur["gpt"] and "遥遥-e10.ckpt" in cur["gpt"], str(cur))
    check("从日志尾巴解析出当前加载的 SoVITS 权重（去掉英文尾巴）",
          cur["sovits"] and cur["sovits"].endswith(".pth"), str(cur))
    check("日志不存在时返回空值而不是报错",
          voice.current_weights(TMP / "nope.log")["gpt"] is None)

    # 社区模型（AI-Hobbyist / 魔搭）的 v4 命名：SoVITS 尾巴多一个 _l32
    check("v4 的 _l32 尾巴也归一",
          voice._group_key("申鹤_ZH-e10.ckpt") == voice._group_key("申鹤_ZH_e10_s290_l32.pth"),
          f"{voice._group_key('申鹤_ZH-e10.ckpt')} vs {voice._group_key('申鹤_ZH_e10_s290_l32.pth')}")
    check("归一后不残留 epoch/step",
          voice._group_key("申鹤_ZH_e10_s290_l32.pth") == "申鹤_zh")
    check("展示名剥掉语言后缀", voice._display_name("申鹤_zh") == "申鹤")
    check("展示名不误删长后缀", voice._display_name("my_voice") == "my_voice")
    check("分组里带 label（给人看的名字）",
          all("label" in g for g in w["groups"]))

    # ────────────────────────────────────────────────────────────
    section("B 参考音频（上传、命名、校验）")
    # ────────────────────────────────────────────────────────────
    check("名字里的目录部分被剥掉", voice.safe_ref_name("../evil.wav") == "evil.wav")
    check("中文名保留，空格换成下划线",
          voice.safe_ref_name("遥遥 01") == "遥遥_01.wav",
          voice.safe_ref_name("遥遥 01"))
    check("没写后缀也补 .wav", voice.safe_ref_name("旁白") == "旁白.wav")
    try:
        voice.safe_ref_name("   ")
        check("空名字被拒绝", False)
    except voice.VoiceError:
        check("空名字被拒绝", True)
    try:
        voice.safe_ref_name("CON")
        check("系统保留名被拒绝", False)
    except voice.VoiceError:
        check("系统保留名被拒绝", True)

    r = voice.save_ref(REFS, "测试音色", WAV_BYTES, "这是原话", "")
    check("save_ref 返回文件名和时长",
          r["file"] == "测试音色.wav" and r["duration"] == 1.0, str(r))
    check("同名 .txt 一并写入",
          (REFS / "测试音色.txt").read_text(encoding="utf-8") == "这是原话")

    try:
        voice.save_ref(REFS, "坏的", b"not a wav at all", "", "")
        check("非 wav 数据被拒绝", False)
    except voice.VoiceError as e:
        check("非 wav 数据被拒绝", "不是 wav" in str(e), str(e))
    try:
        voice.save_ref(REFS, "空的", b"", "", "")
        check("空数据被拒绝", False)
    except voice.VoiceError:
        check("空数据被拒绝", True)

    refs = voice.list_refs(REFS)
    check("list_refs 列出 4 条（含刚上传的）", len(refs) == 4, f"实际 {len(refs)}")
    check("每条都带时长和参考文本",
          all(x["duration"] and x["duration"] > 0 for x in refs)
          and any(x["text"] == "开心的原话" for x in refs))
    check("refs 目录不存在时返回空列表",
          voice.list_refs(TMP / "no-refs") == [])

    # ────────────────────────────────────────────────────────────
    section("C 情绪库")
    # ────────────────────────────────────────────────────────────
    voice.bind_emotion(REFS, "happy", "happy.wav")
    check("绑定写进 emotions.json",
          voice.load_emotions(REFS) == {"happy": "happy.wav"},
          str(voice.load_emotions(REFS)))
    voice.bind_emotion(REFS, "sad", "happy.wav")
    m = voice.load_emotions(REFS)
    check("一个文件只能属于一个情绪（绑到新情绪会摘掉旧的）",
          m.get("sad") == "happy.wav" and "happy" not in m, str(m))
    try:
        voice.bind_emotion(REFS, "不合法!", "happy.wav")
        check("非法情绪名被拒绝", False)
    except voice.VoiceError:
        check("非法情绪名被拒绝", True)
    try:
        voice.bind_emotion(REFS, "ok", "不存在的.wav")
        check("绑定不存在的文件被拒绝", False)
    except voice.VoiceError:
        check("绑定不存在的文件被拒绝", True)

    r = voice.unbind_emotion(REFS, "sad")
    check("解绑成功且 json 里没了", r["ok"] and not voice.load_emotions(REFS))
    try:
        voice.unbind_emotion(REFS, "sad")
        check("解绑一个没绑过的情绪会报错", False)
    except voice.VoiceError:
        check("解绑一个没绑过的情绪会报错", True)

    voice.bind_emotion(REFS, "happy", "sad.wav")
    d = voice.delete_ref(REFS, INSTALL / "_trash", "sad.wav")
    check("删除是移到 _trash 不是真删",
          d["ok"] and not (REFS / "sad.wav").exists()
          and any(p.name == "sad.wav" for p in Path(d["trash"]).glob("*.wav")),
          str(d))
    check("删除后情绪绑定自动摘掉", "happy" not in voice.load_emotions(REFS))
    try:
        voice.delete_ref(REFS, INSTALL / "_trash", "sad.wav")
        check("删不存在的文件会报错", False)
    except voice.VoiceError:
        check("删不存在的文件会报错", True)

    # ────────────────────────────────────────────────────────────
    section("D 路径安全")
    # ────────────────────────────────────────────────────────────
    check("_within 认部署目录里的文件", voice._within(GW / "遥遥-e10.ckpt", voice.weight_roots(APP)))
    OUTSIDE = ROOT / "requirements.txt"      # 一定存在的「目录外文件」样本
    check("_within 挡住部署目录外的文件",
          not voice._within(OUTSIDE, voice.weight_roots(APP)))
    try:
        voice.load_weights("http://127.0.0.1:1", "gpt", str(OUTSIDE), APP, timeout=2)
        check("加载目录外的权重被拒绝", False)
    except voice.VoiceError as e:
        check("加载目录外的权重被拒绝", "只允许" in str(e), str(e))

    check("试听文件名挡住路径穿越", voice.preview_file(ROOT, "../../config.yaml") is None)
    check("试听文件名挡住非 uuid 名字", voice.preview_file(ROOT, "abc.wav") is None)
    name = voice.save_preview(ROOT, WAV_BYTES)
    check("自己生成的试听文件能取回来", voice.preview_file(ROOT, name) is not None, name)

    # ────────────────────────────────────────────────────────────
    section("E 服务连不上时给中文原因")
    # ────────────────────────────────────────────────────────────
    alive, why = voice.api_alive("http://127.0.0.1:1", timeout=1)
    check("连不上判定为不在线", alive is False and "连不上" in why, why)

    st = voice.status(CFG, ROOT)
    check("status 报告服务不在线但不崩", st["apiAlive"] is False)
    check("status 能认出部署目录", st["installDirOk"] is True and st["appDir"] == str(APP))
    check("status 数对了参考音频和音色组",
          st["refs"] == 3 and st["weights"]["groups"] == 2, str(st["weights"]))
    check("status 的 missing 为空（目录齐全）", st["missing"] == [], str(st["missing"]))
    check("status 带回 tts 设置", st["settings"]["maxChars"] == 120)
    check("status 带回全语音开关（默认关）", st["settings"]["alwaysVoice"] is False)
    check("status 带回 trigger/chance（关掉全语音时要能还原）",
          st["settings"]["trigger"] in ("request", "auto")
          and isinstance(st["settings"]["chance"], float))

    all_on = SimpleNamespace(tts=SimpleNamespace(
        enabled=True, always_voice=True, trigger="request", chance=0.55,
        group=True, api_base="http://127.0.0.1:1", install_dir=str(TMP),
        max_chars=120, timeout=2.0, speed=1.0, default_emotion="neutral"))
    check("status 能报出全语音已开",
          voice.status(all_on, ROOT)["settings"]["alwaysVoice"] is True)

    bad_cfg = SimpleNamespace(tts=SimpleNamespace(
        enabled=False, api_base="http://127.0.0.1:1", install_dir=str(TMP / "nowhere"),
        max_chars=120, timeout=2.0, speed=1.0, default_emotion="neutral"))
    st2 = voice.status(bad_cfg, ROOT)
    check("目录填错时 status 明确列出缺什么", len(st2["missing"]) >= 2, str(st2["missing"]))

    try:
        voice.synth("http://127.0.0.1:1", "你好", REFS / "default.wav", "", timeout=2)
        check("合成失败抛 VoiceError", False)
    except voice.VoiceError as e:
        check("合成失败抛 VoiceError 且提示启动服务",
              "start_api.bat" in str(e) or "连不上" in str(e), str(e))
    try:
        voice.synth("http://127.0.0.1:1", "   ", REFS / "default.wav", "", timeout=2)
        check("空文本在发请求前就被拦", False)
    except voice.VoiceError as e:
        check("空文本在发请求前就被拦", "空" in str(e), str(e))

    ref, prompt = voice.resolve_ref(REFS, "happy")
    check("按情绪找到参考音频和文本",
          ref.name == "happy.wav" and prompt == "开心的原话", f"{ref.name} / {prompt}")
    ref2, _ = voice.resolve_ref(REFS, "")
    check("不指定情绪时退回 default.wav", ref2.name == "default.wav", ref2.name)
    ref3, _ = voice.resolve_ref(REFS, "angry")
    check("情绪没配参考音频时退回 default.wav（聊天场景不能没声音）",
          ref3.name == "default.wav", ref3.name)

    # ────────────────────────────────────────────────────────────
    section("F HTTP 接口（真起一个面板）")
    # ────────────────────────────────────────────────────────────
    Handler.ctx = Ctx(CFG_PATH, ROOT)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    print(f"  （测试面板跑在 127.0.0.1:{port}）")

    def http(method, path, body=None, timeout=20):
        c = HTTPConnection("127.0.0.1", port, timeout=timeout)
        payload = None if body is None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"} if payload is not None else {}
        c.request(method, path, payload, hdrs)
        resp = c.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        c.close()
        try:
            return resp.status, json.loads(raw.decode("utf-8")), ctype, raw
        except Exception:
            return resp.status, None, ctype, raw

    st_code, d, _, _ = http("GET", "/api/voice/status")
    check("GET /api/voice/status", st_code == 200 and d.get("ok") is True, str(d)[:120])
    check("status 里 apiAlive=False 且带原因",
          d["apiAlive"] is False and d["apiWhy"], str(d.get("apiWhy")))
    check("status 里的 installDir 就是配置里那个",
          d["installDir"].replace("\\", "/") == str(INSTALL).replace("\\", "/"),
          d["installDir"])

    st_code, d, _, _ = http("GET", "/api/voice/weights")
    check("GET /api/voice/weights 返回配对和当前权重",
          st_code == 200 and len(d["groups"]) == 2
          and d["current"]["gpt"] and "遥遥" in d["current"]["gpt"], str(d)[:120])

    st_code, d, _, _ = http("GET", "/api/voice/refs")
    check("GET /api/voice/refs 列出参考音频",
          st_code == 200 and len(d["refs"]) == 3, f"{len(d.get('refs', []))} 条")
    check("refs 里带情绪映射", "emotions" in d)

    st_code, _, ctype, raw = http("GET", "/api/voice/ref-audio?f=default.wav")
    check("GET /api/voice/ref-audio 能播参考音频",
          st_code == 200 and raw[:4] == b"RIFF" and "audio/wav" in ctype,
          f"{st_code} {ctype}")
    st_code, _, _, _ = http("GET", "/api/voice/ref-audio?f=../../config.yaml")
    check("ref-audio 挡住路径穿越", st_code == 404, str(st_code))
    st_code, _, _, _ = http("GET", "/api/voice/audio?f=../../config.yaml")
    check("试听接口挡住路径穿越", st_code == 404, str(st_code))

    st_code, d, _, _ = http("POST", "/api/voice/preview", {"text": "测试一句"})
    check("服务没开时试听返回 ok=False 而不是 500",
          st_code == 200 and d.get("ok") is False and d.get("error"), str(d)[:140])
    check("试听的错误信息是人话（提示启动服务）",
          "连不上" in d.get("error", "") or "start_api" in d.get("error", ""),
          d.get("error", ""))

    b64 = base64.b64encode(WAV_BYTES).decode()
    st_code, d, _, _ = http("POST", "/api/voice/refs/upload",
                            {"name": "接口上传", "data": "data:audio/wav;base64," + b64,
                             "text": "接口上传的原话", "emotion": "gentle"})
    check("POST refs/upload 带 data: 前缀也能收",
          st_code == 200 and d.get("ok") is True and (REFS / "接口上传.wav").is_file(),
          str(d)[:140])
    check("上传时绑定的情绪落盘",
          voice.load_emotions(REFS).get("gentle") == "接口上传.wav",
          str(voice.load_emotions(REFS)))

    st_code, d, _, _ = http("POST", "/api/voice/refs/upload",
                            {"name": "坏文件", "data": base64.b64encode(b"junk").decode()})
    check("POST refs/upload 拒收非 wav", st_code == 200 and d.get("ok") is False, str(d)[:120])

    st_code, d, _, _ = http("POST", "/api/voice/refs/bind",
                            {"emotion": "happy", "file": "default.wav"})
    check("POST refs/bind", st_code == 200 and d.get("ok") is True
          and voice.load_emotions(REFS).get("happy") == "default.wav", str(d)[:120])

    st_code, d, _, _ = http("POST", "/api/voice/refs/unbind", {"emotion": "happy"})
    check("POST refs/unbind", st_code == 200 and d.get("ok") is True
          and "happy" not in voice.load_emotions(REFS), str(d)[:120])

    st_code, d, _, _ = http("POST", "/api/voice/refs/delete", {"file": "接口上传.wav"})
    check("POST refs/delete 移走文件并解绑",
          st_code == 200 and d.get("ok") is True
          and not (REFS / "接口上传.wav").exists()
          and "gentle" not in voice.load_emotions(REFS), str(d)[:140])

    st_code, d, _, _ = http("POST", "/api/voice/weights/load",
                            {"kind": "gpt", "path": str(ROOT / "config.yaml")})
    check("POST weights/load 拒绝目录外的路径",
          st_code == 200 and d.get("ok") is False, str(d)[:140])

    st_code, d, _, _ = http("POST", "/api/voice/weights/load",
                            {"kind": "乱写", "path": str(GW / "遥遥-e10.ckpt")})
    check("POST weights/load 拒绝非法 kind",
          st_code == 200 and d.get("ok") is False, str(d)[:140])

    st_code, d, _, _ = http("POST", "/api/voice/service", {"action": "start"})
    check("没有 start_api.bat 时启动会给出明确错误",
          st_code == 200 and d.get("ok") is False and "start_api.bat" in d.get("error", ""),
          str(d)[:140])

    st_code, d, _, _ = http("POST", "/api/voice/service", {"action": "乱来"})
    check("service 拒绝非法 action", st_code == 200 and d.get("ok") is False, str(d)[:120])

    # 连续两个带 body 的 POST 打同一条连接（坑 6 的回归）
    from http.client import HTTPConnection as HC
    c = HC("127.0.0.1", port, timeout=20)
    for payload in ({"emotion": "sad", "file": "default.wav"},
                    {"emotion": "sad", "file": "happy.wav"}):
        c.request("POST", "/api/voice/refs/bind",
                  json.dumps(payload).encode(), {"Content-Type": "application/json"})
        resp = c.getresponse()
        resp.read()
    c.close()
    check("同一长连接上第二个 POST 没串包（绑定的是 happy.wav）",
          voice.load_emotions(REFS).get("sad") == "happy.wav",
          str(voice.load_emotions(REFS)))

    httpd.shutdown()

    # ────────────────────────────────────────────────────────────
    section("G 前后端一致性（哑前端最容易犯的错）")
    # ────────────────────────────────────────────────────────────
    # 没有浏览器也要能发现「JS 引用了不存在的 id」这种错 —— 它不会报错，
    # 只会在用户点下去的那一刻抛 TypeError，然后整页不动。
    js = (ROOT / "panel" / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "panel" / "web" / "index.html").read_text(encoding="utf-8")
    server_src = (ROOT / "panel" / "server.py").read_text(encoding="utf-8")

    ids = sorted(set(re.findall(r"\$\('#(v[A-Za-z]+)'\)", js)))
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    missing = [i for i in ids if i not in html_ids]
    check("app.js 引用的声音页元素 id 全在 index.html 里", not missing, str(missing))
    check("声音页至少引用了 20 个元素（别被整段删了）", len(ids) >= 20, f"实际 {len(ids)}")

    tabs = re.findall(r'data-tab="([a-z]+)"', html)
    check("页签有 4 个且包含 voice", len(tabs) == 4 and "voice" in tabs, str(tabs))
    no_section = [t for t in tabs if f'id="tab-{t}"' not in html]
    check("每个页签都有对应的 section", not no_section, str(no_section))

    api_calls = sorted(set(re.findall(r"'(/api/voice/[a-z\-]+)'", js)))
    not_routed = [a for a in api_calls if a not in server_src]
    check("前端调用的 /api/voice/* 后端都有路由", not not_routed, str(not_routed))
    check("前端至少调了 5 个声音接口", len(api_calls) >= 5, str(api_calls))

    # ────────────────────────────────────────────────────────────
    section("H 一键用音色（切权重 + 同步参考音频）")
    # ────────────────────────────────────────────────────────────
    h_refs = TMP / "h_refs"
    h_refs.mkdir(parents=True, exist_ok=True)
    (h_refs / "default.wav").write_bytes(b"RIFFold")
    (h_refs / "default.txt").write_text("旧的参考话", encoding="utf-8")
    (h_refs / "申鹤.wav").write_bytes(b"RIFFnew")
    (h_refs / "申鹤.txt").write_text("新的参考话", encoding="utf-8")

    got = voice.set_default_ref(h_refs, h_refs / "申鹤.wav")
    check("返回新的默认文件名", got == "default.wav")
    check("默认参考音频被替换",
          (h_refs / "default.wav").read_bytes() == b"RIFFnew")
    check("参考文本跟着一起换",
          (h_refs / "default.txt").read_text(encoding="utf-8") == "新的参考话")
    check("旧的 default 进了 _trash 而不是删掉",
          any(voice.trash_dir(TMP).glob("default-*.wav")),
          str(list(voice.trash_dir(TMP).glob("*"))))
    check("已经指向自己时不做无谓拷贝",
          voice.set_default_ref(h_refs, h_refs / "default.wav") == "default.wav")

    # neutral 被绑到别的文件时会绕过 default.wav，切音色要顺手改回来
    voice.save_emotions(h_refs, {"neutral": "申鹤.wav", "happy": "申鹤.wav"})
    (h_refs / "其他.wav").write_bytes(b"RIFFother")
    voice.save_emotions(h_refs, {"neutral": "其他.wav"})
    voice.set_default_ref(h_refs, h_refs / "申鹤.wav")
    em = voice.load_emotions(h_refs)
    check("neutral 被重新指向 default.wav", em.get("neutral") == "default.wav", str(em))

    calls = []
    real_load = voice.load_weights

    def _fake_load(api_base, kind, path, app_dir, timeout=30.0):
        calls.append((kind, path))
        return {"ok": True}

    voice.load_weights = _fake_load  # type: ignore[assignment]
    try:
        out = voice.use_voice("http://127.0.0.1:1", APP, h_refs,
                              gpt_path="g.ckpt", sovits_path="s.pth", label="申鹤")
    finally:
        voice.load_weights = real_load  # type: ignore[assignment]

    check("一次调用切了 GPT 和 SoVITS",
          [c[0] for c in calls] == ["gpt", "sovits"], str(calls))
    check("切换顺序是 GPT → SoVITS（先文本后声码器）",
          calls and calls[0][0] == "gpt")
    check("返回里带同步的参考音频", out.get("ref") == "default.wav", str(out))
    check("提示语说清楚了做了什么", "GPT" in out.get("msg", ""), out.get("msg"))

    calls.clear()
    voice.load_weights = _fake_load  # type: ignore[assignment]
    try:
        out2 = voice.use_voice("http://127.0.0.1:1", APP, h_refs,
                               gpt_path="g.ckpt", label="查无此人")
    finally:
        voice.load_weights = real_load  # type: ignore[assignment]
    check("找不到同名参考音频时不报错，只是不换",
          out2.get("ok") and out2.get("ref") == "" and "没找到同名参考音频" in out2.get("msg", ""),
          str(out2))

    # ────────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print(f"  通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("  失败项：" + "；".join(FAILED))
    print("=" * 70)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
