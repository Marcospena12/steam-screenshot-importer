from __future__ import annotations

import sys
from pathlib import Path

# Adiciona src ao path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from steamshotbridge.app.widgets import create_app_logo_pixmap


def generate_assets() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    assets_dir = Path(__file__).resolve().parents[1] / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    pil_images = []

    for sz in sizes:
        pix = create_app_logo_pixmap(size=sz, dark_mode=True)
        qimg = pix.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        width = qimg.width()
        height = qimg.height()
        ptr = qimg.constBits()
        # Converte para PIL Image
        img = Image.frombuffer("RGBA", (width, height), ptr, "raw", "RGBA", 0, 1)
        pil_images.append(img)
        if sz == 256:
            img.save(assets_dir / "logo_256.png", "PNG")

    # Salva .ico com múltiplos tamanhos
    ico_path = assets_dir / "steamshotbridge.ico"
    pil_images[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in [16, 24, 32, 48, 64, 128, 256]],
        append_images=pil_images[:-1],
    )
    print(f"Assets gerados em: {assets_dir}")
    print(f" - {ico_path} ({ico_path.stat().st_size} bytes)")
    print(f" - {assets_dir / 'logo_256.png'}")


if __name__ == "__main__":
    generate_assets()
