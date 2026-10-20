"""管理面板测试（不联网、不起真的机器人）。

六组：
  A 参数元数据      —— schema 自洽：不重不漏、默认值能摊平、restart 和 HOT_EXCLUDE 一致
  B 配置读写        —— 往返不失真、注释还在、未知键保留、原子写 + 备份
  C 校验            —— 该拦的拦住、该放的放行
  D 热/冷分类       —— 哪些能热更新、哪些必须重启
  E HTTP 接口       —— 真的起一个面板服务，把每个接口打一遍
  F 长连接          —— 不带 body 的 POST 不能污染同一个连接上的下一个请求

跑法：python tests/test_panel.py
"""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import HOT_EXCLUDE, apply_hot, load_config  # noqa: E402
from panel import configio, schema, runner  # noqa: E402
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


# 测试用的工作区（不碰真实 config.yaml）
TMP = ROOT / "data" / "_test_panel"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
CFG_PATH = TMP / "config.yaml"
shutil.copy2(ROOT / "config.example.yaml", CFG_PATH)

# 给测试库配上独立路径，免得面板接口去读正式库
_raw = configio.load(CFG_PATH)
_raw["database"]["path"] = "data/_test_panel/panel.db"
_raw["logging"]["file"] = "data/_test_panel/panel.log"
configio.save(CFG_PATH, _raw, backup=False)


