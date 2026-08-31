# 依赖：
#   pip install PyQt6 charset-normalizer pycryptodome

import codecs
import re
import sys
import threading
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes

from charset_normalizer import from_bytes
from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QFont, QKeySequence, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class LargeFileLoader(QObject):
    encoding_detected = pyqtSignal(str)
    chunk_ready = pyqtSignal(str, object)
    loading_finished = pyqtSignal(bool, str)

    def __init__(
        self,
        path,
        chunk_size,
        sample_size,
        encoding_detector,
    ):
        super().__init__()

        self.path = path
        self.chunk_size = chunk_size
        self.sample_size = sample_size
        self.encoding_detector = encoding_detector
        self.cancelled = False
        self.chunk_inserted = threading.Event()

    def cancel(self):
        self.cancelled = True
        self.chunk_inserted.set()

    def allow_next_chunk(self):
        self.chunk_inserted.set()

    def wait_for_chunk_insert(self):
        while not self.cancelled:
            if self.chunk_inserted.wait(0.1):
                self.chunk_inserted.clear()
                return True

        return False

    def send_chunk(self, text, position):
        if not text:
            return not self.cancelled

        self.chunk_inserted.clear()
        self.chunk_ready.emit(text, position)
        return self.wait_for_chunk_insert()

    def run(self):
        try:
            with self.path.open("rb") as file:
                sample = file.read(self.sample_size)
                file.seek(0)

                encoding = self.encoding_detector(sample)
                decoder_factory = codecs.getincrementaldecoder(encoding)
                decoder = decoder_factory(errors="replace")
                self.encoding_detected.emit(encoding)

                while not self.cancelled:
                    chunk = file.read(self.chunk_size)

                    if not chunk:
                        break

                    text = decoder.decode(chunk, final=False)

                    if not self.send_chunk(text, file.tell()):
                        self.loading_finished.emit(
                            False,
                            "用户取消了大文件读取。",
                        )
                        return

                if self.cancelled:
                    self.loading_finished.emit(
                        False,
                        "用户取消了大文件读取。",
                    )
                    return

                final_text = decoder.decode(b"", final=True)

                if not self.send_chunk(final_text, file.tell()):
                    self.loading_finished.emit(
                        False,
                        "用户取消了大文件读取。",
                    )
                    return
        except (OSError, LookupError) as error:
            self.loading_finished.emit(False, str(error))
            return

        self.loading_finished.emit(True, "")


