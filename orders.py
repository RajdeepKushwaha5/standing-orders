#!/usr/bin/env python3
"""standing-orders: what is your agent being told that nobody reviewed?

    orders.py discover <root> [include_global]
    orders.py inspect  <root> <discover-json>

Instruction files are standing orders: an agent loads them every session without being
asked. They are written once, reviewed once if at all, and then never appear in a diff
again. This reports facts about them and never claims intent.

Pass root=demo for the bundled corpus, so it runs with nothing set up.
Reads files only. Never executes anything, never writes, no credentials, no network.
"""
import json, os, subprocess, sys

sys.dont_write_bytecode = True

# Files an agent loads as instructions. A nested one is loaded too, which is the part
# people forget, so the walk does not stop at the repository root.
NAMES = {
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "CONVENTIONS.md",
    ".cursorrules", ".clinerules", ".windsurfrules", ".aiderrules",
    "copilot-instructions.md",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
             ".mypy_cache", ".pytest_cache", "dist", "build", ".next", "target",
             "vendor", "Pods", "site-packages", ".gradle", ".m2", ".cargo", ".npm",
             ".pnpm-store", ".terraform", ".turbo", ".parcel-cache", "coverage"}

# There is no depth limit by default. Measured on a 28,482 directory drive the walk took
# 155 seconds, which killed the step at its old 120 second timeout; the timeout was
# raised rather than the tree cut short, because a limit is a blind spot and a scan that
# quietly stops descending is the failure this play exists to report. max_depth is
# available for anyone who wants the trade, and the report always states what was
# skipped when they take it.
NO_DEPTH_LIMIT = 0

# Trend Micro, Invisible Prompt Injection: the Unicode tag block. Text is hidden by
# adding 0xE0000 to each ASCII code point, so a reader sees nothing and a tokenizer sees
# words. That also means it decodes straight back, which is why this can print it.
TAG_LO, TAG_HI = 0xE0000, 0xE007F
ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E}
BIDI = {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}
SOFT_HYPHEN = 0x00AD
VARIATION = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


# Three outcomes, and folding them together is what made a broken git read as a clean
# report: git answered, git answered no, git never answered at all.
GIT_OK, GIT_NO, GIT_DOWN = "ok", "no", "down"


def run_status(args):
    """stdout and which of the three outcomes produced it.

    Only git saying "not a git repository" is an answer about repositories. A missing
    binary, a timeout or an unexpected exit code is this play failing to look, and
    reporting that as "not in a repository" tells you nothing is wrong when nothing was
    checked."""
    try:
        p = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=20)
    except Exception:
        return None, GIT_DOWN
    if p.returncode == 0:
        return p.stdout, GIT_OK
    if "not a git repository" in (p.stderr or "").lower():
        return None, GIT_NO
    return None, GIT_DOWN


def run(args):
    """stdout when the command succeeded. Callers that must tell a real answer from a
    failure to look use run_status instead."""
    return run_status(args)[0]


def code(args):
    try:
        return subprocess.run(["git"] + args, capture_output=True, text=True,
                              timeout=20).returncode
    except Exception:
        return None


def classify(cp, prev):
    """Name what a character is. Presence is a fact; intent is not, so nothing here
    decides that a file is malicious."""
    if TAG_LO <= cp <= TAG_HI:
        return "unicode-tag"
    if cp in ZERO_WIDTH:
        return "zero-width"
    if cp in BIDI:
        return "bidi-control"
    if cp == SOFT_HYPHEN:
        return "soft-hyphen"
    for lo, hi in VARIATION:
        if lo <= cp <= hi:
            # A variation selector after a symbol is emoji presentation, which is all the
            # warning signs in the CLAUDE.md files on this machine. Reporting those made
            # the first run of this play 100 percent false positives. It is only worth
            # naming when it sits inside a word.
            if prev and (prev.isalnum() or prev == "_"):
                return "variation-selector-in-word"
            return None
    return None


def decode_tags(text):
    """Tag characters carry their plaintext. Subtracting the offset gives it back."""
    out = []
    for ch in text:
        cp = ord(ch)
        if TAG_LO <= cp <= TAG_HI:
            out.append(chr(cp - TAG_LO))
    s = "".join(out)
    return s if s.strip() else ""


