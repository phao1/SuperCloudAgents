from pathlib import Path

from app import FAREWELL, GREETING, add

assert add(-2, 3) == 1
source = Path("app.py").read_text(encoding="utf-8")
# The task asks to keep the existing Chinese text: escapes like 你 do not count.
for text in ("你好，世界", "下次再见"):
    assert f'"{text}"' in source, f"app.py 中的中文文本 {text!r} 没有原样保留"
assert GREETING == "你好，世界", "中文问候文本被文件工具损坏"
assert FAREWELL == "下次再见", "中文告别文本被文件工具损坏"
print("ok")
