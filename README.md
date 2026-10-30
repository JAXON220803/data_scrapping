# Historical football season statistics

Resumable whole-season collection from Sofascore and Flashscore for the league-seasons with missing shot statistics in the supplied dataset. The initial manifest selects **115 seasons**, including **93 entire seasons** without shot values. Both sources are collected separately, including their extra playoff fixtures. Collection saves every available full-match statistic; exports highlight `HS`, `AS`, `HST`, and `AST`.

Open [PROGRESS.md](PROGRESS.md) for counts by season and [Actions](https://github.com/JAXON220803/data_scrapping/actions) for the latest run. `progress.json` includes source errors, inventories, request counts and coverage shortfalls.

## Automatic operation

After each successful batch saves its checkpoints, the `Collect season statistics` workflow **immediately queues the next runnable batch**. GitHub starts it when a runner is available. Runs are serialized, so batches cannot collect simultaneously. The hourly schedule at minute **17 (UTC)** remains as a recovery trigger for expired cooldowns, the next UTC quota day, or an interrupted chain. You can also start collection from Actions → Run workflow. No football API key or personal access token is needed: GitHub supplies `GITHUB_TOKEN` with contents and actions write permissions. Successor runs use workflow dispatch, which [GitHub permits from its built-in token](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

Defaults in `settings.json`: at least 15 seconds between data requests to each source, 20 requests per source per run, 400 per source per UTC day, and a 1,000-second collection budget. Browser pages may need multiple requests, so 20 requests does **not** mean 20 matches. Website assets such as JavaScript are additional browser traffic; unnecessary images and live odds feeds are disabled. Quotas persist across runners and count failed attempts too. These are our conservative limits, not a provider allowance or guarantee of permission.

Automatic chaining stops when collection completes, no tasks are ready, all selected sources are cooling down, or their daily budgets are exhausted. Request spacing and daily quotas are unchanged; immediate continuation removes the hourly idle gap, not the source limits.

Internet failures retain checkpoints and pause the affected source for an hour. Access denials, robots exclusions, challenges and HTTP 429 pause it for 24 hours. The collector does not bypass restrictions. Rendering or schema errors remain visible and retryable rather than being recorded as successful empty statistics. A repository writer can use **retry_blocked** after fixing an error. Collection of the other source can continue independently.

Live validation recovered real shot statistics from both sources. Flashscore's tested 2016/17 archive exposed 108 fixtures, then its normal **Show more matches** request returned HTTP 200 with the body `0`. This is incomplete source coverage, not a complete season. The collector checks statistics for the fixtures it can discover and retries the archive after 24 hours, preserving already checked matches. The season remains unfinished while pagination is unavailable. Historical coverage from Flashscore is therefore not yet verified end to end.

The first successful hosted batch committed additional real observations from Flashscore. Sofascore returned HTTP 403 from the GitHub runner, despite working in local validation, and is paused until its next permitted retry. Scheduling cannot guarantee a website will serve GitHub-hosted traffic. Source errors and their retry times are recorded in `progress.json` and `checkpoints/sources.json`.

GitHub hosts each scheduled batch, so the owner's computer can be off. This is scheduled execution, not a continuously running server: [GitHub may delay schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), and scheduled workflows in public repositories can be disabled after 60 days without activity. Check Actions if updates stop. Retained artifacts help recover a batch whose final push fails; download them and restore the checkpoint/data files before manually rerunning it. Sudden runner termination before a commit can repeat the last uncommitted batch.

`COMPLETE.json` means all configured source-season tasks were checked. Subsequent scheduled runs make no collection requests. **complete_with_gaps** means the sources did not provide every missing observation; it does not claim those gaps were filled. Use **retry_unavailable** to check unavailable match statistics again. Changing targets/settings invalidates the completion marker. New seasons require updating `targets.json` and adding the corresponding reference CSV.

## Filling percentage

The first line of `PROGRESS.md`, each season row, and the GitHub run summary show the filling percentage. `progress.json` includes `filled_percent`, `initial_missing_matches`, and `completed_missing_matches`.

**Filled % = originally incomplete reference matches now containing all four shot values / originally incomplete reference matches × 100.** Previously complete matches do not inflate this number. A partly filled match stays incomplete, and a match found by both providers counts once. `filled_cells_percent` separately measures individual missing shot cells recovered. Both percentages update when each batch saves its exports. Provider-task completion is reported separately and does not mean all gaps were filled.

## Files and data integrity

| Location | Contents |
|---|---|
| `targets.json` | Exact seasons, Sofascore tournament IDs and Flashscore archive URLs |
| `reference/COUNTRY/SEASON/DIV.csv` | Original match identity, scores and shot fields for affected seasons |
| `data/COUNTRY/DIV/SEASON/SOURCE/fixtures.json` | All discovered source fixtures, all available statistics, source URLs and status |
| `data/.../matches.csv` | Source-specific match export, including JSON of other statistics |
| `filled/COUNTRY/SEASON/DIV.csv` | Reference matches with verified missing shot values filled |
| `data/provenance.json` | Source fixture and field for every recovered cell |
| `checkpoints/` | Durable pagination, quota, source cooldown and task state |
| `progress.json`, `PROGRESS.md` | Coverage and progress, including unavailable data |

Only full-match shot observations are merged. Matching requires the same date, both teams (explicit aliases in settings), final score and finished status. Existing values, including zero, are preserved. Ambiguous matches and inconsistent shot totals are held back. Sofascore takes precedence for an empty field, followed by Flashscore. Missing values remain blank; no statistics are estimated. Provider inventories can contain more fixtures than the reference because of playoffs. Country and league metadata are verified before using a Sofascore tournament ID.

The reference exports intentionally contain match/statistic columns only. Original bookmaker columns and local CSV files are not uploaded or modified by this project. GitHub updates `filled/` in this repository; it cannot update files on a powered-off laptop. Use `git pull` to obtain the latest exports. When integrating into a wider dataset, join by division, date, home and away teams and fill only empty shot fields; retain the original additional columns.

## Local commands

Requires Python 3.12 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m collector.run
```

Run one exact target or one source:

```bash
.venv/bin/python -m collector.run --source sofascore --target Netherlands/N1/2016-2017
.venv/bin/python -m collector.run --source flashscore --max-requests 20
```

Allow collection only where the source permits automated access and your use of its data. Historical coverage varies; a match result page does not guarantee shot statistics exist. No claim is made that all 30,073 initially incomplete matches are recoverable. Inspect actual recovered-cell counts rather than treating request activity as successful recovery.

Do not commit API keys, personal tokens or `.env` files. No keys from the original local worker are included here.
