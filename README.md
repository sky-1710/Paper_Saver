# Paper Saver

논문 PDF를 열면 **왼쪽엔 원문**, **오른쪽엔 같은 레이아웃의 한국어 직역**을 보여주는 데스크톱 리더.
Lecture Saver의 자매 앱이며 같은 구조(PySide6 · Codex CLI 번역 · 프로젝트 폴더)를 따릅니다.

## 설치와 실행

### Windows — 프로그램(권장)
`PaperSaver-<버전>-windows.zip`을 풀고 **`Paper Saver\Paper Saver.exe`** 실행. Python 설치가 필요 없습니다.
폴더째로 원하는 곳에 두고 exe를 우클릭 → *바로 가기 만들기*로 바탕 화면에 두면 됩니다.
처음 실행 때 SmartScreen "Windows의 PC 보호" 창이 뜨면 **추가 정보 → 실행** (서명하지 않은 프로그램이라 뜨는 경고입니다).

### macOS — 프로그램
`PaperSaver-<버전>-macos-arm64.zip`(M1 이후) 또는 `-macos-intel.zip`을 풀어 **`Paper Saver.app`**을 응용 프로그램 폴더로 옮깁니다.
처음 한 번은 **우클릭 → 열기 → 열기** (서명·공증하지 않은 앱이라 그냥 더블클릭하면 막힙니다).
그래도 "손상되었다"고 나오면 터미널에서: `xattr -dr com.apple.quarantine "/Applications/Paper Saver.app"`

맥 앱은 **맥에서만 빌드**할 수 있습니다(Windows에서 만들 수 없음). 방법 두 가지:
- GitHub 저장소에 올리고 **Actions → build → Run workflow** → 끝나면 *Artifacts*에서 맥(Apple Silicon/Intel)·Windows zip을 받습니다 (`.github/workflows/build.yml`).
- 맥에서 직접: `python3 -m pip install -r requirements.txt pyinstaller` 후 `python3 tools/build.py`

### 소스에서 실행 (개발용, 모든 OS)
`PaperSaver-<버전>-source.zip`의 `Paper Saver.bat`(Windows) / `Paper Saver.command`(macOS). Python 3.10 이상 필요, 첫 실행 때 패키지를 설치합니다.

### 직접 빌드
```
python tools/build.py            # 이 OS용 앱 빌드 → 자체 점검 → dist/에 zip
python tools/build.py --install  # (Windows) 추가로 이 PC에 설치하고 바탕 화면·시작 메뉴 바로 가기 생성
python tools/make_zip.py         # 소스 zip
```

## GPT 연결

API 키를 쓰지 않습니다. OpenAI의 **Codex CLI**를 백그라운드에서 실행(`codex exec`)해서, 그 컴퓨터의 Codex에 **로그인된 ChatGPT 계정**으로 번역·사전을 요청합니다. 사용량은 그 계정의 ChatGPT 사용량에서 차감되고 별도 요금은 없습니다.

- **다른 사람에게 이 zip을 줘도 내 계정이 쓰이지 않습니다.** 로그인 정보는 각자 컴퓨터의 `~/.codex/`에 있고 zip에는 들어 있지 않습니다. 받은 사람은 자기 계정으로 로그인해야 번역이 됩니다.
- Codex 설치와 로그인 (한 번만):
  ```
  npm install -g @openai/codex      (Node.js 필요)
  codex login                       (브라우저가 열리면 본인 ChatGPT 계정으로 로그인)
  ```
  VS Code에 **ChatGPT 확장 프로그램**을 설치하고 로그인해 둔 경우에는 그 안에 든 Codex를 자동으로 찾아 씁니다.
- Codex를 못 찾으면 앱이 안내를 띄웁니다. 특이한 위치에 설치했다면 앱의 **설정 → Codex 경로**에 전체 경로를 넣으세요.
- 계정 플랜에 따라 Codex 사용이 제한될 수 있습니다. 한도에 걸리면 번역한 부분까지는 저장되고, 나중에 다시 시작하면 남은 문장만 이어서 번역합니다.

## 기능