def review_status(path):
    """Has a human ever seen this content in a diff? Tracked and unmodified means the
    committed bytes were reviewable. Untracked means nothing ever showed them."""
    d = os.path.dirname(path) or "."
    top, how = run_status(["-C", d, "rev-parse", "--show-toplevel"])
    if how == GIT_DOWN:
        # Not an answer. Saying NOT_IN_GIT_REPO here would report "not a claim that
        # anything is wrong" about a check that never ran.
        return {"state": "GIT_UNAVAILABLE", "repo": None}
    if top is None:
        return {"state": "NOT_IN_GIT_REPO", "repo": None}
    top = top.strip()
    rel = os.path.relpath(path, top)
    tracked = code(["-C", top, "ls-files", "--error-unmatch", "--", rel])
    if tracked is None:
        # git worked a moment ago and does not now; that is not evidence of anything
        return {"state": "GIT_UNAVAILABLE", "repo": top}
    if tracked != 0:
        # Not tracked is two different facts. A file nobody added yet is an oversight.
        # A file an ignore rule excludes will never be reviewed by design, and a
        # teammate who clones the repository will not have it at all, so their agent is
        # working from different instructions than yours. The rule is cited either way.
        why = run(["-C", top, "check-ignore", "-v", "--", rel])
        if why:
            first = why.strip().splitlines()[0] if why.strip() else ""
            rule = first.split("	")[0] if first else ""
            return {"state": "EXCLUDED_BY_RULE", "repo": top, "rule": rule}
        return {"state": "UNTRACKED", "repo": top}
    if code(["-C", top, "diff", "--quiet", "HEAD", "--", rel]) != 0:
        # A checkout on a Windows drive differs from its commit by a carriage return on
        # every line. That is not a changed instruction, and reporting 16 of them as
        # unreviewed edits was the first false positive this play produced. Only a
        # difference that survives ignoring end-of-line bytes is a changed instruction.
        eol_only = code(["-C", top, "diff", "--quiet", "--ignore-cr-at-eol",
                         "HEAD", "--", rel])
        if eol_only == 0:
            return {"state": "TRACKED", "repo": top, "note": "differs only by line endings"}
        if eol_only is None or eol_only > 1:
            return {"state": "MODIFIED_SINCE_COMMIT", "repo": top,
                    "note": "line-ending comparison unavailable"}
        return {"state": "MODIFIED_SINCE_COMMIT", "repo": top}
    return {"state": "TRACKED", "repo": top}


def resolve_root(value):
    if value != "demo":
        return value
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo", "project")


def global_candidates():
    """Instruction files that apply everywhere, which no repository diff can show."""
    home = os.path.expanduser("~")
    return [os.path.join(home, ".claude", "CLAUDE.md"),
            os.path.join(home, ".codex", "AGENTS.md"),
            os.path.join(home, ".config", "aider", "CONVENTIONS.md")]


def discover(root, include_global, max_depth=NO_DEPTH_LIMIT):
    found, unreadable = [], []
    not_descended = 0

    def note(e):
        unreadable.append({"path": str(getattr(e, "filename", "?")),
                           "reason": type(e).__name__})

    for dirpath, dirnames, files in os.walk(root, onerror=note):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if max_depth and depth >= max_depth:
            not_descended += len(dirnames)
            dirnames[:] = []
        for fn in sorted(files):
            if fn not in NAMES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                size = os.path.getsize(full)
            except OSError as e:
                unreadable.append({"path": rel, "reason": type(e).__name__})
                continue
            found.append({
                "display": rel,
                "scope": "root" if os.path.dirname(rel) in ("", ".") else "nested",
                "bytes": size,
                "git": review_status(full),
            })
    if include_global:
        for g in global_candidates():
            if os.path.isfile(g):
                found.append({"display": g, "scope": "global",
                              "bytes": os.path.getsize(g),
                              "git": review_status(g)})
    return {"root": root, "files": found, "unreadable": unreadable,
            "max_depth": max_depth, "not_descended": not_descended}


