"""
Картинка по пятой полевой паре — «18 Zoe Street, Staten Island» (``docs/primer5i.zip``).

Тонкая обёртка: вся отрисовка живёт в ``docs/make_pair_preview.py`` (там же параметры пар),
этот файл оставлен, потому что на него ссылаются документация и README шага 4.

    python docs/make_zoe_preview.py      # → docs/step4_zoe_preview.png

Нужны ``pymupdf`` и ``Pillow``; matplotlib не требуется.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_pair_preview import main  # noqa: E402

if __name__ == "__main__":
    main(["zoe"])
