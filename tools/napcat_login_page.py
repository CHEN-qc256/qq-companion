"""扫码登录页：浏览器里永远是「当前有效」的二维码，不用反复找人重发。

为什么要有这个：

    NapCat 的二维码只有约 2 分钟寿命，过期后必须调 `/QQLogin/RefreshQRcode`
    换一张。手动流程是「截图 → 发人 → 等人扫」，中间任何延迟都会白等一轮。
    这个页把自己刷新掉：图片每 5 秒重新拉一次，服务端只在
    「码超过 100 秒且还没被扫」时才去换新码，所以不会把正在扫的码顶掉。

用法：

    python tools/napcat_login_page.py [--port 8901]

    打开 http://127.0.0.1:8901/ 就行。页面上会显示当前阶段：
    waiting_qrcode（等扫）/ qrcode_scanned（已扫，等确认）/
    initializing（确认了，正在初始化）/ ready（登录完成）。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import napcat_login as nl  # noqa: E402

_LOCK = threading.Lock()
_STATE = {"url": "", "at": 0.0, "png": b"", "phase": "", "error": ""}
SERVE_MAX_AGE = 100.0     # 码超过这个岁数就换新的（NapCat 里大约 120 秒失效）
POLL = 4.0

PHASE_TEXT = {
    "waiting_qrcode": "等待扫码（这个码是最新的，直接扫）",
    "qrcode_scanned": "已扫码，请在手机上点「确认登录」",
    "initializing": "已确认，正在初始化 QQ…",
    "ready": "登录完成",
    "offline": "已掉线",
    "reconnecting": "正在重连",
    "generating_qrcode": "正在生成二维码…",
}


def _refresh() -> None:
    """换一张新码，并把 PNG 画好放进内存。"""
    with _LOCK:
        try:
            nl.refresh_qrcode()
            time.sleep(0.8)
            st = nl.login_state()
            url = st.get("qrcodeurl") or ""
            _STATE.update(phase=st.get("loginPhase") or "", error="")
            if not url:
                _STATE["error"] = "NapCat 没给二维码：" + json.dumps(st, ensure_ascii=False)
                return
            buf = io.BytesIO()
            import qrcode

            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                               box_size=13, border=3)
            qr.add_data(url)
            qr.make(fit=True)
            qr.make_image(fill_color="black", back_color="white").convert("RGB").save(
                buf, format="PNG")
            _STATE.update(url=url, at=time.time(), png=buf.getvalue())
        except Exception as exc:  # 网络抖动/限流都不该让页面崩
            _STATE["error"] = f"{type(exc).__name__}: {exc}"


def _ensure_fresh() -> None:
    """只在码老了、且还没被扫的时候换码。已被扫的码换掉会打断确认。"""
    stale = time.time() - float(_STATE["at"]) > SERVE_MAX_AGE
    if _STATE["png"] and not stale:
        return
    try:
        phase = nl.login_state().get("loginPhase") or ""
    except Exception:
        phase = ""
    _STATE["phase"] = phase
    if phase in ("waiting_qrcode", "generating_qrcode", "") and (
            stale or not _STATE["png"]):
        _refresh()


def _watch() -> None:
    """后台轮询：既更新阶段文字，也顺便在没人看页面时保温。"""
    while True:
        try:
            st = nl.login_state()
            with _LOCK:
                _STATE["phase"] = st.get("loginPhase") or ""
                _STATE["error"] = ""
            if st.get("isLogin"):
                # 登录完成就别再刷新码了
                pass
            elif _STATE["phase"] == "waiting_qrcode" and (
                    time.time() - float(_STATE["at"]) > SERVE_MAX_AGE):
                _refresh()
        except Exception:
            pass
        time.sleep(POLL)


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>QQ 扫码登录</title>
<style>
  body {{ margin:0; font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:#0f1115; color:#e8eaed; display:flex; flex-direction:column;
         align-items:center; justify-content:center; min-height:100vh; }}
  .card {{ background:#fff; padding:18px; border-radius:16px;
           box-shadow:0 10px 40px rgba(0,0,0,.5); }}
  img {{ display:block; width:min(62vw,420px); height:auto; image-rendering:pixelated; }}
  h1 {{ font-size:17px; font-weight:600; margin:0 0 6px; }}
  .phase {{ color:#7ee787; font-size:14px; margin:14px 0 2px; }}
  .hint {{ color:#9aa4b2; font-size:13px; }}
  .err {{ color:#ff7b72; font-size:12px; max-width:520px; text-align:center;
          margin-top:10px; word-break:break-all; }}
</style></head>
<body>
  <h1>扫码登录 QQ（机器人号）</h1>
  <div class="hint">图片每 5 秒自动更新，永远是当前有效的码</div>
  <div class="card"><img id="qr" src="/qr.png?t={ts}" alt="二维码"></div>
  <div class="phase" id="phase">{phase}</div>
  <div class="hint">扫完记得在手机上点「确认登录」，否则会一直停在等待确认</div>
  <div class="err">{err}</div>
<script>
  let last = "{ts}";
  async function tick() {{
    try {{
      const r = await fetch('/state', {{cache:'no-store'}});
      const s = await r.json();
      document.getElementById('phase').textContent = s.phase_text;
      if (s.version !== last) {{
        last = s.version;
        document.getElementById('qr').src = '/qr.png?t=' + last;
      }}
    }} catch (e) {{}}
  }}
  setInterval(tick, 5000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/":
            _ensure_fresh()
            html = PAGE.format(
                ts=int(_STATE["at"]),
                phase=PHASE_TEXT.get(_STATE["phase"], _STATE["phase"] or "…"),
                err=_STATE["error"],
            ).encode()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/qr.png":
            _ensure_fresh()
            if not _STATE["png"]:
                self._send(503, b"no qrcode", "text/plain")
                return
            self._send(200, _STATE["png"], "image/png")
            return
        if path == "/state":
            _ensure_fresh()
            phase = _STATE["phase"]
            body = json.dumps({
                "version": str(int(_STATE["at"])),
                "phase": phase,
                "phase_text": PHASE_TEXT.get(phase, phase or "…"),
            }, ensure_ascii=False).encode()
            self._send(200, body, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    args = ap.parse_args()

    _refresh()
    threading.Thread(target=_watch, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"扫码页: http://127.0.0.1:{args.port}/  (Ctrl+C 退出)", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
