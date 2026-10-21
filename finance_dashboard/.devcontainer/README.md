# Devcontainer

Open this folder in a dev container to get a repeatable environment for:
- Python backend dependencies
- SQLAlchemy-based analysis scripts
- LiteLLM AI helpers
- frontend syntax checks

The container installs `backend/requirements.txt` on first start and forwards port `8000` for the app.

Use the VS Code task picker for:
- Finance: Bootstrap
- Finance: Run backend
- Finance: Check backend Python
- Finance: Check frontend JS
- Finance: Build category defaults

The bootstrap task runs the common checks in sequence and is the easiest way to confirm the container is wired correctly.
