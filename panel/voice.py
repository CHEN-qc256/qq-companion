"""声音管理 —— 本地 GPT-SoVITS 的后端（纯 Python 标准库）。

它只做四件事，每件都能独立失败、互不牵连：
  1. **看**：TTS 服务活没活、当前加载的是哪套权重、显存占多少
  2. **听**：用任意音色 / 任意参考音频合成一句，落盘给前端 <audio> 播
  3. **管**：音色权重（GPT / SoVITS 配对）、参考音频 + 情绪库（refs/emotions.json）
  4. **克隆**：上传一段 3~10 秒干声 → 进 refs → 立刻可用（零样本，不训练）

★ 三条硬约束（违反会静默出错，不是崩溃）：
  - **必须绕开系统代理**：本机有 HTTP_PROXY，不绕的话访问 127.0.0.1 会拿到 502，
    表现是「服务明明活着，面板却说连不上」。用 urllib 的 ProxyHandler({})。
  - **不能 import 第三方包**：面板的定位是纯标准库（见 panel/server.py 抬头）。
  - **路径必须在部署目录里**：所有「按名字取文件」的接口都要先过 _within()，
    否则面板就成了任意文件读取器。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 上传单条参考音频的上限。3~10 秒的 wav 通常 0.3~1 MB，20 MB 已经非常宽松。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 试听文件保留多久（秒）。面板每次试听都会生成一个，不清会越攒越多。
PREVIEW_TTL_SECONDS = 6 * 3600
# 情绪名只允许这些字符 —— 它会被拼进 URL 和文件名。
_EMOTION_RE = re.compile(r"^[a-z][a-z0-9_]{1,19}$")
# 试听文件名固定形态（uuid hex），防止拿它当路径穿越的入口。
_PREVIEW_RE = re.compile(r"^[0-9a-f]{12}\.wav$")
# 拒绝用这些名字当参考音频（Windows 保留名 + 会被当成隐藏文件的名字）
_BAD_NAMES = {"con", "prn", "aux", "nul", "com1", "com2", "com3", "com4",
              "lpt1", "lpt2", "lpt3"}


class VoiceError(Exception):
    """可以被展示给用户的错误（中文、带下一步建议）。"""


# ──────────────────────────────────────────────────────────────────────
#  HTTP（绕代理）
# ──────────────────────────────────────────────────────────────────────
def _opener():
    """永远绕开系统代理。

    ★ 有些环境的 HTTP_PROXY 指向本地代理，它不认识 127.0.0.1，
    结果就是 curl / requests / urllib 访问本机 API 全部拿到 502。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(url: str, payload: Optional[dict] = None,
             timeout: float = 10.0) -> Tuple[int, bytes]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with _opener().open(req, timeout=timeout) as r:
            return int(getattr(r, "status", 200)), r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return int(e.code), body
    except Exception as e:                       # 连接被拒 / 超时 / DNS
        return 0, str(e).encode("utf-8", "ignore")


