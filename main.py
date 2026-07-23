from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from api.files import router as files_router
from api.settings import router as settings_router
from api.webdav import router as webdav_router
from api.library import router as library_router

app = FastAPI(title="Listener")

app.include_router(files_router)
app.include_router(settings_router)
app.include_router(webdav_router)
app.include_router(library_router)

app.mount("/static", StaticFiles(directory="frontend"), name="static")

Path("data/uploads").mkdir(parents=True, exist_ok=True)
Path("data/cache").mkdir(parents=True, exist_ok=True)


@app.get("/")
def index():
    return FileResponse("frontend/index.html")