def inspect(payload):
    rows = []
    unreadable = list(payload.get("unreadable", []))
    base = payload.get("root_path") or payload.get("root") or ""
    for f in payload.get("files", []):
        # the absolute path is derivable, so it is rebuilt here rather than
        # carried on every record across the step boundary
        full = f["display"] if os.path.isabs(f["display"]) else os.path.join(base, f["display"])
        try:
            text = open(full, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append({"path": f["display"], "reason": type(e).__name__})
            continue
        buckets, samples = {}, []
        prev = ""
        line = 1
        for ch in text:
            cp = ord(ch)
            kind = classify(cp, prev)
            if ch == "\n":
                line += 1
            prev = ch
            if not kind:
                continue
            buckets[kind] = buckets.get(kind, 0) + 1
            if len(samples) < 5:
                samples.append({"kind": kind, "line": line,
                                "codepoint": "U+%04X" % cp})
        decoded = decode_tags(text)
        row = dict(f)
        row["invisible"] = buckets
        row["samples"] = samples
        row["decoded"] = decoded[:400]
        row["decoded_truncated"] = len(decoded) > 400
        state = f["git"]["state"]
        if decoded:
            row["verdict"] = "HIDDEN_TEXT_DECODES"
        elif buckets:
            row["verdict"] = "INVISIBLE_CHARACTERS"
        elif state == "EXCLUDED_BY_RULE":
            row["verdict"] = "EXCLUDED_FROM_REVIEW"
        elif state == "UNTRACKED":
            row["verdict"] = "NEVER_IN_A_DIFF"
        elif state == "MODIFIED_SINCE_COMMIT":
            row["verdict"] = "DIFFERS_FROM_COMMITTED"
        elif state == "NOT_IN_GIT_REPO":
            row["verdict"] = "NO_REPOSITORY_TO_REVIEW_AGAINST"
        elif state == "GIT_UNAVAILABLE":
            row["verdict"] = "REVIEW_STATE_UNKNOWN"
        else:
            row["verdict"] = "READ_AND_CLEAN"
        rows.append(row)
    return rows, unreadable


ORDER = ["HIDDEN_TEXT_DECODES", "INVISIBLE_CHARACTERS", "EXCLUDED_FROM_REVIEW",
         "NEVER_IN_A_DIFF", "DIFFERS_FROM_COMMITTED", "REVIEW_STATE_UNKNOWN",
         "NO_REPOSITORY_TO_REVIEW_AGAINST", "READ_AND_CLEAN"]


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: orders.py discover|inspect <root> [json]\n")
        sys.exit(2)
    mode = sys.argv[1]
    raw = sys.argv[2]
    root = resolve_root(raw)
    if raw != "demo" and not os.path.isabs(raw):
        sys.stderr.write(
            "orders.py: root must be an ABSOLUTE path, got: " + raw + "\n"
            "a step runs in rote's own workspace, not the directory you were standing\n"
            "in, so a relative path would scan the wrong tree.\n")
        sys.exit(2)
    if not os.path.isdir(root):
        sys.stderr.write("orders.py: no such directory: " + root + "\n")
        sys.exit(1)

    if mode == "discover":
        flag = sys.argv[3].strip().lower() if len(sys.argv) > 3 else ""
        include_global = raw != "demo" and flag in ("1", "true", "yes")
        depth = NO_DEPTH_LIMIT
        if len(sys.argv) > 4 and sys.argv[4].strip().isdigit():
            depth = max(0, min(64, int(sys.argv[4].strip())))
        data = discover(root, include_global, depth)
        # rote unpacks a play into a fresh temp directory, so the resolved demo path is
        # a run-specific string nobody can act on. Report what was asked for.
        data["root"] = "demo (bundled corpus)" if raw == "demo" else root
        data["root_path"] = root
        print(json.dumps(data, separators=(",", ":")))
        return

    payload = json.loads(sys.argv[3])
    rows, unreadable = inspect(payload)
    rows.sort(key=lambda r: (ORDER.index(r["verdict"]) if r["verdict"] in ORDER else 9,
                             r["display"]))
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    # Only files with something to report travel as records. A clean file is a
    # count, and the total stays exact, so a budget can never turn "ran out of
    # room" into "there was less to find". The cap on reported rows is stated
    # in the report rather than applied in silence.
    CAP = 200
    flagged = [r for r in rows if r["verdict"] != "READ_AND_CLEAN"]
    omitted = max(0, len(flagged) - CAP)
    flagged = flagged[:CAP]
    print(json.dumps({
        "root": payload.get("root"),
        "files_found": len(payload.get("files", [])),
        "counts": counts,
        "unreadable": unreadable,
        "rows_omitted": omitted,
        "max_depth": payload.get("max_depth"),
        "not_descended": payload.get("not_descended", 0),
        "orders": flagged,
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
