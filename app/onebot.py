"""OneBot 11 适配器 —— 对接 NapCat。

支持两种接法（config.onebot.mode）：
  reverse: 我们自己起 WebSocket 服务端，NapCat 作为客户端连进来（推荐，无需公网）
  forward: NapCat 起服务端，我们主动连过去

只用了 OneBot 11 里最基础、各家实现都一致的那几个接口，
不碰平台私有扩展，换 LLMOneBot/Lagrange 也能直接用。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .config import Config

log = logging.getLogger("companion.onebot")

try:  # websockets >= 13 的新 asyncio API
    from websockets.asyncio.server import serve as _ws_serve
    from websockets.asyncio.client import connect as _ws_connect
    _NEW_API = True
except Exception:  # pragma: no cover - 兼容老版本
    from websockets.server import serve as _ws_serve          # type: ignore
    from websockets.client import connect as _ws_connect      # type: ignore
    _NEW_API = False


@dataclass
class IncomingMessage:
    message_type: str                 # private / group
    user_id: int
    group_id: Optional[int]
    self_id: int
    text: str                         # 已抽出纯文本（图片替换成 [图片]）
    has_image: bool = False
    nickname: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)

    @property
    def session_id(self) -> str:
        if self.message_type == "group":
            return f"group:{self.group_id}"
        return f"private:{self.user_id}"


class OneBotAdapter:
    def __init__(self, cfg: Config,
                 on_message: Callable[[IncomingMessage], Awaitable[None]]):
        self.cfg = cfg
        self.on_message = on_message
        self._conn = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._server = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self.self_id: int = 0
        self.connected = asyncio.Event()

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        mode = (self.cfg.onebot.mode or "reverse").lower()
        if mode == "forward":
            self._task = asyncio.create_task(self._forward_loop())
        else:
            await self._start_reverse()

    async def stop(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass

    async def _start_reverse(self) -> None:
        oc = self.cfg.onebot

        async def handler(ws, path: Optional[str] = None):
            # 路径校验：只接受配好的那一条
            got_path = path
            if got_path is None:
                try:
                    got_path = ws.request.path
                except Exception:
                    got_path = oc.path
            if oc.path and got_path and not str(got_path).startswith(oc.path):
                log.warning("拒绝了来路不明的连接：%s", got_path)
                await ws.close()
                return
            await self._serve_conn(ws)

        kwargs: Dict[str, Any] = {"ping_interval": 30, "ping_timeout": 60, "max_size": 16 * 1024 * 1024}
        if _NEW_API:
            self._server = await _ws_serve(handler, oc.host, oc.port, **kwargs)
        else:
            self._server = await _ws_serve(handler, oc.host, oc.port, **kwargs)  # type: ignore
        log.info("反向 WS 已监听 ws://%s:%d%s（等 NapCat 连进来）", oc.host, oc.port, oc.path)

    async def _forward_loop(self) -> None:
        oc = self.cfg.onebot
        while not self._closing:
            try:
                async with _ws_connect(oc.url, ping_interval=30, ping_timeout=60,
                                       max_size=16 * 1024 * 1024) as ws:
                    log.info("已连上 NapCat：%s", oc.url)
                    await self._serve_conn(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("连接 NapCat 失败（%s），5 秒后重试", e)
                await asyncio.sleep(5)

    # ---------------- 连接处理 ----------------

    async def _serve_conn(self, ws) -> None:
        self._conn = ws
        self.connected.set()
        log.info("NapCat 已连接")
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                await self._dispatch(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("连接中断：%s", e)
        finally:
            self.connected.clear()
            if self._conn is ws:
                self._conn = None
            log.info("NapCat 断开")

    async def _dispatch(self, data: Dict[str, Any]) -> None:
        # 1) 是我们发出的 API 调用的回执
        echo = data.get("echo")
        if echo is not None and echo in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(data)
            return

        post_type = data.get("post_type")

        if post_type == "meta_event":
            if data.get("meta_event_type") == "lifecycle":
                info = (data.get("self") or {})
                self.self_id = int(info.get("user_id") or data.get("self_id") or 0)
                log.info("NapCat 生命周期事件，机器人 QQ=%s", self.self_id or "?")
            return

        if post_type != "message":
            return

        msg = self._parse(data)
        if msg is None:
            return
        try:
            await self.on_message(msg)
        except Exception:
            log.exception("处理消息时出错")

    def _parse(self, data: Dict[str, Any]) -> Optional[IncomingMessage]:
        mt = data.get("message_type")
        if mt not in ("private", "group"):
            return None
        self_id = int(data.get("self_id") or 0)
        if self_id:
            self.self_id = self_id
        user_id = int(data.get("user_id") or 0)
        if not user_id or user_id == self.self_id:
            return None  # 忽略自己发的

        text_parts: List[str] = []
        has_image = False
        for seg in (data.get("message") or []):
            if not isinstance(seg, dict):
                continue
            st = seg.get("type")
            sd = seg.get("data") or {}
            if st == "text":
                text_parts.append(str(sd.get("text") or ""))
            elif st == "image":
                has_image = True
                text_parts.append("[图片]")
            elif st == "face":
                text_parts.append("[表情]")
            elif st == "at":
                text_parts.append(f"@{sd.get('qq')}")
            elif st == "reply":
                continue
            elif st == "record":
                text_parts.append("[语音]")

        text = "".join(text_parts).strip()
        if not text and not has_image:
            text = str(data.get("raw_message") or "").strip()
        if not text:
            return None

        sender = data.get("sender") or {}
        return IncomingMessage(
            message_type=mt,
            user_id=user_id,
            group_id=int(data["group_id"]) if data.get("group_id") else None,
            self_id=self.self_id,
            text=text,
            has_image=has_image,
            nickname=str(sender.get("card") or sender.get("nickname") or ""),
            raw=data,
        )

    # ---------------- 发消息 ----------------

    async def call_api(self, action: str, params: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
        if self._conn is None:
            raise RuntimeError("NapCat 还没连上，发不出去")
        echo = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        payload = json.dumps({"action": action, "params": params, "echo": echo},
                             ensure_ascii=False)
        try:
            async with self._send_lock:
                await self._conn.send(payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)

    async def send_text(self, msg: IncomingMessage, text: str) -> None:
        if msg.message_type == "group" and msg.group_id:
            await self.call_api("send_group_msg", {"group_id": msg.group_id, "message": text})
        else:
            await self.call_api("send_private_msg", {"user_id": msg.user_id, "message": text})

    async def send_record(self, msg: IncomingMessage, audio: bytes, *,
                          ext: str = "mp3", timeout: float = 60.0) -> None:
        """发一条语音。audio 是文件原始字节，内部转 base64 传过去。

        OneBot 11 的 `record` 段各实现都认 `base64://`，不用先落盘再传路径
        （NapCat 的 file:// 只认它自己能访问到的路径，跨机器配置很容易踩坑）。
        不带文件名：base64 开头有 RIFF/ID3 魔数，平台自己认得出来，多塞字段
        反而可能被严格实现拒掉。
        """
        if not audio:
            raise RuntimeError("语音是空的")
        seg = [{
            "type": "record",
            "data": {"file": "base64://" + base64.b64encode(audio).decode("ascii")},
        }]
        if msg.message_type == "group" and msg.group_id:
            resp = await self.call_api("send_group_msg",
                                       {"group_id": msg.group_id, "message": seg},
                                       timeout=timeout)
        else:
            resp = await self.call_api("send_private_msg",
                                       {"user_id": msg.user_id, "message": seg},
                                       timeout=timeout)

        # retcode 非 0 说明平台拒了（格式不支持 / 太长 / 风控），抛出去让上层降级
        rc = resp.get("retcode", 0) if isinstance(resp, dict) else 0
        if rc not in (0, None):
            raise RuntimeError(f"发语音被拒（retcode={rc}）：{resp.get('message') or resp}")
