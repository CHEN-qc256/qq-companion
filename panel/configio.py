"""config.yaml 的读写 —— 重点是**写回去的时候保住中文注释**。

为什么不用 `yaml.dump`：
    用户的 config.yaml 里有大量手工注释（比如「★ 头号省钱开关」），
    `yaml.dump` 会把它们全部抹掉。而这个文件恰恰是用户会直接打开看的东西。

所以这里的做法是：
    读 → yaml.safe_load 成 dict → 和 schema 的默认值合并
    写 → 按 schema 的顺序**手写 YAML**，每个参数前面挂两行注释
         （`desc` 用途 + `note` 单位/范围/默认值来源）
    这样生成的注释和面板 tooltip、和 schema.py 里的注释是**同一份文字**，
    三处永远一致，不会出现「改了一处忘了另一处」。

另外两条保命规则：
    - 写之前先备份成 config.yaml.bak
    - schema 没覆盖到的键原样保留（前向兼容，将来加了新参数不会被面板吃掉）
"""
from __future__ import annotations

import json
import re
import shutil
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .schema import ALL_PARAMS, GROUPS, PARAMS_BY_PATH, defaults

# 每个顶层键属于哪个分组（写注释横幅用）
_TOP_GROUP = {
    "llm": "llm",
    "onebot": "onebot",
    "budget": "budget",
    "context": "context",
    "humanize": "humanize",
    "life": "life",
    "memory": "memory",
    "persona": "persona",
    "whitelist": "whitelist",
    "group": "group",
    "database": "runtime",
    "logging": "runtime",
    "pricing": "pricing",
    "tts": "tts",
    "boot": "boot",
    "videopush": "videopush",
}
_GROUP_BY_ID = {g.id: g for g in GROUPS}


# ──────────────────────────────────────────────────────────────────────
#  读
# ──────────────────────────────────────────────────────────────────────
def read_raw(path: Path) -> str:
    """原始文件内容（面板里「看原文」用）。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load(path: Path) -> Dict[str, Any]:
    """读配置并按 schema 补默认值。文件不存在就返回全默认。"""
    raw: Dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}
    return _fill_defaults(raw)


def _fill_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把 schema 里的默认值补到缺失的位置。不覆盖已有值。"""
    out = dict(raw)
    for p in ALL_PARAMS:
        parts = p.path.split(".")
        cur = out
        ok = True
        for seg in parts[:-1]:
            nxt = cur.get(seg)
            if not isinstance(nxt, dict):
                if nxt is None:
                    nxt = {}
                    cur[seg] = nxt
                else:
                    ok = False          # 形状不对（比如是标量），不硬塞
                    break
            cur = nxt
        if ok and parts[-1] not in cur:
            cur[parts[-1]] = p.default
    return out


def get_path(cfg: Dict[str, Any], path: str, fallback: Any = None) -> Any:
    cur: Any = cfg
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return fallback
        cur = cur[seg]
    return cur


