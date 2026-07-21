hindsight

Built for Gemma 4 hackathon

# Run

Streamlit (original):
```
streamlit run app.py
```

Native web app (Flask + HTML):
```
python web/server.py
```
Then open http://127.0.0.1:5000 to browse the three UI mockups.

## Native web app

`web/` is a native replacement for the Streamlit UI. The Python logic that lived
inside `app.py` now sits behind a JSON/SSE API (`web/api.py`, a Flask blueprint
wrapping `db.py`, `query.py`, and `capture_service.py`), and the frontend is
plain HTML/CSS/JS.

- `web/server.py` — Flask app; serves the mockups, `/assets`, `/screenshots`, and the API.
- `web/api.py` — `/api/*` endpoints: conversations CRUD, `/api/ask` (SSE token stream),
  settings, capture controls, data browser, clear history.
- `web/mockups/` — three design directions, all dark mode:
  - `/m/1` **Warm** — Claude-inspired warm charcoal, cozy bubbles, settings modal.
  - `/m/2` **Slate** — ChatGPT-inspired minimal neutral, flat rows, full settings page.
  - `/m/3` **Aurora** — modern glass, orange glow, floating composer, settings drawer.

Each mockup has the Hindsight wordmark (Instrument Serif) top-left, a conversation
sidebar with click/double-click-to-rename, and a user profile + settings gear at the
bottom-left.
