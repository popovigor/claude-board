#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude usage board — collector for an old-iPad dashboard.

Nothing runs permanently: Claude Code itself calls this script as its
status line (on every model reply and, with refreshInterval, every N seconds).
Each call:
  1. saves the JSON Claude Code passes on stdin into ~/.claude/usage-board/sessions/<id>.json
  2. rebuilds board.json (all recent sessions + account rate limits + cached Console cost)
  3. prints a one-line status line for the terminal
  4. at most once every PUSH_EVERY seconds, spawns a short-lived detached child
     (`board.py --push`) that uploads board.json to a secret GitHub Gist.
In the same push child, every LIMITS_EVERY seconds, it also reads per-model weekly
limits (e.g. Fable) from claude.ai's usage endpoint with the local OAuth login,
because Claude Code's status-line JSON only carries the 5-hour and 7-day windows.
The iPad opens a static page (index.html on GitHub Pages) that polls the Gist.

Commands:
  board.py              status-line mode (reads JSON on stdin)
  board.py --setup      create the Gist (needs github_token in config.json), print URLs
  board.py --install    add/replace the statusLine entry in ~/.claude/settings.json
  board.py --push       (internal) upload board.json now
  board.py --show       print the current board.json

Config: ~/.claude/usage-board/config.json
  {
    "github_token": "ghp_...",            # scope: gist
    "gist_id": "",                        # filled by --setup
    "gist_owner": "",                     # filled by --setup
    "pages_url": "https://USER.github.io/claude-board/",   # where index.html lives
    "anthropic_admin_key": ""             # optional: Console org Admin API key for $ costs
  }