def set_path(cfg: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = cfg
    for seg in parts[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ──────────────────────────────────────────────────────────────────────
#  写
# ──────────────────────────────────────────────────────────────────────
def _fmt_num(v: float) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v)


def _quote(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fmt_scalar(v: Any, ptype: str) -> str:
    if ptype == "bool" or isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _fmt_num(v)
    return _quote(str(v))


def _fmt_list(v: Any, indent: str = "") -> str:
    """短列表写成一行，长列表折成多行（缩进跟当前层级对齐）。"""
    if not isinstance(v, list):
        return "[]"
    if not v:
        return "[]"
    flat = all(not isinstance(x, (list, dict)) for x in v)
    if flat and all(isinstance(x, str) for x in v):
        one = "[" + ", ".join(_quote(x) for x in v) + "]"
    elif flat:
        one = "[" + ", ".join(_fmt_scalar(x, "") for x in v) + "]"
    else:
        parts = []
        for x in v:
            if isinstance(x, list):
                parts.append("[" + ", ".join(_fmt_scalar(i, "") for i in x) + "]")
            else:
                parts.append(_fmt_scalar(x, ""))
        one = "[" + ", ".join(parts) + "]"
    if len(one) <= 88:
        return one
    # 长列表折行，缩进对齐到 key 后面
    pad = indent + "  "
    return ("[\n" + "".join(f"{pad}{_quote(x)},\n" for x in v) + f"{indent}]")


def _comment_lines(p, indent: str) -> List[str]:
    """一个参数前面的注释块。文字直接来自 schema —— tooltip 和这里同源。"""
    out: List[str] = []
    for text in (p.desc, p.note):
        if not text:
            continue
        for line in textwrap.wrap(text, width=76, break_long_words=False,
                                  break_on_hyphens=False):
            out.append(f"{indent}# {line}")
    return out


def render(cfg: Dict[str, Any]) -> str:
    """把配置渲染成带中文注释的 YAML 全文。"""
    lines: List[str] = [
        "# qq-companion 配置",
        "#",
        "# 这个文件由「管理面板」生成 —— 每个参数上面的注释就是面板里的说明，两边同源。",
        f"# 手工改这里也能生效，但下次在面板上保存会被面板的内容覆盖。",
        f"# 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "#",
        "# 带 ★ 的项直接决定花多少钱，改之前先看注释。",
    ]

    # 按 schema 顺序重建嵌套结构
    ordered: Dict[str, Any] = {}
    for p in ALL_PARAMS:
        set_path(ordered, p.path, get_path(cfg, p.path, p.default))
    _carry_unknown(ordered, cfg, "")

    for top_key, val in ordered.items():
        gid = _TOP_GROUP.get(top_key)
        g = _GROUP_BY_ID.get(gid) if gid else None
        if g:
            lines.append("")
            lines.append("# " + "─" * 4 + f" {g.title} " + "─" * max(2, 60 - len(g.title)))
            for line in textwrap.wrap(g.desc, width=76, break_long_words=False):
                lines.append(f"# {line}")
        lines.append("")
        _emit(lines, top_key, val, "", top_key)
    return "\n".join(lines) + "\n"


def _emit(lines: List[str], key: str, val: Any, indent: str, path: str) -> None:
    p = PARAMS_BY_PATH.get(path)
    is_map = bool(p and p.type == "map")
    if isinstance(val, dict) and not is_map:
        if p:
            lines.extend(_comment_lines(p, indent))
        lines.append(f"{indent}{key}:")
        for k2, v2 in val.items():
            _emit(lines, k2, v2, indent + "  ", f"{path}.{k2}")
        return

    lines.extend(_comment_lines(p, indent) if p else [])
    if p and p.type == "price_table":
        lines.append(f"{indent}{key}:")
        for model, table in (val or {}).items():
            lines.append(f"{indent}  {model}:")
            for bucket in ("cache_hit", "cache_miss", "output"):
                row = (table or {}).get(bucket) or {}
                lines.append(f"{indent}    {bucket}: {{ peak: {_fmt_num(row.get('peak', 0))}, "
                             f"offpeak: {_fmt_num(row.get('offpeak', 0))} }}")
        return
    if is_map:
        lines.append(f"{indent}{key}:")
        for k2, v2 in (val or {}).items():
            lines.append(f"{indent}  {k2}: {_fmt_scalar(v2, '')}")
        return
    if p and p.type == "pair_list":
        if not val:
            lines.append(f"{indent}{key}: []")
            return
        lines.append(f"{indent}{key}:")
        for pair in val:
            a = pair[0] if len(pair) > 0 else ""
            b = pair[1] if len(pair) > 1 else ""
            lines.append(f"{indent}  - [{_quote(a)}, {_quote(b)}]")
        return
    if isinstance(val, list) and p and p.type in ("int_list", "str_list", "range_list"):
        lines.append(f"{indent}{key}: {_fmt_list(val, indent)}")
        return
    if isinstance(val, list):
        lines.append(f"{indent}{key}: {_fmt_list(val, indent)}")
        return
    lines.append(f"{indent}{key}: {_fmt_scalar(val, p.type if p else '')}")


def _carry_unknown(out: Dict[str, Any], src: Dict[str, Any], prefix: str) -> None:
    """schema 没覆盖的键原样搬过去（前向兼容）。"""
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            slot = out.setdefault(k, {})
            if not isinstance(slot, dict):
                continue
            known_child = any(pp.startswith(full + ".") for pp in PARAMS_BY_PATH)
            if known_child:
                _carry_unknown(slot, v, full)
            else:
                for k2, v2 in v.items():
                    slot.setdefault(k2, v2)
        else:
            if full not in PARAMS_BY_PATH:
                out.setdefault(k, v)


def save(path: Path, cfg: Dict[str, Any], *, backup: bool = True) -> Optional[Path]:
    """写回 config.yaml。返回备份文件路径（没备份就是 None）。"""
    bak = None
    if backup and path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(cfg)
    # 原子写：先写临时文件再替换，避免中途失败留下半个文件
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return bak


# ──────────────────────────────────────────────────────────────────────
#  导入 / 导出
# ──────────────────────────────────────────────────────────────────────
def parse_import(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """解析导入的 yaml/json 文本。返回 (配置, 错误信息)。"""
    text = (text or "").strip()
    if not text:
        return None, "内容是空的"
    # 先试 JSON（JSON 是 YAML 的子集，但先试 JSON 能给出更准的报错）
    try:
        data = json.loads(text)
    except Exception:
        try:
            data = yaml.safe_load(text)
        except Exception as e:
            return None, f"解析失败：{e}"
    if data is None:
        return None, "内容是空的"
    if not isinstance(data, dict):
        return None, "顶层必须是一个对象（不能是列表或标量）"
    return _fill_defaults(data), None


def export_text(cfg: Dict[str, Any], fmt: str = "yaml", *, mask_key: bool = True) -> str:
    """导出当前配置。默认把 API Key 打码 —— 免得导出文件被人瞄一眼就泄露。"""
    data = json.loads(json.dumps(cfg))          # 深拷贝
    if mask_key:
        key = get_path(data, "llm.api_key", "")
        if key:
            set_path(data, "llm.api_key", _mask(str(key)))
        tok = get_path(data, "onebot.access_token", "")
        if tok:
            set_path(data, "onebot.access_token", _mask(str(tok)))
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return render(data)


def _mask(s: str) -> str:
    if len(s) <= 8:
        return "***"
    return s[:5] + "..." + s[-4:]


# ──────────────────────────────────────────────────────────────────────
#  校验
# ──────────────────────────────────────────────────────────────────────
def _deep_over(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """把 over 叠在 base 上（递归合并字典），返回一个新 dict。

    用途：校验一份**残缺**配置（比如导入接口只拿到 `{"llm": {...}}`）时，
    先把 schema 的默认值铺底下，这样后面所有「x > 200」之类的比较都不会
    撞上 None。否则 `get_path(cfg, "group.max_replies_per_hour") > 200`
    会直接 `TypeError: '>' not supported between instances of 'NoneType' and 'int'`。
    """
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_over(out[k], v)
        else:
            out[k] = v
    return out


def validate(cfg: Dict[str, Any], *, panel_port: int = 0,
             root: Optional[Path] = None) -> List[Dict[str, str]]:
    """返回问题列表：[{path, level, msg}]，level 是 error / warn。

    error = 不能保存（值本身不合法）
    warn  = 能保存，但这么设大概率不是你想要的

    传进来的 cfg 可以是**残缺**的（导入接口就是这种），缺的键按 schema
    默认值补齐后再校验 —— 校验的语义是「如果你现在就保存，会得到什么」。
    """
    cfg = _deep_over(defaults(), cfg)
    out: List[Dict[str, str]] = []

    def err(path: str, msg: str) -> None:
        out.append({"path": path, "level": "error", "msg": msg})

    def warn(path: str, msg: str) -> None:
        out.append({"path": path, "level": "warn", "msg": msg})

    # ---- 逐参数的范围校验 ----
    for p in ALL_PARAMS:
        if p.type in ("int", "float"):
            v = get_path(cfg, p.path)
            if v is None or isinstance(v, bool):
                err(p.path, "必须填数字")
                continue
            try:
                num = float(v)
            except (TypeError, ValueError):
                err(p.path, f"「{v}」不是合法数字")
                continue
            if p.type == "int" and abs(num - round(num)) > 1e-9:
                err(p.path, "必须是整数")
                continue
            if p.min is not None and num < p.min:
                err(p.path, f"不能小于 {_fmt_num(p.min)}{p.unit}")
            if p.max is not None and num > p.max:
                err(p.path, f"不能大于 {_fmt_num(p.max)}{p.unit}")

        elif p.type in ("int_list",):
            v = get_path(cfg, p.path) or []
            if not isinstance(v, list):
                err(p.path, "必须是列表")
                continue
            for x in v:
                if isinstance(x, bool) or not isinstance(x, int):
                    err(p.path, f"列表里只能放整数，「{x}」不是")
                    break

        elif p.type == "str_list":
            v = get_path(cfg, p.path) or []
            if not isinstance(v, list):
                err(p.path, "必须是列表")
                continue
            for x in v:
                if not isinstance(x, str):
                    err(p.path, f"列表里只能放字符串，「{x}」不是")
                    break

        elif p.type in ("range", "range_list", "pair_list"):
            v = get_path(cfg, p.path)
            if p.type in ("range", "range_list"):
                pairs = [v] if p.type == "range" else (v or [])
                if not isinstance(pairs, list):
                    err(p.path, "格式不对")
                    continue
                for pair in pairs:
                    if (not isinstance(pair, list) or len(pair) != 2
                            or not all(isinstance(i, (int, float))
                                       and not isinstance(i, bool) for i in pair)):
                        err(p.path, "每一段都必须是 [起点, 终点] 两个整数")
                        break
                    if p.type == "range_list" and pair[0] == pair[1]:
                        warn(p.path, f"[{pair[0]}, {pair[1]}] 起点终点相同，这一段等于没有")
                if p.type == "range" and len(v or []) != 2:
                    err(p.path, "必须是 [起点, 终点] 两个数")
            else:
                if not isinstance(v, list):
                    err(p.path, "必须是列表")
                    continue
                for row in v:
                    if not isinstance(row, list) or len(row) != 2:
                        err(p.path, "每一行都必须是 [正则, 回复]")
                        break
                    try:
                        re.compile(str(row[0]))
                    except re.error as e:
                        err(p.path, f"正则有语法错误：{e}")
                        break

    # ---- 跨字段的一致性 ----
    def lo_hi(a: str, b: str, label: str) -> None:
        va, vb = get_path(cfg, a), get_path(cfg, b)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va > vb:
            err(b, f"{label}的上限比下限还小（{va} > {vb}）")

    lo_hi("humanize.read_delay_min", "humanize.read_delay_max", "读延迟")
    lo_hi("humanize.typing_per_char_min", "humanize.typing_per_char_max", "打字速度")
    lo_hi("humanize.typing_min", "humanize.typing_max", "打字时长")
    lo_hi("humanize.segment_gap_min", "humanize.segment_gap_max", "分条间隔")
    lo_hi("humanize.late_reply_min", "humanize.late_reply_max", "晚回时间")

    cr = get_path(cfg, "context.compress_ratio")
    if isinstance(cr, (int, float)) and (cr < 0.1 or cr > 0.9):
        err("context.compress_ratio", "压缩比例建议在 0.1~0.9 之间（超出这个范围会丢太多细节）")

    # ---- 端口冲突 ----
    mode = get_path(cfg, "onebot.mode", "reverse")
    port = get_path(cfg, "onebot.port", 0)
    if mode == "reverse" and isinstance(port, int):
        if port < 1024:
            warn("onebot.port", f"端口 {port} 是特权端口（<1024），在 Windows 上可能绑不上")
        if panel_port and port == panel_port:
            err("onebot.port", f"和本管理面板的端口（{panel_port}）冲突了，换一个")

    # ---- 人设文件 ----
    if root is not None:
        for key, label in (("persona.file", "私聊人设"), ("persona.group_file", "群聊人设")):
            rel = get_path(cfg, key, "")
            if not rel:
                continue
            fp = Path(rel)
            if not fp.is_absolute():
                fp = root / fp
            if not fp.exists():
                if key == "persona.file":
                    err(key, f"「{rel}」不存在。{label}文件是必填的")
                else:
                    warn(key, f"「{rel}」不存在，群聊会退回私聊人设")

    # ---- 花钱相关的提醒 ----
    if get_path(cfg, "llm.thinking") is True:
        warn("llm.thinking", "★ 思考模式开着，思维链按输出 token 计费（4~8 元/百万），"
                             "开销大约是关闭时的 8 倍。陪聊场景几乎不需要它")
    if get_path(cfg, "llm.mock") is True:
        warn("llm.mock", "离线模拟开着：它不会真的调用模型，只会回固定的一句话。"
                         "想真的聊天请关掉")
    if get_path(cfg, "group.enabled") is True and not (get_path(cfg, "whitelist.groups") or []):
        warn("whitelist.groups", "群白名单是空的 = 任何群都理。别人把小号拉进陌生群，"
                                 "它就会在里面说话。建议填上要活动的群号")
    rate = get_path(cfg, "whitelist.group_reply_rate", 0) or 0
    if rate and rate > 0 and get_path(cfg, "group.address_required") is True:
        warn("whitelist.group_reply_rate",
             f"设了 {rate:.0%} 的随机插话概率，但「只在被叫到时回」是开着的，这个概率不会生效。"
             "两个都要改才行")
    if rate and rate > 0.2:
        warn("whitelist.group_reply_rate",
             f"★ {rate:.0%} 的插话概率在活跃群里大约每 5 条就要接一句，"
             "很容易被当成刷屏机器人踢出去")
    if get_path(cfg, "group.max_replies_per_hour", 30) and \
            get_path(cfg, "group.max_replies_per_hour") > 200:
        warn("group.max_replies_per_hour", "每小时 200 条以上基本等于没有上限")
    if not get_path(cfg, "memory.enabled") and get_path(cfg, "life.proactive.enabled"):
        warn("life.proactive.enabled",
             "长期记忆是关的，主动搭话就没有由头可用 —— 它永远不会主动开口")
    if get_path(cfg, "life.proactive.enabled") and \
            not (get_path(cfg, "life.proactive.topics_from_memory") or 0):
        warn("life.proactive.topics_from_memory",
             "没勾任何由头，主动搭话会因为没有话题而放弃")
    # 「太黏人」这一组是踩过坑才加的：min_idle_hours 调到 0.1（6 分钟）、
    # max_per_day 调到 17，它就会整天对着同一个人自言自语刷屏。
    if get_path(cfg, "life.proactive.enabled"):
        idle = get_path(cfg, "life.proactive.min_idle_hours")
        per_day = get_path(cfg, "life.proactive.max_per_day")
        every = get_path(cfg, "life.proactive.min_hours_between")
        if isinstance(idle, (int, float)) and idle < 1:
            warn("life.proactive.min_idle_hours",
                 f"只安静 {idle} 小时就开口。真人不会隔一会儿就来找你一次，"
                 f"这个值很容易显得黏人，建议至少 3 小时")
        if isinstance(per_day, (int, float)) and per_day > 3:
            warn("life.proactive.max_per_day",
                 f"一天主动 {int(per_day)} 次。超过 3 次就很像推销号了"
                 f"（真人没事不会一天问你八遍「在干嘛」）")
        if isinstance(every, (int, float)) and every <= 0 and \
                isinstance(per_day, (int, float)) and per_day > 1:
            warn("life.proactive.min_hours_between",
                 "两次之间没有间隔限制，它会连着来 —— 设成 2 小时以上更自然")
        if not get_path(cfg, "life.proactive.targets") and \
                not get_path(cfg, "whitelist.masters"):
            warn("life.proactive.targets",
                 "没指定搭话对象、也没配主人：聊过的私聊对象都可能被它主动打扰。"
                 "想只让它找你，就在「谁能跟它说话 → 主人 QQ 号」里填上你自己")
    if get_path(cfg, "llm.model") == "deepseek-v4-pro":
        warn("llm.model", "v4-pro 比 flash 贵 4~6 倍，而且不支持看图。陪聊一般用不上")
    if get_path(cfg, "budget.daily_yuan", 0) == 0:
        warn("budget.daily_yuan", "日预算是 0，启动后会立刻进入硬停状态，一条都不回")
    key = str(get_path(cfg, "llm.api_key", "") or "")
    if not key and not get_path(cfg, "llm.mock"):
        warn("llm.api_key", "没填 API Key 也没开离线模拟，启动后会报错。"
                            "去 platform.deepseek.com 申请一个")
    elif key and not key.startswith("sk-"):
        warn("llm.api_key", "DeepSeek 的 Key 通常以 sk- 开头，确认一下有没有复制错")

    # 去重（同一个 path+msg 只报一次）
    seen = set()
    uniq = []
    for o in out:
        k = (o["path"], o["msg"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(o)
    # error 排前面
    uniq.sort(key=lambda o: (o["level"] != "error", o["path"]))
    return uniq


# ──────────────────────────────────────────────────────────────────────
#  热更新 / 需重启 的差异分析
# ──────────────────────────────────────────────────────────────────────
def diff_paths(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """哪些参数变了。"""
    changed: List[str] = []
    for p in ALL_PARAMS:
        a = get_path(old, p.path)
        b = get_path(new, p.path)
        if a != b:
            changed.append(p.path)
    return changed


def classify(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """返回 (可热更新的改动, 必须重启的改动)。"""
    hot: List[str] = []
    cold: List[str] = []
    for path in diff_paths(old, new):
        p = PARAMS_BY_PATH.get(path)
        if p is None:
            continue
        (cold if p.restart else hot).append(path)
    return hot, cold
