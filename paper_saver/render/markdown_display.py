"""Markdown as GPT writes it, made fit for Qt's Markdown renderer.

Qt's renderer is plain CommonMark, and GPT's answers trip it in two ways:

Maths
    Answers write formulas in LaTeX (``$x^2$``, ``$$\\frac{a}{b}$$``), which
    CommonMark shows as raw source - and the Korean UI font draws every
    backslash as a won sign, so ``\\text{ms}`` reads ``₩text{ms}``. Here the
    LaTeX is turned into Unicode with HTML sub/superscripts: not typeset, but
    a formula a reader can read at a glance. Only what is *shown* changes;
    the note file keeps the LaTeX, which Obsidian and Notion typeset properly.

Bold next to Korean
    CommonMark only closes ``**`` that is "right-flanking", so
    ``**타임 슬라이스(time slice)**는`` - a closing ``**`` between punctuation
    and a Hangul letter - stays literal asterisks. Korean attaches particles
    straight to the word, so this happens constantly. Pairs are turned into
    ``<b>`` tags, which Qt accepts inline.

Code spans and fenced blocks are left exactly as written.
"""

from __future__ import annotations

import html
import re

__all__ = ["center_display_maths", "latex_to_html", "prepare_markdown"]

_SYMBOLS = {
    # Greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # Operators and relations
    "times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆",
    "circ": "∘", "bullet": "∙", "oplus": "⊕", "otimes": "⊗", "odot": "⊙",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "ll": "≪", "gg": "≫",
    "approx": "≈", "sim": "∼", "simeq": "≃", "cong": "≅", "equiv": "≡", "propto": "∝",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
    "supseteq": "⊇", "cup": "∪", "cap": "∩", "setminus": "∖", "emptyset": "∅", "varnothing": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧",
    "lor": "∨", "vee": "∨", "mid": "∣", "parallel": "∥", "perp": "⊥",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⇔", "implies": "⇒",
    "mapsto": "↦", "uparrow": "↑", "downarrow": "↓", "longrightarrow": "⟶",
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "oint": "∮",
    "partial": "∂", "nabla": "∇", "infty": "∞", "prime": "′", "hbar": "ℏ", "ell": "ℓ",
    "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "angle": "∠", "triangle": "△", "degree": "°",
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "lvert": "|", "rvert": "|", "vert": "|", "lVert": "‖", "rVert": "‖", "Vert": "‖",
    "lbrace": "{", "rbrace": "}", "backslash": "\\",
    # Spacing
    "quad": " ", "qquad": "  ", "enspace": " ",
    # Size and style switches that change nothing here
    "displaystyle": "", "textstyle": "", "scriptstyle": "", "limits": "", "nolimits": "",
    "left": "", "right": "", "middle": "", "big": "", "Big": "", "bigg": "", "Bigg": "",
    "bigl": "", "bigr": "", "Bigl": "", "Bigr": "", "biggl": "", "biggr": "",
    "nonumber": "", "notag": "",
}
_FUNCTIONS = {
    "log", "ln", "lg", "exp", "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos",
    "arctan", "sinh", "cosh", "tanh", "max", "min", "sup", "inf", "lim", "liminf", "limsup",
    "arg", "argmax", "argmin", "det", "dim", "ker", "deg", "gcd", "Pr", "mod", "bmod", "Var", "Cov", "E",
}
_TEXT = {"text", "textrm", "mathrm", "operatorname", "mbox", "textnormal", "mathsf", "texttt", "textsf", "mathtt"}
_BOLD = {"textbf", "mathbf", "boldsymbol", "bm", "pmb"}
_ITALIC = {"textit", "mathit", "emph", "mathcal", "mathscr", "mathfrak"}
_ACCENTS = {"hat": "̂", "widehat": "̂", "bar": "̄", "tilde": "̃",
            "widetilde": "̃", "vec": "⃗", "dot": "̇", "ddot": "̈", "check": "̌"}
_DOUBLE_STRUCK = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "H": "ℍ"}
_PAIRED = {"cases": ("{ ", ""), "pmatrix": ("(", ")"), "bmatrix": ("[", "]"),
           "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"), "matrix": ("", "")}
#: Characters CommonMark would act on; written as entities so they stay text.
_MARKDOWN_SPECIAL = {"*": "&#42;", "_": "&#95;", "\\": "&#92;", "`": "&#96;", "[": "&#91;",
                     "]": "&#93;", "$": "&#36;", "~": "&#126;", "#": "&#35;", "|": "&#124;"}


