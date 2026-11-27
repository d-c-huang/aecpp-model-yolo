"""Read film participation from saved runs without decoding video or blocking Tk."""
import json
from pathlib import Path
import threading
import time


def _read(path):
    try:
        value = path.read_text(encoding="utf-8")
        value = json.loads(value)
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


def learning_history(root, *, active=None, cancelled=None):
    root = Path(root)
    result = {}
    folders = {path for path in (root/"runs").glob("*") if path.is_dir()}
    for folder in sorted(folders,key=lambda path:path.name):
        if cancelled is not None and cancelled.is_set():
            break
        job = _read(root/"logs"/(folder.name+"-train.json"))
        complete = _read(folder/"completed.json")
        if (complete.get("kind") or job.get("kind"))!="movie-temporal":
            continue
        movie_ids = job.get("movie_ids")
        if not isinstance(movie_ids,list):
            movie_ids = [movie.get("id") for movie in _read(folder/"movie_sources.json").get("movies",[])]
        state = "completed" if complete else "running" if folder.name==active else job.get("status","unknown")
        if state=="running" and folder.name!=active:
            state = "unfinished"
        for movie_id in {value for value in movie_ids if type(value) is int and value>0}:
            entry = result.setdefault(movie_id,{"completed":0,"attempts":0,"last_state":"unknown","last_run":""})
            entry["attempts"] += 1
            entry["completed"] += int(state=="completed")
            entry.update(last_state=state,last_run=folder.name)
    return result


class MovieUsageIndex:
    def __init__(self,root,active=lambda:None):
        self.root,self.active = Path(root),active
        self.values = {}
        self.ready = False
        self.last_error = ""
        self.closed = threading.Event()
        self.worker = None
        self.next_read = 0.

    def snapshot(self):
        if not self.closed.is_set() and time.monotonic()>=self.next_read and not (self.worker and self.worker.is_alive()):
            self.next_read = time.monotonic()+5
            active = self.active()
            def read():
                try:
                    values = learning_history(self.root,active=active,cancelled=self.closed)
                    if not self.closed.is_set():
                        self.values = values
                        self.last_error = ""
                        self.ready = True
                except Exception as exc:
                    self.last_error = str(exc)
            self.worker = threading.Thread(target=read,daemon=True,name="movie-usage-index")
            self.worker.start()
        return self.values

    def close(self):
        self.closed.set()
