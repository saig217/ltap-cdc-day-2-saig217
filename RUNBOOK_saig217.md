# From-scratch runbook — username `saig217`

Everything below is concrete. No placeholders left to fill in.

Your identifiers:

| Thing | Value |
|---|---|
| App login / notebook `username` widget | `saig217` |
| Lakebase feedback table (writable) | `bootcamp_students.ai_suggestion_feedback_saig217` |
| Lakebase repos table (Day 1) | `bootcamp_students.github_repos_saig217` |
| Lakebase synced edges (read-only) | `bootcamp_cdc.uc_github_graph_edges_saig217` |
| UC catalog / schema | `bootcamp_students` / `bootcamp_cdc` |
| Repos CDC history (Day 1) | `bootcamp_students.bootcamp_cdc.lb_github_repos_saig217_history` |
| Feedback CDC history (you build) | `bootcamp_students.bootcamp_cdc.lb_ai_suggestion_feedback_saig217_history` |
| AI cache (Delta) | `bootcamp_students.bootcamp_cdc.ai_query_cache_saig217` |
| Graph edges (Delta) | `bootcamp_students.bootcamp_cdc.github_graph_edges_saig217` |

`saig217` passes `app.py`'s username regex (`^[a-z_][a-z0-9_]{0,41}$`) — lowercase,
starts with a letter, 7 chars. No quoting needed anywhere.

---

## PHASE 0 — Establish where you actually are (10 min)

Run these before touching anything. They decide how much of Day 1/Day 2 you have to redo.

**0.1 — Lakebase SQL editor:**

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema IN ('bootcamp_students','bootcamp_cdc')
  AND table_name LIKE '%saig217%'