"""
import json
import os
import sys
import time
import hashlib
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
DIR = Path(os.environ.get("CLAUDE_BOARD_DIR", str(HOME / ".claude" / "usage-board")))
SESS_DIR = DIR / "sessions"
CONFIG = DIR / "config.json"
BOARD = DIR / "board.json"
STATE = DIR / "state.json"
COST = DIR / "cost.json"
LIMITS = DIR / "limits.json"
CREDENTIALS = HOME / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
LOG = DIR / "push.log"

PUSH_EVERY = 20          # minimum seconds between Gist uploads
HEARTBEAT = 120          # upload even if nothing changed, at most this often
ACTIVE_WINDOW = 15 * 60  # a session is shown while updated within this window
KEEP = 24 * 3600         # session files older than this are deleted
COST_EVERY = 3600        # Console cost refresh interval
LIMITS_EVERY = 300       # per-model weekly limits (Fable) refresh interval
HTTP_TIMEOUT = 8

GIST_FILE = "board.json"
UA = "claude-usage-board/1.0"


# ----------------------------------------------------------------------------- utils

def now():
    return int(time.time())


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def log(msg):
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        if LOG.stat().st_size > 200_000:
            LOG.unlink()
    except Exception:
        pass


def http(method, url, headers=None, body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d or d[k] is None:
            return default
        d = d[k]
    return d


# ----------------------------------------------------------------------------- collect

def save_session(inp):
    sid = inp.get("session_id") or "unknown"
    cwd = get(inp, "workspace", "current_dir") or inp.get("cwd") or ""
    folder = os.path.basename(cwd.rstrip("/\\")) or cwd
    rec = {
        "id": sid,
        "name": inp.get("session_name") or folder or sid[:8],
        "folder": folder,
        "model": get(inp, "model", "display_name") or get(inp, "model", "id") or "?",
        "ctx_pct": get(inp, "context_window", "used_percentage"),
        "ctx_tokens": get(inp, "context_window", "total_input_tokens"),
        "ctx_size": get(inp, "context_window", "context_window_size"),
        "cost_usd": get(inp, "cost", "total_cost_usd", default=0) or 0,
        "duration_ms": get(inp, "cost", "total_duration_ms", default=0) or 0,
        "lines_added": get(inp, "cost", "total_lines_added", default=0) or 0,
        "lines_removed": get(inp, "cost", "total_lines_removed", default=0) or 0,
        "effort": get(inp, "effort", "level"),
        "fast": bool(inp.get("fast_mode")),
        "branch": get(inp, "worktree", "branch"),
        "updated": now(),
    }
    rl = inp.get("rate_limits")
    if isinstance(rl, dict) and rl:
        rec["rate_limits"] = {
            k: {"pct": get(rl, k, "used_percentage"), "resets_at": get(rl, k, "resets_at")}
            for k in ("five_hour", "seven_day")
            if isinstance(rl.get(k), dict)
        }
    safe = "".join(c for c in sid if c.isalnum() or c in "-_") or "unknown"
    write_json(SESS_DIR / (safe + ".json"), rec)
    return rec


def build_board():
    t = now()
    sessions = []
    rate_limits = None
    rl_time = 0
    today = datetime.now().date()
    cost_today = 0.0
    if SESS_DIR.exists():
        for p in SESS_DIR.glob("*.json"):
            rec = read_json(p, None)
            if not rec:
                continue
            age = t - rec.get("updated", 0)
            if age > KEEP:
                try:
                    p.unlink()
                except Exception:
                    pass
                continue
            if datetime.fromtimestamp(rec.get("updated", 0)).date() == today:
                cost_today += float(rec.get("cost_usd") or 0)
            if rec.get("rate_limits") and rec["updated"] > rl_time:
                rate_limits, rl_time = rec["rate_limits"], rec["updated"]
            if age <= ACTIVE_WINDOW:
                sessions.append(rec)
    sessions.sort(key=lambda r: r.get("updated", 0), reverse=True)
    board = {
        "generated": t,
        "sessions": sessions,
        "rate_limits": rate_limits,
        "rate_limits_at": rl_time or None,
        "api_cost_today_usd": round(cost_today, 4),
        "console_cost": read_json(COST, None),
        "model_limits": read_json(LIMITS, None),
    }
    write_json(BOARD, board)
    return board


def fmt_eta(resets_at):
    if not resets_at:
        return ""
    s = int(resets_at) - now()
    if s <= 0:
        return "now"
    h, m = divmod(s // 60, 60)
    if h >= 24:
        return "%dd%dh" % (h // 24, h % 24)
    return ("%dh%02dm" % (h, m)) if h else ("%dm" % m)


def status_line(rec, board):
    head = "[%s]" % rec["model"]
    if rec.get("ctx_pct") is not None:
        head += " ctx %d%%" % int(rec["ctx_pct"])
    parts = [head]
    rl = board.get("rate_limits") or {}
    lim = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(key)
        if w and w.get("pct") is not None:
            eta = fmt_eta(w.get("resets_at"))
            lim.append("%s %d%%%s" % (label, round(w["pct"]), (" ↻" + eta) if eta else ""))
    for m in get(board, "model_limits", "models", default=[]) or []:
        if m.get("pct") is not None:
            lim.append("%s %d%%" % (m.get("name") or "?", round(m["pct"])))
    if lim:
        parts.append(" · ".join(lim))
    if rec.get("cost_usd"):
        parts.append("$%.2f" % rec["cost_usd"])
    return " | ".join(parts)


def maybe_spawn_push(board):
    cfg = read_json(CONFIG, {})
    if not cfg.get("github_token") or not cfg.get("gist_id"):
        return "no-config"
    st = read_json(STATE, {})
    # digest ignores the timestamp, so an idle session doesn't create a Gist revision every 30 s;
    # a heartbeat push still goes out every HEARTBEAT seconds so the page can tell "idle" from "dead".
    content = dict(board, generated=0)
    digest = hashlib.sha1(json.dumps(content, sort_keys=True).encode()).hexdigest()
    t = now()
    since = t - st.get("last_push", 0)
    cost_due = (t - st.get("cost_fetched", 0)) >= COST_EVERY and bool(cfg.get("anthropic_admin_key"))
    cost_due = cost_due or limits_due(st, t)
    if since < PUSH_EVERY and not cost_due:
        return "throttled"
    if digest == st.get("last_digest") and since < HEARTBEAT and not cost_due:
        return "unchanged"
    st["last_push"] = t
    st["last_digest"] = digest
    write_json(STATE, st)
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--push"], **kwargs)
    except Exception as e:
        log("spawn failed: %s" % e)
        return "spawn-failed"
    return "pushing"


# ----------------------------------------------------------------------------- push

def gist_headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_console_cost(admin_key):
    """Month-to-date and today's cost from the Console Admin Analytics API (org accounts only)."""
    base = "https://api.anthropic.com/v1/organizations/analytics/cost"
    hdr = {
        "x-api-key": admin_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "analytics-usage-cost-2026-08-24",
    }
    utc = datetime.now(timezone.utc)
    month_start = utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = utc.replace(hour=0, minute=0, second=0, microsecond=0)

    def total(starting_at):
        params = "starting_at=%s&limit=1000" % starting_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        cents, page = 0.0, None
        while True:
            url = base + "?" + params + (("&page=" + page) if page else "")
            res = http("GET", url, headers=hdr)
            for row in res.get("data", []):
                cents += float(get(row, "cost", "amount", default="0") or 0)  # cents, decimal string
            if res.get("has_more") and res.get("next_page"):
                page = res["next_page"]
            else:
                break
        return round(cents / 100.0, 2)

    out = {"fetched_at": now(), "month_usd": None, "today_usd": None, "error": None}
    try:
        out["month_usd"] = total(month_start)
        out["today_usd"] = total(day_start)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            detail = ""
        out["error"] = "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def limits_due(st, t):
    return (t - st.get("limits_fetched", 0)) >= LIMITS_EVERY and CREDENTIALS.exists()


