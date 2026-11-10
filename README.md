# Claude Board

A wall dashboard for Claude Code subscription limits, made for an old iPad
(the first-generation iPad mini, Safari on iOS 9). It shows:

- the **5-hour** and **weekly (all models)** limit windows, with reset times
- the **Fable weekly** limit
- active Claude Code sessions: model, effort, cost, context use, and their running or recently finished subagents
- today's Claude Code spend at API list prices, plus Console spend if you add an Admin API key

Nothing runs in the background. Claude Code runs `board.py` as its status line
on every reply and every 30 s. Each run saves the session's data, rebuilds
`board.json` and, at most every 20 s, uploads it to a secret GitHub Gist.
The iPad opens `index.html` (served from GitHub Pages) and reads the Gist every 30 s.

```
Claude Code ──status line──▶ board.py ──▶ secret Gist (board.json) ◀──polls── iPad: index.html on GitHub Pages
                                │
                                └──every 10 min──▶ claude.ai usage endpoint (Fable weekly limit)
```

## Requirements

- Python 3.7+ (standard library only)
- Claude Code signed in with a Claude subscription (Pro/Max/Team)
- A GitHub account

## Setup

### 1. Publish the page

Fork or push this repo to GitHub, then turn on **Settings → Pages → Deploy from
a branch → `main` / root**. The page will be at
`https://USER.github.io/claude-board/`.

### 2. Create a GitHub token

Create a token with the **`gist`** scope (a classic token is simplest:
GitHub → Settings → Developer settings → Personal access tokens).

### 3. Write the config

Create `~/.claude/usage-board/config.json`:

```json
{
  "github_token": "ghp_...",
  "pages_url": "https://USER.github.io/claude-board/",
  "anthropic_admin_key": ""
}
```

`anthropic_admin_key` is optional. It's a Console organization Admin API key,
and it adds a tile with today's and month-to-date Console spend.

### 4. Create the Gist

```sh
python3 board.py --setup
```

This creates a secret Gist, saves its id into the config and prints the URL to
open on the iPad, in the form
`https://USER.github.io/claude-board/?src=https://gist.githubusercontent.com/...`.

### 5. Install the status line

```sh
python3 board.py --install
```

This points `statusLine` in `~/.claude/settings.json` at this copy of
`board.py` with a 30 s refresh, and saves a backup of the settings file first.
Claude Code then runs whatever is checked out here, so keep this copy working.
Run the command again if you move the repo.

### 6. Set up the iPad

1. Open the URL from step 4 in Safari.
2. Tap **Share → Add to Home Screen** so it opens full screen.
3. Set **Settings → General → Auto-Lock → Never** and leave it on the charger.

The board fills in after Claude Code's next reply.

## Commands

| Command | What it does |
| --- | --- |
| `board.py` | Status-line mode: reads Claude Code's JSON on stdin, prints a one-line status |
| `board.py --setup` | Creates the Gist and prints the iPad URL |
| `board.py --install` | Adds or replaces the `statusLine` entry in `~/.claude/settings.json` |
| `board.py --show` | Rebuilds and prints the current `board.json` |
| `board.py --push` | Uploads `board.json` now (normally started automatically) |

## Files

Everything lives in `~/.claude/usage-board/`. Set `CLAUDE_BOARD_DIR` to use
another folder.

| File | Contents |
| --- | --- |
| `config.json` | Token, Gist id, Pages URL, optional Admin key |
| `sessions/` | One file per Claude Code session, deleted after 24 h |
| `board.json` | What the iPad shows |
| `limits.json` | Last Fable weekly reading |
| `cost.json` | Last Console spend reading |
| `state.json` | Push and refresh timestamps |
| `push.log` | Errors from uploads and background fetches |

## Notes

- **Fable limit.** Claude Code's status-line JSON only includes the 5-hour and
  7-day windows. The Fable weekly limit is read from claude.ai's usage endpoint
  (`/api/oauth/usage`) using the login Claude Code stores in
  `~/.claude/.credentials.json`. On macOS, Claude Code keeps that login in the
  Keychain instead, so the Fable ring stays hidden there. The endpoint isn't a
  public API and may change; if it does, the ring disappears and the rest of the
  board keeps working.
- **HTTP 429.** The usage endpoint is rate-limited per account, and Claude Code
  polls it too. `board.py` checks every 10 minutes and doubles the wait after a
  429, up to 60 minutes. The page keeps the last good reading and only shows a
  warning once it's more than 75 minutes old.
- **Privacy.** A secret Gist isn't listed anywhere, but anyone with its URL can
  read it. It contains session names, folder names, models and costs.
- **"Claude Code has been silent".** The page shows this when `board.json` is
  more than 3 minutes old, which usually means no Claude Code session is open.
  Check `push.log` if sessions are open but the board isn't updating.
