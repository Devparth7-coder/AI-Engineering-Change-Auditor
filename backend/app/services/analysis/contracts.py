"""Contract extraction from signatures and docstrings.

This module is the reason the auditor can generate a reproduction test that
does **not** inherit the assumptions of the defective implementation.

The expected behaviour used in generated tests is derived from the *declared
contract* only:

* the function name (``apply_discount`` returning a final price),
* parameter names and annotations,
* the docstring's stated semantics ("return the FINAL price payable"),
* documented worked examples ("price of 1000 and a discount of 20% -> 800"),
* documented ``Raises`` clauses.

If no expectation can be derived, the result is an explicit ``None`` and the
finding is reported as **inconclusive** rather than invented.  Nothing here
calls a model; every value is traceable to a line of source.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Literal

ReturnSemantics = Literal["net_remaining", "component_delta", "passthrough", "unknown"]

# Words that indicate the function returns what REMAINS after an adjustment.
_NET_WORDS = (
    "final", "net", "remaining", "payable", "after", "resulting", "discounted",
    "left", "balance", "total due", "amount due",
)
# Words that indicate the function returns only the SIZE of an adjustment.
_COMPONENT_WORDS = (
    "discount amount", "amount off", "savings", "fee", "surcharge", "delta",
    "difference", "the size of the discount", "how much is taken off",
)

RAISES_RE = re.compile(r"^\s*(?P<exc>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Exit))\b", re.M)

# "For a price of 1,000 and a discount of 20% the final price must be 800"
_EXAMPLE_EXPECT_RE = re.compile(
    r"""(?ix)
    (?:must\s+be|should\s+be|is\s+expected\s+to\s+be|expected\s*[:=]|returns?|yields?|->|=>|=\s*)
    \s*(?P<value>[-+]?\d[\d_,]*(?:\.\d+)?)
    """
)
_BINDING_RE = re.compile(
    r"""(?ix)
    \b(?P<name>[a-z_][a-z0-9_]{2,})\b
    \s*(?:of|=|:|is|equals?|set\s+to|at)\s*
    (?P<value>[-+]?\d[\d_,]*(?:\.\d+)?)\s*(?P<pct>%)?
    """
)
_CODE_LITERAL_RE = re.compile(r"``\s*(?P<expr>[A-Za-z_][A-Za-z0-9_.]*\s*==\s*[-+0-9.]+)\s*``")


@dataclass
class ParamContract:
    name: str
    annotation: str = ""
    default: str | None = None
    doc: str = ""
    domain: str = ""                 # e.g. "fraction in [0, 1]"
    is_fraction: bool = False
    position: int = 0


@dataclass
class ContractExample:
    """A worked example recovered from the declared contract."""

    inputs: dict[str, float]
    expected_output: float
    source_line: int | None = None
    source_text: str = ""
    derivation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": self.inputs,
            "expected_output": self.expected_output,
            "source_line": self.source_line,
            "source_text": self.source_text,
            "derivation": self.derivation,
        }


@dataclass
class FunctionContract:
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    params: list[ParamContract]
    return_annotation: str
    docstring: str
    docstring_line: int | None
    return_semantics: ReturnSemantics
    semantics_evidence: list[str]
    examples: list[ContractExample]
    raises: list[str]
    completeness: list[str]          # what is MISSING from the contract

    @property
    def has_expectation(self) -> bool:
        return bool(self.examples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "params": [p.__dict__ for p in self.params],
            "return_annotation": self.return_annotation,
            "return_semantics": self.return_semantics,
            "semantics_evidence": self.semantics_evidence,
            "examples": [e.to_dict() for e in self.examples],
            "raises": self.raises,
            "completeness": self.completeness,
        }


def _unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def _num(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("_", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _classify_semantics(name: str, docstring: str) -> tuple[ReturnSemantics, list[str]]:
    """Decide whether the declared contract promises a NET value or a COMPONENT."""
    hay_name = name.lower().replace("_", " ")
    evidence: list[str] = []

    # Explicit "returns the X" statements carry the most weight.
    first_line = ""
    for raw in docstring.splitlines():
        stripped = raw.strip()
        if stripped:
            first_line = stripped.lower()
            break

    net_hits = [w for w in _NET_WORDS if w in first_line or w in hay_name]
    comp_hits = [w for w in _COMPONENT_WORDS if w in first_line]

    if "final" in first_line or "net" in first_line or "payable" in first_line:
        evidence.append(f'docstring summary states a net/final result: "{first_line}"')
    for w in net_hits:
        if w in hay_name:
            evidence.append(f'function name contains net-result keyword "{w}"')

    if comp_hits and not net_hits:
        evidence.append(f'docstring summary states a component result: "{first_line}"')
        return "component_delta", evidence
    if net_hits:
        return "net_remaining", evidence
    if comp_hits:
        return "component_delta", evidence
    return "unknown", evidence


def _strip_raises_section(docstring: str) -> str:
    """Drop the ``Raises`` block so parameter domains are not read from it.

    Without this, a Raises line like "``price`` is negative or ``discount_rate``
    is outside ``[0, 1]``" makes *both* parameters look like fractions.
    """
    return re.split(r"(?im)^\s*(?:raises|exceptions?)\s*\n\s*[-=~^]+\s*$", docstring, maxsplit=1)[0]


def _param_domain(docstring: str, pname: str) -> tuple[str, bool]:
    """Find a documented domain for a parameter, e.g. "fraction in [0, 1]"."""
    scope = _strip_raises_section(docstring)
    pattern = re.compile(
        rf"(?is)``?{re.escape(pname)}``?\s*(?:is|:|must\s+be)?\s*(?P<dom>[^.\n]{{0,140}})"
    )
    domain_keywords = (
        "fraction", "[0, 1]", "[0,1]", "between 0 and 1", "range", "must be",
        "non-negative", "negative", "positive", "one of", "percent", "integer",
        "greater", "less", "at most", "at least",
    )
    best = ""
    for m in pattern.finditer(scope):
        candidate = m.group("dom").strip()
        if not candidate:
            continue
        if any(k in candidate.lower() for k in domain_keywords):
            best = candidate
            break
        if not best:
            best = candidate
    dom = best
    is_fraction = bool(
        re.search(r"(?i)fraction", dom)
        or re.search(r"\[0,\s*1\]", dom)
        or re.search(r"(?i)between 0 and 1", dom)
        or re.search(rf"(?i)``?{re.escape(pname)}``?.{{0,140}}0\.\d+\s*means", scope)
    )
    return dom, is_fraction


def _docstring_line_offset(func: ast.AST) -> int | None:
    body = getattr(func, "body", [])
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0].lineno
    return None


def _unwrap_docstring(docstring: str) -> list[tuple[int, str]]:
    """Join hard-wrapped lines into paragraphs, then split into sentences.

    Docstrings wrap mid-sentence, so a naive per-line scan separates a worked
    example's inputs ("a price of 1000") from its expectation ("must be 800").
    Returns ``(paragraph_index, sentence)`` pairs.
    """
    paragraphs: list[str] = []
    buf: list[str] = []
    for raw in docstring.splitlines():
        stripped = raw.strip()
        # Blank lines and section underlines ("------") end a paragraph.
        if not stripped or re.fullmatch(r"[-=~^]{3,}", stripped):
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        buf.append(stripped)
    if buf:
        paragraphs.append(" ".join(buf))

    sentences: list[tuple[int, str]] = []
    for idx, para in enumerate(paragraphs):
        for sent in re.split(r"(?<=[.])\s+(?=[A-Z(`])", para):
            sent = sent.strip()
            if sent:
                sentences.append((idx, sent))
    return sentences


def _extract_examples(
    docstring: str,
    params: list[ParamContract],
    doc_line: int | None,
) -> list[ContractExample]:
    """Recover numeric worked examples from the declared contract."""
    if not docstring:
        return []
    param_names = {p.name for p in params}
    fraction_params = {p.name for p in params if p.is_fraction}
    examples: list[ContractExample] = []

    for idx, sentence in _unwrap_docstring(docstring):
        bindings: dict[str, float] = {}
        derivation: list[str] = []

        # 1. Pull ``name == value`` code literals out FIRST and remove them from
        #    the text being scanned.  Otherwise "``discount_rate == 0.20``" is
        #    misread by the expectation regex as "expected result is 0.20".
        scan_text = sentence
        for cm in reversed(list(_CODE_LITERAL_RE.finditer(sentence))):
            expr = cm.group("expr")
            lhs, _, rhs = expr.partition("==")
            name, val = lhs.strip(), _num(rhs.strip())
            if name in param_names and val is not None:
                bindings[name] = val
                derivation.append(f"``{expr}`` -> {name}={val:g}")
            scan_text = scan_text[: cm.start()] + " " + scan_text[cm.end() :]

        # 2. Find the declared expectation in the cleaned sentence.
        m = _EXAMPLE_EXPECT_RE.search(scan_text)
        if not m:
            continue
        expected = _num(m.group("value"))
        if expected is None:
            continue

        # 3. Bind the remaining prose ("a price of 1000", "a discount of 20%").
        for bm in _BINDING_RE.finditer(scan_text):
            value = _num(bm.group("value"))
            if value is None:
                continue
            match = _match_param(bm.group("name").lower(), param_names)
            if not match or match in bindings:
                continue
            pct = bm.group("pct") == "%"
            if pct and match in fraction_params:
                value = value / 100.0
                derivation.append(
                    f'"{bm.group(0).strip()}" -> {match}={value:g} '
                    f"(percent converted to the documented fraction domain)"
                )
            elif pct:
                derivation.append(f'"{bm.group(0).strip()}" -> {match}={value:g}%')
            else:
                derivation.append(f'"{bm.group(0).strip()}" -> {match}={value:g}')
            bindings[match] = value

        if not bindings:
            continue
        derivation.append(f"declared expected return value = {expected:g}")
        examples.append(
            ContractExample(
                inputs=bindings,
                expected_output=expected,
                source_line=(doc_line + idx) if doc_line else None,
                source_text=sentence,
                derivation=derivation,
            )
        )
    return _dedupe_examples(examples)


def _dedupe_examples(examples: list[ContractExample]) -> list[ContractExample]:
    seen: set[tuple[tuple[tuple[str, float], ...], float]] = set()
    out: list[ContractExample] = []
    for ex in examples:
        key = (tuple(sorted(ex.inputs.items())), ex.expected_output)
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    return out


_PARAM_ALIASES = {
    "price": {"price", "amount", "cost", "value", "base", "total"},
    "discount": {"discount", "rate", "pct", "percent", "discount_rate", "discount rate"},
    "quantity": {"quantity", "qty", "count", "n", "number"},
    "tier": {"tier", "level", "grade"},
}


def _match_param(word: str, param_names: set[str]) -> str | None:
    if word in param_names:
        return word
    norm = word.replace(" ", "_")
    if norm in param_names:
        return norm
    for pname in param_names:
        base = pname.lower().replace("_", "")
        if base == word.replace("_", ""):
            return pname
        if word in pname.lower().split("_") or pname.lower() in word:
            return pname
    for canonical, aliases in _PARAM_ALIASES.items():
        if word in aliases or canonical in word:
            for pname in param_names:
                if canonical in pname.lower() or pname.lower() in aliases:
                    return pname
    return None


def extract_contract(func: ast.FunctionDef | ast.AsyncFunctionDef, file_path: str, source_lines: list[str]) -> FunctionContract:
    """Build the declared contract for one function definition."""
    docstring = ast.get_docstring(func, clean=True) or ""
    doc_line = _docstring_line_offset(func)
    params: list[ParamContract] = []
    args = func.args
    defaults: list[ast.expr | None] = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    for pos, arg in enumerate(args.posonlyargs + args.args):
        if arg.arg in ("self", "cls"):
            continue
        dom, is_fraction = _param_domain(docstring, arg.arg)
        params.append(
            ParamContract(
                name=arg.arg,
                annotation=_unparse(arg.annotation),
                default=_unparse(defaults[pos]) if pos < len(defaults) and defaults[pos] is not None else None,
                doc=dom,
                domain=dom,
                is_fraction=is_fraction,
                position=pos,
            )
        )
    semantics, sem_ev = _classify_semantics(func.name, docstring)

    raises: list[str] = []
    raises_block = re.search(r"(?im)^\s*(raises|raise)\s*\n((?:\s{4,}.*\n?)+)", docstring + "\n")
    if raises_block:
        raises = [m.group("exc") for m in RAISES_RE.finditer(raises_block.group(2))]
    for exc in re.findall(r"\b(ValueError|TypeError|KeyError|ZeroDivisionError|OverflowError)\b", docstring):
        if exc not in raises:
            raises.append(exc)

    examples = _extract_examples(docstring, params, doc_line)

    completeness: list[str] = []
    if not docstring:
        completeness.append("no docstring: expected behaviour is not declared")
    if not examples:
        completeness.append("no numeric worked example found in the declared contract")
    if semantics == "unknown":
        completeness.append("return semantics (net result vs. component amount) are not stated")
    if not any(p.annotation for p in params):
        completeness.append("parameters are unannotated")

    return FunctionContract(
        name=func.name,
        qualified_name=func.name,
        file_path=file_path,
        line_start=func.lineno,
        line_end=getattr(func, "end_lineno", func.lineno),
        params=params,
        return_annotation=_unparse(func.returns),
        docstring=docstring,
        docstring_line=doc_line,
        return_semantics=semantics,
        semantics_evidence=sem_ev,
        examples=examples,
        raises=raises,
        completeness=completeness,
    )


def contract_for_symbol(
    source: str, file_path: str, symbol: str
) -> FunctionContract | None:
    """Look up one function's contract by name."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return extract_contract(node, file_path, lines)
    return None


def all_contracts(source: str, file_path: str) -> list[FunctionContract]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[FunctionContract] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(extract_contract(node, file_path, lines))
    return out
