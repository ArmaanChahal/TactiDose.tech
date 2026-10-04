"""Static checks for the web UI in ``tactidose/ui/static`` (no browser needed).

* Every JS file passes ``node --check`` as an ES module, and relative imports resolve.
* Every ``/api/...`` path (and query parameter) used in JS exists in ``docs/API.md``;
  helper calls (get/post/put/patch/del/upload) use the documented HTTP method.
* Every HTML page has ``lang``, one ``<main>``, a ``<title>``, references only existing
  local assets, has no external URLs, no inline handlers or inline scripts, and valid
  id references (labels, ARIA).
* No ``innerHTML``-style injection of non-literal values, no eval.
* Accessibility contracts: kiosk sizes (>= 32px text, >= 56px status, >= 96px buttons),
  theme token contrast (>= 7:1 for text in both themes), ARIA tabs structure,
  explicit confirmation checkboxes, kiosk intents and demo fault names match the API.
* Pure JS logic (formatting, kiosk banner derivation, API client, SSE client, chart and
  carousel geometry, form validation) is unit-tested under Node with fakes.

Node-based tests are skipped (not failed) when ``node`` is not installed.
"""

from __future__ import annotations

import json
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
NODE = shutil.which("node")

PAGES = {"/": "index.html", "/caregiver": "caregiver.html", "/demo": "demo.html"}
PAGE_ENTRY = {"index.html": "js/kiosk.js", "caregiver.html": "js/caregiver.js", "demo.html": "js/demo.js"}
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


def js_literals(src: str) -> list[tuple[int, str]]:
    """(offset, text) of every string/template literal; comments are skipped."""
    found: list[tuple[int, str]] = []
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
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


# --------------------------------------------------------------------------- API.md parsing

_ROUTE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/api/[^`\s]*)`")


def _norm_path(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/")


def documented_routes() -> dict[tuple[str, str], set[str]]:
    """{(METHOD, normalised path): {documented query parameter names}}."""
    routes: dict[tuple[str, str], set[str]] = {}
    for method, full in _ROUTE_RE.findall(_read(API_MD)):
        path, _, query = full.partition("?")
        params = {kv.split("=", 1)[0] for kv in query.split("&") if kv}
        routes.setdefault((method, _norm_path(path)), set()).update(params)
    return routes


def documented_intents() -> set[str]:
    row = next(line for line in _read(API_MD).splitlines() if line.startswith("| `POST /api/intents`"))
    return set(re.findall(r'"([A-Z_]{3,})"', row))


def documented_faults() -> set[str]:
    m = re.search(r"faults:\{([^}]*)\}", _read(API_MD))
    assert m, "fault list not found in API.md"
    return {f.strip() for f in m.group(1).split(",")}


def _api_literals() -> list[tuple[Path, str]]:
    out = []
    for path in JS_FILES:
        for _, text in js_literals(_read(path)):
            if text.startswith("/api/"):
                out.append((path, text))
    return out


# --------------------------------------------------------------------------- HTML parsing


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None], int]] = []
        self.ids: list[str] = []
        self.title = ""
        self._in_title = False
        self.script_text: list[str] = []
        self._in_script = False
        self.labels_for: set[str] = set()

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

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_script and data.strip():
            self.script_text.append(data)

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


def imports_of(path: Path) -> list[str]:
    src = strip_js_comments(_read(path))
    return [m.group(2) for m in _IMPORT_RE.finditer(src)] + [m.group(2) for m in _DYNAMIC_IMPORT_RE.finditer(src)]


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
    for rel in ("index.html", "caregiver.html", "demo.html", "css/base.css", "css/kiosk.css",
                "css/caregiver.css", "css/demo.css", "js/api.js", "js/events.js", "js/dom.js",
                "js/kiosk.js", "js/caregiver.js", "js/demo.js", "js/chart.js", "js/carousel.js"):
        assert (STATIC / rel).is_file(), rel


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


# =========================================================================== API contract


def test_every_api_path_in_js_is_documented():
    routes = documented_routes()
    by_path: dict[str, set[str]] = {}
    for (_method, path), params in routes.items():
        by_path.setdefault(path, set()).update(params)
    used = _api_literals()
    assert len(used) > 30, "expected the UI to use most of the API"
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


_CALL_RE = re.compile(r"\b(get|post|put|patch|del|upload)\(\s*(?=['\"`])")
_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "del": "DELETE", "upload": "POST"}


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
    assert checked > 30
    assert not problems, "\n".join(problems)


def test_ui_covers_the_documented_api():
    """Every documented endpoint is reachable from at least one page."""
    used = {_norm_path(text.partition("?")[0]) for _, text in _api_literals()}
    documented = {path for _, path in documented_routes()}
    missing = sorted(documented - used)
    assert not missing, f"endpoints never used by the UI: {missing}"


