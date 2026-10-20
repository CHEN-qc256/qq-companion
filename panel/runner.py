"""面板和机器人本体之间的那层桥：状态 / 启停 / 日志 / 花费。

设计上刻意**不重复实现**已经有的东西：
  - 进程号怎么找：复用 main.py 的 pid_file / read_pid / stop_running
  - 端口是否在听：复用 app/netprobe.py
  - 花费怎么算：直接只读那个 sqlite（不重算价格，读的是记好的 cost_yuan）

面板是**另一个进程**，跟机器人本体没有共享内存。所以「热更新」只能靠一个
约定：面板往 data/.panel-reload 写一个时间戳，机器人那边的后台任务每几秒
stat 一次这个文件，发现变了就重读 config.yaml 并把可热更新的字段就地换掉。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────
#  配置
# ──────────────────────────────────────────────────────────────────────
def load_cfg(config_path: Path):
    from app.config import load_config
    return load_config(config_path)


def cfg_or_none(config_path: Path):
    try:
        return load_cfg(config_path)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
#  热更新信号
# ──────────────────────────────────────────────────────────────────────
def reload_flag_path(cfg) -> Path:
    return cfg.db_path().parent / ".panel-reload"


def request_reload(cfg) -> str:
    """告诉正在跑的机器人「配置变了，重读一遍」。返回那个文件的路径。"""
    p = reload_flag_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    return str(p)


# ──────────────────────────────────────────────────────────────────────
#  状态
# ──────────────────────────────────────────────────────────────────────
def _main():
    """延迟导入 main.py（它 import 了一大堆东西，面板启动时不必付这个成本）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import main as m
    return m


def status(cfg, config_path: Path) -> Dict[str, Any]:
    """机器人现在的运行状态。任何一步失败都不抛异常，只把字段填成「不知道」。"""
    m = _main()
    from app import netprobe

    pid = 0
    pidfile = None
    started_at: Optional[float] = None
    try:
        pidfile = m.pid_file(cfg)
        pid = m.read_pid(cfg)
        if pidfile.exists():
            started_at = pidfile.stat().st_mtime
    except Exception:
        pass

    alive = bool(pid) and netprobe.pid_alive(pid)

    port_open = False
    port = 0
    mode = "?"
    try:
        port = int(cfg.onebot.port)
        mode = str(cfg.onebot.mode)
        if mode == "reverse":
            host = cfg.onebot.host
            probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "*", "") else host
            port_open = netprobe.tcp_open(probe_host, port, timeout=0.5)
    except Exception:
        pass

    cfg_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0

    return {
        "running": alive,
        "pid": pid if alive else 0,
        "stale_pid": pid if (pid and not alive) else 0,
        "startedAt": started_at,
        "uptimeSeconds": (time.time() - started_at) if (alive and started_at) else 0,
        "mode": mode,
        "port": port,
        "portOpen": port_open,
        "configPath": str(config_path),
        "configMtime": cfg_mtime,
        "configNewerThanProcess": bool(alive and started_at and cfg_mtime > started_at + 1),
        "logPath": str(cfg.log_path()),
        "dbPath": str(cfg.db_path()),
        "pidFile": str(pidfile) if pidfile else "",
        "python": sys.executable,
    }


# ──────────────────────────────────────────────────────────────────────
#  启停
# ──────────────────────────────────────────────────────────────────────
def start(cfg, config_path: Path) -> Dict[str, Any]:
    st = status(cfg, config_path)
    if st["running"]:
        return {"ok": False, "msg": f"已经有一个在跑了（PID {st['pid']}），先停掉再启动。"}

    # pidfile 是旧的（进程早死了）就先清掉，否则 main.py 会以为有实例在跑
    try:
        m = _main()
        pf = m.pid_file(cfg)
        if pf.exists():
            pf.unlink(missing_ok=True)
    except Exception:
        pass

    out_path = cfg.db_path().parent / "panel-service.out"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(out_path, "ab")
    try:
        fh.write(f"\n===== 面板启动于 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n".encode("utf-8"))
        fh.flush()
        flags = 0
        if os.name == "nt":
            # 独立进程 + 独立进程组：面板关掉之后机器人不会被顺手带走
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py"), "--config", str(config_path)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as e:
        fh.close()
        return {"ok": False, "msg": f"启动失败：{e}"}
    finally:
        try:
            fh.close()
        except Exception:
            pass

    # 给它一点时间把端口绑上 / 把 pidfile 写出来，然后回头核实一次
    deadline = time.time() + 6.0
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(0.4)
        last = status(cfg, config_path)
        if last["running"]:
            break
        if proc.poll() is not None:
            tail = _tail_text(out_path, 2000)
            return {"ok": False,
                    "msg": f"进程刚起来就退出了（退出码 {proc.returncode}）。"
                           f"最后几行输出：\n{tail}"}
    if not last.get("running"):
        return {"ok": True, "pid": proc.pid,
                "msg": f"已经拉起来了（PID {proc.pid}），但还没看到它写 pidfile，稍等刷新一下。"}
    return {"ok": True, "pid": last["pid"],
            "msg": f"已启动（PID {last['pid']}），监听 {last['port']}"}