| | |
|---|---|
| 번역 페이지 | 원본 페이지 위에 **본문 문단만** 지우고 그 사각형 안에 한국어를 조판. 그림·표·수식·저자·참고문헌은 원본 픽셀이 같은 좌표에 그대로 남습니다. 한국어가 넘치면 글자가 작아질 뿐 사각형은 절대 커지지 않습니다. |
| 인라인 수식 | 문장 속 수식은 `⟦m1⟧` 표식으로 GPT에 보내고, 번역문 속 제자리에 원본 수식 이미지를 다시 심습니다(아래첨자 위치까지 유지). |
| 문장 연결 | 어느 쪽이든 문장 **근처**에 마우스를 올리면 원문과 번역이 함께 하이라이트되고, 하단 바에 두 문장이 전체로 표시됩니다. |
| 사전 | 원문 단어 클릭 → **그 문장 안에서의 뜻** + 일반 뜻(GPT). 한 번 찾은 단어는 캐시되어 즉시 표시. ☆로 단어장에 저장(예문 포함, Anki용 TSV 내보내기). |
| 메모 | 자유 메모장(자동 저장) + 문장 메모(문장 우클릭 또는 Ctrl+M). 메모가 달린 문장은 옅게 하이라이트되고, 그 문장 근처에 포인터를 가져가면 작은 메모 팝업이 뜨며 다른 곳으로 가면 사라집니다. 팝업을 누르면 수정. |
| 형광펜 | **드래그한 만큼** 칠합니다(원문·번역 어느 쪽이든, 단어 단위로 맞춤). 색은 툴바의 `형광펜` 버튼에서 선택하거나 끌 수 있고, 칠한 곳을 우클릭하면 색 바꾸기·지우기·복사. 드래그 없이 클릭하면 사전. 문장 전체를 양쪽에 칠하려면 문장 우클릭 → 형광펜(또는 Ctrl+H). Markdown 내보내기에 포함. |
| 내보내기 | 번역본 PDF, 원문·번역 나란히 PDF, 메모·단어장 Markdown. |

번역은 지금 보고 있는 쪽부터 3개 요청을 동시에 진행하고, 끝난 부분은 즉시 저장·표시됩니다. 중간에 멈춰도 다시 시작하면 남은 문장만 번역합니다.

## 단축키

Ctrl+O 열기 · Ctrl+T 번역 시작/중지 · Ctrl+M 문장 메모 · Ctrl+H 문장 형광펜 · Ctrl+B 사전·메모 패널 · Ctrl+휠 / Ctrl+± 확대 · Ctrl+0 폭 맞춤 · PgUp/PgDn 쪽 이동

맥에서는 Ctrl 대신 ⌘. 단, 문장 메모는 **⌘⇧M**, 문장 형광펜은 **⌘⇧H** (⌘M·⌘H는 macOS가 창 최소화·앱 숨기기로 먼저 가져갑니다).

## 저장 위치

`문서\Paper Saver\Papers\<논문 제목>\` (맥: `~/Documents/Paper Saver/Papers/`) — `paper.pdf`, `layout.json`, `translation.json`, `notes.json`, `highlights.json`, `wordbook.json`, `exports\`.
같은 PDF를 다시 열면(파일 해시 기준) 기존 프로젝트를 그대로 엽니다. 사전 캐시와 설정은 `%APPDATA%\PaperSaver\` (맥: `~/Library/Application Support/PaperSaver/`).

## 개인정보

GPT로 전송되는 것은 **논문 텍스트뿐**입니다(처음 사용할 때 동의를 묻고, 설정에서 끌 수 있음). PDF 파일·그림·메모는 전송되지 않습니다.

## 레이아웃 분석 점검

```
.venv\Scripts\python run.py --inspect paper.pdf 4      (맥: .venv/bin/python run.py --inspect ...)
```
쪽마다 문단 종류(본문/제목/캡션/수식/그림 등), 단(column), 문장 분할을 출력합니다. 번역이 이상한 문단이 있으면 여기서 원인을 봅니다.

## 알려진 한계

- 표 **안의** 글자는 번역하지 않습니다(원본 그대로). 캡션은 번역합니다.
- 스캔 PDF(텍스트 레이어 없음)는 번역할 문장이 없습니다.
- 드롭캡(첫 글자 장식)은 번역 페이지에서 사라지고 번역문이 일반 글자로 시작합니다.
- 레이아웃 분석은 휴리스틱입니다. IEEE·ACM·arXiv·NeurIPS 논문으로 검증했으며, 특이한 양식에서는 문단이 쪼개지거나 합쳐질 수 있습니다.

## 테스트

```
.venv\Scripts\python -m pytest
```