def test_kiosk_intents_match_api():
    allowed = documented_intents()
    assert {"CHECK_DUE", "DISPENSE", "CONFIRM_TAKEN", "REPEAT", "CANCEL", "HELP", "PRIMARY_ACTION"} <= allowed
    page = parse_page("index.html")
    used = {a["data-intent"] for a in page.find("button") if a.get("data-intent")}
    assert used == {"CHECK_DUE", "DISPENSE", "CONFIRM_TAKEN", "REPEAT", "HELP", "CANCEL"}
    src = _read(STATIC / "js" / "kiosk-state.js")
    m = re.search(r"KIOSK_INTENTS = Object\.freeze\(\[([^\]]*)\]", src)
    assert m
    assert set(re.findall(r"'([A-Z_]+)'", m.group(1))) <= allowed
    assert "postIntent(intent, 'ui')" in _read(STATIC / "js" / "kiosk.js")


def test_demo_faults_and_phrases_match_spec():
    src = _read(STATIC / "js" / "demo.js")
    m = re.search(r"FAULTS = Object\.freeze\(\[(.*?)\]\);", src, re.S)
    assert m
    names = set(re.findall(r"\['([a-z_]+)'", m.group(1)))
    assert names == documented_faults()
    phrases = {a["data-phrase"] for a in parse_page("demo.html").find("button") if a.get("data-phrase")}
    assert phrases >= {"What do I take now?", "Dispense", "Taken", "Repeat", "Cancel", "Help", "I haven't taken it"}
    assert "postText(text, 'keyboard')" in src


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


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_page_references_only_existing_local_assets(name: str):
    page = parse_page(name)
    problems = []
    for tag, attrs, line in page.tags:
        for attr in ("src", "href", "poster", "data", "action", "formaction"):
            value = attrs.get(attr)
            if value is None or (tag == "html"):
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
    assert any(s.get("src") == "/static/js/theme-init.js" for s in scripts), "theme must be applied before paint"
    assert any(a.get("href") == "/static/css/base.css" for a in page.find("link"))


@pytest.mark.parametrize("name", list(PAGES.values()))
def test_page_has_no_inline_handlers_or_styles(name: str):
    page = parse_page(name)
    for tag, attrs, line in page.tags:
        handlers = [a for a in attrs if a.startswith("on")]
        assert not handlers, f"line {line}: inline handler {handlers} on <{tag}>"
        assert "style" not in attrs, f"line {line}: inline style on <{tag}>"
        assert not str(attrs.get("href") or "").lower().startswith("javascript:")


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
    for sink in ("insertAdjacentHTML", "document.write", "createContextualFragment", "DOMParser", "eval(", "new Function"):
        assert sink not in src, f"{_rel(path)} uses {sink}"


@pytest.mark.parametrize("path", JS_FILES, ids=_rel)
def test_no_inline_handlers_created_from_js(path: Path):
    src = strip_js_comments(_read(path))
    assert not re.search(r"setAttribute\(\s*['\"]on", src), f"{_rel(path)} sets an on* attribute"
    assert not re.search(r"\.on[a-z]+\s*=(?!=)", src), f"{_rel(path)} assigns an on* handler property"


# =========================================================================== accessibility contracts


def _hex_tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def theme_tokens() -> dict[str, dict[str, str]]:
    css = _read(STATIC / "css" / "base.css")
    dark = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    light = re.search(r':root\[data-theme="light"\]\s*\{(.*?)\n\}', css, re.S)
    assert dark and light
    dark_tokens = _hex_tokens(dark.group(1))
    return {"dark": dark_tokens, "light": {**dark_tokens, **_hex_tokens(light.group(1))}}


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
    ("fg", "bg"), ("fg", "surface"), ("fg", "surface-2"), ("fg-2", "bg"), ("fg-2", "surface"), ("fg-2", "surface-2"),
    ("fg-3", "bg"), ("fg-3", "surface"), ("fg-3", "surface-2"), ("link", "bg"), ("link", "surface"),
    ("accent", "bg"), ("accent-fg", "accent"), ("ok-fg", "bg"), ("ok-fg", "surface"), ("ok-fg", "surface-2"),
    ("warn-fg", "bg"), ("warn-fg", "surface"), ("warn-fg", "surface-2"), ("danger-fg", "bg"), ("danger-fg", "surface"),
    ("danger-fg", "surface-2"), ("info-fg", "surface"), ("info-fg", "surface-2"), ("danger-bg-fg", "danger-bg"),
    ("caution-fg", "caution-bg"), ("due-fg", "due-bg"), ("invert-fg", "invert-bg"), ("offline-fg", "offline-bg"),
]
NON_TEXT_PAIRS = [("border", "bg"), ("border", "surface"), ("focus", "bg"), ("focus", "surface"),
                  ("chart-1", "surface"), ("chart-axis", "surface")]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_theme_contrast(theme: str):
    tokens = theme_tokens()[theme]
    low = [f"--{fg} on --{bg}: {contrast(tokens[fg], tokens[bg]):.2f}" for fg, bg in TEXT_PAIRS
           if contrast(tokens[fg], tokens[bg]) < 7.0]
    assert not low, "text below 7:1 — " + "; ".join(low)
    low = [f"--{fg} on --{bg}: {contrast(tokens[fg], tokens[bg]):.2f}" for fg, bg in NON_TEXT_PAIRS
           if contrast(tokens[fg], tokens[bg]) < 3.0]
    assert not low, "non-text below 3:1 — " + "; ".join(low)


