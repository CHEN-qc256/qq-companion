"""从 panel/schema.py 生成参数对照表（Markdown）。

schema.py 是参数的唯一真源，所以这张表也由它生成 —— 手写 82 行迟早会跟代码分家。

    python tools/param_table.py                 # 打印到屏幕
    python tools/param_table.py --write         # 写进 参数对照表.md
    python tools/param_table.py --write --out 别的名字.md
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: F401,E402  统一 UTF-8 输出 + 行缓冲

from panel import schema  # noqa: E402

OUT_DEFAULT = ROOT / "参数对照表.md"

_TYPE_CN = {
    "str": "文本",
    "text": "多行文本",
    "password": "密码",
    "path": "文件路径",
    "int": "整数",
    "float": "小数",
    "bool": "开关",
    "enum": "下拉选择",
    "int_list": "整数列表",
    "str_list": "字符串列表",
    "range": "区间",
    "range_list": "区间列表",
    "pair_list": "键值对列表",
    "map": "映射表",
    "price_table": "计价表",
}


def show_default(p: schema.Param) -> str:
    """把默认值渲染成一句人话，太长就截断。"""
    v = p.default
    if isinstance(v, bool):
        return "开" if v else "关"
    if v is None:
        return "—"
    if isinstance(v, str):
        if p.type == "password":
            return "（空）" if not v else "***"
        return f"`{v}`" if v else "（空）"
    if isinstance(v, list):
        if not v:
            return "（空）"
        if all(isinstance(x, (int, float)) for x in v):
            return "`" + ", ".join(str(x) for x in v) + "`"
        if all(isinstance(x, list) for x in v):
            return "`" + "; ".join(
                f"{a}–{b}" for a, b in (x for x in v if len(x) == 2)
            ) + "`"
        s = ", ".join(str(x) for x in v)
        return f"`{s}`" if len(s) <= 40 else f"`{s[:37]}…`（{len(v)} 项）"
    if isinstance(v, dict):
        if not v:
            return "（空）"
        if p.type == "price_table":
            return f"{len(v)} 个模型"
        s = ", ".join(f"{k}={v[k]}" for k in list(v)[:3])
        return f"`{s}`" if len(s) <= 40 else f"`{s[:37]}…`"
    return f"`{v}`"


def show_range(p: schema.Param) -> str:
    if p.min is None and p.max is None:
        return "—"
    lo = "" if p.min is None else f"{p.min:g}"
    hi = "" if p.max is None else f"{p.max:g}"
    if p.step:
        return f"{lo} ~ {hi}（步进 {p.step:g}）"
    return f"{lo} ~ {hi}"


def show_choices(p: schema.Param) -> str:
    if not p.choices:
        return "—"
    return " / ".join(str(c.get("label", c.get("value", ""))) for c in p.choices)


def esc(s: str) -> str:
    """表格里的竖线要转义，否则会把列切断。"""
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def render() -> str:
    lines: list[str] = []
    add = lines.append

    add("# qq-companion 参数对照表")
    add("")
    add("这张表由 `python tools/param_table.py --write` 从 `panel/schema.py` 生成，")
    add("**不要手改** —— 参数改了就跑一遍这个命令。")
    add("")
    add(f"共 **{len(schema.ALL_PARAMS)}** 个参数，分 **{len(schema.GROUPS)}** 组。")
    add("")
    add("「是否需重启」列的含义：")
    add("")
    add("- **不用** —— 在管理面板上点保存，机器人自己会在一两秒内读进新值（热更新）。")
    add("- **要** —— 必须点一下面板上的「重启」才生效。这类一般是启动时就固定死的东西，")
    add("  比如 API Key、端口、数据库路径、日志文件名。")
    add("")

    # 目录
    add("## 目录")
    add("")
    for g in schema.GROUPS:
        params = [p for p in schema.ALL_PARAMS if p.group == g.id]
        add(f"- [{g.title}](#{g.id})（{len(params)} 个）")
    add("")

    for g in schema.GROUPS:
        params = [p for p in schema.ALL_PARAMS if p.group == g.id]
        if not params:
            continue
        add(f"## {g.title}")
        add("")
        add(f"> {g.desc}")
        add("")
        add("| 参数名 | 配置文件里的键 | 类型 | 默认值 | 取值范围 | 含义 | 是否需重启 |")
        add("|---|---|---|---|---|---|---|")
        for p in params:
            flag = "**要**" if p.restart else "不用"
            add(
                "| {label} | `{path}` | {ty} | {dflt} | {rng} | {desc} | {flag} |".format(
                    label=esc(p.label),
                    path=esc(p.path),
                    ty=_TYPE_CN.get(p.type, p.type),
                    dflt=esc(show_default(p)),
                    rng=esc(show_range(p) if not p.choices else show_choices(p)),
                    desc=esc(p.desc),
                    flag=flag,
                )
            )
        add("")

        # 有补充说明或候选项的，跟在表后面单独列，免得把表格撑变形
        extra = [p for p in params if p.note]
        if extra:
            add("**补充说明**")
            add("")
            for p in extra:
                add(f"- `{p.path}` —— {p.note}")
            add("")

    add("---")
    add("")
    add("## 改参数的三种方式")
    add("")
    add("1. **管理面板**（推荐）：双击 `panel.bat`，浏览器里改完点保存。")
    add("   面板会把注释一并写回去，生成的配置文件还是一份能直接看懂的 YAML。")
    add("2. **直接改 `config.yaml`**：改完重启机器人，或者在面板上点一下「重启」。")
    add("3. **让机器人重读**：改完 `config.yaml` 后，机器人会在一两秒内自己发现文件变了并加载，")
    add("   热更新类参数立刻生效，需重启类的会在日志里提醒你去点重启。")
    add("")
    add("> 面板保存前会做一遍校验：不合法的值会标红并且拒绝保存，")
    add("> 有些「合法但大概不是你想要的」设置（比如开着思考模式、群聊没限定群号）会给黄色提醒。")
    add("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    do_write = "--write" in args
    out = OUT_DEFAULT
    if "--out" in args:
        i = args.index("--out")
        if i + 1 < len(args):
            out = Path(args[i + 1])
            if not out.is_absolute():
                out = ROOT / out

    text = render()
    if do_write:
        out.write_text(text, encoding="utf-8")
        print(f"✓ 已写入 {out}（{len(text)} 字符，{len(schema.ALL_PARAMS)} 个参数）")
    else:
        print(text)
        print(f"（想写进文件就加 --write，默认输出 {OUT_DEFAULT.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
