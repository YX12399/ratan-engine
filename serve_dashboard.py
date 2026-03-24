"""
Quick patch: add dashboard serving to the running engine.
Import and call add_dashboard(app) after create_app().
"""
from fastapi.responses import HTMLResponse
from pathlib import Path

DASHBOARD_PATH = Path(__file__).parent / "dashboard" / "index.html"

def add_dashboard(app):
    @app.get("/dashboard", response_class=HTMLResponse)
    async def serve_dashboard():
        return DASHBOARD_PATH.read_text()
    
    @app.get("/", response_class=HTMLResponse) 
    async def root_redirect():
        return '<html><head><meta http-equiv="refresh" content="0;url=/dashboard"></head></html>'