def _min_px(value: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)px", value)
    assert m, value
    return float(m.group(1))


def test_kiosk_size_minimums():
    css = _read(STATIC / "css" / "kiosk.css")
    for prop, minimum in (("--k-font-base", 32), ("--k-font-status", 56), ("--k-btn-min-h", 96)):
        values = re.findall(rf"{prop}:\s*([^;]+);", css)
        assert values, prop
        for value in values:  # every (media-query) definition keeps the minimum
            assert _min_px(value) >= minimum, f"{prop}: {value}"
    assert re.search(r"\.k-btn\s*\{[^}]*min-height:\s*var\(--k-btn-min-h\)", css)
    assert re.search(r"\.status-word\s*\{[^}]*font-size:\s*var\(--k-font-status\)", css)
    assert "prefers-reduced-motion" in _read(STATIC / "css" / "base.css")
    assert ":focus-visible" in _read(STATIC / "css" / "base.css")


def test_kiosk_page_contract():
    page = parse_page("index.html")
    live = {(a.get("id"), a.get("aria-live")) for _, a, _ in page.tags if a.get("aria-live")}
    assert ("caption", "polite") in live and ("caption-alert", "assertive") in live
    banner = next(a for _, a, _ in page.tags if a.get("id") == "status-banner")
    assert banner.get("role") == "status"
    assert any(a.get("href") == "/caregiver" for a in page.find("a"))
    cancel = next(a for a in page.find("button") if a.get("data-intent") == "CANCEL")
    assert "k-btn-cancel" in (cancel.get("class") or "")
    assert any(a.get("id") == "theme-toggle" for a in page.find("button"))
    html = _read(STATIC / "index.html")
    for key in ("SPACE", "ENTER", "ESC", ">R<", ">H<"):
        assert key in html, f"shortcut {key} not mentioned on screen"
    words = _read(STATIC / "js" / "kiosk-state.js")
    for text in ("DEVICE READY", "PREPARING", "KEEP HANDS CLEAR", "DOSE READY", "SAY 'TAKEN'", "NEEDS ASSISTANCE", "DEVICE OFFLINE"):
        assert text in words, text


def test_caregiver_tabs_structure():
    page = parse_page("caregiver.html")
    tablists = [a for _, a, _ in page.tags if a.get("role") == "tablist"]
    assert len(tablists) == 1 and tablists[0].get("aria-label")
    tabs = [a for _, a, _ in page.tags if a.get("role") == "tab"]
    assert [t["data-tab"] for t in tabs] == ["today", "medications", "scan", "compartments", "schedules", "device", "analytics"]
    panels = {a["id"]: a for _, a, _ in page.tags if a.get("role") == "tabpanel"}
    assert sum(t.get("aria-selected") == "true" for t in tabs) == 1
    for tab in tabs:
        panel = panels[tab["aria-controls"]]
        assert panel.get("aria-labelledby") == tab["id"]
        if tab.get("aria-selected") == "true":
            assert "hidden" not in panel
        else:
            assert tab.get("tabindex") == "-1" and "hidden" in panel