def stop(cfg, config_path: Path) -> Dict[str, Any]:
    m = _main()
    from app import netprobe

    pid = m.read_pid(cfg)
    if not pid:
        return {"ok": False, "msg": "没找到 pidfile，本来就没在跑。"}
    if not netprobe.pid_alive(pid):
        try:
            m.pid_file(cfg).unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "msg": f"PID {pid} 早就不在了，顺手清掉了 pidfile。"}
    if pid == os.getpid():
        return {"ok": False, "msg": "pidfile 里记的是面板自己，没动它。"}

    if sys.platform == "win32":
        rc = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True).returncode
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
            rc = 0
        except Exception:
            rc = 1
    try:
        m.pid_file(cfg).unlink(missing_ok=True)
    except Exception:
        pass
    if rc == 0:
        return {"ok": True, "pid": pid, "msg": f"已停止（PID {pid}）。"}
    return {"ok": False, "msg": f"停止 PID {pid} 失败（返回码 {rc}）。"}


def restart(cfg, config_path: Path) -> Dict[str, Any]:
    st = status(cfg, config_path)
    notes: List[str] = []
    if st["running"]:
        r = stop(cfg, config_path)
        notes.append(r["msg"])
        time.sleep(1.0)
    r = start(cfg, config_path)
    notes.append(r["msg"])
    return {"ok": r.get("ok", False), "msg": " ".join(notes)}


# ──────────────────────────────────────────────────────────────────────
#  日志
# ──────────────────────────────────────────────────────────────────────
def _tail_text(path: Path, nbytes: int) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"（读不到 {path}：{e}）"


def tail_log(log_path: Path, offset: int = 0, limit_bytes: int = 262144) -> Dict[str, Any]:
    """从 offset 字节往后读增量。返回新的 offset，前端拿它做轮询游标。

    一个细节：如果正好读到一个半截行，就把那半行退回不消费，
    否则前端会出现「半条日志」闪一下然后变成完整行。
    """
    try:
        size = log_path.stat().st_size
    except FileNotFoundError:
        return {"offset": 0, "text": "", "size": 0, "reset": False, "exists": False}

    reset = False
    if offset > size or offset < 0:
        offset = 0
        reset = True          # 文件被截断或清空过，让前端也清一下

    try:
        with log_path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(limit_bytes)
    except Exception as e:
        return {"offset": offset, "text": "", "size": size, "reset": False,
                "exists": True, "error": str(e)}

    consumed = len(chunk)
    text = chunk.decode("utf-8", errors="replace")
    if consumed >= limit_bytes and not text.endswith("\n"):
        cut = text.rfind("\n")
        if cut >= 0:
            keep = text[:cut + 1]
            consumed = len(keep.encode("utf-8"))
            text = keep
    return {"offset": offset + consumed, "text": text, "size": size,
            "reset": reset, "exists": True}


def clear_log(log_path: Path) -> Dict[str, Any]:
    """清空日志文件（面板上那个「清空」按钮）。"""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as f:
            f.write(b"")
        return {"ok": True, "msg": "日志已清空（机器人那边还在往同一个文件写）。"}
    except Exception as e:
        return {"ok": False, "msg": f"清空失败：{e}"}


# ──────────────────────────────────────────────────────────────────────
#  花费 / 记忆（只读 sqlite）
# ──────────────────────────────────────────────────────────────────────
def _connect_ro(db: Path) -> sqlite3.Connection:
    """只读打开。WAL 库读不了就退回普通连接（机器人没在跑时不会有冲突）。"""
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1").fetchone()
        return con
    except Exception:
        con = sqlite3.connect(str(db), timeout=3.0)
        con.row_factory = sqlite3.Row
        return con


