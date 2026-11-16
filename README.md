# cs2render: CS2 demo highlights to video, in the background

Give it a CS2 demo and it finds the highlights (3K, 4K, aces and won clutches), records each one in
first person from the player's view **with game sound**, adds a title card, and joins them into a
highlight reel. CS2 runs **behind your other windows**, plays its sound on an output you don't listen to
(so you hear nothing), and your CS2 settings are restored afterwards.

## Quick start

```bat
cd C:\cs2render
python render.py "C:\Users\unsel\Downloads\match.dem"
```

The clips go to `C:\cs2render\output\<demo name>\`:

```
01_R15_4K_p21.mp4                  <- one file per highlight
02_R13_3K_1v2_clutch_p21.mp4
...
highlight_reel.mp4                 <- all clips joined
clips.json                         <- details (player, round, kills, ticks)
scoreboard.html                    <- FACEIT-style scoreboard page (open in a browser)
scoreboard.txt                     <- the same as text, plus every player's swing per round
```

## Useful options

| Command | What it does |
|---|---|
| `python render.py match.dem --list` | Only lists the highlights it found and records nothing (quick) |
| `python render.py match.dem --player "!"` | Only one player's highlights (repeat `--player` for several) |
| `python render.py match.dem --max-clips 10` | Records up to 10 highlights (default 5) |
| `python render.py match.dem --min-score 150` | Only the biggest plays: 4Ks, aces, big clutches (see the scoring below) |
| `--gaps cut` | Cut quiet stretches out instead of fast-forwarding them |
| `python render.py a.dem b.dem c.dem` | Several demos in one go |
| `--no-titles` / `--no-reel` | No title overlay / no joined reel |
| `--fps 60 --height 1080` | Output frame rate and resolution |
| `--no-audio` | No game sound in the clips |
| `--audio-device "Digital"` | Choose which output CS2 plays on while rendering (part of its name) |
| `--audio-audible` | Record sound even if there's no spare output (you'll hear CS2 while it renders) |
| `python render.py --fix-audio-route` | Emergency fix: CS2 is silent after a crashed render; run it with CS2 open |

### Title card

Each clip opens with a FACEIT-style card on the left: the player's Steam picture, name, level badge (1-10,
the website's levels), the play ("3K + 1V2 CLUTCH") and round, map and rating. It slides in at the start and
fades out after about 3.5 seconds. Levels and ratings come from the website (`/api/levels` at `SERVER_URL` in
`upload.env`, else the local site); if it isn't reachable the card has no level. Made by `titlecard.py`, which
needs Pillow (`pip install pillow`); without it clips get the plain text title. `--no-titles` turns it off.

### How highlights are scored

Every player's round gets **points**, and each point comes with a reason. `--list` shows them, for example:

```
143 pts  R15  !   4K
         4K (+45), 4x kill (+40), 4x eco kill vs full buy (+24), 2x long-range kill (30+ m) (+10), ...
