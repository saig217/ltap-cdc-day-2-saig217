"""
Databricks App: GitHub Repo Explorer

- Serves a small Flask API
- Reads/writes repos to the student's table in the shared Lakebase schema
  (bootcamp_students.github_repos_<username>) using a single GitHub API call per add
  (see github_client.py:get_repo)

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os
import re

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import lakebase
from github_client import GitHubClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("github-insights-app")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

# "owner/repo" shape check, e.g. "databricks/spark" or "sylph-ai/adal".
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# All students share a single schema to simplify CDF replication.
# Tables are namespaced by appending _<username> to the base table name,
# e.g. github_repos_ada lives in bootcamp_students.github_repos_ada.
SHARED_SCHEMA = "bootcamp_students"
# Graph edges are synced into Lakebase as bootcamp_cdc.uc_github_graph_edges_<username>.
GRAPH_SCHEMA = "bootcamp_cdc"

# Feedback vocabulary. Kept in sync with the CHECK constraint in
# sql/create_ai_suggestion_feedback.sql. Only 'bad' currently affects the
# pipeline; 'good' is stored for future use (e.g. boosting suggestions).
_ALLOWED_FEEDBACK = {"bad", "good"}

# Postgres unquoted identifiers: must start with a letter or underscore,
# followed by letters/digits/underscores, and fit within the 63-byte
# identifier length limit (minus the longest table prefix like
# "github_file_contents_"). This is intentionally strict (lowercase only,
# no leading digit) so every valid username maps 1:1 to a safe, unquoted
# Postgres table suffix - no chance of SQL injection via CREATE TABLE,
# which can't be parameterized like data values.
_MAX_TABLE_PREFIX_LEN = len("github_file_contents_")
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,%d}$" % (63 - _MAX_TABLE_PREFIX_LEN - 1))


def _validate_username(username: str) -> str:
    """Normalize and validate a student username as a safe Postgres table-name
    suffix. Raises ValueError with a student-facing message on failure."""
    if not isinstance(username, str):
        raise ValueError("Username is required.")
    username = username.strip().lower()
    if not username:
        raise ValueError("Username is required.")
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "Username must be lowercase letters, digits, or underscores, "
            "start with a letter or underscore, and be short enough to fit "
            "a Postgres identifier (max %d chars)." % (63 - _MAX_TABLE_PREFIX_LEN)
        )
    return username


def _table_for(base_table: str, username: str, schema: str = SHARED_SCHEMA) -> str:
    """Return the fully-qualified table name: <schema>.<base_table>_<username>."""
    return f"{schema}.{base_table}_{username}"


_TABLE_MISSING = "Table not found. Per-user tables should already exist; you do not create a schema."








@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page),
    so the frontend's resp.json() call never chokes on HTML."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


def _current_username() -> str | None:
    """Return the signed-in student's validated username, or None."""
    return session.get("username")


@app.route("/")
def index():
    """Simple UI to add repos. Requires signing in with a username first."""
    username = _current_username()
    if not username:
        return redirect(url_for("login"))
    return render_template(
        "index.html",
        username=username,
        schema=SHARED_SCHEMA,
        graph_table=_table_for("uc_github_graph_edges", username, GRAPH_SCHEMA),
        feedback_table=_table_for("ai_suggestion_feedback", username),
    )


@app.route("/login", methods=["GET"])
def login():
    """Sign-in page: student enters a username, no password (this mirrors a
    classroom "pick your name" flow, not a real auth system)."""
    if _current_username():
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def do_login():
    """
    Validate the submitted username as a safe Postgres table-name suffix
    and start a session. Shared schemas already exist; tables are
    namespaced as <table>_<username>.
    """
    if request.is_json:
        raw_username = request.json.get("username", "")
    else:
        raw_username = request.form.get("username", "")

    try:
        username = _validate_username(raw_username)
    except ValueError as exc:
        if request.is_json:
            return jsonify({"error": str(exc)}), 400
        return render_template("login.html", error=str(exc)), 400

    session["username"] = username

    if request.is_json:
        return jsonify({"username": username, "schema": SHARED_SCHEMA})
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    if request.is_json:
        return jsonify({"status": "ok"})
    return redirect(url_for("login"))


