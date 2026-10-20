"""启动入口。

    python main.py                 # 正常跑
    python main.py --check         # 只体检配置，不连 NapCat
    python main.py --config x.yaml

第一次用请先跑 `python setup.py`，它会一路问你问题然后把配置写好。
连不上 NapCat 就跑 `python doctor.py`。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from app import netprobe
from app.budget import Budget
from app.config import Config, load_config, ROOT as ROOT_PATH
from app.context import ContextBuilder, SPEAKING_RULES, est_tokens
from app.life import Life
from app.llm import DeepSeek
from app.memory import Memory
from app.onebot import OneBotAdapter
from app.orchestrator import Orchestrator
from app.proactive import Proactive
from app.videopush import VideoPush
from app.store import Store

log = logging.getLogger("companion")

_STATE_FROM_DB = "SELECT COUNT(DISTINCT session_id) AS n FROM messages"


def pid_file(cfg: Config) -> Path:
    """把进程号写在数据库旁边。stop.bat 靠它精确停掉自己，不误杀别的 python。

    文件名跟数据库走（companion.db → companion.pid，group_e2e.db → group_e2e.pid），
    不是写死一个 companion.pid：几份配置共用 data/ 目录时（比如拿
    tests/config.group.yaml 另起一个测试实例），写死会互相挡着起不来。
    """
    return cfg.db_path().with_suffix(".pid")


def read_pid(cfg: Config) -> int:
    p = pid_file(cfg)
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def already_running(cfg: Config) -> int:
    """已经在跑的自己的进程号，没有就返回 0。"""
    pid = read_pid(cfg)
    if pid and pid != os.getpid() and netprobe.pid_alive(pid):
        return pid
    return 0


def setup_logging(cfg: Config) -> None:
    cfg.log_path().parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(cfg.log_path(), encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=getattr(logging, (cfg.logging.level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # websockets.server 的 ERROR 必须压掉：任何一次「只连一下看看端口通不通」的探测
    # （管理面板 /api/status 每隔几秒就调 netprobe.tcp_open，doctor.py 和启动体检也调）
    # 都会在它这里留下一整段 opening handshake failed 的堆栈。实测一晚上刷了 2900 多行，
    # 把真正的日志埋了。握手失败对机器人没有任何可操作价值 —— 「NapCat 有没有连上」
    # 由 companion.onebot 自己打「NapCat 已连接 / 断开」，那条才是要看的。
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    logging.getLogger("websockets.http11").setLevel(logging.CRITICAL)


def preflight(cfg: Config) -> int:
    """启动前体检。它挡住的每一个问题，都是以后半夜排查两小时的坑。"""
    ok = True
    print("── 配置体检 ──────────────────────────────")

    print(f"  模型            : {cfg.llm.model}")
    if cfg.llm.thinking:
        print("  ✗ 思考模式是开着的！思维链按输出价（4~8元/M）计费，")
        print("    陪聊每轮白烧几百 token。请在 config.yaml 里设 llm.thinking: false")
        ok = False
    else:
        print("  思考模式        : 关闭 ✓（这是最大的省钱开关）")

    print(f"  输出上限        : {cfg.llm.max_tokens} tokens")
    if cfg.llm.max_tokens > 400:
        print("  ! 输出上限偏高。输出比缓存未命中输入还贵，陪聊 200 足够")

    if not cfg.llm.api_key and not cfg.llm.mock:
        print("  ✗ 没配 api_key。填 config.yaml 的 llm.api_key，"
              "或设环境变量 DEEPSEEK_API_KEY")
        ok = False
    else:
        masked = (cfg.llm.api_key[:6] + "…") if cfg.llm.api_key else "(mock 模式，不需要)"
        print(f"  API Key         : {masked}")

    if not cfg.pricing.currency_per_million:
        print("  ! 单价表是空的，成本统计会全是 0")
    else:
        band = "高峰" if cfg.pricing.is_peak() else "空闲"
        t = cfg.pricing.currency_per_million.get(cfg.llm.model)
        if t:
            k = "peak" if band == "高峰" else "offpeak"
            print(f"  当前时段        : {band}"
                  f"（命中 {t['cache_hit'][k]}元/M，"
                  f"未命中 {t['cache_miss'][k]}元/M，"
                  f"输出 {t['output'][k]}元/M）")
            ratio = t["cache_miss"]["peak"] / max(t["cache_hit"]["peak"], 1e-9)
            print(f"  缓存价差        : 未命中是命中的 {ratio:.0f} 倍 ← 全部优化都围绕这个数")
        else:
            print(f"  ! 单价表里没有模型 {cfg.llm.model}，成本会按 0 计")

    print(f"  人设文件        : {cfg.persona_path()} "
          f"({est_tokens(cfg.persona_text)} tok, {len(cfg.persona_text)} 字)")
    if est_tokens(cfg.persona_text) > 1200:
        print("  ! 人设太长。它每轮都要发，虽然走缓存价，但还是精简点好")
    print(f"  说话规矩        : {est_tokens(SPEAKING_RULES)} tok（永久缓存，成本≈0）")
    print(f"  历史预算        : {cfg.context.history_tokens} tok → 超了就压成摘要")
    print(f"  接入方式        : " + (f"{cfg.onebot.mode} "
          f"({cfg.onebot.host}:{cfg.onebot.port}{cfg.onebot.path})"
          if cfg.onebot.mode == "reverse" else f"forward {cfg.onebot.url}"))
    print(f"  日预算          : {cfg.budget.daily_yuan} 元（超过转省电，"
          f"{cfg.budget.daily_yuan * cfg.budget.hard_stop_multiplier:.1f} 元硬停）")

    # ---- P2/P3/P4 相关 ----
    life = Life(cfg)
    m = cfg.memory
    print(f"  作息            : {life.describe()}"
          if cfg.life.enabled else "  作息            : 关闭（全天候在线）")
    if cfg.life.enabled:
        print(f"  上班时段        : {cfg.life.working}"
              + ("（仅工作日）" if cfg.life.weekday_only_work else ""))
    print(f"  长期记忆        : " + (f"开（每 {m.extract_every_turns} 轮抽一次，"
          f"本地检索 0 token）" if m.enabled else "关"))
    p = cfg.life.proactive
    if p.enabled and cfg.life.enabled:
        print(f"  主动搭话        : {int(p.active_hours[0])}~{int(p.active_hours[1])} 点，"
              f"静默超过 {p.min_idle_hours:.0f} 小时、每天最多 {p.max_per_day} 次")
    else:
        print("  主动搭话        : 关（它不会自己找你说话）")

    g = cfg.group
    if not g.enabled:
        print("  群聊            : 关（完全不理群消息）")
    else:
        scope = (f"只在 {cfg.whitelist.groups} 里活动" if cfg.whitelist.groups
                 else "任何群都理")
        mode = ("被 @ 或被叫名字才回" if g.address_required
                else f"随机插话（{cfg.whitelist.group_reply_rate:.0%}）")
        print(f"  群聊            : {scope}，{mode}")
        print(f"  群内限流        : 每人冷却 {g.per_user_cooldown_seconds:.0f}s、"
              f"整群冷却 {g.reply_cooldown_seconds:.0f}s、每小时最多 {g.max_replies_per_hour} 条")
        if not cfg.whitelist.groups:
            print("                    ↑ whitelist.groups 是空的，任何群都理。"
                  "第一次收到某个群的消息时日志会打群号，填进去就收窄了")

    # ---- 谁能说话 / 谁是主人 ----
    if cfg.whitelist.masters:
        print(f"  主人            : {cfg.whitelist.masters}"
              "（只有这几个被当主人，其他人私聊算朋友）")
    elif cfg.whitelist.private:
        print("  主人            : 没设（whitelist.masters 为空）"
              "⇒ 能私聊它的都当主人。想区分就填你自己的 QQ 号")
    if cfg.llm.web_search:
        print(f"  联网搜索        : 开（最多 {cfg.llm.max_tool_rounds} 轮，"
              "模型自己决定搜不搜；闲聊不触发就不花钱）")
    else:
        print("  联网搜索        : 关（它只会用训练时的知识回答）")

    t = cfg.tts
    if not t.enabled:
        print("  语音            : 关（只会打字；面板的「声音」页签仍可试听和克隆）")
    elif getattr(t, "always_voice", False):
        print("  语音            : 开 · 全语音模式（每条回复都发，群里也发；"
              "面板「声音」页的按钮可以关掉）")
    elif (t.trigger or "request").lower() == "auto":
        print(f"  语音            : 开（{int(round(t.chance * 100))}% 的回复会变成语音"
              + ("，群里也发" if t.group else "，群里只在被点名时要") + "）")
    else:
        print("  语音            : 开（只在你明确要的时候发，"
              "例如「发条语音」）")
    if t.enabled and not (Path(t.install_dir) / "refs" / "default.wav").is_file():
        print("                    ⚠ refs 里没有 default.wav，发语音会一直降级成文字")

    # ---- B 站视频推送 ----
    vp = cfg.videopush
    from app.videopush import parse_topics
    topics = parse_topics(vp.topics)
    if not vp.enabled:
        print("  视频推送        : 关（不会去 B 站搜视频）")
    elif not topics:
        print("  视频推送        : ⚠ 开着但没配关键词（videopush.topics），什么都不会发生")
    else:
        who = vp.targets or cfg.whitelist.masters
        print(f"  视频推送        : {'、'.join(t.keyword for t in topics)}"
              f"　每天 {'/'.join(vp.times)}，最多 {vp.max_per_day} 条"
              f"　→ {who or '⚠ 没有收件人'}")
        print(f"                    筛选：思考模式{'开' if vp.thinking else '关'}，"
              f"评分 ≥{vp.min_score}，候选 {vp.candidates} 条/关键词")
        print(f"                    BiliNote：{vp.bilinote_url}"
              + ("（没起会自动拉起来）" if vp.bilinote_autostart else "（要求手动启动）"))

    # 开机自启：真相在注册表，不在 config.yaml
    try:
        from app import autostart
        b = autostart.status()
        if not b["supported"]:
            print("  开机自启        : 不支持（只有 Windows 有）")
        elif not b["enabled"]:
            print("  开机自启        : 关（登录后要手动开；面板顶栏可一键打开）")
        elif b["stale"]:
            print("  开机自启        : ⚠ 注册表里的路径不是当前目录，点一次面板上的按钮会重写")
        else:
            print(f"  开机自启        : 开（登录时触发，{b['runKey']}\\{b['valueName']}）")
            print(f"                    命令：{b['command']}")
        for w in b["warn"]:
            print(f"                    ⚠ {w}")
    except Exception as e:
        print(f"  开机自启        : 读不出来（{e}）")

    # NapCat：自启能不能真的收到消息，取决于它。注入 QQ 要管理员权限，
    # 所以看的是「提权计划任务装了没」，而不是「bat 填了没」。
    try:
        from app import napcat
        ns = napcat.status(config_path=Path("config.yaml"))
        if not ns["shell"]:
            print("  NapCat          : × 没找到 NapCat.Shell（自启时不会拉起 QQ）")
        elif ns["task"]:
            print(f"  NapCat          : 已装提权任务（{ns['taskName']}），自启时零 UAC 拉起")
        else:
            print("  NapCat          : ⚠ 没装提权任务，自启时拉不起来 —— 双击 install_napcat_task.bat")
        print(f"                    QQ 进程：{'在跑' if ns['qqRunning'] else '没在跑'}"
              f"　快速登录：{ns['uin'] or '（没配，需要扫码）'}")
        if ns["connected"] is not None:
            print(f"                    QQ 连接：{'已连上' if ns['connected'] else '⚠ 还没连上（发消息不会有回复）'}")
    except Exception as e:
        print(f"  NapCat          : 读不出来（{e}）")

    # ---- 数据现状（有库才看） ----
    try:
        if cfg.db_path().exists():
            st = Store(cfg.db_path())
            try:
                n_sess = int(st.query(_STATE_FROM_DB)[0]["n"] or 0)
                n_mem = int(st.query("SELECT COUNT(*) AS n FROM facts")[0]["n"] or 0)
                bound = st.query("SELECT uid, nickname FROM allowlist")
            finally:
                st.close()
            print(f"  已经聊过        : {n_sess} 个会话，长期记忆 {n_mem} 条")
            if bound:
                who = ", ".join(f"{r['nickname'] or '?'}({r['uid']})" for r in bound)
                print(f"  已绑定的人      : {who}")
            else:
                print("  已绑定的人      : （还没人私聊过）")
        else:
            print("  数据库          : 还没建，第一次启动时创建")
    except Exception as e:
        print(f"  ! 读数据库失败：{e}")

    print(f"  数据库          : {cfg.db_path()}")
    run_pid = already_running(cfg)
    print(f"  运行状态        : " + (f"已经在跑（PID {run_pid}）"
          if run_pid else "没在跑"))
    print("──────────────────────────────────────────")
    print("  体检通过 ✓" if ok else "  有阻断项 ✗，先解决上面标 ✗ 的")
    return 0 if ok else 1


async def run(cfg: Config) -> None:
    pid_path = pid_file(cfg)
    running = already_running(cfg)
    if running:
        log.error("已经有一个 qq-companion 在跑了（PID %d）。", running)
        log.error("要重启就先跑 stop.bat，或者：taskkill /PID %d /F", running)
        return

    store = Store(cfg.db_path())
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    llm = DeepSeek(cfg, store)
    ctx = ContextBuilder(cfg, store, llm)
    budget = Budget(cfg, store)
    life = Life(cfg)
    memory = Memory(cfg, store, llm)

    orchestrator: Orchestrator

    async def on_message(msg):
        await orchestrator.handle(msg)

    adapter = OneBotAdapter(cfg, on_message)
    orchestrator = Orchestrator(cfg, store, llm, ctx, budget, adapter, life, memory)
    proactive = Proactive(cfg, store, llm, ctx, budget, adapter, life, memory)
    videopush = VideoPush(cfg, store, llm, adapter)

    try:
        await adapter.start()
    except OSError as e:
        pid_path.unlink(missing_ok=True)
        if getattr(e, "errno", None) in (48, 98, 10048) or "10048" in str(e):
            who = netprobe.port_owner(int(cfg.onebot.port))
            log.error("端口 %s 已被占用%s —— 多半是上一个 main.py 还没退干净。",
                      cfg.onebot.port, f"（PID {who}）" if who else "")
            if who:
                log.error("停掉它：taskkill /PID %d /F", who)
            log.error("或者改 config.yaml 里 onebot.port 换个端口。")
            store.close()
            return
        store.close()
        raise

    log.info("qq-companion 已启动。人设=%s，模型=%s，思考模式=%s，作息=%s",
             cfg.persona.name, cfg.llm.model,
             "开" if cfg.llm.thinking else "关", life.describe())
    if cfg.onebot.mode == "reverse":
        log.info("在 %s:%s%s 等 NapCat 连过来。",
                 cfg.onebot.host, cfg.onebot.port, cfg.onebot.path)
    else:
        log.info("正在连 NapCat：%s", cfg.onebot.url)
    if cfg.whitelist.masters:
        # 已经明确写了谁是主人，再说「第一个私聊的会被认成主人」就是误导了。
        log.info("主人已设定：%s（whitelist.masters）。其他人私聊会被当成朋友，不会被叫主人、"
                 "也听不到你和它私下聊的事。", "、".join(str(u) for u in cfg.whitelist.masters))
    else:
        log.info("怎么用：拿你的主号给这个 QQ 小号发任意一条消息，第一个私聊它的人会被自动认成主人。")

    proactive_task = asyncio.create_task(proactive.run_forever())
    videopush_task = asyncio.create_task(videopush.run_forever())
    reload_task = asyncio.create_task(_watch_reload(cfg, log))

    try:
        await asyncio.Event().wait()
    finally:
        proactive.stop()
        proactive_task.cancel()
        videopush_task.cancel()
        reload_task.cancel()
        await adapter.stop()
        await videopush.aclose()
        await llm.aclose()
        store.close()
        pid_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────
#  配置热更新
# ──────────────────────────────────────────────────────────────────────
async def _watch_reload(cfg: Config, log: logging.Logger) -> None:
    """盯着管理面板留下的信号文件，把可热更新的配置就地换掉。

    为什么要有这个：`config.yaml` 只在启动时读一次。面板作为**另一个进程**
    改完文件，运行中的进程并不知道。所以约定一个信号文件
    （`data/.panel-reload`），面板保存时写一下时间戳，这里每 4 秒看一眼。

    换掉的方式是 `apply_hot(cfg, fresh)` —— 就地把新值写进**当前这个 cfg
    对象**。Orchestrator / Memory / Life / Budget / ContextBuilder 全都持有
    同一个 cfg 引用，且都是每次用到时现读 `self.cfg.xxx`，所以改完立刻生效。

    哪些能换、哪些不能，定义在 app/config.py 的 HOT_EXCLUDE 里 —— 那份名单
    是单一真源，面板上的「需重启」徽章读的也是它。
    """
    from app.config import apply_hot, load_config

    src = Path(cfg.source_path) if cfg.source_path else (ROOT_PATH / "config.yaml")
    flag = cfg.db_path().parent / ".panel-reload"
    try:
        seen = flag.stat().st_mtime
    except OSError:
        seen = 0.0

    while True:
        await asyncio.sleep(4.0)
        try:
            stamp = flag.stat().st_mtime
        except OSError:
            continue
        if stamp <= seen:
            continue
        seen = stamp

        try:
            fresh = load_config(src)
        except Exception as e:
            log.warning("热更新：新配置读不出来（%s），这次跳过，用旧配置继续跑。", e)
            continue
        try:
            changed = apply_hot(cfg, fresh)
        except Exception as e:
            log.warning("热更新失败（%s），配置没动。", e)
            continue

        if not changed:
            log.info("热更新：收到信号，但没有可热更新的字段发生变化。")
            continue
        log.info("配置热更新：%d 项已生效 —— %s", len(changed),
                 "、".join(changed[:6]) + ("…" if len(changed) > 6 else ""))
        if any(c.startswith("persona.") for c in changed):
            log.info("人设改了：历史前缀对不上，下一轮缓存会整体失效重建（只是这一次贵一点）。")
        if any(c in ("onebot.port", "onebot.mode", "database.path") for c in changed):
            log.warning("注意：%s 属于需重启项，这次改动**没有**生效，重启后才算数。",
                        "、".join(c for c in changed
                                  if c in ("onebot.port", "onebot.mode", "database.path")))


def stop_running(cfg: Config) -> int:
    """停掉 pidfile 里记着的那个实例。只认自己写下的 PID，不去按进程名乱杀。"""
    import subprocess

    path = pid_file(cfg)
    pid = read_pid(cfg)
    if not pid:
        print("没有正在跑的实例（找不到 data/companion.pid）。")
        return 0
    if not netprobe.pid_alive(pid):
        print(f"PID {pid} 早就不在了，清一下残留的 pidfile。")
        path.unlink(missing_ok=True)
        return 0

    print(f"正在停 PID {pid} …")
    if sys.platform == "win32":
        rc = subprocess.run(["taskkill", "/PID", str(pid), "/F"]).returncode
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
            rc = 0
        except OSError as e:
            print(f"停不掉：{e}")
            rc = 1
    path.unlink(missing_ok=True)
    if rc == 0:
        print("已停止。")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="把 AI 接进 QQ，像真人一样说话")
    ap.add_argument("--config", default=None, help="配置文件路径，默认 config.yaml")
    ap.add_argument("--check", action="store_true", help="只体检配置，不连 NapCat")
    ap.add_argument("--stop", action="store_true", help="停掉正在跑的实例")
    args = ap.parse_args()

    try:
        cfg = load_config(Path(args.config) if args.config else None)
    except FileNotFoundError as e:
        print(e)
        return 2

    if args.stop:
        return stop_running(cfg)

    setup_logging(cfg)

    if args.check:
        return preflight(cfg)

    rc = preflight(cfg)
    if rc != 0:
        print("\n先修好上面的问题再启动。")
        return rc

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
