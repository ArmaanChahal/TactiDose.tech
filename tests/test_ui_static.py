"""Static checks for the v2 web UI in ``tactidose/ui/static`` (no browser needed).

* Every JS file passes ``node --check`` as an ES module; relative imports resolve and every
  named import is exported by its module.
* Every ``/api/...`` path (and query parameter) used in JS exists in ``docs/API.md`` (v2, with
  its ``…/x`` shorthand expanded); helper calls (get/post/put/patch/del/upload/postRaw) use the
  documented HTTP method; the main v2 endpoints are all used.
* Every SSE topic the UI listens to exists in ``tactidose/core/bus.py`` ``Topic``.
* Every HTML page has ``lang``, one ``<main>``, a ``<title>``, a skip link, references only
  existing local assets, has no external URLs, no inline handlers / scripts / styles, and
  valid id references (labels, ARIA); ids used by the page's scripts exist.
* No ``innerHTML``-style injection of non-literal values, no eval, no on* handler properties.
* Accessibility contracts: three themes with >= 7:1 text contrast, rem-based font sizes,
  patient (>= 24px body, >= 5rem drop/talk buttons) and kiosk size minimums, reduced motion /
  contrast / forced colours / visible focus, no long ALL-CAPS text, ARIA tabs, explicit
  medication confirmation, plain words for every status/reason enum value in db/models.py.
* Pure JS logic (formatting, status view models, API client, SSE client, PCM audio, forms,
  notifications, chat, history, reports, demo checklist helpers) is unit-tested under Node.

Node-based tests are skipped (not failed) when ``node`` is not installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tactidose" / "ui" / "static"
API_MD = ROOT / "docs" / "API.md"
BUS_PY = ROOT / "tactidose" / "core" / "bus.py"
MODELS_PY = ROOT / "tactidose" / "db" / "models.py"
PROTOCOL_PY = ROOT / "tactidose" / "hardware" / "protocol.py"
NODE = shutil.which("node")

#: Pages served by the API (docs/API.md "Pages") -> static file.
PAGES = {"/login": "login.html", "/patient": "patient.html", "/care": "care.html", "/kiosk": "kiosk.html", "/demo": "demo.html"}
PAGE_ENTRY = {
    "login.html": "js/login.js",
    "patient.html": "js/patient.js",
    "care.html": "js/care.js",
    "kiosk.html": "js/kiosk.js",
    "demo.html": "js/demo.js",
}
JS_FILES = sorted(STATIC.rglob("*.js"))
HTML_FILES = sorted(STATIC.glob("*.html"))
CSS_FILES = sorted(STATIC.rglob("*.css"))
#: Namespace identifiers that look like URLs but are never fetched.
ALLOWED_URLS = {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"}

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rel(path: Path) -> str:
    return path.relative_to(STATIC).as_posix()


# --------------------------------------------------------------------------- JS source scanning


def _skip_string(src: str, i: int) -> int:
    """Index just past the quoted string starting at ``src[i]`` (' or ")."""
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return i


def _read_template(src: str, i: int) -> tuple[str, int]:
    """Parse the template literal at ``src[i]`` ('`'). ``${…}`` parts become ``{}``."""
    out: list[str] = []
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            out.append(src[i:i + 2])
            i += 2
        elif c == "`":
            return "".join(out), i + 1
        elif src.startswith("${", i):
            depth, i = 1, i + 2
            while i < len(src) and depth:
                ch = src[i]
                if ch in "'\"":
                    i = _skip_string(src, i)
                    continue
                if ch == "`":
                    _, i = _read_template(src, i)
                    continue
                depth += {"{": 1, "}": -1}.get(ch, 0)
                i += 1
            out.append("{}")
        else:
            out.append(c)
            i += 1
    return "".join(out), i


def _regex_may_start(src: str, i: int) -> bool:
    """True when a '/' at ``src[i]`` starts a regex literal (not a division)."""
    j = i - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    if src[j] in "(,=:[!&|?{};+-*%<>~^":
        return True
    word = re.search(r"([A-Za-z_$][\w$]*)$", src[:j + 1])
    return bool(word and word.group(1) in {"return", "typeof", "case", "of", "in", "new", "delete", "void", "throw"})


def _skip_regex(src: str, i: int) -> int:
    i += 1
    in_class = False
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            return i
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < len(src) and src[i].isalpha():
                i += 1
            return i
        i += 1
    return i


def js_literals(src: str) -> list[tuple[int, str]]:
    """(offset, text) of every string/template literal; comments and regexes are skipped."""
    found: list[tuple[int, str]] = []
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src[i] == "/" and _regex_may_start(src, i):
            i = _skip_regex(src, i)
        elif src[i] in "'\"":
            end = _skip_string(src, i)
            found.append((i, src[i + 1:end - 1]))
            i = end
        elif src[i] == "`":
            text, end = _read_template(src, i)
            found.append((i, text))
            i = end
        else:
            i += 1
    return found


def strip_js_comments(src: str) -> str:
    """Source with comments blanked out (string contents kept)."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src[i] == "/" and _regex_may_start(src, i):
            end = _skip_regex(src, i)
            out.append(src[i:end])
            i = end
        elif src[i] in "'\"":
            end = _skip_string(src, i)
            out.append(src[i:end])
            i = end
        elif src[i] == "`":
            _, end = _read_template(src, i)
            out.append(src[i:end])
            i = end
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------- API.md (v2) parsing

_SEGMENT_RE = re.compile(r"`((?:GET|POST|PUT|PATCH|DELETE)(?:/(?:GET|POST|PUT|PATCH|DELETE))*)?\s*([/…][^`\s]*)`")
_SECTION_PREFIX_RE = re.compile(r"\(`(/api/[^`]*?)/…`\)")


def _norm_path(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/")


def documented_routes() -> dict[tuple[str, str], set[str]]:
    """{(METHOD, normalised path): {documented query parameter names}} from docs/API.md.

    Handles the v2 shorthands: ``…/status`` under "## Patient data (`/api/patients/{pid}/…`)",
    ``GET/POST`` (two methods), ``… / `…/reject``` (a further path in the same cell, read as a
    sibling or a child of the previous one) and ``?download=1`` mentioned later in the row.
    """
    routes: dict[tuple[str, str], set[str]] = {}
    prefix = None
    for line in _read(API_MD).splitlines():
        if line.startswith("#"):
            m = _SECTION_PREFIX_RE.search(line)
            prefix = m.group(1) if m else None
            continue
        if not line.startswith("| `"):
            continue
        cells = line.split("|")
        row_keys: list[tuple[str, str]] = []
        prev_path, prev_methods = None, None
        for methods_text, raw in _SEGMENT_RE.findall(cells[1]):
            methods = methods_text.split("/") if methods_text else (prev_methods or [])
            path, _, query = raw.partition("?")
            if path.startswith("…"):
                rest = path[1:]
                if prev_path:
                    candidates = [prev_path.rsplit("/", 1)[0] + rest, prev_path + rest]
                elif prefix:
                    candidates = [prefix + rest]
                else:
                    continue
            else:
                candidates = [path]
            params = {kv.split("=", 1)[0] for kv in query.split("&") if kv}
            for method in methods:
                for cand in candidates:
                    key = (method, _norm_path(cand))
                    routes.setdefault(key, set()).update(params)
                    row_keys.append(key)
            prev_path, prev_methods = candidates[-1] if len(candidates) == 1 else candidates[0], methods
        for q in re.findall(r"`\?([^`\s]+)`", line):
            for key in row_keys:
                routes[key].update(kv.split("=", 1)[0] for kv in q.split("&") if kv)
    return routes


def _api_literals() -> list[tuple[Path, str]]:
    out = []
    for path in JS_FILES:
        for _, text in js_literals(_read(path)):
            if re.match(r"^/api/[a-z]", text):
                out.append((path, text))
    return out


def bus_topics() -> set[str]:
    src = _read(BUS_PY)
    body = src[src.index("class Topic"):src.index("@dataclass")]
    return set(re.findall(r'^\s+[A-Z_]+\s*=\s*"([^"]+)"', body, re.M))


def enum_values(path: Path, name: str) -> set[str]:
    src = _read(path)
    m = re.search(rf"class {name}\(str, Enum\):(.*?)(?=\n\n\n|\nclass |\n[A-Z_]+ = )", src, re.S)
    assert m, f"enum {name} not found in {path.name}"
    return set(re.findall(r'^\s+[A-Z_]+\s*=\s*"([^"]+)"', m.group(1), re.M))


# --------------------------------------------------------------------------- HTML parsing


class PageParser(HTMLParser):
    SKIP_TEXT_TAGS = {"kbd", "code", "option", "datalist", "script", "style", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None], int]] = []
        self.ids: list[str] = []
        self.title = ""
        self._in_title = False
        self.script_text: list[str] = []
        self._in_script = False
        self.labels_for: set[str] = set()
        self.texts: list[str] = []
        self._stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a, self.getpos()[0]))
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "title":
            self._in_title = True
        if tag == "script":
            self._in_script = True
        if tag == "label" and a.get("for"):
            self.labels_for.add(a["for"])
        if tag not in {"meta", "link", "input", "br", "img", "hr", "source"}:
            skip = tag in self.SKIP_TEXT_TAGS or "mono" in (a.get("class") or "").split()
            self._stack.append((tag, skip))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_script = False
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_script and data.strip():
            self.script_text.append(data)
        if data.strip() and not any(skip for _, skip in self._stack):
            self.texts.append(data.strip())

    def find(self, tag: str) -> list[dict[str, str | None]]:
        return [a for t, a, _ in self.tags if t == tag]


def parse_page(name: str) -> PageParser:
    p = PageParser()
    p.feed(_read(STATIC / name))
    p.close()
    return p


# --------------------------------------------------------------------------- module graph


_IMPORT_RE = re.compile(r"""(?:^|[\s;])(?:import|export)\s+(?:[^'";]*?\s+from\s+)?(['"])([^'"]+)\1""", re.M)
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\(\s*(['"])([^'"]+)\1\s*\)""")
_NAMED_IMPORT_RE = re.compile(r"""import\s*\{([^}]*)\}\s*from\s*'([^']+)'""")


def imports_of(path: Path) -> list[str]:
    src = strip_js_comments(_read(path))
    return [m.group(2) for m in _IMPORT_RE.finditer(src)] + [m.group(2) for m in _DYNAMIC_IMPORT_RE.finditer(src)]


