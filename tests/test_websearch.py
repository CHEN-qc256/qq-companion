"""联网搜索 + 主人/客人身份的离线测试。

    python tests/test_websearch.py

A~D 组全部离线：喂假 HTML 给解析器、拿假工具函数替掉真搜索、拿脚本化的假 LLM
驱动工具调用循环。E 组才真的联网，连不上只提示不判失败（免得没网时红一片）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.orchestrator as O  # noqa: E402
from app.config import Config, load_config  # noqa: E402
from app.context import (ContextBuilder, GUEST_IDENTITY, MASTER_IDENTITY,  # noqa: E402
                         GROUP_SPEAKING_RULES)
from app.llm import ChatResult, DeepSeek  # noqa: E402
from app.store import Store  # noqa: E402
from app.websearch import (TOOLS, fetch_page, format_hits, html_to_text,  # noqa: E402
                           parse_bing, parse_ddg, run_tool, search, strip_tags,
                           unwrap_ddg)

TMP = ROOT / "data" / "_test_web"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  —— {detail}" if detail and not cond else ""))


def section(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 46 - len(t)))


# ──────────────────────────────────────────────────────────────────────
#  假数据
# ──────────────────────────────────────────────────────────────────────
DDG_PAGE = """
<div class="result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">标题&lt;一&gt;</a>
  <a class="result__snippet" href="x">这是<b>摘要</b>一 &amp; 东西</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="https://example.org/b">标题二</a>
  <a class="result__snippet" href="y">摘要二</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="/relative/only">相对链接</a>
</div>
"""

BING_PAGE = """
<ol id="b_results">
<li class="b_algo"><h2><a href="https://bing1.com/x">必应标题一</a></h2>
<p>必应摘要一 &amp; 补充</p></li>
<li class="b_algo"><h2><a href="https://bing2.com/y">必应标题二</a></h2>
<p>必应摘要二</p></li>
<li class="b_algo"><h2><a href="javascript:void(0)">广告</a></h2></li>
</ol>
"""


class ScriptLLM(DeepSeek):
    """按剧本返回 ChatResult，并记下每次收到的 messages。"""

    def __init__(self, cfg, store, script):
        super().__init__(cfg, store)
        self.script = list(script)
        self.calls: list[dict] = []

    async def chat(self, messages, **kw):
        self.calls.append({
            "purpose": kw.get("purpose"),
            "messages": [dict(m) for m in messages],
            "tools": kw.get("tools"),
        })
        if self.script:
            return self.script.pop(0)
        return ChatResult(text="（剧本用完了）")


class FakeOrch:
    """只要 cfg + llm —— _chat_with_tools 只用这两样。"""

    def __init__(self, cfg, llm):
        self.cfg = cfg
        self.llm = llm


def write_config(cfg_dir: Path) -> Path:
    p = cfg_dir / "config.yaml"
    p.write_text(f"""
llm:
  api_key: ""
  model: "deepseek-flash"
  thinking: false
  max_tokens: 200
  mock: true
  web_search: true
  max_tool_rounds: 2
context:
  history_tokens: 1500
budget:
  min_interval_seconds: 0
pricing:
  currency_per_million:
    deepseek-flash:
      cache_hit:  {{ peak: 0.04, offpeak: 0.02 }}
      cache_miss: {{ peak: 2.0,  offpeak: 1.0 }}
      output:     {{ peak: 8.0,  offpeak: 4.0 }}
  peak_weekdays: [0, 1, 2, 3, 4]
  peak_ranges: [[9, 12], [14, 18]]
  holidays: []
humanize:
  read_delay_min: 0
  read_delay_max: 0
  typing_per_char_min: 0
  typing_per_char_max: 0
  typing_min: 0
  typing_max: 0
  segment_gap_min: 0
  segment_gap_max: 0
  debounce_seconds: 0.05
  late_reply_chance: 0
life:
  enabled: false
  proactive:
    enabled: false
memory:
  enabled: false
whitelist:
  private: []
  groups: []
  auto_accept_first: 0
  masters: [10001]