def _json_of(raw: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8", "ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def api_alive(api_base: str, timeout: float = 2.0) -> Tuple[bool, str]:
    """服务活没活。返回 (活着吗, 失败原因)。

    探 /docs 而不是 /：api_v2.py 是 FastAPI，/ 没有路由会 404，
    而 404 也说明「服务是活的」—— 但我们只认 200，避免把别的进程当成本服务。
    """
    code, _ = _request(api_base.rstrip("/") + "/docs", timeout=timeout)
    if code == 200:
        return True, ""
    if code == 0:
        return False, "连不上（服务没启动，或端口不对）"
    return False, f"服务有响应但状态码是 {code}，可能不是 GPT-SoVITS"


# ──────────────────────────────────────────────────────────────────────
#  目录探测
# ──────────────────────────────────────────────────────────────────────
def _safe_dir(p: str | Path) -> Optional[Path]:
    try:
        path = Path(p)
        return path if path.is_dir() else None
    except (OSError, ValueError):
        return None


def detect_app_dir(install_dir: Path) -> Optional[Path]:
    """找到真正含 api_v2.py 的那个程序目录。

    整合包解压后是 install/app/GPT-SoVITS-v2pro-*/ 这种带版本号的名字，
    升级换包时目录名会变 —— 所以不能写死，只能扫。
    """
    base = install_dir / "app"
    if not base.is_dir():
        return None
    cands = [d for d in base.iterdir() if d.is_dir() and (d / "api_v2.py").is_file()]
    if not cands:
        return None
    cands.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return cands[0]


def refs_dir(install_dir: Path) -> Path:
    return install_dir / "refs"


def log_path(install_dir: Path) -> Path:
    return install_dir / "api_v2.log"


def start_bat(install_dir: Path) -> Path:
    return install_dir / "start_api.bat"


def preview_dir(root: Path) -> Path:
    d = root / "data" / "_voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def trash_dir(install_dir: Path) -> Path:
    return install_dir / "_trash"


def _within(target: Path, roots: List[Path]) -> bool:
    """target 是否在 roots 之一里面（解析软链接之后再比）。"""
    try:
        t = target.resolve()
    except (OSError, ValueError):
        return False
    for r in roots:
        try:
            if t == r.resolve() or r.resolve() in t.parents:
                return True
        except (OSError, ValueError):
            continue
    return False


def weight_roots(app_dir: Optional[Path]) -> List[Path]:
    """允许读写的权重目录（当前版本 + 历史版本都算）。"""
    if not app_dir or not app_dir.is_dir():
        return []
    out = []
    for pat in ("GPT_weights*", "SoVITS_weights*", "GPT_SoVITS"):
        out.extend([d for d in app_dir.glob(pat) if d.is_dir()])
    return out


# ──────────────────────────────────────────────────────────────────────
#  权重（音色）
# ──────────────────────────────────────────────────────────────────────
def _group_key(filename: str) -> str:
    """把微调产出的文件名归一成「同一个音色」。

    WebUI 训练出来的名字长这样：
        xxx-e15.ckpt          （GPT / s1）
        xxx_e8_s208.pth       （SoVITS / s2）
        xxx_e10_s290_l32.pth  （v4 的 SoVITS，尾巴多一个 _l32）
    去掉末尾的 epoch / step / layer 尾巴，剩下的就是同一个人。
    """
    stem = Path(filename).stem
    stem = re.sub(r"[-_]e\d+([-_]s\d+)?([-_]l\d+)?$", "", stem, flags=re.I)
    return stem.strip().lower()


def _display_name(key: str) -> str:
    """申鹤_zh → 申鹤；my_voice_zh → my_voice。

    只剥两字母的语言后缀（zh/ja/en/ko），别把 my_voice 的 voice 当后缀吃掉。
    """
    parts = key.split("_")
    if len(parts) > 1 and len(parts[-1]) == 2 and parts[-1].isalpha():
        parts = parts[:-1]
    return "_".join(p for p in parts if p) or key


def list_weights(app_dir: Optional[Path]) -> Dict[str, Any]:
    gpt: List[Dict[str, Any]] = []
    sovits: List[Dict[str, Any]] = []
    if not app_dir or not app_dir.is_dir():
        return {"gpt": [], "sovits": [], "groups": []}

    for d in sorted(app_dir.glob("GPT_weights*")):
        if not d.is_dir():
            continue
        ver = d.name.replace("GPT_weights_", "").replace("GPT_weights", "v1")
        for f in sorted(d.glob("*.ckpt")):
            st = f.stat()
            gpt.append({"file": f.name, "path": str(f), "version": ver,
                        "size": st.st_size, "mtime": st.st_mtime,
                        "group": _group_key(f.name)})
    for d in sorted(app_dir.glob("SoVITS_weights*")):
        if not d.is_dir():
            continue
        ver = d.name.replace("SoVITS_weights_", "").replace("SoVITS_weights", "v1")
        for f in sorted(d.glob("*.pth")):
            st = f.stat()
            sovits.append({"file": f.name, "path": str(f), "version": ver,
                           "size": st.st_size, "mtime": st.st_mtime,
                           "group": _group_key(f.name)})

    groups: Dict[str, Dict[str, Any]] = {}
    for item in gpt:
        g = groups.setdefault(item["group"], {"name": item["group"],
                                              "label": _display_name(item["group"]),
                                              "gpt": None, "sovits": None})
        if not g["gpt"] or item["mtime"] > g["gpt"]["mtime"]:
            g["gpt"] = item
    for item in sovits:
        g = groups.setdefault(item["group"], {"name": item["group"],
                                              "label": _display_name(item["group"]),
                                              "gpt": None, "sovits": None})
        if not g["sovits"] or item["mtime"] > g["sovits"]["mtime"]:
            g["sovits"] = item

    ordered = sorted(groups.values(),
                     key=lambda g: max((g["gpt"] or {}).get("mtime", 0),
                                       (g["sovits"] or {}).get("mtime", 0)),
                     reverse=True)
    return {"gpt": gpt, "sovits": sovits, "groups": ordered}


def current_weights(log_file: Path) -> Dict[str, Any]:
    """从 api_v2.log 尾巴里读出「现在真正加载的是哪两个权重」。

    ★ 为什么读日志而不是问服务：api_v2.py **没有** 查询当前权重的接口。
    它能热切换（/set_*_weights），但切完不吭声。日志里每次加载都会打一行
    `Loading VITS weights from ...`，这是唯一的真源。
    读不到就当未知 —— 不要猜。
    """
    out: Dict[str, Any] = {"gpt": None, "sovits": None, "log": str(log_file)}
    try:
        if not log_file.is_file():
            return out
        size = log_file.stat().st_size
        with open(log_file, "rb") as f:
            f.seek(max(0, size - 262144))
            text = f.read().decode("utf-8", "ignore")
    except OSError:
        return out

    for line in reversed(text.splitlines()):
        if out["gpt"] is None and "Loading Text2Semantic weights from" in line:
            out["gpt"] = line.split("Loading Text2Semantic weights from", 1)[1].strip().rstrip(". ")
        if out["sovits"] is None and "Loading VITS weights from" in line:
            tail = line.split("Loading VITS weights from", 1)[1]
            # 真实日志是「路径. <All keys matched successfully>」—— 尾巴上会剩一个句号
            tail = tail.replace("<All keys matched successfully>", "").strip()
            out["sovits"] = tail.rstrip(". ")
        if out["gpt"] and out["sovits"]:
            break
    return out


def load_weights(api_base: str, kind: str, path: str,
                 app_dir: Optional[Path], timeout: float = 30.0) -> Dict[str, Any]:
    """热加载一个权重（不重启服务）。"""
    kind = (kind or "").strip().lower()
    if kind not in ("gpt", "sovits"):
        raise VoiceError("kind 只能是 gpt 或 sovits")
    p = Path(path or "")
    if not p.is_file():
        raise VoiceError(f"权重文件不存在：{path}")
    roots = weight_roots(app_dir)
    if not _within(p, roots):
        raise VoiceError("只允许加载部署目录里的权重文件")

    endpoint = "set_gpt_weights" if kind == "gpt" else "set_sovits_weights"
    url = "%s/%s?weights_path=%s" % (api_base.rstrip("/"), endpoint,
                                    urllib.parse.quote(str(p)))
    code, raw = _request(url, timeout=timeout)
    if code == 200:
        return {"ok": True, "msg": f"已切换 {'GPT' if kind == 'gpt' else 'SoVITS'} 权重"}
    detail = _json_of(raw).get("Exception") or raw.decode("utf-8", "ignore")[:300]
    raise VoiceError(f"切换失败（HTTP {code}）：{detail or '服务没有响应'}")


def use_voice(api_base: str, app_dir: Optional[Path], r_dir: Path,
              *, gpt_path: str = "", sovits_path: str = "",
              label: str = "", timeout: float = 90.0) -> Dict[str, Any]:
    """一键「用这个音色」：切 GPT + 切 SoVITS + 把同名参考音频设成默认。

    分开切权重有两个坑，社区模型尤其明显：
      1. 只切一半（比如只有 .ckpt 没有 .pth）会得到一个「音色错位」的组合
      2. 切了权重却还用着 default.wav，听起来跟名字对不上
    所以合成一个动作，前端点一次全搞定。
    """
    done: List[str] = []
    if gpt_path:
        load_weights(api_base, "gpt", gpt_path, app_dir, timeout=timeout)
        done.append("GPT")
    if sovits_path:
        load_weights(api_base, "sovits", sovits_path, app_dir, timeout=timeout)
        done.append("SoVITS")

    ref_name = ""
    if label:
        cand = r_dir / f"{label}.wav"
        if cand.is_file():
            ref_name = set_default_ref(r_dir, cand)

    msg = "已切换 " + " + ".join(done) if done else "没有可切换的权重"
    if ref_name:
        msg += f"，参考音频同步为 {ref_name}"
    elif label:
        msg += "（没找到同名参考音频，仍用原来的）"
    return {"ok": True, "msg": msg, "ref": ref_name}


def set_default_ref(r_dir: Path, src: Path) -> str:
    """把 src 复制成 refs/default.wav，旧的进 _trash。

    聊天侧（app/tts.py）找不到情绪对应的音频时最终回退的就是 default.wav，
    所以「换音色」在这里落地就够了，不用改任何配置。
    """
    dst = r_dir / "default.wav"
    if src.resolve() == dst.resolve():
        return dst.name

    if dst.is_file():
        t_dir = trash_dir(r_dir.parent)
        t_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(dst, t_dir / f"default-{stamp}.wav")
        except OSError:
            pass

    shutil.copy2(src, dst)
    src_txt = src.with_suffix(".txt")
    dst_txt = r_dir / "default.txt"
    if src_txt.is_file():
        shutil.copy2(src_txt, dst_txt)

    # neutral 如果被显式绑到别的文件，会绕过 default.wav —— 一并指过来
    em = load_emotions(r_dir)
    if em.get("neutral") not in (None, "default.wav"):
        em["neutral"] = "default.wav"
        try:
            save_emotions(r_dir, em)
        except OSError:
            pass
    return dst.name


# ──────────────────────────────────────────────────────────────────────
#  参考音频 + 情绪库
# ──────────────────────────────────────────────────────────────────────
def emotions_path(r_dir: Path) -> Path:
    return r_dir / "emotions.json"


def load_emotions(r_dir: Path) -> Dict[str, str]:
    """{情绪名: 文件名}。文件坏了就当空的 —— 不因为一个 json 让整页打不开。"""
    p = emotions_path(r_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()
                    if _EMOTION_RE.match(str(k))}
    except (OSError, ValueError):
        pass
    return {}


def save_emotions(r_dir: Path, mapping: Dict[str, str]) -> None:
    r_dir.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in mapping.items() if _EMOTION_RE.match(k)}
    tmp = emotions_path(r_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, emotions_path(r_dir))