def _text(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return "".join(_MARKDOWN_SPECIAL.get(c, c) for c in escaped)


def _plain(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


class _Latex:
    """A small recursive reader over one LaTeX expression."""

    def __init__(self, source: str) -> None:
        self.s = source
        self.i = 0

    def run(self, closer: str | None = None) -> str:
        out: list[str] = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if closer is not None and c == closer:
                self.i += 1
                break
            if c == "{":
                self.i += 1
                out.append(self.run("}"))
            elif c == "}":
                self.i += 1
            elif c in "^_":
                self.i += 1
                tag = "sup" if c == "^" else "sub"
                out.append(f"<{tag}>{self.atom()}</{tag}>")
            elif c == "\\":
                out.append(self.command())
            elif c.isalpha() and c.isascii():
                start = self.i
                while self.i < len(self.s) and self.s[self.i].isalpha() and self.s[self.i].isascii():
                    self.i += 1
                out.append(f"<i>{_text(self.s[start:self.i])}</i>")
            elif c.isspace():
                while self.i < len(self.s) and self.s[self.i].isspace():
                    self.i += 1
                out.append(" ")
            else:
                self.i += 1
                out.append({"&": " ", "~": " ", "-": "−", "*": "∗", "'": "′"}.get(c) or _text(c))
        return "".join(out)

    def _skip_space(self) -> None:
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def atom(self) -> str:
        """One argument: a group, a command, or a single character."""
        self._skip_space()
        if self.i >= len(self.s):
            return ""
        c = self.s[self.i]
        if c == "{":
            self.i += 1
            return self.run("}")
        if c == "\\":
            return self.command()
        self.i += 1
        return f"<i>{_text(c)}</i>" if c.isalpha() and c.isascii() else _text(c)

    def raw_group(self) -> str:
        """The literal text of the next ``{...}``, for \\text and friends."""
        self._skip_space()
        if self.i >= len(self.s):
            return ""
        if self.s[self.i] != "{":
            self.i += 1
            return self.s[self.i - 1]
        depth, start = 0, self.i
        while self.i < len(self.s):
            if self.s[self.i] == "{":
                depth += 1
            elif self.s[self.i] == "}":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start + 1 : self.i - 1]
            self.i += 1
        return self.s[start + 1 :]

    def optional(self) -> str:
        self._skip_space()
        if self.i < len(self.s) and self.s[self.i] == "[":
            end = self.s.find("]", self.i)
            if end > 0:
                inner = _Latex(self.s[self.i + 1 : end]).run()
                self.i = end + 1
                return inner
        return ""

    def command(self) -> str:
        self.i += 1  # the backslash
        if self.i >= len(self.s):
            return ""
        c = self.s[self.i]
        if not c.isalpha():
            self.i += 1
            if c == "\\":
                return "<br>"
            return {",": " ", ";": " ", ":": " ", "!": "", " ": " ", "|": "‖"}.get(c, _text(c))
        start = self.i
        while self.i < len(self.s) and self.s[self.i].isalpha():
            self.i += 1
        name = self.s[start : self.i]

        if name in ("left", "right", "bigl", "bigr", "Bigl", "Bigr") and self.i < len(self.s) and self.s[self.i] == ".":
            self.i += 1
            return ""
        if name in _SYMBOLS:
            return _SYMBOLS[name]
        if name in _FUNCTIONS:
            return _text(name) + ("" if self.i < len(self.s) and self.s[self.i] in "^_({[" else " ")
        if name in _TEXT:
            # Text mode still escapes: \text{time\_slice}, \text{75\%}.
            return _text(re.sub(r"\\([_%&$#{} ])", r"\1", self.raw_group()))
        if name in _BOLD:
            return f"<b>{self.atom()}</b>"
        if name in _ITALIC:
            return f"<i>{_plain_tags(self.atom())}</i>"
        if name == "mathbb":
            inner = self.raw_group()
            return "".join(_DOUBLE_STRUCK.get(ch, f"<b>{_text(ch)}</b>") for ch in inner)
        if name in _ACCENTS:
            inner = self.atom()
            if len(_plain(inner)) != 1:
                return inner
            # The combining mark goes inside the italic, so it composes with its letter.
            mark = _ACCENTS[name]
            return inner[:-4] + mark + "</i>" if inner.endswith("</i>") else inner + mark
        if name in ("overline", "widebar"):
            return f"<span style='text-decoration: overline'>{self.atom()}</span>"
        if name in ("underline",):
            return f"<u>{self.atom()}</u>"
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            top, bottom = self.atom(), self.atom()
            return f"{_wrap(top)}/{_wrap(bottom)}"
        if name == "binom":
            top, bottom = self.atom(), self.atom()
            return f"C({top}, {bottom})"
        if name == "sqrt":
            index = self.optional()
            body = self.atom()
            root = f"<sup>{index}</sup>√" if index else "√"
            return root + (body if re.fullmatch(r"[\w.]*", _plain(body).strip()) else f"({body})")
        if name == "not":
            following = self.atom()
            return following + "̸"
        if name in ("mathop", "mathrel", "mathbin", "mathord", "underbrace", "overbrace", "boxed", "hbox"):
            return self.atom()
        if name == "tag":
            return f" ({_text(self.raw_group())})"
        if name in ("label", "vspace", "hspace", "color", "textcolor"):
            self.raw_group()
            return self.atom() if name == "textcolor" else ""
        if name == "begin":
            env = self.raw_group().rstrip("*")
            if env in ("array", "tabular"):
                self.raw_group()
            return _PAIRED.get(env, ("", ""))[0]
        if name == "end":
            env = self.raw_group().rstrip("*")
            return _PAIRED.get(env, ("", ""))[1]
        return _text(name)


def _plain_tags(fragment: str) -> str:
    """Drop nested <i> so an italic group is not italic twice."""
    return re.sub(r"</?i>", "", fragment)


def _wrap(fragment: str) -> str:
    """Parenthesise a fraction part that is more than one symbol."""
    text = _plain(fragment).strip()
    if len(text) <= 1 or re.fullmatch(r"[\w.]+", text):
        return fragment.strip()
    return f"({fragment.strip()})"


def latex_to_html(expression: str) -> str:
    """One LaTeX expression as inline HTML that Qt shows and Markdown leaves alone."""
    result = _Latex(expression.strip()).run()
    return re.sub(r" {2,}", " ", result).strip()


# -- whole answers ----------------------------------------------------------------

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_DISPLAY = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
_INLINE = re.compile(r"(?<![\\$0-9A-Za-z])\$(?=[^\s$])([^$\n]*?[^\s$\\])\$(?!\d)|\\\((.+?)\\\)")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.S)
_BOLD_PAIR = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