class RedNotepad(QMainWindow):
    LARGE_FILE_THRESHOLD = 8 * 1024 * 1024
    READ_CHUNK_SIZE = 256 * 1024
    ENCODING_SAMPLE_SIZE = 1024 * 1024
    MAX_HIGHLIGHTS = 2000
    MAX_OCCURRENCE_HIGHLIGHTS = 3000
    MAX_COLLECTED_MATCHES = 100000
    SENSITIVE_MAGIC = b"REDNOTEPAD-GCM-256-V1\n"
    SENSITIVE_EXTENSION = ".rns"
    SENSITIVE_KDF_ITERATIONS = 600000
    SENSITIVE_SALT_SIZE = 16
    SENSITIVE_NONCE_SIZE = 12
    SENSITIVE_TAG_SIZE = 16

    def __init__(self):
        super().__init__()

        self.current_file = None
        self.current_encoding = "utf-8"
        self.current_match_span = None
        self.font_size = 18
        self.sensitive_mode = False
        self.sensitive_password = None
        self.current_file_is_sensitive = False

        self.loading_thread = None
        self.loading_worker = None
        self.loading_path = None
        self.loading_size = 0

        self.search_highlight_selections = []
        self.occurrence_highlight_selections = []
        self.selection_highlight_timer = QTimer(self)
        self.selection_highlight_timer.setSingleShot(True)
        self.selection_highlight_timer.setInterval(140)
        self.selection_highlight_timer.timeout.connect(
            self.update_selected_occurrence_highlights
        )

        self.search_dialog_animation = None
        self.loader_opacity_animation = None
        self.progress_animation = None

        self.setWindowTitle("未命名 - RedNotepad")
        self.resize(1200, 800)
        self.setAcceptDrops(True)

        self.create_editor()
        self.create_search_dialog()
        self.create_loader_panel()
        self.create_layout()
        self.create_actions()
        self.create_menu()
        self.create_status_bar()
        self.apply_style()

        self.loader_panel.hide()

    # ------------------------------------------------------------
    # 编辑器
    # ------------------------------------------------------------

    def create_editor(self):
        self.editor = QPlainTextEdit()

        font = QFont("Consolas")
        font.setPointSize(self.font_size)

        self.editor.setFont(font)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.textChanged.connect(self.document_changed)
        self.editor.selectionChanged.connect(
            self.schedule_selected_occurrence_highlight
        )

    # ------------------------------------------------------------
    # 查找弹窗
    # ------------------------------------------------------------

    def create_search_dialog(self):
        self.search_dialog = QDialog(self)
        self.search_dialog.setObjectName("searchDialog")
        self.search_dialog.setWindowTitle("查找与替换")
        self.search_dialog.setModal(False)
        self.search_dialog.setMinimumWidth(900)
        self.search_dialog.rejected.connect(self.search_dialog_closed)

        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("查找内容")

        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("替换为")

        self.search_mode = QComboBox()
        self.search_mode.addItem("普通文本", "normal")
        self.search_mode.addItem("正则表达式", "regex")
        self.search_mode.addItem("通配符", "wildcard")

        self.regex_behavior = QComboBox()
        self.regex_behavior.addItem("re.finditer", "finditer")
        self.regex_behavior.addItem("re.search", "search")
        self.regex_behavior.addItem("re.findall", "findall")
        self.regex_behavior.addItem("re.match", "match")
        self.regex_behavior.addItem("re.fullmatch", "fullmatch")
        self.regex_behavior.setEnabled(False)

        self.wildcard_behavior = QComboBox()
        self.wildcard_behavior.addItem("最短匹配", "shortest")
        self.wildcard_behavior.addItem("最长匹配", "longest")
        self.wildcard_behavior.setEnabled(False)

        self.case_sensitive = QCheckBox("区分大小写")

        self.previous_button = QPushButton("上一个（Shift+F3）")
        self.next_button = QPushButton("下一个（F3）")
        self.replace_button = QPushButton("替换")
        self.replace_all_button = QPushButton("全部替换")
        self.close_search_button = QPushButton("关闭")
        self.result_label = QLabel("")

        self.previous_button.clicked.connect(self.find_previous)
        self.next_button.clicked.connect(self.find_next)
        self.replace_button.clicked.connect(self.replace_current)
        self.replace_all_button.clicked.connect(self.replace_all)
        self.close_search_button.clicked.connect(self.search_dialog.reject)

        self.find_edit.returnPressed.connect(self.find_next)
        self.find_edit.textChanged.connect(self.search_settings_changed)
        self.search_mode.currentIndexChanged.connect(self.search_mode_changed)
        self.regex_behavior.currentIndexChanged.connect(self.search_settings_changed)
        self.wildcard_behavior.currentIndexChanged.connect(self.search_settings_changed)
        self.case_sensitive.stateChanged.connect(self.search_settings_changed)

        find_layout = QHBoxLayout()
        find_layout.addWidget(QLabel("查找"))
        find_layout.addWidget(self.find_edit, 1)

        replace_layout = QHBoxLayout()
        replace_layout.addWidget(QLabel("替换"))
        replace_layout.addWidget(self.replace_edit, 1)

        option_layout = QHBoxLayout()
        option_layout.addWidget(QLabel("匹配方式"))
        option_layout.addWidget(self.search_mode)
        option_layout.addWidget(self.regex_behavior)
        option_layout.addWidget(self.wildcard_behavior)
        option_layout.addWidget(self.case_sensitive)
        option_layout.addStretch(1)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.result_label)
        button_layout.addStretch(1)
        button_layout.addWidget(self.previous_button)
        button_layout.addWidget(self.next_button)
        button_layout.addWidget(self.replace_button)
        button_layout.addWidget(self.replace_all_button)
        button_layout.addWidget(self.close_search_button)

        layout = QVBoxLayout(self.search_dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addLayout(find_layout)
        layout.addLayout(replace_layout)
        layout.addLayout(option_layout)
        layout.addLayout(button_layout)

    def search_mode_changed(self):
        mode = self.search_mode.currentData()

        self.regex_behavior.setEnabled(mode == "regex")
        self.wildcard_behavior.setEnabled(mode == "wildcard")

        self.search_settings_changed()

    def search_settings_changed(self):
        self.current_match_span = None

        if self.loading_worker is not None:
            return

        self.update_match_count()

    # ------------------------------------------------------------
    # 加载面板
    # ------------------------------------------------------------

    def create_loader_panel(self):
        self.loader_panel = QWidget()

        self.loader_opacity_effect = QGraphicsOpacityEffect(self.loader_panel)
        self.loader_opacity_effect.setOpacity(0.0)
        self.loader_panel.setGraphicsEffect(self.loader_opacity_effect)

        self.loading_label = QLabel("正在分块读取文件…")

        self.loading_progress = QProgressBar()
        self.loading_progress.setRange(0, 1000)
        self.loading_progress.setValue(0)
        self.loading_progress.setTextVisible(True)

        self.cancel_loading_button = QPushButton("取消")
        self.cancel_loading_button.clicked.connect(self.cancel_large_file_loading)

        layout = QHBoxLayout(self.loader_panel)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(self.loading_label)
        layout.addWidget(self.loading_progress, 1)
        layout.addWidget(self.cancel_loading_button)

    # ------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------

    def create_layout(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self.loader_panel)
        layout.addWidget(self.editor, 1)

        self.setCentralWidget(container)

    # ------------------------------------------------------------
    # 操作与菜单
    # ------------------------------------------------------------

    def create_actions(self):
        self.new_action = QAction("新建", self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_action.triggered.connect(self.new_file)

        self.open_action = QAction("打开", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.open_file)

        self.open_sensitive_action = QAction("打开敏感资料", self)
        self.open_sensitive_action.setShortcut("Ctrl+Alt+O")
        self.open_sensitive_action.triggered.connect(self.open_sensitive_file)

        self.save_action = QAction("保存", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.triggered.connect(self.save_file)

        self.save_as_action = QAction("另存为", self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.triggered.connect(self.save_file_as)

        self.sensitive_mode_action = QAction("敏感资料模式", self)
        self.sensitive_mode_action.setShortcut("Ctrl+Alt+E")
        self.sensitive_mode_action.triggered.connect(self.enter_sensitive_mode)

        self.exit_action = QAction("退出", self)
        self.exit_action.triggered.connect(self.close)

        self.undo_action = QAction("撤销", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.editor.undo)

        self.redo_action = QAction("重做", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.editor.redo)

        self.cut_action = QAction("剪切", self)
        self.cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        self.cut_action.triggered.connect(self.editor.cut)

        self.copy_action = QAction("复制", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self.editor.copy)

        self.paste_action = QAction("粘贴", self)
        self.paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        self.paste_action.triggered.connect(self.editor.paste)

        self.select_all_action = QAction("全选", self)
        self.select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        self.select_all_action.triggered.connect(self.editor.selectAll)

        self.find_action = QAction("查找", self)
        self.find_action.setShortcut(QKeySequence.StandardKey.Find)
        self.find_action.triggered.connect(self.show_find)

        self.replace_action = QAction("替换", self)
        self.replace_action.setShortcut("Ctrl+H")
        self.replace_action.triggered.connect(self.show_replace)

        self.find_next_action = QAction("查找下一个", self)
        self.find_next_action.setShortcut("F3")
        self.find_next_action.triggered.connect(self.find_next)

        self.find_previous_action = QAction("查找上一个", self)
        self.find_previous_action.setShortcut("Shift+F3")
        self.find_previous_action.triggered.connect(self.find_previous)

        self.addAction(self.find_next_action)
        self.addAction(self.find_previous_action)
        self.search_dialog.addAction(self.find_next_action)
        self.search_dialog.addAction(self.find_previous_action)

        self.remove_extra_line_breaks_action = QAction("删除多余换行", self)
        self.remove_extra_line_breaks_action.triggered.connect(
            self.remove_extra_line_breaks
        )

        self.englishize_action = QAction("英文化", self)
        self.englishize_action.triggered.connect(self.englishize_punctuation)

        self.wrap_action = QAction("自动换行", self)
        self.wrap_action.setCheckable(True)
        self.wrap_action.triggered.connect(self.toggle_word_wrap)

        self.sponsor_action = QAction("赞助", self)
        self.sponsor_action.triggered.connect(self.show_sponsor)

    def create_menu(self):
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction(self.new_action)
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.open_sensitive_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)
        file_menu.addAction(self.sensitive_mode_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        edit_menu = self.menuBar().addMenu("编辑")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.cut_action)
        edit_menu.addAction(self.copy_action)
        edit_menu.addAction(self.paste_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.select_all_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.find_action)
        edit_menu.addAction(self.replace_action)
        edit_menu.addSeparator()

        helper_menu = edit_menu.addMenu("辅助处理")
        helper_menu.addAction(self.remove_extra_line_breaks_action)
        helper_menu.addAction(self.englishize_action)

        view_menu = self.menuBar().addMenu("查看")
        view_menu.addAction(self.wrap_action)

        font_menu = view_menu.addMenu("字体大小")
        font_sizes = (10, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48)

        for size in font_sizes:
            action = QAction(f"{size} pt", self)
            action.triggered.connect(
                lambda checked=False, selected_size=size: self.set_font_size(selected_size)
            )
            font_menu.addAction(action)

        view_menu.addSeparator()
        view_menu.addAction(self.sponsor_action)

    def show_sponsor(self):
        QMessageBox.information(
            self,
            "赞助",
            "本软件采用AGPL3.0许可证发布\n\n"
            "bc1qwfd0rrptzn4vyp2qj666crg8ky50px7e4yj5pg  赞助地址。\n"
            "0x2D92f9e4D8Ac7EFfA9cD7Cd5eccd364CAC7c201B    以太坊\n\n"
            "如果你希望赞助作者。",
        )

    # ------------------------------------------------------------
    # 状态栏与字体大小
    # ------------------------------------------------------------

    def create_status_bar(self):
        self.encoding_label = QLabel("编码：UTF-8", self)
        self.file_size_label = QLabel("大小：0 B", self)
        self.encoding_label.hide()
        self.file_size_label.hide()

        self.document_stats_label = QLabel()
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().setFixedHeight(24)
        self.statusBar().addPermanentWidget(self.document_stats_label)

        self.stats_update_timer = QTimer(self)
        self.stats_update_timer.setSingleShot(True)
        self.stats_update_timer.setInterval(180)
        self.stats_update_timer.timeout.connect(self.update_document_stats)
        self.update_document_stats()

    def update_document_stats(self):
        if self.loading_worker is not None:
            return

        line_count = self.editor.document().blockCount()
        character_count = 0
        text = self.editor.toPlainText()

        for character in text:
            if character.isspace():
                continue

            character_count += 1

        self.document_stats_label.setText(
            f"行数：{line_count}  总字数：{character_count}"
        )

    def set_font_size(self, size):
        self.font_size = size
        font = self.editor.font()
        font.setPointSize(size)
        self.editor.setFont(font)

    # ------------------------------------------------------------
    # 辅助处理
    # ------------------------------------------------------------

    def remove_extra_line_breaks(self):
        text = self.editor.toPlainText()
        cleaned_lines = []

        for line in text.split("\n"):
            if not line.strip():
                continue

            cleaned_lines.append(line)

        cleaned_text = "\n".join(cleaned_lines)

        if cleaned_text == text:
            return

        cursor = self.editor.textCursor()
        old_position = cursor.position()

        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(cleaned_text)
        cursor.endEditBlock()

        maximum_position = self.editor.document().characterCount() - 1
        cursor.setPosition(min(old_position, maximum_position))
        self.editor.setTextCursor(cursor)
        self.editor.document().setModified(True)

    def englishize_punctuation(self):
        text = self.editor.toPlainText()
        punctuation_map = str.maketrans({
            "，": ",",
            "。": ".",
            "、": ",",
            "；": ";",
            "：": ":",
            "？": "?",
            "！": "!",
            "“": '"',
            "”": '"',
            "‘": "'",
            "’": "'",
            "「": '"',
            "」": '"',
            "『": '"',
            "』": '"',
            "（": "(",
            "）": ")",
            "【": "[",
            "】": "]",
            "〔": "[",
            "〕": "]",
            "［": "[",
            "］": "]",
            "｛": "{",
            "｝": "}",
            "《": "<",
            "》": ">",
            "〈": "<",
            "〉": ">",
            "～": "~",
            "·": ".",
            "⋯": "...",
            "…": "...",
            "—": "-",
            "﹐": ",",
            "﹑": ",",
            "﹒": ".",
            "﹔": ";",
            "﹕": ":",
            "﹖": "?",
            "﹗": "!",
            "＂": '"',
            "＇": "'",
        })

        english_text = text.replace("……", "...")
        english_text = english_text.replace("——", "--")
        english_text = english_text.translate(punctuation_map)
        english_text = re.sub(
            r"[ \t\u3000\u00a0]+$",
            "",
            english_text,
            flags=re.MULTILINE,
        )

        if english_text == text:
            return

        cursor = self.editor.textCursor()
        old_position = cursor.position()

        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(english_text)
        cursor.endEditBlock()

        maximum_position = self.editor.document().characterCount() - 1
        cursor.setPosition(min(old_position, maximum_position))
        self.editor.setTextCursor(cursor)
        self.editor.document().setModified(True)

    # ------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QDialog, QWidget {
                background-color: #000000;
                color: #FF0000;
            }

            QPlainTextEdit {
                background-color: #000000;
                color: #FF0000;
                border: none;
                padding: 8px;
                selection-background-color: #FFD700;
                selection-color: #000000;
            }

            QMenuBar, QMenu, QStatusBar {
                background-color: #000000;
                color: #FF0000;
            }

            QMenuBar {
                border-bottom: 1px solid #550000;
                font-size: 16px;
                padding: 3px;
            }

            QMenuBar::item {
                padding: 6px 12px;
            }

            QMenu {
                font-size: 16px;
            }

            QMenu::item {
                padding: 8px 28px 8px 14px;
            }

            QStatusBar {
                border-top: 1px solid #550000;
                font-size: 13px;
                padding: 0;
            }

            QStatusBar QLabel {
                font-size: 13px;
                padding: 0 6px;
            }

            QMenuBar::item:selected, QMenu::item:selected {
                background-color: #FFD700;
                color: #000000;
            }

            QLineEdit, QComboBox, QSpinBox {
                background-color: #000000;
                color: #FF0000;
                border: 1px solid #880000;
                border-radius: 3px;
                padding: 7px;
                font-size: 16px;
                selection-background-color: #FFD700;
                selection-color: #000000;
            }

            QLineEdit:focus {
                border: 1px solid #FF0000;
            }

            QPushButton {
                background-color: #110000;
                color: #FF0000;
                border: 1px solid #880000;
                border-radius: 3px;
                padding: 7px 12px;
                font-size: 16px;
            }

            QPushButton:hover {
                background-color: #330000;
            }

            QPushButton:pressed {
                background-color: #FFD700;
                color: #000000;
            }

            QComboBox QAbstractItemView {
                background-color: #000000;
                color: #FF0000;
                font-size: 16px;
                selection-background-color: #FFD700;
                selection-color: #000000;
            }

            QLabel, QCheckBox {
                color: #FF0000;
                font-size: 16px;
            }

            QDialog#searchDialog QLabel,
            QDialog#searchDialog QCheckBox,
            QDialog#searchDialog QLineEdit,
            QDialog#searchDialog QComboBox,
            QDialog#searchDialog QComboBox QAbstractItemView,
            QDialog#searchDialog QPushButton {
                font-size: 18px;
            }

            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }

            QProgressBar {
                background-color: #000000;
                color: #FF0000;
                border: 1px solid #880000;
                border-radius: 3px;
                text-align: center;
                min-height: 26px;
                font-size: 15px;
            }

            QProgressBar::chunk {
                background-color: #FFD700;
            }
        """)

    # ------------------------------------------------------------
    # 动画
    # ------------------------------------------------------------

    def animate_search_dialog(self):
        if self.search_dialog.isVisible():
            self.search_dialog.raise_()
            self.search_dialog.activateWindow()
            return

        self.search_dialog.setWindowOpacity(0.0)
        self.search_dialog.show()
        self.search_dialog.raise_()
        self.search_dialog.activateWindow()

        self.search_dialog_animation = QPropertyAnimation(
            self.search_dialog,
            b"windowOpacity",
            self,
        )
        self.search_dialog_animation.setDuration(180)
        self.search_dialog_animation.setStartValue(0.0)
        self.search_dialog_animation.setEndValue(1.0)
        self.search_dialog_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.search_dialog_animation.start()

    def animate_loader_panel(self, visible):
        if visible:
            self.loader_panel.show()

        self.loader_opacity_animation = QPropertyAnimation(
            self.loader_opacity_effect,
            b"opacity",
            self,
        )
        self.loader_opacity_animation.setDuration(180)
        self.loader_opacity_animation.setStartValue(
            self.loader_opacity_effect.opacity()
        )
        self.loader_opacity_animation.setEndValue(1.0 if visible else 0.0)

        if not visible:
            self.loader_opacity_animation.finished.connect(self.loader_panel.hide)

        self.loader_opacity_animation.start()

    def animate_progress_to(self, value):
        self.progress_animation = QPropertyAnimation(
            self.loading_progress,
            b"value",
            self,
        )
        self.progress_animation.setDuration(100)
        self.progress_animation.setStartValue(self.loading_progress.value())
        self.progress_animation.setEndValue(value)
        self.progress_animation.start()

    # ------------------------------------------------------------
    # 编码检测
    # ------------------------------------------------------------

    def detect_encoding(self, sample):
        if not sample:
            return "utf-8"

        if sample.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"

        if sample.startswith(b"\xff\xfe"):
            return "utf-16-le"

        if sample.startswith(b"\xfe\xff"):
            return "utf-16-be"

        result = from_bytes(sample).best()

        if result is None or not result.encoding:
            return "utf-8"

        return result.encoding

    def decode_file_data(self, file_data):
        encoding = self.detect_encoding(file_data)

        try:
            return file_data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            pass

        fallback_encodings = (
            "utf-8",
            "gb18030",
            "big5",
            "shift_jis",
            "latin-1",
        )

        for fallback_encoding in fallback_encodings:
            try:
                return file_data.decode(fallback_encoding), fallback_encoding
            except (UnicodeDecodeError, LookupError):
                continue

        return file_data.decode("utf-8", errors="replace"), "utf-8"

    # ------------------------------------------------------------
    # 文件加载
    # ------------------------------------------------------------

    def open_file(self):
        if self.loading_worker is not None:
            return

        if not self.confirm_save_changes():
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "打开文件",
            "",
            "支持的文件 (*.txt *.log *.json *.xml *.csv *.py *.md *.ini *.cfg *.rns);;"
            "敏感资料 (*.rns);;文本文件 (*.txt *.log *.json *.xml *.csv *.py *.md *.ini *.cfg);;"
            "所有文件 (*.*)",
        )

        if not file_path:
            return

        self.open_file_path(file_path)

    def open_file_path(self, file_path):
        path = Path(file_path).expanduser()

        if not path.is_file():
            QMessageBox.warning(self, "打开失败", f"文件不存在或不是普通文件：\n{path}")
            return

        if self.path_is_sensitive_file(path):
            self.open_sensitive_file_path(path)
            return

        self.reset_sensitive_state()

        try:
            file_size = path.stat().st_size
        except OSError as error:
            QMessageBox.critical(self, "打开失败", str(error))
            return

        self.file_size_label.setText(f"大小：{self.format_file_size(file_size)}")

        if file_size >= self.LARGE_FILE_THRESHOLD:
            self.start_large_file_loading(path, file_size)
            return

        self.open_small_file(path)

    def path_is_sensitive_file(self, path):
        try:
            with path.open("rb") as file:
                magic = file.read(len(self.SENSITIVE_MAGIC))
        except OSError:
            return False

        return magic == self.SENSITIVE_MAGIC

    def sensitive_password_message(self, action):
        return (
            "敏感资料模式使用 AES-256-GCM 加密。\n\n"
            "如果只是常规记事本使用，不建议启用此功能。\n"
            "该加密格式不能保证与其他编辑器\n"
            "或软件互相兼容；\n"
            "上传到云端后，云端通常也无法\n"
            "直接预览、索引或解析文本内容。\n"
            "除非你确实需要存储敏感内容，\n"
            "否则建议继续使用普通文本格式。\n\n"
            "请妥善保管密码：密码不会写入文件，\n"
            "忘记后无法恢复内容。\n\n"
            f"{action}密码："
        )

    def prompt_sensitive_password(self, action):
        password, accepted = QInputDialog.getText(
            self,
            "敏感资料模式",
            self.sensitive_password_message(action),
            QLineEdit.EchoMode.Password,
        )

        if not accepted:
            return None

        if not password:
            QMessageBox.warning(self, "密码不能为空", "敏感资料密码不能为空。")
            return None

        return password

    def prompt_new_sensitive_password(self):
        password = self.prompt_sensitive_password("请输入")
        if password is None:
            return None

        confirmation, accepted = QInputDialog.getText(
            self,
            "确认敏感资料密码",
            "请再次输入相同的密码。\n\n"
            "密码不会保存到程序配置中。\n"
            "忘记密码后无法恢复加密内容。",
            QLineEdit.EchoMode.Password,
        )

        if not accepted:
            return None

        if password != confirmation:
            QMessageBox.warning(self, "密码不一致", "两次输入的密码不一致。")
            return None

        return password

    def derive_sensitive_key(self, password, salt, iterations):
        return PBKDF2(
            password.encode("utf-8"),
            salt,
            dkLen=32,
            count=iterations,
            hmac_hash_module=SHA256,
        )

    def encrypt_sensitive_text(self, text, password):
        salt = get_random_bytes(self.SENSITIVE_SALT_SIZE)
        nonce = get_random_bytes(self.SENSITIVE_NONCE_SIZE)
        iterations = self.SENSITIVE_KDF_ITERATIONS
        key = self.derive_sensitive_key(password, salt, iterations)

        cipher = AES.new(
            key,
            AES.MODE_GCM,
            nonce=nonce,
            mac_len=self.SENSITIVE_TAG_SIZE,
        )
        cipher.update(self.SENSITIVE_MAGIC)

        plaintext = text.encode("utf-8")
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)

        return (
            self.SENSITIVE_MAGIC
            + iterations.to_bytes(4, "big")
            + salt
            + nonce
            + tag
            + ciphertext
        )

    def decrypt_sensitive_data(self, file_data, password):
        minimum_size = (
            len(self.SENSITIVE_MAGIC)
            + 4
            + self.SENSITIVE_SALT_SIZE
            + self.SENSITIVE_NONCE_SIZE
            + self.SENSITIVE_TAG_SIZE
        )

        if len(file_data) < minimum_size:
            raise ValueError("敏感资料文件格式不完整。")

        if not file_data.startswith(self.SENSITIVE_MAGIC):
            raise ValueError("这不是 RedNotepad 敏感资料文件。")

        offset = len(self.SENSITIVE_MAGIC)
        iterations = int.from_bytes(file_data[offset:offset + 4], "big")
        offset += 4

        if iterations < 100000 or iterations > 2000000:
            raise ValueError("敏感资料文件的密钥派生参数无效。")

        salt_end = offset + self.SENSITIVE_SALT_SIZE
        salt = file_data[offset:salt_end]
        offset = salt_end

        nonce_end = offset + self.SENSITIVE_NONCE_SIZE
        nonce = file_data[offset:nonce_end]
        offset = nonce_end

        tag_end = offset + self.SENSITIVE_TAG_SIZE
        tag = file_data[offset:tag_end]
        ciphertext = file_data[tag_end:]

        key = self.derive_sensitive_key(password, salt, iterations)
        cipher = AES.new(
            key,
            AES.MODE_GCM,
            nonce=nonce,
            mac_len=self.SENSITIVE_TAG_SIZE,
        )
        cipher.update(self.SENSITIVE_MAGIC)

        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext.decode("utf-8")

    def open_sensitive_file(self):
        if self.loading_worker is not None:
            return

        if not self.confirm_save_changes():
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "打开敏感资料",
            "",
            "RedNotepad 敏感资料 (*.rns);;所有文件 (*.*)",
        )

        if not file_path:
            return

        self.open_sensitive_file_path(Path(file_path))

    def open_sensitive_file_path(self, path):
        if not path.is_file():
            QMessageBox.warning(self, "打开失败", f"文件不存在或不是普通文件：\n{path}")
            return

        password = self.prompt_sensitive_password("请输入")
        if password is None:
            return

        try:
            file_data = path.read_bytes()
            text = self.decrypt_sensitive_data(file_data, password)
        except OSError as error:
            QMessageBox.critical(self, "打开失败", str(error))
            return
        except (ValueError, UnicodeDecodeError):
            QMessageBox.critical(
                self,
                "无法解密",
                "密码错误、文件已损坏，或者该文件不是受支持的敏感资料格式。",
            )
            return

        self.editor.setPlainText(text)
        self.current_file = path
        self.current_encoding = "utf-8"
        self.current_match_span = None
        self.sensitive_mode = True
        self.sensitive_password = password
        self.current_file_is_sensitive = True

        self.editor.document().setModified(False)
        self.encoding_label.setText("模式：AES-256-GCM")
        self.file_size_label.setText(
            f"大小：{self.format_file_size(len(file_data))}"
        )
        self.update_title()

    def reset_sensitive_state(self):
        self.sensitive_mode = False
        self.sensitive_password = None
        self.current_file_is_sensitive = False

    def open_small_file(self, path):
        self.reset_sensitive_state()

        try:
            file_data = path.read_bytes()
        except OSError as error:
            QMessageBox.critical(self, "打开失败", str(error))
            return

        text, encoding = self.decode_file_data(file_data)

        self.editor.setPlainText(text)
        self.current_file = path
        self.current_encoding = encoding
        self.current_match_span = None
        self.editor.document().setModified(False)

        self.encoding_label.setText(f"编码：{encoding.upper()}")
        self.file_size_label.setText(
            f"大小：{self.format_file_size(len(file_data))}"
        )
        self.update_title()

    def start_large_file_loading(self, path, file_size):
        self.reset_sensitive_state()
        self.loading_path = path
        self.loading_size = file_size
        self.current_encoding = "utf-8"
        self.current_file = path
        self.current_match_span = None

        self.occurrence_highlight_selections = []
        self.search_highlight_selections = []
        self.editor.clear()
        self.editor.document().setUndoRedoEnabled(False)
        self.editor.setReadOnly(True)

        self.loading_progress.setValue(0)
        self.cancel_loading_button.setEnabled(True)
        self.loading_label.setText(
            f"正在后台读取 {path.name}（{self.format_file_size(file_size)}）…"
        )
        self.encoding_label.setText("编码：检测中…")
        self.file_size_label.setText(f"大小：{self.format_file_size(file_size)}")

        self.set_search_enabled(False)
        self.animate_loader_panel(True)

        self.loading_thread = QThread(self)
        self.loading_worker = LargeFileLoader(
            path,
            self.READ_CHUNK_SIZE,
            self.ENCODING_SAMPLE_SIZE,
            self.detect_encoding,
        )
        self.loading_worker.moveToThread(self.loading_thread)

        self.loading_thread.started.connect(self.loading_worker.run)
        self.loading_worker.encoding_detected.connect(
            self.large_file_encoding_detected
        )
        self.loading_worker.chunk_ready.connect(self.receive_large_file_chunk)
        self.loading_worker.loading_finished.connect(
            self.finish_large_file_loading
        )
        self.loading_worker.loading_finished.connect(self.loading_thread.quit)
        self.loading_thread.finished.connect(self.loading_worker.deleteLater)
        self.loading_thread.finished.connect(self.large_file_thread_finished)
        self.loading_thread.finished.connect(self.loading_thread.deleteLater)
        self.loading_thread.start()

    def large_file_encoding_detected(self, encoding):
        self.current_encoding = encoding
        self.encoding_label.setText(f"编码：{encoding.upper()}")

    def receive_large_file_chunk(self, text, position):
        self.insert_loaded_text(text)
        progress = 1000

        if self.loading_size > 0:
            progress = int(position * 1000 / self.loading_size)

        if progress > 1000:
            progress = 1000

        self.animate_progress_to(progress)

        percent = progress / 10
        self.loading_label.setText(
            f"正在后台分块读取 {self.loading_path.name}… {percent:.1f}%"
        )

        if self.loading_worker is not None:
            self.loading_worker.allow_next_chunk()

    def insert_loaded_text(self, text):
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.editor.setTextCursor(cursor)

    def finish_large_file_loading(self, success, error_message=""):
        path = self.loading_path

        self.loading_path = None
        self.loading_size = 0

        self.editor.setReadOnly(False)
        self.editor.document().setUndoRedoEnabled(True)
        self.set_search_enabled(True)

        if success:
            self.loading_progress.setValue(1000)
            self.editor.document().setModified(False)
            self.loading_label.setText("文件读取完成")
            self.update_title()
            QTimer.singleShot(350, lambda: self.animate_loader_panel(False))
            return

        self.editor.clear()
        self.current_file = None
        self.current_encoding = "utf-8"
        self.editor.document().setModified(False)
        self.animate_loader_panel(False)

        QMessageBox.critical(
            self,
            "读取失败",
            f"{path}\n\n{error_message}",
        )

        self.update_title()

    def large_file_thread_finished(self):
        self.loading_worker = None
        self.loading_thread = None
        self.stats_update_timer.start()

    def cancel_large_file_loading(self):
        if self.loading_worker is None:
            return

        self.cancel_loading_button.setEnabled(False)
        self.loading_label.setText("正在取消后台读取…")
        self.loading_worker.cancel()

    def set_search_enabled(self, enabled):
        self.find_action.setEnabled(enabled)
        self.replace_action.setEnabled(enabled)
        self.find_next_action.setEnabled(enabled)
        self.find_previous_action.setEnabled(enabled)

        self.find_edit.setEnabled(enabled)
        self.replace_edit.setEnabled(enabled)
        self.search_mode.setEnabled(enabled)
        self.regex_behavior.setEnabled(
            enabled and self.search_mode.currentData() == "regex"
        )
        self.wildcard_behavior.setEnabled(
            enabled and self.search_mode.currentData() == "wildcard"
        )
        self.case_sensitive.setEnabled(enabled)
        self.previous_button.setEnabled(enabled)
        self.next_button.setEnabled(enabled)
        self.replace_button.setEnabled(enabled)
        self.replace_all_button.setEnabled(enabled)
        self.remove_extra_line_breaks_action.setEnabled(enabled)
        self.englishize_action.setEnabled(enabled)

    def format_file_size(self, size):
        units = ("B", "KB", "MB", "GB", "TB")
        value = float(size)
        unit = units[0]

        for candidate in units:
            unit = candidate

            if value < 1024.0 or candidate == units[-1]:
                break

            value /= 1024.0

        if unit == "B":
            return f"{int(value)} {unit}"

        return f"{value:.2f} {unit}"

    # ------------------------------------------------------------
    # 保存操作
    # ------------------------------------------------------------

    def new_file(self):
        if self.loading_worker is not None:
            self.cancel_large_file_loading()
            return

        if not self.confirm_save_changes():
            return

        self.reset_sensitive_state()
        self.editor.clear()
        self.current_file = None
        self.current_encoding = "utf-8"
        self.current_match_span = None

        self.editor.document().setModified(False)
        self.encoding_label.setText("编码：UTF-8")
        self.file_size_label.setText("大小：0 B")
        self.update_title()

    def save_file(self):
        if self.loading_worker is not None:
            QMessageBox.information(
                self,
                "文件仍在读取",
                "请等待大文件读取完成后再保存。",
            )
            return False

        if self.sensitive_mode:
            if self.current_file is None or not self.current_file_is_sensitive:
                return self.save_sensitive_file_as()

            return self.save_sensitive_file()

        if self.current_file is None:
            return self.save_file_as()

        text = self.editor.toPlainText()

        try:
            file_data = text.encode(self.current_encoding)
        except (UnicodeEncodeError, LookupError):
            answer = QMessageBox.question(
                self,
                "编码无法保存",
                f"当前文件编码 {self.current_encoding.upper()} 无法保存部分字符。\n\n是否改用 UTF-8 保存？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )

            if answer != QMessageBox.StandardButton.Yes:
                return False

            self.current_encoding = "utf-8"
            file_data = text.encode("utf-8")

        try:
            self.current_file.write_bytes(file_data)
        except OSError as error:
            QMessageBox.critical(self, "保存失败", str(error))
            return False

        self.editor.document().setModified(False)
        self.encoding_label.setText(f"编码：{self.current_encoding.upper()}")
        self.file_size_label.setText(
            f"大小：{self.format_file_size(len(file_data))}"
        )
        self.update_title()
        return True

    def save_file_as(self):
        if self.sensitive_mode:
            return self.save_sensitive_file_as()

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "另存为",
            "",
            "文本文件 (*.txt);;所有文件 (*.*)",
        )

        if not file_path:
            return False

        self.current_file = Path(file_path)
        return self.save_file()

    def enter_sensitive_mode(self):
        if self.loading_worker is not None:
            QMessageBox.information(
                self,
                "文件仍在读取",
                "请等待大文件读取完成后再进入敏感资料模式。",
            )
            return

        password = self.prompt_new_sensitive_password()
        if password is None:
            return

        self.sensitive_mode = True
        self.sensitive_password = password
        self.current_file_is_sensitive = False

        if not self.save_sensitive_file_as():
            self.reset_sensitive_state()

    def save_sensitive_file_as(self):
        if self.sensitive_password is None:
            password = self.prompt_new_sensitive_password()
            if password is None:
                return False

            self.sensitive_password = password
            self.sensitive_mode = True

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存敏感资料",
            "",
            "RedNotepad 敏感资料 (*.rns);;所有文件 (*.*)",
        )

        if not file_path:
            return False

        path = Path(file_path)

        if path.suffix.lower() != self.SENSITIVE_EXTENSION:
            path = path.with_name(path.name + self.SENSITIVE_EXTENSION)

        previous_file = self.current_file
        previous_sensitive_flag = self.current_file_is_sensitive

        self.current_file = path
        self.current_file_is_sensitive = True

        if self.save_sensitive_file():
            return True

        self.current_file = previous_file
        self.current_file_is_sensitive = previous_sensitive_flag
        return False

    def save_sensitive_file(self):
        if self.current_file is None:
            return self.save_sensitive_file_as()

        if self.sensitive_password is None:
            password = self.prompt_sensitive_password("请输入")
            if password is None:
                return False

            self.sensitive_password = password

        try:
            file_data = self.encrypt_sensitive_text(
                self.editor.toPlainText(),
                self.sensitive_password,
            )
            self.current_file.write_bytes(file_data)
        except OSError as error:
            QMessageBox.critical(self, "保存失败", str(error))
            return False
        except (ValueError, TypeError) as error:
            QMessageBox.critical(self, "加密失败", str(error))
            return False

        self.sensitive_mode = True
        self.current_file_is_sensitive = True
        self.current_encoding = "utf-8"
        self.editor.document().setModified(False)

        self.encoding_label.setText("模式：AES-256-GCM")
        self.file_size_label.setText(
            f"大小：{self.format_file_size(len(file_data))}"
        )
        self.update_title()
        return True

    def confirm_save_changes(self):
        if not self.editor.document().isModified():
            return True

        answer = QMessageBox.question(
            self,
            "文件尚未保存",
            "当前内容已经修改，是否保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )

        if answer == QMessageBox.StandardButton.Save:
            return self.save_file()

        if answer == QMessageBox.StandardButton.Discard:
            return True

        return False

    def document_changed(self):
        self.update_title()

        if self.loading_worker is None:
            self.stats_update_timer.start()

        if self.search_dialog.isVisible() and self.loading_worker is None:
            self.update_match_count()

    def update_title(self):
        file_name = "未命名"

        if self.current_file is not None:
            file_name = self.current_file.name

        modified = " *" if self.editor.document().isModified() else ""
        sensitive = " [敏感资料]" if self.sensitive_mode else ""
        self.setWindowTitle(f"{file_name}{modified}{sensitive} - RedNotepad")

    # ------------------------------------------------------------
    # 查找界面
    # ------------------------------------------------------------

    def show_find(self):
        if self.loading_worker is not None:
            return

        self.animate_search_dialog()

        selected_text = self.editor.textCursor().selectedText()

        if selected_text:
            self.find_edit.setText(selected_text)

        self.find_edit.setFocus()
        self.find_edit.selectAll()
        self.update_match_count()

    def show_replace(self):
        if self.loading_worker is not None:
            return

        self.animate_search_dialog()
        self.replace_edit.setFocus()
        self.update_match_count()

    def search_dialog_closed(self):
        self.editor.setFocus()
        self.current_match_span = None
        self.clear_search_highlights()

    # ------------------------------------------------------------
    # 查找引擎
    # ------------------------------------------------------------

    def build_pattern(self):
        query = self.find_edit.text()

        if not query:
            return None

        mode = self.search_mode.currentData()

        if mode == "normal":
            expression = re.escape(query)
        elif mode == "wildcard":
            expression = self.wildcard_to_regex(query)
        else:
            expression = query

        flags = re.MULTILINE

        if not self.case_sensitive.isChecked():
            flags |= re.IGNORECASE

        try:
            return re.compile(expression, flags)
        except re.error as error:
            self.result_label.setText(f"表达式错误：{error}")
            return None

    def wildcard_to_regex(self, wildcard):
        result = []
        longest = self.wildcard_behavior.currentData() == "longest"

        for character in wildcard:
            if character == "*":
                if longest:
                    result.append(r"[\s\S]+")
                else:
                    result.append(r"[\s\S]+?")
                continue

            if character == "?":
                result.append(r"[\s\S]")
                continue

            result.append(re.escape(character))

        return "".join(result)

    def collect_matches(self):
        pattern = self.build_pattern()

        if pattern is None:
            return [], False

        text = self.editor.toPlainText()
        mode = self.search_mode.currentData()

        if mode != "regex":
            return self.collect_finditer_matches(pattern, text)

        behavior = self.regex_behavior.currentData()

        if behavior == "search":
            match = pattern.search(text)

            if match is None:
                return [], False

            return [match], False

        if behavior == "match":
            match = pattern.match(text)

            if match is None:
                return [], False

            return [match], False

        if behavior == "fullmatch":
            match = pattern.fullmatch(text)

            if match is None:
                return [], False

            return [match], False

        if behavior == "findall":
            pattern.findall(text)
            return self.collect_finditer_matches(pattern, text)

        return self.collect_finditer_matches(pattern, text)

    def collect_finditer_matches(self, pattern, text):
        matches = []
        truncated = False

        for match in pattern.finditer(text):
            matches.append(match)

            if len(matches) >= self.MAX_COLLECTED_MATCHES:
                truncated = True
                break

        return matches, truncated

    def python_index_to_qt_position(self, text, index):
        return len(text[:index].encode("utf-16-le")) // 2

    def qt_position_to_python_index(self, text, qt_position):
        if qt_position <= 0:
            return 0

        utf16_position = 0
        index = 0

        for character in text:
            utf16_position += 2 if ord(character) > 0xFFFF else 1
            index += 1

            if utf16_position >= qt_position:
                return index

        return len(text)

    def select_match(self, match):
        text = self.editor.toPlainText()
        start = self.python_index_to_qt_position(text, match.start())
        end = self.python_index_to_qt_position(text, match.end())

        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)

        self.editor.setTextCursor(cursor)
        self.editor.centerCursor()
        self.current_match_span = (match.start(), match.end())

        self.highlight_matches()

    def find_next(self):
        matches, truncated = self.collect_matches()

        if not matches:
            self.result_label.setText("未找到")
            self.current_match_span = None
            self.clear_search_highlights()
            return

        text = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        position = self.qt_position_to_python_index(text, cursor.selectionEnd())

        if self.current_match_span is not None:
            position = self.current_match_span[1]

            if self.current_match_span[0] == self.current_match_span[1]:
                position += 1

        target = None

        for match in matches:
            if match.start() >= position:
                target = match
                break

        if target is None:
            target = matches[0]

        self.select_match(target)
        self.show_match_number(target, matches, truncated)

    def find_previous(self):
        matches, truncated = self.collect_matches()

        if not matches:
            self.result_label.setText("未找到")
            self.current_match_span = None
            self.clear_search_highlights()
            return

        text = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        position = self.qt_position_to_python_index(text, cursor.selectionStart())

        if self.current_match_span is not None:
            position = self.current_match_span[0]

        target = None

        for match in reversed(matches):
            if match.end() <= position:
                target = match
                break

        if target is None:
            target = matches[-1]

        self.select_match(target)
        self.show_match_number(target, matches, truncated)

    def show_match_number(self, target, matches, truncated):
        current_number = 0

        for number, match in enumerate(matches):
            if match.start() == target.start() and match.end() == target.end():
                current_number = number + 1
                break

        suffix = "+" if truncated else ""
        self.result_label.setText(
            f"{current_number} / {len(matches)}{suffix}"
        )

    def update_match_count(self):
        if not self.find_edit.text():
            self.result_label.setText("")
            self.clear_search_highlights()
            return

        matches, truncated = self.collect_matches()

        if not matches:
            self.result_label.setText("0 个结果")
        else:
            suffix = "+" if truncated else ""
            self.result_label.setText(
                f"{len(matches)}{suffix} 个结果"
            )

        self.highlight_matches(matches)

    # ------------------------------------------------------------
    # 替换
    # ------------------------------------------------------------

    def replace_current(self):
        if not self.find_edit.text():
            return

        if self.current_match_span is None:
            self.find_next()

        if self.current_match_span is None:
            return

        matches, _ = self.collect_matches()
        target = None

        for match in matches:
            if (match.start(), match.end()) == self.current_match_span:
                target = match
                break

        if target is None:
            self.current_match_span = None
            self.find_next()
            return

        replacement = self.get_replacement_text(target)
        text = self.editor.toPlainText()

        start = self.python_index_to_qt_position(text, target.start())
        end = self.python_index_to_qt_position(text, target.end())

        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement)

        self.editor.setTextCursor(cursor)
        self.current_match_span = None
        self.find_next()

    def get_replacement_text(self, match):
        replacement = self.replace_edit.text()

        if self.search_mode.currentData() != "regex":
            return replacement

        try:
            return match.expand(replacement)
        except re.error as error:
            QMessageBox.warning(self, "替换表达式错误", str(error))
            return match.group(0)

    def replace_all(self):
        pattern = self.build_pattern()

        if pattern is None:
            return

        text = self.editor.toPlainText()
        replacement = self.replace_edit.text()
        mode = self.search_mode.currentData()

        try:
            if mode == "regex":
                new_text, count = pattern.subn(replacement, text)
            else:
                new_text, count = pattern.subn(lambda match: replacement, text)
        except re.error as error:
            QMessageBox.warning(self, "替换表达式错误", str(error))
            return

        if count == 0:
            self.result_label.setText("没有可替换内容")
            return

        cursor = self.editor.textCursor()
        old_position = cursor.position()

        self.editor.setPlainText(new_text)

        cursor = self.editor.textCursor()
        maximum_position = self.editor.document().characterCount() - 1
        cursor.setPosition(min(old_position, maximum_position))
        self.editor.setTextCursor(cursor)

        self.current_match_span = None
        self.editor.document().setModified(True)
        self.result_label.setText(f"已替换 {count} 处")

    # ------------------------------------------------------------
    # 查找结果高亮
    # ------------------------------------------------------------

    def highlight_matches(self, matches=None):
        if matches is None:
            matches, _ = self.collect_matches()

        if not matches:
            self.clear_search_highlights()
            return

        text = self.editor.toPlainText()
        selections = []
        count = 0

        for match in matches:
            if count >= self.MAX_HIGHLIGHTS:
                break

            start = self.python_index_to_qt_position(text, match.start())
            end = self.python_index_to_qt_position(text, match.end())

            selection = QTextEdit.ExtraSelection()
            cursor = self.editor.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)

            selection.cursor = cursor
            selection.format.setBackground(QColor("#550000"))
            selection.format.setForeground(QColor("#FF0000"))

            selections.append(selection)
            count += 1

        self.search_highlight_selections = selections
        self.apply_extra_selections()

    def clear_search_highlights(self):
        self.search_highlight_selections = []
        self.apply_extra_selections()

    # ------------------------------------------------------------
    # 选中文本的同内容联动高亮
    # ------------------------------------------------------------

    def schedule_selected_occurrence_highlight(self):
        self.selection_highlight_timer.start()

    def update_selected_occurrence_highlights(self):
        if self.loading_worker is not None:
            self.clear_occurrence_highlights()
            return

        selected_cursor = self.editor.textCursor()

        if not selected_cursor.hasSelection():
            self.clear_occurrence_highlights()
            return

        selected_text = selected_cursor.selectedText()

        if not selected_text:
            self.clear_occurrence_highlights()
            return

        document = self.editor.document()
        find_flags = QTextDocument.FindFlag.FindCaseSensitively
        search_cursor = QTextCursor(document)
        search_cursor.setPosition(0)

        selected_start = selected_cursor.selectionStart()
        selected_end = selected_cursor.selectionEnd()
        selections = []
        count = 0

        while count < self.MAX_OCCURRENCE_HIGHLIGHTS:
            found_cursor = document.find(
                selected_text,
                search_cursor,
                find_flags,
            )

            if found_cursor.isNull():
                break

            found_start = found_cursor.selectionStart()
            found_end = found_cursor.selectionEnd()

            if found_start == selected_start and found_end == selected_end:
                search_cursor.setPosition(found_end)
                continue

            selection = QTextEdit.ExtraSelection()
            selection.cursor = found_cursor
            selection.format.setBackground(QColor(255, 215, 0))
            selection.format.setForeground(QColor(0, 0, 0))
            selections.append(selection)

            count += 1
            search_cursor.setPosition(found_end)

        self.occurrence_highlight_selections = selections
        self.apply_extra_selections()

    def clear_occurrence_highlights(self):
        if not self.occurrence_highlight_selections:
            return

        self.occurrence_highlight_selections = []
        self.apply_extra_selections()

    def apply_extra_selections(self):
        selections = []
        selections.extend(self.search_highlight_selections)
        selections.extend(self.occurrence_highlight_selections)
        self.editor.setExtraSelections(selections)

    # ------------------------------------------------------------
    # 视图与退出
    # ------------------------------------------------------------

    def toggle_word_wrap(self, enabled):
        if enabled:
            self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            return

        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    # ------------------------------------------------------------
    # 拖拽打开文件
    # ------------------------------------------------------------

    def dragEnterEvent(self, event):
        mime_data = event.mimeData()

        if not mime_data.hasUrls():
            event.ignore()
            return

        for url in mime_data.urls():
            if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                event.acceptProposedAction()
                return

        event.ignore()

    def dropEvent(self, event):
        if self.loading_worker is not None:
            event.ignore()
            return

        mime_data = event.mimeData()

        if not mime_data.hasUrls():
            event.ignore()
            return

        dropped_path = None

        for url in mime_data.urls():
            if not url.isLocalFile():
                continue

            path = Path(url.toLocalFile())

            if path.is_file():
                dropped_path = path
                break

        if dropped_path is None:
            event.ignore()
            return

        if not self.confirm_save_changes():
            event.ignore()
            return

        self.open_file_path(dropped_path)
        event.acceptProposedAction()

    def closeEvent(self, event):
        if self.loading_worker is not None:
            self.loading_worker.cancel()

            if self.loading_thread is not None:
                self.loading_thread.quit()

                if not self.loading_thread.wait(3000):
                    event.ignore()
                    return

            event.accept()
            return

        if self.confirm_save_changes():
            event.accept()
            return

        event.ignore()


def main():
    startup_file = None

    if len(sys.argv) > 1:
        startup_file = sys.argv[1]

    app = QApplication(sys.argv)
    app.setApplicationName("RedNotepad")

    interface_font = QFont("Microsoft YaHei UI")
    interface_font.setPointSize(11)
    app.setFont(interface_font)

    window = RedNotepad()
    window.show()

    if startup_file:
        window.open_file_path(startup_file)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
