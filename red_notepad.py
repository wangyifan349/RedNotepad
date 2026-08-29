# Dependencies:
#   pip install PyQt6 charset-normalizer

import codecs
import re
import sys
from pathlib import Path

from charset_normalizer import from_bytes
from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QKeySequence, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class RedNotepad(QMainWindow):
    LARGE_FILE_THRESHOLD = 8 * 1024 * 1024
    READ_CHUNK_SIZE = 512 * 1024
    ENCODING_SAMPLE_SIZE = 1024 * 1024
    MAX_HIGHLIGHTS = 2000
    MAX_OCCURRENCE_HIGHLIGHTS = 3000
    MAX_COLLECTED_MATCHES = 100000

    def __init__(self):
        super().__init__()

        self.current_file = None
        self.current_encoding = "utf-8"
        self.current_match_span = None
        self.font_size = 14

        self.loading_file = None
        self.loading_path = None
        self.loading_size = 0
        self.loading_decoder = None
        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self.read_next_file_chunk)

        self.search_highlight_selections = []
        self.occurrence_highlight_selections = []
        self.selection_highlight_timer = QTimer(self)
        self.selection_highlight_timer.setSingleShot(True)
        self.selection_highlight_timer.setInterval(140)
        self.selection_highlight_timer.timeout.connect(
            self.update_selected_occurrence_highlights
        )

        self.search_panel_animation = None
        self.search_opacity_animation = None
        self.loader_opacity_animation = None
        self.progress_animation = None

        self.setWindowTitle("Untitled - RedNotepad")
        self.resize(1200, 800)

        self.create_editor()
        self.create_search_panel()
        self.create_loader_panel()
        self.create_layout()
        self.create_actions()
        self.create_menu()
        self.create_status_bar()
        self.apply_style()

        self.search_panel.hide()
        self.loader_panel.hide()

    # ------------------------------------------------------------
    # Editor
    # ------------------------------------------------------------

    def create_editor(self):
        self.editor = QPlainTextEdit()

        font = QFont("Consolas")
        font.setPointSize(self.font_size)

        self.editor.setFont(font)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.textChanged.connect(self.document_changed)
        self.editor.cursorPositionChanged.connect(self.update_cursor_status)
        self.editor.selectionChanged.connect(
            self.schedule_selected_occurrence_highlight
        )

    # ------------------------------------------------------------
    # Search panel
    # ------------------------------------------------------------

    def create_search_panel(self):
        self.search_panel = QWidget()
        self.search_panel.setMaximumHeight(0)

        self.search_opacity_effect = QGraphicsOpacityEffect(self.search_panel)
        self.search_opacity_effect.setOpacity(0.0)
        self.search_panel.setGraphicsEffect(self.search_opacity_effect)

        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find text")

        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace with")

        self.search_mode = QComboBox()
        self.search_mode.addItem("Plain Text", "normal")
        self.search_mode.addItem("Regular Expression", "regex")
        self.search_mode.addItem("Wildcard", "wildcard")

        self.regex_behavior = QComboBox()
        self.regex_behavior.addItem("re.finditer", "finditer")
        self.regex_behavior.addItem("re.search", "search")
        self.regex_behavior.addItem("re.findall", "findall")
        self.regex_behavior.addItem("re.match", "match")
        self.regex_behavior.addItem("re.fullmatch", "fullmatch")
        self.regex_behavior.setEnabled(False)

        self.case_sensitive = QCheckBox("Case Sensitive")

        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.replace_button = QPushButton("Replace")
        self.replace_all_button = QPushButton("Replace All")
        self.close_search_button = QPushButton("Close")
        self.result_label = QLabel("")

        self.previous_button.clicked.connect(self.find_previous)
        self.next_button.clicked.connect(self.find_next)
        self.replace_button.clicked.connect(self.replace_current)
        self.replace_all_button.clicked.connect(self.replace_all)
        self.close_search_button.clicked.connect(self.hide_search_panel)

        self.find_edit.returnPressed.connect(self.find_next)
        self.find_edit.textChanged.connect(self.search_settings_changed)
        self.search_mode.currentIndexChanged.connect(self.search_mode_changed)
        self.regex_behavior.currentIndexChanged.connect(self.search_settings_changed)
        self.case_sensitive.stateChanged.connect(self.search_settings_changed)

        layout = QHBoxLayout(self.search_panel)
        layout.setContentsMargins(8, 6, 8, 6)

        layout.addWidget(QLabel("Find"))
        layout.addWidget(self.find_edit, 2)
        layout.addWidget(QLabel("Replace"))
        layout.addWidget(self.replace_edit, 2)
        layout.addWidget(self.search_mode)
        layout.addWidget(self.regex_behavior)
        layout.addWidget(self.case_sensitive)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.replace_button)
        layout.addWidget(self.replace_all_button)
        layout.addWidget(self.result_label)
        layout.addWidget(self.close_search_button)

    def search_mode_changed(self):
        is_regex = self.search_mode.currentData() == "regex"
        self.regex_behavior.setEnabled(is_regex)
        self.search_settings_changed()

    def search_settings_changed(self):
        self.current_match_span = None

        if self.loading_file is not None:
            return

        self.update_match_count()

    # ------------------------------------------------------------
    # Loader panel
    # ------------------------------------------------------------

    def create_loader_panel(self):
        self.loader_panel = QWidget()

        self.loader_opacity_effect = QGraphicsOpacityEffect(self.loader_panel)
        self.loader_opacity_effect.setOpacity(0.0)
        self.loader_panel.setGraphicsEffect(self.loader_opacity_effect)

        self.loading_label = QLabel("Loading file in chunks...")

        self.loading_progress = QProgressBar()
        self.loading_progress.setRange(0, 1000)
        self.loading_progress.setValue(0)
        self.loading_progress.setTextVisible(True)

        self.cancel_loading_button = QPushButton("Cancel")
        self.cancel_loading_button.clicked.connect(self.cancel_large_file_loading)

        layout = QHBoxLayout(self.loader_panel)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(self.loading_label)
        layout.addWidget(self.loading_progress, 1)
        layout.addWidget(self.cancel_loading_button)

    # ------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------

    def create_layout(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self.loader_panel)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.search_panel)

        self.setCentralWidget(container)

    # ------------------------------------------------------------
    # Actions and menus
    # ------------------------------------------------------------

    def create_actions(self):
        self.new_action = QAction("New", self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_action.triggered.connect(self.new_file)

        self.open_action = QAction("Open", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.open_file)

        self.save_action = QAction("Save", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.triggered.connect(self.save_file)

        self.save_as_action = QAction("Save As", self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.triggered.connect(self.save_file_as)

        self.exit_action = QAction("Exit", self)
        self.exit_action.triggered.connect(self.close)

        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.editor.undo)

        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.editor.redo)

        self.cut_action = QAction("Cut", self)
        self.cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        self.cut_action.triggered.connect(self.editor.cut)

        self.copy_action = QAction("Copy", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self.editor.copy)

        self.paste_action = QAction("Paste", self)
        self.paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        self.paste_action.triggered.connect(self.editor.paste)

        self.select_all_action = QAction("Select All", self)
        self.select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        self.select_all_action.triggered.connect(self.editor.selectAll)

        self.find_action = QAction("Find", self)
        self.find_action.setShortcut(QKeySequence.StandardKey.Find)
        self.find_action.triggered.connect(self.show_find)

        self.replace_action = QAction("Replace", self)
        self.replace_action.setShortcut("Ctrl+H")
        self.replace_action.triggered.connect(self.show_replace)

        self.find_next_action = QAction("Find Next", self)
        self.find_next_action.setShortcut("F3")
        self.find_next_action.triggered.connect(self.find_next)

        self.find_previous_action = QAction("Find Previous", self)
        self.find_previous_action.setShortcut("Shift+F3")
        self.find_previous_action.triggered.connect(self.find_previous)

        self.wrap_action = QAction("Word Wrap", self)
        self.wrap_action.setCheckable(True)
        self.wrap_action.triggered.connect(self.toggle_word_wrap)

    def create_menu(self):
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(self.new_action)
        file_menu.addAction(self.open_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        edit_menu = self.menuBar().addMenu("Edit")
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
        edit_menu.addAction(self.find_next_action)
        edit_menu.addAction(self.find_previous_action)

        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self.wrap_action)

        font_menu = view_menu.addMenu("Font Size")
        font_sizes = (10, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48)

        for size in font_sizes:
            action = QAction(f"{size} pt", self)
            action.triggered.connect(
                lambda checked=False, selected_size=size: self.set_font_size(selected_size)
            )
            font_menu.addAction(action)

    # ------------------------------------------------------------
    # Status bar and font size
    # ------------------------------------------------------------

    def create_status_bar(self):
        self.cursor_label = QLabel("Line 1, Column 1")
        self.encoding_label = QLabel("Encoding: UTF-8")
        self.file_size_label = QLabel("Size: 0 B")
        self.font_size_label = QLabel(f"Font: {self.font_size}")

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setMinimum(6)
        self.font_size_spin.setMaximum(100)
        self.font_size_spin.setValue(self.font_size)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.setFixedWidth(85)
        self.font_size_spin.valueChanged.connect(self.set_font_size)

        self.statusBar().addPermanentWidget(self.encoding_label)
        self.statusBar().addPermanentWidget(self.file_size_label)
        self.statusBar().addPermanentWidget(self.font_size_label)
        self.statusBar().addPermanentWidget(self.font_size_spin)
        self.statusBar().addPermanentWidget(self.cursor_label)

    def update_cursor_status(self):
        cursor = self.editor.textCursor()
        line = cursor.blockNumber() + 1
        column = cursor.positionInBlock() + 1
        self.cursor_label.setText(f"Line {line}, Column {column}")

    def set_font_size(self, size):
        self.font_size = size
        font = self.editor.font()
        font.setPointSize(size)
        self.editor.setFont(font)
        self.font_size_label.setText(f"Font: {size}")

        if self.font_size_spin.value() != size:
            self.font_size_spin.setValue(size)

    # ------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
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
            }

            QStatusBar {
                border-top: 1px solid #550000;
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
                padding: 4px;
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
                padding: 5px 10px;
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
                selection-background-color: #FFD700;
                selection-color: #000000;
            }

            QLabel, QCheckBox {
                color: #FF0000;
            }

            QProgressBar {
                background-color: #000000;
                color: #FF0000;
                border: 1px solid #880000;
                border-radius: 3px;
                text-align: center;
                min-height: 20px;
            }

            QProgressBar::chunk {
                background-color: #FFD700;
            }
        """)

    # ------------------------------------------------------------
    # Animation
    # ------------------------------------------------------------

    def animate_search_panel(self, visible):
        if visible:
            self.search_panel.show()

        start_height = self.search_panel.maximumHeight()
        end_height = 62 if visible else 0

        self.search_panel_animation = QPropertyAnimation(
            self.search_panel,
            b"maximumHeight",
            self,
        )
        self.search_panel_animation.setDuration(220)
        self.search_panel_animation.setStartValue(start_height)
        self.search_panel_animation.setEndValue(end_height)
        self.search_panel_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.search_opacity_animation = QPropertyAnimation(
            self.search_opacity_effect,
            b"opacity",
            self,
        )
        self.search_opacity_animation.setDuration(180)
        self.search_opacity_animation.setStartValue(
            self.search_opacity_effect.opacity()
        )
        self.search_opacity_animation.setEndValue(1.0 if visible else 0.0)

        if not visible:
            self.search_panel_animation.finished.connect(self.search_panel.hide)

        self.search_panel_animation.start()
        self.search_opacity_animation.start()

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
    # Encoding detection
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
    # File loading
    # ------------------------------------------------------------

    def open_file(self):
        if self.loading_file is not None:
            return

        if not self.confirm_save_changes():
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open File",
            "",
            "Text Files (*.txt *.log *.json *.xml *.csv *.py *.md *.ini *.cfg);;All Files (*.*)",
        )

        if not file_path:
            return

        self.open_file_path(file_path)

    def open_file_path(self, file_path):
        path = Path(file_path).expanduser()

        if not path.is_file():
            QMessageBox.warning(self, "Open Failed", f"File does not exist or is not a regular file:\n{path}")
            return

        try:
            file_size = path.stat().st_size
        except OSError as error:
            QMessageBox.critical(self, "Open Failed", str(error))
            return

        self.file_size_label.setText(f"Size: {self.format_file_size(file_size)}")

        if file_size >= self.LARGE_FILE_THRESHOLD:
            self.start_large_file_loading(path, file_size)
            return

        self.open_small_file(path)

    def open_small_file(self, path):
        try:
            file_data = path.read_bytes()
        except OSError as error:
            QMessageBox.critical(self, "Open Failed", str(error))
            return

        text, encoding = self.decode_file_data(file_data)

        self.editor.setPlainText(text)
        self.current_file = path
        self.current_encoding = encoding
        self.current_match_span = None
        self.editor.document().setModified(False)

        self.encoding_label.setText(f"Encoding: {encoding.upper()}")
        self.file_size_label.setText(
            f"Size: {self.format_file_size(len(file_data))}"
        )
        self.update_title()

    def start_large_file_loading(self, path, file_size):
        try:
            sample_file = path.open("rb")
            sample = sample_file.read(self.ENCODING_SAMPLE_SIZE)
            sample_file.close()

            encoding = self.detect_encoding(sample)
            decoder_factory = codecs.getincrementaldecoder(encoding)
            decoder = decoder_factory(errors="replace")
            loading_file = path.open("rb")
        except (OSError, LookupError) as error:
            QMessageBox.critical(self, "Open Failed", str(error))
            return

        self.loading_file = loading_file
        self.loading_path = path
        self.loading_size = file_size
        self.loading_decoder = decoder
        self.current_encoding = encoding
        self.current_file = path
        self.current_match_span = None

        self.occurrence_highlight_selections = []
        self.search_highlight_selections = []
        self.editor.clear()
        self.editor.document().setUndoRedoEnabled(False)
        self.editor.setReadOnly(True)

        self.loading_progress.setValue(0)
        self.loading_label.setText(
            f"Loading {path.name} in chunks ({self.format_file_size(file_size)})..."
        )
        self.encoding_label.setText(f"Encoding: {encoding.upper()}")
        self.file_size_label.setText(f"Size: {self.format_file_size(file_size)}")

        self.set_search_enabled(False)
        self.animate_loader_panel(True)

        self.loading_timer.start(0)

    def read_next_file_chunk(self):
        if self.loading_file is None:
            self.loading_timer.stop()
            return

        try:
            chunk = self.loading_file.read(self.READ_CHUNK_SIZE)
        except OSError as error:
            self.finish_large_file_loading(False, str(error))
            return

        if not chunk:
            try:
                final_text = self.loading_decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                final_text = ""

            if final_text:
                self.insert_loaded_text(final_text)

            self.finish_large_file_loading(True)
            return

        try:
            text = self.loading_decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            text = chunk.decode(self.current_encoding, errors="replace")

        self.insert_loaded_text(text)

        position = self.loading_file.tell()
        progress = 1000

        if self.loading_size > 0:
            progress = int(position * 1000 / self.loading_size)

        if progress > 1000:
            progress = 1000

        self.animate_progress_to(progress)

        percent = progress / 10
        self.loading_label.setText(
            f"Loading {self.loading_path.name} in chunks... {percent:.1f}%"
        )

    def insert_loaded_text(self, text):
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.editor.setTextCursor(cursor)

    def finish_large_file_loading(self, success, error_message=""):
        self.loading_timer.stop()

        if self.loading_file is not None:
            try:
                self.loading_file.close()
            except OSError:
                pass

        path = self.loading_path

        self.loading_file = None
        self.loading_path = None
        self.loading_size = 0
        self.loading_decoder = None

        self.editor.setReadOnly(False)
        self.editor.document().setUndoRedoEnabled(True)
        self.set_search_enabled(True)

        if success:
            self.loading_progress.setValue(1000)
            self.editor.document().setModified(False)
            self.loading_label.setText("File loading complete")
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
            "Read Failed",
            f"{path}\n\n{error_message}",
        )

        self.update_title()

    def cancel_large_file_loading(self):
        if self.loading_file is None:
            return

        self.finish_large_file_loading(False, "Large-file loading was canceled by the user.")

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
        self.case_sensitive.setEnabled(enabled)
        self.previous_button.setEnabled(enabled)
        self.next_button.setEnabled(enabled)
        self.replace_button.setEnabled(enabled)
        self.replace_all_button.setEnabled(enabled)

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
    # Save operations
    # ------------------------------------------------------------

    def new_file(self):
        if self.loading_file is not None:
            self.cancel_large_file_loading()

        if not self.confirm_save_changes():
            return

        self.editor.clear()
        self.current_file = None
        self.current_encoding = "utf-8"
        self.current_match_span = None

        self.editor.document().setModified(False)
        self.encoding_label.setText("Encoding: UTF-8")
        self.file_size_label.setText("Size: 0 B")
        self.update_title()

    def save_file(self):
        if self.loading_file is not None:
            QMessageBox.information(
                self,
                "File Is Still Loading",
                "Please wait until the large file has finished loading before saving.",
            )
            return False

        if self.current_file is None:
            return self.save_file_as()

        text = self.editor.toPlainText()

        try:
            file_data = text.encode(self.current_encoding)
        except (UnicodeEncodeError, LookupError):
            answer = QMessageBox.question(
                self,
                "Encoding Cannot Save Text",
                f"The current encoding {self.current_encoding.upper()} cannot represent some characters.\n\nSave as UTF-8 instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )

            if answer != QMessageBox.StandardButton.Yes:
                return False

            self.current_encoding = "utf-8"
            file_data = text.encode("utf-8")

        try:
            self.current_file.write_bytes(file_data)
        except OSError as error:
            QMessageBox.critical(self, "Save Failed", str(error))
            return False

        self.editor.document().setModified(False)
        self.encoding_label.setText(f"Encoding: {self.current_encoding.upper()}")
        self.file_size_label.setText(
            f"Size: {self.format_file_size(len(file_data))}"
        )
        self.update_title()
        return True

    def save_file_as(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save As",
            "",
            "Text Files (*.txt);;All Files (*.*)",
        )

        if not file_path:
            return False

        self.current_file = Path(file_path)
        return self.save_file()

    def confirm_save_changes(self):
        if not self.editor.document().isModified():
            return True

        answer = QMessageBox.question(
            self,
            "Unsaved Changes",
            "The document has been modified. Do you want to save it?",
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

        if self.search_panel.isVisible() and self.loading_file is None:
            self.update_match_count()

    def update_title(self):
        file_name = "Untitled"

        if self.current_file is not None:
            file_name = self.current_file.name

        modified = " *" if self.editor.document().isModified() else ""
        self.setWindowTitle(f"{file_name}{modified} - RedNotepad")

    # ------------------------------------------------------------
    # Search UI
    # ------------------------------------------------------------

    def show_find(self):
        if self.loading_file is not None:
            return

        self.animate_search_panel(True)

        selected_text = self.editor.textCursor().selectedText()

        if selected_text:
            self.find_edit.setText(selected_text)

        self.find_edit.setFocus()
        self.find_edit.selectAll()
        self.update_match_count()

    def show_replace(self):
        if self.loading_file is not None:
            return

        self.animate_search_panel(True)
        self.replace_edit.setFocus()
        self.update_match_count()

    def hide_search_panel(self):
        self.editor.setFocus()
        self.current_match_span = None
        self.clear_search_highlights()
        self.animate_search_panel(False)

    # ------------------------------------------------------------
    # Search engine
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
            self.result_label.setText(f"Expression error: {error}")
            return None

    def wildcard_to_regex(self, wildcard):
        result = []

        for character in wildcard:
            if character == "*":
                result.append(r"[\s\S]*?")
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
            self.result_label.setText("Not found")
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
            self.result_label.setText("Not found")
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
            self.result_label.setText("0 results")
        else:
            suffix = "+" if truncated else ""
            self.result_label.setText(
                f"{len(matches)}{suffix} results"
            )

        self.highlight_matches(matches)

    # ------------------------------------------------------------
    # Replacement
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
            QMessageBox.warning(self, "Replacement Expression Error", str(error))
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
            QMessageBox.warning(self, "Replacement Expression Error", str(error))
            return

        if count == 0:
            self.result_label.setText("Nothing to replace")
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
        self.result_label.setText(f"Replaced {count} occurrence(s)")

    # ------------------------------------------------------------
    # Search highlighting
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
    # Selected-text occurrence highlighting
    # ------------------------------------------------------------

    def schedule_selected_occurrence_highlight(self):
        self.selection_highlight_timer.start()

    def update_selected_occurrence_highlights(self):
        if self.loading_file is not None:
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
            selection.format.setBackground(QColor("#3A1600"))
            selection.format.setForeground(QColor("#FF8C00"))
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
    # View and shutdown
    # ------------------------------------------------------------

    def toggle_word_wrap(self, enabled):
        if enabled:
            self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            return

        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def closeEvent(self, event):
        if self.loading_file is not None:
            self.cancel_large_file_loading()

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

    window = RedNotepad()
    window.show()

    if startup_file:
        window.open_file_path(startup_file)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
