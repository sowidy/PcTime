"""Run with `python /path/to/server/run.py`; no repository-root imports."""
import config
import uvicorn


if __name__ == "__main__":
    uvicorn.run("app:app", host=config.HOST, port=config.PORT, workers=1)