def main() -> int:
    # ────────────────────────────────────────────────────────────
    section("A 参数元数据（schema 自洽）")
    # ────────────────────────────────────────────────────────────
    paths = [p.path for p in schema.ALL_PARAMS]
    # 不写死具体数字：加参数是常态，写死只会让人顺手改断言。
    # 这里卡的是「别被整段删没了」和「路径集合自洽」。
    check("参数数量在合理范围（80~140）", 80 <= len(paths) <= 140, f"实际 {len(paths)}")
    check("路径没有重复", len(paths) == len(set(paths)),
          f"重复：{[p for p in paths if paths.count(p) > 1]}")
    check("每个参数都有中文说明", all(p.desc.strip() for p in schema.ALL_PARAMS))
    check("每个参数都有 label", all(p.label.strip() for p in schema.ALL_PARAMS))

    gids = {g.id for g in schema.GROUPS}
    check("每个参数都归属一个已定义的分组",
          all(p.group in gids for p in schema.ALL_PARAMS),
          f"野分组：{sorted({p.group for p in schema.ALL_PARAMS} - gids)}")
    check("12 个分组都有参数",
          all(any(p.group == g.id for p in schema.ALL_PARAMS) for g in schema.GROUPS),
          f"空分组：{[g.id for g in schema.GROUPS if not any(p.group == g.id for p in schema.ALL_PARAMS)]}")

    d = schema.defaults()
    missing = [p.path for p in schema.ALL_PARAMS if configio.get_path(d, p.path) is None]
    check("defaults() 覆盖了所有参数", not missing, f"缺：{missing[:5]}")

    # restart 徽章必须和 app 侧的热更新名单一字不差
    bad_restart = [p.path for p in schema.ALL_PARAMS
                   if p.restart != (p.path in HOT_EXCLUDE)]
    check("restart 徽章与 HOT_EXCLUDE 完全一致", not bad_restart,
          f"不一致：{bad_restart[:5]}")
    check("需重启的参数是 11 个",
          sum(1 for p in schema.ALL_PARAMS if p.restart) == 11,
          f"实际 {sum(1 for p in schema.ALL_PARAMS if p.restart)}")

    # 控件类型必须是前端认识的那几个
    KNOWN = {"str", "password", "path", "int", "float", "bool", "enum",
             "int_list", "str_list", "range", "range_list", "pair_list",
             "map", "price_table"}
    bad_type = [(p.path, p.type) for p in schema.ALL_PARAMS if p.type not in KNOWN]
    check("控件类型都在前端支持范围内", not bad_type, f"{bad_type[:5]}")
    check("enum 参数都带 choices",
          all(p.choices for p in schema.ALL_PARAMS if p.type == "enum"))
    check("map 参数都带 keys",
          all(p.keys for p in schema.ALL_PARAMS if p.type == "map"))
    # 加分组是正常演进（最近一次是「B 站视频推送」）。这里卡的是「别整组消失」。
    check("groups_with_params 返回 15 组（多了「B 站视频推送」）",
          len(schema.groups_with_params()) == 15,
          f"实际 {len(schema.groups_with_params())} 组")

    # ────────────────────────────────────────────────────────────
    section("B 配置读写（往返不失真）")
    # ────────────────────────────────────────────────────────────
    cfg = configio.load(CFG_PATH)
    text = configio.render(cfg)
    back = __import__("yaml").safe_load(text)

    bad = [(p.path, configio.get_path(cfg, p.path), configio.get_path(back, p.path))
           for p in schema.ALL_PARAMS
           if configio.get_path(cfg, p.path) != configio.get_path(back, p.path)]
    check("往返后每个参数的值都没变", not bad, f"{bad[:3]}")

    check("生成的文本带中文注释",
          sum(1 for l in text.splitlines() if l.strip().startswith("#")) > 100,
          f"只有 {sum(1 for l in text.splitlines() if l.strip().startswith('#'))} 行注释")

    # 回归护栏：中文里当引号用的直引号会提前闭合 Python 字符串。
    # 这个坑在 schema.py 上踩过一次（整个文件编译不过），留一条断言盯着。
    import re as _re
    _offenders = []
    for _f in sorted((ROOT / "panel").glob("*.py")):
        _hits = _re.findall(r'[\u4e00-\u9fff]"[\u4e00-\u9fff]', _f.read_text(encoding="utf-8"))
        if _hits:
            _offenders.append((_f.name, len(_hits), _hits[:2]))
    check("panel/*.py 里没有「中文+直引号+中文」的写法",
          not _offenders, f"{_offenders}")

    check("每个分组标题都写进了文件",
          all(f"# ──── {g.title}" in text for g in schema.GROUPS),
          f"缺：{[g.title for g in schema.GROUPS if f'# ──── {g.title}' not in text]}")

    # 未知键必须保留（配置文件比代码新时不丢东西）
    cfg2 = dict(cfg)
    cfg2["futureFeature"] = {"hello": "world", "n": 3}
    t2 = configio.render(cfg2)
    check("schema 没覆盖的顶层键被原样保留", "futureFeature" in t2 and "world" in t2)

    # 原子写 + 备份
    bak = configio.save(CFG_PATH, cfg)
    check("保存时生成了 config.yaml.bak", bak is not None and Path(bak).exists())
    check("保存后读回来还是同一份", configio.load(CFG_PATH) == cfg)
    check("没有留下 .tmp 临时文件",
          not list(TMP.glob("*.tmp")), f"{list(TMP.glob('*.tmp'))}")

    # 导入
    parsed, err = configio.parse_import("llm:\n  model: deepseek-v4-pro\n")
    check("import 能解析局部 YAML", err is None and parsed["llm"]["model"] == "deepseek-v4-pro",
          str(err))
    check("import 会把没写的参数补成默认值",
          configio.get_path(parsed, "budget.daily_yuan") is not None)
    _, err2 = configio.parse_import("llm: [这不是映射")
    check("import 遇到坏 YAML 会报错而不是崩", err2 is not None)
    _, err3 = configio.parse_import("hello")
    check("import 遇到非映射会报错", err3 is not None)

    # 导出打码
    exp_mask = configio.export_text(cfg, "yaml", mask_key=True)
    exp_raw = configio.export_text(cfg, "yaml", mask_key=False)
    check("导出打码后不含原始 API Key",
          (cfg["llm"]["api_key"] or "EMPTY") not in exp_mask)
    check("导出不打码时含原始 API Key",
          not cfg["llm"]["api_key"] or cfg["llm"]["api_key"] in exp_raw)
    check("导出 JSON 能被解析", json.loads(configio.export_text(cfg, "json"))["llm"] is not None)

    # ────────────────────────────────────────────────────────────
    section("C 校验")
    # ────────────────────────────────────────────────────────────
    good = configio.load(CFG_PATH)
    good["whitelist"]["groups"] = [30003]        # 把唯一的 warn 消掉
    good["llm"]["api_key"] = "sk-" + "x" * 30
    good["llm"]["thinking"] = False
    good["llm"]["mock"] = False
    issues = configio.validate(good, panel_port=9999, root=ROOT)
    errs = [i for i in issues if i["level"] == "error"]
    check("干净配置没有 error", not errs, f"{errs}")

    def only(vals, mutate):
        c = json.loads(json.dumps(vals))
        mutate(c)
        return configio.validate(c, panel_port=9999, root=ROOT)

    r = only(good, lambda c: c["humanize"].__setitem__("read_delay_min", 999))
    check("超出 max 会被标 error",
          any(i["path"] == "humanize.read_delay_min" and i["level"] == "error" for i in r))

    r = only(good, lambda c: (c["humanize"].__setitem__("read_delay_min", 9),
                              c["humanize"].__setitem__("read_delay_max", 1)))
    check("下限大于上限会被标 error",
          any(i["path"] == "humanize.read_delay_max" and i["level"] == "error" for i in r))

    r = only(good, lambda c: c["humanize"].__setitem__("trivial_replies", [["([", "x"]]))
    check("坏正则会被标 error",
          any(i["path"] == "humanize.trivial_replies" and i["level"] == "error" for i in r))

    r = only(good, lambda c: c["onebot"].__setitem__("port", 9999))
    check("端口和面板端口冲突会被标 error",
          any(i["path"] == "onebot.port" and i["level"] == "error" for i in r))

    r = only(good, lambda c: c["whitelist"].__setitem__("private", ["不是数字"]))
    check("int_list 里混进字符串会被标 error",
          any(i["path"] == "whitelist.private" and i["level"] == "error" for i in r))

    r = only(good, lambda c: c["llm"].__setitem__("thinking", True))
    check("开思考模式会给 warn", any(i["path"] == "llm.thinking" and i["level"] == "warn" for i in r))

    r = only(good, lambda c: c["whitelist"].__setitem__("groups", []))
    check("群白名单为空会给 warn",
          any(i["path"] == "whitelist.groups" and i["level"] == "warn" for i in r))

    r = only(good, lambda c: c["llm"].__setitem__("api_key", ""))
    check("没 Key 也没开 mock 会给 warn",
          any(i["path"] == "llm.api_key" for i in r))

    r = only(good, lambda c: c["llm"].__setitem__("api_key", ""))
    r2 = configio.validate({**json.loads(json.dumps(good)),
                            "llm": {**good["llm"], "api_key": "", "mock": True}},
                           panel_port=9999, root=ROOT)
    check("开了 mock 就不再提醒没 Key",
          not any(i["path"] == "llm.api_key" for i in r2))

    # ────────────────────────────────────────────────────────────
    section("D 热更新 vs 需重启 分类")
    # ────────────────────────────────────────────────────────────
    a = configio.load(CFG_PATH)
    b = json.loads(json.dumps(a))
    b["humanize"]["late_reply_chance"] = 0.5        # 热
    b["llm"]["model"] = "deepseek-v4-pro"           # 热
    b["onebot"]["port"] = 4321                      # 冷
    b["llm"]["api_key"] = "sk-zzz"                  # 冷
    hot, cold = configio.classify(a, b)
    check("热更新项识别正确",
          set(hot) == {"humanize.late_reply_chance", "llm.model"}, f"{hot}")
    check("需重启项识别正确",
          set(cold) == {"onebot.port", "llm.api_key"}, f"{cold}")

    hot2, cold2 = configio.classify(a, json.loads(json.dumps(a)))
    check("没有改动时两边都是空", not hot2 and not cold2)

    # apply_hot 必须真的把值写进去，且**不碰**冷参数。
    # 注意这里要用 app.config.load_config（返回 dataclass），不是 configio.load（返回 dict）。
    live = load_config(CFG_PATH)
    before_port = live.onebot.port
    before_key = live.llm.api_key
    new = load_config(CFG_PATH)
    new.humanize.late_reply_chance = 0.77
    new.onebot.port = 8888                    # 冷：不该生效
    new.llm.api_key = "sk-SHOULD-NOT-APPLY"   # 冷：不该生效
    changed = apply_hot(live, new)
    check("apply_hot 报告了热更新项",
          "humanize.late_reply_chance" in changed, f"{changed}")
    check("apply_hot 真的换了值", live.humanize.late_reply_chance == 0.77)
    check("apply_hot 没碰冷参数（端口）", live.onebot.port == before_port,
          f"{live.onebot.port} != {before_port}")
    check("apply_hot 没碰冷参数（API Key）", live.llm.api_key == before_key)
    check("apply_hot 不把冷参数列进 changed",
          not any(c in changed for c in ("onebot.port", "llm.api_key")), f"{changed}")

    # ────────────────────────────────────────────────────────────
    section("E HTTP 接口")
    # ────────────────────────────────────────────────────────────
    Handler.ctx = Ctx(CFG_PATH, ROOT)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    base_host = "127.0.0.1"
    print(f"  （测试面板跑在 127.0.0.1:{port}）")

    def http(method, path, body=None, conn=None, timeout=30):
        own = conn is None
        c = conn or HTTPConnection(base_host, port, timeout=timeout)
        payload = None if body is None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"} if payload is not None else {}
        c.request(method, path, payload, hdrs)
        resp = c.getresponse()
        raw = resp.read()
        if own:
            c.close()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except Exception:
            return resp.status, {"_raw": raw.decode("utf-8", "replace")}

    st, d1 = http("GET", "/api/health")
    check("GET /api/health", st == 200 and d1.get("ok") is True, str(d1)[:120])

    st, d2 = http("GET", "/api/schema")
    check("GET /api/schema 返回 15 组，参数数与模块一致",
          st == 200 and len(d2["groups"]) == 15
          and d2["total"] == len(schema.ALL_PARAMS),
          f"total={d2.get('total') if isinstance(d2, dict) else '?'}")
    check("schema 里带了 defaults", "llm" in d2.get("defaults", {}))

    st, d3 = http("GET", "/api/config")
    check("GET /api/config 返回 values + raw + issues",
          st == 200 and "values" in d3 and "raw" in d3 and "issues" in d3)
    check("返回的 raw 就是磁盘上的原文",
          d3["raw"].strip() == CFG_PATH.read_text(encoding="utf-8").strip())

    st, d4 = http("GET", "/api/status")
    check("GET /api/status 有 running/port/mode",
          st == 200 and "running" in d4 and "port" in d4 and "mode" in d4)
    check("测试配置没在跑", d4["running"] is False)

    st, d5 = http("GET", "/api/stats")
    check("GET /api/stats 能跑通", st == 200 and "exists" in d5, str(d5)[:120])

    st, d6 = http("GET", "/api/logs?offset=0")
    check("GET /api/logs 返回 offset/text", st == 200 and "offset" in d6 and "text" in d6)

    # ★ 只读：POST /api/boot 会真的写/删用户注册表里的启动项，
    #   测试里点一下就可能把用户的开机自启关掉，所以这里只测读。
    st, d6b = http("GET", "/api/boot/status")
    check("GET /api/boot/status 给出注册表真相",
          st == 200 and {"enabled", "runKey", "command", "stale"} <= set(d6b),
          str(d6b)[:160])
    check("自启状态里带「下次会拉起什么」",
          (d6b.get("settings") or {}).get("headless") is True
          and "delay" in (d6b.get("settings") or {}), str(d6b.get("settings")))
    check("面板读到的参数记录和 schema 默认值一致",
          d6b.get("cfgEnabled") is False)

    st, d7 = http("GET", "/api/fs?path=.")
    check("GET /api/fs 能列目录",
          st == 200 and "app" in d7["dirs"] and any(f["name"] == "main.py" for f in d7["files"]))
    st, d8 = http("GET", "/api/fs?path=../../..")
    check("GET /api/fs 挡住越界访问", st in (400, 403), f"status={st} {d8}")

    st, d9 = http("GET", "/api/debug/ping")
    check("GET /api/debug/ping", st == 200 and d9.get("pong") is True)

    st, d10 = http("GET", "/api/nonexistent")
    check("未知接口返回 404", st == 404, f"status={st}")

    st, d11 = http("POST", "/api/config/validate", {"values": good})
    check("POST /api/config/validate", st == 200 and "issues" in d11)

    st, d12 = http("POST", "/api/config/import", {"text": "llm:\n  model: deepseek-v4-pro"})
    check("POST /api/config/import",
          st == 200 and d12["ok"] and d12["values"]["llm"]["model"] == "deepseek-v4-pro")

    st, d13 = http("POST", "/api/config/import", {"text": "!!!bad"})
    check("POST import 坏 YAML 返回 ok=false", st == 200 and d13["ok"] is False)

    # 内容没变的保存
    cur = http("GET", "/api/config")[1]["values"]
    st, d14 = http("POST", "/api/config", {"values": cur})
    check("POST /api/config 内容不变时也成功",
          st == 200 and d14["ok"] and not d14["hotChanged"] and not d14["coldChanged"])

    # 有 error 时不落盘
    badv = json.loads(json.dumps(cur))
    badv["humanize"]["read_delay_min"] = 9999
    before_text = CFG_PATH.read_text(encoding="utf-8")
    st, d15 = http("POST", "/api/config", {"values": badv})
    check("有 error 时拒绝保存", d15["ok"] is False and "issues" in d15)
    check("被拒绝时磁盘文件一个字节都没动",
          CFG_PATH.read_text(encoding="utf-8") == before_text)

    # force 可以越过 error
    st, d16 = http("POST", "/api/config", {"values": badv, "force": True})
    check("force=true 时可以强存", st == 200 and d16["ok"] is True)
    # 存回去
    configio.save(CFG_PATH, cur, backup=False)

    st, d17 = http("GET", "/api/config/export?format=json&mask=0")
    check("导出接口返回 200", st == 200)
    st, d18 = http("GET", "/api/config/defaults")
    check("默认值接口返回 values", st == 200 and "llm" in d18["values"])

    # 全语音模式按钮：只动 tts.always_voice 一个布尔值，绝不能顺手改掉 trigger/chance
    before_tts = configio.load(CFG_PATH)["tts"]
    st, d20a = http("POST", "/api/voice/always", {"on": True})
    after_tts = configio.load(CFG_PATH)["tts"]
    check("POST /api/voice/always 打开全语音",
          st == 200 and d20a.get("ok") and d20a.get("alwaysVoice") is True, str(d20a)[:120])
    check("全语音开关写进了 config.yaml", after_tts.get("always_voice") is True)
    check("打开全语音没动 trigger",
          after_tts.get("trigger") == before_tts.get("trigger"),
          f"{after_tts.get('trigger')} != {before_tts.get('trigger')}")
    check("打开全语音没动 chance",
          after_tts.get("chance") == before_tts.get("chance"),
          f"{after_tts.get('chance')} != {before_tts.get('chance')}")
    st, d20b = http("POST", "/api/voice/always", {"on": False})
    off_tts = configio.load(CFG_PATH)["tts"]
    check("再点一次关掉全语音",
          d20b.get("ok") and d20b.get("alwaysVoice") is False
          and off_tts.get("always_voice") is False)
    check("关掉之后 trigger / chance 还是原来的值（保留原设置）",
          off_tts.get("trigger") == before_tts.get("trigger")
          and off_tts.get("chance") == before_tts.get("chance"))

    st, d19 = http("POST", "/api/logs/clear")
    check("清空日志接口", st == 200)

    st, d20 = http("POST", "/api/service/stop")
    check("停止接口在没跑的时候也能优雅回应",
          st == 200 and "msg" in d20, str(d20)[:120])

    # ────────────────────────────────────────────────────────────
    section("F 长连接（不带 body 的 POST 不能污染下一个请求）")
    # ────────────────────────────────────────────────────────────
    conn = HTTPConnection(base_host, port, timeout=20)
    try:
        # 先 POST 一个不需要 body 的接口，再在**同一条连接**上 GET
        st_a, a = http("POST", "/api/logs/clear", None, conn=conn)
        st_b, b = http("GET", "/api/health", None, conn=conn)
        st_c, c = http("POST", "/api/logs/clear", {"ignored": 1}, conn=conn)
        st_d, dd = http("GET", "/api/status", None, conn=conn)
        check("POST(无 body) 之后 GET 正常", st_a == 200 and st_b == 200 and b.get("ok"),
              f"{st_a} {st_b}")
        check("POST(有 body) 之后 GET 正常", st_c == 200 and st_d == 200,
              f"{st_c} {st_d}")
        check("四个请求都在同一条连接上完成",
              conn.sock is not None, "连接被服务端提前关掉了")
    finally:
        conn.close()

    httpd.shutdown()
    httpd.server_close()

    # ────────────────────────────────────────────────────────────
    section("G 前端静态资源（hidden 属性必须真的能隐藏）")
    # ────────────────────────────────────────────────────────────
    # 这个坑真实发生过：弹窗 <div class="modal" hidden> 在页面上一直显示，
    # 把整个配置页挡死，用户看到的是个写着「标题」的空框，点哪儿都没反应。
    # 根因是 `hidden` 属性只是 UA 样式表里的 [hidden]{display:none}，优先级
    # 低于任何 class 规则，而 .modal 恰好有 display:flex。
    import re as _re

    web = ROOT / "panel" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "style.css").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")

    check("style.css 里有 [hidden] 兜底规则",
          bool(_re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none", css)),
          "没有这条，任何带 display 规则的 class 都会压过 hidden 属性")
    check("[hidden] 兜底带 !important",
          bool(_re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", css)),
          "不带 !important 的话，行内样式或更高优先级的规则仍可能压过它")

    check("弹窗默认带 hidden", bool(_re.search(r'id="modal"[^>]*\shidden', html)),
          "弹窗没有 hidden 就会一进页面就挡在那里")
    check("app.js 用 .hidden = false 打开弹窗",
          "$('#modal').hidden = false" in js)
    check("app.js 用 .hidden = true 关掉弹窗",
          "$('#modal').hidden = true" in js)
    check("点遮罩能关弹窗",
          "e.target.id === 'modal'" in js)

    # 每个带 hidden 的元素，其 class 都必须在「有 [hidden] 兜底」的前提下才安全
    for m in _re.finditer(r"<[a-z]+[^>]*\shidden[^>]*>", html):
        tag = m.group(0)
        eid = _re.search(r'id="([^"]+)"', tag)
        check(f"带 hidden 的元素 {eid.group(1) if eid else tag[:30]} 有 id（便于查）",
              eid is not None, tag[:60])

    check("前端没有把 hidden 当 class 用（两套机制容易混）",
          "classList.toggle('hidden'" not in js and "classList.add('hidden'" not in js)

    # 日志轮询的游标要往前走，否则会一直重复读同一段
    check("app.js 记录日志 offset 游标", "offset" in js and "reset" in js)

    # ────────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print(f"  通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("  失败项：")
        for f in FAILED:
            print("    -", f)
    print("=" * 70)

    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
