"""假 NapCat 客户端 —— 不用真 QQ 号就能端到端验证机器人。

它连上机器人开的反向 WebSocket，冒充 NapCat 发事件、收动作，
并把每次回复的**到达时刻**打印出来，这样"像不像人"是肉眼可验证的：

    # 终端 1
    python main.py
    # 终端 2
    python tools/mock_napcat.py
    > 在吗
    > 今天好累啊

参数：
    --url      机器人反向 WS 地址，默认 ws://127.0.0.1:3001/onebot/v11/ws
    --script   不给就进交互模式；给了就按行读完退出（用于自动化测试）
    --gap      脚本模式下两条消息之间的间隔秒数，默认 1.5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: F401,E402  —— 触发 app/__init__.py 里的 UTF-8 输出修复

try:
    from websockets.asyncio.client import connect
except ImportError:  # 老版本 websockets
    from websockets.client import connect  # type: ignore

SELF_ID = 10001
PEER_ID = 20002
GROUP_ID = 30003

T0 = time.perf_counter()


def stamp() -> str:
    return f"[{time.perf_counter() - T0:7.2f}s]"


def event_private(text: str) -> str:
    return json.dumps({
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": int(time.time() * 1000) % 100000,
        "user_id": PEER_ID,
        "self_id": SELF_ID,
        "time": int(time.time()),
        "sender": {"user_id": PEER_ID, "nickname": "测试君"},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }, ensure_ascii=False)


def event_group(text: str, *, gid: int = GROUP_ID, uid: int = PEER_ID,
                nick: str = "群友", at_bot: bool = True) -> str:
    """群聊事件。

    at_bot=True 时把正文前面塞一个 at 段 —— 这是群里唯一会被理的情况
    （whitelist.group_reply_rate 默认 0，没被点名就当没看见）。
    """
    segs: list = []
    if at_bot:
        segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
        segs.append({"type": "text", "data": {"text": " "}})
    segs.append({"type": "text", "data": {"text": text}})
    raw = (f"[CQ:at,qq={SELF_ID}] {text}" if at_bot else text)
    return json.dumps({
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": int(time.time() * 1000) % 100000,
        "user_id": uid,
        "group_id": gid,
        "self_id": SELF_ID,
        "time": int(time.time()),
        "sender": {"user_id": uid, "nickname": nick, "card": nick},
        "message": segs,
        "raw_message": raw,
    }, ensure_ascii=False)


def event_lifecycle() -> str:
    return json.dumps({
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
        "self_id": SELF_ID,
        "time": int(time.time()),
    }, ensure_ascii=False)


async def pump(ws, stats: dict) -> None:
    """收机器人的调用，把发消息的动作打印出来。"""
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            action = data.get("action", "")
            params = data.get("params") or {}
            echo = data.get("echo")

            if action in ("send_private_msg", "send_group_msg"):
                segs = params.get("message")
                if isinstance(segs, list):
                    text = "".join(s.get("data", {}).get("text", "")
                                   for s in segs if s.get("type") == "text")
                else:
                    text = str(segs)
                stats["replies"].append(time.perf_counter() - T0)
                print(f"{stamp()}  ← 机器人: {text}")
            elif action == "get_login_info":
                print(f"{stamp()}  · 机器人要身份信息")
            elif action not in ("send_msg",):
                print(f"{stamp()}  · 调用 {action} {params}")

            if echo is not None:
                await ws.send(json.dumps(
                    {"status": "ok", "retcode": 0, "data": {"message_id": 1, "user_id": SELF_ID},
                     "echo": echo}, ensure_ascii=False))
    except Exception:
        pass


async def report_rhythm(stats: dict) -> None:
    """把收到的回复间隔算出来，直接对照拟人化参数看合不合理。"""
    times = stats.get("replies") or []
    if len(times) < 2:
        return
    gaps = [b - a for a, b in zip(times, times[1:])]
    print("\n  ── 节奏复盘 ──────────────────────────")
    print(f"  收到 {len(times)} 条，间隔："
          + "、".join(f"{g:.2f}s" for g in gaps))
    print(f"  平均 {sum(gaps) / len(gaps):.2f}s")
    print("  间隔应该落在 typing_min(1.2s) + segment_gap(0.6~2.2s) 附近；")
    print("  如果几乎是 0，说明拟人化被绕过了，或者机器人在一条里发完。")


async def main_async(args) -> int:
    stats: dict = {"replies": []}

    print(f"连接 {args.url} …")
    try:
        ws = await connect(args.url, additional_headers={"X-Self-ID": str(SELF_ID)})
    except TypeError:
        ws = await connect(args.url, extra_headers={"X-Self-ID": str(SELF_ID)})
    except Exception as e:
        print(f"连不上：{e}")
        print("检查 main.py 起了没、onebot.mode 是不是 reverse、端口对不对。")
        return 1

    async with ws:
        print("已连接，冒充 QQ 号", PEER_ID, "\n")
        await ws.send(event_lifecycle())
        asyncio.create_task(pump(ws, stats))
        await asyncio.sleep(0.3)

        if args.script:
            lines = [ln.strip() for ln in
                     Path(args.script).read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
            for i, line in enumerate(lines):
                # 群聊模式：允许用「昵称|内容」指定说话的人，不写就用 --nick
                nick, _, body = line.partition("|")
                if not body:
                    nick, body = args.nick, line
                if args.group:
                    who = f"{nick}（{body[:14]}）"
                    print(f"{stamp()}  → 群里的 {who}" +
                          ("" if not args.no_at else "   [没 @ 它]"))
                    await ws.send(event_group(body, gid=args.group, nick=nick,
                                              uid=PEER_ID + i, at_bot=not args.no_at))
                else:
                    print(f"{stamp()}  → 我: {body}")
                    await ws.send(event_private(body))
                if i < len(lines) - 1:
                    await asyncio.sleep(args.gap)
            # 等机器人把话说完（分条 + 打字延迟，给足时间）
            await asyncio.sleep(args.wait)
            await report_rhythm(stats)
            return 0

        print("输入消息回车发送，Ctrl+C 退出。\n")
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, input, f"{stamp()}  → 我: ")
            except (EOFError, KeyboardInterrupt):
                break
            if not line.strip():
                continue
            if args.group:
                await ws.send(event_group(line.strip(), gid=args.group,
                                          at_bot=not args.no_at))
            else:
                await ws.send(event_private(line.strip()))

        await report_rhythm(stats)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="假 NapCat 客户端，离线验证机器人")
    ap.add_argument("--url", default="ws://127.0.0.1:3001/onebot/v11/ws")
    ap.add_argument("--script", default=None, help="按行读取的文本文件，读完退出")
    ap.add_argument("--gap", type=float, default=1.5, help="脚本模式两条消息间隔，默认 1.5s")
    ap.add_argument("--wait", type=float, default=20.0, help="脚本末尾等回复的秒数，默认 20s")
    ap.add_argument("--group", type=int, default=None,
                    help="群号。给了就发群消息（默认每条都 @ 机器人）")
    ap.add_argument("--no-at", action="store_true",
                    help="群聊模式下不 @ 机器人 —— 用来验证「没点名就不理」")
    ap.add_argument("--nick", default="群友", help="群聊模式下默认的说话人昵称")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
