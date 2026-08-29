# Lab Day 2 Homework — Closing the AI Feedback Loop

Replace `saig217` with your login suffix everywhere (same value used at `/login`
and in the notebook's `username` widget).

## Deviations from the assignment text (deliberate)

| Assignment says | This build uses | Why |
|---|---|---|
| `student_saig217.ai_suggestion_feedback` | `bootcamp_students.ai_suggestion_feedback_saig217` | Lakebase caps replication slots at ~10 (one per schema). The Aug 20 announcement moved every student table into the shared `bootcamp_students` schema; `app.py` already hard-codes this via `SHARED_SCHEMA` + `_table_for`. |
| Delta table `..._cdc` | `bootcamp_students.bootcamp_cdc.lb_ai_suggestion_feedback_saig217_history` | Matches the existing CDC naming already in the notebook (`lb_github_repos_saig217_history`) and `databricks.yml`. The assignment's own Part 3 verification query uses `lb_..._history`; only its Part 4 snippet says `_cdc`. |
| `SELECT ... WHERE feedback = 'bad'` | `ROW_NUMBER()` reduction to latest change per key, then `feedback = 'bad'` | CDC history is append-only. The naive query blocks a suggestion forever even after the verdict is changed or the row is deleted. |
| Per-row `DELETE ... args={...}` loop | Single `MERGE INTO ... WHEN MATCHED THEN DELETE` | One job instead of N; no reliance on named-parameter binding inside DML. |
| — | `package_name` added to `ai_suggested` edge metadata | **The load-bearing fix.** See below. |

### The package_name problem

`ai_query_cache` is keyed on `(source_repo, package_name, suggested_repo)`, and Part 4
builds its blocked set from the same triple. But the original notebook records
AI edges as `record_edge(source, suggested_repo, "ai_suggested", {"validated": True})`
— no package name — and `ai_suggestions` collects `(source, target)` pairs, discarding
the `pkg` it had in hand.

Consequence if you don't fix it: the UI submits a blank or invented `package_name`,
the Lakebase insert succeeds, CDC replicates it, the Delta table shows a row, the
screenshot looks correct — and the `MERGE` matches zero cache rows. The loop is
cosmetically closed and functionally dead.

Fix: `ai_suggestions` now carries 3-tuples and `package_name` lands in the edge
metadata. **This requires re-running the traversal and re-syncing** for the UI to
see it. Edges created before the patch still work — the UI prompts for the package
name rather than silently writing a value that can never match.

## Order of operations

CDC registration is the long pole and the only step with an external dependency.
Do it first, then write code while replication catches up.

### 1. Create the table (Part 1)

Run `sql/create_ai_suggestion_feedback.sql` in the Lakebase SQL editor **as
yourself** — `ALTER TABLE ... REPLICA IDENTITY FULL` is owner-only DDL. The file
ends with verification queries; do not skip them.

If check #2 (`pg_publication_tables`) returns nothing, CDC will never see your
inserts. Check #3 tells you whether the publication is schema-wide (auto-includes
your table) or table-by-table (someone with ownership must run
`ALTER PUBLICATION <pub> ADD TABLE bootcamp_students.ai_suggestion_feedback_saig217`).
That is the one thing here you may not be able to fix alone — find out now, not at 1 AM.

### 2. Register the reverse sync (Part 3)

Same mechanism that produced `lb_github_repos_saig217_history` on Day 1: in the
Lakebase UI, add `bootcamp_students.ai_suggestion_feedback_saig217` to the
existing database-to-Delta sync targeting `bootcamp_students.bootcamp_cdc`.

The smoke-test `INSERT` at the bottom of the SQL file gives replication something
to carry. Verify in a Databricks notebook:

```sql
SELECT * FROM bootcamp_students.bootcamp_cdc.lb_ai_suggestion_feedback_saig217_history
WHERE _pg_change_type = 'insert'
ORDER BY _sort_by DESC;
```

**Screenshot this.** It is a graded artifact (Part 3).

If the table doesn't appear, confirm the exact name — `SHOW TABLES IN
bootcamp_students.bootcamp_cdc` — and adjust `feedback_history_table` at the top of
the notebook to whatever CDC actually created.

### 3. Deploy the app (Parts 2 + 5)

Changed files: `lakebase.py`, `app.py`, `templates/index.html`.

`run_write_returning()` is new in `lakebase.py` and is required: `run_query()` never
commits (psycopg2 `autocommit=False`, and `get_connection()` closes without
committing), so `INSERT ... RETURNING` through `run_query` would roll back and
return a row that was never persisted.

Local test:

```bash
curl -c c.txt -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" -d '{"username":"saig217"}'

curl -b c.txt -X POST http://localhost:8000/graph/edges/feedback \
  -H "Content-Type: application/json" \
  -d '{"source_repo":"facebook/react","package_name":"loose-envify",
       "suggested_repo":"zertosh/loose-envify","feedback":"bad",
       "reason":"Repo is archived"}'
# expect 201 + the inserted row

curl -b c.txt http://localhost:8000/graph/edges/feedback   # bonus history view
```

Error cases to verify (graded): no cookie → 401; missing `package_name` → 400;
`"feedback":"terrible"` → 400; malformed `source_repo` → 400; table dropped → 404.

### 4. Re-run the traversal (Part 4)

Notebook widgets unchanged. On the run you'll see:

```
  ✓ Loaded N blocked suggestions from human feedback
  ✓ Purged blocked mappings from ...ai_query_cache_saig217
    ↻ Re-querying facebook/react with 1 repo(s) excluded
```

Then re-sync `github_graph_edges_saig217` so the app picks up the new
`package_name` metadata.

Proof the loop closed:

```sql
-- should be empty for anything you marked bad
SELECT * FROM bootcamp_students.bootcamp_cdc.ai_query_cache_saig217
WHERE suggested_repo = '<the repo you rejected>';
```

### 5. Screenshots

Save into `screenshots/`:

- `deployed_feedback.png` — deployed app URL visible in the address bar, an
  `ai_suggested` edge with the "Mark as Bad" button, and the green success banner.
- `cdc_delta_table.png` — the `lb_ai_suggestion_feedback_saig217_history` query
  with at least one `insert` row.
- `pipeline_blocked.png` (recommended) — notebook output showing the blocked/purge/
  re-query lines. This is what proves Part 4 actually ran; nothing else does.

Then **delete the Databricks App** per the Aug 17 instruction.

## Bonus coverage

- **+5 feedback history view** — `GET /graph/edges/feedback` plus the "Feedback
  History" table in `index.html`, with Undo (the delete shows up as
  `_pg_change_type = 'delete'`, which the blocked-set query already filters out).
- **+5 re-query with negative prompt** — rejected repos are appended to the
  `ai_query` prompt as an explicit exclusion list, the cache is bypassed for any
  repo with feedback, and blocked pairs are filtered out of the new results before
  they're written back to the cache.

## Known weaknesses (say these out loud if asked in review)

- `ai_query_cache` is written with `mode("append")` and no dedup, so repeated runs
  accumulate duplicate mappings. The MERGE deletes all matching duplicates, so
  blocking still works, but the cache grows.
- The cache-hit branch is per-`source_repo`, not per-package. Bypassing it entirely
  when feedback exists means one rejection costs a full re-query of that repo's
  dependency set.
- `'good'` feedback is stored but does nothing in the pipeline yet.