#: A face drawn for maths: real italics, and combining accents that sit on
#: their letter. The Korean UI font has neither (its italic is a slant, and
#: its backslash is a won sign).
MATH_FONTS = "'Cambria Math', 'Cambria', 'STIX Two Math', 'Latin Modern Math', 'Times New Roman', serif"


#: Names no installed font: it only marks a display formula's paragraph so
#: the HTML can be centred afterwards (Markdown has no way to say so, and
#: Qt drops leading spaces).
DISPLAY_MARKER = "Paper Saver Display"


def _math(expression: str, *, display: bool = False) -> str:
    """Maths in its font. Line breaks (``\\\\``) never reach Qt as ``<br>``:
    a ``<br>`` inside an inline span makes Qt's Markdown reader drop the rest
    of the document. A display formula becomes one paragraph per line; an
    inline one keeps its lines apart with a wide space."""
    fonts = f"'{DISPLAY_MARKER}', {MATH_FONTS}" if display else MATH_FONTS
    lines = [line.strip() for line in latex_to_html(expression).split("<br>")]
    spans = [f'<span style="font-family: {fonts}">{line}</span>' for line in lines if line]
    return "\n\n".join(spans) if display else " ".join(spans)


def _display(match: re.Match[str]) -> str:
    expression = match.group(1) if match.group(1) is not None else match.group(2)
    # Paragraphs of their own; the source's own line breaks are only layout,
    # and a blank line among them must not end the formula early.
    return f"\n\n{_math(expression.replace(chr(10), ' '), display=True)}\n\n"


def center_display_maths(body: str) -> str:
    """Centre the paragraphs Qt made from display formulas."""
    return re.sub(
        r"<p (?=[^>]*>(?:(?!</p>).)*" + re.escape(DISPLAY_MARKER) + ")",
        '<p align="center" ',
        body,
        flags=re.S,
    )


def _inline(text: str) -> str:
    text = _INLINE.sub(lambda m: _math(m.group(1) if m.group(1) is not None else m.group(2)), text)
    return _BOLD_PAIR.sub(r"<b>\1</b>", text)


def _prose(chunk: str) -> str:
    chunk = _DISPLAY.sub(_display, chunk)
    out: list[str] = []
    pos = 0
    for match in _CODE_SPAN.finditer(chunk):
        out.append(_inline(chunk[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_inline(chunk[pos:]))
    return "".join(out)


def prepare_markdown(text: str) -> str:
    """GPT-flavoured Markdown rewritten so Qt's CommonMark shows it as meant."""
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        marker = _FENCE.match(line)
        if fence is None and marker:
            if prose:
                out.append(_prose("\n".join(prose)))
                prose = []
            fence = marker.group(1)[0] * 3
            out.append(line)
        elif fence is not None:
            out.append(line)
            if line.strip().startswith(fence):
                fence = None
        else:
            prose.append(line)
    if prose:
        out.append(_prose("\n".join(prose)))
    return "\n".join(out)
