#!/usr/bin/env python3
"""Shared annotations server for film-matinee.

Stores annotations per-film in <root>/<film_id>/annotations.json.
Works alongside the main film-matinee server (no manifest required).
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cors_headers(allow_origin: str) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        headers["Access-Control-Allow-Headers"] = "Content-Type"
    return headers


def _json(data: Any, *, status: int = 200, allow_origin: str = "*") -> web.Response:
    return web.json_response(
        data,
        status=status,
        headers=_cors_headers(allow_origin),
        dumps=lambda v: json.dumps(v, ensure_ascii=False),
    )


def _err(message: str, *, status: int = 400, allow_origin: str = "*") -> web.Response:
    return _json({"error": message}, status=status, allow_origin=allow_origin)


def _safe_film_id(film_id: str) -> str:
    """Sanitize film_id to prevent path traversal."""
    if not film_id or ".." in film_id or "/" in film_id or "\\" in film_id:
        raise ValueError("invalid film_id")
    return film_id


def _annotations_path(root: Path, film_id: str) -> Path:
    film_dir = root / _safe_film_id(film_id)
    film_dir.mkdir(parents=True, exist_ok=True)
    return film_dir / "annotations.json"


def _read_annotations(root: Path, film_id: str) -> dict[str, Any]:
    path = _annotations_path(root, film_id)
    if not path.exists():
        return {"version": 1, "annotations": []}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "annotations": []}
    data.setdefault("version", 1)
    data.setdefault("annotations", [])
    return data


def _write_annotations(root: Path, film_id: str, data: dict[str, Any]) -> None:
    path = _annotations_path(root, film_id)
    tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


@contextmanager
def _annotations_lock(root: Path, film_id: str):
    lock_path = _annotations_path(root, film_id).with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    try:
        import fcntl
    except ImportError:
        import msvcrt
        fd = lock_path.open("a+b")
        try:
            fd.seek(0)
            if not fd.read(1):
                fd.write(b"\0")
                fd.flush()
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fd.close()
    else:
        fd = lock_path.open("w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()


def make_app(root: Path, *, allow_origin: str = "*") -> web.Application:
    app = web.Application(client_max_size=256 * 1024)

    async def http_root(request: web.Request) -> web.Response:
        return _json({
            "name": "film-matinee-notes",
            "root": str(root),
        })

    async def http_get_annotations(request: web.Request) -> web.Response:
        film_id = request.query.get("film", "")
        if not film_id:
            return _err("missing 'film' parameter")
        try:
            data = _read_annotations(root, film_id)
        except ValueError as e:
            return _err(str(e), status=403)
        return _json(data)

    async def http_post_note(request: web.Request) -> web.Response:
        body = await request.json()
        film_id = str(body.get("film_id", "")).strip()
        if not film_id:
            return _err("missing 'film_id'")
        text = str(body.get("text", "")).strip()
        if not text:
            return _err("note text is empty")
        chunk_index = int(body.get("chunk_index", -1))
        timecode = str(body.get("timecode", "")).strip()
        author = str(body.get("author", "Anonymous") or "Anonymous").strip()
        note = {
            "id": f"N{uuid.uuid4().hex[:8]}",
            "film_id": film_id,
            "chunk_index": chunk_index,
            "timecode": timecode,
            "author": author,
            "text": text,
            "created_at": _now(),
            "replies": [],
        }
        try:
            with _annotations_lock(root, film_id):
                data = _read_annotations(root, film_id)
                data.setdefault("annotations", []).append(note)
                _write_annotations(root, film_id, data)
        except ValueError as e:
            return _err(str(e), status=403)
        return _json(note, status=201)

    async def http_post_reply(request: web.Request) -> web.Response:
        note_id = request.match_info["note_id"]
        body = await request.json()
        film_id = str(body.get("film_id", "")).strip()
        if not film_id:
            return _err("missing 'film_id'")
        text = str(body.get("text", "")).strip()
        if not text:
            return _err("reply text is empty")
        author = str(body.get("author", "Anonymous") or "Anonymous").strip()
        try:
            with _annotations_lock(root, film_id):
                data = _read_annotations(root, film_id)
                for note in data.get("annotations", []):
                    if note.get("id") == note_id:
                        reply = {
                            "id": f"R{uuid.uuid4().hex[:8]}",
                            "author": author,
                            "text": text,
                            "created_at": _now(),
                        }
                        note.setdefault("replies", []).append(reply)
                        _write_annotations(root, film_id, data)
                        return _json(reply, status=201)
        except ValueError as e:
            return _err(str(e), status=403)
        return _err("note not found", status=404)

    async def http_options(request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_cors_headers(allow_origin))

    async def http_bilibili_subtitles(request: web.Request) -> web.Response:
        """Fetch B站 video subtitles via public API (server-side, no CORS issues)."""
        bvid = request.query.get("bvid", "").strip()
        if not bvid:
            return _err("missing 'bvid' parameter")
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                # Step 1: get cid + duration from bvid
                async with session.get(
                    f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                    headers=headers,
                ) as resp:
                    data = await resp.json()
                    if data.get("code") != 0:
                        return _json({"subtitles": [], "title": "", "duration": 0,
                                       "error": data.get("message", "API error")})
                    d = data["data"]
                    cid = d["cid"]
                    title = d.get("title", "")
                    duration = d.get("duration", 0)

                # Step 2: get subtitle URL list
                async with session.get(
                    f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}",
                    headers=headers,
                ) as resp:
                    data = await resp.json()
                    if data.get("code") != 0:
                        return _json({"subtitles": [], "title": title, "duration": duration,
                                       "error": "player API error"})
                    subs = (data.get("data", {}).get("subtitle", {}) or {}).get("subtitles", [])
                    if not subs:
                        return _json({"subtitles": [], "title": title, "duration": duration,
                                       "error": "no subtitles available"})

                # Prefer Chinese subtitle
                sub_url = None
                for s in subs:
                    if "zh" in s.get("lan", ""):
                        sub_url = s.get("subtitle_url", "")
                        break
                if not sub_url:
                    sub_url = subs[0].get("subtitle_url", "")
                if not sub_url:
                    return _json({"subtitles": [], "title": title, "duration": duration,
                                   "error": "no subtitle URL"})

                if sub_url.startswith("//"):
                    sub_url = "https:" + sub_url

                # Step 3: fetch subtitle JSON
                async with session.get(sub_url, headers=headers) as resp:
                    sub_data = await resp.json()
                    body = sub_data.get("body", [])
                    subtitles = [
                        {"start": item.get("from", 0), "end": item.get("to", 0),
                         "text": item.get("content", "")}
                        for item in body
                    ]
                    return _json({"subtitles": subtitles, "title": title, "duration": duration})
        except Exception as e:
            return _json({"subtitles": [], "title": "", "duration": 0, "error": str(e)})

    app.router.add_get("/", http_root)
    app.router.add_get("/annotations", http_get_annotations)
    app.router.add_post("/annotations", http_post_note)
    app.router.add_post("/annotations/{note_id}/replies", http_post_reply)
    app.router.add_get("/bilibili/subtitles", http_bilibili_subtitles)
    app.router.add_options("/{tail:.*}", http_options)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Shared annotations server for film-matinee.")
    parser.add_argument("--root", type=str, required=True, help="Root directory for films (e.g. /data/films)")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--allow-origin", default="*")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"[film-matinee-notes] WARNING: root {root} does not exist, creating...")
        root.mkdir(parents=True, exist_ok=True)

    print(
        f"[film-matinee-notes] root={root} bind={args.bind}:{args.port} "
        f"allow_origin={args.allow_origin!r}",
        flush=True,
    )
    web.run_app(
        make_app(root, allow_origin=args.allow_origin),
        host=args.bind,
        port=args.port,
        print=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
