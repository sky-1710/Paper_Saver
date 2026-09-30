"""GPT answers shown in Qt: LaTeX made readable, bold that works beside Korean."""

from __future__ import annotations

import html
import re

from paper_saver.render.markdown_display import latex_to_html, prepare_markdown

#: The answer that came back broken (§ GPT tab), verbatim.
ANSWER = """캡처의 "10개의 프로세스가 실행 중이면 각 프로세스의 타임 슬라이스는 4.8ms 대신 6ms"라는 말은, **계산된 실행 시간이 너무 짧으면 최소값인 6ms를 보장한다**는 뜻입니다.

앞의 예시에서 목표 시간인 `sched_latency`는 48ms입니다. 이를 실행할 프로세스 10개에 똑같이 나누면, 각 프로세스가 한 번에 실행하는 시간인 **타임 슬라이스(time slice)**는 다음과 같습니다.

$$ 48\\text{ms} \\div 10 = 4.8\\text{ms} $$

그런데 **최소 실행 시간(min_granularity)**이 6ms라면, 4.8ms는 너무 짧으므로 각 프로세스에 **6ms씩** 줍니다. 이 예시는 `min_granularity`가 6ms라고 가정한 것입니다.

따라서 10개가 한 번씩 실행하는 데는 실행 시간만 합쳐 $10 \\times 6 = 60\\text{ms}$가 걸립니다."""


#: A later answer: multi-line $$ blocks, aligned environments, \_ and \% in text.
ALIGNED = "$$\n\\text{time\\_slice}_k\n=\n\\frac{\\text{weight}_k}{\\sum_{i=0}^{n-1}\\text{weight}_i} \\times \\text{sched\\_latency}\n$$\n\n즉, 프로세스 $k$의 **시간 조각(time slice)**은 '전체 가중치 중 자기 몫 × 배분할 전체 시간'입니다. 여기서는 전체 시간이 $48\\,\\text{ms}$입니다.\n\n### 1. A의 nice가 −5, B의 nice가 0이면\n\n**나이스 값(nice value)**에 대응하는 가중치(weight)가 각각 $3121$, $1024$입니다. nice 값이 낮은 A가 더 큰 가중치를 받습니다.\n\n$$\n\\begin{aligned}\n\\text{A의 실행 시간} &= 48 \\times \\frac{3121}{3121+1024} \\approx 36.14\\,\\text{ms} \\\\\n\\text{B의 실행 시간} &= 48 \\times \\frac{1024}{3121+1024} \\approx 11.86\\,\\text{ms}\n\\end{aligned}\n$$\n\nA가 B보다 약 3배 오래 실행됩니다. 슬라이드의 **$36\\,\\text{ms}$와 $12\\,\\text{ms}$는 근삿값**이라서, 엄밀히는 등호 대신 $\\approx$를 써야 합니다.\n\n### 2. 둘의 nice를 각각 5와 10으로 바꾸면\n\n가중치는 A가 $335$, B가 $110$이므로 다음과 같습니다.\n\n$$\n\\begin{aligned}\n\\text{A의 몫} &= \\frac{335}{335+110} \\approx 75.3\\% \\\\\n\\text{B의 몫} &= \\frac{110}{335+110} \\approx 24.7\\%\n\\end{aligned}\n$$"


