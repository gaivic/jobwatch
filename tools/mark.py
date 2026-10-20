#!/usr/bin/env python3
"""Update job status in status.json via the GitHub REST API.

A CLI tool for external automations (auto-apply bots, email watchers) to mark jobs
without needing a local checkout. Works via the GitHub Contents API with conflict
retry logic to coexist with the tracker page.

Environment variables:
    JOBWATCH_GH_TOKEN   GitHub fine-grained token with Contents: Read and write
    JOBWATCH_REPO       Repository as owner/repo (default: gaivic/jobwatch)

Stage names the tracker understands (in progression order):
    seen, queued, applied, oa, interview, offer, rejected, dropped

    The 'queued' stage represents jobs queued for application (user clicked Apply
    in the Daily view). An auto-apply bot polls for queued jobs, submits applications,
    then marks them 'applied' (or leaves a note on failure).

Examples:
    # Mark a job as applied by company + title
    python3 tools/mark.py applied --company "TikTok" --title "Software Engineer"

    # Mark with URL for disambiguation
    python3 tools/mark.py applied --company "Amazon" --title "SDE" --url "https://amazon.jobs/..."

    # Mark by exact job ID
    python3 tools/mark.py oa --id "simplify:abc123-def456"

    # Add a new manual job and mark it applied (job not in roles.json)
    python3 tools/mark.py applied --company "Startup Inc" --title "Engineer" --add-manual

    # Find/list matching jobs without marking
    python3 tools/mark.py find --company "TikTok"

    # List all jobs in 'queued' stage (for auto-apply bot polling)
    python3 tools/mark.py list --stage queued

    # Dry run (show what would happen)
    python3 tools/mark.py applied --company "TikTok" --title "SDE" --dry-run

    # Force overwrite even if moving backward
    python3 tools/mark.py seen --id "simplify:abc" --force
"""

import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API = "https://api.github.com"
DEFAULT_REPO = "gaivic/jobwatch"

STAGES = ("todo", "seen", "queued", "applied", "oa", "interview", "offer", "rejected", "dropped")
STORED_STAGES = frozenset(STAGES[1:])  # "todo" is never stored
STAGE_RANK = {"offer": 7, "interview": 6, "oa": 5, "applied": 4, "queued": 3.5, "rejected": 3, "seen": 2, "dropped": 1, "todo": 0}


