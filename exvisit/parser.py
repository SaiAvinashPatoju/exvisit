"""Hand-rolled recursive-descent parser for .exv — matches spec/exvisit-dsl-v0.1.md.

No external deps; fast enough for reference impl. Rust port uses pest."""
from __future__ import annotations
import re
from typing import List, Optional, Tuple
from .ast import exvisitDoc, Namespace, Node, Edge, EdgeKind


class ParseError(Exception):
    def __init__(self, msg: str, line: int, col: int):
        super().__init__(f"[line {line}:{col}] {msg}")
        self.line = line
        self.col = col


# ---------- Lexer -----------------------------------------------------------
TOKEN_RE = re.compile(
    r"""
    (?P<COMMENT>\#[^\n]*)             |
    (?P<NEWLINE>\n)                   |
    (?P<WS>[ \t\r]+)                  |
    (?P<EDGES>===\s*edges\s*===)      |
    (?P<NS>@L\d+)                     |
    (?P<ARROW>->|~>)                  |
    (?P<LBRACK>\[)                    |
    (?P<RBRACK>\])                    |
    (?P<LBRACE>\{)                    |
    (?P<RBRACE>\})                    |
    (?P<COMMA>,)                      |
    (?P<LINELOC>lines=\d+\.\.\d+)   |
    (?P<STRING>"[^"\n]*")             |
    (?P<NUMBER>-?\d+)                 |
    # PATH first when it clearly contains path-ish chars (slash or wildcard)
    (?P<PATH>[A-Za-z0-9_\-.\*~]*[/\*][A-Za-z0-9_\-./\*~]*) |
    (?P<IDENT>[A-Za-z_][A-Za-z0-9_\.]*) |
    (?P<PATH2>[A-Za-z0-9_\-./\*~]+)
    """,
    re.VERBOSE,
)


class Tok:
    __slots__ = ("kind", "val", "line", "col")
    def __init__(self, kind, val, line, col):
        self.kind = kind; self.val = val; self.line = line; self.col = col
    def __repr__(self):
        return f"Tok({self.kind},{self.val!r}@{self.line}:{self.col})"


def tokenize(src: str) -> List[Tok]:
    toks: List[Tok] = []
    line, col = 1, 1
    i = 0
    while i < len(src):
        m = TOKEN_RE.match(src, i)
        if not m:
            raise ParseError(f"unexpected char {src[i]!r}", line, col)
        kind = m.lastgroup
        val = m.group()
        if kind == "NEWLINE":
            toks.append(Tok("NL", val, line, col))
            line += 1; col = 1
        elif kind in ("WS", "COMMENT"):
            col += len(val)
        else:
            # unify PATH and PATH2 into a single PATH kind
            if kind == "PATH2":
                kind = "PATH"
            toks.append(Tok(kind, val, line, col))
            col += len(val)
        i = m.end()
    toks.append(Tok("EOF", "", line, col))
    return toks


