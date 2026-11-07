"""兼容入口：使用 streamlit run main.py 启动，实际界面位于 apps 中。"""
from pathlib import Path
import runpy

# 以入口文件位置解析路径，避免工作目录不同导致找不到实际界面脚本。
runpy.run_path(str(Path(__file__).resolve().parent / "apps" / "streamlit_app.py"), run_name="__main__")