def wav_duration(path: Path) -> Optional[float]:
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate() or 1
            return round(w.getnframes() / float(rate), 2)
    except Exception:
        return None


def list_refs(r_dir: Path) -> List[Dict[str, Any]]:
    mapping = load_emotions(r_dir)
    owner = {v: k for k, v in mapping.items()}
    out: List[Dict[str, Any]] = []
    if not r_dir.is_dir():
        return out
    for f in sorted(r_dir.glob("*.wav")):
        if not f.is_file():
            continue
        txt = f.with_suffix(".txt")
        text = ""
        if txt.is_file():
            try:
                text = txt.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
        st = f.stat()
        out.append({
            "file": f.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "duration": wav_duration(f),
            "text": text,
            "emotion": owner.get(f.name, ""),
        })
    out.sort(key=lambda r: (-(r["mtime"] or 0), r["file"]))
    return out


def safe_ref_name(name: str) -> str:
    """把用户输入的名字变成安全的文件名。

    保留中文和数字，其余一律换成下划线；强制 .wav 后缀。
    """
    raw = (name or "").strip()
    raw = Path(raw).name                      # 去掉任何目录部分
    if raw.lower().endswith(".wav"):
        raw = raw[:-4]
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]", "_", raw, flags=re.UNICODE).strip("._")
    if not cleaned:
        raise VoiceError("名字不能为空（可以用中文、字母、数字、下划线）")
    if cleaned.lower() in _BAD_NAMES:
        raise VoiceError(f"「{cleaned}」是系统保留名，换一个")
    if len(cleaned) > 60:
        cleaned = cleaned[:60]
    return cleaned + ".wav"