@app.route("/schema/status")
def schema_status():
    """Check whether this user's shared-schema tables exist in Lakebase."""
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    repos_name = f"github_repos_{username}"
    files_name = f"github_files_{username}"
    rows = lakebase.run_query(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_name IN (%s, %s)
        """,
        (SHARED_SCHEMA, repos_name, files_name),
    )
    existing = {r["table_name"] for r in rows}

    return jsonify({
        "schema": SHARED_SCHEMA,
        "github_repos": _table_for("github_repos", username),
        "github_repos_exists": repos_name in existing,
        "github_files": _table_for("github_files", username),
        "github_files_exists": files_name in existing,
    })


@app.route("/repos", methods=["GET"])
def get_repos():
    """Return the repos in the student's github_repos table."""
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    table = _table_for("github_repos", username)
    try:
        rows = lakebase.run_query(
            f"SELECT full_name, language, stargazers_count, open_issues_count, "
            f"forks_count, is_favorite, ingested_at "
            f"FROM {table} ORDER BY full_name ASC"
        )
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": _TABLE_MISSING}), 404
        raise

    return jsonify(rows)


@app.route("/repos", methods=["POST"])
def add_repo():
    """
    Fetch the latest stats for a single "owner/repo" from GitHub using
    exactly ONE API call (see GitHubClient.get_repo), then insert/update
    it in the student's github_repos table.
    """
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    if request.is_json:
        full_name = request.json.get("full_name", "")
    else:
        full_name = request.form.get("full_name", "")

    full_name = full_name.strip() if isinstance(full_name, str) else ""

    if not full_name or not _REPO_RE.match(full_name):
        return jsonify({"error": f"Invalid repo name (expected owner/repo): {full_name!r}"}), 400

    client = GitHubClient()
    try:
        data = client.get_repo(full_name)
    except requests.HTTPError:
        return jsonify({"error": f"Unknown repo: {full_name}"}), 400

    table = _table_for("github_repos", username)

    try:
        lakebase.run_write(
            f"""
            INSERT INTO {table}
                (id, full_name, language, stargazers_count, open_issues_count, forks_count, payload, ingested_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (full_name) DO UPDATE
                SET stargazers_count = EXCLUDED.stargazers_count,
                    open_issues_count = EXCLUDED.open_issues_count,
                    forks_count = EXCLUDED.forks_count,
                    language = EXCLUDED.language,
                    payload = EXCLUDED.payload,
                    ingested_at = EXCLUDED.ingested_at
            """,
            (
                data.get("id"),
                full_name,
                data.get("language"),
                data.get("stargazers_count"),
                data.get("open_issues_count"),
                data.get("forks_count"),
                __import__("json").dumps(data),
            ),
        )
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": _TABLE_MISSING}), 404
        raise

    return jsonify({
        "full_name": full_name,
        "language": data.get("language"),
        "stargazers_count": data.get("stargazers_count"),
        "open_issues_count": data.get("open_issues_count"),
        "forks_count": data.get("forks_count"),
    })


@app.route("/repos/favorite", methods=["POST"])
def toggle_favorite():
    """
    Mark or unmark a repo as a favorite.
    Expects {"full_name": "owner/repo", "is_favorite": true/false}.
    """
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    if request.is_json:
        full_name = request.json.get("full_name", "")
        is_favorite = request.json.get("is_favorite", True)
    else:
        full_name = request.form.get("full_name", "")
        is_favorite = request.form.get("is_favorite", "true").lower() in ("true", "1", "yes")

    full_name = full_name.strip() if isinstance(full_name, str) else ""

    if not full_name or not _REPO_RE.match(full_name):
        return jsonify({"error": f"Invalid repo name (expected owner/repo): {full_name!r}"}), 400

    table = _table_for("github_repos", username)

    try:
        affected = lakebase.run_write(
            f"UPDATE {table} SET is_favorite = %s WHERE full_name = %s",
            (bool(is_favorite), full_name),
        )
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": _TABLE_MISSING}), 404
        raise

    if affected == 0:
        return jsonify({"error": f"Repo not found: {full_name!r}"}), 404

    return jsonify({"full_name": full_name, "is_favorite": bool(is_favorite)})


@app.route("/graph/edges", methods=["GET"])
def get_graph_edges():
    """Return graph edges from bootcamp_cdc.uc_github_graph_edges_<username>.

    Optional query params:
      - edge_type: filter by type (dependency, ai_suggested)
      - source: filter by source repo
      - target: filter by target repo
      - limit: max results (default 200)
    """
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    edge_type = request.args.get("edge_type")
    source = request.args.get("source")
    target = request.args.get("target")
    limit = min(int(request.args.get("limit", 200)), 1000)

    conditions = []
    params = []

    if edge_type:
        conditions.append("edge_type = %s")
        params.append(edge_type)
    if source:
        conditions.append("source = %s")
        params.append(source)
    if target:
        conditions.append("target = %s")
        params.append(target)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    table = _table_for("uc_github_graph_edges", username, GRAPH_SCHEMA)
    try:
        rows = lakebase.run_query(
            f"SELECT source, target, edge_type, metadata, discovered_at "
            f"FROM {table}"
            f"{where_clause} ORDER BY discovered_at DESC LIMIT %s",
            tuple(params),
        )
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": "Graph edges table not found."}), 404
        raise

    return jsonify(rows)


@app.route("/graph/stats", methods=["GET"])
def get_graph_stats():
    """Return summary stats about the graph edges."""
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    table = _table_for("uc_github_graph_edges", username, GRAPH_SCHEMA)
    try:
        rows = lakebase.run_query(
            f"SELECT edge_type, COUNT(*) as count "
            f"FROM {table} "
            f"GROUP BY edge_type ORDER BY count DESC"
        )
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": "Graph edges table not found."}), 404
        raise

    total = sum(r["count"] for r in rows)
    return jsonify({"total_edges": total, "by_type": rows})


@app.route("/graph/edges/feedback", methods=["POST"])
def submit_edge_feedback():
    """
    Record human feedback on an AI-suggested graph edge.

    Body: {"source_repo": "owner/repo", "package_name": "pkg",
           "suggested_repo": "owner/repo", "feedback": "bad",
           "reason": "optional free text"}

    Writes to bootcamp_students.ai_suggestion_feedback_<username>, which is a
    real (writable) Postgres table -- unlike the synced graph tables, which are
    read-only by design. The Lakebase write is the head of the reverse flow:
    CDC picks it up into Delta, and the traversal notebook reads it to drop bad
    suggestions from ai_query_cache.
    """
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    payload = request.json if request.is_json else request.form

    def _field(name: str) -> str:
        value = payload.get(name, "")
        return value.strip() if isinstance(value, str) else ""

    source_repo = _field("source_repo")
    package_name = _field("package_name")
    suggested_repo = _field("suggested_repo")
    feedback = _field("feedback").lower() or "bad"
    reason = payload.get("reason")
    reason = reason.strip() if isinstance(reason, str) and reason.strip() else None

    if not source_repo or not _REPO_RE.match(source_repo):
        return jsonify({"error": f"Invalid source_repo (expected owner/repo): {source_repo!r}"}), 400
    if not suggested_repo or not _REPO_RE.match(suggested_repo):
        return jsonify({"error": f"Invalid suggested_repo (expected owner/repo): {suggested_repo!r}"}), 400
    if not package_name or len(package_name) > 255:
        return jsonify({"error": "package_name is required (1-255 chars)"}), 400
    if feedback not in _ALLOWED_FEEDBACK:
        return jsonify({
            "error": f"Invalid feedback {feedback!r}; allowed: {sorted(_ALLOWED_FEEDBACK)}"
        }), 400
    if reason is not None and len(reason) > 2000:
        return jsonify({"error": "reason must be 2000 characters or fewer"}), 400

    table = _table_for("ai_suggestion_feedback", username)

    try:
        rows = lakebase.run_write_returning(
            f"""
            INSERT INTO {table}
                (source_repo, package_name, suggested_repo, feedback, reason)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_repo, package_name, suggested_repo) DO UPDATE
                SET feedback   = EXCLUDED.feedback,
                    reason     = EXCLUDED.reason,
                    created_at = now()
            RETURNING id, source_repo, package_name, suggested_repo,
                      feedback, reason, created_at
            """,
            (source_repo, package_name, suggested_repo, feedback, reason),
        )
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message:
            return jsonify({
                "error": f"Feedback table not found: {table}. "
                         f"Run sql/create_ai_suggestion_feedback.sql first."
            }), 404
        if "violates check constraint" in message:
            return jsonify({"error": f"Invalid feedback value: {feedback!r}"}), 400
        raise

    return jsonify(rows[0]), 201


@app.route("/graph/edges/feedback", methods=["GET"])
def list_edge_feedback():
    """Bonus: feedback history for the signed-in student, newest first."""
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    limit = min(int(request.args.get("limit", 100)), 500)
    table = _table_for("ai_suggestion_feedback", username)

    try:
        rows = lakebase.run_query(
            f"SELECT id, source_repo, package_name, suggested_repo, feedback, "
            f"reason, created_at FROM {table} ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": f"Feedback table not found: {table}."}), 404
        raise

    return jsonify(rows)