def exports_of(path: Path) -> set[str]:
    src = strip_js_comments(_read(path))
    names = set(re.findall(r"export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z0-9_$]+)", src))
    for group in re.findall(r"export\s*\{([^}]*)\}", src):
        names.update(part.strip().split(" as ")[-1].strip() for part in group.split(",") if part.strip())
    return names


def module_closure(entry: Path) -> set[Path]:
    seen: set[Path] = set()
    todo = [entry]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for spec in imports_of(path):
            if spec.startswith("."):
                todo.append((path.parent / spec).resolve())
    return seen


# =========================================================================== files & syntax


def test_expected_files_exist():
    for rel in (*PAGES.values(), *PAGE_ENTRY.values(), "css/base.css", "css/login.css", "css/patient.css", "css/care.css",
                "css/demo.css", "css/kiosk.css", "js/api.js", "js/events.js", "js/session.js", "js/notifications.js",
                "js/theme.js", "js/theme-init.js", "js/voice.js", "js/pcm.js", "js/pcm-worklet.js", "js/reports.js",
                "js/status.js", "js/words.js", "img/favicon.svg"):
        assert (STATIC / rel).is_file(), rel
    for old in ("index.html", "caregiver.html", "js/caregiver.js", "js/kiosk-state.js"):
        assert not (STATIC / old).exists(), f"v1 file {old} should be gone"


