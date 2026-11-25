# NoteFlow

NoteFlow is a Flask note-sharing application backed by PostgreSQL. The original
`server.py` and local `database.db` are retained as legacy project files; the
deployable application entry point is `app.py`.

## Local setup

1. Install Python 3.10 or newer and create a virtual environment.
2. Install project dependencies with `pip install -r requirements.txt`.
3. Configure `DATABASE_URL` and `SECRET_KEY` in your local environment (or in
   an ignored `.env` file). Generate a secret with
   `python -c "import secrets; print(secrets.token_hex(32))"`.
4. Run `schema.sql` once against the Supabase PostgreSQL database. The schema
   uses `password_hash` for both account and note passwords.
5. Start the app with `python app.py`; it listens on `http://127.0.0.1:8000`.

The app does not create database tables or demo accounts at startup. Set
`PGSSLMODE` only if your PostgreSQL provider requires a particular SSL mode;
the default is `prefer`.

## Vercel deployment

The root `app.py` is the Python serverless entry point. `vercel.json` routes
requests to Flask, including `/style.css`. Before deploying, add `DATABASE_URL`
and a long, random `SECRET_KEY` as Vercel environment variables for every
deployment environment you intend to use. Apply `schema.sql` to Supabase first.
Use a Supabase PostgreSQL connection string and keep it out of source control.

The existing local `database.db` is not used by the Flask app and is ignored by
Git.
