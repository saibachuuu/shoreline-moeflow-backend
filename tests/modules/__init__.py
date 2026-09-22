"""模块测试的共享守卫。

`docs/optional-modules.md` §4 检查项 11 要求：**零模块时整个测试套件仍然全绿**。

但模块测试天然会 import 模块（`app.modules.ziteng_partner...`）。模块目录一旦被删掉
（这正是「目录即开关」的关闭方式），这些 import 就会在 **collection 阶段**抛
`ModuleNotFoundError`，pytest 报收集错误并中断——整个套件变红，而不是优雅跳过。

本模块提供 `requires_module()`：在收集期检测模块是否存在，不存在就整文件 skip。
用法（放在测试文件**最顶部**，必须早于任何模块 import）：

    from tests.modules import requires_module
    requires_module("ziteng_partner")

    from app.modules.ziteng_partner import client  # 只有模块存在时才会执行到这里

这样「关掉模块」得到一个干净的 skip，而不是一堆收集错误。
"""

from __future__ import annotations

from pathlib import Path

import pytest

__all__ = ["requires_module", "module_exists"]

# backend 根目录：tests/modules/__init__.py -> tests -> backend
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


def module_exists(name: str) -> bool:
    """模块目录是否存在（与 `app/modules.discover()` 的判定口径一致）。

    只做**文件系统**判断，不 import——collection 阶段要避免副作用。
    """
    return (_BACKEND_ROOT / "app" / "modules" / name / "__init__.py").is_file()


def requires_module(name: str) -> None:
    """模块不存在时，在收集期跳过当前测试文件。

    在模块级调用；`allow_module_level=True` 让 pytest 立刻跳过整个文件。
    """
    if not module_exists(name):
        pytest.skip(
            f"可选模块 {name!r} 未启用（目录不存在），跳过其测试",
            allow_module_level=True,
        )
