"""启动评测可视化服务（独立端口 8787）。

用法:
    python -m evalview.server
    # 或
    python scripts/run_evalview.py

依赖: fastapi, uvicorn（后端已有）
"""
from evalview.server import main

if __name__ == "__main__":
    main()
