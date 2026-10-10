"""demo_app.py 回归测试：演示页全脚本执行无异常（streamlit 未安装时跳过）。"""

import pytest

pytest.importorskip("streamlit")

# AppTest.from_file 的相对路径按调用文件所在目录解析，这里给仓库内绝对路径
_APP_PATH = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "atlas" / "demo_app.py")


def test_demo_app_renders_without_exception():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(_APP_PATH, default_timeout=120)
    at.run()
    assert not at.exception