ORDER BY table_schema, table_name;
```

Expect at minimum `github_repos_saig217`. If that is missing, you never finished
Day 1 setup — do Phase 1 before anything else.

**0.2 — Databricks notebook:**

```sql
SHOW TABLES IN bootcamp_students.bootcamp_cdc LIKE '*saig217*';
```

Look for `lb_github_repos_saig217_history` (Day 1 CDC working) and
`github_graph_edges_saig217` (Day 2 traversal ran).

**0.3 — If edges exist, check whether they're usable:**

```sql
SELECT edge_type, COUNT(*) AS n
FROM bootcamp_students.bootcamp_cdc.github_graph_edges_saig217
GROUP BY edge_type;
```

You need `ai_suggested` rows. Zero means the AI validation loop never succeeded and
you must re-run Phase 4 regardless.

**Decision:**
- `lb_github_repos_saig217_history` missing → start at Phase 1.
- It exists, edges missing/empty → start at Phase 2, run Phase 4 fully.
- Both exist → start at Phase 2. You still re-run Phase 4 to get `package_name` into
  the edge metadata.

---

## PHASE 1 — Day 1 prerequisites (only if Phase 0 says you need them)

In the Lakebase SQL editor, signed in as yourself (you must own the tables to set
replica identity):

```sql
-- from sql/create_github_repos.sql, already substituted
CREATE TABLE IF NOT EXISTS bootcamp_students.github_repos_saig217 (...);
ALTER TABLE bootcamp_students.github_repos_saig217 REPLICA IDENTITY FULL;
```

Then add seed repos through the app — see the seed list below. **Every row in this
table becomes a seed** (the notebook does not filter on `is_favorite`), so keep it to
3–4 repos. You share a GitHub token with ~70 students.

You do not "register" this table for CDC either — same schema-level rule as Phase 3.
Once it has `REPLICA IDENTITY FULL` and at least one row, wait ~15s and confirm:

```sql
SELECT COUNT(*), MIN(_pg_change_type)
FROM bootcamp_students.bootcamp_cdc.lb_github_repos_saig217_history;
```

The initial snapshot writes every existing row as `_pg_change_type = 'insert'`.

### Seed repos to add

| Repo | Root manifest | Why |
|---|---|---|
| `gin-gonic/gin` | `go.mod` | Go module paths literally contain `github.com/owner/repo`, so the model has almost nothing to guess. Highest validation hit rate — your best source of `ai_suggested` edges. |
| `tiangolo/fastapi` | `pyproject.toml` | Deps map to unambiguous repos (`encode/starlette`, `pydantic/pydantic`). |
| `pallets/flask` | `pyproject.toml` | Deps are same-org (`pallets/werkzeug`, `pallets/jinja`, `pallets/click`) — near-certain validation. |
| `expressjs/express` | `package.json` | Many small deps across `jshttp/*`, `expressjs/*`, `debug-js/debug`. Mixed accuracy on purpose: you need at least one genuinely wrong suggestion to mark as bad. |

**Why these and not others:** line ~500 of the notebook filters dependency files with
`path IN ('package.json', 'requirements.txt', 'pyproject.toml', ...)` — an *exact*
match, not a suffix match. A manifest nested inside a monorepo (`packages/x/package.json`)
is fetched into staging but never reaches `ai_query`. All four above have their
manifest at the repo root.

Avoid as your only seed: `facebook/react` (the assignment's own example). It's a
monorepo whose root `package.json` is mostly devDependencies, so suggestions skew
toward build tooling with commonly-hallucinated owners. Useful as a *fifth* seed if
you want a reliably bad suggestion to reject; useless as a primary source of edges.

---

## PHASE 2 — Create the feedback table (Part 1, 15 pts)

Open `sql/create_ai_suggestion_feedback.sql` — already substituted for `saig217` —
and run it **in the Lakebase SQL editor as your own identity**.

Then run the verification block at the bottom of that file:

1. `relreplident` must be `f`. If it's `d`, the ALTER didn't take (usually an
   ownership problem) and CDF will skip the table.
2. `SELECT * FROM wal2delta.tables;` — the feed's own state table. Your table should
   show up with status `SNAPSHOTTING`, then `STREAMING`.
3. Run the smoke-test INSERT. **This is not optional.** CDF skips any table with zero
   rows, so the destination Delta table does not get created until a row exists.

---

## PHASE 3 — Confirm CDF picks up your table (Part 3, 25 pts)

**You almost certainly do not have to configure anything here.** Lakebase CDF is
configured *at the schema level* — once it is started on a schema, every current
**and future** table in that schema is included automatically. That is exactly why
the Aug 20 announcement moved everyone into `bootcamp_students`: one feed, one slot,
49 students. Your table inherits the existing feed the moment it has
`REPLICA IDENTITY FULL` and at least one row.

So "registering" reduces to: create the table (Phase 2), set replica identity,
insert a row, wait ~15 seconds (CDF batches and flushes on roughly that interval).

⚠️ **Do not click "Disable" on the Lakebase CDF tab.** Disabling stops the feed for
*every* schema in the project — you would break CDC for the entire cohort, and
re-enabling does not re-snapshot. If something looks wrong, check
`wal2delta.tables` and ask on Discord instead.

If your Lakebase role happens to have CAN MANAGE on the project and the feed genuinely
isn't running: app switcher → **Lakebase Postgres** → your project → branch → **Branch
overview** → **Lakebase CDF** tab → **Start**, with source schema `bootcamp_students`,
destination catalog `bootcamp_students`, destination schema `bootcamp_cdc`. Confirm
with the instructor before starting a feed that affects everyone.

Verify in a Databricks notebook:

```sql
SHOW TABLES IN bootcamp_students.bootcamp_cdc LIKE '*feedback*';

SELECT * FROM bootcamp_students.bootcamp_cdc.lb_ai_suggestion_feedback_saig217_history
WHERE _pg_change_type = 'insert'
ORDER BY _sort_by DESC;
```

You should see the smoke-test row (`facebook/react` / `loose-envify` /
`zertosh/loose-envify`).

**If the Delta table doesn't appear**, work down this list in order:

| Cause | Check | Fix |
|---|---|---|
| Table has zero rows | `SELECT COUNT(*)` in Lakebase | Run the smoke-test INSERT — CDF skips empty tables |
| Replica identity not set | `relreplident` = `f`? | Re-run the `ALTER TABLE` as the table owner |
| Not flushed yet | — | Wait ~15s; CDF batches |
| Feed not running | `SELECT * FROM wal2delta.tables;` | Ask on Discord — do not start/stop the feed yourself |
| Name collision | `SHOW TABLES IN bootcamp_students.bootcamp_cdc LIKE 'lb_*saig217*'` | CDF auto-suffixes duplicates as `..._history_1`. The notebook's `_resolve_cdf_table()` picks the populated one automatically — but check the printed `candidate ...: N rows` lines to confirm it chose right. |

⚠️ **The `_1` trap.** If a source table was ever dropped and recreated, CDF preserves
the orphaned destination Delta table and writes the new feed to `lb_<table>_history_1`.
The unsuffixed table still exists with zero rows. Reading it produces no error and no
data — the traversal runs green and writes nothing, because `write_graph_edges()`
returns silently on an empty edge list. `_resolve_cdf_table()` in the notebook now
picks whichever candidate actually has rows, for both the repos and feedback tables.

**If the table name differs** from `lb_ai_suggestion_feedback_saig217_history`, edit
line ~60 of `notebooks/day2_processing/github graph traversal.py` to match. The
loader fails soft — a wrong name prints a warning and proceeds with zero blocks, so
it looks like "no feedback yet" rather than an error. Watch the printed count.

One thing to know for later: **schema changes trigger a full re-snapshot** of the
affected table. If you add a column to the feedback table after CDF is running, CDF
re-reads the whole table and rewrites the Delta destination — harmless here, but it
means every row reappears as `insert`, which your `ROW_NUMBER()` reduction handles
correctly and the assignment's naive `WHERE feedback = 'bad'` does not.

📸 **Screenshot this query result** → `screenshots/cdc_delta_table.png`.

---

## PHASE 4 — Deploy the app (Parts 2 + 5, 45 pts)

Changed files vs. the lab repo: `lakebase.py`, `app.py`, `templates/index.html`.

Push this repo to your Git folder, then create/redeploy the Databricks App pointing
at it. Confirm the app has access to the `database/lakebase-url` and `github/token`
secret scopes (the Aug 18 permissions fix).

**Test the endpoint before trusting the UI:**

```bash
curl -c c.txt -X POST https://<your-app-url>/login \
  -H "Content-Type: application/json" -d '{"username":"saig217"}'

curl -b c.txt -X POST https://<your-app-url>/graph/edges/feedback \
  -H "Content-Type: application/json" \
  -d '{"source_repo":"facebook/react","package_name":"loose-envify",
       "suggested_repo":"zertosh/loose-envify","feedback":"bad",
       "reason":"Repo is archived and unmaintained"}'
```

Expect `201` and the inserted row. Then check the four graded error paths:

| Request | Expected |
|---|---|
| No session cookie | `401` |
| Missing `package_name` | `400` |
| `"feedback":"terrible"` | `400` |
| `"source_repo":"notarepo"` | `400` |

`GET /graph/edges/feedback` returns your history (bonus #1).

In the UI: set the graph filter to **AI Suggested**, click **Mark as Bad** on a row,
enter a reason. You should get a green banner with the feedback id, and the row
should appear in **Feedback History** below.

📸 **Screenshot** the deployed URL + the AI-suggested edge + the green success
banner in one frame → `screenshots/deployed_feedback.png`.

---

## PHASE 5 — Close the loop in the pipeline (Part 4, 15 pts)

Open `notebooks/day2_processing/github graph traversal.py`, set widgets:

- `catalog` = `bootcamp_students`
- `schema` = `bootcamp_cdc`
- `username` = `saig217`
- `target_repos` = `50` (keep it small — shared GitHub token, ~70 students)
- `max_hops` = `2`
- `fetch_mode` = `graphql`

Run it. You are looking for these lines in the output:

```
  ✓ Loaded N blocked suggestions from human feedback
  ✓ Purged blocked mappings from bootcamp_students.bootcamp_cdc.ai_query_cache_saig217
    ↻ Re-querying facebook/react with 1 repo(s) excluded
```

`N = 0` means the feedback table name is wrong or CDC hasn't propagated — go back to
Phase 3, don't proceed.

Prove the cache actually changed:

```sql
SELECT * FROM bootcamp_students.bootcamp_cdc.ai_query_cache_saig217
WHERE suggested_repo = 'zertosh/loose-envify';   -- or whatever you rejected
```

Zero rows = the loop closed.

📸 **Screenshot the notebook output** → `screenshots/pipeline_blocked.png`. This is
the only artifact that proves Part 4 ran; the graders cannot infer it from the code.

Finally, re-sync `github_graph_edges_saig217` back to Lakebase so the app's edges
carry `package_name` and future Mark-as-Bad clicks stop prompting for it.

---

## PHASE 6 — Package and submit

```bash
zip -r ltap-cdc-day-2-saig217.zip . -x "*/__pycache__/*" "*.pyc"
```

Must contain: `app.py`, `templates/index.html`,
`notebooks/day2_processing/github graph traversal.py`,
`sql/create_ai_suggestion_feedback.sql`, and all three screenshots.

Then **delete the Databricks App** (Aug 17 instruction).

---

## If you run out of time

Priority order by points-per-minute:

1. Phase 2 + 3 (40 pts) — table + CDC. Highest value, least code.
2. Phase 4 API endpoint (20 pts) — testable with curl alone, no UI needed.
3. Phase 4 UI (15 pts).
4. Phase 5 pipeline (15 pts) — the code is already written; it costs a notebook run.
5. Phase 4 screenshot (10 pts) — trivial once the app is up.

Bonuses are already implemented; they cost you nothing extra beyond the runs above.
