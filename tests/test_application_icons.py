import os
import struct
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


ROOT_DIR = Path(__file__).resolve().parents[1]
EXPECTED_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
APPLICATION_ICONS = (
    ROOT_DIR / "Client" / "assets" / "icons" / "sc-client.ico",
    ROOT_DIR / "Trader_Server" / "assets" / "icons" / "trader-server.ico",
)
APPLICATION_ASSETS = (
    ROOT_DIR / "Client" / "assets" / "icons" / "sc-client.svg",
    ROOT_DIR / "Client" / "assets" / "icons" / "sc-client.png",
    ROOT_DIR / "Client" / "assets" / "icons" / "sc-client.ico",
    ROOT_DIR / "Trader_Server" / "assets" / "icons" / "trader-server.svg",
    ROOT_DIR / "Trader_Server" / "assets" / "icons" / "trader-server.png",
    ROOT_DIR / "Trader_Server" / "assets" / "icons" / "trader-server.ico",
)


def _ico_sizes(path: Path) -> tuple[int, ...]:
    data = path.read_bytes()
    reserved, image_type, image_count = struct.unpack_from("<HHH", data)
    if reserved != 0 or image_type != 1:
        raise ValueError(f"invalid ICO header: {path}")

    sizes = []
    for index in range(image_count):
        width, height = struct.unpack_from("BB", data, 6 + index * 16)
        sizes.append((width or 256, height or 256))
    if any(width != height for width, height in sizes):
        raise ValueError(f"non-square ICO frame: {path}")
    return tuple(width for width, _height in sizes)


class ApplicationIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_official_application_icon_assets_exist(self):
        for path in APPLICATION_ASSETS:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

    def test_application_icons_contain_expected_windows_sizes(self):
        for path in APPLICATION_ICONS:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())
                self.assertEqual(_ico_sizes(path), EXPECTED_ICON_SIZES)

    def test_qt_can_load_application_icons(self):
        for path in APPLICATION_ICONS:
            with self.subTest(path=path):
                icon = QIcon(str(path))
                self.assertFalse(icon.isNull())
                self.assertEqual(
                    tuple(size.width() for size in icon.availableSizes()),
                    EXPECTED_ICON_SIZES,
                )


if __name__ == "__main__":
    unittest.main()