def test_confirmation_is_explicit_everywhere():
    html = _read(STATIC / "caregiver.html")
    page = parse_page("caregiver.html")
    for form_id, box_id in (("med-form", "med-confirm"), ("scan-form", "scan-confirm")):
        box = next(a for _, a, _ in page.tags if a.get("id") == box_id)
        assert box.get("type") == "checkbox" and box.get("name") == "confirmed" and "required" in box
        assert re.search(rf'<label for="{box_id}">I confirm this information is correct</label>', html)
        assert re.search(rf'<form id="{form_id}"[^>]*novalidate', html)
    assert "UNCONFIRMED — review every field against the label" in html
    file_input = next(a for _, a, _ in page.tags if a.get("id") == "scan-file")
    assert file_input.get("accept") == "image/jpeg,image/png,image/webp"
    scan_js = _read(STATIC / "js" / "cg" / "scan.js")
    assert "data.append('image'" in scan_js and "facingMode: { ideal: 'environment' }" in scan_js
    flows = _read(STATIC / "js" / "demo" / "flows.js")
    assert "I confirm this information is correct" in flows and "UNCONFIRMED" in flows
    medform = _read(STATIC / "js" / "medform.js")
    assert "confirmBox.checked" in medform and "body.confirmed = true" in medform


def test_chart_has_table_twin_and_follows_mark_specs():
    html = _read(STATIC / "caregiver.html")
    assert re.search(r"<details[^>]*>\s*<summary>Show the data as a table</summary>", html)
    chart = _read(STATIC / "js" / "chart.js")
    assert "MAX_BAR = 24" in chart and "radius = 4" in chart
    assert "role: 'img'" in chart and "'aria-label': describeRow(r)" in chart
    css = _read(STATIC / "css" / "caregiver.css")
    assert re.search(r"\.col-bar\s*\{\s*fill:\s*var\(--chart-1\)", css)
    assert "stroke-dasharray" not in re.search(r"\.chart-grid\s*\{[^}]*\}", css).group(0)


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
    assert.equal(f.formatClock('2026-10-04T00:30:00+00:00'), '12:30 AM');
    assert.equal(f.formatClock(null), '–');
    assert.equal(f.formatClock('nonsense'), '–');
    // UTC timestamps are shown in the device offset, never the browser's zone
    assert.equal(f.formatClockDevice('2026-10-04T15:00:00+00:00', -420), '8:00 AM');
    assert.equal(f.formatClockDevice('2026-10-04T15:00:00.123456+00:00', -420), '8:00 AM');
    assert.equal(f.deviceOffsetFrom('2026-10-04T08:00:00-07:00'), -420);
    assert.equal(f.dateKey('2026-10-04T23:30:00-07:00'), '2026-10-04');
    assert.equal(f.relativeDayWord('2026-10-05T08:00:00-07:00', '2026-10-04T22:00:00-07:00'), 'TOMORROW');
    assert.equal(f.relativeDayWord('2026-10-04T23:00:00-07:00', '2026-10-04T22:00:00-07:00'), 'TODAY');
    assert.equal(f.shiftDateKey('2026-10-31', 1), '2026-11-01');
    assert.equal(f.formatLongDate('2026-10-05'), 'Monday 5 October 2026');
    assert.equal(f.addMinutesToLocal('2026-10-04T23:50:30-07:00', 15), '2026-10-05T00:05');
    assert.equal(f.normalizeTime('8:5'), '08:05');
    assert.equal(f.normalizeTime('08:00:00'), '08:00');
    assert.equal(f.normalizeTime('25:00'), null);
    assert.equal(f.time24To12('20:00'), '8:00 PM');
    assert.ok(Math.abs(f.toPercent(0.857) - 85.7) < 1e-9);
    assert.equal(f.toPercent(86), 86);
    assert.equal(f.toPercent(null), null);
    assert.equal(f.formatPercent(1), '100%');
    assert.equal(f.formatPercent(0), '0%');
    assert.equal(f.formatMinutes(12.4), '12 min');
    assert.equal(f.formatMinutes(75), '1 h 15 min');
    assert.equal(f.formatMinutes(null), '–');
    assert.equal(f.formatOffset(7500), '+2 h 05 min');
    assert.equal(f.describeRepeat({ frequency: 'WEEKLY', days_of_week: ['FRI', 'MON'] }), 'Mon, Fri');
    assert.equal(f.describeRepeat({ frequency: 'DAILY', days_of_week: [] }), 'Every day');
    const review = f.doseStatusInfo('HARDWARE_ERROR', true);
    assert.equal(review.word, 'Hardware error');
    assert.equal(review.needsReview, true);
    for (const [status, info] of Object.entries(f.DOSE_STATUS)) {
      assert.ok(info.word && info.icon && info.tone, `${status} needs a word, an icon and a tone`);
    }
    for (const info of Object.values(f.DEVICE_STATE)) assert.ok(info.word && info.icon);
    """)


@needs_node
def test_js_kiosk_banner_logic(js_tree: Path):
    run_node(js_tree, """
    import * as k from './js/kiosk-state.js';
    const dev = (over = {}) => ({ connected: true, responsive: true, state: 'READY', homed: true, gate: 'CLOSED', slot: 0, ...over });
    const dose = (over = {}) => ({ event_id: 1, medication_name: 'Vitamin C (demo candy)', strength: '1 piece',
      compartment_number: 3, slot: 2, scheduled_local: '2026-10-04T08:00:00-07:00', status: 'DUE', ...over });
    const base = (over = {}) => ({ loaded: true, serverOnline: true, stateError: false, device: dev(), phase: 'IDLE',
      awaiting: null, due: { due: [], awaiting_confirmation: [], accessed: [], blocked: [], next_upcoming: null },
      nowLocal: '2026-10-04T07:55:00-07:00', ...over });
    const key = (v) => k.deriveBanner(v).key;

    assert.equal(key({ loaded: false }), 'connecting');
    assert.equal(k.deriveBanner({ loaded: false, serverOnline: false }).word, 'DEVICE OFFLINE');
    assert.equal(k.deriveBanner(base({ serverOnline: false })).detail, 'RECONNECTING…');
    assert.equal(k.deriveBanner(base({ stateError: true })).detail, 'STATUS UNAVAILABLE');
    assert.equal(key(base({ device: null })), 'offline');
    assert.equal(key(base({ device: dev({ connected: false }) })), 'offline');
    assert.equal(k.deriveBanner(base({ device: dev({ responsive: false }) })).detail, 'NOT RESPONDING');
    // fail closed: an offline device never shows DOSE READY
    assert.equal(key(base({ device: dev({ connected: false }), phase: 'AWAITING_CONFIRMATION', awaiting: dose() })), 'offline');
    // carousel motion always wins: keep hands clear
    for (const state of ['MOVING', 'AT_TARGET', 'HOMING']) {
      const b = k.deriveBanner(base({ device: dev({ state }), phase: 'AWAITING_CONFIRMATION' }));
      assert.equal(b.word, 'PREPARING');
      assert.equal(b.detail, 'KEEP HANDS CLEAR');
    }
    assert.equal(key(base({ phase: 'PREPARING' })), 'preparing');
    assert.equal(k.deriveBanner(base({ device: dev({ state: 'FAULT' }) })).word, 'NEEDS ASSISTANCE');
    assert.equal(key(base({ phase: 'ATTENTION' })), 'attention');
    const ready = base({ phase: 'AWAITING_CONFIRMATION', awaiting: dose({ status: 'DISPENSED' }) });
    assert.equal(k.deriveBanner(ready).word, 'DOSE READY');
    assert.equal(k.deriveBanner(ready).detail, "SAY 'TAKEN'");
    assert.equal(k.suggestedIntent(ready), 'CONFIRM_TAKEN');
    const review = base({ due: { ...base().due, blocked: [{ dose: dose({ status: 'HARDWARE_ERROR' }), reason: 'NEEDS_REVIEW' }] } });
    assert.equal(key(review), 'attention');
    assert.equal(key(base({ device: dev({ gate: 'OPEN', state: 'GATE_OPEN' }) })), 'open');
    const due = base({ due: { ...base().due, due: [dose()] } });
    assert.equal(k.deriveBanner(due).word, 'DOSE DUE');
    assert.equal(k.suggestedIntent(due), 'DISPENSE');
    assert.equal(k.deriveBanner(base()).word, 'DEVICE READY');
    assert.equal(k.suggestedIntent(base()), null);
    for (const b of Object.values(k.BANNERS)) assert.ok(b.word && b.icon && b.tone, b.key);

    assert.equal(k.nextEventText(ready), 'OPEN NOW: COMPARTMENT 3');
    assert.equal(k.nextEventText(due), 'DUE NOW: 8:00 AM · COMPARTMENT 3');
    const later = base({ due: { ...base().due, next_upcoming: dose({ scheduled_local: '2026-10-04T14:00:00-07:00' }) } });
    assert.equal(k.nextEventText(later), 'NEXT EVENT: 2:00 PM · COMPARTMENT 3');
    const tomorrow = base({ due: { ...base().due, next_upcoming: dose({ scheduled_local: '2026-10-05T08:00:00-07:00' }) } });
    assert.equal(k.nextEventText(tomorrow), 'NEXT EVENT: TOMORROW 8:00 AM · COMPARTMENT 3');
    const unassigned = base({ due: { ...base().due, due: [dose({ compartment_number: null, slot: null })] } });
    assert.equal(k.nextEventText(unassigned), 'DUE NOW: 8:00 AM · NO COMPARTMENT ASSIGNED');
    assert.equal(k.nextEventText(base()), 'NO UPCOMING DOSES');
    assert.equal(k.nextEventText(base({ serverOnline: false })), '');
    assert.equal(k.doseDetailText(due), 'VITAMIN C (DEMO CANDY) · 1 PIECE');

    assert.equal(k.intentForKey('Escape', { typing: true }), 'CANCEL');
    assert.equal(k.intentForKey(' '), 'PRIMARY_ACTION');
    assert.equal(k.intentForKey('Enter'), 'PRIMARY_ACTION');
    assert.equal(k.intentForKey(' ', { onControl: true }), null);
    assert.equal(k.intentForKey('r'), 'REPEAT');
    assert.equal(k.intentForKey('H'), 'HELP');
    assert.equal(k.intentForKey('r', { typing: true }), null);
    assert.equal(k.intentForKey('x'), null);
    assert.equal(k.voiceText({ enabled: true, listening: true }).word, 'VOICE ON');
    assert.equal(k.voiceText({ enabled: false }).word, 'VOICE OFF');
    """)


@needs_node
def test_js_api_client(js_tree: Path):
    run_node(js_tree, """
    import * as api from './js/api.js';
    const calls = [];
    const queue = [];
    const store = new Map();
    let prompts = 0;
    let promptAnswer = '4321';
    const respond = (status, body, { text = false } = {}) => queue.push({ status, body, text });
    api.configureApi({
      fetch: async (path, init) => {
        calls.push({ path, ...init });
        const next = queue.shift();
        if (!next) throw new TypeError('Failed to fetch');
        const raw = next.body === undefined ? '' : (next.text ? next.body : JSON.stringify(next.body));
        return { ok: next.status < 300, status: next.status, statusText: 'X', text: async () => raw };
      },
      storage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) },
      promptPin: () => { prompts += 1; return promptAnswer; },
    });

    respond(200, { ok: true });
    assert.deepEqual(await api.get('/api/state'), { ok: true });
    assert.equal(calls[0].method, 'GET');
    assert.equal(calls[0].headers.Accept, 'application/json');
    assert.equal(calls[0].headers['Content-Type'], undefined);
    assert.equal(calls[0].headers['X-Caregiver-Pin'], undefined);

    respond(201, { medication_id: 7 });
    await api.post('/api/medications', { name: 'X', confirmed: true });
    assert.equal(calls[1].headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(calls[1].body), { name: 'X', confirmed: true });

    respond(422, { detail: [{ loc: ['body', 'confirmed'], msg: 'Input should be True', type: 'literal_error' }] });
    await assert.rejects(api.post('/api/medications', {}), (e) => e instanceof api.ApiError && e.status === 422
      && e.message === 'confirmed: Input should be True' && Array.isArray(e.detail));
    respond(409, { detail: 'A dose is awaiting confirmation' });
    await assert.rejects(api.post('/api/compartments/2/present', {}), (e) => e.status === 409 && e.message === 'A dose is awaiting confirmation');
    respond(500, 'Internal Server Error', { text: true });
    await assert.rejects(api.get('/api/state'), (e) => e.status === 500 && e.message === 'Internal Server Error');

    // 401 -> ask for the PIN once, retry once with the header, remember the PIN
    respond(401, { detail: 'Caregiver PIN required' });
    respond(200, { ok: true });
    const n = calls.length;
    assert.deepEqual(await api.post('/api/hardware/home', {}), { ok: true });
    assert.equal(prompts, 1);
    assert.equal(calls[n + 1].headers['X-Caregiver-Pin'], '4321');
    assert.equal(store.get(api.PIN_STORAGE_KEY), '4321');
    respond(200, []);
    await api.get('/api/schedules');
    assert.equal(calls.at(-1).headers['X-Caregiver-Pin'], '4321');

    // wrong PIN twice -> forget it, no endless retry
    respond(401, { detail: 'Caregiver PIN required' });
    respond(401, { detail: 'Caregiver PIN required' });
    await assert.rejects(api.post('/api/hardware/stop', {}), (e) => e.status === 401);
    assert.equal(store.has(api.PIN_STORAGE_KEY), false);
    assert.equal(prompts, 2);

    // prompt cancelled -> no retry
    promptAnswer = null;
    respond(401, { detail: 'Caregiver PIN required' });
    const before = calls.length;
    await assert.rejects(api.post('/api/demo/seed', {}), (e) => e.status === 401);
    assert.equal(calls.length, before + 1);

    // network failure -> status 0, network flag
    await assert.rejects(api.get('/api/state'), (e) => e.status === 0 && e.network === true);

    // multipart upload: FormData passed through, no JSON content type
    const form = new FormData();
    form.append('image', new Blob(['x'], { type: 'image/png' }), 'a.png');
    respond(200, { scan_id: 1, status: 'PENDING_REVIEW' });
    await api.upload('/api/onboarding/scan', form);
    assert.equal(calls.at(-1).body, form);
    assert.equal(calls.at(-1).headers['Content-Type'], undefined);

    respond(200, { text: 'ok' });
    await api.postIntent('DISPENSE');
    assert.equal(calls.at(-1).path, '/api/intents');
    assert.deepEqual(JSON.parse(calls.at(-1).body), { intent: 'DISPENSE', source: 'ui' });
    respond(200, { text: 'ok' });
    await api.postText('what do i take now');
    assert.deepEqual(JSON.parse(calls.at(-1).body), { text: 'what do i take now', source: 'keyboard' });
    respond(204, undefined);
    assert.equal(await api.del('/api/schedules/3'), null);
    assert.equal(api.detailToMessage(null, 'fallback'), 'fallback');
    """)


@needs_node
def test_js_event_stream(js_tree: Path):
    run_node(js_tree, """
    import { EventStream, RECONNECTED, KNOWN_TOPICS, parseTimestamp } from './js/events.js';
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
    stream.on('device.state', (data, env, meta) => got.push({ data, seq: env.seq, replayed: meta.replayed }));
    stream.on('*', (_d, env) => all.push(env.topic));
    stream.on(RECONNECTED, () => { reconnected += 1; });
    stream.start();
    assert.equal(FakeES.all.length, 1);
    assert.equal(FakeES.all[0].url, '/api/events');
    for (const t of KNOWN_TOPICS) assert.ok(FakeES.all[0].listeners[t], `listens to ${t}`);
    assert.equal(stream.status, 'connecting');

    const es = FakeES.all[0];
    es.emit('open');
    assert.equal(stream.status, 'open');
    const iso = (ms) => new Date(ms).toISOString().replace('Z', '123+00:00');
    // replayed history right after connecting
    es.emit('device.state', { seq: 1, topic: 'device.state', data: { state: 'READY' }, ts: iso(now - 60000) });
    // a live event
    es.emit('device.state', { seq: 2, topic: 'device.state', data: { state: 'MOVING' }, ts: iso(now) });
    // duplicate (same seq + ts) is dropped
    es.emit('device.state', { seq: 2, topic: 'device.state', data: { state: 'MOVING' }, ts: iso(now) });
    es.emit('device.state', '{not json');
    assert.deepEqual(got.map((g) => [g.seq, g.replayed]), [[1, true], [2, false]]);
    assert.deepEqual(all, ['device.state', 'device.state']);

    // a topic registered later is subscribed on the live source
    const later = [];
    stream.on('custom.topic', (d) => later.push(d));
    es.emit('custom.topic', { seq: 3, topic: 'custom.topic', data: { a: 1 }, ts: iso(now) });
    assert.deepEqual(later, [{ a: 1 }]);

    // drop -> managed reconnect with exponential backoff
    es.emit('error');
    assert.equal(stream.status, 'reconnecting');
    assert.equal(es.closed, true);
    assert.equal(timers.at(-1).ms, 1000);
    timers.at(-1).fn();
    assert.equal(FakeES.all.length, 2);
    FakeES.all[1].emit('error');
    assert.equal(timers.at(-1).ms, 2000);
    timers.at(-1).fn();
    FakeES.all[2].emit('error');
    assert.equal(timers.at(-1).ms, 4000);
    timers.at(-1).fn();
    now += 10000;
    FakeES.all[3].emit('open');
    assert.equal(stream.status, 'open');
    assert.equal(reconnected, 1);
    // after a server restart seq starts again at 1, but ts differs -> delivered
    FakeES.all[3].emit('device.state', { seq: 1, topic: 'device.state', data: { state: 'BOOT' }, ts: iso(now) });
    assert.equal(got.at(-1).data.state, 'BOOT');
    // replay of an event already seen before the drop is not delivered twice
    FakeES.all[3].emit('device.state', { seq: 2, topic: 'device.state', data: { state: 'MOVING' }, ts: iso(100000) });
    assert.equal(got.filter((g) => g.seq === 2).length, 1);

    stream.close();
    assert.equal(stream.status, 'closed');
    FakeES.all[3].emit('error');
    assert.equal(FakeES.all.length, 4, 'no reconnect after close()');
    assert.deepEqual(statuses, ['idle', 'connecting', 'open', 'reconnecting', 'open', 'closed']);
    assert.ok(Number.isFinite(parseTimestamp('2026-10-04T15:00:00.123456+00:00')));
    """)


@needs_node
def test_js_chart_carousel_and_log_helpers(js_tree: Path):
    run_node(js_tree, """
    import { chartRows, directLabelIndexes, labelEvery, columnPath, describeRow } from './js/chart.js';
    import { polar, slotAngle, shortestDelta, sectorPath, CarouselView } from './js/carousel.js';
    import { isHeartbeat } from './js/linelog.js';
    import { describeCommand, snapshotRows } from './js/hwview.js';

    const rows = chartRows([
      { date: '2026-10-03', scheduled: 3, taken: 3, missed: 0, rate: 1 },
      { date: '2026-10-04', scheduled: 3, taken: 1, missed: 2 },
      { date: '2026-10-05', scheduled: 0, taken: 0, missed: 0, rate: 0 },
      { date: '2026-10-06', scheduled: 2, taken: 1, missed: 0, rate: 50 },
    ]);
    assert.equal(rows[0].pct, 100);
    assert.ok(Math.abs(rows[1].pct - 33.333) < 0.01, 'rate computed when missing');
    assert.equal(rows[2].pct, null, 'no doses -> no rate');
    assert.equal(rows[3].pct, 50);
    assert.equal(rows[3].other, 1);
    assert.deepEqual([...directLabelIndexes(rows)].sort(), [1, 3], 'endpoint + lowest day only');
    assert.equal(labelEvery(20), 3);
    assert.equal(labelEvery(100), 1);
    const d = columnPath(10, 20, 24, 100);
    assert.ok(d.startsWith('M10 120V24A4 4') && d.endsWith('Z'), d);
    assert.equal(columnPath(0, 0, 24, 0), '');
    assert.equal(describeRow(rows[1]), 'Sunday 4 October 2026: 33% taken, 1 of 3 doses, 2 missed');
    assert.equal(describeRow(rows[2]), 'Monday 5 October 2026: no doses scheduled');

    assert.deepEqual(polar(100, 90), [100, 0]);
    assert.deepEqual(polar(100, 0), [0, -100]);
    assert.equal(slotAngle(3, 6), 180);
    assert.equal(shortestDelta(350, 10), 20);
    assert.equal(shortestDelta(10, 350), -20);
    assert.ok(sectorPath(0, 6).startsWith('M-56 -96.99A112 112 0 0 1 56 -96.99'), sectorPath(0, 6));
    const describe = (state) => CarouselView.prototype.describe.call({ state });
    assert.equal(describe({ slot: 2, gate: 'OPEN', targetSlot: null }), 'Compartment 3 is at the gate. Gate open.');
    assert.equal(describe({ slot: null, gate: 'CLOSED', targetSlot: 3, moving: true }),
      'The carousel is turning. Moving to compartment 4. Gate closed.');
    assert.equal(describe({ slot: null, gate: 'UNKNOWN', targetSlot: null, moving: false }),
      'Position unknown (between compartments or not homed). Gate state unknown.');

    assert.ok(isHeartbeat('PING') && isHeartbeat('OK PONG') && isHeartbeat('OK STATUS state=READY homed=1'));
    assert.ok(!isHeartbeat('MOVE_SLOT 3') && !isHeartbeat('ERR BUSY'));
    assert.equal(describeCommand({ ok: false, result: { command: 'DISPENSE_SLOT 2', ok: false, code: 'TIMEOUT', definitive: false } }),
      'DISPENSE_SLOT 2 → failed (TIMEOUT, uncertain)');
    const snap = Object.fromEntries(snapshotRows({ connected: true, responsive: true, state: 'FAULT', homed: false, slot: null, gate: 'CLOSED' }));
    assert.equal(snap['State'], 'Fault (needs homing) (FAULT)');
    assert.equal(snap['At the gate'], 'Unknown / between compartments');
    """)


@needs_node
def test_js_form_validation(js_tree: Path):
    run_node(js_tree, """
    import { readMedicationForm, linesOf } from './js/medform.js';
    import { scheduleBody } from './js/cg/schedules.js';
    const fakeForm = (values) => ({ elements: { namedItem: (n) => (n in values ? (n === 'confirmed' ? { checked: values[n] } : { value: values[n] }) : null) } });
    const fields = { name: ' Vitamin C ', strength: '', instructions_text: 'Take one.', warnings: 'a\\n\\n b ', confirmed_by: 'Ana', confirmed: true };

    assert.equal(readMedicationForm(fakeForm({ ...fields, name: '  ' })).field, 'name');
    const unconfirmed = readMedicationForm(fakeForm({ ...fields, confirmed: false }));
    assert.equal(unconfirmed.ok, false);
    assert.equal(unconfirmed.field, 'confirmed', 'nothing is sent without the confirmation tick');
    const ok = readMedicationForm(fakeForm(fields));
    assert.deepEqual(ok.body, { name: 'Vitamin C', instructions_text: 'Take one.', warnings: ['a', 'b'], confirmed: true, confirmed_by: 'Ana' });
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
    """)