# ---------- Parser ----------------------------------------------------------
class _P:
    def __init__(self, toks: List[Tok]):
        # strip newlines into a sentinel we can use for row boundaries
        self.toks = toks
        self.i = 0
        self._collected_edges: List[Edge] = []

    def _skip_nl(self):
        while self.toks[self.i].kind == "NL":
            self.i += 1

    def peek(self, offset=0) -> Tok:
        return self.toks[self.i + offset]

    def eat(self, *kinds) -> Tok:
        t = self.toks[self.i]
        if t.kind not in kinds:
            raise ParseError(f"expected {kinds}, got {t.kind} {t.val!r}", t.line, t.col)
        self.i += 1
        return t

    def accept(self, *kinds) -> Optional[Tok]:
        if self.toks[self.i].kind in kinds:
            t = self.toks[self.i]; self.i += 1; return t
        return None

    # --- grammar ---
    def parse_doc(self) -> exvisitDoc:
        self._skip_nl()
        root_ns = self.parse_namespace(parent_path="")
        self._skip_nl()
        # allow top-level edges blocks after the root namespace
        while self.peek().kind == "EDGES":
            self.eat("EDGES")
            self.parse_edges_block()
            self._skip_nl()
        doc = exvisitDoc(root=root_ns)
        doc.edges = self._collected_edges
        if self.peek().kind != "EOF":
            t = self.peek()
            raise ParseError(f"trailing tokens: {t.kind} {t.val!r}", t.line, t.col)
        return doc

    def parse_namespace(self, parent_path: str) -> Namespace:
        self._skip_nl()
        tok = self.eat("NS")
        level = int(tok.val[2:])
        name_tok = self.eat("IDENT")
        name = name_tok.val
        bounds = self.parse_bounds()
        src_glob = None
        if self.peek().kind == "STRING":
            src_glob = self.eat("STRING").val.strip('"')
        self.eat("LBRACE")
        path = f"{parent_path}.{name}" if parent_path else name
        ns = Namespace(level=level, name=name, bounds=bounds, src_glob=src_glob, path=path)
        while True:
            self._skip_nl()
            t = self.peek()
            if t.kind == "RBRACE":
                self.eat("RBRACE"); break
            if t.kind == "NS":
                ns.children.append(self.parse_namespace(parent_path=path))
            elif t.kind == "EDGES":
                self.eat("EDGES")
                self.parse_edges_block()
            elif t.kind == "IDENT":
                ns.nodes.append(self.parse_node_row(ns_path=path))
            else:
                raise ParseError(f"unexpected {t.kind} {t.val!r} in namespace body", t.line, t.col)
        return ns

    def parse_bounds(self) -> Tuple[int, int, int, int]:
        self.eat("LBRACK")
        nums = []
        for i in range(4):
            if i > 0:
                self.eat("COMMA")
            nums.append(int(self.eat("NUMBER").val))
        self.eat("RBRACK")
        return tuple(nums)  # type: ignore

    def parse_node_row(self, ns_path: str) -> Node:
        name = self.eat("IDENT").val
        bounds = self.parse_bounds()
        src_path = None
        line_range = None
        states: List[str] = []
        # optional src (PATH or IDENT-like token) and optional state machine
        while True:
            t = self.peek()
            if t.kind == "NL" or t.kind == "EOF":
                break
            if t.kind == "STRING":
                src_path = self.eat("STRING").val.strip('"'); continue
            if t.kind in ("PATH", "IDENT") and src_path is None and self._looks_like_path(t.val):
                src_path = self.eat(t.kind).val; continue
            if t.kind == "LINELOC":
                raw = self.eat("LINELOC").val[len("lines="):]
                start, end = raw.split("..", 1)
                line_range = (int(start), int(end))
                continue
            if t.kind == "LBRACE":
                states = self.parse_state_machine(); continue
            break
        return Node(name=name, bounds=bounds, src_path=src_path, line_range=line_range, states=states, ns_path=ns_path)

    @staticmethod
    def _looks_like_path(s: str) -> bool:
        return ("." in s) or ("/" in s) or s.endswith(".py") or s.endswith(".rs") or "*" in s

    def parse_state_machine(self) -> List[str]:
        self.eat("LBRACE")
        states = [self.eat("IDENT").val]
        while self.peek().kind == "ARROW":
            self.eat("ARROW")
            states.append(self.eat("IDENT").val)
        self.eat("RBRACE")
        return states

    def parse_edges_block(self):
        while True:
            self._skip_nl()
            t = self.peek()
            if t.kind in ("RBRACE", "EOF", "EDGES", "NS"):
                break
            if t.kind != "IDENT":
                raise ParseError(f"expected edge source, got {t.kind} {t.val!r}", t.line, t.col)
            src = self.eat("IDENT").val
            arrow = self.eat("ARROW").val
            dst = self.eat("IDENT").val
            kind = EdgeKind.SYNC if arrow == "->" else EdgeKind.ASYNC
            self._collected_edges.append(Edge(src=src, dst=dst, kind=kind))


def parse(src: str) -> exvisitDoc:
    toks = tokenize(src)
    p = _P(toks)
    return p.parse_doc()

