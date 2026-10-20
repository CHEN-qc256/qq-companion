"""让 `python -m panel` 能直接跑起来。"""
from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