def norm(s: str) -> str:
    """Normalise a name for matching (same as lib.js and jobwatch.py)."""
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def gh_request(method: str, url: str, token: str, body: Any = None) -> tuple[int, dict, bytes]:
    """Make a GitHub API request. Returns (status, headers_dict, body_bytes)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "jobwatch-mark/1.0",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_file(repo: str, token: str, path: str, ref: str | None = None) -> tuple[Any, str | None]:
    """Get a JSON file from the repo. Returns (parsed_json, sha) or (None, None) if 404."""
    url = f"{API}/repos/{repo}/contents/{path}"
    if ref:
        url += f"?ref={ref}"
    status, _, body = gh_request("GET", url, token)
    if status == 404:
        return None, None
    if status != 200:
        raise RuntimeError(f"GET {path} failed: HTTP {status}")
    meta = json.loads(body)
    content = base64.b64decode(meta["content"]).decode()
    return json.loads(content), meta["sha"]


def put_file(repo: str, token: str, path: str, content: Any, sha: str | None, message: str) -> str:
    """Write a JSON file to the repo. Returns the new sha."""
    url = f"{API}/repos/{repo}/contents/{path}"
    encoded = base64.b64encode((json.dumps(content, indent=1, sort_keys=True) + "\n").encode()).decode()
    body = {"message": message, "content": encoded}
    if sha:
        body["sha"] = sha
    status, _, resp = gh_request("PUT", url, token, body)
    if status not in (200, 201):
        err_msg = json.loads(resp).get("message", "") if resp else ""
        raise RuntimeError(f"PUT {path} failed: HTTP {status} {err_msg}")
    return json.loads(resp)["content"]["sha"]


def load_roles(repo: str, token: str) -> list[dict]:
    """Load roles.json and manual.json, merging them."""
    roles_data, _ = get_file(repo, token, "roles.json")
    manual_data, _ = get_file(repo, token, "manual.json")
    
    roles = roles_data if isinstance(roles_data, list) else []
    manual = manual_data if isinstance(manual_data, list) else []
    
    seen_ids = {r.get("id") for r in roles if r.get("id")}
    merged = list(roles)
    for m in manual:
        if m.get("id") and m.get("id") not in seen_ids:
            merged.append(m)
    return merged


def find_jobs(roles: list[dict], company: str | None, title: str | None, url: str | None) -> list[dict]:
    """Find jobs matching the given criteria. Returns matching roles."""
    matches = []
    company_norm = norm(company) if company else None
    title_norm = norm(title) if title else None
    
    for role in roles:
        if not role.get("id") or not role.get("company"):
            continue
        
        if company_norm and company_norm not in norm(role.get("company", "")):
            continue
        
        if title_norm:
            role_title_norm = norm(role.get("title", ""))
            if title_norm not in role_title_norm and role_title_norm not in title_norm:
                continue
        
        if url:
            role_url = role.get("url", "")
            if url not in role_url and role_url not in url:
                continue
        
        matches.append(role)
    
    return matches


def filter_active(roles: list[dict]) -> list[dict]:
    """Filter to only active/open jobs."""
    return [r for r in roles if r.get("active") is not False]


def current_stage(status: dict, job_id: str) -> str:
    """Get the current stage for a job from status.json."""
    entry = status.get(job_id)
    if entry is None:
        return "todo"
    if isinstance(entry, str):
        return entry if entry in STORED_STAGES else "todo"
    if isinstance(entry, dict):
        s = entry.get("s")
        return s if s in STORED_STAGES else "todo"
    return "todo"


def is_backward(from_stage: str, to_stage: str) -> bool:
    """Check if moving from from_stage to to_stage is backward."""
    return STAGE_RANK.get(to_stage, 0) < STAGE_RANK.get(from_stage, 0)


def build_entry(prev_entry: dict | str | None, new_stage: str, now_ts: int) -> dict | None:
    """Build a new status.json entry with proper history tracking."""
    if new_stage == "todo":
        if prev_entry is None:
            return None
        if isinstance(prev_entry, dict):
            if prev_entry.get("star") or prev_entry.get("note"):
                out = {}
                if prev_entry.get("star"):
                    out["star"] = True
                if prev_entry.get("note"):
                    out["note"] = prev_entry["note"]
                return out
        return None  # tombstone case is handled by caller
    
    prev_s = None
    prev_h = []
    prev_star = False
    prev_note = None
    prev_t = 0
    
    if isinstance(prev_entry, str):
        prev_s = prev_entry if prev_entry in STORED_STAGES else None
    elif isinstance(prev_entry, dict):
        prev_s = prev_entry.get("s") if prev_entry.get("s") in STORED_STAGES else None
        prev_h = prev_entry.get("h", [])
        prev_star = prev_entry.get("star", False)
        prev_note = prev_entry.get("note")
        prev_t = prev_entry.get("t", 0) or 0
    
    t = max(now_ts, prev_t + 1) if prev_t >= now_ts else now_ts
    
    entry: dict = {"s": new_stage, "t": t}
    
    if prev_star:
        entry["star"] = True
    if prev_note:
        entry["note"] = prev_note
    
    if prev_s != new_stage:
        h = list(prev_h) if prev_h else []
        if prev_s and not prev_h:
            h.append({"s": prev_s, "t": None})
        h.append({"s": new_stage, "t": t})
        entry["h"] = h[-12:]
    elif prev_h:
        entry["h"] = prev_h[-12:]
    
    return entry


def gen_manual_id() -> str:
    """Generate a manual job ID like the tracker does."""
    now = int(time.time())
    rand = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=8))
    return f"manual:{base36(now)}{rand}"


def base36(n: int) -> str:
    """Convert integer to base36 string."""
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = []
    while n:
        result.append(digits[n % 36])
        n //= 36
    return "".join(reversed(result))


def create_manual_role(company: str, title: str, url: str | None, location: str | None, category: str | None) -> dict:
    """Create a manual.json role entry."""
    now = int(time.time())
    role = {
        "id": gen_manual_id(),
        "company": company,
        "title": title,
        "url": url or "",
        "location": location or "location N/A",
        "category": category or "SWE",
        "posted": "added by you",
        "date_posted": now,
        "active": True,
        "manual": True,
        "added": now,
    }
    return role


def update_status_with_retry(
    repo: str, token: str, job_id: str, new_stage: str, force: bool, dry_run: bool, max_retries: int = 5
) -> dict:
    """Update status.json with conflict retry. Returns result dict."""
    for attempt in range(max_retries):
        status_data, sha = get_file(repo, token, "status.json")
        status = status_data if isinstance(status_data, dict) else {}
        
        cur = current_stage(status, job_id)
        if is_backward(cur, new_stage) and not force:
            return {
                "success": False,
                "error": f"Refusing to move from '{cur}' to '{new_stage}' (backward). Use --force to override.",
                "current_stage": cur,
                "requested_stage": new_stage,
            }
        
        now_ts = int(time.time())
        new_entry = build_entry(status.get(job_id), new_stage, now_ts)
        
        if cur == new_stage and status.get(job_id) == new_entry:
            return {
                "success": True,
                "action": "no_change",
                "job_id": job_id,
                "stage": new_stage,
            }
        
        if new_entry is None:
            if job_id in status:
                del status[job_id]
        else:
            status[job_id] = new_entry
        
        if dry_run:
            return {
                "success": True,
                "action": "dry_run",
                "job_id": job_id,
                "previous_stage": cur,
                "new_stage": new_stage,
                "entry": new_entry,
            }
        
        try:
            new_sha = put_file(repo, token, "status.json", status, sha, f"jobwatch: mark {job_id} as {new_stage}")
            return {
                "success": True,
                "action": "updated",
                "job_id": job_id,
                "previous_stage": cur,
                "new_stage": new_stage,
                "sha": new_sha,
            }
        except RuntimeError as e:
            if "409" in str(e) or "422" in str(e):
                wait = (2 ** attempt) + random.random()
                time.sleep(wait)
                continue
            raise
    
    return {
        "success": False,
        "error": f"Failed after {max_retries} conflict retries",
        "job_id": job_id,
    }


def add_manual_role_with_retry(
    repo: str, token: str, role: dict, dry_run: bool, max_retries: int = 5
) -> dict:
    """Add a role to manual.json with conflict retry."""
    for attempt in range(max_retries):
        manual_data, sha = get_file(repo, token, "manual.json")
        manual = manual_data if isinstance(manual_data, list) else []
        
        existing = [r for r in manual if r.get("company") == role["company"] and r.get("title") == role["title"]]
        if existing:
            return {
                "success": True,
                "action": "existing",
                "role": existing[0],
            }
        
        manual.append(role)
        
        if dry_run:
            return {
                "success": True,
                "action": "dry_run",
                "role": role,
            }
        
        try:
            put_file(repo, token, "manual.json", manual, sha, f"jobwatch: add manual role {role['company']} - {role['title']}")
            return {
                "success": True,
                "action": "added",
                "role": role,
            }
        except RuntimeError as e:
            if "409" in str(e) or "422" in str(e):
                wait = (2 ** attempt) + random.random()
                time.sleep(wait)
                continue
            raise
    
    return {
        "success": False,
        "error": f"Failed after {max_retries} conflict retries",
    }


def cmd_find(args: argparse.Namespace) -> int:
    """Handle the find/list subcommand."""
    token = os.environ.get("JOBWATCH_GH_TOKEN")
    repo = args.repo or os.environ.get("JOBWATCH_REPO", DEFAULT_REPO)
    
    if not token:
        print(json.dumps({"success": False, "error": "JOBWATCH_GH_TOKEN not set"}))
        return 1
    
    try:
        roles = load_roles(repo, token)
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 1
    
    matches = find_jobs(roles, args.company, args.title, args.url)
    
    if args.active_only:
        matches = filter_active(matches)
    
    status_data, _ = get_file(repo, token, "status.json")
    status = status_data if isinstance(status_data, dict) else {}
    
    results = []
    for role in matches:
        job_id = role.get("id")
        stage = current_stage(status, job_id) if job_id else "todo"
        
        if args.stage and stage != args.stage:
            continue
        
        results.append({
            "id": job_id,
            "company": role.get("company"),
            "title": role.get("title"),
            "url": role.get("url"),
            "location": role.get("location"),
            "active": role.get("active", True),
            "manual": role.get("manual", False),
            "current_stage": stage,
        })
    
    output = {
        "success": True,
        "count": len(results),
        "matches": results,
    }
    print(json.dumps(output, indent=2))
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    """Handle the mark subcommand."""
    token = os.environ.get("JOBWATCH_GH_TOKEN")
    repo = args.repo or os.environ.get("JOBWATCH_REPO", DEFAULT_REPO)
    
    if not token:
        print(json.dumps({"success": False, "error": "JOBWATCH_GH_TOKEN not set"}))
        return 1
    
    stage = args.stage
    if stage not in STORED_STAGES:
        print(json.dumps({"success": False, "error": f"Invalid stage: {stage}. Valid: {', '.join(STORED_STAGES)}"}))
        return 1
    
    job_id = args.id
    
    if not job_id:
        if not args.company:
            print(json.dumps({"success": False, "error": "Either --id or --company is required"}))
            return 1
        
        try:
            roles = load_roles(repo, token)
        except Exception as e:
            print(json.dumps({"success": False, "error": str(e)}))
            return 1
        
        matches = find_jobs(roles, args.company, args.title, args.url)
        active_matches = filter_active(matches)
        
        if len(active_matches) == 0:
            if args.add_manual:
                if not args.title:
                    print(json.dumps({"success": False, "error": "--title required with --add-manual"}))
                    return 1
                
                role = create_manual_role(args.company, args.title, args.url, args.location, args.category)
                add_result = add_manual_role_with_retry(repo, token, role, args.dry_run)
                
                if not add_result["success"]:
                    print(json.dumps(add_result))
                    return 1
                
                job_id = add_result["role"]["id"]
            else:
                if matches:
                    closed_info = [{"id": m.get("id"), "company": m.get("company"), "title": m.get("title"), "active": False} for m in matches[:5]]
                    print(json.dumps({
                        "success": False,
                        "error": f"No active jobs match. Found {len(matches)} closed/inactive. Use --add-manual to create a new entry.",
                        "closed_matches": closed_info,
                    }))
                else:
                    print(json.dumps({
                        "success": False,
                        "error": f"No jobs match company='{args.company}' title='{args.title or 'any'}'. Use --add-manual to create a new entry.",
                    }))
                return 1
        
        elif len(active_matches) == 1:
            job_id = active_matches[0]["id"]
        
        else:
            candidates = [{"id": m.get("id"), "company": m.get("company"), "title": m.get("title"), "url": m.get("url")} for m in active_matches[:10]]
            print(json.dumps({
                "success": False,
                "error": f"Multiple matches ({len(active_matches)}). Use --id or add --title/--url to disambiguate.",
                "candidates": candidates,
            }))
            return 1
    
    result = update_status_with_retry(repo, token, job_id, stage, args.force, args.dry_run)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Update job status via GitHub API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Stage names (in progression order):
  seen      - Marked as seen/reviewed
  queued    - Queued for application (from Daily view's Apply button)
  applied   - Application submitted
  oa        - Online assessment received
  interview - Interviewing
  offer     - Offer received
  rejected  - Rejected
  dropped   - Dismissed/not interested

The tool refuses to move a job backward (e.g., from 'interview' to 'applied')
unless --force is specified. Moving from 'queued' to 'applied' is forward progress.
""",
    )
    parser.add_argument("--repo", help=f"Repository as owner/repo (default: $JOBWATCH_REPO or {DEFAULT_REPO})")
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    mark_parser = subparsers.add_parser("mark", help="Mark a job with a stage (default command)")
    mark_parser.add_argument("stage", choices=list(STORED_STAGES), help="Stage to set")
    mark_parser.add_argument("--id", help="Exact job ID (e.g., simplify:abc123 or manual:xyz)")
    mark_parser.add_argument("--company", "-c", help="Company name (partial match)")
    mark_parser.add_argument("--title", "-t", help="Job title (partial match)")
    mark_parser.add_argument("--url", "-u", help="Job URL (partial match)")
    mark_parser.add_argument("--force", "-f", action="store_true", help="Force even if moving backward")
    mark_parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would happen without writing")
    mark_parser.add_argument("--add-manual", "-a", action="store_true", help="Add to manual.json if no match found")
    mark_parser.add_argument("--location", help="Location for --add-manual")
    mark_parser.add_argument("--category", choices=["SWE", "ML", "Quant", "HW"], default="SWE", help="Category for --add-manual")
    
    find_parser = subparsers.add_parser("find", aliases=["list"], help="Find/list jobs matching criteria")
    find_parser.add_argument("--company", "-c", help="Company name (partial match)")
    find_parser.add_argument("--title", "-t", help="Job title (partial match)")
    find_parser.add_argument("--url", "-u", help="Job URL (partial match)")
    find_parser.add_argument("--active-only", action="store_true", help="Only show active/open jobs")
    find_parser.add_argument("--stage", "-s", choices=list(STAGES), help="Filter by current stage (e.g., queued, applied)")
    
    for s in STORED_STAGES:
        stage_parser = subparsers.add_parser(s, help=f"Mark job as {s}")
        stage_parser.add_argument("--id", help="Exact job ID")
        stage_parser.add_argument("--company", "-c", help="Company name (partial match)")
        stage_parser.add_argument("--title", "-t", help="Job title (partial match)")
        stage_parser.add_argument("--url", "-u", help="Job URL (partial match)")
        stage_parser.add_argument("--force", "-f", action="store_true", help="Force even if moving backward")
        stage_parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would happen")
        stage_parser.add_argument("--add-manual", "-a", action="store_true", help="Add to manual.json if no match")
        stage_parser.add_argument("--location", help="Location for --add-manual")
        stage_parser.add_argument("--category", choices=["SWE", "ML", "Quant", "HW"], default="SWE", help="Category for --add-manual")
    
    args = parser.parse_args(argv)
    
    if args.command is None:
        parser.print_help()
        return 0
    
    if args.command in ("find", "list"):
        return cmd_find(args)
    
    if args.command in STORED_STAGES:
        args.stage = args.command
        return cmd_mark(args)
    
    if args.command == "mark":
        return cmd_mark(args)
    
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
