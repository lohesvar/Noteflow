import html
import os
import re
import secrets
import string
from datetime import datetime
from functools import wraps
import psycopg2
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    current_app,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash


load_dotenv()

CATEGORIES = ("Personal", "Work", "Code", "Ideas", "Journal")
EXPIRY_OPTIONS = ("never", "burn", "1h", "24h")
HASH_ID_LENGTH = 8
MAX_NOTE_CONTENT_LENGTH = 200_000


class DatabaseConfigurationError(RuntimeError):
    """Raised when the PostgreSQL connection string is unavailable."""


def create_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        DATABASE_URL=os.getenv("DATABASE_URL"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(
            os.getenv("VERCEL") == "1"
            or os.getenv("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
        ),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    if not app.config["SECRET_KEY"] and not os.getenv("VERCEL"):
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        app.logger.warning(
            "SECRET_KEY is not configured; local sessions will not survive restarts."
        )

    @app.before_request
    def protect_requests():
        if not current_app.config.get("SECRET_KEY"):
            return (
                "Application configuration is incomplete: set SECRET_KEY.",
                503,
                {"Content-Type": "text/plain; charset=utf-8"},
            )

        if request.method == "POST":
            submitted_token = request.form.get("csrf_token", "")
            session_token = session.get("_csrf_token", "")
            if not submitted_token or not secrets.compare_digest(
                submitted_token, session_token
            ):
                abort(400, description="The form expired. Reload the page and try again.")

        return None

    @app.context_processor
    def inject_template_context():
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return {
            "csrf_token": token,
            "logged_user": current_user(),
            "categories": CATEGORIES,
        }

    @app.teardown_appcontext
    def close_database_connection(_error=None):
        connection = g.pop("database_connection", None)
        if connection is not None:
            connection.close()

    @app.errorhandler(DatabaseConfigurationError)
    def handle_missing_database_configuration(_error):
        current_app.logger.error("DATABASE_URL is not configured.")
        return (
            render_template(
                "error.html",
                title="Service unavailable",
                status_code=503,
                message="The database is not configured yet. Please try again later.",
            ),
            503,
        )

    @app.errorhandler(psycopg2.Error)
    def handle_database_error(_error):
        current_app.logger.error("A PostgreSQL request failed.")
        return (
            render_template(
                "error.html",
                title="Service unavailable",
                status_code=503,
                message="NoteFlow could not reach its database. Please try again shortly.",
            ),
            503,
        )

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(410)
    @app.errorhandler(413)
    def handle_http_error(error):
        return (
            render_template(
                "error.html",
                title=error.name,
                status_code=error.code,
                message=error.description
                if error.code == 400
                else {
                    403: "You do not have permission to access this note.",
                    404: "The page or note you requested could not be found.",
                    410: "This note has expired and is no longer available.",
                    413: "This request is too large. Shorten the note and try again.",
                }.get(error.code, "The request could not be completed."),
            ),
            error.code,
        )

    def get_db():
        connection = g.get("database_connection")
        if connection is None:
            database_url = current_app.config.get("DATABASE_URL")
            if not database_url:
                raise DatabaseConfigurationError
            connection = psycopg2.connect(
                database_url,
                connect_timeout=5,
                sslmode=os.getenv("PGSSLMODE", "prefer"),
            )
            g.database_connection = connection
        return connection

    def current_user():
        user_id = session.get("user_id")
        if user_id is None:
            return None
        with get_db().cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT id, username FROM users WHERE id = %s",
                (user_id,),
            )
            user = cursor.fetchone()
        if user is None:
            session.clear()
        return user

    def login_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            g.current_user = user
            return view(*args, **kwargs)

        return wrapped_view

    def user_can_access(note, user):
        return bool(note["is_public"]) or (
            user is not None and note["user_id"] == user["id"]
        )

    def fetch_note(hash_id):
        with get_db().cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT * FROM notes WHERE hash_id = %s", (hash_id,))
            return cursor.fetchone()

    def reject_if_expired(note):
        with get_db().cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (expiry = '1h' AND created_at <= NOW() - INTERVAL '1 hour')
                    OR (expiry = '24h' AND created_at <= NOW() - INTERVAL '24 hours')
                FROM notes
                WHERE id = %s
                """,
                (note["id"],),
            )
            is_expired = cursor.fetchone()[0]
            if is_expired:
                cursor.execute("DELETE FROM notes WHERE id = %s", (note["id"],))
        if is_expired:
            get_db().commit()
            abort(410)

    def render_password_wall(hash_id, error=False, action=None):
        return render_template(
            "view_note.html",
            title="Protected note",
            note=None,
            hash_id=hash_id,
            password_required=True,
            password_error=error,
            password_action=action or url_for("view_note", hash_id=hash_id),
        )

    def render_note(note):
        with get_db().cursor() as cursor:
            cursor.execute(
                "UPDATE notes SET views = views + 1 WHERE id = %s RETURNING views",
                (note["id"],),
            )
            note["views"] = cursor.fetchone()[0]
            if note["expiry"] == "burn":
                cursor.execute("DELETE FROM notes WHERE id = %s", (note["id"],))
        get_db().commit()

        return render_template(
            "view_note.html",
            title=note["title"],
            note=note,
            hash_id=note["hash_id"],
            password_required=False,
            content_html=simple_markdown_to_html(note["content"]),
            content_raw=note["content"],
            created_at=format_created_at(note["created_at"]),
            expiry_text=expiry_label(note["expiry"]),
        )

    def import_note_redirect(note):
        return redirect(url_for("index", import_id=note["hash_id"]))

    @app.get("/style.css")
    def stylesheet():
        from flask import send_from_directory

        response = make_response(
            send_from_directory(app.root_path, "style.css", mimetype="text/css")
        )
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if request.method == "GET":
            if current_user():
                return redirect(safe_next_url(request.args.get("next")))
            return render_template(
                "login.html",
                error_message=None,
                active_form=request.args.get("mode", "login"),
                next_url=request.args.get("next", ""),
            )

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db().cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT id, username, password_hash FROM users WHERE username = %s",
                (username,),
            )
            user = cursor.fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            return (
                render_template(
                    "login.html",
                    error_message="Invalid username or password.",
                    active_form="login",
                    next_url=request.form.get("next", ""),
                    username_input=username,
                ),
                401,
            )

        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        return redirect(safe_next_url(request.form.get("next")))

    @app.post("/signup")
    def signup():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or len(username) > 50 or len(password) < 8:
            return (
                render_template(
                    "login.html",
                    error_message="Enter a username of up to 50 characters and a password of at least 8 characters.",
                    active_form="signup",
                    next_url="",
                    username_input=username,
                ),
                400,
            )

        password_hash = generate_password_hash(password)
        with get_db().cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (%s, %s)
                ON CONFLICT (username) DO NOTHING
                RETURNING id, username
                """,
                (username, password_hash),
            )
            user = cursor.fetchone()
        if user is None:
            return (
                render_template(
                    "login.html",
                    error_message="That username is already in use.",
                    active_form="signup",
                    next_url="",
                    username_input=username,
                ),
                409,
            )

        get_db().commit()
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        user = current_user()
        if user is None:
            return redirect(url_for("login"))

        imported = None
        import_id = request.args.get("import_id", "")
        if import_id:
            note = fetch_note(import_id)
            if note is not None and user_can_access(note, user):
                authorized = not note["password_hash"] or (
                    session.pop("authorized_import", None) == note["hash_id"]
                )
                if authorized:
                    reject_if_expired(note)
                    imported = {
                        "title": f"{note['title']} (Copy)",
                        "content": note["content"],
                        "category": note["category"],
                    }

        return render_template(
            "index.html",
            title="Create note",
            active_tab="create",
            imported=imported,
            error_message=None,
        )

    @app.post("/create")
    @login_required
    def create_note():
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "")
        category = request.form.get("category", "")
        expiry = request.form.get("expiry", "")
        note_password = request.form.get("password", "")
        is_public = request.form.get("is_public") == "1"

        if (
            not title
            or len(title) > 200
            or not content.strip()
            or len(content) > MAX_NOTE_CONTENT_LENGTH
            or category not in CATEGORIES
            or expiry not in EXPIRY_OPTIONS
            or (note_password and len(note_password) < 8)
        ):
            return (
                render_template(
                    "index.html",
                    title="Create note",
                    active_tab="create",
                    imported={
                        "title": title,
                        "content": content,
                        "category": category if category in CATEGORIES else "Personal",
                    },
                    error_message=(
                        "Add a title and content, choose a valid category and expiration, "
                        "and use at least 8 characters for a note password."
                    ),
                ),
                400,
            )

        password_hash = generate_password_hash(note_password) if note_password else None
        connection = get_db()
        for _attempt in range(5):
            hash_id = "".join(
                secrets.choice(string.ascii_lowercase + string.digits)
                for _ in range(HASH_ID_LENGTH)
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO notes
                        (hash_id, user_id, title, content, category, expiry,
                         password_hash, is_public)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (hash_id) DO NOTHING
                    RETURNING hash_id
                    """,
                    (
                        hash_id,
                        g.current_user["id"],
                        title,
                        content,
                        category,
                        expiry,
                        password_hash,
                        is_public,
                    ),
                )
                created = cursor.fetchone()
            if created:
                connection.commit()
                return redirect(url_for("view_note", hash_id=hash_id))

        raise RuntimeError("Could not allocate a unique note identifier.")

    @app.get("/library")
    @login_required
    def library():
        category = request.args.get("category", "all")
        if category not in ("all", *CATEGORIES):
            abort(400, description="Choose a valid note category.")
        with get_db().cursor(cursor_factory=RealDictCursor) as cursor:
            if category == "all":
                cursor.execute(
                    """
                    SELECT id, hash_id, title, content, category, expiry,
                           password_hash, is_public, views, created_at
                    FROM notes
                    WHERE user_id = %s
                      AND NOT (
                          (expiry = '1h' AND created_at <= NOW() - INTERVAL '1 hour')
                          OR (expiry = '24h' AND created_at <= NOW() - INTERVAL '24 hours')
                      )
                    ORDER BY created_at DESC
                    """,
                    (g.current_user["id"],),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, hash_id, title, content, category, expiry,
                           password_hash, is_public, views, created_at
                    FROM notes
                    WHERE user_id = %s AND category = %s
                      AND NOT (
                          (expiry = '1h' AND created_at <= NOW() - INTERVAL '1 hour')
                          OR (expiry = '24h' AND created_at <= NOW() - INTERVAL '24 hours')
                      )
                    ORDER BY created_at DESC
                    """,
                    (g.current_user["id"], category),
                )
            notes = cursor.fetchall()
        return render_template(
            "library.html",
            title="My library",
            active_tab="library",
            notes=notes,
            category_filter=category,
        )

    @app.get("/explore")
    def explore():
        category = request.args.get("category", "all")
        if category not in ("all", *CATEGORIES):
            abort(400, description="Choose a valid note category.")
        with get_db().cursor(cursor_factory=RealDictCursor) as cursor:
            if category == "all":
                cursor.execute(
                    """
                    SELECT notes.hash_id, notes.title,
                           CASE WHEN COALESCE(notes.password_hash, '') = ''
                                     AND notes.expiry <> 'burn'
                                THEN notes.content ELSE '' END AS content,
                           (COALESCE(notes.password_hash, '') <> '') AS password_protected,
                           notes.expiry, notes.category, notes.views, notes.created_at,
                           users.username AS author
                    FROM notes
                    LEFT JOIN users ON notes.user_id = users.id
                    WHERE notes.is_public = TRUE
                      AND NOT (
                          (notes.expiry = '1h' AND notes.created_at <= NOW() - INTERVAL '1 hour')
                          OR (notes.expiry = '24h' AND notes.created_at <= NOW() - INTERVAL '24 hours')
                      )
                    ORDER BY notes.created_at DESC
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT notes.hash_id, notes.title,
                           CASE WHEN COALESCE(notes.password_hash, '') = ''
                                     AND notes.expiry <> 'burn'
                                THEN notes.content ELSE '' END AS content,
                           (COALESCE(notes.password_hash, '') <> '') AS password_protected,
                           notes.expiry, notes.category, notes.views, notes.created_at,
                           users.username AS author
                    FROM notes
                    LEFT JOIN users ON notes.user_id = users.id
                    WHERE notes.is_public = TRUE AND notes.category = %s
                      AND NOT (
                          (notes.expiry = '1h' AND notes.created_at <= NOW() - INTERVAL '1 hour')
                          OR (notes.expiry = '24h' AND notes.created_at <= NOW() - INTERVAL '24 hours')
                      )
                    ORDER BY notes.created_at DESC
                    """,
                    (category,),
                )
            notes = cursor.fetchall()
        return render_template(
            "explore.html",
            title="Explore notes",
            active_tab="explore",
            notes=notes,
            category_filter=category,
        )

    @app.route("/note/<hash_id>", methods=("GET", "POST"))
    def view_note(hash_id):
        note = fetch_note(hash_id)
        if note is None:
            abort(404)
        user = current_user()
        if not user_can_access(note, user):
            abort(403)
        reject_if_expired(note)

        if note["password_hash"]:
            if request.method == "POST":
                if not check_password_hash(
                    note["password_hash"], request.form.get("password", "")
                ):
                    return render_password_wall(hash_id, error=True), 401
            else:
                return render_password_wall(hash_id)

        return render_note(note)

    @app.route("/import/<hash_id>", methods=("GET", "POST"))
    @login_required
    def import_note(hash_id):
        note = fetch_note(hash_id)
        if note is None:
            abort(404)
        if not user_can_access(note, g.current_user):
            abort(403)
        reject_if_expired(note)

        if note["password_hash"]:
            if request.method == "GET":
                return render_password_wall(
                    hash_id, action=url_for("import_note", hash_id=hash_id)
                )
            if not check_password_hash(
                note["password_hash"], request.form.get("password", "")
            ):
                return (
                    render_password_wall(
                        hash_id,
                        error=True,
                        action=url_for("import_note", hash_id=hash_id),
                    ),
                    401,
                )
            session["authorized_import"] = hash_id

        return import_note_redirect(note)

    @app.post("/note/<hash_id>/delete")
    @login_required
    def delete_note(hash_id):
        with get_db().cursor() as cursor:
            cursor.execute(
                "DELETE FROM notes WHERE hash_id = %s AND user_id = %s RETURNING id",
                (hash_id, g.current_user["id"]),
            )
            deleted = cursor.fetchone()
            if deleted is None:
                cursor.execute("SELECT id FROM notes WHERE hash_id = %s", (hash_id,))
                exists = cursor.fetchone()
                if exists is None:
                    abort(404)
                abort(403)
        get_db().commit()
        return redirect(url_for("library"))

    return app


