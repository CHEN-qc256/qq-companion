"""管理面板的 HTTP 服务 —— 只用 Python 标准库（http.server + json + threading）。

为什么不用 FastAPI/Flask：这个项目的第三方依赖只有 httpx / websockets / PyYAML
三个，都是机器人本身跑起来必需的。为了一个本地配置面板再拉一个 Web 框架进来
不划算 —— 而 http.server 足够支撑「一个人在本机点点点」这个量级。

安全默认值：**只绑定 127.0.0.1**。面板能读到 API Key、能启停服务，
绑到 0.0.0.0 就等于把这两样东西开放给同一个局域网。
需要从别的机器访问时，自己加 --host 0.0.0.0，并清楚后果。

启动：
    python -m panel                # 默认 http://127.0.0.1:8899
    python -m panel --port 9000 --config tests/config.e2e.yaml
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import configio, runner, schema, voice

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8899
DEFAULT_HOST = "127.0.0.1"


class Ctx:
    """一次运行的共享状态。"""

    def __init__(self, config_path: Path, root: Path):
        self.config_path = config_path
        self.root = root
        self.started = time.time()
        self._lock = threading.Lock()

    def cfg(self):
        return runner.load_cfg(self.config_path)

    def lock(self):
        return self._lock


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "qq-companion-panel/1.0"
    protocol_version = "HTTP/1.1"

    ctx: Ctx = None            # type: ignore[assignment]

    # ---- 基础工具 ----
    def log_message(self, fmt: str, *args) -> None:
        """默认实现会往 stderr 打每一行请求，太吵。只在出错时留一句。"""
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("  [panel] %s\n" % (fmt % args))

    def _send(self, code: int, body: bytes, ctype: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, _json_bytes(obj), "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    def handle_one_request(self) -> None:
        """每个请求开始前把 body 缓存清掉。

        ★ 这里必须清：`BaseHTTPRequestHandler` 的实例在**一条长连接上是复用的**
        （handle() 里 while 循环反复调 handle_one_request，self 是同一个对象）。
        如果不清，第二个请求调 _read_body_bytes() 会直接拿到第一个请求缓存的
        body 并原样返回，于是第二个请求的真 body 留在 socket 里变成下一个
        「请求行」—— 实测表现就是日志里出现 `"{"b": 2}" 400`。

        这个坑只有「同一个连接上连发两个带 body 的 POST」才会暴露，
        所以单请求测试是完全测不出来的。
        """
        self._body_cache = None
        return super().handle_one_request()

    def _read_body_bytes(self) -> bytes:
        """把请求体读出来，并且**只读一次**（缓存在本请求内）。

        必须读到最后一个字节：HTTP/1.1 是长连接，没读完的 body 会留在 socket
        里，被当成下一个请求的请求行 —— 实测表现是
        `Unsupported method ('{}GET')`（`{}` 正是上一个 POST 的 body）。
        """
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        self._body_cache = self.rfile.read(n) if n > 0 else b""
        return self._body_cache

    def _body(self) -> Dict[str, Any]:
        raw = self._read_body_bytes()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"请求体不是合法 JSON：{e}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是一个对象")
        return data

    def _query(self) -> Dict[str, str]:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items() if v}

    # ---- 开机自启 ----
    def _boot_payload(self) -> Dict[str, Any]:
        """注册表的真相 + 配置文件里的记录 + 上次自启的结果，一起给前端。"""
        from app import autostart
        ctx = self.ctx
        try:
            st = autostart.status(ctx.root)
        except Exception as e:                     # 极端情况：注册表被策略锁死
            st = {"supported": False, "enabled": False, "command": "",
                  "expected": "", "stale": False, "warn": [f"读注册表失败：{e}"],
                  "pythonw": "", "bootPy": "", "runKey": "", "valueName": ""}
        payload: Dict[str, Any] = {"ok": True, **st}
        cfg = runner.cfg_or_none(ctx.config_path)
        if cfg is not None:
            b = getattr(cfg, "boot", None)
            payload["cfgEnabled"] = bool(getattr(b, "enabled", False))
            payload["settings"] = {
                "delay": int(getattr(b, "delay", 10) or 0),
                "panel": bool(getattr(b, "panel", True)),
                "openPanel": bool(getattr(b, "open_panel", True)),
                "tts": bool(getattr(b, "tts", False)),
                "napcat": bool(getattr(b, "napcat", True)),
                "napcatQq": getattr(b, "napcat_qq", "") or "",
                "napcatPath": getattr(b, "napcat_path", "") or "",
                "headless": bool(getattr(b, "headless", True)),
            }
            payload["state"] = autostart.read_state(cfg.db_path().parent)
            # NapCat 的现状：有没有找到目录、提权任务装没装、QQ 是否在线。
            # 这几项决定「自启之后能不能真的收到消息」，所以一并给前端。
            try:
                from app import napcat
                payload["napcat"] = napcat.status(config_path=ctx.config_path)
            except Exception as e:
                payload["napcat"] = {"error": str(e)}
        return payload

    def _sync_boot_flag(self, on: bool) -> None:
        """把 boot.enabled 记进 config.yaml。

        只是「意图记录」：真正决定会不会自启的只有注册表那一行。但两处不一致
        会让用户困惑（按钮显示开着、参数页却显示关着），所以顺手对齐。
        失败也不影响自启本身，静默跳过即可。
        """
        try:
            cfg = configio.load(self.ctx.config_path)
            if configio.get_path(cfg, "boot.enabled") == on:
                return
            configio.set_path(cfg, "boot.enabled", on)
            with self.ctx.lock():
                configio.save(self.ctx.config_path, cfg)
        except Exception:
            pass

    # ---- 静态文件 ----
    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        if ".." in rel.split("/"):
            self._err("路径不合法", 400)
            return
        fp = WEB_DIR / rel
        if not fp.is_file():
            self._err(f"没有这个文件：{rel}", 404)
            return
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, fp.read_bytes(), ctype)

    # ---- 路由 ----
    def do_GET(self) -> None:            # noqa: N802
        path = urlparse(self.path).path
        try:
            self._read_body_bytes()      # 同上：保持长连接的字节流干净
            if path.startswith("/api/"):
                return self._api_get(path)
            return self._serve_static(path)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._err(f"服务端出错：{e}", 500)

    def do_HEAD(self) -> None:           # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:           # noqa: N802
        path = urlparse(self.path).path
        try:
            # 先无条件把 body 读干净（有些接口不需要 body，但 socket 里有残留
            # 会污染同一个长连接上的下一个请求）
            self._read_body_bytes()
            return self._api_post(path)
        except ValueError as e:
            self._err(str(e), 400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._err(f"服务端出错：{e}", 500)

    # ---- GET ----
    def _api_get(self, path: str) -> None:
        ctx = self.ctx
        q = self._query()

        if path == "/api/health":
            return self._json({
                "ok": True,
                "version": __import__("panel").__version__,
                "root": str(ctx.root),
                "configPath": str(ctx.config_path),
                "uptimeSeconds": round(time.time() - ctx.started, 1),
                "python": sys.executable,
            })

        if path == "/api/schema":
            return self._json({
                "groups": schema.groups_with_params(),
                "total": len(schema.ALL_PARAMS),
                "defaults": schema.defaults(),
            })

        if path == "/api/config":
            cfg = configio.load(ctx.config_path)
            issues = configio.validate(cfg, panel_port=self.server.server_port,
                                       root=ctx.root)
            return self._json({
                "values": cfg,
                "raw": configio.read_raw(ctx.config_path),
                "path": str(ctx.config_path),
                "exists": ctx.config_path.exists(),
                "mtime": ctx.config_path.stat().st_mtime if ctx.config_path.exists() else 0,
                "issues": issues,
            })

        if path == "/api/status":
            cfg = runner.cfg_or_none(ctx.config_path)
            if cfg is None:
                return self._json({"running": False, "error": "配置读不出来，先修好 config.yaml"})
            return self._json(runner.status(cfg, ctx.config_path))

        if path == "/api/stats":
            cfg = runner.cfg_or_none(ctx.config_path)
            if cfg is None:
                return self._json({"exists": False, "error": "配置读不出来"})
            try:
                days = max(1, min(90, int(q.get("days", "7"))))
            except ValueError:
                days = 7
            return self._json(runner.stats(cfg, days=days))

        # ── 开机自启 ────────────────────────────────────────────────
        # 注册表才是真相，config.yaml 里的 boot.enabled 只是同一件事的记录。
        if path == "/api/boot/status":
            return self._json(self._boot_payload())

        if path == "/api/logs":
            cfg = runner.cfg_or_none(ctx.config_path)
            if cfg is None:
                return self._json({"offset": 0, "text": "", "exists": False})
            try:
                offset = int(q.get("offset", "0"))
            except ValueError:
                offset = 0
            return self._json(runner.tail_log(cfg.log_path(), offset))

        if path == "/api/config/export":
            cfg = configio.load(ctx.config_path)
            fmt = q.get("format", "yaml").lower()
            mask = q.get("mask", "1") == "1"
            text = configio.export_text(cfg, "json" if fmt == "json" else "yaml",
                                        mask_key=mask)
            fn = "config.json" if fmt == "json" else "config.yaml"
            return self._send(
                200, text.encode("utf-8"),
                "application/json; charset=utf-8" if fmt == "json"
                else "text/yaml; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{fn}"'})

        if path == "/api/config/defaults":
            return self._json({"values": schema.defaults(),
                               "issues": configio.validate(
                                   schema.defaults(),
                                   panel_port=self.server.server_port,
                                   root=ctx.root)})

        if path == "/api/fs":
            return self._fs(ctx, q.get("path", "."))

        if path == "/api/debug/ping":
            return self._json({"pong": True})

        # ── 声音（GPT-SoVITS）───────────────────────────────────────
        # ★ 这组接口全部只读本机部署目录 + 调 127.0.0.1 的语音服务，
        #   不发任何数据到外网。所有失败的返回都是 {ok:false,error:"中文原因"}，
        #   让页面直接把原因显示出来，而不是抛一个看不清的 500。
        if path == "/api/voice/status":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            return self._json(voice.status(cfg, ctx.root))

        if path == "/api/voice/weights":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            install = Path(cfg.tts.install_dir)
            app_dir = voice.detect_app_dir(install)
            data = voice.list_weights(app_dir)
            data["ok"] = True
            data["appDir"] = str(app_dir) if app_dir else ""
            data["current"] = voice.current_weights(voice.log_path(install))
            return self._json(data)

        if path == "/api/voice/refs":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            r_dir = voice.refs_dir(Path(cfg.tts.install_dir))
            return self._json({"ok": True,
                               "refsDir": str(r_dir),
                               "refs": voice.list_refs(r_dir),
                               "emotions": voice.load_emotions(r_dir)})

        if path == "/api/voice/audio":
            p = voice.preview_file(ctx.root, q.get("f", ""))
            if p is None:
                return self._err("没有这个试听文件", 404)
            return self._send(200, p.read_bytes(), "audio/wav",
                              {"Content-Disposition": f'inline; filename="{p.name}"'})

        if path == "/api/voice/ref-audio":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            # 只认 refs 目录下的 .wav，且必须用裸文件名（Path().name 已经把目录部分剥掉）
            name = Path(q.get("f", "")).name
            p = voice.refs_dir(Path(cfg.tts.install_dir)) / name
            if not name.lower().endswith(".wav") or not p.is_file():
                return self._err("没有这个参考音频", 404)
            return self._send(200, p.read_bytes(), "audio/wav")

        if path == "/api/debug/pid":
            from app import netprobe
            raw = q.get("pid") or "0"
            try:
                pid = int(raw)
            except ValueError:
                return self._err("pid 必须是整数")
            if pid <= 0:
                cfg = runner.cfg_or_none(ctx.config_path)
                pid = int(runner.status(cfg, ctx.config_path).get("stale_pid") or 0) if cfg else 0
            return self._json(netprobe.probe_pid(pid))

        self._err(f"未知接口：{path}", 404)

    def _voice_cfg(self):
        """取配置；取不到就直接回一个能看懂的错误，返回 None 让调用方停手。"""
        cfg = runner.cfg_or_none(self.ctx.config_path)
        if cfg is None:
            self._json({"ok": False, "error": "配置读不出来，先修好 config.yaml"})
        return cfg

    def _fs(self, ctx: Ctx, rel: str) -> None:
        """目录浏览（给「人设文件」这类 path 参数当文件选择器用）。

        只允许在项目根目录里逛 —— 面板不该变成一个任意文件浏览器。
        """
        root = ctx.root
        rel = (rel or ".").strip().replace("\\", "/")
        try:
            base = (root / rel).resolve()
        except Exception:
            return self._err("路径不合法", 400)
        if root not in base.parents and base != root:
            return self._err("只能浏览项目目录里的文件", 403)
        if not base.is_dir():
            return self._err(f"不是一个目录：{rel}", 400)

        dirs, files = [], []
        try:
            for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if entry.name.startswith(".") or entry.name == "__pycache__":
                    continue
                try:
                    if entry.is_dir():
                        dirs.append(entry.name)
                    elif entry.is_file():
                        files.append({"name": entry.name,
                                      "size": entry.stat().st_size})
                except OSError:
                    continue
        except OSError as e:
            return self._err(f"读不了这个目录：{e}", 400)

        rel_out = "." if base == root else base.relative_to(root).as_posix()
        parent = None
        if base != root:
            up = base.parent
            parent = "." if up == root else up.relative_to(root).as_posix()
        return self._json({"path": rel_out, "parent": parent,
                           "dirs": dirs, "files": files,
                           "root": "."})

    # ---- POST ----
    def _api_post(self, path: str) -> None:
        ctx = self.ctx

        if path == "/api/config/validate":
            data = self._body()
            values = data.get("values") or {}
            return self._json({"issues": configio.validate(
                values, panel_port=self.server.server_port, root=ctx.root)})

        if path == "/api/config/import":
            data = self._body()
            parsed, err = configio.parse_import(str(data.get("text") or ""))
            if err:
                return self._json({"ok": False, "error": err})
            return self._json({
                "ok": True, "values": parsed,
                "issues": configio.validate(parsed, panel_port=self.server.server_port,
                                            root=ctx.root)})

        if path == "/api/config":
            data = self._body()
            values = data.get("values")
            if not isinstance(values, dict):
                return self._err("缺少 values")
            auto = bool(data.get("restart", False))
            force = bool(data.get("force", False))

            issues = configio.validate(values, panel_port=self.server.server_port,
                                       root=ctx.root)
            errors = [i for i in issues if i["level"] == "error"]
            if errors and not force:
                return self._json({
                    "ok": False,
                    "error": f"有 {len(errors)} 个参数不合法，没有保存。",
                    "issues": issues,
                }, 200)

            old = configio.load(ctx.config_path) if ctx.config_path.exists() else {}
            hot, cold = configio.classify(old, values)

            with ctx.lock():
                bak = configio.save(ctx.config_path, values)

            payload: Dict[str, Any] = {
                "ok": True,
                "saved": True,
                "backup": str(bak) if bak else None,
                "hotChanged": hot,
                "coldChanged": cold,
                "issues": issues,
                "messages": [],
            }

            if not (hot or cold):
                payload["messages"].append("没有参数发生变化，文件还是重写了一遍（使注释同步）。")
                return self._json(payload)

            cfg = runner.cfg_or_none(ctx.config_path)
            st = runner.status(cfg, ctx.config_path) if cfg else {"running": False}

            if not st.get("running"):
                payload["messages"].append("保存成功。服务当前没在跑，下次启动就是这个配置。")
                if auto:
                    r = runner.start(cfg, ctx.config_path)
                    payload["messages"].append(r["msg"])
                    payload["ok"] = r.get("ok", False)
                return self._json(payload)

            # 服务在跑：能热更新的直接推给它，必须重启的等用户决定
            if hot:
                try:
                    runner.request_reload(cfg)
                    payload["messages"].append(
                        f"{len(hot)} 个热更新参数已推给正在跑的进程，几秒内生效"
                        f"（{'、'.join(hot[:4])}{'…' if len(hot) > 4 else ''}）。")
                    payload["reloaded"] = True
                except Exception as e:
                    payload["messages"].append(f"热更新信号发失败：{e}")

            if cold:
                payload["restartRequired"] = True
                payload["messages"].append(
                    f"还有 {len(cold)} 个参数必须重启才生效："
                    f"{'、'.join(cold)}。")
                if auto:
                    r = runner.restart(cfg, ctx.config_path)
                    payload["messages"].append(r["msg"])
                    payload["ok"] = r.get("ok", False)
            elif auto:
                payload["messages"].append("全部是热更新参数，不用重启。")
            return self._json(payload)

        if path in ("/api/service/start", "/api/service/stop", "/api/service/restart"):
            cfg = runner.cfg_or_none(ctx.config_path)
            if cfg is None:
                return self._json({"ok": False, "msg": "配置读不出来，先修好 config.yaml"})
            fn = {"start": runner.start, "stop": runner.stop,
                  "restart": runner.restart}[path.rsplit("/", 1)[1]]
            r = fn(cfg, ctx.config_path)
            r["status"] = runner.status(cfg, ctx.config_path)
            return self._json(r)

        if path == "/api/logs/clear":
            cfg = runner.cfg_or_none(ctx.config_path)
            if cfg is None:
                return self._json({"ok": False, "msg": "配置读不出来"})
            return self._json(runner.clear_log(cfg.log_path()))

        if path == "/api/service/reload":
            cfg = runner.cfg_or_none(ctx.config_path)
            if cfg is None:
                return self._json({"ok": False, "msg": "配置读不出来"})
            try:
                p = runner.request_reload(cfg)
                return self._json({"ok": True, "msg": f"已通知重读热更新参数（{p}）。"})
            except Exception as e:
                return self._json({"ok": False, "msg": f"失败：{e}"})

        # ── 开机自启 ────────────────────────────────────────────────
        if path == "/api/boot":
            from app import autostart
            data = self._body()
            on = bool(data.get("on"))
            try:
                r = autostart.enable(ctx.root) if on else autostart.disable(ctx.root)
            except autostart.AutostartError as e:
                return self._json({"ok": False, "error": str(e)})
            self._sync_boot_flag(on)
            msg = (f"已开启。下次登录 Windows 会自动拉起（写在 {r['runKey']}）。"
                   if on else "已关闭。注册表里那一行已删掉，下次登录不会再自动起。")
            return self._json({"ok": True, "msg": msg, **self._boot_payload()})

        if path == "/api/boot/run":
            # 立刻按同样的顺序跑一遍（不等待延迟），用来验证「真能起来」
            from app import autostart
            cfg = runner.cfg_or_none(ctx.config_path)
            data_dir = cfg.db_path().parent if cfg else (ctx.root / "data")
            try:
                pid = autostart.spawn(
                    [str(autostart.pythonw()), str(ctx.root / "boot.py"),
                     "--now", "--config", str(ctx.config_path)],
                    cwd=ctx.root, log_path=data_dir / "boot.out")
            except Exception as e:
                return self._json({"ok": False, "error": f"拉不起来：{e}"})
            return self._json({
                "ok": True,
                "msg": f"已开始按顺序拉起（PID {pid}）。约 10 秒后看 data/boot.log。",
            })

        if path == "/api/videopush/run":
            # 排队做一次视频推送：只写标记文件，真正的活在机器人进程里跑
            # （面板是另一个进程，拿不到它的 VideoPush 对象）。
            cfg = runner.cfg_or_none(ctx.config_path)
            if cfg is None:
                return self._json({"ok": False, "error": "配置读不出来，先修好 config.yaml"})
            if not cfg.videopush.enabled:
                return self._json({"ok": False,
                                   "error": "视频推送关着（配置参数 → B 站视频推送 → 启用视频推送）"})
            if not cfg.videopush.topics:
                return self._json({"ok": False,
                                   "error": "还没填关键词（配置参数 → B 站视频推送 → 感兴趣的关键词）"})
            try:
                flag = cfg.db_path().parent / ".videopush-now"
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(str(time.time()), encoding="utf-8")
            except Exception as e:
                return self._json({"ok": False, "error": f"写标记文件失败：{e}"})
            return self._json({
                "ok": True,
                "msg": "已排队。机器人会在下一次调度检查（最多 1 分钟）里跑一轮，"
                       "进度看「实时日志」里 videopush 开头的行。",
            })

        # ── 声音（GPT-SoVITS）───────────────────────────────────────
        # ★ 一律 200 + {ok:false,error}：这些失败都是「用户能自己修」的
        #   （服务没开、refs 里没文件、文件名不对），页面要能把原因原样显示出来。
        if path == "/api/voice/preview":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            try:
                r_dir = voice.refs_dir(Path(cfg.tts.install_dir))
                ref, prompt = voice.resolve_ref(
                    r_dir,
                    str(data.get("emotion") or "").strip(),
                    str(data.get("ref") or "").strip())
                speed = float(data.get("speed") or cfg.tts.speed or 1.0)
                raw = voice.synth(cfg.tts.api_base, str(data.get("text") or ""),
                                  ref, prompt,
                                  speed=max(0.6, min(1.5, speed)),
                                  timeout=float(cfg.tts.timeout or 60.0))
                name = voice.save_preview(ctx.root, raw)
                f = voice.preview_file(ctx.root, name)
                return self._json({"ok": True, "url": f"/api/voice/audio?f={name}",
                                   "bytes": len(raw),
                                   "seconds": voice.wav_duration(f) if f else None,
                                   "ref": ref.name})
            except (voice.VoiceError, ValueError) as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/refs/upload":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            blob = data.get("data") or ""
            if isinstance(blob, str) and blob.startswith("data:"):
                blob = blob.split(",", 1)[-1]        # 去掉 data:audio/wav;base64,
            try:
                raw = base64.b64decode(blob, validate=False)
            except Exception:
                return self._json({"ok": False, "error": "音频数据不是合法的 base64"})
            try:
                return self._json(voice.save_ref(
                    voice.refs_dir(Path(cfg.tts.install_dir)),
                    str(data.get("name") or ""), raw,
                    str(data.get("text") or ""),
                    str(data.get("emotion") or "").strip()))
            except voice.VoiceError as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/refs/bind":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            try:
                return self._json(voice.bind_emotion(
                    voice.refs_dir(Path(cfg.tts.install_dir)),
                    str(data.get("emotion") or ""),
                    str(data.get("file") or "")))
            except voice.VoiceError as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/refs/unbind":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            try:
                return self._json(voice.unbind_emotion(
                    voice.refs_dir(Path(cfg.tts.install_dir)),
                    str(data.get("emotion") or "")))
            except voice.VoiceError as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/refs/delete":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            try:
                install = Path(cfg.tts.install_dir)
                return self._json(voice.delete_ref(
                    voice.refs_dir(install), voice.trash_dir(install),
                    str(data.get("file") or "")))
            except (voice.VoiceError, OSError) as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/weights/load":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            try:
                app_dir = voice.detect_app_dir(Path(cfg.tts.install_dir))
                return self._json(voice.load_weights(
                    cfg.tts.api_base, str(data.get("kind") or ""),
                    str(data.get("path") or ""), app_dir))
            except voice.VoiceError as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/use":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            try:
                root = Path(cfg.tts.install_dir)
                return self._json(voice.use_voice(
                    cfg.tts.api_base,
                    voice.detect_app_dir(root),
                    voice.refs_dir(root),
                    gpt_path=str(data.get("gpt") or ""),
                    sovits_path=str(data.get("sovits") or ""),
                    label=str(data.get("label") or "").strip(),
                ))
            except voice.VoiceError as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/service":
            cfg = self._voice_cfg()
            if cfg is None:
                return
            data = self._body()
            action = str(data.get("action") or "").strip().lower()
            try:
                if action == "start":
                    alive, _ = voice.api_alive(cfg.tts.api_base, timeout=1.5)
                    if alive:
                        return self._json({"ok": True, "msg": "服务已经在跑了"})
                    return self._json(voice.start_service(Path(cfg.tts.install_dir)))
                if action in ("stop", "restart"):
                    return self._json(voice.control(
                        cfg.tts.api_base, "exit" if action == "stop" else "restart"))
                raise voice.VoiceError("action 只能是 start / stop / restart")
            except voice.VoiceError as e:
                return self._json({"ok": False, "error": str(e)})

        if path == "/api/voice/always":
            # 「全语音模式」那个按钮。只改一个布尔值，绝不动 trigger / chance ——
            # 用户明确要求「关掉之后仍然保留原来的设置」，所以覆盖开关必须独立。
            if not ctx.config_path.exists():
                return self._json({"ok": False, "error": "还没有 config.yaml，先去配置页保存一次"})
            data = self._body()
            on = bool(data.get("on"))
            values = configio.load(ctx.config_path)
            sect = values.get("tts")
            if not isinstance(sect, dict):
                return self._json({"ok": False, "error": "config.yaml 里的 tts 段格式不对"})
            sect["always_voice"] = on
            try:
                with ctx.lock():
                    configio.save(ctx.config_path, values)
            except Exception as e:
                return self._json({"ok": False, "error": f"写 config.yaml 失败：{e}"})

            cfg = runner.cfg_or_none(ctx.config_path)
            st = runner.status(cfg, ctx.config_path) if cfg else {"running": False}
            if not st.get("running"):
                return self._json({"ok": True, "alwaysVoice": on, "reloaded": False,
                                   "msg": "已保存。服务没在跑，下次启动就是全语音模式。"})
            try:
                runner.request_reload(cfg)
            except Exception as e:
                return self._json({"ok": True, "alwaysVoice": on, "reloaded": False,
                                   "msg": f"已保存，但热更新信号发失败了：{e}（重启机器人才生效）"})

            if on:
                msg = "已开启：从现在起每条回复都会发语音条（含群聊）。"
            else:
                t = getattr(cfg, "tts", None)
                msg = (f"已关闭：回到原来的设置（trigger={getattr(t, 'trigger', 'request')}，"
                       f"概率 {float(getattr(t, 'chance', 0) or 0):.2f}）。")
            return self._json({"ok": True, "alwaysVoice": on, "reloaded": True, "msg": msg})

        self._err(f"未知接口：{path}", 404)


# ──────────────────────────────────────────────────────────────────────
#  启动
# ──────────────────────────────────────────────────────────────────────
def find_free_port(host: str, want: int, tries: int = 12) -> Tuple[int, bool]:
    """端口被占就往上试几个。返回 (端口, 是否换过)。"""
    import socket
    for i in range(tries):
        p = want + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p, (i != 0)
            except OSError:
                continue
    raise SystemExit(f"从 {want} 往上试了 {tries} 个端口都被占着，换个 --port 吧。")


def serve(config_path: Path, host: str, port: int, *, open_browser: bool = True,
          strict_port: bool = False) -> int:
    root = Path(__file__).resolve().parent.parent
    config_path = config_path if config_path.is_absolute() else (Path.cwd() / config_path)
    config_path = config_path.resolve()

    if not config_path.exists():
        print(f"× 找不到配置文件：{config_path}")
        print("  先跑一次 setup.py 生成，或者用 --config 指定别的位置。")
        return 2

    if strict_port:
        chosen = port
    else:
        chosen, moved = find_free_port(host, port)
        if moved:
            print(f"! 端口 {port} 被占着，换成了 {chosen}")

    Handler.ctx = Ctx(config_path, root)
    httpd = ThreadingHTTPServer((host, chosen), Handler)
    httpd.daemon_threads = True

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{chosen}/"
    print()
    print("  ┌──────────────────────────────────────────────────────────┐")
    print("  │  qq-companion 管理面板                                    │")
    print("  └──────────────────────────────────────────────────────────┘")
    print(f"  地址     : {url}")
    print(f"  配置     : {config_path}")
    print(f"  项目根   : {root}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  ⚠ 绑定在 {host} —— 同局域网的人也能打开这个页面，")
        print("    它能读到你的 API Key、能启停服务。不确定就别这么开。")
    print()
    print("  按 Ctrl+C 退出（退出面板不会停掉机器人本体）")
    print()

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  面板已退出。机器人本体不受影响 —— 要停它请在面板上点「停止」，")
        print("  或者双击 stop.bat。")
    finally:
        httpd.server_close()
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m panel",
        description="qq-companion 的可视化配置管理面板（纯标准库，不需要 Node）")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"监听地址（默认 {DEFAULT_HOST}，只允许本机访问）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"监听端口（默认 {DEFAULT_PORT}，被占就自动往上试）")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    ap.add_argument("--strict-port", action="store_true", help="端口被占就直接退出，不自动换")
    args = ap.parse_args(argv)

    try:
        import app  # noqa: F401  —— 拿到 UTF-8 控制台修复，否则中文/符号在 GBK 下会崩
    except Exception:
        pass

    return serve(Path(args.config), args.host, args.port,
                 open_browser=not args.no_browser, strict_port=args.strict_port)


if __name__ == "__main__":
    raise SystemExit(main())
