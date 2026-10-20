"""qq-companion 的可视化配置管理面板。

纯 Python 标准库 + 原生 HTML/JS —— 不装 Node、不装 npm 包、不需要构建。
启动方式：

    python -m panel              # 或者双击 panel.bat

它做五件事：
  1. 把 config.yaml 的全部参数渲染成表单，每个参数带中文说明、校验、导入导出
  2. 启停/重启机器人本体（复用 main.py 的 pidfile 逻辑，不重复实现）
  3. 实时日志（轮询 data/companion.log 的增量部分）
  4. 花费 / 长期记忆 / 已绑定的人（直接读那个 sqlite，只读）
  5. 声音（GPT-SoVITS）：试听、换音色、上传参考音频做零样本克隆
"""
__all__ = ["schema", "configio", "runner", "server", "voice"]
__version__ = "1.0.0"
