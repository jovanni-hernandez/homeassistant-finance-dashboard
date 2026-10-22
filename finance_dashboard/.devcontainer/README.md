# Devcontainer

Open this folder in a dev container to get a repeatable environment for:
- Python backend dependencies
- SQLAlchemy-based analysis scripts
- LiteLLM AI helpers
- frontend syntax checks

The container installs `backend/requirements.txt` on first start, includes Chromium/ChromeDriver for Selenium, and forwards port `8000` for the local app. The browser task runs the real FastAPI app with an isolated database.

Use the VS Code task picker for:
- Finance: Bootstrap
- Finance: Run backend
- Finance: Check backend Python
- Finance: Check frontend JS
- Finance: Build category defaults

The bootstrap task runs the common checks in sequence and is the easiest way to confirm the container is wired correctly.