def simple_markdown_to_html(text):
    """Render a small, safe Markdown subset used by shared notes."""
    rendered = []
    paragraph = []
    list_items = []
    code_lines = []
    in_code = False

    def inline_format(value):
        value = html.escape(value)
        value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
        return re.sub(r"\*(.+?)\*", r"<em>\1</em>", value)

    def flush_paragraph():
        if paragraph:
            rendered.append(f"<p>{inline_format(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list():
        if list_items:
            rendered.append("<ul>")
            rendered.extend(f"<li>{inline_format(item)}</li>" for item in list_items)
            rendered.append("</ul>")
            list_items.clear()

    for line in text.splitlines():
        if line.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code:
                rendered.append(
                    f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>"
                )
                code_lines.clear()
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:])
            continue
        flush_list()
        if line.startswith("## "):
            flush_paragraph()
            rendered.append(f"<h2>{inline_format(line[3:])}</h2>")
        elif line.startswith("# "):
            flush_paragraph()
            rendered.append(f"<h1>{inline_format(line[2:])}</h1>")
        else:
            paragraph.append(line)

    if in_code:
        rendered.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(rendered)


def format_created_at(created_at):
    if isinstance(created_at, datetime):
        return created_at.strftime("%b %d, %Y")
    return str(created_at).split(" ")[0]


def expiry_label(expiry):
    return {
        "never": "Never expires",
        "burn": "Burn after reading",
        "1h": "Expires in 1 hour",
        "24h": "Expires in 24 hours",
    }.get(expiry, "Expiration unavailable")


def safe_next_url(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