@app.route("/graph/edges/feedback", methods=["DELETE"])
def delete_edge_feedback():
    """Bonus support: undo a feedback row so the suggestion is unblocked.

    The delete is captured by CDC as _pg_change_type = 'delete', which the
    notebook's blocked-set query already filters out.
    """
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    payload = request.json if request.is_json else request.form
    try:
        feedback_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Numeric feedback id is required"}), 400

    table = _table_for("ai_suggestion_feedback", username)
    try:
        affected = lakebase.run_write(f"DELETE FROM {table} WHERE id = %s", (feedback_id,))
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": f"Feedback table not found: {table}."}), 404
        raise

    if affected == 0:
        return jsonify({"error": f"No feedback row with id {feedback_id}"}), 404

    return jsonify({"id": feedback_id, "removed": True})


@app.route("/repos", methods=["DELETE"])
def remove_repo():
    """
    Remove a repo from the student's github_repos table.
    Expects {"full_name": "owner/repo"}.
    """
    username = _current_username()
    if not username:
        return jsonify({"error": "Not signed in"}), 401

    if request.is_json:
        full_name = request.json.get("full_name", "")
    else:
        full_name = request.form.get("full_name", "")

    full_name = full_name.strip() if isinstance(full_name, str) else ""

    if not full_name or not _REPO_RE.match(full_name):
        return jsonify({"error": f"Invalid repo name (expected owner/repo): {full_name!r}"}), 400

    table = _table_for("github_repos", username)

    try:
        affected = lakebase.run_write(
            f"DELETE FROM {table} WHERE full_name = %s",
            (full_name,),
        )
    except Exception as exc:
        if "does not exist" in str(exc):
            return jsonify({"error": _TABLE_MISSING}), 404
        raise

    if affected == 0:
        return jsonify({"error": f"Repo not found: {full_name!r}"}), 404

    return jsonify({"full_name": full_name, "removed": True})


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")
