# Poky Bhakundo FPL Tracker

An unofficial community dashboard for the Poky Bhakundo Fantasy Premier League competitions. It tracks weekly and monthly prizes, season contests, qualification seeds, and two knockout brackets.

**Live site:** [pokybhakundo.github.io/Poky_FPL](https://pokybhakundo.github.io/Poky_FPL/)

> This is an independent community project. It is not affiliated with, endorsed by, or sponsored by the Premier League or Fantasy Premier League.

## Features

- Manager of the Week and Manager of the Month standings
- King of the Ring and First Centurion tracking
- Jota Legends Classic-league knockout bracket
- Diogo Legacy H2H-league knockout bracket
- Champions Wall: season-by-season winners across every contest, plus a Hall of Fame card grid ranking managers by title count
- Fixed GW34/GW35 seed pairings based on the competition bracket
- Automatic winner advancement through GW38
- Live/provisional current-Gameweek results with automatic page refresh and a "last updated" freshness indicator
- Desktop connected-bracket view with live-state-colored connector lines
- Mobile round-by-round view with GW tabs, swipe navigation, expandable match details (state persists across polls), and a full-bracket option
- Static GitHub Pages frontend, updated by a GitHub Actions workflow that skips its own pipeline when nothing can change (`should-poll`)

## Knockout format

The top 20 managers qualify after GW33:

- Seeds T1–T12 advance directly to the Round of 16.
- Seeds T13–T20 play four qualifiers in GW34.
- Round of 16: GW35
- Quarterfinals: GW36
- Semifinals: GW37
- Final: GW38

Knockout matches are decided by:

1. Highest Gameweek points
2. Most goals scored
3. Fewest goals conceded
4. Highest current Classic-league rank

The same final tiebreak criterion is used for both the Classic and H2H brackets. There is no virtual coin toss.

## Project structure

| Path | Purpose |
| --- | --- |
| `FPL.PY` | Fetches FPL data, builds contests, seeds brackets, and advances winners |
| `server.py` | Local Flask server and development API |
| `bracket_map.json` | Fixed quarterfinal-to-final bracket tree |
| `state/` | Generated qualification, bracket, contest, and dashboard state |
| `docs/index.html` | Static responsive frontend |
| `docs/data/` | State mirrored for GitHub Pages |
| `.github/workflows/fpl-tracker.yml` | `workflow_dispatch`-only updates, driven by an external scheduler or manual runs |

## Requirements

- Python 3.12 or newer
- Internet access to the Fantasy Premier League endpoints

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python server.py
```

Open [http://localhost:8000](http://localhost:8000).

## Command-line usage

Save finalized league standings:

```bash
python FPL.PY --mode qualify
```

Lock a specific Gameweek snapshot for testing:

```bash
python FPL.PY --mode qualify --gw 33 --lock
```

Create both knockout brackets:

```bash
python FPL.PY --mode init-bracket --league both --start-gw 34
```

Advance finalized matches:

```bash
python FPL.PY --mode advance --league both
```

Include the current live Gameweek as provisional data:

```bash
python FPL.PY --mode advance --league both --live
```

Rebuild contest and dashboard data:

```bash
python FPL.PY --mode contests
python FPL.PY --mode dashboard
```

Rebuild the current-GW, all-players standings (both leagues, unlike the top-20-only snapshots above) used by the "Current GW Rankings" tab:

```bash
python FPL.PY --mode standings
```

Rewrite the fixed GW34/GW35 seed maps:

```bash
python FPL.PY --mode draw --league both --force
```

Live-preview the real qualification bracket while the qualifying Gameweek (`start-gw - 1`) is in progress — no-op otherwise, and h2h is skipped entirely until that GW is actually live:

```bash
python FPL.PY --mode live-reseed --league both --start-gw 34
```

Classic-only smoke test: rebuilds the full bracket tree, but only the Qualifier round's data source rotates with whichever Gameweek is currently live (still labeled GW34); everything from Round of 16 onward stays correctly TBD until the real season gets there. Automatically disables itself once the real qualifying window is reached:

```bash
python FPL.PY --mode live-reseed-test
```

Check whether the scheduled pipeline needs to run at all (prints `true`/`false`; used by the workflow to skip work when the current GW is fully finalized and the next deadline is still far off):

```bash
python FPL.PY --mode should-poll
```

## Configuration

The private-league IDs and competition defaults are defined near the top of `FPL.PY`:

- `CLASSIC_LEAGUE_ID`
- `H2H_LEAGUE_ID`
- `BASE_KNOCKOUT_START_GW`
- `CENTURION_START_GW`
- `MOM_PHASES`

Do not commit secrets. This project currently uses public FPL endpoints and does not require an API key.

## Automation and deployment

`.github/workflows/fpl-tracker.yml` has no native GitHub Actions `schedule:` trigger — GitHub's built-in cron has a 5-minute floor and is frequently delayed under load, so an external scheduler calls the workflow's `workflow_dispatch` API with `mode: scheduled` on its own cadence instead.

A `scheduled` dispatch first runs `should-poll`, which returns `false` (skipping the rest of the pipeline for that tick) whenever the current Gameweek is fully finalized and the next deadline is still more than an hour off — nothing can change in that window, so there's no reason to hit the FPL API. When polling is worthwhile, the run:

1. Saves finalized qualification data when applicable (`qualify`).
2. Live-previews the real knockout bracket for whichever league's qualifying GW is currently live, and rebuilds the classic-only qualifier smoke test (`live-reseed`, `live-reseed-test`).
3. Advances both knockout brackets with current-Gameweek provisional points (`advance --live`).
4. Rebuilds contest results, including the provisional Manager of the Week (`contests`).
5. Rebuilds `dashboard.json`.
6. Mirrors generated files from `state/` into `docs/data/`.
7. Commits changed state for GitHub Pages.

Manual `workflow_dispatch` runs can invoke any other mode (`qualify`, `live-reseed`, `init-bracket`, `advance`, `dashboard`, `contests`, `standings`) with a selected league and starting Gameweek. The `live` option includes current-Gameweek provisional points.

The open website checks the generated JSON once per minute while the tab is visible, and shows a "last updated" timestamp sourced from `contests.json`'s `generated_at` field, so users can see how fresh the data actually is. Because GitHub Pages is static, the practical update delay is the external scheduler's cadence plus GitHub Actions and Pages deployment time; it is not second-by-second live scoring.

### h2h is inert until GW33

`scheduled_targets()` drops `h2h` from `live-reseed` and `advance` entirely — no API calls attempted — until the real qualifying Gameweek (`start-gw - 1`, i.e. GW33 for a GW34 knockout start) is actually live. `run_qualify()` skips fetching h2h standings on every GW finalize for the same reason. Classic is unaffected by this guard.

### Classic qualifier smoke test

`live-reseed-test` (classic only) shows the full bracket tree — T1–T12 direct byes and TBD placeholders for every round — the same way the real bracket will look, but only the Qualifier round's underlying data rotates with whichever Gameweek is currently live; it's always displayed as GW34 regardless of which real GW is feeding it. Every other round keeps its real, not-yet-live GW number, so it stays genuinely unresolved instead of fake-progressing on early-season data. It automatically stops rebuilding once the real qualifying window (GW33) is reached, handing off to the production `live-reseed` path.

## Typography and visual identity

The frontend uses the Barlow family with local system fallbacks. Its matchday-broadcast palette combines deep navy surfaces, broadcast blue navigation, championship gold, winner green, and live-state red; it remains intentionally distinct from the official FPL purple-and-green identity.

These choices reduce visual similarity but do not grant permission to use third-party trademarks, logos, screenshots, data, or other protected material elsewhere in the project.

## Licensing and third-party rights

### Code license

The original Python, HTML, CSS, JavaScript, workflow, configuration, and documentation are available under the [MIT License](LICENSE).

The scope notice in `LICENSE` excludes FPL data, third-party trademarks and branding, and image assets unless they are expressly identified as project-owned original artwork.

### Current branding status

The public-facing artwork has been updated:

- `docs/LOGO.jpeg` uses the custom Poky Bhakundo football identity.
- `docs/brackets.jpeg` uses the Poky Bhakundo logo and contains no Premier League lion or official league logo.
- The website uses Barlow with system fallbacks and a separate visual palette rather than the official FPL font and website styling.

No official Premier League lion or league logo is intentionally included in the current interface. The project remains clearly identified as an independent, unofficial community tracker.

Use `docs/gwmom.jpeg` only if it was created for this project or you otherwise have permission to publish it. If its source is uncertain, replace it with an original table or render the same information directly in HTML.

This branding review does not make the project “copyright-free.” The original project code, documentation, and artwork are themselves protected works and are licensed as described above. Third-party names, trademarks, and FPL-derived data remain subject to their respective owners' rights and terms.

## Disclaimer

This software is provided for community and informational use. Match data may be provisional, delayed, incomplete, or changed after official review. Verify results before awarding prizes.

Premier League, Fantasy Premier League, their logos, and related marks belong to their respective owners. No endorsement is implied.