@pytest.fixture(scope="module")
def js_tree(tmp_path_factory) -> Path:
    """Copy of static/js inside a {"type": "module"} package (so .js parses as ESM)."""
    root = tmp_path_factory.mktemp("uijs")
    shutil.copytree(STATIC / "js", root / "js")
    (root / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    return root


@needs_node
@pytest.mark.parametrize("path", JS_FILES, ids=_rel)
def test_js_passes_node_check(js_tree: Path, path: Path):
    target = js_tree / "js" / path.relative_to(STATIC / "js")
    proc = subprocess.run([NODE, "--check", str(target)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("path", JS_FILES, ids=_rel)
def test_js_imports_resolve_locally(path: Path):
    for spec in imports_of(path):
        assert spec.startswith("./") or spec.startswith("../"), f"{_rel(path)}: non-relative import {spec!r}"
        assert spec.endswith(".js"), f"{_rel(path)}: import without .js extension {spec!r}"
        assert (path.parent / spec).resolve().is_file(), f"{_rel(path)}: missing module {spec!r}"


def test_named_imports_are_exported():
    problems = []
    for path in JS_FILES:
        for names, spec in _NAMED_IMPORT_RE.findall(strip_js_comments(_read(path))):
            exported = exports_of((path.parent / spec).resolve())
            for name in (n.strip().split(" as ")[0].strip() for n in names.split(",")):
                if name and name not in exported:
                    problems.append(f"{_rel(path)}: {name!r} is not exported by {spec}")
    assert not problems, "\n".join(problems)


# =========================================================================== API contract


def test_api_md_parser_understands_the_v2_shorthands():
    routes = documented_routes()
    for key in [("POST", "/api/patients/{}/drops"), ("GET", "/api/patients/{}/status"), ("GET", "/api/demo/clock"),
                ("POST", "/api/demo/clock"), ("POST", "/api/patients/{}/scans/{}/reject"), ("GET", "/api/events"),
                ("POST", "/api/agent/transcribe"), ("DELETE", "/api/care/links/{}")]:
        assert key in routes, key
    assert {"days", "status"} <= routes[("GET", "/api/patients/{}/drops")]
    assert "download" in routes[("GET", "/api/reports/{}/pdf")]
    assert {"unread", "limit"} <= routes[("GET", "/api/notifications")]


def test_every_api_path_in_js_is_documented():
    by_path: dict[str, set[str]] = {}
    for (_method, path), params in documented_routes().items():
        by_path.setdefault(path, set()).update(params)
    used = _api_literals()
    assert len(used) > 40, "expected the UI to use most of the API"
    problems = []
    for path, text in used:
        route, _, query = text.partition("?")
        norm = _norm_path(route)
        if norm not in by_path:
            problems.append(f"{_rel(path)}: {text!r} is not in docs/API.md")
            continue
        for kv in filter(None, query.split("&")):
            key = kv.split("=", 1)[0]
            if key not in by_path[norm]:
                problems.append(f"{_rel(path)}: query parameter {key!r} of {route} is not documented")
    assert not problems, "\n".join(problems)


_CALL_RE = re.compile(r"\b(get|post|put|patch|del|upload|postRaw)\(\s*(?=['\"`])")
_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "del": "DELETE", "upload": "POST", "postRaw": "POST"}


def test_api_helper_calls_use_documented_methods():
    routes = documented_routes()
    checked = 0
    problems = []
    for path in JS_FILES:
        src = strip_js_comments(_read(path))
        literals = dict(js_literals(src))
        for m in _CALL_RE.finditer(src):
            text = literals.get(m.end())
            if text is None or not text.startswith("/api/"):
                continue
            checked += 1
            key = (_METHODS[m.group(1)], _norm_path(text.partition("?")[0]))
            if key not in routes:
                problems.append(f"{_rel(path)}: {key[0]} {text} is not a documented route")
    assert checked > 40
    assert not problems, "\n".join(problems)


#: The main v2 flow must be reachable from the UI (optional extras are not required).
MAIN_ENDPOINTS = [
    "/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/care/patients", "/api/care/links",
    "/api/care/links/{}", "/api/patients/{}/status", "/api/patients/{}/containers", "/api/patients/{}/containers/{}",
    "/api/patients/{}/containers/{}/refill", "/api/patients/{}/medications", "/api/patients/{}/medications/{}",
    "/api/patients/{}/schedules", "/api/patients/{}/schedules/{}", "/api/patients/{}/settings", "/api/patients/{}/drops",
    "/api/patients/{}/drops/{}/resolve", "/api/patients/{}/doses", "/api/patients/{}/doses/{}/skip",
    "/api/patients/{}/conversations", "/api/patients/{}/conversations/{}/messages", "/api/patients/{}/reports",
    "/api/agent/chat", "/api/agent/transcribe", "/api/reports/{}", "/api/reports/{}/pdf", "/api/reports/{}/send",
    "/api/notifications", "/api/notifications/read", "/api/device", "/api/device/home", "/api/device/stop",
    "/api/device/reconnect", "/api/demo/command", "/api/demo/clock", "/api/demo/jump-to-next-dose", "/api/demo/simulator",
    "/api/demo/reset", "/api/patients/{}/scans", "/api/patients/{}/scans/{}/confirm", "/api/patients/{}/scans/{}/reject",
    "/api/health",
]


def test_ui_uses_the_main_v2_endpoints():
    used = {_norm_path(text.partition("?")[0]) for _, text in _api_literals()}
    assert "/api/events" in {lit for p in JS_FILES for _, lit in js_literals(_read(p))}, "SSE endpoint"
    missing = [p for p in MAIN_ENDPOINTS if p not in used]
    assert not missing, f"endpoints never used by the UI: {missing}"


def test_page_routes_match_api_md():
    md = _read(API_MD)
    for route in PAGES:
        assert f"| `{route}` |" in md, f"{route} is not a documented page"


# =========================================================================== live events (SSE topics)


def _js_topics() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in JS_FILES:
        src = strip_js_comments(_read(path))
        for topic in re.findall(r"\.on\(\s*'([^']+)'", src):
            used.setdefault(topic, set()).add(_rel(path))
        for arr in re.findall(r"[A-Z_]*TOPICS\s*=\s*Object\.freeze\(\[(.*?)\]\)", src, re.S):
            for topic in re.findall(r"'([^']+)'", arr):
                used.setdefault(topic, set()).add(_rel(path))
    return used


def test_every_sse_topic_used_exists_in_bus_topic():
    topics = bus_topics()
    assert {"notification", "drop.updated", "patient.status", "agent.message", "report.updated", "device.state"} <= topics
    used = _js_topics()
    assert len(used) >= 10
    unknown = {t: sorted(files) for t, files in used.items() if t != "*" and t not in topics}
    assert not unknown, f"SSE topics not in core/bus.py Topic: {unknown}"


def test_portal_topics_cover_the_api_md_event_list():
    md = _read(API_MD)
    row = next(line for line in md.splitlines() if line.startswith("| `GET /api/events`"))
    listed = set(re.findall(r"`([a-z]+(?:\.[a-z]+)?)`", row)) & bus_topics()
    src = _read(STATIC / "js" / "events.js")
    known = set(re.findall(r"'([a-z.]+)'", src[src.index("PORTAL_TOPICS"):src.index("KNOWN_TOPICS =")]))
    assert listed and listed <= known, f"events.js does not subscribe to {sorted(listed - known)}"


# =========================================================================== HTML pages


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_page_basics(name: str):
    page = parse_page(name)
    html = page.find("html")
    assert html and html[0].get("lang") == "en"
    assert len(page.find("main")) == 1, "exactly one <main> landmark"
    assert page.title.strip(), "missing <title>"
    assert any(a.get("charset") for a in page.find("meta"))
    assert any(a.get("name") == "viewport" for a in page.find("meta"))
    assert page.find("header") and page.find("footer"), "header/footer landmarks"
    dupes = {i for i in page.ids if page.ids.count(i) > 1}
    assert not dupes, f"duplicate ids: {dupes}"
    first_link = page.find("a")[0]
    assert first_link.get("href") == "#main" and "skip-link" in (first_link.get("class") or ""), "skip link first"
    main = page.find("main")[0]
    assert main.get("id") == "main"
    assert page.find("h1"), "a page heading (h1)"


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_page_references_only_existing_local_assets(name: str):
    page = parse_page(name)
    problems = []
    for tag, attrs, line in page.tags:
        for attr in ("src", "href", "poster", "data", "action", "formaction"):
            value = attrs.get(attr)
            if value is None:
                continue
            if re.match(r"^(https?:)?//", value, re.I):
                problems.append(f"line {line}: external URL {value}")
            elif tag == "a" and attr == "href":
                if not (value.startswith("#") or value in PAGES):
                    problems.append(f"line {line}: link to unknown page {value}")
            elif value.startswith("/static/"):
                if not (STATIC / value[len("/static/"):]).is_file():
                    problems.append(f"line {line}: missing asset {value}")
            else:
                problems.append(f"line {line}: asset must be an absolute /static/ path: {value}")
    assert not problems, "\n".join(problems)
    scripts = page.find("script")
    assert scripts and all(s.get("src") for s in scripts), "inline <script> blocks are not allowed"
    assert not page.script_text
    entry = "/static/" + PAGE_ENTRY[name]
    assert any(s.get("src") == entry and s.get("type") == "module" for s in scripts)
    order = [(t, a) for t, a, _ in page.tags if t in ("script", "link")]
    init = next(i for i, (t, a) in enumerate(order) if a.get("src") == "/static/js/theme-init.js")
    css = next(i for i, (t, a) in enumerate(order) if a.get("href") == "/static/css/base.css")
    assert init < css, "theme-init.js must run before the stylesheets (no flash of the wrong theme)"
    assert order[init][1].get("type") is None, "theme-init.js is a classic script"


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_page_has_no_inline_handlers_or_styles(name: str):
    page = parse_page(name)
    for tag, attrs, line in page.tags:
        handlers = [a for a in attrs if a.startswith("on")]
        assert not handlers, f"line {line}: inline handler {handlers} on <{tag}>"
        assert "style" not in attrs, f"line {line}: inline style on <{tag}>"
        assert not str(attrs.get("href") or "").lower().startswith("javascript:")
    assert not page.find("style"), "no <style> blocks"


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_page_id_references_and_labels(name: str):
    page = parse_page(name)
    ids = set(page.ids)
    problems = []
    for tag, attrs, line in page.tags:
        for attr in ("for", "aria-controls", "aria-labelledby", "aria-describedby", "list"):
            for ref in (attrs.get(attr) or "").split():
                if ref not in ids:
                    problems.append(f"line {line}: <{tag} {attr}={ref!r}> points to a missing id")
        if tag == "button" and not attrs.get("type"):
            problems.append(f"line {line}: <button> without type")
        if tag == "img" and attrs.get("alt") is None:
            problems.append(f"line {line}: <img> without alt")
        if tag in ("input", "select", "textarea") and attrs.get("type") not in ("hidden", "submit", "button"):
            labelled = attrs.get("id") in page.labels_for or attrs.get("aria-label") or attrs.get("aria-labelledby")
            if not labelled:
                problems.append(f"line {line}: <{tag} id={attrs.get('id')!r}> has no label")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_static_ids_used_by_page_scripts_exist(name: str):
    """byId('…') calls in the page's module graph must match ids in that page."""
    ids = set(parse_page(name).ids)
    missing = []
    for module in module_closure((STATIC / PAGE_ENTRY[name]).resolve()):
        for ref in re.findall(r"\bbyId\(\s*'([^']+)'\s*\)", _read(module)):
            if ref not in ids:
                missing.append(f"{_rel(module)}: #{ref}")
    assert not missing, "\n".join(missing)


def test_no_external_urls_in_any_asset():
    problems = []
    for path in [*JS_FILES, *HTML_FILES, *CSS_FILES, *STATIC.rglob("*.svg")]:
        for url in re.findall(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s'\"`)<>]*", _read(path)):
            if url not in ALLOWED_URLS:
                problems.append(f"{_rel(path)}: {url}")
    assert not problems, "\n".join(problems)
    for path in CSS_FILES:
        css = _read(path)
        assert "@import" not in css, _rel(path)
        assert "@font-face" not in css, f"{_rel(path)}: no web fonts (offline, system fonts only)"
        for url in re.findall(r"url\(([^)]*)\)", css):
            assert url.strip("'\" ").startswith(("data:", "/static/")), f"{_rel(path)}: url({url})"


# =========================================================================== JS safety


_HTML_SINKS = re.compile(r"\.(innerHTML|outerHTML)\s*(\+?=)\s*([^;\n]*)")


@pytest.mark.parametrize("path", JS_FILES, ids=_rel)
def test_no_html_injection_sinks(path: Path):
    src = strip_js_comments(_read(path))
    for m in _HTML_SINKS.finditer(src):
        rhs = m.group(3).strip()
        literal = re.fullmatch(r"""(['"])[^'"\\]*\1|`[^`$\\]*`""", rhs)
        assert literal and m.group(2) == "=", f"{_rel(path)}: {m.group(0)!r} assigns a non-literal value"
    for sink in ("insertAdjacentHTML", "document.write", "createContextualFragment", "DOMParser", "eval(", "new Function", "srcdoc"):
        assert sink not in src, f"{_rel(path)} uses {sink}"


_HANDLER_PROPERTY_RE = re.compile(
    r"\.on(?:click|dblclick|change|input|submit|reset|key\w+|load\w*|error|message\w*|open|close|cancel|result|end|start|"
    r"audio\w+|ended|play\w*|pause|focus\w*|blur|mouse\w+|pointer\w+|touch\w+|resize|scroll|abort|timeout|progress|"
    r"beforeunload|unload|hashchange|popstate|visibilitychange|speech\w+|nomatch|sound\w+|statechange|dataavailable|stop|"
    r"processorerror|toggle)\s*=(?!=)"
)


@pytest.mark.parametrize("path", JS_FILES, ids=_rel)
def test_no_inline_handlers_created_from_js(path: Path):
    src = strip_js_comments(_read(path))
    assert not re.search(r"setAttribute\(\s*['\"]on", src), f"{_rel(path)} sets an on* attribute"
    assert not _HANDLER_PROPERTY_RE.search(src), f"{_rel(path)} assigns an on* handler property (use addEventListener)"


def test_fetch_uses_same_origin_credentials():
    api = strip_js_comments(_read(STATIC / "js" / "api.js"))
    assert "credentials: 'same-origin'" in api
    others = [p for p in JS_FILES if p.name != "api.js" and re.search(r"\bfetch\(", strip_js_comments(_read(p)))]
    assert not others, f"use api.js instead of fetch(): {[_rel(p) for p in others]}"


# =========================================================================== accessibility contracts


def _block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert m, f"{selector} block not found"
    return m.group(1)


def _hex_tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def theme_tokens() -> dict[str, dict[str, str]]:
    css = _read(STATIC / "css" / "base.css")
    dark = _hex_tokens(_block(css, ":root"))
    return {
        "dark": dark,
        "light": {**dark, **_hex_tokens(_block(css, ':root[data-theme="light"]'))},
        "yellow": {**dark, **_hex_tokens(_block(css, ':root[data-theme="yellow"]'))},
    }


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    ("fg", "bg"), ("fg", "surface"), ("fg", "surface-2"), ("fg", "surface-3"),
    ("fg-2", "bg"), ("fg-2", "surface"), ("fg-2", "surface-2"), ("fg-2", "surface-3"),
    ("fg-3", "bg"), ("fg-3", "surface"), ("fg-3", "surface-2"),
    ("link", "bg"), ("link", "surface"), ("link", "surface-2"), ("accent-fg", "accent"),
    ("ok-fg", "bg"), ("ok-fg", "surface"), ("ok-fg", "surface-2"),
    ("warn-fg", "bg"), ("warn-fg", "surface"), ("warn-fg", "surface-2"),
    ("danger-fg", "bg"), ("danger-fg", "surface"), ("danger-fg", "surface-2"),
    ("info-fg", "bg"), ("info-fg", "surface"), ("info-fg", "surface-2"),
    ("danger-bg-fg", "danger-bg"), ("caution-fg", "caution-bg"), ("invert-fg", "invert-bg"), ("good-bg-fg", "good-bg"),
]
NON_TEXT_PAIRS = [("border", "bg"), ("border", "surface"), ("focus", "bg"), ("focus", "surface"), ("focus", "surface-2")]


@pytest.mark.parametrize("theme", ["dark", "light", "yellow"])
def test_theme_contrast(theme: str):
    tokens = theme_tokens()[theme]
    low = [f"--{fg} on --{bg}: {contrast(tokens[fg], tokens[bg]):.2f}" for fg, bg in TEXT_PAIRS
           if contrast(tokens[fg], tokens[bg]) < 7.0]
    assert not low, "text below 7:1 — " + "; ".join(low)
    low = [f"--{fg} on --{bg}: {contrast(tokens[fg], tokens[bg]):.2f}" for fg, bg in NON_TEXT_PAIRS
           if contrast(tokens[fg], tokens[bg]) < 3.0]
    assert not low, "non-text below 3:1 — " + "; ".join(low)


def test_three_themes_and_text_sizes_are_wired():
    init = _read(STATIC / "js" / "theme-init.js")
    for theme in ("light", "dark", "yellow"):
        assert f"'{theme}'" in init
    for size in ("large", "xlarge"):
        assert f"'{size}'" in init
    theme_js = _read(STATIC / "js" / "theme.js")
    assert re.findall(r"\{ id: '([a-z]+)'", theme_js)[:3] == ["dark", "light", "yellow"], "dark (white on black) is the default"
    css = _read(STATIC / "css" / "base.css")
    assert ':root[data-text-size="large"]' in css and ':root[data-text-size="xlarge"]' in css


def _rem(value: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)rem", value)
    assert m, value
    return float(m.group(1))


def test_font_sizes_are_relative():
    problems = []
    for path in CSS_FILES:
        for value in re.findall(r"font-size:\s*([^;]+);", _read(path)):
            if re.search(r"\d(px|pt)\b", value):
                problems.append(f"{_rel(path)}: font-size {value}")
        if "text-transform: uppercase" in _read(path):
            problems.append(f"{_rel(path)}: text-transform: uppercase (no shouting text)")
    assert not problems, "\n".join(problems)


def test_patient_portal_size_minimums():
    css = _read(STATIC / "css" / "patient.css")
    assert _rem(_block(css, ".patient")) >= 1.5, "patient body text >= 24px"
    assert _rem(re.search(r"\.btn-drop\s*\{[^}]*min-height:\s*([^;]+);", css).group(1)) >= 5
    assert _rem(re.search(r"\.btn-talk\s*\{[^}]*min-height:\s*([^;]+);", css).group(1)) >= 5
    assert _rem(re.search(r"\.patient \.btn\s*\{[^}]*min-height:\s*([^;]+);", css).group(1)) >= 3
    assert _rem(re.search(r"\.view-title\s*\{[^}]*font-size:\s*([^;]+);", css).group(1).replace("em", "rem")) >= 1.5


def test_kiosk_size_minimums():
    css = _read(STATIC / "css" / "kiosk.css")
    for prop, minimum in (("--k-font-base", 2.0), ("--k-font-status", 3.5), ("--k-btn-min-h", 6.0)):
        values = re.findall(rf"{prop}:\s*([^;]+);", css)
        assert values, prop
        for value in values:  # every (media-query) definition keeps the minimum
            assert _rem(value) >= minimum, f"{prop}: {value}"
    assert re.search(r"\.k-btn\s*\{[^}]*min-height:\s*var\(--k-btn-min-h\)", css)
    assert re.search(r"\.status-word\s*\{[^}]*font-size:\s*var\(--k-font-status\)", css)


def test_motion_contrast_forced_colors_and_focus():
    css = _read(STATIC / "css" / "base.css")
    for feature in ("prefers-reduced-motion: reduce", "prefers-contrast: more", "forced-colors: active", ":focus-visible", ".skip-link:focus"):
        assert feature in css, feature


_ALLOWED_CAPS = {"OK", "AM", "PM", "ID", "PDF", "SMTP", "USB", "STOP", "ESP"}


def _shouting(text: str) -> list[str]:
    """Runs of two or more ALL-CAPS words (letters only), e.g. 'DEVICE READY'."""
    runs = []
    for m in re.finditer(r"\b(?:[A-Z]{2,}\b[ \t]+){1,}[A-Z]{2,}\b", text):
        words = [w for w in m.group(0).split() if w not in _ALLOWED_CAPS]
        if len(words) >= 2:
            runs.append(m.group(0))
    single = [w for w in re.findall(r"\b[A-Z]{5,}\b", text) if w not in _ALLOWED_CAPS]
    return runs + single


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_no_long_all_caps_text_in_pages(name: str):
    page = parse_page(name)
    found = [t for t in page.texts if _shouting(t)]
    assert not found, f"ALL-CAPS text (hard to read): {found}"


def test_no_long_all_caps_text_in_ui_strings():
    found = []
    for path in JS_FILES:
        for _, text in js_literals(strip_js_comments(_read(path))):
            if " " not in text or "_" in text:  # codes like 'DROP_SLOT 0' / single tokens are not prose
                continue
            if re.search(r"[a-z]", text) is None and len(text) < 12:
                continue  # short codes such as 'OK DROPPED'
            runs = [r for r in re.findall(r"\b(?:[A-Z]{2,}\b[ \t]+){1,}[A-Z]{2,}\b", text)
                    if len([w for w in r.split() if w not in _ALLOWED_CAPS]) >= 2]
            if runs:
                found.append(f"{_rel(path)}: {text!r}")
    assert not found, "\n".join(found)


def test_care_tabs_structure():
    page = parse_page("care.html")
    tablists = [a for _, a, _ in page.tags if a.get("role") == "tablist"]
    assert len(tablists) == 1 and tablists[0].get("aria-label")
    tabs = [a for _, a, _ in page.tags if a.get("role") == "tab"]
    assert [t["data-tab"] for t in tabs] == ["overview", "schedule", "containers", "settings", "medications",
                                             "conversations", "history", "reports", "notifications", "device"]
    panels = {a["id"]: a for _, a, _ in page.tags if a.get("role") == "tabpanel"}
    assert sum(t.get("aria-selected") == "true" for t in tabs) == 1
    for tab in tabs:
        panel = panels[tab["aria-controls"]]
        assert panel.get("aria-labelledby") == tab["id"]
        if tab.get("aria-selected") == "true":
            assert "hidden" not in panel
        else:
            assert tab.get("tabindex") == "-1" and "hidden" in panel
    for key in ("overview", "schedule", "containers", "settings", "medications", "conversations", "history", "reports", "notifications", "device"):
        assert re.search(rf"\b{key}: create\w*\(|\b{key}: \{{", _read(STATIC / "js" / "care.js")), f"care.js wires the {key} tab"


def test_medication_confirmation_is_explicit():
    html = _read(STATIC / "care.html")
    page = parse_page("care.html")
    for form_id, box_id in (("med-form", "med-confirm"), ("scan-form", "scan-confirm")):
        box = next(a for _, a, _ in page.tags if a.get("id") == box_id)
        assert box.get("type") == "checkbox" and box.get("name") == "confirmed" and "required" in box
        assert re.search(rf'<label for="{box_id}">I confirm this information is correct</label>', html)
        assert re.search(rf'<form id="{form_id}"[^>]*novalidate', html)
    assert "Unconfirmed: check every field against the label" in html
    file_input = next(a for _, a, _ in page.tags if a.get("id") == "scan-file")
    assert file_input.get("accept") == "image/jpeg,image/png,image/webp"
    meds = _read(STATIC / "js" / "care" / "medications.js")
    assert "data.append('image'" in meds and "/scans/${scan.scan_id}/confirm" in meds
    medform = _read(STATIC / "js" / "medform.js")
    assert "!confirmBox.checked" in medform and "body.confirmed = true" in medform


def test_patient_page_contract():
    page = parse_page("patient.html")
    attrs = {a.get("id"): a for _, a, _ in page.tags if a.get("id")}
    log = attrs["chat-log"]
    assert log.get("role") == "log" and log.get("aria-live") == "polite"
    assert attrs["voice-status"].get("role") == "status"
    assert attrs["talk-btn"].get("aria-pressed") == "false"
    assert attrs["toasts"].get("role") == "region" and attrs["toasts"].get("aria-label")
    views = [a.get("data-view") for a in page.find("a") if a.get("data-view")]
    assert views == ["home", "assistant", "schedule", "history", "reports", "share"]
    for v in views:
        assert f"view-{v}" in attrs and f"{v}-title" in attrs and attrs[f"{v}-title"].get("tabindex") == "-1"
    for ident in ("share-pid", "share-code", "next-pill", "cooldown-text", "containers", "last-drop", "device-line", "drop-result"):
        assert ident in attrs, ident
    html = _read(STATIC / "patient.html")
    assert "<kbd>Alt</kbd> + <kbd>M</kbd>" in html, "talk shortcut is shown on screen"
    js = _read(STATIC / "js" / "patient.js")
    assert "announce(view.message" in js, "drop results are announced from DropOutcome.message"
    assert "/drops`, { slot: v.slot }" in js


def test_login_page_contract():
    page = parse_page("login.html")
    roles = {a.get("value") for a in page.find("input") if a.get("name") == "role"}
    assert roles == {"patient", "doctor", "family"}
    attrs = {a.get("id"): a for _, a, _ in page.tags if a.get("id")}
    assert attrs["signin-password"].get("autocomplete") == "current-password"
    assert attrs["reg-password"].get("autocomplete") == "new-password" and attrs["reg-password"].get("minlength") == "8"
    assert attrs["signin-email"].get("type") == "email"
    for ident in ("patient-codes", "codes-pid", "codes-code", "link-step", "link-pid", "link-code"):
        assert ident in attrs, ident
    assert "Share these two codes with your doctor or family" in _read(STATIC / "login.html")


def test_every_enum_value_has_plain_words():
    words = _read(STATIC / "js" / "words.js")

    def table(name: str) -> set[str]:
        m = re.search(rf"export const {name} = Object\.freeze\(\{{(.*?)\n\}}\);", words, re.S)
        assert m, name
        return set(re.findall(r"^\s+([A-Za-z_]+):", m.group(1), re.M))

    assert enum_values(MODELS_PY, "DropStatus") <= table("DROP_STATUS")
    assert enum_values(MODELS_PY, "DenyReason") <= table("DENY_REASON")
    assert enum_values(MODELS_PY, "DropSource") <= table("DROP_SOURCE")
    assert enum_values(MODELS_PY, "NotificationKind") <= table("NOTIFICATION_KIND")
    assert enum_values(MODELS_PY, "DoseStatus") <= table("DOSE_STATUS")
    assert enum_values(PROTOCOL_PY, "DeviceState") <= table("DEVICE_STATE")
    assert enum_values(PROTOCOL_PY, "Err") | enum_values(PROTOCOL_PY, "HostCode") <= table("HARDWARE_REASON")
    assert {"SENT", "SAVED", "FAILED"} <= table("DELIVERY_STATUS")


# =========================================================================== JS logic under Node


def run_node(js_tree: Path, body: str) -> None:
    script = js_tree / f"t_{abs(hash(body))}.mjs"
    script.write_text("import assert from 'node:assert/strict';\n" + textwrap.dedent(body), encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60, cwd=js_tree)
    assert proc.returncode == 0, proc.stderr or proc.stdout


@needs_node
def test_js_format_helpers(js_tree: Path):
    run_node(js_tree, """
    import * as f from './js/format.js';
    assert.equal(f.formatClock('2026-10-04T08:00:00-07:00'), '8:00 AM');
    assert.equal(f.formatClock('2026-10-04T13:05:00-07:00'), '1:05 PM');
    assert.equal(f.formatClock(null), '–');
    // UTC timestamps are shown in the device offset, never the browser's zone
    assert.equal(f.formatClockDevice('2026-10-04T15:00:00.123456+00:00', -420), '8:00 AM');
    assert.equal(f.deviceOffsetFrom('2026-10-04T08:00:00-07:00'), -420);
    assert.equal(f.dateKey('2026-10-05T02:30:00+00:00', -420), '2026-10-04');
    assert.equal(f.relativeDayWord('2026-10-05T08:00:00-07:00', '2026-10-04T22:00:00-07:00'), 'tomorrow');
    assert.equal(f.relativeDayWord('2026-10-05T04:00:00+00:00', '2026-10-04T22:00:00-07:00'), 'today');
    assert.equal(f.formatWhen('2026-10-04T15:00:00+00:00', '2026-10-04T22:00:00-07:00'), 'today at 8:00 AM');
    assert.equal(f.formatWhen('2026-10-02T15:00:00+00:00', '2026-10-04T22:00:00-07:00'), 'on Fri 2 Oct at 8:00 AM');
    assert.equal(f.advanceLocalIso('2026-10-04T23:59:30-07:00', 45), '2026-10-05T00:00:15-07:00');
    assert.equal(f.addMinutesToLocal('2026-10-04T23:50:30-07:00', 15), '2026-10-05T00:05');
    assert.equal(f.normalizeTime('8:5'), '08:05');
    assert.equal(f.normalizeTime('25:00'), null);
    assert.equal(f.time24To12('20:00'), '8:00 PM');
    assert.equal(f.formatPercent(0.857), '86%');
    assert.equal(f.formatDuration(30), 'less than a minute');
    assert.equal(f.formatDuration(22 * 60 + 5), '23 minutes');
    assert.equal(f.formatDuration(60), '1 minute');
    assert.equal(f.formatDuration(3600), '1 hour');
    assert.equal(f.formatDuration(3900), '1 hour 5 minutes');
    assert.equal(f.formatCountdown(0), 'now');
    assert.equal(f.formatCountdown(1380), 'in 23 minutes');
    assert.equal(f.plural(1, 'pill'), '1 pill');
    assert.equal(f.plural(0, 'pill'), '0 pills');
    assert.equal(f.describeRepeat({ frequency: 'WEEKLY', days_of_week: ['FRI', 'MON'] }), 'Mon, Fri');
    assert.equal(f.describeRepeat({ frequency: 'DAILY', days_of_week: [] }), 'Every day');
    assert.equal(f.formatOffset(7500), '+2 h 05 min');
    assert.equal(f.spellOut('AB12'), 'A, B, 1, 2');
    assert.equal(f.formatBytes(48211), '47 KB');
    """)


@needs_node
def test_js_status_view_models(js_tree: Path):
    run_node(js_tree, """
    import * as s from './js/status.js';
    const c = (over = {}) => ({ slot: 1, container_number: 2, compartment_id: 2, medication_id: 3, medication_name: 'Vitamin C (demo candy)',
      strength: '1 piece', pill_count: 12, capacity: 30, low_stock_threshold: 3, low_stock: false, empty: false, ...over });
    const ok = s.containerView(c());
    assert.equal(ok.title, 'Container 2');
    assert.equal(ok.countText, '12 pills left');
    assert.equal(ok.badge, null);
    assert.equal(ok.canDrop, true);
    assert.equal(ok.dropLabel, 'Drop pill: Vitamin C (demo candy), container 2');
    const low = s.containerView(c({ pill_count: 2, low_stock: true }));
    assert.equal(low.badge.word, 'Low');
    assert.equal(low.countText, '2 pills left');
    const one = s.containerView(c({ pill_count: 1, low_stock: true }));
    assert.equal(one.countText, '1 pill left');
    const empty = s.containerView(c({ pill_count: 0, empty: true }));
    assert.equal(empty.badge.word, 'Empty');
    assert.equal(empty.canDrop, false);
    assert.match(empty.blocked, /empty/);
    const none = s.containerView(c({ medication_id: null, medication_name: null, pill_count: 0, empty: true }));
    assert.equal(none.hasMed, false);
    assert.equal(none.badge, null, 'an unused container is not "empty stock"');

    const status = { now_local: '2026-10-04T08:42:00-07:00', cooldown_minutes: 60, cooldown_remaining_s: 23 * 60,
      next_manual_allowed_at: '2026-10-04T16:05:00+00:00', containers: [], device: {} };
    const cd = s.cooldownView(status);
    assert.equal(cd.active, true);
    assert.equal(cd.text, 'You can drop another pill at 9:05 AM — in 23 minutes.');
    assert.equal(s.remainingCooldown(status, 23 * 60 + 1), 0);
    const after = s.cooldownView(status, s.remainingCooldown(status, 23 * 60 + 5));
    assert.equal(after.active, false);
    assert.equal(after.text, 'You can drop a pill now.');
    assert.match(s.cooldownView({ ...status, cooldown_minutes: 0, cooldown_remaining_s: 0 }).rule, /no waiting time/);

    const next = (over) => s.nextPillText({ now_local: '2026-10-04T08:42:00-07:00', next_scheduled: { medication_name: 'Vitamin C',
      scheduled_local: '2026-10-04T13:00:00-07:00', container_number: 2, ...over } });
    assert.equal(next({}), 'Vitamin C at 1:00 PM — container 2');
    assert.equal(next({ scheduled_local: '2026-10-05T08:00:00-07:00' }), 'Vitamin C tomorrow at 8:00 AM — container 2');
    assert.equal(next({ container_number: null, slot: null }), 'Vitamin C at 1:00 PM — no container assigned');
    assert.equal(s.nextPillText({ now_local: status.now_local, next_scheduled: null }), 'No pills are scheduled.');
    assert.equal(s.containerNumber({ slot: null, container_number: null }), null, 'an unknown slot is not container 1');
    assert.equal(s.containerNumber({ slot: 0 }), 1);

    const drop = { medication_name: 'Vitamin C', status: 'DROPPED', source: 'schedule', requested_local: '2026-10-04T08:00:00-07:00',
      completed_at: '2026-10-04T15:00:03+00:00', container_number: 1 };
    assert.equal(s.lastDropText({ now_local: status.now_local, last_drop: drop }), 'Last pill: Vitamin C, today at 8:00 AM. Dropped automatically at its time.');
    assert.match(s.lastDropText({ now_local: status.now_local, last_drop: { ...drop, status: 'UNCERTAIN' } }), /not certain/);
    assert.equal(s.lastDropText({ last_drop: null }), 'No pills have dropped yet.');

    assert.equal(s.deviceView({ connected: true, responsive: true, state: 'READY', homed: true }).word, 'Ready');
    assert.equal(s.deviceView({ connected: false }).word, 'Offline');
    assert.equal(s.deviceView({ connected: true, state: 'FAULT' }).ok, false);
    assert.equal(s.deviceView({ mode: 'none' }).word, 'Not set up');
    assert.equal(s.deviceView(null).ok, false);

    assert.deepEqual(s.alertsView({ alerts: [{ kind: 'EMPTY', message: 'Container 3 is empty.' }, 'Plain text'] }).map((a) => a.tone), ['bad', 'caution']);
    assert.equal(s.todayKey(status), '2026-10-04');

    const outcome = s.outcomeView({ status: 'DENIED', reason: 'COOLDOWN', message: 'Please wait until 9:05 AM.' });
    assert.equal(outcome.dropped, false);
    assert.equal(outcome.message, 'Please wait until 9:05 AM.', 'the server message is used as is');
    assert.equal(s.outcomeView({ status: 'UNCERTAIN', message: 'x' }).urgent, true);

    const ready = { ...status, cooldown_remaining_s: 0, device: { connected: true, responsive: true, state: 'READY' } };
    assert.equal(s.kioskBanner({ status: ready }).key, 'ready');
    assert.equal(s.kioskBanner({ status: { ...ready, cooldown_remaining_s: 600 }, remainingS: 600 }).word, 'Please wait');
    assert.equal(s.kioskBanner({ status: ready, dropping: true }).key, 'dropping');
    assert.equal(s.kioskBanner({ status: ready, online: false }).word, 'Offline');
    assert.equal(s.kioskBanner({ status: { ...ready, device: { connected: false } } }).key, 'device');
    assert.equal(s.kioskBanner({ status: { ...ready, next_scheduled: { status: 'DUE', medication_name: 'C', scheduled_local: '2026-10-04T08:30:00-07:00', container_number: 1 } } }).key, 'due');
    """)


@needs_node
def test_js_words_cover_codes(js_tree: Path):
    run_node(js_tree, """
    import * as w from './js/words.js';
    assert.equal(w.reasonText('COOLDOWN'), 'Too soon after the last pill');
    assert.equal(w.reasonText('ERR NO_PILL'.split(' ')[1]), 'No pill came out');
    assert.equal(w.reasonText('NO_PILL (sensor)'), 'No pill came out');
    assert.equal(w.reasonText(null), null);
    assert.equal(w.sourceText('agent', 'patient'), 'You asked the assistant');
    assert.equal(w.sourceText('schedule', 'caregiver'), 'Scheduled auto-drop');
    assert.equal(w.dropStatusInfo('UNCERTAIN', true).needsReview, true);
    assert.equal(w.notificationInfo('DROP_UNCERTAIN').urgent, true);
    assert.equal(w.doseStatusInfo('HARDWARE_ERROR', true).tone, 'bad');
    for (const table of [w.DROP_STATUS, w.NOTIFICATION_KIND, w.DOSE_STATUS, w.DEVICE_STATE, w.DELIVERY_STATUS]) {
      for (const [key, info] of Object.entries(table)) assert.ok(info.word && info.icon && info.tone, key);
    }
    """)


@needs_node
def test_js_api_client(js_tree: Path):
    run_node(js_tree, """
    import * as api from './js/api.js';
    const calls = [];
    const queue = [];
    const assigned = [];
    const loc = { pathname: '/care', search: '?x=1', hash: '#history', assign: (u) => assigned.push(u) };
    const respond = (status, body, { text = false } = {}) => queue.push({ status, body, text });
    api.configureApi({
      location: loc,
      fetch: async (path, init) => {
        calls.push({ path, ...init });
        const next = queue.shift();
        if (!next) throw new TypeError('Failed to fetch');
        const raw = next.body === undefined ? '' : (next.text ? next.body : JSON.stringify(next.body));
        return { ok: next.status < 300, status: next.status, statusText: 'X', text: async () => raw };
      },
    });

    respond(200, { ok: true });
    assert.deepEqual(await api.get('/api/auth/me'), { ok: true });
    assert.equal(calls[0].method, 'GET');
    assert.equal(calls[0].credentials, 'same-origin', 'the session cookie travels with every request');
    assert.equal(calls[0].headers.Accept, 'application/json');
    assert.equal(calls[0].headers['Content-Type'], undefined);

    respond(201, { medication_id: 7 });
    await api.post('/api/patients/1/medications', { name: 'X', confirmed: true });
    assert.equal(calls[1].headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(calls[1].body), { name: 'X', confirmed: true });

    respond(422, { detail: [{ loc: ['body', 'confirmed'], msg: 'Input should be True', type: 'literal_error' }] });
    await assert.rejects(api.post('/api/patients/1/medications', {}), (e) => e instanceof api.ApiError && e.status === 422
      && e.message === 'confirmed: Input should be True' && Array.isArray(e.detail));
    respond(403, { detail: 'Only doctor or family accounts can change this' });
    await assert.rejects(api.patch('/api/patients/1/settings', {}), (e) => e.status === 403 && e.message.startsWith('Only doctor'));
    respond(500, 'Internal Server Error', { text: true });
    await assert.rejects(api.get('/api/patients/1/status'), (e) => e.status === 500 && e.message === 'Internal Server Error');

    // 401 -> sign-in page with ?next= (once), unless the caller opts out
    respond(401, { detail: 'Not signed in' });
    await assert.rejects(api.get('/api/patients/1/status'), (e) => e.status === 401);
    assert.deepEqual(assigned, ['/login?next=%2Fcare%3Fx%3D1%23history']);
    respond(401, { detail: 'Not signed in' });
    await assert.rejects(api.get('/api/patients/1/drops?days=7'), (e) => e.status === 401);
    assert.equal(assigned.length, 1, 'only one redirect');
    api.configureApi({ location: loc });
    respond(401, { detail: 'Wrong email or password' });
    await assert.rejects(api.post('/api/auth/login', {}, { redirectOn401: false }), (e) => e.status === 401 && e.message === 'Wrong email or password');
    assert.equal(assigned.length, 1, 'no redirect when opted out');

    // raw bytes (offline speech) keep their content type
    respond(200, { text: 'can i have my pill', confidence: 0.9, engine: 'vosk' });
    const pcm = new ArrayBuffer(8);
    await api.postRaw('/api/agent/transcribe', pcm, { contentType: 'application/octet-stream' });
    assert.equal(calls.at(-1).body, pcm);
    assert.equal(calls.at(-1).headers['Content-Type'], 'application/octet-stream');

    // multipart upload: FormData passed through, no JSON content type
    const form = new FormData();
    form.append('image', new Blob(['x'], { type: 'image/png' }), 'a.png');
    respond(200, { scan_id: 1, status: 'PENDING_REVIEW' });
    await api.upload('/api/patients/1/scans', form);
    assert.equal(calls.at(-1).body, form);
    assert.equal(calls.at(-1).headers['Content-Type'], undefined);

    respond(204, undefined);
    assert.equal(await api.del('/api/patients/1/schedules/3'), null);
    await assert.rejects(api.get('/api/health'), (e) => e.status === 0 && e.network === true);

    // open-redirect protection for ?next=
    assert.equal(api.safeNext('/care#overview'), '/care#overview');
    for (const bad of ['https://evil.example', '//evil.example', '/\\\\evil', 'javascript:alert(1)', '/api/auth/me', '/login?next=/x', null]) {
      assert.equal(api.safeNext(bad), null, String(bad));
    }
    assert.equal(api.loginUrl('/patient'), '/login?next=%2Fpatient');
    assert.equal(api.loginUrl('//evil'), '/login');
    assert.equal(api.detailToMessage(null, 'fallback'), 'fallback');
    """)


@needs_node
def test_js_session_routing(js_tree: Path):
    run_node(js_tree, """
    import { destinationFor, homeFor, isCaregiver, roleName } from './js/session.js';
    assert.equal(homeFor('patient'), '/patient');
    assert.equal(homeFor('doctor'), '/care');
    assert.equal(homeFor('family'), '/care');
    assert.equal(destinationFor('patient', '/kiosk'), '/kiosk');
    assert.equal(destinationFor('patient', '/care'), '/patient', 'a patient never lands in the care portal');
    assert.equal(destinationFor('doctor', '/patient#home'), '/care');
    assert.equal(destinationFor('family', '/care#history'), '/care#history');
    assert.equal(destinationFor('doctor', null), '/care');
    assert.ok(isCaregiver({ role: 'family' }) && !isCaregiver({ role: 'patient' }));
    assert.equal(roleName('family'), 'Family member');
    """)


@needs_node
def test_js_event_stream(js_tree: Path):
    run_node(js_tree, """
    import { EventStream, RECONNECTED, KNOWN_TOPICS, PORTAL_TOPICS, parseTimestamp } from './js/events.js';
    class FakeES {
      static all = [];
      constructor(url) { this.url = url; this.listeners = {}; this.closed = false; FakeES.all.push(this); }
      addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
      close() { this.closed = true; }
      emit(type, data) { for (const fn of this.listeners[type] || []) fn({ data: typeof data === 'string' ? data : JSON.stringify(data) }); }
    }
    let now = 100000;
    const timers = [];
    const stream = new EventStream({
      eventSourceFactory: (u) => new FakeES(u),
      timers: { setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {} },
      now: () => now,
      random: () => 0,
    });
    const statuses = [];
    stream.onStatus((s) => statuses.push(s));
    const got = [];
    const all = [];
    let reconnected = 0;
    stream.on('drop.updated', (data, env, meta) => got.push({ data, seq: env.seq, replayed: meta.replayed }));
    stream.on('*', (_d, env) => all.push(env.topic));
    stream.on(RECONNECTED, () => { reconnected += 1; });
    stream.start();
    assert.equal(FakeES.all[0].url, '/api/events');
    for (const t of KNOWN_TOPICS) assert.ok(FakeES.all[0].listeners[t], `listens to ${t}`);
    assert.ok(PORTAL_TOPICS.includes('notification') && PORTAL_TOPICS.includes('patient.status'));

    const es = FakeES.all[0];
    es.emit('open');
    assert.equal(stream.status, 'open');
    const iso = (ms) => new Date(ms).toISOString().replace('Z', '123+00:00');
    es.emit('drop.updated', { seq: 1, topic: 'drop.updated', data: { drop_id: 1 }, ts: iso(now - 60000) });  // replayed history
    es.emit('drop.updated', { seq: 2, topic: 'drop.updated', data: { drop_id: 2 }, ts: iso(now) });          // live
    es.emit('drop.updated', { seq: 2, topic: 'drop.updated', data: { drop_id: 2 }, ts: iso(now) });          // duplicate
    es.emit('drop.updated', '{not json');
    assert.deepEqual(got.map((g) => [g.seq, g.replayed]), [[1, true], [2, false]]);
    assert.deepEqual(all, ['drop.updated', 'drop.updated']);

    es.emit('error');
    assert.equal(stream.status, 'reconnecting');
    assert.equal(stream.failures, 1);
    assert.equal(timers.at(-1).ms, 1000);
    timers.at(-1).fn();
    FakeES.all[1].emit('error');
    assert.equal(timers.at(-1).ms, 2000);
    timers.at(-1).fn();
    now += 10000;
    FakeES.all[2].emit('open');
    assert.equal(stream.failures, 0);
    assert.equal(reconnected, 1);
    // replay of an event seen before the drop is not delivered twice
    FakeES.all[2].emit('drop.updated', { seq: 2, topic: 'drop.updated', data: { drop_id: 2 }, ts: iso(100000) });
    assert.equal(got.filter((g) => g.seq === 2).length, 1);
    stream.close();
    FakeES.all[2].emit('error');
    assert.equal(FakeES.all.length, 3, 'no reconnect after close()');
    assert.deepEqual(statuses, ['idle', 'connecting', 'open', 'reconnecting', 'open', 'closed']);
    assert.ok(Number.isFinite(parseTimestamp('2026-10-04T15:00:00.123456+00:00')));
    """)


@needs_node
def test_js_pcm_audio(js_tree: Path):
    run_node(js_tree, """
    import { resample, floatToPcm16, concatFloat32, createSilenceDetector, displayTranscript, rms, TARGET_RATE, MAX_SECONDS } from './js/pcm.js';
    assert.equal(TARGET_RATE, 16000);
    assert.ok(MAX_SECONDS < 30, 'stays under the 30 s server limit');
    const second = new Float32Array(48000).map((_, i) => Math.sin((2 * Math.PI * 440 * i) / 48000) * 0.5);
    const down = resample(second, 48000);
    assert.equal(down.length, 16000);
    assert.ok(Math.abs(rms(down) - rms(second)) < 0.05, 'level preserved');
    const ramp = Float32Array.from([0, 0.3, 0.6, 0.9, 0.6, 0.3]);
    assert.deepEqual(Array.from(resample(ramp, 48000, 16000)).map((v) => Math.round(v * 10) / 10), [0.3, 0.6]);
    const up = resample(Float32Array.from([0, 1]), 8000, 16000);
    assert.deepEqual(Array.from(up), [0, 0.5, 1, 1]);
    assert.equal(resample(second, 16000).length, 48000);
    assert.throws(() => resample(second, 0));

    const pcm = floatToPcm16(Float32Array.from([0, 1, -1, 2, -2, 0.5, NaN]));
    const view = new DataView(pcm);
    assert.equal(pcm.byteLength, 14);
    assert.deepEqual([0, 1, 2, 3, 4, 5, 6].map((i) => view.getInt16(i * 2, true)), [0, 32767, -32768, 32767, -32768, 16384, 0]);
    assert.equal(new Uint8Array(pcm)[2], 0xff, 'little-endian');
    assert.equal(concatFloat32([Float32Array.from([1]), Float32Array.from([2, 3])]).length, 3);

    const det = createSilenceDetector({ threshold: 0.1, silenceMs: 200, maxWaitMs: 500 });
    const loud = new Float32Array(1600).fill(0.5);
    const quiet = new Float32Array(1600);
    assert.equal(det.update(quiet, 16000), 'waiting');
    assert.equal(det.update(loud, 16000), 'speaking');
    assert.equal(det.update(quiet, 16000), 'speaking');
    assert.equal(det.update(quiet, 16000), 'done');
    const nothing = createSilenceDetector({ threshold: 0.1, maxWaitMs: 150 });
    assert.equal(nothing.update(quiet, 16000), 'waiting');
    assert.equal(nothing.update(quiet, 16000), 'nothing', 'no speech within maxWaitMs');
    assert.equal(displayTranscript('can i have [unk] pill'), 'can i have … pill');
    """)


@needs_node
def test_js_voice_input_falls_back_to_offline_capture(js_tree: Path):
    """A SpeechRecognition that never starts (no speech service) -> AudioWorklet capture ->
    16 kHz PCM16 upload to /api/agent/transcribe -> result; the microphone is released."""
    run_node(js_tree, """
    import { VoiceInput, ReplySpeaker } from './js/voice.js';
    import { configureApi } from './js/api.js';
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const tracks = [];
    Object.defineProperty(globalThis, 'navigator', { configurable: true, writable: true, value: {
      permissions: { query: async () => ({ state: 'prompt' }) },
      mediaDevices: { getUserMedia: async () => { const t = { stop() { this.stopped = true; } }; tracks.push(t); return { getTracks: () => [t] }; } },
    } });
    class FakeSR extends EventTarget {
      start() { FakeSR.started += 1; }
      stop() { this.dispatchEvent(new Event('end')); }
      abort() { this.aborted = true; this.dispatchEvent(new Event('end')); }
    }
    FakeSR.started = 0;
    globalThis.webkitSpeechRecognition = FakeSR;
    let node = null;
    class FakePort extends EventTarget { start() {} }
    globalThis.AudioWorkletNode = class { constructor(ctx, name) { this.name = name; this.port = new FakePort(); node = this; } connect() {} disconnect() {} };
    const contexts = [];
    globalThis.AudioContext = class {
      constructor() { this.sampleRate = 48000; this.state = 'running'; this.destination = {}; this.audioWorklet = { addModule: async (url) => { this.module = url; } }; contexts.push(this); }
      createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
      createGain() { return { gain: { value: 1 }, connect() {} }; }
      async close() { this.closed = true; }
    };
    const posted = [];
    configureApi({ fetch: async (path, init) => { posted.push({ path, init });
      return { ok: true, status: 200, text: async () => JSON.stringify({ text: 'what do i take now', confidence: 0.8, engine: 'vosk' }) }; } });
    const states = [];
    const results = [];
    const voice = new VoiceInput({ onState: (s) => states.push(s), onResult: (t, meta) => results.push([t, meta.mode]), startTimeoutMs: 20 });
    assert.equal(voice.engine, 'browser');
    voice.start();
    for (let i = 0; i < 50 && !node; i += 1) await sleep(10);
    assert.equal(FakeSR.started, 1);
    assert.ok(node, 'fell back to AudioWorklet capture');
    assert.equal(node.name, 'tactidose-pcm-capture');
    assert.equal(contexts[0].module, '/static/js/pcm-worklet.js');
    assert.equal(voice.engine, 'offline', 'a recognizer that never starts is not used again');
    const chunk = new Float32Array(2048).fill(0.3);
    for (let i = 0; i < 12; i += 1) node.port.dispatchEvent(new MessageEvent('message', { data: chunk }));
    voice.stop();
    for (let i = 0; i < 50 && !results.length; i += 1) await sleep(10);
    assert.equal(posted.length, 1);
    assert.equal(posted[0].path, '/api/agent/transcribe');
    assert.equal(posted[0].init.headers['Content-Type'], 'application/octet-stream');
    assert.equal(posted[0].init.body.byteLength, Math.floor((12 * 2048) / 3) * 2, '48 kHz float -> 16 kHz PCM16');
    assert.deepEqual(results, [['what do i take now', 'offline']]);
    assert.ok(tracks.length >= 2 && tracks.every((t) => t.stopped), 'every microphone stream is released');
    assert.ok(contexts.every((c) => c.closed));
    assert.deepEqual([...new Set(states)], ['starting', 'listening', 'processing', 'idle']);

    // cancel: nothing is sent
    voice.start();
    for (let i = 0; i < 50 && voice.state !== 'listening'; i += 1) await sleep(10);
    node.port.dispatchEvent(new MessageEvent('message', { data: chunk }));
    voice.cancel();
    await sleep(20);
    assert.equal(posted.length, 1, 'cancel sends nothing');
    assert.equal(voice.state, 'idle');

    // spoken replies fall back to the browser voice when there is no server audio
    const spoken = [];
    globalThis.SpeechSynthesisUtterance = class extends EventTarget { constructor(t) { super(); this.text = t; } };
    globalThis.speechSynthesis = { cancel() {}, speak(u) { spoken.push(u.text); } };
    const speaker = new ReplySpeaker();
    assert.equal(speaker.speak('Your pill dropped.'), true);
    assert.deepEqual(spoken, ['Your pill dropped.']);
    assert.equal(speaker.speaking, true);
    speaker.stop();
    assert.equal(speaker.speaking, false);
    """)


@needs_node
def test_js_forms_validation(js_tree: Path):
    run_node(js_tree, """
    import { readMedicationForm, linesOf } from './js/medform.js';
    import { scheduleBody, groupSchedules } from './js/care/schedule.js';
    import { settingsBody } from './js/care/settings.js';
    import { refillBody, containerSettingsBody } from './js/care/containers.js';
    import { linkBody, normalizeLinkCode, linkErrorText } from './js/links.js';
    import { registerBody, authErrorText } from './js/authforms.js';
    import { ApiError } from './js/api.js';
    const fakeForm = (values) => ({ elements: { namedItem: (n) => (n in values ? (n === 'confirmed' ? { checked: values[n] } : { value: values[n] }) : null) } });
    const fields = { name: ' Vitamin C ', strength: '', instructions_text: 'Take one.', warnings: 'a\\n\\n b ', confirmed: true };
    assert.equal(readMedicationForm(fakeForm({ ...fields, name: '  ' })).field, 'name');
    const unconfirmed = readMedicationForm(fakeForm({ ...fields, confirmed: false }));
    assert.equal(unconfirmed.ok, false);
    assert.equal(unconfirmed.field, 'confirmed', 'nothing is sent without the confirmation tick');
    assert.deepEqual(readMedicationForm(fakeForm(fields)).body, { name: 'Vitamin C', instructions_text: 'Take one.', warnings: ['a', 'b'], confirmed: true });
    assert.equal(readMedicationForm(fakeForm(fields), { keepEmpty: true }).body.strength, '');
    assert.deepEqual(linesOf(' x \\r\\n\\ny'), ['x', 'y']);

    assert.deepEqual(scheduleBody({ medicationId: '3', time: '8:00', frequency: 'DAILY', days: [], editing: false }).body,
      { time_of_day: '08:00', frequency: 'DAILY', medication_id: 3 });
    assert.equal(scheduleBody({ medicationId: '', time: '08:00', frequency: 'DAILY', days: [], editing: false }).field, 'medication_id');
    assert.equal(scheduleBody({ medicationId: '3', time: '', frequency: 'DAILY', days: [], editing: false }).field, 'time_of_day');
    assert.equal(scheduleBody({ medicationId: '3', time: '08:00', frequency: 'WEEKLY', days: [], editing: false }).field, 'days');
    assert.deepEqual(scheduleBody({ medicationId: '3', time: '08:00', frequency: 'WEEKLY', days: ['FRI', 'MON'], editing: false }).body.days_of_week, ['MON', 'FRI']);
    assert.deepEqual(scheduleBody({ medicationId: '', time: '20:15', frequency: 'DAILY', days: [], active: false, editing: true }).body,
      { time_of_day: '20:15', frequency: 'DAILY', days_of_week: ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'], active: false });
    const groups = groupSchedules([
      { schedule_id: 1, medication_id: 2, medication_name: 'B', time_of_day: '20:00', active: true },
      { schedule_id: 2, medication_id: 2, medication_name: 'B', time_of_day: '08:00', active: true },
      { schedule_id: 3, medication_id: 1, medication_name: 'A', time_of_day: '09:00', active: false },
    ]);
    assert.deepEqual(groups.map((g) => [g.name, g.items.map((s) => s.time_of_day)]), [['B', ['08:00', '20:00']]]);
    assert.equal(groupSchedules([{ medication_id: 1, medication_name: 'A', time_of_day: '09:00', active: false }], { showInactive: true }).length, 1);

    assert.deepEqual(settingsBody('45', true).body, { manual_cooldown_minutes: 45, auto_drop_enabled: true });
    assert.deepEqual(settingsBody('0', false).body, { manual_cooldown_minutes: 0, auto_drop_enabled: false });
    assert.equal(settingsBody('1441', true).ok, false);
    assert.equal(settingsBody('-5', true).ok, false);
    assert.equal(settingsBody('ten', true).ok, false);

    assert.deepEqual(refillBody('set', '20', 30).body, { set: 20 });
    assert.deepEqual(refillBody('add', '5', 30, 20).body, { add: 5 });
    assert.equal(refillBody('add', '15', 30, 20).ok, false, 'cannot overfill');
    assert.equal(refillBody('set', '31', 30).ok, false);
    assert.equal(refillBody('add', '0', 30, 0).ok, false);
    assert.equal(refillBody('set', '2.5', 30).ok, false);
    assert.deepEqual(containerSettingsBody('30', '3').body, { capacity: 30, low_stock_threshold: 3 });
    assert.equal(containerSettingsBody('3', '3').ok, false);
    assert.equal(containerSettingsBody('0', '0').ok, false);

    assert.equal(normalizeLinkCode('abcd-23 45'), 'ABCD2345');
    assert.deepEqual(linkBody(' 12 ', 'alex 2026').body, { patient_id: 12, link_code: 'ALEX2026' });
    assert.equal(linkBody('', 'X').field, 'patient_id');
    assert.equal(linkBody('12a', 'ALEX2026').field, 'patient_id');
    assert.equal(linkBody('12', '').field, 'link_code');
    assert.equal(linkBody('12', 'AB!?').field, 'link_code');
    assert.match(linkErrorText({ status: 404 }), /do not match/);

    assert.equal(registerBody({ name: 'A', email: 'a@b.co', password: 'short', role: 'patient' }).field, 'reg-password');
    assert.equal(registerBody({ name: '', email: 'a@b.co', password: 'longenough', role: 'patient' }).field, 'reg-name');
    assert.equal(registerBody({ name: 'A', email: 'nope', password: 'longenough', role: 'patient' }).field, 'reg-email');
    assert.equal(registerBody({ name: 'A', email: 'a@b.co', password: 'longenough', role: 'admin' }).ok, false);
    assert.deepEqual(registerBody({ name: ' Ann ', email: ' a@b.co ', password: 'longenough', role: 'family', phone: ' 555 ' }).body,
      { email: 'a@b.co', password: 'longenough', display_name: 'Ann', role: 'family', phone: '555' });
    assert.match(authErrorText(new ApiError('x', { status: 401 }), 'signin'), /not right/);
    assert.match(authErrorText(new ApiError('x', { status: 409 }), 'register'), /already exists/);
    """)


@needs_node
def test_js_notifications_chat_history_reports(js_tree: Path):
    run_node(js_tree, """
    import { mergeNotifications, unreadCount, notificationSpeech } from './js/notifications.js';
    import { toolSummary, messageView, visibleTo } from './js/chat.js';
    import { dropSummary, groupByDay } from './js/history.js';
    import { doseSummary, canSkip } from './js/doses.js';
    import { deliveryText, statsHighlights } from './js/reports.js';
    import { summarizeDay } from './js/care/overview.js';
    import { conversationLine } from './js/care/conversations.js';
    import { scanErrorText } from './js/care/medications.js';
    import { dayTitle } from './js/patient/schedule.js';

    const n = (id, at, extra = {}) => ({ notification_id: id, created_at: at, kind: 'PILL_DROPPED', title: 'Pill dropped', body: 'Vitamin C dropped.', read_at: null, ...extra });
    const merged = mergeNotifications([n(1, '2026-10-04T15:00:00+00:00')], [n(2, '2026-10-04T16:00:00.123456+00:00'), n(1, '2026-10-04T15:00:00+00:00', { read_at: 'x' })]);
    assert.deepEqual(merged.map((x) => x.notification_id), [2, 1]);
    assert.equal(merged[1].read_at, 'x');
    assert.equal(unreadCount(merged), 1);
    assert.equal(notificationSpeech(n(3, null)), 'Pill dropped. Vitamin C dropped.');
    assert.equal(mergeNotifications([], Array.from({ length: 150 }, (_, i) => n(i, null))).length, 100);

    const tool = { role: 'tool', tool_name: 'request_pill', tool_args: { container_number: 1 }, tool_result: { status: 'DENIED', message: 'Please wait until 9:05 AM.' } };
    assert.equal(toolSummary(tool).text, 'Asked for a pill: not dropped. Please wait until 9:05 AM.');
    assert.equal(toolSummary(tool, 'caregiver').text, 'Requested a pill: not dropped. Please wait until 9:05 AM.');
    assert.equal(toolSummary({ role: 'tool', tool_name: 'get_patient_status', tool_result: {} }).text, 'Checked your pill status.');
    assert.match(toolSummary({ role: 'tool', tool_name: 'confirm_pill_taken', tool_result: { ok: false, error: 'nothing to confirm' } }).text, /nothing to confirm/);
    assert.equal(visibleTo({ role: 'tool', tool_name: 'get_patient_status' }, 'patient'), false);
    assert.equal(visibleTo({ role: 'tool', tool_name: 'get_patient_status' }, 'caregiver'), true);
    assert.equal(visibleTo({ role: 'tool', tool_name: 'request_pill' }, 'patient'), true);
    assert.equal(messageView({ role: 'user', content: 'pill [unk] please', input_mode: 'voice' }).text, 'pill … please');
    assert.equal(messageView({ role: 'user', content: 'hi', input_mode: 'voice' }, { audience: 'caregiver' }).who, 'Patient (spoken)');
    assert.equal(messageView({ role: 'assistant', content: 'ok', model: 'rules' }, { audience: 'caregiver' }).who, 'Assistant (rules)');

    const drop = { drop_id: 4, status: 'DENIED', reason: 'COOLDOWN', source: 'manual', medication_name: 'Vitamin C', container_number: 1,
      requested_local: '2026-10-04T08:10:00-07:00', requested_at: '2026-10-04T15:10:00+00:00' };
    const v = dropSummary(drop, { offsetMin: -420 });
    assert.equal(v.title, 'Vitamin C — container 1');
    assert.equal(v.time, '8:10 AM');
    assert.equal(v.reason, 'Too soon after the last pill');
    assert.equal(v.source, 'You pressed Drop pill');
    assert.equal(dropSummary({ ...drop, status: 'UNCERTAIN', needs_review: true }).needsReview, true);
    assert.equal(dropSummary({ ...drop, status: 'DROPPED', reason: null, pill_count_after: 11 }).pills, '11 pills left in the container afterwards');
    const groups = groupByDay([drop, { ...drop, drop_id: 3, requested_local: '2026-10-03T21:00:00-07:00' }], -420, '2026-10-04T09:00:00-07:00');
    assert.deepEqual(groups.map((g) => g.label), ['Today — Sunday 4 October 2026', 'Yesterday — Saturday 3 October 2026']);

    const dose = { event_id: 9, status: 'DISPENSED', medication_name: 'Calcium', container_number: 2, scheduled_local: '2026-10-04T13:00:00-07:00',
      dispensed_at: '2026-10-04T20:00:05+00:00', dispense_source: 'schedule' };
    assert.equal(doseSummary(dose, { offsetMin: -420 }).detail, 'Dropped at 1:00 PM — dropped automatically at its time');
    assert.equal(canSkip({ status: 'SCHEDULED' }) && canSkip({ status: 'DUE' }) && !canSkip(dose) && !canSkip({ status: 'MISSED' }), true);
    assert.equal(doseSummary({ ...dose, slot: null, container_number: null }).container, 'No container assigned', 'null slot is not container 1');
    assert.equal(dropSummary({ ...drop, slot: null, container_number: null }).title, 'Vitamin C');
    assert.deepEqual(summarizeDay([{ status: 'DISPENSED' }, { status: 'TAKEN' }, { status: 'MISSED' }, { status: 'CANCELLED' }, { status: 'SCHEDULED' }, { status: 'HARDWARE_ERROR' }]),
      { dropped: 2, missed: 1, skipped: 1, open: 1, problems: 1 });

    assert.match(deliveryText({ status: 'SAVED', to_email: 'dr.lee@demo.tactidose' }), /^Saved as an email file to dr.lee@demo.tactidose\\. Email sending is not set up/);
    assert.equal(deliveryText({ status: 'SENT', to_email: 'a@b.co' }), 'Sent to a@b.co.');
    assert.equal(deliveryText({ status: 'FAILED', to_email: 'a@b.co', error: 'connection refused' }), 'Not sent to a@b.co: connection refused');
    assert.deepEqual(statsHighlights({ adherence_rate: 0.857, scheduled_doses: 14, missed: 1 }), ['Adherence 86%', '14 scheduled doses', '1 missed dose']);
    assert.deepEqual(statsHighlights(null), []);
    assert.equal(conversationLine({ last_message_at: '2026-10-04T16:02:00+00:00', message_count: 6, channel: 'voice' }, '2026-10-04T10:00:00-07:00'),
      'today at 9:02 AM · 6 messages · spoken');
    assert.match(scanErrorText({ status: 503 }), /not set up/);
    assert.equal(dayTitle('2026-10-05', '2026-10-04'), 'Tomorrow — Monday 5 October 2026');
    assert.equal(dayTitle('2026-10-09', '2026-10-04'), 'Friday 9 October 2026');
    """)


@needs_node
def test_js_demo_and_carousel_helpers(js_tree: Path):
    run_node(js_tree, """
    import { findScheduledDrop, maxId, waitFor, buildFlows } from './js/demo/flows.js';
    import { faultLabel, healthValue, FAULT_LABELS } from './js/demo/labels.js';
    import { polar, slotAngle, shortestDelta, sectorPath, pillsFromPhysical } from './js/carousel.js';
    import { isHeartbeat } from './js/linelog.js';
    import { describeCommand, snapshotRows } from './js/hwview.js';
    import { createPrefs, DEFAULT_PREFS } from './js/prefs.js';

    assert.equal(maxId([{ drop_id: 3 }, { drop_id: 9 }], 'drop_id'), 9);
    assert.equal(maxId(null, 'drop_id'), 0);
    const drops = [
      { drop_id: 5, source: 'schedule', status: 'DROPPED', completed_at: 'x' },
      { drop_id: 7, source: 'manual', status: 'DROPPED', completed_at: 'x' },
      { drop_id: 8, source: 'schedule', status: 'UNCERTAIN', completed_at: null },
      { drop_id: 9, source: 'schedule', status: 'DENIED', reason: 'ALREADY_SATISFIED', completed_at: null },
    ];
    assert.equal(findScheduledDrop(drops, 5).drop_id, 9, 'in-flight rows are not an outcome yet');
    assert.equal(findScheduledDrop(drops, 9), null);
    let n = 0;
    assert.equal(await waitFor(async () => (++n >= 3 ? 'yes' : null), { timeoutMs: 1000, intervalMs: 1 }), 'yes');
    assert.equal(await waitFor(async () => null, { timeoutMs: 5, intervalMs: 1 }), null);
    const flows = buildFlows({ session: () => null, recent: () => [], renderClock() {}, skipCooldown() {}, notify() {} });
    assert.deepEqual(flows.map((f) => f.id), ['A', 'B', 'C', 'D']);
    for (const f of flows) assert.ok(f.steps.length >= 2 && f.steps.every((s) => s.label && typeof s.run === 'function'));

    assert.deepEqual(faultLabel('motor_jam'), FAULT_LABELS.motor_jam);
    assert.deepEqual(faultLabel('drop_sensor_dead'), ['Drop sensor dead', '']);
    assert.equal(healthValue(false), 'off');
    assert.equal(healthValue({ mode: 'sim' }), 'sim');
    assert.equal(healthValue({ configured: false }), 'not set up');

    assert.deepEqual(polar(100, 90), [100, 0]);
    assert.equal(slotAngle(1, 3), 120);
    assert.equal(shortestDelta(350, 10), 20);
    assert.ok(sectorPath(0, 3).startsWith('M-96.99 -56A112 112 0 0 1 96.99 -56'), sectorPath(0, 3));
    assert.deepEqual(pillsFromPhysical({ pills: [20, 19, 0] }, 3), [20, 19, 0]);
    assert.deepEqual(pillsFromPhysical({ pill_counts: { 0: 5, 2: 1 } }, 3), [5, null, 1]);
    assert.deepEqual(pillsFromPhysical({ containers: [{ slot: 1, pills: 7 }] }, 3), [null, 7, null]);
    assert.deepEqual(pillsFromPhysical({}, 2), [null, null]);

    assert.ok(isHeartbeat('PING') && isHeartbeat('OK STATUS state=READY') && !isHeartbeat('DROP_SLOT 1'));
    assert.equal(describeCommand({ ok: false, result: { command: 'DROP_SLOT 1', ok: false, code: 'TIMEOUT', definitive: false } }),
      'DROP_SLOT 1 → failed (TIMEOUT, uncertain)');
    const rows = Object.fromEntries(snapshotRows({ connected: true, state: 'READY', proto: '1.1', drop_sensor: true, slot: 0 }));
    assert.equal(rows['Protocol'], 'v1.1 (pill drop supported)');
    assert.equal(rows['Drop sensor'], 'Present');

    const store = new Map();
    const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
    const prefs = createPrefs(storage);
    assert.equal(prefs.get('speakReplies'), DEFAULT_PREFS.speakReplies);
    const seen = [];
    prefs.onChange((k, v) => seen.push([k, v]));
    prefs.set('confirmDrops', false);
    assert.equal(createPrefs(storage).get('confirmDrops'), false, 'persisted');
    assert.deepEqual(seen, [['confirmDrops', false]]);
    assert.throws(() => prefs.set('nope', true));
    store.set('tactidose.prefs', '{broken');
    assert.equal(createPrefs(storage).get('confirmDrops'), true, 'bad storage falls back to defaults');
    """)