def _check_wav(raw: bytes) -> None:
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise VoiceError("这个文件不是 wav。先用 ffmpeg 转一下（面板上有现成命令）")


def save_ref(r_dir: Path, name: str, raw: bytes, text: str = "",
             emotion: str = "") -> Dict[str, Any]:
    """把上传的音频存成参考音频，同时写同名 .txt（参考文本必须逐字对应）。"""
    if not raw:
        raise VoiceError("没有收到音频数据")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise VoiceError(f"文件太大（{len(raw) / 1048576:.1f} MB），上限 "
                         f"{MAX_UPLOAD_BYTES // 1048576} MB")
    _check_wav(raw)
    fname = safe_ref_name(name)
    r_dir.mkdir(parents=True, exist_ok=True)

    target = r_dir / fname
    tmp = target.with_suffix(".wav.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, target)
    target.with_suffix(".txt").write_text((text or "").strip(), encoding="utf-8")

    if emotion:
        if not _EMOTION_RE.match(emotion):
            raise VoiceError("情绪名只能用小写字母、数字、下划线，2~20 位")
        mapping = load_emotions(r_dir)
        mapping[emotion] = fname
        save_emotions(r_dir, mapping)

    return {"ok": True, "file": fname,
            "duration": wav_duration(target),
            "msg": f"已保存 {fname}，现在就能在试听区用它说话"}


def bind_emotion(r_dir: Path, emotion: str, filename: str) -> Dict[str, Any]:
    emotion = (emotion or "").strip()
    if not _EMOTION_RE.match(emotion):
        raise VoiceError("情绪名只能用小写字母、数字、下划线，2~20 位")
    p = r_dir / Path(filename or "").name
    if not p.is_file():
        raise VoiceError(f"参考音频不存在：{filename}")
    mapping = load_emotions(r_dir)
    # 一个文件只能属于一个情绪：先把它从别处摘掉，避免「同一段音频绑两个情绪」
    for k in [k for k, v in mapping.items() if v == p.name]:
        mapping.pop(k)
    mapping[emotion] = p.name
    save_emotions(r_dir, mapping)
    return {"ok": True, "msg": f"{p.name} 已绑定到 {emotion}"}


def unbind_emotion(r_dir: Path, emotion: str) -> Dict[str, Any]:
    mapping = load_emotions(r_dir)
    if emotion not in mapping:
        raise VoiceError(f"没有绑定 {emotion}")
    mapping.pop(emotion)
    save_emotions(r_dir, mapping)
    return {"ok": True, "msg": f"已解绑 {emotion}"}


def delete_ref(r_dir: Path, t_dir: Path, filename: str) -> Dict[str, Any]:
    """删除参考音频 —— 移进 _trash 而不是真删。

    ★ 实测最容易犯的错：正在被合成用的那个 wav 删掉后，下一次请求报
    「找不到参考音频」，而用户以为自己只是清理了没用的文件。所以：
    留一份在 _trash，并且把 emotions.json 里的绑定一并摘掉。
    """
    name = Path(filename or "").name
    p = r_dir / name
    if not p.is_file():
        raise VoiceError(f"参考音频不存在：{name}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = t_dir / f"refs-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in (p, p.with_suffix(".txt")):
        if f.is_file():
            shutil.move(str(f), str(dest / f.name))

    mapping = load_emotions(r_dir)
    changed = [k for k, v in mapping.items() if v == name]
    for k in changed:
        mapping.pop(k)
    if changed:
        save_emotions(r_dir, mapping)

    note = f"（同时解绑了 {', '.join(changed)}）" if changed else ""
    return {"ok": True, "msg": f"{name} 已移到 _trash{note}", "trash": str(dest)}


# ──────────────────────────────────────────────────────────────────────
#  合成 / 试听
# ──────────────────────────────────────────────────────────────────────
def resolve_ref(r_dir: Path, emotion: str = "",
                filename: str = "") -> Tuple[Path, str]:
    """按「文件名 → 情绪 → default.wav」三级回退找参考音频。

    返回 (wav 路径, 参考文本)。找不到就抛错并告诉用户缺什么。
    """
    if filename:
        p = r_dir / Path(filename).name
        if not p.is_file():
            raise VoiceError(f"参考音频不存在：{filename}")
    elif emotion:
        name = load_emotions(r_dir).get(emotion)
        p = r_dir / name if name else r_dir / f"{emotion}.wav"
        if not p.is_file():
            # 情绪没配就退回默认音色，而不是直接失败 —— 聊天场景更需要「有声音」
            p = r_dir / "default.wav"
            if not p.is_file():
                raise VoiceError(f"没有 {emotion} 的参考音频，也没有 default.wav")
    else:
        p = r_dir / "default.wav"
        if not p.is_file():
            raise VoiceError("refs 里没有 default.wav，先上传一段参考音频")

    txt = p.with_suffix(".txt")
    prompt = txt.read_text(encoding="utf-8").strip() if txt.is_file() else ""
    return p, prompt


def synth(api_base: str, text: str, ref_wav: Path, prompt_text: str,
          *, speed: float = 1.0, text_lang: str = "zh", timeout: float = 60.0,
          ref_lang: str = "") -> bytes:
    """合成一句，返回 wav 字节。失败抛 VoiceError（带服务端原始信息）。"""
    text = (text or "").strip()
    if not text:
        raise VoiceError("文本是空的")
    payload = {
        "text": text,
        "text_lang": text_lang or "zh",
        "ref_audio_path": str(ref_wav),
        "prompt_text": prompt_text or "",
        "prompt_lang": ref_lang or text_lang or "zh",
        "text_split_method": "cut5",
        "speed_factor": float(speed or 1.0),
        "media_type": "wav",
        "streaming_mode": False,
    }
    code, raw = _request(api_base.rstrip("/") + "/tts", payload, timeout=timeout)
    if code == 200 and raw[:4] == b"RIFF":
        return raw
    detail = _json_of(raw).get("Exception") or raw.decode("utf-8", "ignore")[:300]
    if code == 0:
        raise VoiceError("连不上语音服务。先双击 start_api.bat，等它加载完（约 1 分钟）再试")
    raise VoiceError(f"合成失败（HTTP {code}）：{detail or '服务端没有返回原因'}")


def save_preview(root: Path, data: bytes) -> str:
    d = preview_dir(root)
    _sweep_previews(d)
    name = uuid.uuid4().hex[:12] + ".wav"
    (d / name).write_bytes(data)
    return name


def _sweep_previews(d: Path) -> None:
    """顺手清掉过期试听文件（只删本模块自己生成的 uuid 名字）。"""
    cutoff = time.time() - PREVIEW_TTL_SECONDS
    try:
        for f in d.glob("*.wav"):
            if _PREVIEW_RE.match(f.name) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def preview_file(root: Path, name: str) -> Optional[Path]:
    if not _PREVIEW_RE.match(name or ""):
        return None
    p = preview_dir(root) / name
    return p if p.is_file() else None


# ──────────────────────────────────────────────────────────────────────
#  服务控制
# ──────────────────────────────────────────────────────────────────────
def control(api_base: str, command: str, timeout: float = 10.0) -> Dict[str, Any]:
    command = (command or "").strip().lower()
    if command not in ("exit", "restart"):
        raise VoiceError("只支持 exit / restart")
    url = "%s/control?command=%s" % (api_base.rstrip("/"), command)
    code, raw = _request(url, timeout=timeout)
    if code == 200:
        return {"ok": True, "msg": "已退出" if command == "exit" else "正在重启"}
    detail = raw.decode("utf-8", "ignore")[:200]
    raise VoiceError(f"操作失败（HTTP {code}）：{detail or '服务没有响应'}")


def start_service(install_dir: Path) -> Dict[str, Any]:
    """启动 TTS 服务：优先用部署目录里的 start_api.bat（新开一个窗口）。

    ★ 故意不在这里加载模型：加载要 40~60 秒，面板的 HTTP 请求等不起，
    而且它是个常驻进程，必须活在自己的窗口里，不能挂在面板进程下面。
    """
    bat = start_bat(install_dir)
    if not bat.is_file():
        raise VoiceError(f"找不到启动脚本：{bat}（先确认 tts.install_dir 填对了）")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(["cmd", "/c", "start", "", str(bat)],
                         cwd=str(install_dir), creationflags=flags)
    except OSError as e:
        raise VoiceError(f"启动失败：{e}")
    return {"ok": True, "msg": "已拉起启动脚本。模型加载约 1 分钟，稍后点「刷新」"}


def gpu_memory() -> Optional[Dict[str, Any]]:
    """显存占用。拿不到就返回 None（没装 N 卡 / 没装驱动都不算错）。"""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return None
        line = (r.stdout or "").strip().splitlines()[0]
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            return None
        return {"name": parts[0], "usedMiB": int(float(parts[1])),
                "totalMiB": int(float(parts[2]))}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def status(cfg, root: Path) -> Dict[str, Any]:
    """一次把面板需要的所有「现状」都取回来。"""
    install = Path(getattr(getattr(cfg, "tts", None), "install_dir", "") or "")
    api_base = (getattr(getattr(cfg, "tts", None), "api_base", "") or "").strip()

    app_dir = detect_app_dir(install) if str(install) else None
    r_dir = refs_dir(install)
    l_path = log_path(install)

    alive, why = api_alive(api_base) if api_base else (False, "还没填语音服务地址")
    cur = current_weights(l_path)
    weights = list_weights(app_dir)
    refs = list_refs(r_dir)

    missing = []
    if not install.is_dir():
        missing.append(f"部署目录不存在：{install}")
    if not app_dir:
        missing.append("部署目录下没找到含 api_v2.py 的 app 子目录")
    if not r_dir.is_dir():
        missing.append("没有 refs 目录（上传一次参考音频会自动建）")

    return {
        "ok": True,
        "apiBase": api_base,
        "apiAlive": alive,
        "apiWhy": why,
        "installDir": str(install),
        "installDirOk": install.is_dir(),
        "appDir": str(app_dir) if app_dir else "",
        "refsDir": str(r_dir),
        "logPath": str(l_path),
        "startBat": str(start_bat(install)),
        "missing": missing,
        "current": cur,
        "weights": {"gpt": len(weights["gpt"]), "sovits": len(weights["sovits"]),
                    "groups": len(weights["groups"])},
        "refs": len(refs),
        "emotions": load_emotions(r_dir),
        "gpu": gpu_memory(),
        "settings": {
            "enabled": bool(getattr(getattr(cfg, "tts", None), "enabled", False)),
            "alwaysVoice": bool(getattr(getattr(cfg, "tts", None), "always_voice", False)),
            "trigger": getattr(getattr(cfg, "tts", None), "trigger", "request") or "request",
            "chance": float(getattr(getattr(cfg, "tts", None), "chance", 0.0) or 0.0),
            "group": bool(getattr(getattr(cfg, "tts", None), "group", False)),
            "maxChars": int(getattr(getattr(cfg, "tts", None), "max_chars", 120) or 120),
            "speed": float(getattr(getattr(cfg, "tts", None), "speed", 1.0) or 1.0),
            "timeout": float(getattr(getattr(cfg, "tts", None), "timeout", 60.0) or 60.0),
            "defaultEmotion": getattr(getattr(cfg, "tts", None),
                                      "default_emotion", "neutral") or "neutral",
        },
        "now": time.time(),
    }
