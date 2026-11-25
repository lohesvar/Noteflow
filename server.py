import os
import sys
import sqlite3
import random
import string
import urllib.parse
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- DATABASE SETUP ---
DB_FILE = "database.db"

def init_db():
    """Initializes SQLite database with schema.sql and creates user-connected notes if empty."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Read and execute schema
    if os.path.exists("schema.sql"):
        with open("schema.sql", "r", encoding="utf-8") as f:
            try:
                cursor.executescript(f.read())
            except sqlite3.OperationalError:
                # Tables already exist, proceed
                pass
    conn.commit()

    # Pre-populate with a demo user and mock notes if users table is empty
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        # Create a default developer account: 'admin' / 'admin123'
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", ("admin", "admin123"))
        admin_id = cursor.lastrowid
        
        mock_notes = [
            ("get-started", admin_id, "Getting Started with NoteFlow", 
             "# Getting Started with NoteFlow\n\nWelcome to NoteFlow, a premium note sharing platform! Here are some quick tips to get started:\n\n- **Create Notes**: Write down your thoughts, instructions, or code snippets in this editor.\n- **Categories**: Organize your library using categories like Personal, Work, Code, Ideas, or Journal.\n- **Security**: Toggle password protection in the sidebar settings to secure sensitive notes.\n- **Share**: Publish to the Public Feed or share the generated URL link with others.", 
             "Ideas", "never", "", 1),
            ("pitch-deck", admin_id, "Startup Pitch Deck Checklist", 
             "# Startup Pitch Deck Checklist\n\nImportant slides to include:\n\n- **Problem**: Clear pain points of target audience.\n- **Solution**: Your unique value proposition.\n- **Market Validation**: Real proof points and demographics.\n- **Business Model**: How you make money.", 
             "Ideas", "never", "", 1),
            ("workout", admin_id, "Weekly Workout Routine", 
             "# Weekly Workout Routine\n\nKeep active and healthy with this routine:\n\n- **Mon**: Push Day (Chest/Shoulders/Triceps)\n- **Wed**: Pull Day (Back/Biceps)\n- **Fri**: Leg Day (Quads/Hamstrings/Calves)\n- **Sun**: Light cardio & stretching", 
             "Personal", "never", "", 1),
            ("launch", admin_id, "Project Launch Checklist", 
             "# Project Launch Checklist\n\nTasks before shipping product release:\n\n- [ ] DNS propagation checks\n- [ ] CDN cache invalidation\n- [ ] Telemetry dashboard configured\n- [ ] Load testing validated", 
             "Work", "never", "", 1)
        ]
        cursor.executemany(
            "INSERT INTO notes (hash_id, user_id, title, content, category, expiry, password, is_public) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            mock_notes
        )
        conn.commit()
    conn.close()

# --- TEMPLATE ENGINE HELPER ---
def render_template(template_name, title, active_tab, context={}, logged_user=None):
    """Reads sub-template and base template, performing token replacement & injecting auth states."""
    template_path = os.path.join("templates", template_name)
    if not os.path.exists(template_path):
        return f"Template {template_name} not found."
        
    with open(template_path, "r", encoding="utf-8") as f:
        sub_content = f.read()

    # 1. If this is a standalone login page, render it directly without wrapping in base.html
    if template_name == "login.html":
        for key, val in context.items():
            sub_content = sub_content.replace(f"{{{{ {key} }}}}", str(val))
        return sub_content

    # 2. Otherwise, replace variables in the sub-template
    for key, val in context.items():
        sub_content = sub_content.replace(f"{{{{ {key} }}}}", str(val))

    # 3. Load base frame layout
    with open(os.path.join("templates", "base.html"), "r", encoding="utf-8") as f:
        base_content = f.read()

    # Determine which sidebar tab link should have the 'active' class
    active_tab_create = "active" if active_tab == "create" else ""
    active_tab_library = "active" if active_tab == "library" else ""
    active_tab_explore = "active" if active_tab == "explore" else ""

    # Build authentication sidebar links & username badge
    if logged_user:
        logged_username = logged_user
        auth_links = """
        <a href="/logout" class="nav-btn" style="color:var(--danger); border-top: 1px solid var(--border-color); margin-top: 24px;">
          <i data-lucide="log-out"></i>
          <span>Sign Out</span>
        </a>
        """
    else:
        logged_username = "Guest"
        auth_links = """
        <a href="/login" class="nav-btn" style="border-top: 1px solid var(--border-color); margin-top: 24px;">
          <i data-lucide="log-in"></i>
          <span>Sign In</span>
        </a>
        """

    # 4. Fill base template variables
    base_context = {
        "title": title,
        "content": sub_content,
        "active_tab_create": active_tab_create,
        "active_tab_library": active_tab_library,
        "active_tab_explore": active_tab_explore,
        "logged_username": logged_username,
        "auth_sidebar_links": auth_links
    }

    for key, val in base_context.items():
        base_content = base_content.replace(f"{{{{ {key} }}}}", str(val))

    return base_content

# --- BEGINNER-FRIENDLY MARKDOWN CONVERTER ---
def simple_markdown_to_html(text):
    """Converts basic Markdown headers, bold, italics, lists, and code blocks to HTML."""
    import html
    safe_text = html.escape(text)

    # Convert code blocks
    import re
    safe_text = re.sub(r'```css\n([\s\S]*?)```', r'<pre><code class="css">\1</code></pre>', safe_text)
    safe_text = re.sub(r'```([\s\S]*?)```', r'<pre><code>\1</code></pre>', safe_text)

    lines = safe_text.split("\n")
    in_list = False
    html_lines = []

    for line in lines:
        if line.startswith("<pre>") or line.startswith("<code>") or line.endswith("</pre>") or line.endswith("</code>"):
            html_lines.append(line)
            continue

        if line.startswith("## "):
            line = f"<h2>{line[3:]}</h2>"
        elif line.startswith("# "):
            line = f"<h1>{line[2:]}</h1>"
        elif line.startswith("- "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            line = f"<li>{line[2:]}</li>"
        else:
            if in_list and line.strip() != "":
                html_lines.append("</ul>")
                in_list = False
            if line.strip() == "":
                line = "<br>"
            else:
                line = f"<p>{line}</p>"
        
        line = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line)
        line = re.sub(r'\*(.*?)\*', r'<em>\1</em>', line)

        html_lines.append(line)

    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)

# --- SERVER REQUEST HANDLER ---
class NoteFlowHandler(BaseHTTPRequestHandler):

    def address_string(self):
        # Override to prevent slow DNS lookups (reverse lookup)
        return self.client_address[0]

    def log_message(self, format, *args):
        # Override to clean up stdout terminal logging outputs
        sys.stderr.write("%s - - [%s] %s\n" %
                         (self.address_string(),
                          self.log_date_time_string(),
                          format%args))

    def get_logged_user(self):
        """Helper to parse the Cookie header and find the logged-in username."""
        cookie_header = self.headers.get('Cookie', '')
        cookies = {}
        if cookie_header:
            for c in cookie_header.split(";"):
                if "=" in c:
                    k, v = c.strip().split("=", 1)
                    cookies[k] = v
        return cookies.get('session_user')

    def get_user_id(self, username):
        """Fetches the user database ID corresponding to a username."""
        if not username:
            return None
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def do_GET(self):
        """Handles page navigation and resource loading."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query)
        logged_user = self.get_logged_user()

        # Serve static stylesheet
        if path == "/style.css":
            if os.path.exists("style.css"):
                self.send_response(200)
                self.send_header("Content-Type", "text/css")
                self.end_headers()
                with open("style.css", "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "style.css not found")
            return

        # Route 1: Serve Login & Signup Screen
        if path == "/login":
            if logged_user:
                # If already logged in, redirect to home editor
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            
            context = {
                "error_display": "none",
                "error_message": ""
            }
            html = render_template("login.html", "Sign In", "", context, logged_user=None)
            self.wfile.write(html.encode("utf-8"))
            return

        # Route 2: Logout Trigger
        elif path == "/logout":
            self.send_response(303)
            # Clear Cookie by setting Max-Age=0
            self.send_header("Set-Cookie", "session_user=; Path=/; Max-Age=0")
            self.send_header("Location", "/login")
            self.end_headers()
            return

        # Route 3: Home page (Note Editor)
        elif path == "/":
            # AUTH GUARD: If not logged in, redirect to /login
            if not logged_user:
                self.send_response(303)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            # Check if there is an import query (for copying notes)
            import_title = query_params.get("import_title", [""])[0]
            import_content = query_params.get("import_content", [""])[0]
            import_category = query_params.get("import_category", [""])[0]
            
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            
            html = render_template("index.html", "Create Note", "create", {}, logged_user)
            # If cloning a note, populate parameters
            if import_title or import_content:
                html = html.replace('value="Welcome to NoteFlow"', f'value="{html_title_escape(import_title)}"')
                html = html.replace('># Getting Started with NoteFlow\n\nWelcome to NoteFlow, a premium note sharing platform! Here are some quick tips to get started:\n\n- **Create Notes**: Write down your thoughts, instructions, or code snippets in this editor.\n- **Categories**: Organize your library using categories like Personal, Work, Code, Ideas, or Journal.\n- **Security**: Toggle password protection in the sidebar settings to secure sensitive notes.\n- **Share**: Publish to the Public Feed or share the generated URL link with others.</textarea>', f'>{html_escape(import_content)}</textarea>')
                if import_category:
                    html = html.replace(f'value="{import_category}"', f'value="{import_category}" selected')
                    
            self.wfile.write(html.encode("utf-8"))
            return

        # Route 4: My Library List
        elif path == "/library":
            # AUTH GUARD
            if not logged_user:
                self.send_response(303)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            category_filter = query_params.get("category", ["all"])[0]
            user_id = self.get_user_id(logged_user)
            
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Show ONLY notes owned by this user
            query = "SELECT * FROM notes WHERE user_id = ?"
            params = [user_id]

            if category_filter != "all":
                query += " AND category = ?"
                params.append(category_filter)

            query += " ORDER BY created_at DESC"
            cursor.execute(query, params)
                
            notes = cursor.fetchall()
            conn.close()

            # Render notes cards
            cards_html = ""
            for note in notes:
                has_pw = '<span class="meta-item"><i data-lucide="lock" style="width:14px; height:14px;"></i></span>' if note['password'] else ""
                snippet = note['content'][:140] + "..." if len(note['content']) > 140 else note['content']
                snippet = html_escape(snippet)
                
                cards_html += f"""
                <div class="note-card glass-panel" onclick="window.location.href='/note/{note['hash_id']}'">
                  <div class="card-header">
                    <span class="category-badge category-{note['category'].lower()}">{note['category']}</span>
                    <span class="note-date">My Note</span>
                  </div>
                  <h3>{html_escape(note['title'])}</h3>
                  <p class="note-snippet">{snippet}</p>
                  <div class="card-footer">
                    <div class="card-meta">
                      <span class="meta-item"><i data-lucide="eye" style="width:14px; height:14px; margin-right:4px;"></i>{note['views']}</span>
                      {has_pw}
                    </div>
                    <div class="card-actions">
                      <a href="/note/{note['hash_id']}" class="action-btn" title="View Note" onclick="event.stopPropagation();"><i data-lucide="eye"></i></a>
                      <button class="action-btn" title="Copy Link" onclick="event.stopPropagation(); navigator.clipboard.writeText(window.location.origin + '/note/{note['hash_id']}'); alert('Link copied!');"><i data-lucide="share-2"></i></button>
                      <form action="/note/{note['hash_id']}/delete" method="POST" style="display:inline;" onclick="event.stopPropagation();">
                        <button type="submit" class="action-btn" title="Delete Note" style="color:var(--danger); border-color:transparent; background:none;" onclick="return confirm('Delete this note?');"><i data-lucide="trash-2"></i></button>
                      </form>
                    </div>
                  </div>
                </div>
                """

            empty_display = "flex" if not notes else "none"
            grid_display = "grid" if notes else "none"

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            
            context = {
                "notes_count": len(notes),
                "notes_list": cards_html,
                "empty_state_display": empty_display,
                "grid_display": grid_display,
                "filter_all_selected": "selected" if category_filter == "all" else "",
                "filter_Personal_selected": "selected" if category_filter == "Personal" else "",
                "filter_Work_selected": "selected" if category_filter == "Work" else "",
                "filter_Code_selected": "selected" if category_filter == "Code" else "",
                "filter_Ideas_selected": "selected" if category_filter == "Ideas" else "",
                "filter_Journal_selected": "selected" if category_filter == "Journal" else ""
            }
            html = render_template("library.html", "My Library", "library", context, logged_user)
            self.wfile.write(html.encode("utf-8"))
            return

        # Route 5: Explore Feed (Public notes accessible to anyone)
        elif path == "/explore":
            category_filter = query_params.get("category", ["all"])[0]
            
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT notes.*, users.username FROM notes LEFT JOIN users ON notes.user_id = users.id WHERE is_public = 1"
            params = []

            if category_filter != "all":
                query += " AND category = ?"
                params.append(category_filter)

            query += " ORDER BY created_at DESC"
            cursor.execute(query, params)
                
            notes = cursor.fetchall()
            conn.close()

            # Render public feed cards
            cards_html = ""
            for note in notes:
                author = note['username'] if note['username'] else "Anonymous"
                snippet = note['content'][:140] + "..." if len(note['content']) > 140 else note['content']
                snippet = html_escape(snippet)
                
                cards_html += f"""
                <div class="note-card glass-panel" onclick="window.location.href='/note/{note['hash_id']}'">
                  <div class="card-header">
                    <span class="category-badge category-{note['category'].lower()}">{note['category']}</span>
                    <span class="note-date">Public</span>
                  </div>
                  <h3>{html_escape(note['title'])}</h3>
                  <p class="note-snippet">{snippet}</p>
                  <div class="card-footer">
                    <div class="card-meta">
                      <img src="https://api.dicebear.com/7.x/bottts/svg?seed={author}" class="avatar-xs" style="margin-right:6px;">
                      <span class="meta-author">{html_escape(author)}</span>
                      <span class="meta-item" style="margin-left:12px;"><i data-lucide="eye" style="width:14px; height:14px; margin-right:4px;"></i>{note['views']}</span>
                    </div>
                    <div class="card-actions">
                      <a href="/note/{note['hash_id']}" class="action-btn"><i data-lucide="eye"></i></a>
                    </div>
                  </div>
                </div>
                """

            empty_display = "flex" if not notes else "none"
            grid_display = "grid" if notes else "none"

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            
            context = {
                "notes_count": len(notes),
                "notes_list": cards_html,
                "empty_state_display": empty_display,
                "grid_display": grid_display,
                "filter_all_selected": "selected" if category_filter == "all" else "",
                "filter_Personal_selected": "selected" if category_filter == "Personal" else "",
                "filter_Work_selected": "selected" if category_filter == "Work" else "",
                "filter_Code_selected": "selected" if category_filter == "Code" else "",
                "filter_Ideas_selected": "selected" if category_filter == "Ideas" else "",
                "filter_Journal_selected": "selected" if category_filter == "Journal" else ""
            }
            html = render_template("explore.html", "Explore Feed", "explore", context, logged_user)
            self.wfile.write(html.encode("utf-8"))
            return

        # Route 6: Single Note View
        elif path.startswith("/note/"):
            hash_id = path.split("/")[-1]
            
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM notes WHERE hash_id = ?", (hash_id,))
            note = cursor.fetchone()
            
            if not note:
                conn.close()
                self.send_error(404, "Note not found")
                return

            # SECURITY CHECK: If note is private, only the owner user can view it!
            if not note['is_public']:
                user_id = self.get_user_id(logged_user)
                if not user_id or note['user_id'] != user_id:
                    conn.close()
                    self.send_error(403, "Access Forbidden: This note is set to Private.")
                    return

            # Increment view counts
            cursor.execute("UPDATE notes SET views = views + 1 WHERE hash_id = ?", (hash_id,))
            conn.commit()
            conn.close()

            # If note has password protection, show the password wall
            if note['password']:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                
                context = {
                    "hash_id": hash_id,
                    "password_wall_display": "block",
                    "note_display": "none",
                    "error_display": "none"
                }
                html = render_template("view_note.html", "Protected Note", "library", context, logged_user)
                self.wfile.write(html.encode("utf-8"))
                return

            # Otherwise, render note immediately
            self.render_decrypted_note(note, logged_user)
            return

        # Route 7: Import Note Fork
        elif path.startswith("/import/"):
            # AUTH GUARD: Redirect to login if cloning notes
            if not logged_user:
                self.send_response(303)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            hash_id = path.split("/")[-1]
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM notes WHERE hash_id = ?", (hash_id,))
            note = cursor.fetchone()
            conn.close()

            if not note:
                self.send_error(404, "Note to import not found")
                return

            # Redirect to editor page with URL queries loaded
            query = urllib.parse.urlencode({
                "import_title": note['title'] + " (Copy)",
                "import_content": note['content'],
                "import_category": note['category']
            })
            self.send_response(303)
            self.send_header("Location", f"/?{query}")
            self.end_headers()
            return

        else:
            self.send_error(404, "Path not found")

    def do_POST(self):
        """Handles Note saving, password matching, user authentication forms, and deletion."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        logged_user = self.get_logged_user()

        # Parse form body
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(post_data)

        # Route 1: Login Submission
        if path == "/login":
            username = params.get('username', [''])[0].strip()
            password = params.get('password', [''])[0]

            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Simple plaintext matching for educational/beginner purposes
            cursor.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, password))
            user = cursor.fetchone()
            conn.close()

            if user:
                # Valid user! Set session cookie and redirect to home
                self.send_response(303)
                self.send_header("Set-Cookie", f"session_user={username}; Path=/; Max-Age=3600")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                # Invalid credentials. Render page with error.
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                
                context = {
                    "error_display": "flex",
                    "error_message": "Invalid username or password. Please try again."
                }
                html = render_template("login.html", "Sign In", "", context, logged_user=None)
                self.wfile.write(html.encode("utf-8"))
            return

        # Route 2: Signup Submission
        elif path == "/signup":
            username = params.get('username', [''])[0].strip()
            password = params.get('password', [''])[0]

            if not username or len(password) < 4:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                
                context = {
                    "error_display": "flex",
                    "error_message": "Password must be at least 4 characters long."
                }
                html = render_template("login.html", "Sign In", "", context, logged_user=None)
                # Redirect tabs to signup mode in JS script helper
                html = html.replace("urlParams.get('mode') === 'signup'", "true")
                self.wfile.write(html.encode("utf-8"))
                return

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # Check username uniqueness
            cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
            if cursor.fetchone():
                conn.close()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                
                context = {
                    "error_display": "flex",
                    "error_message": f"Username '{username}' is already taken."
                }
                html = render_template("login.html", "Sign In", "", context, logged_user=None)
                html = html.replace("urlParams.get('mode') === 'signup'", "true")
                self.wfile.write(html.encode("utf-8"))
                return

            # Insert new user record
            cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            conn.commit()
            conn.close()

            # Successfully registered! Automatically sign in and redirect
            self.send_response(303)
            self.send_header("Set-Cookie", f"session_user={username}; Path=/; Max-Age=3600")
            self.send_header("Location", "/")
            self.end_headers()
            return

        # Route 3: Create Note
        elif path == "/create":
            # AUTH GUARD
            if not logged_user:
                self.send_response(303)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            title = params.get('title', ['Untitled'])[0]
            content = params.get('content', [''])[0]
            category = params.get('category', ['Personal'])[0]
            expiry = params.get('expiry', ['never'])[0]
            password = params.get('password', [''])[0]
            is_public = 1 if 'is_public' in params else 0

            # Find logged-in user ID
            user_id = self.get_user_id(logged_user)

            # Generate unique hash_id
            hash_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

            # Save to SQLite notes table
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO notes (hash_id, user_id, title, content, category, expiry, password, is_public) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hash_id, user_id, title, content, category, expiry, password, is_public)
            )
            conn.commit()
            conn.close()

            # Redirect to created note detail page
            self.send_response(303)
            self.send_header("Location", f"/note/{hash_id}")
            self.end_headers()
            return

        # Route 4: Password check wall verify
        elif path.startswith("/note/") and not path.endswith("/delete"):
            hash_id = path.split("/")[-1]
            entered_pw = params.get('password', [''])[0]

            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM notes WHERE hash_id = ?", (hash_id,))
            note = cursor.fetchone()
            conn.close()

            if not note:
                self.send_error(404, "Note not found")
                return

            if note['password'] == entered_pw:
                # Password is correct! Serve note page decrypted.
                self.render_decrypted_note(note, logged_user)
            else:
                # Password incorrect. Show password wall again with error message.
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                
                context = {
                    "hash_id": hash_id,
                    "password_wall_display": "block",
                    "note_display": "none",
                    "error_display": "block"
                }
                html = render_template("view_note.html", "Protected Note", "library", context, logged_user)
                self.wfile.write(html.encode("utf-8"))
            return

        # Route 5: Delete Note Action
        elif path.startswith("/note/") and path.endswith("/delete"):
            # AUTH GUARD
            if not logged_user:
                self.send_response(303)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            hash_id = path.split("/")[-2]
            user_id = self.get_user_id(logged_user)

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # Security verification: Ensure the logged-in user owns the note!
            cursor.execute("SELECT user_id FROM notes WHERE hash_id = ?", (hash_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                self.send_error(404, "Note not found")
                return
            if row[0] != user_id:
                conn.close()
                self.send_error(403, "Access Forbidden: You are not the owner of this note.")
                return

            # Perform deletion
            cursor.execute("DELETE FROM notes WHERE hash_id = ?", (hash_id,))
            conn.commit()
            conn.close()

            # Redirect to library
            self.send_response(303)
            self.send_header("Location", "/library")
            self.end_headers()
            return

        else:
            self.send_error(404, "Invalid POST target")

    def render_decrypted_note(self, note, logged_user):
        """Helper to render a note once access verification passes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        
        # Format dates & expiration
        created_val = note['created_at'].split(" ")[0] if " " in note['created_at'] else note['created_at']
        expiry_text = "Never Expiring"
        if note['expiry'] == "burn":
            expiry_text = "Burn after reading"
        elif note['expiry'] == "1h":
            expiry_text = "Expires in 1 hour"
        elif note['expiry'] == "24h":
            expiry_text = "Expires in 24 hours"

        context = {
            "hash_id": note['hash_id'],
            "title": html_escape(note['title']),
            "category": note['category'],
            "category_lower": note['category'].lower(),
            "created_at": created_val,
            "views": note['views'],
            "expiry_text": expiry_text,
            "content_html": simple_markdown_to_html(note['content']),
            "content_raw": html_escape(note['content']),
            "password_wall_display": "none",
            "note_display": "block"
        }
        html = render_template("view_note.html", note['title'], "library", context, logged_user)
        self.wfile.write(html.encode("utf-8"))

# --- UTILITY ESCAPING METHODS ---
def html_escape(text):
    """Escapes standard characters for inner HTML slots."""
    import html
    return html.escape(text)

def html_title_escape(text):
    """Escapes quotes specifically for tag attribute values."""
    return text.replace('"', '&quot;').replace("'", "&#x27;")

# --- MAIN INVOCATION ---
class DualStackHTTPServer(HTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

def run(port=8000):
    init_db()
    server_address = ('', port)
    httpd = DualStackHTTPServer(server_address, NoteFlowHandler)
    print(f"==================================================")
    print(f"NoteFlow Server started successfully on port {port}")
    print(f"Open: http://localhost:{port} in your browser")
    print(f"==================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping NoteFlow server...")
        httpd.server_close()

if __name__ == '__main__':
    run()
