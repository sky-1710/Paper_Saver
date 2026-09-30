"""One light stylesheet. The pages are the content; the chrome stays quiet."""

STYLE = """
QMainWindow, QWidget { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', 'Noto Sans KR', sans-serif; font-size: 10pt; }
QToolBar { background: #f6f7f9; border: none; border-bottom: 1px solid #dfe2e6; spacing: 4px; padding: 3px 6px; }
QToolBar QToolButton { padding: 4px 9px; border-radius: 5px; }
QToolBar QToolButton:hover { background: #e7eaee; }
QToolBar QToolButton:checked { background: #dbe7fb; }
QLabel#paneTitle { background: #3c4046; color: #e8eaed; padding: 4px 10px; font-weight: 600; }
QLabel#sectionTitle { font-weight: 600; color: #333; padding: 2px 0; }
QLabel#muted { color: #888; font-size: 9pt; }
QFrame#pairBar { background: #fbfaf5; border-top: 1px solid #e3e0d4; }
QLabel#pairSource { color: #444; font-family: 'Georgia', 'Times New Roman', serif; font-size: 10.5pt; }
QLabel#pairTarget { color: #1a3e8c; font-size: 10.5pt; }
QTabWidget::pane { border: none; border-left: 1px solid #dfe2e6; }
QTabBar::tab { padding: 6px 16px; }
QTabBar::tab:selected { font-weight: 600; }
QPushButton { padding: 5px 12px; border: 1px solid #cfd4da; border-radius: 5px; background: #fff; }
QPushButton:hover { background: #f0f3f7; }
QPushButton#primary { background: #1a73e8; color: white; border-color: #1a73e8; font-size: 12pt; padding: 10px 26px; }
QPushButton#primary:hover { background: #1765cc; }
QProgressBar { border: 1px solid #cfd4da; border-radius: 4px; text-align: center; height: 16px; background: #fff; }
QProgressBar::chunk { background: #34a853; border-radius: 3px; }
QListWidget, QTableWidget, QPlainTextEdit, QTextBrowser, QLineEdit { border: 1px solid #dfe2e6; border-radius: 4px; }
QPlainTextEdit { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', 'Menlo', 'Consolas', monospace; }
"""
