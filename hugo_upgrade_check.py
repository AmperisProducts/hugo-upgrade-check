#!/usr/bin/env python3
"""hugo-upgrade-check: build a Hugo project with its current Hugo version and with a newer
release, then report what the upgrade would change in the published site.

It answers one question before you bump the version: does the new release change my site,
and if so, where? The comparison covers:

  * files published or no longer published (processed images are matched even when Hugo
    renames them because the image processing hash changed)
  * SEO elements of every HTML page: title, meta description, canonical, og:url, robots,
    hreflang alternates and alias redirects
  * pages whose HTML changed after normalizing build noise (generator tag, fingerprints,
    integrity attributes, version strings)
  * other files with different bytes, by extension
  * images with different bytes, and whether they look different (needs Pillow)
  * URLs added to or removed from the sitemap
  * new WARN and ERROR lines in the build log
  * release notes between both versions: security fixes, breaking changes, and
    deprecated or removed identifiers that your templates or configuration still use

Only the Python standard library is required. Pillow is optional (visual image check).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

__version__ = "1.0.0"
REPO = "gohugoio/hugo"
UA = f"hugo-upgrade-check/{__version__} (+https://github.com/AmperisProducts/hugo-upgrade-check)"
NO_EXTENDED_SINCE = (0, 170, 0)  # Hugo 0.170.0 dropped the extended edition
GENERATED_HASH = re.compile(r"_(?:hu_?)?[0-9a-f]{12,64}(?=[._])")  # processed images, QR codes, etc.
FINGERPRINT = re.compile(r"\.[0-9a-f]{32,64}(?=\.(?:min\.)?(?:css|js|mjs)\b)")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".tif", ".tiff", ".bmp"}
PSNR_THRESHOLD = 35.0
MAX_VISUAL_PAIRS = 800
TEXT_OUTPUT = {".xml", ".json", ".txt", ".css", ".js", ".mjs", ".svg", ".webmanifest", ".csv", ".rss",
               ".atom", ".ics", ".md"}

SECURITY = re.compile(r"CVE-\d{4}-\d+|GHSA-[\w-]+|\bsecurity\b|\bvulnerab", re.I)
DEPRECATION = re.compile(r"deprecat|remov|no longer|breaking", re.I)
BREAKING = re.compile(r"\bbreaking\b", re.I)
IDENT = re.compile(r"`([^`\s]{4,60})`|\b([a-z]+\.[A-Z]\w+)\b|(\.(?:Site|Page|File|Params|Resources|Sites|Language)\.\w+)")
LITERALS = {"true", "false", "null", "none", "nil", "hugo", "extended"}
PROJECT_DIRS = ("layouts", "config", "assets", "archetypes", "themes", "_vendor")
PROJECT_FILES = ("hugo.toml", "hugo.yaml", "hugo.yml", "hugo.json", "config.toml", "config.yaml", "config.yml",
                 "config.json")
TEXT_SUFFIXES = {".html", ".xml", ".json", ".toml", ".yaml", ".yml", ".js", ".ts", ".txt", ".md", ".css", ".scss"}
VERSION_HINTS = [  # where projects usually pin the Hugo version
    (re.compile(r"HUGO_VERSION\s*[:=]\s*['\"]?v?(\d+\.\d+\.\d+)"), "HUGO_VERSION"),
    (re.compile(r"hugo-version\s*:\s*['\"]?v?(\d+\.\d+\.\d+)"), "hugo-version"),
    (re.compile(r"\bmin\s*=\s*['\"]v?(\d+\.\d+\.\d+)"), "module.hugoVersion.min"),
]


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(msg, flush=True)


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def http(url: str, raw: bool = False, timeout: int = 60):
    headers = {"User-Agent": UA}
    if "api.github.com" in url:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                body = r.read()
            return body if raw else json.loads(body or b"null")
        except Exception as e:  # network hiccups and GitHub rate limiting
            last = e
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"Request failed: {url}: {last}")


def github_output(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in kv.items():
            fh.write(f"{k}={v}\n")


def text_files(root: Path):
    for d in PROJECT_DIRS:
        base = root / d
        if base.is_dir():
            for f in base.rglob("*"):
                if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES and "node_modules" not in f.parts:
                    yield f
    for name in PROJECT_FILES:
        if (root / name).is_file():
            yield root / name


# -------------------------------------------------------------------------- versions
def stable_releases() -> list:
    rels = []
    for page in range(1, 4):
        batch = http(f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}") or []
        rels += [r for r in batch if not r.get("draft") and not r.get("prerelease")]
        if len(batch) < 100:
            break
    if not rels:
        latest = http(f"https://api.github.com/repos/{REPO}/releases/latest")
        rels = [latest] if latest and latest.get("tag_name") else []
    if not rels:
        raise SystemExit("Could not read Hugo releases from the GitHub API")
    return rels


def find_pinned_versions(root: Path) -> list:
    """Places in the project that pin a Hugo version: (file, line, kind, version)."""
    found = []
    candidates = list(root.glob("*.toml")) + list(root.glob("*.json")) + list(root.glob("*.jsonc")) + \
        list(root.glob("*.sh")) + list(root.glob(".hugo-version")) + list(root.glob("config/**/*.toml")) + \
        list(root.glob("config/**/*.y*ml")) + list(root.glob(".github/workflows/*.y*ml")) + \
        list(root.glob(".gitlab-ci.yml")) + list(root.glob("*.y*ml"))
    for f in sorted(set(candidates)):
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if f.name == ".hugo-version":
                m = re.match(r"\s*v?(\d+\.\d+\.\d+)", line)
                if m:
                    found.append((str(f.relative_to(root)), n, ".hugo-version", m.group(1)))
                continue
            for rx, kind in VERSION_HINTS:
                m = rx.search(line)
                if m and (kind != "module.hugoVersion.min" or f.suffix == ".toml"):
                    found.append((str(f.relative_to(root)), n, kind, m.group(1)))
    return found


def detect_current(root: Path, pinned: list) -> str | None:
    if os.environ.get("HUGO_VERSION"):
        return os.environ["HUGO_VERSION"].lstrip("v")
    exact = [p[3] for p in pinned if p[2] != "module.hugoVersion.min"]
    pool = exact or [p[3] for p in pinned]
    return max(pool, key=vtuple) if pool else None


# -------------------------------------------------------------------------- install
def asset_name(v: str, extended: bool) -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    use_ext = extended and vtuple(v) < NO_EXTENDED_SINCE
    edition = "hugo_extended" if use_ext else "hugo"
    if system == "linux":
        return f"{edition}_{v}_linux-{arch}.tar.gz"
    if system == "darwin":
        return f"{edition}_{v}_darwin-universal.tar.gz"
    if system == "windows":
        return f"{edition}_{v}_windows-{arch}.zip"
    raise SystemExit(f"Unsupported platform: {system}/{machine}")


def install(v: str, dest: Path, extended: bool) -> Path:
    name = asset_name(v, extended)
    base = f"https://github.com/{REPO}/releases/download/v{v}"
    sums = http(f"{base}/hugo_{v}_checksums.txt", raw=True).decode()
    expected = next((l.split()[0] for l in sums.splitlines() if l.strip().endswith(name)), None)
    if not expected:
        raise SystemExit(f"Hugo v{v} has no asset named {name}")
    log(f"Downloading {name}")
    data = http(f"{base}/{name}", raw=True, timeout=600)
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit(f"SHA-256 mismatch for {name}")
    dest.mkdir(parents=True, exist_ok=True)
    exe = "hugo.exe" if name.endswith(".zip") else "hugo"
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            (dest / exe).write_bytes(z.read(exe))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            (dest / exe).write_bytes(t.extractfile(t.getmember("hugo")).read())
        (dest / exe).chmod(0o755)
    return dest / exe


def build(hugo: Path, root: Path, out: Path, work: Path, label: str, args: list) -> dict:
    env = dict(os.environ, HUGO_CACHEDIR=str(work / f"cache-{label}"),
               HUGO_RESOURCEDIR=str(work / f"resources-{label}"))
    env.pop("HUGO_VERSION", None)
    cmd = [str(hugo), "build", "--logLevel", "warn", "--destination", str(out)] + args
    log(f"Building with {label}: {' '.join(shlex.quote(c) for c in cmd)}")
    t0 = time.time()
    p = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = p.stdout + p.stderr
    (work / f"build-{label}.log").write_text(output, encoding="utf-8")
    return {"exit_code": p.returncode, "seconds": round(time.time() - t0, 1), "log": output}


# -------------------------------------------------------------------- release notes
def analyse_notes(releases: list, current: str, target: str, root: Path) -> dict:
    span = sorted((r for r in releases if vtuple(current) < vtuple(r["tag_name"]) <= vtuple(target)),
                  key=lambda r: vtuple(r["tag_name"]))
    notes = "\n\n".join(f"## {r['tag_name']}\n{r.get('body') or ''}" for r in span)
    corpus = {str(f.relative_to(root)): f.read_text(encoding="utf-8", errors="ignore") for f in text_files(root)}
    security = sorted({s.lower() if s.lower() in ("security", "vulnerab") else s: s
                       for s in SECURITY.findall(notes)}.values(), key=str.lower)
    breaking, used = [], {}
    for line in notes.splitlines():
        if BREAKING.search(line):
            breaking.append(line.strip()[:300])
        if not DEPRECATION.search(line):
            continue
        for groups in IDENT.findall(line):
            ident = next(g for g in groups if g).split("(")[0].strip()
            if len(ident) < 4 or ident.lower() in LITERALS or ident in used:
                continue
            where = [path for path, txt in corpus.items() if ident in txt]
            if where:
                used[ident] = {"files": where[:10], "note": line.strip()[:300]}
    return {"releases": [r["tag_name"] for r in span], "security": security,
            "breaking": list(dict.fromkeys(breaking)), "deprecated_in_use": used, "notes": notes}


# ------------------------------------------------------------------------ comparison
ATTR = re.compile(r'([a-zA-Z:-]+)(?:\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+))?')


def attrs(tag: str) -> dict:
    return {k.lower(): (v.strip("\"'") if v else "") for k, v in ATTR.findall(tag[1:])}


def seo_signature(html: str) -> dict:
    sig = {"hreflang": []}
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    sig["title"] = m.group(1).strip() if m else None
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        a = attrs(tag)
        rel = a.get("rel", "").lower()
        if rel == "canonical":
            sig["canonical"] = a.get("href")
        elif rel == "alternate" and "hreflang" in a:
            sig["hreflang"].append((a["hreflang"], a.get("href")))
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        a = attrs(tag)
        key = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
        if key in {"description", "robots", "og:url", "refresh"}:
            sig[key] = a.get("content")
    sig["hreflang"].sort()
    return sig


def normalize(html: str, versions) -> str:
    html = re.sub(r"<meta\b[^>]*\bname=[\"']?generator\b[^>]*>", "", html, flags=re.I)
    html = re.sub(r"<generator\b[^>]*>.*?</generator>", "", html, flags=re.I | re.S)
    html = FINGERPRINT.sub(".H", html)
    html = re.sub(r"\sintegrity=(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    html = GENERATED_HASH.sub("_H", html)
    for v in versions:
        html = html.replace(v, "HUGOVERSION")
    return html


def sitemap_urls(d: Path) -> set:
    urls = set()
    for f in d.rglob("sitemap*.xml"):
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", f.read_text(encoding="utf-8", errors="ignore")):
            if not loc.endswith(".xml"):
                urls.add(loc)
    return urls


def warnings(output: str) -> set:
    found = set()
    for line in output.splitlines():
        if re.search(r"\b(WARN|ERROR)\b", line):
            line = re.sub(r"\d+(\.\d+)?\s?(ms|s|µs|us)\b", "", line)
            found.add(re.sub(r"\s+", " ", line).strip())
    return found


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def image_size(p: Path):
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.size
    except Exception:
        return None


def psnr(a: Path, b: Path):
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        return None
    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            if ia.size != ib.size:
                return "size"
            ia, ib = ia.convert("RGBA"), ib.convert("RGBA")
            st = ImageStat.Stat(ImageChops.difference(ia, ib))
            mse = sum(st.sum2) / (ia.size[0] * ia.size[1] * 4)
            return float("inf") if mse == 0 else 10 * math.log10(255 ** 2 / mse)
    except Exception:
        return None


def compare(old: Path, new: Path, versions, rows: list) -> dict:
    def index(d: Path):
        groups = defaultdict(list)
        for p in d.rglob("*"):
            if p.is_file():
                rel = p.relative_to(d).as_posix()
                groups[GENERATED_HASH.sub("_H", rel)].append(rel)
        return groups

    go, gn = index(old), index(new)
    removed = added = renamed = 0
    pairs = []
    for key in sorted(set(go) | set(gn)):
        lo, ln = sorted(go.get(key, [])), sorted(gn.get(key, []))
        if len(lo) > len(ln):
            removed += len(lo) - len(ln)
            rows.append(("file_removed", key, f"{len(lo)} -> {len(ln)}"))
        elif len(ln) > len(lo):
            added += len(ln) - len(lo)
            rows.append(("file_added", key, f"{len(lo)} -> {len(ln)}"))
        if lo == ln or len(lo) == len(ln) == 1:
            pairs += list(zip(lo, ln))
        else:  # several generated files share one name pattern: pair identical bytes first
            by_hash = defaultdict(list)
            for rel in ln:
                by_hash[sha256(new / rel)].append(rel)
            rest_old = []
            for rel in lo:
                bucket = by_hash.get(sha256(old / rel))
                if bucket:
                    pairs.append((rel, bucket.pop()))
                else:
                    rest_old.append(rel)
            rest_new = [rel for bucket in by_hash.values() for rel in bucket]

            def keyer(d):
                return lambda r: (image_size(d / r) or (0, 0), (d / r).stat().st_size, r)
            pairs += list(zip(sorted(rest_old, key=keyer(old)), sorted(rest_new, key=keyer(new))))
    seo = html_changed = html_total = 0
    other, images = defaultdict(int), []
    for ro, rn in pairs:
        a, b = old / ro, new / rn
        if ro != rn:
            renamed += 1
        if ro.endswith(".html"):
            html_total += 1
            ta = a.read_text(encoding="utf-8", errors="ignore")
            tb = b.read_text(encoding="utf-8", errors="ignore")
            sa, sb = seo_signature(ta), seo_signature(tb)
            if sa != sb:
                seo += 1
                diff = {k: [sa.get(k), sb.get(k)] for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)}
                rows.append(("seo_changed", ro, json.dumps(diff, ensure_ascii=False)[:1500]))
            if normalize(ta, versions) != normalize(tb, versions):
                html_changed += 1
                rows.append(("html_changed", ro, ""))
        elif a.stat().st_size != b.stat().st_size or sha256(a) != sha256(b):
            if FINGERPRINT.search(ro) or re.search(r"\.[0-9a-f]{32,64}\.", ro):
                continue  # fingerprinted asset: a new hash is expected
            if a.suffix.lower() in IMG_EXT:
                images.append((ro, rn))
                continue
            if a.suffix.lower() in TEXT_OUTPUT and \
                    normalize(a.read_text(encoding="utf-8", errors="ignore"), versions) == \
                    normalize(b.read_text(encoding="utf-8", errors="ignore"), versions):
                continue  # only build noise, such as the generator version in RSS
            other[a.suffix.lower() or "(no extension)"] += 1
            rows.append(("file_changed", ro, ""))
    sample = images if len(images) <= MAX_VISUAL_PAIRS else random.Random(0).sample(images, MAX_VISUAL_PAIRS)
    look_different, unverifiable, worst = 0, 0, float("inf")
    for ro, rn in sample:
        v = psnr(old / ro, new / rn)
        if v is None:
            unverifiable += 1
        elif v == "size":
            look_different += 1
            rows.append(("image_size_changed", ro, rn))
        else:
            worst = min(worst, v)
            if v < PSNR_THRESHOLD:
                look_different += 1
                rows.append(("image_looks_different", ro, f"{v:.1f} dB"))
    so, sn = sitemap_urls(old), sitemap_urls(new)
    for u in sorted(so - sn):
        rows.append(("sitemap_url_removed", u, ""))
    for u in sorted(sn - so):
        rows.append(("sitemap_url_added", u, ""))
    return {"files_old": sum(map(len, go.values())), "files_new": sum(map(len, gn.values())),
            "files_removed": removed, "files_added": added, "files_renamed": renamed,
            "html_pages": html_total, "html_changed": html_changed, "seo_changed": seo,
            "other_changed": dict(other), "images_changed": len(images), "images_checked": len(sample),
            "images_look_different": look_different, "images_unverifiable": unverifiable,
            "worst_psnr_db": None if worst == float("inf") else round(worst, 1),
            "sitemap_urls_old": len(so), "sitemap_urls_new": len(sn), "sitemap_changes": len(so ^ sn)}


# ---------------------------------------------------------------------------- report
def markdown(r: dict) -> str:
    out = [f"# Hugo upgrade check: v{r['current']} → v{r['target']}", ""]
    out.append(f"**Result: {r['result']}**")
    out.append("")
    for reason in r["reasons"]:
        out.append(f"- {reason}")
    if r["reasons"]:
        out.append("")
    n = r.get("notes") or {}
    if n:
        out += ["## Release notes", "", f"Releases covered: {', '.join(n['releases']) or 'none'}", ""]
        if n["security"]:
            out += [f"- Security references: {', '.join(n['security'][:15])}"]
        for line in n["breaking"][:15]:
            out += [f"- Breaking: {line}"]
        for ident, info in n["deprecated_in_use"].items():
            out += [f"- `{ident}` is deprecated or removed and appears in: {', '.join(info['files'][:5])}"]
        out.append("")
    b = r.get("builds") or {}
    if b:
        out += ["## Builds", "", "Version | Exit code | Seconds", ":-- | --: | --:"]
        for label in ("current", "target"):
            if label in b:
                out.append(f"v{r[label]} | {b[label]['exit_code']} | {b[label]['seconds']}")
        out.append("")
    c = r.get("comparison")
    if c:
        out += ["## Output comparison", "", "Check | Result", ":-- | :--",
                f"Published files | {c['files_old']} → {c['files_new']} ({c['files_removed']} removed, "
                f"{c['files_added']} added, {c['files_renamed']} renamed by a new content hash)",
                f"HTML pages with different content | {c['html_changed']} of {c['html_pages']}",
                f"HTML pages with different SEO elements | {c['seo_changed']}",
                f"Other files with different bytes | "
                f"{', '.join(f'{k}: {v}' for k, v in sorted(c['other_changed'].items())) or 'none'}",
                f"Images with different bytes | {c['images_changed']} ({c['images_look_different']} look different "
                f"of {c['images_checked']} checked; worst PSNR {c['worst_psnr_db'] if c['worst_psnr_db'] is not None else 'n/a'} dB; "
                f"{c['images_unverifiable']} not verifiable)",
                f"Sitemap URLs | {c['sitemap_urls_old']} → {c['sitemap_urls_new']} ({c['sitemap_changes']} changes)",
                f"New WARN/ERROR lines | {len(r.get('new_warnings', []))}", ""]
        for w in r.get("new_warnings", [])[:20]:
            out.append(f"    {w}")
        if r.get("new_warnings"):
            out.append("")
    if r.get("pinned"):
        out += ["## Where this project pins the Hugo version", ""]
        for f, line, kind, v in r["pinned"]:
            out.append(f"- `{f}` line {line} ({kind}): {v}")
        out.append("")
    out.append(f"Details: `differences.csv` and the build logs in the output directory. "
               f"Generated by hugo-upgrade-check {__version__}.")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare a Hugo project's output between two Hugo versions.")
    ap.add_argument("--source", default=".", help="project directory (default: current directory)")
    ap.add_argument("--current", help="Hugo version the project uses now (default: detected from the project)")
    ap.add_argument("--target", default="latest", help="Hugo version to test (default: latest stable release)")
    ap.add_argument("--build-args", default="", help="extra arguments for 'hugo build', e.g. \"--minify --gc\"")
    ap.add_argument("--output", default="hugo-upgrade-check", help="directory for the report and logs")
    ap.add_argument("--no-extended", action="store_true", help="use the standard edition for versions before 0.170.0")
    ap.add_argument("--max-html-changed", type=float, default=0.0,
                    help="fraction of HTML pages allowed to change before the result is 'differences' (default 0)")
    ap.add_argument("--fail-on", choices=["never", "failure", "differences"], default="failure",
                    help="exit with code 1 on: build failure only (default), any difference, or never")
    ap.add_argument("--keep-builds", action="store_true", help="keep both public directories for manual diffing")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    root = Path(a.source).resolve()
    work = Path(a.output).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    pinned = find_pinned_versions(root)
    current = (a.current or detect_current(root, pinned) or "").lstrip("v")
    if not current:
        raise SystemExit("Cannot detect the current Hugo version; pass --current")
    releases = stable_releases()
    if a.target == "latest":
        target = max((r["tag_name"] for r in releases), key=vtuple).lstrip("v")
    else:
        target = a.target.lstrip("v")
    r = {"tool_version": __version__, "current": current, "target": target, "reasons": [],
         "pinned": pinned, "date": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    log(f"Current Hugo: v{current}  Target Hugo: v{target}")

    if vtuple(target) <= vtuple(current):
        r["result"] = "up-to-date"
        r["reasons"].append(f"v{current} is already at or above v{target}")
    else:
        r["notes"] = analyse_notes(releases, current, target, root)
        (work / "release-notes.md").write_text(r["notes"]["notes"], encoding="utf-8")
        extended = not a.no_extended
        h_old = install(current, work / "bin-current", extended)
        h_new = install(target, work / "bin-target", extended)
        args = shlex.split(a.build_args, posix=os.name != "nt")
        bo = build(h_old, root, work / "public-current", work, "current", args)
        bn = build(h_new, root, work / "public-target", work, "target", args)
        r["builds"] = {"current": {k: bo[k] for k in ("exit_code", "seconds")},
                       "target": {k: bn[k] for k in ("exit_code", "seconds")}}
        rows = []
        if bo["exit_code"] or bn["exit_code"]:
            r["result"] = "build-failed"
            if bo["exit_code"]:
                r["reasons"].append(f"The build with the current version v{current} failed (build-current.log)")
            if bn["exit_code"]:
                r["reasons"].append(f"The build with v{target} failed (build-target.log)")
        else:
            c = compare(work / "public-current", work / "public-target", [current, target], rows)
            r["comparison"] = c
            r["new_warnings"] = sorted(warnings(bn["log"]) - warnings(bo["log"]))
            for w in r["new_warnings"]:
                rows.append(("new_warning", "", w[:1500]))
            reasons = r["reasons"]
            if c["files_removed"] or c["files_added"]:
                reasons.append(f"Published files change: {c['files_removed']} removed, {c['files_added']} added")
            if c["seo_changed"]:
                reasons.append(f"{c['seo_changed']} pages change title, description, canonical, og:url, robots, "
                               f"hreflang or redirect")
            if c["sitemap_changes"]:
                reasons.append(f"The sitemap changes in {c['sitemap_changes']} URLs")
            if r["new_warnings"]:
                reasons.append(f"{len(r['new_warnings'])} new WARN/ERROR lines in the build log")
            if c["images_look_different"]:
                reasons.append(f"{c['images_look_different']} images look different "
                               f"(PSNR below {PSNR_THRESHOLD:.0f} dB or a new size)")
            if c["html_pages"] and c["html_changed"] / c["html_pages"] > a.max_html_changed:
                reasons.append(f"{c['html_changed']} of {c['html_pages']} HTML pages have different content")
            if c["other_changed"]:
                reasons.append("Other files change: " +
                               ", ".join(f"{k} ({v})" for k, v in sorted(c["other_changed"].items())))
            if r["notes"]["deprecated_in_use"]:
                reasons.append(f"{len(r['notes']['deprecated_in_use'])} identifiers marked as deprecated or "
                               f"removed in the release notes appear in the project")
            r["result"] = "differences" if reasons else "identical"
            if not reasons:
                reasons.append("The published site is identical apart from build noise")
        with open(work / "differences.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows([("type", "path", "detail")] + rows)
        if not a.keep_builds:
            for d in ("public-current", "public-target", "bin-current", "bin-target", "cache-current",
                      "cache-target", "resources-current", "resources-target"):
                shutil.rmtree(work / d, ignore_errors=True)

    report = markdown(r)
    (work / "report.md").write_text(report, encoding="utf-8")
    slim = {k: v for k, v in r.items()}
    if "notes" in slim:
        slim["notes"] = {k: v for k, v in slim["notes"].items() if k != "notes"}
    (work / "report.json").write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(report)
    github_output(result=r["result"], current=current, target=target,
                  update_available=str(vtuple(target) > vtuple(current)).lower(), report=str(work / "report.md"))
    log("")
    log(report)
    if a.fail_on == "failure" and r["result"] == "build-failed":
        return 1
    if a.fail_on == "differences" and r["result"] in ("build-failed", "differences"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