```

| Group | What counts (points) |
|---|---|
| Impact | kill +10 each; multi-kill bonus 2K +5, 3K +20, 4K +45, ace +80; won clutch 1v1 +10, 1v2 +30, 1v3 +50, 1v4 +70, 1v5 +90; clutch on <35 HP +10; killed the last enemy +5; round won +5; defuse with <1 s left +40 (<3 s +15); +10 per enemy alive while defusing; HE grenade +1 per 10 damage and +8 per extra enemy hit; nade kill +15; flash assist +6 |
| Difficulty | headshot +2; Deagle headshot +4; noscope +15; through smoke +8; while flashed +10; mid-air +12; wallbang +8; 30+ m +5; flick (crosshair moved 40°+ in the last 125 ms) +6; knife +30; Zeus +25; 3+ kills within 3 s +20 (within 6 s +10) |
| Context (only for plays already worth 25+) | eco vs full buy +6 per kill; match point +10; overtime +10; last round of a half +5; close game late on +5 |
| Round impact | **swing**: +0.5 per 1% the player changed their team's chance to win the round (at most −10 if negative); see *Player stats* below |
| Penalty | victim had 20 HP or less −4 |

The opening kill of a round gets no bonus of its own. The default threshold is **100 points** (`--min-score`),
and each match gets at most 5 clips (`--max-clips`), the best ones first. With round impact counted, an ordinary
3K lands around 90-120 and a 4K or clutch well above, so 100 keeps the standout plays: a quiet match gets fewer
than 5 clips instead of filler. `--min-score 45` brings back every 3K. All weights are in `POINTS` at the top of
`highlights.py`.

### Quiet stretches inside a highlight

If a highlight has more than 10 seconds of nothing between actions (for example 4 kills, then the 5th one
50 seconds later), that stretch is **fast-forwarded** at 4–16x (aiming for about 5 seconds on screen),
without a speed label; its sound is muted. Use `--gaps cut` to cut those stretches out instead.

You can also list highlights on their own with `python highlights.py <path to .dem>` (add `--all` to see
every scored play).

## Player stats: KAST and Swing

`python scoreboard_html.py match.dem` makes a FACEIT-style scoreboard page (renders do this
automatically): final score, round history, and per team Rating, Swing, K, D, A, ADR, KAST, K/D, K/R,
HS, HS%, 5K/4K/3K/2K and MVPs. **Rating** uses the HLTV Rating 2.0 formula (community approximation,
1.00 = average); FACEIT's own rating formula isn't public, so it differs slightly from FACEIT's numbers.
Rank/Elo isn't in the demo.
Profile pictures come from each player's public Steam profile (looked up by Steam ID, no key needed) and
are cached in `cache\avatars\` for a week; private profiles and bots show initials instead.

```bat
python stats.py "C:\path\to\match.dem"             :: scoreboard
python stats.py match.dem --rounds                   :: + every player's swing per round
python stats.py match.dem --trace 13                 :: win probability through round 13, event by event
python stats.py --evaluate a.dem b.dem c.dem         :: test the win model on your demos
```

- **KAST**: % of rounds where the player got a **K**ill, an **A**ssist (flash assists count), **S**urvived
  the round (deaths after the round ended don't count against you), or was **T**raded (their killer was
  killed by a teammate within 5 seconds).
- **Swing**: how much the player changed their team's chance of winning, averaged per round.
  - A win-probability model gives P(T wins) at every moment from: players alive (weighted by HP),
    average equipment value of the alive players, bomb planted or not, round or bomb time left,
    defuse kit, and defuses in progress.
  - Every kill, plant and defuse moves that probability; the change is credited to players:
    - **Kill:** the killer gets 40%, the rest is split by damage dealt to the victim that round (so the
      player who did 90 damage gets most of the credit for a 10 HP finish); a flash assist gets 10%.
    - **Death:** the victim loses what their team lost; halved if they were traded.
    - **Plant / defuse:** the planter / defuser. A defuse is worth T's chance right after the last real
      event, never what the clock added.
  - The clock running down isn't credited to anyone.
  - Typical values are about −5% to +10% per round; +3% or more is a strong game.
- ADR counts damage actually dealt (a 150-damage Deagle hit on a 30 HP player counts 30), so it matches
  the game's own number.

**How good is the win model?** Tested on 722 game states from 4 matches (91 rounds):

| | Log loss (lower is better) | Picks the round winner |
|---|---|---|
| Coin flip (base rate) | 0.604 | 63% |
| This model | **0.383** | **83%** |

It's well calibrated: states it rated 0–10% were won 3% of the time, 30–50% → 39%, 90–100% → 100%.
Re-fitting it on these demos didn't improve it, so the defaults stay. With many more demos,
`python stats.py --fit *.dem` re-estimates it (saved to `win_model.json`). The numbers won't match
HLTV's exactly, since their model is trained on a huge set of pro matches, but they rank impact the
same way.

## Server stats (5v5stats.com history + our demos)

The website's **Leaderboard**, **Matches** and player pages hold the 5v5 Turkish Server's full history from
5v5stats.com (its owner agreed), rated with 5v5stats' own formula, plus every newer match we upload.

```bat
python import5v5\sync.py                        :: get the latest 5v5stats data (about weekly) into the website
python upload.py --demo "C:\path\match.dem"      :: add a demo's stats only (no CS2 needed, works while you play)
python render.py match.dem --upload              :: render highlights and add the stats too
```

- **Rating:** 5v5stats' formula, reverse-engineered from its published numbers and reproduced exactly (same
  scores, ranks and 1-10 tiers for all 123 players and all three periods; `python tests\test_rating.py`). It
  weights z-scores of KPR/DPR/ADR (counted twice by the site), a KAST estimate `(KPR + APR + 1 - DPR) / 3`,
  multi-kill rounds, HS%, accuracy, smoothed 1v1/1v2 clutch and opening-duel rates and utility, then shrinks
  it by `rounds / (rounds + 200)`; rank 1-10 tiers come from rank percentiles. Details in `web\rating.py`.
- **Skor (display scale, ours):** `e^(0.5 x adjusted score)`, FACEIT/HLTV style: 1.00 = server average, nobody
  below 0 (range about 0.47-1.89). Same order and tiers as 5v5stats; only the scale differs, so a very weak
  player no longer "cancels" a star when balancing.
- **Takım Kur (team builder, /balancer):** pick 10 of the 60 most active players (most matches in the last
  30 days first); power is 5v5stats' own `0.75 x all-time + 0.25 x 30-day` score (reproduced exactly), shown
  on the 1.00 scale above. All 252 5+5 splits are checked and the one
  with the most even total *and* level-by-level matchup wins. A chosen map shifts each player's power by
  their ADR on it (approximation; 5v5stats' map formula isn't public). "Kopyala" copies the teams and a
  link that reopens the same selection.
- **New demos** are measured the same way as the server's MatchZy plugin (`counters.py`: kills, damage,
  shots and hits, 1v1/1v2, opening duels, utility damage, flashes, 3K+ rounds, sides) and added to each
  player's 5v5stats totals.
- **No double counting:** when 5v5stats later includes a match we uploaded, the two are merged automatically
  (same map, date, score and players); the 5v5stats copy counts and links to our highlights.
- **30 days / this week** use the same window as 5v5stats (the 30 / 7 days up to its newest match) with its
  exact totals, plus our demo matches from that window on, so they match 5v5stats except for those demos.
- Downloads are cached in `import5v5\raw\` (one page per second, public pages only, not the site's `/api/`).

## Website with admin panel (prototype, runs on this PC)

```bat
python C:\cs2render\web\app.py
```

Then open http://localhost:5000 (the public site) and http://localhost:5000/admin (admin). The admin
password and the upload token are in `web\admin.env`, created on first start. The site is only reachable
from this PC.

The website only stores **results**: rendering stays on this PC, and finished matches are uploaded to it.
Demos are never uploaded, and the site never runs CS2.

```bat
python upload.py de_inferno_2026-11-05_02-31-19            :: send one rendered match (public right away)
python upload.py de_inferno_2026-11-05_02-31-19 --hidden   :: ... but keep it hidden (publish it in Admin)
python upload.py --all                                     :: every rendered match in output\
python render.py match.dem --upload                        :: render and upload in one go
python upload.py <match> --zip-only                        :: just make the package, to upload by hand in Admin
```

- A **package** is one zip: `match_data.json` (stats), the clips, thumbnails and profile pictures. The site
  checks it (only those file types, no paths outside the match folder) and ignores anything else.
- **Uploading the same match again** updates it: same link and title (and makes it public, unless you pass `--hidden`).
- **Admin:** publish / hide each match, give it a title, delete it, see its clips, players and the upload log.
  Hidden matches and their files are visible only to the admin, with a bar on the page saying so.
- **Storage:** `web\data\site.db` (SQLite: matches, players, clips, uploads) and `web\data\media\<id>\`
  (clips, thumbnails, profile pictures).
- **Hosted:** see the next section (Vercel + Neon + Cloudflare R2). The same code runs both ways.

## Hosting the website: Vercel + Neon Postgres + Cloudflare R2

The site runs as a Vercel function (Flask, `api/index.py` + `vercel.json`). Vercel functions keep nothing
between requests, so the data lives elsewhere:

| What | Where | Set by |
|---|---|---|
| Stats, matches, players, upload log | Neon Postgres | `DATABASE_URL` (or `POSTGRES_URL`) |
| Clips, thumbnails, profile pictures | Cloudflare R2 bucket, served from its public address | `R2_*`, `MEDIA_URL` |
| Admin password, session key, upload token | Vercel environment variables | `ADMIN_PASSWORD`, `SECRET_KEY`, `UPLOAD_TOKEN` |

Without these settings the site falls back to SQLite and files on this PC, as above (`config.py`).

**Uploads skip Vercel's 4.5 MB request limit:** `upload.py` asks the site for signed links
(`/api/upload/begin`), sends each file straight into the bucket, then sends only the stats
(`/api/upload/finish`). Each upload goes to a new folder in the bucket (`m/<match>/<upload>/`), and the old
one is deleted afterwards, so nobody watching a clip gets cut off. Against a site without R2, `upload.py` sends
the zip as before. The admin page's "upload a package" works too, but on Vercel only for zips under 4.5 MB.

**Setup, once:**

1. **R2:** in the Cloudflare dashboard, R2, create a bucket (e.g. `cs2-media`). Settings, Public access:
   allow the `r2.dev` address (or connect your own domain) and copy that URL. Manage API tokens, create a
   token with *Object Read & Write* on that bucket; copy the Access Key ID and Secret Access Key. The
   account ID is on the R2 overview page.
   No CORS rule is needed: browsers only play the files, and uploads come from `upload.py`.
2. **Neon:** copy the connection string (the *pooled* one, `...-pooler...`, `sslmode=require`).
3. **Vercel:** import the GitHub repo as a project (no framework, root directory = repo root). In Settings,
   Environment Variables, add:

   ```
   DATABASE_URL           postgresql://...neon.tech/...?sslmode=require
   ADMIN_PASSWORD         (pick one)
   SECRET_KEY             (64 random hex characters)
   UPLOAD_TOKEN           (long random string)
   R2_ACCOUNT_ID          ...
   R2_ACCESS_KEY_ID       ...
   R2_SECRET_ACCESS_KEY   ...
   R2_BUCKET              cs2-media
   MEDIA_URL              https://pub-xxxx.r2.dev   (or your domain)
   ```

   Then deploy. Tables are created on the first request.
4. **Fill the database from this PC** (the 5v5stats history; takes seconds, the import is batched). Once:
   `pip install "psycopg[binary]"`. Then put
   `DATABASE_URL=...` in `web\admin.env` for one run, or set it in the shell:

   ```bat
   set DATABASE_URL=postgresql://...
   python import5v5\sync.py --offline
   ```

   Remove it from `admin.env` afterwards if you want the local prototype back on SQLite.
5. **Upload matches:** in `upload.env` (next to `upload.py`):

   ```
   SERVER_URL=https://your-project.vercel.app
   UPLOAD_TOKEN=(the same token as in Vercel)
   ```

   then `python upload.py --all`.

Notes: Python packages for the site are in `requirements.txt` (Flask, psycopg). `.vercelignore` keeps the
renderer, tools and local data out of the deployment. Video traffic is served by R2 (no egress fees), not by
Vercel. Matches on the site are public to anyone with the link once published, with player names and Steam
pictures.

## Publishing as static files (alternative)

Rendering stays on this PC; `publish.py` copies finished matches to a website: `index.html` lists all
published matches, and each match has its summary page with the clips playable on it ("Play all" plays
them in a row). `TARGET` in `publish.env` picks where:

- **`local` (now):** a simulated website on this PC. Matches are copied to `C:\cs2render\site`, and
  `python serve_site.py` serves it at http://localhost:8080/index.html (only reachable from this PC).
  Nothing leaves the PC.
- **`r2`:** the real website, a Cloudflare R2 bucket with public access (fill in the R2 part of `publish.env`).

```bat
python publish.py de_inferno_2026-11-05_02-31-19 --dry-run    :: what would be uploaded (nothing is sent)
python publish.py de_inferno_2026-11-05_02-31-19              :: upload it and refresh the match list
python publish.py --list                                      :: published matches and their links
python publish.py --unpublish de_inferno_2026-11-05_02-31-19  :: take a match off the website
```

- For R2: fill in `publish.env` once (the steps are in that file); the keys stay on this PC.
  `--target local|r2` overrides `TARGET` for one run.
- Only what the page needs is uploaded: page, clips, thumbnails, profile pictures (~200 MB per match today).
  Not the joined reel, `clips.json` (local paths) or temporary files. Unchanged files are skipped.
- Published matches are public to anyone with the link, including player names and Steam pictures.
- The free `r2.dev` link is rate-limited (fine for friends); connect your own domain for more traffic.

## Rules while it runs

- **Steam must be running and signed in.** CS2 must be **closed** before you start; the script refuses
  to run if CS2 is already open, so it never touches a game you're playing.
- **The FACEIT client must be closed** (tray icon > Exit). Its anti-cheat protects CS2 while it runs, which
  blocks the console the renderer controls the demo with. The script checks this and tells you.
- CS2 pops up for a few seconds while it launches, then moves behind your windows. You can keep working
  (browsing, Discord and so on). **Don't minimize the CS2 window**, because Windows stops drawing
  minimized windows. Covering it is fine.
- Your graphics card is busy while it records, so don't game at the same time.
- CS2 is started with `-insecure` (VAC off) for the render only and closed afterwards. To play
  online, start CS2 normally from Steam.
- The PC is kept awake during a render.

## Game audio

- During a render, CS2's sound is sent to a **spare output** you don't listen to. On this PC that's
  `Digital Output (Realtek USB2.0 Audio)`, an optical jack with nothing connected, so you hear
  nothing. It's the same as Windows' *Settings > Sound > Volume mixer > app > Output device*, set only
  while the render runs and reset to default before CS2 closes.
- `tools\proccap.exe` records **only CS2's own audio stream** (Windows process loopback), so Discord,
  music and system sounds are never in the clips.
- Sync: every recording starts with the demo paused (silent, frozen picture) and resumes, so audio and
  video are lined up on that exact moment. Checked against 94 real shots from the demo: sound within
  ~5 ms of the shot, audio/video offset 10-34 ms (below what people notice).
- Fast-forwarded stretches are silent.
- If no spare output exists (for example, only headphones), audio is off unless you add
  `--audio-audible`. Alternatively install a free virtual audio cable (for example VB-CABLE) and it is
  used as the spare output automatically.
- If a render is ever killed hard and CS2 seems silent afterwards: open CS2, then run
  `python render.py --fix-audio-route`. Or in Windows: *Settings > Sound > Volume mixer >
  Counter-Strike 2 > Output device > Default*.

## Your settings

Before launching, the script saves the exact values of every CS2 setting it changes (volume, FPS/ping
display, build info, background frame limit, sound device, and a 120 fps cap while recording, because
uncapped CS2 keeps the GPU at ~99% and the window capture then freezes). Afterwards it puts them back, byte
for byte.
If a run is interrupted, they are restored automatically the next time you run the script. A copy of
the settings file from 2026-11-07 is in `backup\`.

## How it works

1. `highlights.py` reads the demo with `demoparser2` and finds multi-kills and won clutches, including
   the exact ticks.
2. `render.py` launches CS2 with a console port (`-netconport`) and controls the demo playback
   through it: jump to a tick, spectate the player, play, pause.
3. FFmpeg records only the CS2 window (Windows Graphics Capture, `gfxcapture`) and encodes on the
   GPU (NVENC). Each segment's real frame rate is checked, and choppy segments are recorded again.
4. Segments are cut frame-accurately, joined, titled, and combined into the reel. This processing runs
   in the background (at low priority) while CS2 already records the next clip. Clips are recorded in
   match order, because once a demo has played to the match end CS2 keeps showing the victory screen.
5. `audio.py` + `tools\proccap.exe` (built from `tools\proccap.cs` with Windows' own C# compiler) route
   CS2 to the spare output and record its audio; `sync.py` aligns it on the resume moment.
6. `stats.py` computes KAST, ADR and Swing (win-probability model) for the scoreboard and the highlight
   points.
7. `settings_guard.py` snapshots and restores your CS2 settings.

## Known limits

- Voice chat recorded in the demo isn't included (only game sound).
- HLAE (in `hlae\`) is installed but not used: its current release crashes with CS2's Nov 2026 update.
  Once HLAE is updated, it could give frame-perfect, offline (faster than real time) rendering.
- Recording happens in real time, so a 20-second clip takes about 20 seconds plus a few seconds of
  seeking. Launching CS2 and loading the demo takes about a minute per demo.
