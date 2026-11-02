"""One light stylesheet. The pages are the content; the chrome stays quiet."""

STYLE = """
QMainWindow, QWidget { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', 'Noto Sans KR', sans-serif; font-size: 10pt; }
QToolBar { background: #f6f7f9; border: none; border-bottom: 1px solid #dfe2e6; spacing: 4px; padding: 3px 6px; }
QToolBar QToolButton { padding: 4px 9px; border-radius: 5px; }
QToolBar QToolButton:hover { background: #e7eaee; }
QToolBar QToolButton:checked { background: #dbe7fb; }
QFrame#paneHeader { background: #3c4046; }
QFrame#paneHeader QToolButton { color: #bdc1c6; background: transparent; border: none; border-radius: 4px; padding: 2px 9px; font-weight: 600; }
QFrame#paneHeader QToolButton:hover { color: #ffffff; background: #4a4f56; }
QFrame#paneHeader QToolButton:checked { color: #ffffff; background: #5f6368; }
QLabel#reviewLead { font-size: 10.5pt; color: #3c4043; }
QPushButton#modeButton { padding: 3px 10px; }
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
QPushButton:checked { background: #dbe7fb; border-color: #1a73e8; color: #1a4fb4; font-weight: 600; }
QPushButton#primary { background: #1a73e8; color: white; border-color: #1a73e8; font-size: 12pt; padding: 10px 26px; }
QPushButton#primary:hover { background: #1765cc; }
QProgressBar { border: 1px solid #cfd4da; border-radius: 4px; text-align: center; height: 16px; background: #fff; }
QProgressBar::chunk { background: #34a853; border-radius: 3px; }
QListWidget, QTableWidget, QPlainTextEdit, QTextBrowser, QTextEdit#liveMarkdown, QLineEdit { border: 1px solid #dfe2e6; border-radius: 4px; }
QFrame#quizMaterials { background: #f8f9fb; border: 1px solid #e3e6ea; border-radius: 6px; }
QLabel#materialKind { font-weight: 600; color: #1a4fb4; padding-top: 4px; }
QLabel#quizSlide { font-size: 12pt; font-weight: 700; color: #202124; }
QLabel#quizTitle { font-size: 11pt; color: #3c4043; }
QLabel#quizLead { font-size: 11pt; font-weight: 600; color: #202124; }
QLabel#quizScore { font-weight: 600; color: #1a4fb4; }
QLabel#quizError { font-size: 11pt; font-weight: 600; color: #c5221f; }
QFrame#quizCard { background: #fff; border: 1px solid #dfe2e6; border-radius: 8px; }
QFrame#quizCard[result="right"] { border: 1px solid #34a853; }
QFrame#quizCard[result="wrong"] { border: 1px solid #d93025; }
QFrame#quizResult { background: #eef3fd; border: 1px solid #c6d6f5; border-radius: 8px; }
QLabel#quizNumber { font-weight: 600; color: #5f6368; }
QLabel#quizQuestion { font-family: 'Georgia', 'Times New Roman', serif; font-size: 11pt; color: #202124; }
QLabel#quizHint { background: #fef7e0; border-radius: 4px; padding: 5px 7px; color: #6b4e00; }
QLabel#quizRight { font-weight: 700; color: #137333; }
QLabel#quizWrong { font-weight: 700; color: #c5221f; }
QLabel#quizExplainTitle { font-weight: 600; color: #3c4043; }
QLabel#quizExplain { color: #3c4043; }
QLabel#quizEvidence { color: #5f6368; padding-top: 2px; }
QFrame#quizAway { background: #e6f4ea; border: 1px solid #b7dfc3; border-radius: 6px; }
QPushButton#quizChoice { font-size: 13pt; font-weight: 700; min-width: 64px; padding: 5px 0; }
QPushButton#quizPrimary { background: #1a73e8; color: white; border-color: #1a73e8; padding: 7px 18px; font-weight: 600; }
QPushButton#quizPrimary:hover { background: #1765cc; }
QPushButton#quizPrimary:disabled { background: #a8c7fa; border-color: #a8c7fa; }
QFrame#bookmarkCard { background: #fff; border: 1px solid #e3e6ea; border-radius: 6px; }
QFrame#bookmarkCard:hover { background: #f8f9fb; }
QLabel#bookmarkPage { color: #80868b; font-size: 9pt; }
QLabel#bookmarkText { font-family: 'Georgia', 'Times New Roman', serif; color: #202124; }
QLabel#bookmarkTranslation { color: #1a3e8c; }
QLabel#bookmarkNote { background: #fef7e0; border-radius: 4px; padding: 5px 7px; color: #5c4400; margin-top: 2px; }
QTreeWidget { border: 1px solid #dfe2e6; border-radius: 4px; }
QTreeWidget::item { padding: 4px 2px; }
QPlainTextEdit { font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', 'Menlo', 'Consolas', monospace; }
"""
