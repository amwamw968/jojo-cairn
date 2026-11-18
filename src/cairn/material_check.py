"""Check OCR text against arithmetic it actually contains.

Nothing here is a stored answer key. A value is returned only when the
expression and the printed answer both parse and agree. Otherwise the caller
must leave the question unchecked.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_ELLIPSIS = r"(?:\.{3}|…|···|⋯)"
_MOUNT = re.compile(
    rf"(\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)\s*\+\s*(?:\d+(?:\.\d+)?\s*\+\s*)?{_ELLIPSIS}\s*\+\s*(\d+(?:\.\d+)?)"
)
_ANSWER = re.compile(r"(?:答案|Answer)\s*[:：]?\s*([0-9A-Za-z^+*/×x÷.()\-−]+)", re.I)
_LINEAR = re.compile(r"^\d+(?:\.\d+)?(?:\s*[+\-−]\s*\d+(?:\.\d+)?)+$")
_POW_CHARS = re.compile(r"^[0-9+\-−*/×x÷^().\s]+$")


def stem_only(body: str) -> str:
    return re.split(r"答案|Answer|解析", body or "", maxsplit=1)[0].strip()


def printed_answer(body: str) -> str | None:
    match = _ANSWER.search(body or "")
    if match is None:
        return None
    token = match.group(1).strip().strip(".")
    return token or None


def _normalize_text(text: str) -> str:
    translated = (text or "").translate(str.maketrans("０１２３４５６７８９＋－", "0123456789+-"))
    return translated.replace("×", "*").replace("÷", "/")


def parse_exercises(text: str) -> list[dict[str, str]]:
    """Split a page into exercises. A missing number still keeps the block order."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if re.match(r"(?:练|Practice)\s*\S", line, flags=re.I) and current:
            groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)
    items = []
    for index, group in enumerate(groups, 1):
        body = "\n".join(group).strip()
        if not re.search(r"\d", body):
            continue
        match = re.search(r"(?:练|Practice)\s*(\d+)", body, flags=re.I)
        items.append({"key": match.group(1) if match else f"n{index}", "body": body})
    if items:
        return items
    body = (text or "").strip()
    return [{"key": "", "body": body}] if body else []