def _today_start() -> float:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def stats(cfg, *, days: int = 7, recent: int = 8) -> Dict[str, Any]:
    """面板「花费与记忆」那一屏的数据。"""
    db = cfg.db_path()
    out: Dict[str, Any] = {
        "exists": db.exists(),
        "dbPath": str(db),
        "dbSize": db.stat().st_size if db.exists() else 0,
        "todayYuan": 0.0, "todayCalls": 0, "todayHit": 0, "todayMiss": 0,
        "todayOutput": 0, "hitRate": None, "byPurpose": [],
        "daily": [], "facts": 0, "sessions": 0, "messages": 0,
        "allowlist": [], "recentFacts": [], "recentMessages": [],
        "firstDay": None, "lastCallAt": None,
    }
    if not db.exists():
        return out

    try:
        con = _connect_ro(db)
    except Exception as e:
        out["error"] = f"打不开数据库：{e}"
        return out

    try:
        t0 = _today_start()

        row = con.execute(
            "SELECT COALESCE(SUM(cost_yuan),0) c, COUNT(*) n,"
            " COALESCE(SUM(cache_hit),0) h, COALESCE(SUM(cache_miss),0) m,"
            " COALESCE(SUM(output_tokens),0) o"
            " FROM usage WHERE ts >= ?", (t0,)).fetchone()
        out["todayYuan"] = round(float(row["c"] or 0), 6)
        out["todayCalls"] = int(row["n"] or 0)
        out["todayHit"] = int(row["h"] or 0)
        out["todayMiss"] = int(row["m"] or 0)
        out["todayOutput"] = int(row["o"] or 0)
        tot = out["todayHit"] + out["todayMiss"]
        out["hitRate"] = round(out["todayHit"] / tot * 100, 1) if tot else None

        for r in con.execute(
                "SELECT purpose, COUNT(*) n, COALESCE(SUM(cost_yuan),0) c,"
                " COALESCE(SUM(cache_hit),0) h, COALESCE(SUM(cache_miss),0) m"
                " FROM usage WHERE ts >= ? GROUP BY purpose ORDER BY c DESC", (t0,)):
            h, m2 = int(r["h"] or 0), int(r["m"] or 0)
            out["byPurpose"].append({
                "purpose": r["purpose"] or "?",
                "calls": int(r["n"] or 0),
                "yuan": round(float(r["c"] or 0), 6),
                "hitRate": round(h / (h + m2) * 100, 1) if (h + m2) else None,
            })

        # 最近 N 天每天花了多少
        day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        buckets: Dict[str, Dict[str, Any]] = {}
        for i in range(days - 1, -1, -1):
            d = day0.timestamp() - i * 86400
            key = datetime.fromtimestamp(d).strftime("%m-%d")
            buckets[key] = {"day": key, "ts": d, "yuan": 0.0, "calls": 0}
        since = day0.timestamp() - (days - 1) * 86400
        for r in con.execute(
                "SELECT ts, cost_yuan FROM usage WHERE ts >= ?", (since,)):
            key = datetime.fromtimestamp(float(r["ts"])).strftime("%m-%d")
            b = buckets.get(key)
            if b:
                b["yuan"] = round(b["yuan"] + float(r["cost_yuan"] or 0), 6)
                b["calls"] += 1
        out["daily"] = list(buckets.values())

        out["facts"] = int(con.execute("SELECT COUNT(*) n FROM facts").fetchone()["n"])
        out["sessions"] = int(con.execute(
            "SELECT COUNT(DISTINCT session_id) n FROM messages").fetchone()["n"])
        out["messages"] = int(con.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"])

        r = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM usage").fetchone()
        out["firstDay"] = r["a"]
        out["lastCallAt"] = r["b"]

        for r in con.execute(
                "SELECT uid, nickname, added_at FROM allowlist ORDER BY added_at"):
            out["allowlist"].append({"uid": int(r["uid"]),
                                     "nickname": r["nickname"] or "",
                                     "addedAt": float(r["added_at"] or 0)})

        for r in con.execute(
                "SELECT id, session_id, content, importance, created_at, access_count"
                " FROM facts ORDER BY id DESC LIMIT ?", (recent,)):
            out["recentFacts"].append({
                "id": int(r["id"]), "session": r["session_id"] or "",
                "content": r["content"] or "",
                "importance": float(r["importance"] or 0),
                "createdAt": float(r["created_at"] or 0),
                "accessCount": int(r["access_count"] or 0),
            })

        for r in con.execute(
                "SELECT session_id, role, content, display, created_at"
                " FROM messages ORDER BY id DESC LIMIT ?", (recent,)):
            out["recentMessages"].append({
                "session": r["session_id"] or "",
                "role": r["role"] or "",
                "content": (r["display"] or r["content"] or "")[:120],
                "createdAt": float(r["created_at"] or 0),
            })
    except Exception as e:
        out["error"] = f"读数据出错：{e}"
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out