group:
  enabled: false
persona:
  file: "persona/default.md"
  name: "遥遥"
  group_file: "persona/group.md"
  group_name: "遥遥"
database:
  path: "{(cfg_dir / 'companion.db').as_posix()}"
logging:
  level: "ERROR"
  file: "{(cfg_dir / 'companion.log').as_posix()}"
""", encoding="utf-8")
    return p


async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    cfg: Config = load_config(write_config(TMP))
    store = Store(cfg.db_path())

    # ══════════ A. HTML 解析 ══════════
    section("A. HTML 解析")
    check("strip_tags 去标签并还原实体",
          strip_tags("<b>你好</b> &amp; 再见") == "你好 & 再见",
          repr(strip_tags("<b>你好</b> &amp; 再见")))
    check("strip_tags 处理空值", strip_tags("") == "" and strip_tags(None) == "")
    check("html_to_text 丢掉 script 里的东西",
          "alert" not in html_to_text("<p>正文</p><script>alert(1)</script>"),
          html_to_text("<p>正文</p><script>alert(1)</script>"))
    check("html_to_text 把块级标签换行",
          "第一段" in html_to_text("<p>第一段</p><p>第二段</p>")
          and html_to_text("<p>第一段</p><p>第二段</p>").count("\n") >= 1)
    check("html_to_text 还原实体",
          "a & b" in html_to_text("<p>a &amp; b</p>")
          and "&amp;" not in html_to_text("<p>a &amp; b</p>"))

    check("unwrap_ddg 剥出真网址",
          unwrap_ddg("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa") == "https://example.com/a",
          unwrap_ddg("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa"))
    check("unwrap_ddg 处理协议相对链接",
          unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.cn%2F1").startswith("https://x.cn"))
    check("unwrap_ddg 对普通链接原样返回",
          unwrap_ddg("https://plain.com/p") == "https://plain.com/p")
    check("unwrap_ddg 空值返回空", unwrap_ddg("") == "" and unwrap_ddg(None) == "")

    ddg = parse_ddg(DDG_PAGE, 5)
    check("parse_ddg 解析出标题/链接/摘要", len(ddg) == 2, f"实际 {len(ddg)} 条")
    check("parse_ddg 标题去标签还原实体",
          bool(ddg) and ddg[0]["title"] == "标题<一>", ddg[0]["title"] if ddg else "")
    check("parse_ddg 链接是真实网址",
          bool(ddg) and ddg[0]["url"] == "https://example.com/a",
          ddg[0]["url"] if ddg else "")
    check("parse_ddg 摘要跟随次序",
          bool(ddg) and "摘要一" in ddg[0]["snippet"] and "摘要二" in ddg[1]["snippet"])
    check("parse_ddg 丢掉相对链接",
          all(h["url"].startswith("http") for h in ddg))
    check("parse_ddg 尊重 limit", len(parse_ddg(DDG_PAGE, 1)) == 1)
    check("parse_ddg 对空页面返回空", parse_ddg("", 5) == [] and parse_ddg(None, 5) == [])

    # 真机上 DuckDuckGo 的 HTML 端点会把广告混进结果里，标题像「Chat With XXX Now」。
    # 留着它模型会把广告当资料念给用户 —— 必须在解析阶段就丢掉，而且不能占掉 limit 名额。
    AD_PAGE = (
        '<a class="result__a" href="//duckduckgo.com/y.js?ad_domain=simpl.ai&ad_provider=b">'
        'Chat With Deepseek R1 Now - AI Reasoning</a>'
        '<a class="result__snippet">广告</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgood.cn%2F1">真好结果</a>'
        '<a class="result__snippet">真摘要</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgood.cn%2F2">第二个</a>'
        '<a class="result__snippet">摘要二</a>'
    )
    ads = parse_ddg(AD_PAGE, 2)
    check("parse_ddg 丢掉广告链接", all("ad_domain" not in h["url"] for h in ads),
          str([h["url"] for h in ads]))
    check("广告不占 limit 名额", len(ads) == 2 and ads[0]["url"] == "https://good.cn/1",
          str([h["url"] for h in ads]))
    check("parse_bing 也丢广告",
          all("ad_domain" not in h["url"] for h in parse_bing(AD_PAGE, 5)))

    bing = parse_bing(BING_PAGE, 5)
    check("parse_bing 解析出标题/链接/摘要", len(bing) == 2, f"实际 {len(bing)} 条")
    check("parse_bing 丢掉非 http 链接（广告）",
          all(h["url"].startswith("http") for h in bing))
    check("parse_bing 摘要正确", bool(bing) and "必应摘要一" in bing[0]["snippet"])
    check("parse_bing 尊重 limit", len(parse_bing(BING_PAGE, 1)) == 1)
    check("parse_bing 对空页面返回空", parse_bing("", 5) == [])

    # ══════════ B. 工具定义与分发 ══════════
    section("B. 工具定义与分发")
    check("工具定义是两个", len(TOOLS) == 2, str(len(TOOLS)))
    check("工具名是 web_search / fetch_page",
          [t["function"]["name"] for t in TOOLS] == ["web_search", "fetch_page"])
    check("每个工具都有 description 和 parameters",
          all(t["function"].get("description") and t["function"].get("parameters") for t in TOOLS))
    check("web_search 的 query 是必填",
          TOOLS[0]["function"]["parameters"]["required"] == ["query"])
    check("工具描述里写明了「日常闲聊别调」",
          "日常闲聊" in TOOLS[0]["function"]["description"])

    check("format_hits 没结果时说人话",
          "没有结果" in format_hits("x", []))
    hit_txt = format_hits("天气", [
        {"title": "T1", "url": "https://a.cn", "snippet": "S1"},
        {"title": "T2", "url": "https://b.cn", "snippet": "S" * 400},
    ])
    check("format_hits 带编号、标题、链接", "1. T1" in hit_txt and "https://a.cn" in hit_txt)
    check("format_hits 摘要截断到 160 字", hit_txt.count("S") < 200, str(hit_txt.count("S")))
    check("format_hits 提醒这是搜索结果不是记忆", "不是你的记忆" in hit_txt)

    check("run_tool 拒绝空关键词",
          "没给搜索关键词" in await run_tool("web_search", {}))
    check("run_tool 对未知工具说清楚",
          "没有叫 x 的工具" in await run_tool("x", {}))
    check("fetch_page 拒绝非 http 网址",
          "不合法" in await fetch_page("file:///C:/boot.ini"))
    check("fetch_page 拒绝空网址", "不合法" in await fetch_page(""))

    # ══════════ C. 主人 vs 客人 ══════════
    section("C. 主人 vs 客人身份")
    llm = DeepSeek(cfg, store)
    ctx = ContextBuilder(cfg, store, llm)
    msgs_master, _ = await ctx.build("private:10001", "【他刚说】在吗")
    msgs_guest, _ = await ctx.build("private:20002", "【他刚说】在吗")

    check("主人的私聊 system 是 2 条", len(msgs_master) >= 2)
    check("主人：system 里有主人身份锚",
          MASTER_IDENTITY in msgs_master[1]["content"])
    check("客人：system 里有客人身份锚",
          GUEST_IDENTITY in msgs_guest[1]["content"])
    check("客人：system 里没有主人身份锚",
          MASTER_IDENTITY not in msgs_guest[1]["content"])
    check("客人锚点写明别叫老大", "别叫他「老大」" in msgs_guest[1]["content"])
    check("客人锚点写明别讲主人私事", "不要讲你和主人私下聊过的事" in msgs_guest[1]["content"])
    check("人物设定（persona）两边都在",
          msgs_master[0]["content"] == msgs_guest[0]["content"] == cfg.persona_text)

    again, _ = await ctx.build("private:10001", "【他刚说】在吗")
    check("同一个会话两次组装结果完全一致（缓存前缀稳定）",
          [m["content"] for m in again] == [m["content"] for m in msgs_master])
    check("主人和客人的前缀确实不同",
          msgs_master[1]["content"] != msgs_guest[1]["content"])

    msgs_group, _ = await ctx.build("group:40004", "【群里】小明：在吗")
    gsys = msgs_group[2]["content"]
    check("群聊 system 是 3 条", len(msgs_group) >= 3)
    check("群聊规矩里有主人 QQ 号", "10001" in gsys, gsys[-120:])
    check("群聊仍然保留「别把群友当主人」",
          "别把群友当成主人" in gsys or "别把群友当成主人" in GROUP_SPEAKING_RULES)

    # masters 留空 = 老行为（谁来都是主人）
    cfg.whitelist.masters = []
    msgs_none, _ = await ctx.build("private:20002", "【他刚说】在吗")
    check("masters 留空时退回老行为（当主人对待）",
          MASTER_IDENTITY in msgs_none[1]["content"])
    check("Config.is_master 与 masters 一致",
          cfg.is_master(1) and cfg.is_master(999999))
    cfg.whitelist.masters = [10001]
    check("配了 masters 后只有名单内是主人",
          cfg.is_master(10001) and not cfg.is_master(20002))
    check("is_master 兼容字符串数字", cfg.is_master("10001"))

    # ══════════ D. 工具调用循环 ══════════
    section("D. 工具调用循环")
    real_tool = O.run_web_tool
    tool_calls: list[tuple[str, dict]] = []

    async def fake_tool(name, args):
        tool_calls.append((name, args))
        return f"[假搜索结果:{args.get('query', '')}]"

    O.run_web_tool = fake_tool
    try:
        # D1 模型不调工具：只问一次
        script = [ChatResult(text="不搜，直接说")]
        sl = ScriptLLM(cfg, store, script)
        orch = FakeOrch(cfg, sl)
        r = await O.Orchestrator._chat_with_tools(
            orch, [{"role": "user", "content": "hi"}], sid="private:10001", max_tokens=50)
        check("不调工具时只请求一次", len(sl.calls) == 1, str(len(sl.calls)))
        check("不调工具时返回模型的文本", r.text == "不搜，直接说")
        check("请求里带上了 tools", sl.calls[0]["tools"] is not None)
        check("没执行任何工具", tool_calls == [])

        # D2 模型调一次工具：两轮
        tool_calls.clear()
        assistant_msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "web_search", "arguments": '{"query":"今天天气"}'}}]}
        script = [ChatResult(text="", tool_calls=assistant_msg["tool_calls"],
                             assistant_message=assistant_msg),
                  ChatResult(text="今天晴，18 度")]
        sl = ScriptLLM(cfg, store, script)
        orch = FakeOrch(cfg, sl)
        r = await O.Orchestrator._chat_with_tools(
            orch, [{"role": "user", "content": "今天天气咋样"}],
            sid="private:10001", max_tokens=50)
        check("调工具后请求了两次", len(sl.calls) == 2, str(len(sl.calls)))
        check("执行了正确的工具和参数",
              tool_calls == [("web_search", {"query": "今天天气"})], str(tool_calls))
        check("第一轮的结果文本被后续轮次取代", r.text == "今天晴，18 度", r.text)
        check("第二轮请求里带上了 assistant 那条 tool_calls 消息",
              any(m.get("tool_calls") for m in sl.calls[1]["messages"]))
        check("第二轮请求里带上了 role=tool 的结果",
              any(m.get("role") == "tool" and "假搜索结果" in str(m.get("content"))
                  for m in sl.calls[1]["messages"]))
        check("tool 消息的 tool_call_id 与请求一致",
              any(m.get("tool_call_id") == "call_1" for m in sl.calls[1]["messages"]))
        check("工具轮的 purpose 标成 search",
              sl.calls[1]["purpose"] == "search", str(sl.calls[1]["purpose"]))

        # D3 坏 JSON 参数 → 当空字典，不崩
        tool_calls.clear()
        bad = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "web_search", "arguments": "{不是 json"}}]}
        script = [ChatResult(text="", tool_calls=bad["tool_calls"], assistant_message=bad),
                  ChatResult(text="好了")]
        sl = ScriptLLM(cfg, store, script)
        r = await O.Orchestrator._chat_with_tools(
            FakeOrch(cfg, sl), [{"role": "user", "content": "x"}],
            sid="private:10001", max_tokens=50)
        check("参数是坏 JSON 时不崩", r.text == "好了")
        check("坏 JSON 被当成空参数",
              tool_calls == [("web_search", {})], str(tool_calls))

        # D4 轮次用完还想要工具 → 去掉 tools 再问最后一次，必须有话说
        tool_calls.clear()
        def tc(i):
            m = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "web_search", "arguments": '{"query":"永动机"}'}}]}
            return ChatResult(text="", tool_calls=m["tool_calls"], assistant_message=m)
        script = [tc(1), tc(2), tc(3), ChatResult(text="没搜到，我说不知道")]
        sl = ScriptLLM(cfg, store, script)
        r = await O.Orchestrator._chat_with_tools(
            FakeOrch(cfg, sl), [{"role": "user", "content": "y"}],
            sid="private:10001", max_tokens=50)
        check("轮次用完最多请求 max_tool_rounds+2 次",
              len(sl.calls) <= cfg.llm.max_tool_rounds + 2, str(len(sl.calls)))
        check("轮次用完还能有话说（不会空回复）", bool(r.text.strip()), repr(r.text))
        check("最后一次请求不带 tools",
              sl.calls[-1]["tools"] is None, str(sl.calls[-1]["tools"]))

        # D5 关掉联网搜索 → 完全不发 tools
        cfg.llm.web_search = False
        sl = ScriptLLM(cfg, store, [ChatResult(text="在的")])
        r = await O.Orchestrator._chat_with_tools(
            FakeOrch(cfg, sl), [{"role": "user", "content": "z"}],
            sid="private:10001", max_tokens=50)
        check("关掉联网搜索时不发 tools", sl.calls[0]["tools"] is None)
        check("关掉联网搜索时只请求一次", len(sl.calls) == 1)
        cfg.llm.web_search = True

        # D6 工具自己炸了也不影响出话
        async def boom_tool(name, args):
            raise RuntimeError("炸了")
        O.run_web_tool = boom_tool
        m = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c9", "type": "function",
             "function": {"name": "web_search", "arguments": '{"query":"x"}'}}]}
        sl = ScriptLLM(cfg, store, [
            ChatResult(text="", tool_calls=m["tool_calls"], assistant_message=m),
            ChatResult(text="搜的时候出错了，我不太清楚")])
        r = await O.Orchestrator._chat_with_tools(
            FakeOrch(cfg, sl), [{"role": "user", "content": "w"}],
            sid="private:10001", max_tokens=50)
        check("工具抛异常时不崩", r.text.startswith("搜的时候出错了"), r.text)
        check("工具异常被写回给模型",
              any("工具执行失败" in str(x.get("content"))
                  for x in sl.calls[1]["messages"]))
    finally:
        O.run_web_tool = real_tool

    # ══════════ E. 源码护栏 + 真联网 ══════════
    section("E. 源码护栏与真联网")
    src = (ROOT / "app" / "websearch.py").read_text(encoding="utf-8")
    import re as _re
    bad = _re.findall(r'[\u4e00-\u9fff]"[\u4e00-\u9fff]', src)
    check("websearch.py 里没有中文直引号（会提前闭合 Python 字符串）",
          not bad, str(bad))

    try:
        hits = await search("DeepSeek 是什么", 3)
    except Exception as e:                                   # noqa: BLE001
        hits, err = [], f"{type(e).__name__}: {e}"
    else:
        err = ""
    if err:
        check("真联网搜索不抛异常（没网就跳过）", False, err)
    else:
        check("真联网搜索不抛异常", True)
        print(f"    （真搜到 {len(hits)} 条"
              + (f"：{hits[0]['title'][:30]}" if hits else "，可能没网或搜索源改版") + "）")

    store.close()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：")
        for f in FAIL:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