def iso_to_epoch(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def fetch_model_limits():
    """Per-model weekly windows (kind 'weekly_scoped', e.g. Fable) from claude.ai's usage endpoint.
    Uses the access token Claude Code keeps in ~/.claude/.credentials.json (read-only; Claude Code refreshes it)."""
    out = {"fetched_at": now(), "models": [], "error": None}
    try:
        token = get(read_json(CREDENTIALS, {}), "claudeAiOauth", "accessToken")
        if not token:
            out["error"] = "no Claude login found"
            return out
        res = http("GET", USAGE_URL, headers={"Authorization": "Bearer " + token,
                                             "anthropic-beta": "oauth-2025-04-20"})
        for row in res.get("limits") or []:
            name = get(row, "scope", "model", "display_name")
            if not isinstance(row, dict) or row.get("kind") != "weekly_scoped" or not name:
                continue
            out["models"].append({"name": name, "pct": row.get("percent"),
                                  "resets_at": iso_to_epoch(row.get("resets_at")),
                                  "severity": row.get("severity")})
    except urllib.error.HTTPError as e:
        out["error"] = "HTTP %s" % e.code
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def do_push():
    cfg = read_json(CONFIG, {})
    token, gist_id = cfg.get("github_token"), cfg.get("gist_id")
    if not token or not gist_id:
        log("push skipped: no github_token/gist_id")
        return 1
    st = read_json(STATE, {})
    if cfg.get("anthropic_admin_key") and now() - st.get("cost_fetched", 0) >= COST_EVERY:
        cost = fetch_console_cost(cfg["anthropic_admin_key"])
        write_json(COST, cost)
        st = read_json(STATE, {})
        st["cost_fetched"] = now()
        write_json(STATE, st)
        if cost.get("error"):
            log("console cost error: %s" % cost["error"])
    if limits_due(st, now()):
        prev = read_json(LIMITS, None)
        lim = fetch_model_limits()
        if lim.get("error"):
            log("model limits error: %s" % lim["error"])
            if prev and prev.get("models"):          # keep the last good numbers, flag the error
                lim["models"], lim["fetched_at"] = prev["models"], prev.get("fetched_at")
        write_json(LIMITS, lim)
        st = read_json(STATE, {})
        st["limits_fetched"] = now()
        write_json(STATE, st)
    board = build_board()
    try:
        http("PATCH", "https://api.github.com/gists/" + gist_id, headers=gist_headers(token),
             body={"files": {GIST_FILE: {"content": json.dumps(board, ensure_ascii=False)}}})
        st = read_json(STATE, {})
        st["last_ok"] = now()
        write_json(STATE, st)
        return 0
    except urllib.error.HTTPError as e:
        log("gist push failed: HTTP %s" % e.code)
    except Exception as e:
        log("gist push failed: %s" % e)
    return 1


# ----------------------------------------------------------------------------- setup

def do_setup():
    cfg = read_json(CONFIG, {})
    if not cfg.get("github_token"):
        print("Put a GitHub token with the 'gist' scope into %s first:\n"
              '  {"github_token": "ghp_..."}' % CONFIG)
        return 1
    if not cfg.get("gist_id"):
        res = http("POST", "https://api.github.com/gists", headers=gist_headers(cfg["github_token"]),
                   body={"description": "Claude usage board", "public": False,
                         "files": {GIST_FILE: {"content": json.dumps({"generated": now(), "sessions": []})}}})
        cfg["gist_id"] = res["id"]
        cfg["gist_owner"] = get(res, "owner", "login", default="")
        write_json(CONFIG, cfg)
        print("Gist created:", res.get("html_url"))
    raw = "https://gist.githubusercontent.com/%s/%s/raw/%s" % (cfg.get("gist_owner", ""), cfg["gist_id"], GIST_FILE)
    pages = (cfg.get("pages_url") or "https://%s.github.io/claude-board/" % cfg.get("gist_owner", "USER")).rstrip("/") + "/"
    print("\nData URL (raw gist):\n  " + raw)
    print("\nOpen this on the iPad and add it to the Home Screen:\n  %s?src=%s" % (pages, raw))
    print("\nStatus line config for ~/.claude/settings.json (or run: board.py --install):")
    print(json.dumps({"statusLine": {"type": "command", "command": statusline_command(), "refreshInterval": 30}}, indent=2))
    return 0


def statusline_command():
    me = os.path.abspath(__file__).replace("\\", "/")
    py = "python" if os.name == "nt" else "python3"
    return '%s "%s"' % (py, me)


def do_install():
    settings = HOME / ".claude" / "settings.json"
    data = read_json(settings, {})
    if settings.exists():
        backup = settings.with_suffix(".json.bak-%d" % now())
        backup.write_bytes(settings.read_bytes())
        print("Backup:", backup)
    data["statusLine"] = {"type": "command", "command": statusline_command(), "refreshInterval": 30}
    settings.parent.mkdir(parents=True, exist_ok=True)
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("statusLine installed into", settings)
    return 0


# ----------------------------------------------------------------------------- main

def main(argv):
    if "--push" in argv:
        return do_push()
    if "--setup" in argv:
        return do_setup()
    if "--install" in argv:
        return do_install()
    if "--show" in argv:
        print(json.dumps(build_board(), indent=2, ensure_ascii=False))
        return 0
    try:
        inp = json.load(sys.stdin)
    except Exception:
        print("[usage-board] no input")
        return 0
    rec = save_session(inp)
    board = build_board()
    print(status_line(rec, board))
    maybe_spawn_push(board)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
