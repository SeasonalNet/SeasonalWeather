"""Conservative, pre-compilation safety checks for configured TTS regexes."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

MAX_REGEX_REPETITION = 256
MAX_CONFIGURED_REGEX_RULES = 32
MAX_CONFIGURED_REGEX_PATTERN = 256
MAX_CONFIGURED_REGEX_REPLACEMENT = 512
MAX_CONFIGURED_REGEX_REPLACEMENTS = 8192
MAX_REGEX_CLASS_RANGE = 256

_PYTHON_IGNORECASE_FAMILIES = (
    frozenset(("I", "i", "İ", "ı")),
    frozenset(("S", "s", "ſ")),
    frozenset(("K", "k", "K")),
)


def validate_replacement(replacement: str) -> None:
    """Apply the one bounded replacement contract used by config and runtime."""

    if not isinstance(replacement, str) or len(replacement) > MAX_CONFIGURED_REGEX_REPLACEMENT:
        raise ValueError("text override replacement is overlong or invalid")
    if "\x00" in replacement:
        raise ValueError("text override replacement contains NUL")
    if re.search(r"\\(?:[0-9]|g|k)", replacement):
        raise ValueError("text override replacement uses an unsupported backreference")
    try:
        re.compile("").sub(replacement, "")
    except (re.error, OverflowError, ValueError) as exc:
        raise ValueError("text override replacement is invalid") from exc


@dataclass(frozen=True)
class _Symbols:
    chars: frozenset[str] = frozenset()
    unknown: bool = False

    def overlaps(self, other: _Symbols) -> bool:
        return self.unknown or other.unknown or bool(self.chars & other.chars)


@dataclass(frozen=True)
class _Repeat:
    first: _Symbols
    last: _Symbols
    variable: bool
    unbounded: bool
    at_start: bool = True
    at_end: bool = True


@dataclass(frozen=True)
class _Info:
    nullable: bool
    first: _Symbols
    last: _Symbols
    repeats: tuple[_Repeat, ...] = ()
    alternation: bool = False


def _union(items: list[_Symbols]) -> _Symbols:
    return _Symbols(frozenset().union(*(item.chars for item in items)), any(item.unknown for item in items))


class _Parser:
    _ESCAPED_LITERAL = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    _SUPPORTED_ESCAPES = frozenset(
        "abBdfnNrsStuvwWxUDAZz0123456789"
    )

    def __init__(self, pattern: str, flags: int = 0) -> None:
        self.pattern = pattern
        self.flags = flags
        self.index = 0

    def parse(self) -> _Info:
        info = self._expression()
        if self.index != len(self.pattern):
            raise ValueError("text override regex has unmatched grouping")
        return info

    def _expression(self) -> _Info:
        branches = [self._sequence()]
        while self._take("|"):
            branches.append(self._sequence())
        if len(branches) == 1:
            return branches[0]
        if any(item.nullable for item in branches):
            # A nullable alternative can be skipped at every repeated group,
            # creating exponentially many ways to divide the same input.
            # Reject it before Python's compiler or matcher sees the pattern.
            raise ValueError("text override regex has a nullable alternative")
        self._check_ambiguous_alternation(branches)
        return _Info(
            any(item.nullable for item in branches),
            _union([item.first for item in branches]),
            _union([item.last for item in branches]),
            tuple(repeat for item in branches for repeat in item.repeats),
            True,
        )

    @staticmethod
    def _check_ambiguous_alternation(branches: list[_Info]) -> None:
        """Reject alternatives that can consume the same first symbol.

        A branch-prefix such as ``a|aa`` is exponentially ambiguous when
        repeated in a sequence, even though neither branch has a quantifier.
        The conservative grammar keeps only structurally disjoint branches;
        case-expanded symbols include Python's supported IGNORECASE families.
        """

        for index, left in enumerate(branches):
            for right in branches[index + 1 :]:
                if left.first.overlaps(right.first):
                    raise ValueError("text override regex has ambiguous competing alternatives")

    def _sequence(self) -> _Info:
        factors: list[_Info] = []
        while self.index < len(self.pattern) and self.pattern[self.index] not in ")|":
            factors.append(self._factor())
        if not factors:
            return _Info(True, _Symbols(), _Symbols())
        self._check_competing_repetitions(factors)
        self._check_sequence_boundaries(factors)
        first_items, last_items = self._sequence_edges(factors)
        repeats = self._sequence_repeats(factors)
        return _Info(
            all(item.nullable for item in factors),
            _union(first_items),
            _union(last_items),
            repeats,
            any(item.alternation for item in factors),
        )

    @staticmethod
    def _check_competing_repetitions(factors: list[_Info]) -> None:
        """Reject variable repeats that can divide the same input region.

        A minimum greater than zero does not make adjacent repeats safe.  Two
        variable atoms are ambiguous whenever the factors between them can be
        skipped and their boundary symbols overlap.  Fixed repetitions are
        deliberately absent from this check: their work is bounded and their
        syntax remains useful for configuration authors.
        """

        occurrences = [
            (index, repeat) for index, factor in enumerate(factors) for repeat in factor.repeats if repeat.variable
        ]
        for left_index, left in occurrences:
            for right_index, right in occurrences:
                if right_index <= left_index:
                    continue
                if not all(factor.nullable for factor in factors[left_index + 1 : right_index]):
                    continue
                if left.last.overlaps(right.first):
                    raise ValueError("text override regex has competing repeated atoms")

    def _check_sequence_boundaries(self, factors: list[_Info]) -> None:
        for index, factor in enumerate(factors):
            for repeat in factor.repeats:
                self._check_repeat_boundaries(repeat, factors, index)

    def _check_repeat_boundaries(self, repeat: _Repeat, factors: list[_Info], index: int) -> None:
        at_start = repeat.at_start and all(item.nullable for item in factors[:index])
        at_end = repeat.at_end and all(item.nullable for item in factors[index + 1 :])
        if repeat.variable and at_start:
            self._check_boundary(repeat.first, factors, index, step=-1, attribute="last")
        if repeat.variable and at_end:
            self._check_boundary(repeat.last, factors, index, step=1, attribute="first")

    @staticmethod
    def _sequence_edges(factors: list[_Info]) -> tuple[list[_Symbols], list[_Symbols]]:
        first_items: list[_Symbols] = []
        for factor in factors:
            first_items.append(factor.first)
            if not factor.nullable:
                break
        last_items: list[_Symbols] = []
        for factor in reversed(factors):
            last_items.append(factor.last)
            if not factor.nullable:
                break
        return first_items, last_items

    @staticmethod
    def _sequence_repeats(factors: list[_Info]) -> tuple[_Repeat, ...]:
        return tuple(
            replace(
                repeat,
                at_start=repeat.at_start and all(item.nullable for item in factors[:index]),
                at_end=repeat.at_end and all(item.nullable for item in factors[index + 1 :]),
            )
            for index, factor in enumerate(factors)
            for repeat in factor.repeats
        )

    @staticmethod
    def _check_boundary(symbols: _Symbols, factors: list[_Info], index: int, *, step: int, attribute: str) -> None:
        cursor = index + step
        while 0 <= cursor < len(factors):
            if symbols.overlaps(getattr(factors[cursor], attribute)):
                raise ValueError("text override regex has competing repeated atoms")
            if not factors[cursor].nullable:
                break
            cursor += step

    def _factor(self) -> _Info:
        atom = self._atom()
        quantifier = self._quantifier()
        if quantifier is None:
            return atom
        # Python re gives these suffixes lazy/possessive semantics.  They are
        # intentionally outside this conservative grammar; treating the
        # suffix as a literal would make the proof disagree with the matcher.
        if self.index < len(self.pattern) and self.pattern[self.index] in "?+":
            raise ValueError("text override regex uses an unsupported lazy or possessive quantifier")
        minimum, maximum = quantifier
        return self._repeat_atom(atom, minimum, maximum)

    def _quantifier(self) -> tuple[int, int | None] | None:
        if self.index >= len(self.pattern):
            return None
        char = self.pattern[self.index]
        if char == "*":
            self.index += 1
            return 0, None
        if char == "+":
            self.index += 1
            return 1, None
        if char == "?":
            self.index += 1
            return 0, 1
        if char == "{":
            return self._interval()
        return None

    @staticmethod
    def _repeat_atom(atom: _Info, minimum: int, maximum: int | None) -> _Info:
        if atom.repeats:
            raise ValueError("text override regex has nested repeated atoms")
        if atom.alternation:
            raise ValueError("text override regex has ambiguous repeated grouping")
        if atom.nullable and maximum != minimum:
            raise ValueError("text override regex repeats a nullable atom")
        return _Info(
            minimum == 0 or atom.nullable,
            atom.first,
            atom.last,
            (_Repeat(atom.first, atom.last, maximum != minimum, maximum is None),),
        )

    def _interval(self) -> tuple[int, int | None]:
        self.index += 1
        minimum = self._read_interval_number()
        if self.index >= len(self.pattern):
            raise ValueError("text override regex has an unterminated repetition interval")
        if self.pattern[self.index] == "}":
            maximum: int | None = minimum
            self.index += 1
        elif self.pattern[self.index] == ",":
            self.index += 1
            maximum = self._read_optional_interval_number()
            if self.index >= len(self.pattern) or self.pattern[self.index] != "}":
                raise ValueError("text override regex has an invalid repetition interval")
            self.index += 1
        else:
            raise ValueError("text override regex has an invalid repetition interval")
        self._validate_interval(minimum, maximum)
        return minimum, maximum

    @staticmethod
    def _validate_interval(minimum: int, maximum: int | None) -> None:
        if maximum is not None and maximum < minimum:
            raise ValueError("text override regex has a descending repetition interval")
        if minimum > MAX_REGEX_REPETITION or (maximum is not None and maximum > MAX_REGEX_REPETITION):
            raise ValueError("text override regex repetition is overlarge")

    def _read_interval_number(self) -> int:
        start = self.index
        while self.index < len(self.pattern) and self.pattern[self.index].isdigit():
            self.index += 1
        if start == self.index:
            raise ValueError("text override regex has an invalid repetition interval")
        digits = self.pattern[start : self.index]
        if len(digits) > 3:
            raise ValueError("text override regex repetition is overlarge")
        return int(digits)

    def _read_optional_interval_number(self) -> int | None:
        start = self.index
        while self.index < len(self.pattern) and self.pattern[self.index].isdigit():
            self.index += 1
        if start == self.index:
            return None
        digits = self.pattern[start : self.index]
        if len(digits) > 3:
            raise ValueError("text override regex repetition is overlarge")
        return int(digits)

    def _atom(self) -> _Info:
        if self.index >= len(self.pattern):
            raise ValueError("text override regex has an empty atom")
        char = self.pattern[self.index]
        self.index += 1
        return self._atom_for_char(char)

    def _atom_for_char(self, char: str) -> _Info:
        if char in "^$":
            return _Info(True, _Symbols(), _Symbols())
        if char == ".":
            return self._wildcard()
        if char == "[":
            return self._character_class()
        if char == "(":
            return self._group()
        if char == "\\":
            return self._escape()
        if char in "){|":
            raise ValueError("text override regex has invalid grouping or repetition")
        return self._literal(char)

    @staticmethod
    def _wildcard() -> _Info:
        symbols = _Symbols(unknown=True)
        return _Info(False, symbols, symbols)

    def _literal(self, char: str) -> _Info:
        symbols = _Symbols(frozenset(self._case_variants({char})))
        return _Info(False, symbols, symbols)

    def _case_variants(self, chars: set[str]) -> set[str]:
        if not self.flags & re.IGNORECASE:
            return chars
        variants = set(chars)
        for char in chars:
            variants.update((char.lower(), char.upper(), char.casefold()))
            for family in _PYTHON_IGNORECASE_FAMILIES:
                if char in family:
                    variants.update(family)
        return variants

    def _group(self) -> _Info:
        if self.index < len(self.pattern) and self.pattern[self.index] == "?":
            if not self.pattern.startswith("?:", self.index):
                raise ValueError("text override uses an unsafe regex group")
            self.index += 2
        info = self._expression()
        if self.index >= len(self.pattern) or self.pattern[self.index] != ")":
            raise ValueError("text override regex has unmatched grouping")
        self.index += 1
        return info

    def _escape(self) -> _Info:
        if self.index >= len(self.pattern):
            raise ValueError("text override regex has a trailing escape")
        escaped = self.pattern[self.index]
        self.index += 1
        self._validate_escape(escaped)
        if escaped == "x":
            literal = self._read_hex_escape(2)
            return self._literal(literal)
        if escaped == "u":
            literal = self._read_hex_escape(4)
            return self._literal(literal)
        if escaped == "U":
            literal = self._read_hex_escape(8)
            return self._literal(literal)
        if escaped in {"A", "Z", "z", "b", "B"}:
            return _Info(True, _Symbols(), _Symbols())
        if escaped in {"d", "D", "w", "W", "s", "S"}:
            symbols = _Symbols(unknown=True)
            return _Info(False, symbols, symbols)
        literal = self._ESCAPED_LITERAL.get(escaped, escaped)
        symbols = _Symbols(frozenset({literal}))
        return _Info(False, symbols, symbols)

    @classmethod
    def _validate_escape(cls, escaped: str) -> None:
        if escaped.isdigit() or escaped == "g":
            raise ValueError("text override regex uses an unsafe backreference")
        if escaped == "N":
            raise ValueError("text override regex uses an unsupported Unicode named escape")
        if escaped.isalpha() and escaped not in cls._SUPPORTED_ESCAPES:
            raise ValueError("text override regex uses an unsupported escape")

    def _read_hex_escape(self, length: int) -> str:
        end = self.index + length
        value = self.pattern[self.index : end]
        if len(value) != length or any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("text override regex has an invalid hexadecimal escape")
        self.index = end
        codepoint = int(value, 16)
        try:
            return chr(codepoint)
        except ValueError as exc:
            raise ValueError("text override regex has an invalid Unicode escape") from exc

    def _character_class(self) -> _Info:
        negated = self.index < len(self.pattern) and self.pattern[self.index] == "^"
        if negated:
            self.index += 1
        chars, unknown, closed = self._class_contents()
        unknown = unknown or negated
        if not closed:
            raise ValueError("text override regex has an unterminated character class")
        symbols = _Symbols(frozenset(self._case_variants(chars)), unknown)
        return _Info(False, symbols, symbols)

    def _class_contents(self) -> tuple[set[str], bool, bool]:
        chars: set[str] = set()
        unknown = False
        while self.index < len(self.pattern):
            if self.pattern[self.index] == "]" and chars:
                self.index += 1
                return chars, unknown, True
            start_chars, start_unknown = self._class_item()
            chars, unknown = self._class_piece(chars, unknown, start_chars, start_unknown)
        return chars, unknown, False

    def _class_piece(
        self, chars: set[str], unknown: bool, start_chars: set[str], start_unknown: bool
    ) -> tuple[set[str], bool]:
        if not self._has_class_range():
            chars.update(start_chars)
            return chars, unknown or start_unknown
        self.index += 1
        return self._expand_class_range(chars, unknown, start_chars, start_unknown)

    def _has_class_range(self) -> bool:
        return (
            self.index < len(self.pattern) - 1
            and self.pattern[self.index] == "-"
            and self.pattern[self.index + 1] != "]"
        )

    def _expand_class_range(
        self, chars: set[str], unknown: bool, start_chars: set[str], start_unknown: bool
    ) -> tuple[set[str], bool]:
        end_chars, end_unknown = self._class_item()
        if len(start_chars) != 1 or len(end_chars) != 1 or start_unknown or end_unknown:
            return chars, True
        first, last = ord(next(iter(start_chars))), ord(next(iter(end_chars)))
        if last < first:
            raise ValueError("text override regex has a descending character range")
        if last - first + 1 > MAX_REGEX_CLASS_RANGE:
            raise ValueError("text override regex character range is overlarge")
        chars.update(chr(code) for code in range(first, last + 1))
        return chars, unknown

    def _class_item(self) -> tuple[set[str], bool]:
        if self.index >= len(self.pattern):
            raise ValueError("text override regex has an unterminated character class")
        if self.pattern[self.index] != "\\":
            char = self.pattern[self.index]
            self.index += 1
            return {char}, False
        self.index += 1
        if self.index >= len(self.pattern):
            raise ValueError("text override regex has a trailing class escape")
        escaped = self.pattern[self.index]
        self.index += 1
        self._validate_class_escape(escaped)
        if escaped == "x":
            return self._case_variants({self._read_hex_escape(2)}), False
        if escaped == "u":
            return self._case_variants({self._read_hex_escape(4)}), False
        if escaped == "U":
            return self._case_variants({self._read_hex_escape(8)}), False
        if escaped in {"d", "D", "w", "W", "s", "S"}:
            return set(), True
        if escaped in {"b"}:
            return {"\b"}, False
        return self._case_variants({self._ESCAPED_LITERAL.get(escaped, escaped)}), False

    @classmethod
    def _validate_class_escape(cls, escaped: str) -> None:
        if escaped in {"N", "g", "k"} or escaped.isdigit():
            raise ValueError("text override regex uses an unsupported class escape")
        if escaped.isalpha() and escaped not in cls._SUPPORTED_ESCAPES:
            raise ValueError("text override regex uses an unsupported class escape")

    def _take(self, char: str) -> bool:
        if self.index < len(self.pattern) and self.pattern[self.index] == char:
            self.index += 1
            return True
        return False


def validate_safe_regex(pattern: str, *, flags: int = 0) -> None:
    """Validate the bounded subset before Python's regex engine is entered."""

    _validate_flags(flags)
    if len(pattern) > MAX_CONFIGURED_REGEX_PATTERN:
        raise ValueError("text override regex pattern is overlong")
    _Parser(pattern, flags).parse()
    try:
        re.compile(pattern, flags)
    except (re.error, OverflowError, ValueError) as exc:
        raise ValueError("text override regex is invalid") from exc


def compile_safe_regex(pattern: str, *, flags: int = 0) -> re.Pattern[str]:
    """Compile only after the exact conservative grammar has accepted it."""

    _validate_flags(flags)
    if len(pattern) > MAX_CONFIGURED_REGEX_PATTERN:
        raise ValueError("text override regex pattern is overlong")
    _Parser(pattern, flags).parse()
    try:
        return re.compile(pattern, flags)
    except (re.error, OverflowError, ValueError) as exc:
        raise ValueError("text override regex is invalid") from exc


def _validate_flags(flags: int) -> None:
    if flags & ~int(re.IGNORECASE):
        raise ValueError("text override regex uses unsupported flags")