def _shown(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def test_latex_becomes_readable_text() -> None:
    cases = {
        r"48\text{ms} \div 10 = 4.8\text{ms}": "48ms ÷ 10 = 4.8ms",
        r"10 \times 6 = 60\text{ms}": "10 × 6 = 60ms",
        r"\frac{a+b}{2}": "(a+b)/2",
        r"\frac{1}{N}\sum_{i=1}^{N} x_i^2": "1/N∑i=1N xi2",
        r"\alpha \leq \beta": "α ≤ β",
        r"\sqrt{x^2 + y^2}": "√(x2 + y2)",
        r"\mathbb{R}^{n \times d}": "ℝn × d",
        r"\hat{y}": "ŷ",  # y with a combining hat: drawn as ŷ
        r"\left( \frac{Q K^T}{\sqrt{d_k}} \right)": "( (Q KT)/(√dk) )",
        r"\operatorname{softmax}(x)": "softmax(x)",
        r"\text{if } x > 0 \\ 0 \text{ otherwise}": "if x > 0  0 otherwise",  # \\ is a <br>, stripped here
    }
    for source, expected in cases.items():
        assert _shown(latex_to_html(source)) == expected, source
    rendered = latex_to_html(r"x_i^2")
    assert "<sub>" in rendered and "<sup>" in rendered and "<i>x</i>" in rendered
    # No backslash survives to be drawn as a won sign.
    assert "\\" not in latex_to_html(r"\alpha + \unknowncommand + \{x\}")


def test_gpt_answer_prepared_for_qt() -> None:
    prepared = prepare_markdown(ANSWER)
    assert "\\text" not in prepared and "$" not in prepared
    assert "<b>타임 슬라이스(time slice)</b>는" in prepared
    assert "<b>최소 실행 시간(min&#95;granularity)</b>이" in prepared or "<b>최소 실행 시간(min_granularity)</b>이" in prepared
    assert "<b>6ms씩</b>" in prepared
    # Code spans are left exactly as written.
    assert "`sched_latency`" in prepared and "`min_granularity`" in prepared
    # Display maths is a paragraph of its own.
    assert "\n\n48ms ÷ 10 = 4.8ms\n\n" in _shown(prepared)


def test_prices_code_and_fences_are_not_maths() -> None:
    assert prepare_markdown("costs $5 and $6 today") == "costs $5 and $6 today"
    assert prepare_markdown("run `echo $HOME $PATH`") == "run `echo $HOME $PATH`"
    fenced = "```python\nx = '$a$' ** 2 ** 3\n```"
    assert prepare_markdown(fenced) == fenced


def test_qt_shows_the_answer_cleanly(qapp) -> None:
    from paper_saver.ui.markdown_view import markdown_html

    shown = _shown(markdown_html(ANSWER, None, 400))
    assert "**" not in shown and "$" not in shown and "₩" not in shown and "\\" not in shown
    assert "48ms ÷ 10 = 4.8ms" in shown and "10 × 6 = 60ms" in shown
    body = markdown_html(ANSWER, None, 400)
    assert re.search(r'font-weight:700;">타임 슬라이스\(time slice\)</span>', body)
    # The display formula, and only it, is centred.
    centred = re.findall(r'<p align="center"[^>]*>(.*?)</p>', body, re.S)
    assert [_shown(p) for p in centred] == ["48ms ÷ 10 = 4.8ms"]


def test_aligned_blocks_do_not_swallow_the_rest(qapp) -> None:
    """A <br> inside a span once made Qt drop everything after the formula."""
    from paper_saver.ui.markdown_view import markdown_html

    body = markdown_html(ALIGNED, None, 400)
    shown = _shown(body)
    assert "\\" not in shown and "$" not in shown and "**" not in shown
    assert "time_slice" in shown and "sched_latency" in shown
    centred = [_shown(p) for p in re.findall(r'<p align="center"[^>]*>(.*?)</p>', body, re.S)]
    # One centred paragraph per line of each formula.
    assert centred == [
        "time_slicek = weightk/(∑i=0n−1weighti) × sched_latency",
        "A의 실행 시간 = 48 × 3121/(3121+1024) ≈ 36.14 ms",
        "B의 실행 시간 = 48 × 1024/(3121+1024) ≈ 11.86 ms",
        "A의 몫 = 335/(335+110) ≈ 75.3%",
        "B의 몫 = 110/(335+110) ≈ 24.7%",
    ]
    # The text after every formula is still there.
    assert "A가 B보다 약 3배 오래 실행됩니다" in shown and "가중치는 A가 335, B가 110" in shown
