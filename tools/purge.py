#!/usr/bin/env python3
"""Remove a label or product from a corpus on request, and check that it is gone from the history.

  python3 -I tools/purge.py add --corpus us|eu --id <setid|EMEA-H-C-nnnnnn> --reason <reason>
                                --requested-by "<company, or 'named individual'>" --request-date YYYY-MM-DD
                                [--note "..."] [--date YYYY-MM-DD]
  python3 -I tools/purge.py rebuild --corpus us|eu|all [--workers 4] [--us-out build/us] [--eu-out build/eu]
  python3 -I tools/purge.py check [--us-repo build/us] [--eu-repo build/eu]

add records the id in sources/removed.json (reasons: rights-holder-request, personal-data-request, other) and
flags it in molecules/*.json. Every build, append and daily run then leaves it out. rebuild runs the normal full
builds (tools/build.py build --clean, tools/eu.py build --clean) so the history is written again without it,
then the EU crosswalk, check, manifest.json and the README numbers; every commit after the removed label's first
version gets a new hash. The force-push and the tag prune are printed for the maintainer and never run here.
check exits 1 when a removed id is still anywhere in a corpus repo's history (any ref), in its tree at main, in a
Source-Id trailer (US) or a commit author (EU), or when a daily/* tag is not reachable from main (a tag left
over from the history before the rebuild).

The procedure is in reproduce.md, 'Removing a product on request'. Standard library only, no network.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import eu  # noqa: E402
import notices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
ABOUT = ("Labels and products removed from the corpora on request. Every build and append excludes the ids listed "
         "here, the daily job makes no request for them, and a full rebuild followed by a force-push replaces the "
         "published history (procedure: reproduce.md, 'Removing a product on request'). No personal data: a named "
         "individual is recorded as 'named individual' only.")


class PurgeError(RuntimeError):
    """A removal request cannot be recorded, or the rebuild did not complete."""


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def paths(root: Path) -> dict[str, Path]:
    return {"removed": root / "sources" / "removed.json", "us": root / "sources" / "us",
            "eu": root / "sources" / "eu", "molecules": root / "molecules"}


# ---------------------------------------------------------------- add

def normalise_id(corpus: str, ident: str) -> str:
    ident = ident.strip()
    if corpus == "us":
        return ident.lower()
    return eu.ema_dir(ident) if ident.upper().startswith("EMEA/H/C/") else ident


def write_removed(path: Path, entries: list[dict]) -> None:
    doc = {"about": ABOUT, "removed": sorted(entries, key=lambda e: (e["corpus"], e["id"]))}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def flag_molecules(corpus: str, ident: str, molecules_dir: Path) -> list[str]:
    """Mark the id in molecules/<inn>.json: status 'removed' on us[] entries, removed true on the eu[] reference.
    Returns the changed file names."""
    changed = []
    for path in sorted(p for p in molecules_dir.glob("*.json") if p.name != "selection.json"):
        raw = path.read_text(encoding="utf-8")
        mol = json.loads(raw)
        hit = False
        if corpus == "us":
            for entry in mol.get("us") or []:
                if isinstance(entry, dict) and str(entry.get("setid", "")).lower() == ident:
                    entry["status"] = "removed"
                    hit = True
        else:
            for entry in mol.get("eu") or []:
                if entry.get("label_id") == ident and entry.get("role") == "reference":
                    entry["removed"] = True
                    hit = True
        if hit:
            text = eu.molecule_text(mol)
            if text != raw:
                path.write_text(text, encoding="utf-8")
                changed.append(path.name)
    return changed


def add(root: Path, corpus: str, ident: str, reason: str, requested_by: str, request_date: str,
        note: str | None = None, removed_on: str | None = None) -> dict:
    """Record one removal in sources/removed.json and flag it in molecules/. Returns the new entry plus the
    changed molecule files."""
    p = paths(root)
    ident = normalise_id(corpus, ident)
    if not build.valid_removed_id(corpus, ident):
        raise PurgeError(f"{ident!r} is not a {corpus.upper()} id (US: a DailyMed setid; EU: EMEA-H-C-nnnnnn)")
    if not (p[corpus] / f"{ident}.json").exists():
        raise PurgeError(f"no pinned sources file sources/{corpus}/{ident}.json; check the id")
    entries = build.load_removed(p["removed"])
    if any(e["corpus"] == corpus and e["id"].lower() == ident.lower() for e in entries):
        raise PurgeError(f"{corpus} {ident} is already in sources/removed.json")
    entry = {"corpus": corpus, "id": ident, "reason": reason, "requested_by": requested_by.strip(),
             "request_date": request_date, "removed_on": removed_on or today()}
    if note:
        entry["note"] = note.strip()
    trial = p["removed"].with_suffix(".check")
    write_removed(trial, entries + [entry])
    try:
        build.load_removed(trial)                  # the same validation every build applies
    finally:
        trial.unlink(missing_ok=True)
    write_removed(p["removed"], entries + [entry])
    return {"entry": entry, "molecules_changed": flag_molecules(corpus, ident, p["molecules"])}


def next_steps(corpus: str) -> str:
    return "\n".join([
        f"acknowledge the request from {notices.CONTACT}, then:",
        f"  python3 -I tools/purge.py rebuild --corpus {corpus} --workers 4",
        "  python3 -I tools/purge.py check",
        "then follow reproduce.md, 'Removing a product on request' (force-push, tag prune, submodule, commit)."])


# ---------------------------------------------------------------- rebuild

def remotes(repo: Path) -> list[tuple[str, str]]:
    """(name, url) of the remotes of a corpus repo, so a clean rebuild (which deletes .git) can restore them."""
    if not (repo / ".git").exists():
        return []
    out = build.git(repo, "remote", "-v")
    found = {}
    for line in out.splitlines():
        name, url, kind = line.split()
        if kind == "(push)" or name not in found:
            found[name] = url
    return sorted(found.items())


def restore_remotes(repo: Path, saved: list[tuple[str, str]]) -> None:
    for name, url in saved:
        build.git(repo, "remote", "add", name, url)
        if name == "origin":
            build.git(repo, "config", "branch.main.remote", "origin")
            build.git(repo, "config", "branch.main.merge", "refs/heads/main")


def run_tool(args: list[str], log=print) -> None:
    cmd = [sys.executable, "-I", *args]
    log("$ " + " ".join(cmd[2:]), flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise PurgeError(f"{' '.join(args[:2])} failed with exit code {proc.returncode}")


def rebuild(corpus: str, workers: int, us_out: Path, eu_out: Path, build_date: str | None = None,
            log=print) -> dict:
    """Full rebuild of the corpus repos (the normal build commands), then crosswalk, check, manifest and README
    numbers. Returns the new HEADs and the wall time per corpus in seconds."""
    targets = ["us", "eu"] if corpus == "all" else [corpus]
    outs = {"us": us_out.resolve(), "eu": eu_out.resolve()}
    seconds, heads = {}, {}
    for name in targets:
        out = outs[name]
        saved = remotes(out)
        started = time.monotonic()
        tool = "build.py" if name == "us" else "eu.py"
        run_tool([str(TOOLS / tool), "build", "--clean", "--out", str(out), "--workers", str(workers)], log)
        seconds[name] = round(time.monotonic() - started, 1)
        restore_remotes(out, saved)
        heads[name] = build.git(out, "rev-parse", "main").strip()
        log(f"{name}: rebuilt in {seconds[name]} s; HEAD {heads[name]}"
            + (f"; remotes restored: {', '.join(n for n, _ in saved)}" if saved else ""), flush=True)
    if (outs["eu"] / ".git").exists():
        run_tool([str(TOOLS / "eu.py"), "crosswalk", "--eu-repo", str(outs["eu"])], log)
    failures = check(ROOT, outs["us"], outs["eu"])
    if failures:
        raise PurgeError("check failed after the rebuild:\n  " + "\n  ".join(failures))
    run_tool([str(TOOLS / "manifest.py"), "write", "--build-date", build_date or today(),
              "--us-repo", str(outs["us"]), "--eu-repo", str(outs["eu"])], log)
    run_tool([str(TOOLS / "readme_numbers.py"), "--write"], log)
    return {"heads": heads, "seconds": seconds}


def manual_steps(heads: dict[str, str], outs: dict[str, Path]) -> str:
    lines = ["Run by hand (this script never pushes):"]
    for name, head in heads.items():
        repo = outs[name]
        lines += [f"  git -C {repo} push --force origin main            # {name} HEAD {head[:12]}",
                  f"  git -C {repo} push origin --prune '+refs/tags/*:refs/tags/*'"]
    lines += ["  git submodule update --remote us eu                  # in the top repository (public copy)",
              "  python3 -I tools/visualise.py all --png",
              "  python3 -I tools/cli.py compare-check --date <record end> --info-date 2015-01-01 "
              "--out reports/compare-check.json",
              "  python3 -I tools/readme_numbers.py --write && python3 -I tools/readme_numbers.py --check",
              "  then commit sources/removed.json, molecules/, manifest.json, README.md, reports/, visualisations/",
              "  and ask GitHub Support to clear cached views of the removed files."]
    return "\n".join(lines)


# ---------------------------------------------------------------- check

def check(root: Path, us_repo: Path | None, eu_repo: Path | None) -> list[str]:
    """Failures (empty when every removed id is gone from the repos that exist)."""
    entries = build.load_removed(paths(root)["removed"])
    failures = []
    for corpus, repo in (("us", us_repo), ("eu", eu_repo)):
        if repo is None or not (repo / ".git").exists():
            continue
        ids = sorted({e["id"].lower() if corpus == "us" else e["id"] for e in entries if e["corpus"] == corpus})
        name = repo.name
        if ids:
            tree = build.git(repo, "ls-tree", "-r", "--name-only", "main").splitlines()
            if corpus == "us":
                trailer = build.git(repo, "log", "--all", "--format=%(trailers:key=Source-Id,valueonly,separator=)")
                sources = {t.strip().split(":", 1)[-1].lower() for t in trailer.splitlines() if t.strip()}
            else:
                authors = {a.split("@", 1)[0].split(".", 1)[0] for a in
                           build.git(repo, "log", "--all", "--format=%ae").split()}
            for ident in ids:
                if build.git(repo, "log", "--all", "--format=%H", "--", f"*/{ident}/*").strip():
                    failures.append(f"{name}: {ident} is still in the history (git log --all)")
                if any(f"/{ident}/" in p for p in tree):
                    failures.append(f"{name}: {ident} is still in the tree at main")
                if corpus == "us" and ident in sources:
                    failures.append(f"{name}: a commit still has Source-Id dailymed:{ident}")
                if corpus == "eu" and ident in authors:
                    failures.append(f"{name}: a commit is still authored as {ident}")
        for tag in build.git(repo, "tag", "--list", "daily/*").split():
            reachable = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tag, "main"],
                                       capture_output=True, env=build.git_env(repo)).returncode == 0
            if not reachable:
                failures.append(f"{name}: tag {tag} is not reachable from main (left over from the old history)")
    return failures


def daily_tags(repo: Path | None) -> int:
    if repo is None or not (repo / ".git").exists():
        return 0
    return len(build.git(repo, "tag", "--list", "daily/*").split())


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)   # tests: a copy of sources/ and molecules/
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="record a removal in sources/removed.json and flag it in molecules/")
    a.add_argument("--corpus", required=True, choices=build.REMOVED_CORPORA)
    a.add_argument("--id", required=True, help="US setid or EU product folder EMEA-H-C-nnnnnn")
    a.add_argument("--reason", required=True, choices=build.REMOVED_REASONS)
    a.add_argument("--requested-by", required=True, help="company name, or 'named individual' (no personal data)")
    a.add_argument("--request-date", required=True, help="YYYY-MM-DD the request arrived")
    a.add_argument("--note", help="optional; no personal data")
    a.add_argument("--date", help="removed_on (default: today, UTC)")
    r = sub.add_parser("rebuild", help="rebuild the corpus repos without the removed ids, then check and write "
                                       "manifest.json and the README numbers")
    r.add_argument("--corpus", required=True, choices=["us", "eu", "all"])
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--us-out", default=str(ROOT / "build" / "us"))
    r.add_argument("--eu-out", default=str(ROOT / "build" / "eu"))
    r.add_argument("--build-date", help="build_date for manifest.json (default: today, UTC)")
    c = sub.add_parser("check", help="exit 1 if a removed id is still in a corpus repo")
    c.add_argument("--us-repo", default=str(ROOT / "build" / "us"))
    c.add_argument("--eu-repo", default=str(ROOT / "build" / "eu"))
    args = p.parse_args(argv)
    root = Path(args.root)
    try:
        if args.cmd == "add":
            for day in (args.request_date, args.date):
                if day and not build.ISO_DAY.match(day):
                    raise PurgeError(f"{day!r} is not YYYY-MM-DD")
            res = add(root, args.corpus, args.id, args.reason, args.requested_by, args.request_date, args.note,
                      args.date)
            e = res["entry"]
            print(f"recorded {e['corpus']} {e['id']} ({e['reason']}, removed_on {e['removed_on']}) in "
                  f"sources/removed.json; molecules flagged: {', '.join(res['molecules_changed']) or 'none'}")
            print(next_steps(e["corpus"]))
            return 0
        if args.cmd == "rebuild":
            started = time.monotonic()
            outs = {"us": Path(args.us_out), "eu": Path(args.eu_out)}
            res = rebuild(args.corpus, args.workers, outs["us"], outs["eu"], args.build_date)
            print(f"rebuild done in {time.monotonic() - started:.1f} s; per corpus: "
                  + ", ".join(f"{k} {v} s" for k, v in res["seconds"].items()))
            for name, head in res["heads"].items():
                print(f"{name} HEAD {head}")
            print(manual_steps(res["heads"], {k: v.resolve() for k, v in outs.items()}))
            return 0
        us_repo, eu_repo = Path(args.us_repo), Path(args.eu_repo)
        failures = check(root, us_repo, eu_repo)
        for line in failures:
            print(f"FAIL {line}", file=sys.stderr)
        n = len(build.load_removed(paths(root)["removed"]))
        tags = daily_tags(us_repo) + daily_tags(eu_repo)
        print(f"{n} removed id(s) checked; {tags} daily/* tag(s); "
              f"{'ok' if not failures else f'{len(failures)} failure(s)'}")
        return 1 if failures else 0
    except (PurgeError, build.BuildError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
