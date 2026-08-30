import os
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QMessageBox, QPushButton

from Client.ui_qt import theme
from Client.ui_qt.main_window import DuplicateLoginDialog, ManagerLoginDialog, SessionActionDialog


class ClientPopupStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _dispose(widget):
        widget.close()
        widget.deleteLater()

    def test_session_action_dialog_uses_dark_readable_theme(self):
        dialog = SessionActionDialog(
            title="Confirm",
            message="The current session will be disconnected.",
            confirm_text="Confirm",
        )
        try:
            self.app.processEvents()
            self.assertEqual(dialog.objectName(), "sessionActionDialog")
            self.assertEqual(dialog.styleSheet(), theme.POPUP_QSS)
            self.assertEqual(
                dialog.palette().color(dialog.backgroundRole()).name().upper(),
                theme.PANEL_BG,
            )
            buttons = dialog.findChildren(QPushButton)
            self.assertEqual(
                {button.objectName() for button in buttons},
                {"dialogCancelButton", "dialogConfirmButton"},
            )
            self.assertIsNotNone(dialog.findChild(QPushButton, "dialogConfirmButton"))
            self.assertIsNotNone(dialog.findChild(QPushButton, "dialogCancelButton"))
        finally:
            self._dispose(dialog)

    def test_duplicate_login_dialog_keeps_session_action_behavior_and_theme(self):
        dialog = DuplicateLoginDialog()
        try:
            self.assertIsInstance(dialog, SessionActionDialog)
            self.assertEqual(dialog.objectName(), "sessionActionDialog")
            self.assertEqual(dialog.styleSheet(), theme.POPUP_QSS)
            self.assertTrue(dialog.isModal())
        finally:
            self._dispose(dialog)

    def test_manager_login_dialog_uses_popup_theme_without_changing_controls(self):
        dialog = ManagerLoginDialog(startup=False)
        try:
            self.app.processEvents()
            self.assertEqual(dialog.objectName(), "managerLoginDialog")
            self.assertEqual(dialog.styleSheet(), theme.POPUP_QSS)
            self.assertEqual(len(dialog.findChildren(QLineEdit)), 2)
            button_box = dialog.findChild(QDialogButtonBox, "dialogButtonBox")
            self.assertIsNotNone(button_box)
            ok_button = button_box.button(QDialogButtonBox.Ok)
            cancel_button = button_box.button(QDialogButtonBox.Cancel)
            self.assertIsNotNone(ok_button)
            self.assertIsNotNone(cancel_button)
            self.assertEqual(ok_button.objectName(), "loginConfirmButton")
            self.assertEqual(cancel_button.objectName(), "dialogCancelButton")
        finally:
            self._dispose(dialog)

    def test_all_popup_styles_are_defined_from_shared_theme_tokens(self):
        self.assertIn(f"background: {theme.PANEL_BG};", theme.POPUP_QSS)
        self.assertIn(f"color: {theme.TEXT_PRIMARY};", theme.POPUP_QSS)
        self.assertIn(f"background: {theme.TOAST_BG};", theme.TOAST_QSS)
        self.assertIn("QWidget#settingsOverlay", theme.APP_QSS)
        self.assertIn(f"color: {theme.TEXT_MUTED};", theme.APP_QSS)
        self.assertIn("QListView#comboPopup::item:disabled", theme.APP_QSS)

        message_box = QMessageBox()
        try:
            message_box.setStyleSheet(theme.POPUP_QSS)
            message_box.ensurePolished()
            self.assertEqual(message_box.styleSheet(), theme.POPUP_QSS)
        finally:
            self._dispose(message_box)