def _decimal(value: str) -> Decimal | None:
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def _mountain(body: str) -> str | None:
    text = _normalize_text(body)
    mark = r"(?:\.{2,}|·+|…+|⋯+|\+\s*[·.]+\s*\+)"
    line = next((item for item in text.splitlines() if re.search(mark, item)), "")
    if not line and re.search(mark, text):
        line = text
    if not line:
        return None
    numbers = [_decimal(item) for item in re.findall(r"\d+(?:\.\d+)?", line)]
    numbers = [item for item in numbers if item is not None]
    if len(numbers) < 4:
        return None
    start, second, peak = numbers[0], numbers[1], max(numbers)
    step = second - start
    if step <= 0 or peak < start:
        return None
    steps = (peak - start) / step
    if steps != steps.to_integral_value():
        return None
    total = (steps + 1) * (start + peak) - peak
    rendered = format(total.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _squares(body: str) -> str | None:
    numbers = [int(item) for item in re.findall(r"(\d+)\s*(?:\^2|²)", body or "")]
    if len(numbers) < 4 or numbers[-1] != 1 or numbers[0] % 2 != 0:
        return None
    if numbers[1] != numbers[0] - 1:
        return None
    if not re.search(r"[-−]", body or ""):
        return None
    total = numbers[0] * (numbers[0] + 1) // 2
    return str(total)


def _normalize_power_expr(raw: str) -> str:
    text = raw.replace("×", "*").replace("x", "*").replace("X", "*").replace("÷", "/")
    text = text.replace("−", "-").replace(" ", "")
    return text


class _Value:
    def __init__(self, factors: dict[int, int] | None = None):
        self.factors = {base: exp for base, exp in (factors or {}).items() if exp != 0}

    def copy(self) -> "_Value":
        return _Value(dict(self.factors))

    def mul(self, other: "_Value") -> "_Value":
        merged = dict(self.factors)
        for base, exp in other.factors.items():
            merged[base] = merged.get(base, 0) + exp
        return _Value(merged)

    def div(self, other: "_Value") -> "_Value | None":
        merged = dict(self.factors)
        for base, exp in other.factors.items():
            merged[base] = merged.get(base, 0) - exp
        if any(exp < 0 for exp in merged.values()):
            return None
        return _Value(merged)

    def pow(self, exponent: int) -> "_Value | None":
        if exponent < 0:
            return None
        return _Value({base: exp * exponent for base, exp in self.factors.items()})

    def canonical(self) -> str:
        if not self.factors:
            return "1"
        parts = []
        for base in sorted(self.factors):
            exp = self.factors[base]
            parts.append(str(base) if exp == 1 else f"{base}^{exp}")
        return "*".join(parts)

    def as_int_text(self) -> str | None:
        if any(exp < 0 for exp in self.factors.values()):
            return None
        value = 1
        for base, exp in self.factors.items():
            if base <= 0 or exp > 40:
                return None
            value *= base**exp
            if value > 10**12:
                return None
        return str(value)


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.index = 0

    def parse(self) -> _Value | None:
        value = self._sum()
        if value is None or self.index != len(self.text):
            return None
        return value

    def _peek(self) -> str:
        return self.text[self.index] if self.index < len(self.text) else ""

    def _sum(self) -> _Value | None:
        # Powers in these sheets use explicit * and /. A bare minus is not used.
        return self._product()

    def _product(self) -> _Value | None:
        value = self._power()
        if value is None:
            return None
        while self._peek() in {"*", "/"}:
            op = self._peek()
            self.index += 1
            other = self._power()
            if other is None:
                return None
            value = value.mul(other) if op == "*" else value.div(other)
            if value is None:
                return None
        return value

    def _power(self) -> _Value | None:
        base = self._primary()
        if base is None:
            return None
        if self._peek() == "^":
            self.index += 1
            exponent = self._int()
            if exponent is None:
                return None
            return base.pow(exponent)
        return base

    def _primary(self) -> _Value | None:
        if self._peek() == "(":
            self.index += 1
            value = self._product()
            if value is None or self._peek() != ")":
                return None
            self.index += 1
            return value
        number = self._int()
        if number is None:
            return None
        return _Value({number: 1})

    def _int(self) -> int | None:
        start = self.index
        while self._peek().isdigit():
            self.index += 1
        if start == self.index:
            return None
        return int(self.text[start : self.index])


def _powers(body: str) -> str | None:
    stem = stem_only(body)
    match = re.search(r"[0-9(][0-9+\-−*/×x÷^().\s]*", stem)
    if match is None:
        return None
    expr = _normalize_power_expr(match.group(0))
    if "^" not in expr or not _POW_CHARS.fullmatch(expr):
        return None
    value = _Parser(expr).parse()
    if value is None:
        return None
    return value.canonical()


def _linear(body: str) -> str | None:
    stem = stem_only(body).replace("−", "-").replace(" ", "")
    match = re.search(r"\d+(?:\.\d+)?(?:[+\-]\d+(?:\.\d+)?)+", stem)
    if match is None or _ELLIPSIS.replace("(?:", "").strip() and re.search(_ELLIPSIS, stem):
        return None
    expr = match.group(0)
    if not _LINEAR.fullmatch(expr):
        return None
    total = Decimal(0)
    sign = 1
    for token in re.findall(r"[+\-]|\d+(?:\.\d+)?", expr):
        if token == "+":
            sign = 1
        elif token == "-":
            sign = -1
        else:
            number = _decimal(token)
            if number is None:
                return None
            total += sign * number
    text = format(total.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def compute_body(body: str) -> str | None:
    """Return a computed value, or None when the text is not a supported expression."""
    if re.search(r"(?:\^2|²)", body or ""):
        squared = _squares(body)
        if squared is not None:
            return squared
    if re.search(r"(?:\.{2,}|·+|…+|⋯+|\+\s*[·.]+\s*\+)", body or ""):
        return _mountain(body)
    powered = _powers(body)
    if powered is not None:
        return powered
    return _linear(body)


def _same(computed: str, printed: str) -> bool:
    left = _normalize_power_expr(computed)
    right = _normalize_power_expr(printed)
    if left == right:
        return True
    left_num, right_num = _decimal(left), _decimal(right)
    if left_num is not None and right_num is not None and left_num == right_num:
        return True
    parsed_left, parsed_right = _Parser(left).parse(), _Parser(right).parse()
    if parsed_left is None or parsed_right is None:
        return False
    if parsed_left.canonical() == parsed_right.canonical():
        return True
    left_int, right_int = parsed_left.as_int_text(), parsed_right.as_int_text()
    return left_int is not None and left_int == right_int


def checked_answer(stem_body: str, solution_body: str | None) -> str | None:
    """Agree only when the expression and a printed answer compute to the same value."""
    computed = compute_body(stem_body)
    printed = printed_answer(solution_body or "") or printed_answer(stem_body)
    if computed is None or printed is None:
        return None
    if not _same(computed, printed):
        return None
    return printed


def faithful_analysis(solution_body: str | None, answer: str) -> str | None:
    """Keep printed solution text only when it still contains the checked answer."""
    if not solution_body:
        return None
    parts = re.split(r"解析", solution_body, maxsplit=1)
    analysis = parts[1].strip() if len(parts) == 2 else ""
    if len(analysis) < 4:
        return None
    if _normalize_power_expr(answer) not in _normalize_power_expr(analysis) and answer not in analysis:
        return None
    return analysis[:4000]
