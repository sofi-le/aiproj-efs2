import csv, io, json, os, re, subprocess, tempfile, threading, time, traceback, wave
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from google import genai
from google.genai import types

import gcal
import wiz


client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                      http_options=types.HttpOptions(api_version="v1beta"))


CHAT_MODEL = "gemini-3.5-flash"
TTS_MODEL  = "gemini-2.5-flash-preview-tts"
VOICE      = "Kore"

HERE     = Path(__file__).parent
LOG_DIR  = HERE / "logs"
LOG_FILE = LOG_DIR / "log.jsonl"
LOG_DIR.mkdir(exist_ok=True)

SYSTEM = (
    "You are a hands-free voice AI assistant. Be brief and concrete. "
    "Answer in 1-3 short sentences meant to be heard, not read. "
    "No markdown, lists, or emojis. If the audio is unclear, say so briefly.\n"
    "Translation: if the user asks how to say something in another language, 'say' is only "
    "the translation, spoken in that language. If the speech itself is in a language other "
    "than English, 'say' is only its English translation.\n"
    "Notes: if the user asks you to log, note, record or remember something, kind is 'note', "
    "'heard' is the note written cleanly as one sentence, and 'say' is a short confirmation.\n"
    "Calendar: every note also goes on the user's calendar. For a note, fill in 'event': "
    "'title' (a few words), 'start' (local time as YYYY-MM-DDTHH:MM: the time the note is "
    "about if it mentions one, otherwise the current time) and 'minutes' (length, default 30). "
    "Requests like 'add lunch with Maya Thursday at noon' or 'remind me to...' are notes too.\n"
    "Light: the user has a smart light bulb. If they ask to turn it on or off, dim or "
    "brighten it, or change its colour, kind is 'light' and you fill in 'light': 'on' (true or "
    "false), 'brightness' (10 to 100), and either 'color' (hex like #FF8800) or 'kelvin' "
    "(2200 warm to 6500 cool) for white light. Only include what should change, plus 'on'. "
    "'say' is a short confirmation. For anything else, leave 'light' out.\n"
    "Safety: never state from a photo or a guess that something dangerous is safe. Give the "
    "general rule and tell them to check for themselves.\n"
    "Reply with these fields. 'heard': what the user said, transcribed (in the original "
    "language). 'say': what gets spoken back. 'show': the key number, word, or translation "
    "in at most 40 plain ASCII characters. 'kind': 'note', 'translation', 'light', or 'question'."
)
SYSTEM_PHOTO = (
    "\nA photo from the device's camera is attached: it shows what the user is looking at "
    "right now. Use it to answer the question."
)
REPLY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "heard": {"type": "STRING"},
        "say":   {"type": "STRING"},
        "show":  {"type": "STRING"},
        "kind":  {"type": "STRING", "enum": ["question", "translation", "note", "light"]},
        "event": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {
                "title":   {"type": "STRING"},
                "start":   {"type": "STRING"},
                "minutes": {"type": "INTEGER"},
            },
        },
        "light": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {
                "on":         {"type": "BOOLEAN"},
                "brightness": {"type": "INTEGER"},
                "color":      {"type": "STRING"},
                "kelvin":     {"type": "INTEGER"},
            },
        },
    },
    "required": ["heard", "say", "show", "kind"],
}

app = FastAPI()


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    (HERE / "last_error.txt").write_text(
        f"{datetime.now().isoformat(timespec='seconds')} {request.method} {request.url.path}\n"
        + "".join(traceback.format_exception(exc)))
    reason = f"{type(exc).__name__}: {exc}"[:300]
    print("ERROR:", reason)
    return JSONResponse({"detail": reason}, status_code=500)

device_last_seen = 0.0                    


def ask_llm(wav: bytes | None = None, jpg: bytes | None = None, text: str | None = None) -> dict:
    parts = []
    if wav:
        parts.append(types.Part.from_bytes(data=wav, mime_type="audio/wav"))
    if text:
        parts.append(types.Part.from_text(text=text))
    system = SYSTEM
    if jpg:
        parts.append(types.Part.from_bytes(data=jpg, mime_type="image/jpeg"))
        system += SYSTEM_PHOTO
    system += "\nCurrent local time: " + datetime.now().strftime("%Y-%m-%dT%H:%M (%A)") + "."
    light_now = wiz.state()
    if light_now:
        system += "\n" + light_now
    # Gemini sometimes cuts the JSON short: retry
    for attempt in range(3):
        r = client.models.generate_content(
            model=CHAT_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=REPLY_SCHEMA,
            ),
        )
        try:
            d = json.loads(r.text or "")
            say = str(d["say"]).strip()
            if say:
                return {
                    "heard": str(d.get("heard", "")).strip() or (text or ""),
                    "say": say,
                    "show": str(d.get("show", "")).strip(),
                    "kind": d.get("kind") if d.get("kind") in ("question", "translation", "note",
                                                               "light") else "question",
                    "light": d.get("light") if isinstance(d.get("light"), dict) else None,
                    "event": d.get("event") if isinstance(d.get("event"), dict) else None,
                }
        except (ValueError, KeyError, TypeError):
            pass
        reason = r.candidates[0].finish_reason if r.candidates else None
        print(f"Incomplete answer (try {attempt + 1}, finish_reason={reason}): {r.text!r}")
    return {"heard": text or "", "say": "Sorry, I didn't catch that.", "show": "Try again",
            "kind": "question", "light": None, "event": None}


def gemini_tts(text: str) -> bytes | None:
    r = client.models.generate_content(
        model=TTS_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                )
            ),
        ),
    )
    c = r.candidates[0] if r.candidates else None
    parts = c.content.parts if c and c.content and c.content.parts else []
    return next((p.inline_data.data for p in parts if p.inline_data and p.inline_data.data), None)


def mac_tts(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "say.wav")
        subprocess.run(["say", "-o", out, "--data-format=LEI16@24000", "--", text],
                       check=True, timeout=30)
        with wave.open(out, "rb") as w:
            return w.readframes(w.getnframes())


tts_blocked_until = 0.0


def tts(text: str) -> bytes:
    global tts_blocked_until
    if time.time() >= tts_blocked_until:
        for attempt in range(2):
            try:
                audio = gemini_tts(text)
            except Exception as e:
                print("Gemini speech failed:", str(e)[:160])
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    tts_blocked_until = time.time() + 600
                    break
                continue
            if audio:
                return audio
            print(f"Gemini speech returned no audio (try {attempt + 1})")
    print("Using the Mac's voice for this reply.")
    return mac_tts(text)


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    return buf.getvalue()


def ascii_header(text: str, limit: int) -> str:
    return " ".join(text.split()).encode("ascii", "ignore").decode()[:limit]


def read_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    entries = []
    for line in LOG_FILE.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def handle(source: str, wav: bytes | None = None, jpg: bytes | None = None,
           text: str | None = None) -> tuple[dict, bytes]:

    t0 = time.time()
    now = datetime.now()
    entry_id = now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"
    a = ask_llm(wav=wav, jpg=jpg, text=text)
    if a["kind"] == "light" and a["light"] and not wiz.apply(a["light"]):
        a["say"], a["show"] = "I couldn't reach the light.", "Light not found"
    on_calendar = []
    cal = None
    if a["kind"] == "note" and gcal.enabled():
        ev = a["event"] or {}
        cal = threading.Thread(target=lambda: on_calendar.append(gcal.add_event(
            str(ev.get("title") or a["heard"]), ev.get("start"), ev.get("minutes") or 30,
            description=a["heard"])))
        cal.start()
    pcm = tts(a["say"])
    if cal:
        cal.join(45)
    if wav:
        (LOG_DIR / f"{entry_id}.wav").write_bytes(wav)
    if jpg:
        (LOG_DIR / f"{entry_id}.jpg").write_bytes(jpg)
    (LOG_DIR / f"{entry_id}_reply.wav").write_bytes(pcm_to_wav(pcm))
    entry = {
        "id": entry_id,
        "time": now.isoformat(timespec="seconds"),
        "source": source,
        "kind": a["kind"],
        "heard": a["heard"],
        "say": a["say"],
        "show": a["show"],
        "light": a["light"] if a["kind"] == "light" else None,
        "calendar": bool(on_calendar and on_calendar[0]),
        "photo": bool(jpg),
        "audio": bool(wav),
        "seconds": round(time.time() - t0, 1),
    }
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{entry['kind']}] heard: {entry['heard']!r} -> {entry['say']!r} ({entry['seconds']} s)")
    return entry, pcm


@app.post("/ask")
async def ask(request: Request):
    global device_last_seen
    device_last_seen = time.time()
    body = await request.body()
    # body = WAV + optional JPEG; X-Wav-Bytes = length of the WAV
    n = int(request.headers.get("X-Wav-Bytes", len(body)))
    wav, jpg = body[:n], body[n:]
    entry, pcm = await run_in_threadpool(handle, "device", wav, jpg or None)
    device_last_seen = time.time()
    return Response(content=pcm, media_type="application/octet-stream",
                    headers={"X-Reply": ascii_header(entry["say"], 200)})  


@app.get("/test")
def test():
    pcm = tts("Server works. Your assistant is ready to talk.")
    return Response(content=pcm_to_wav(pcm), media_type="audio/wav")


@app.get("/")
def dashboard():
    return FileResponse(HERE / "dashboard.html", headers={"Cache-Control": "no-store"})


@app.get("/api/log")
def api_log():
    return {"entries": read_log(),
            "device_seconds_ago": round(time.time() - device_last_seen) if device_last_seen else None}


def delete_files(entry_id: str):
    for suffix in (".wav", ".jpg", "_reply.wav"):
        (LOG_DIR / f"{entry_id}{suffix}").unlink(missing_ok=True)


@app.delete("/api/log/{entry_id}")
def api_delete_entry(entry_id: str):
    if not re.fullmatch(r"[0-9-]+", entry_id):
        raise HTTPException(404)
    entries = read_log()
    kept = [e for e in entries if e["id"] != entry_id]
    if len(kept) == len(entries):
        raise HTTPException(404)
    LOG_FILE.write_text("".join(json.dumps(e) + "\n" for e in kept))
    delete_files(entry_id)
    return {"deleted": 1}


@app.delete("/api/log")
def api_delete_all():
    entries = read_log()
    for e in entries:
        if re.fullmatch(r"[0-9-]+", str(e.get("id", ""))):
            delete_files(e["id"])
    LOG_FILE.write_text("")
    return {"deleted": len(entries)}


@app.get("/media/{name}")
def media(name: str):
    if not re.fullmatch(r"[0-9A-Za-z_-]+\.(wav|jpg)", name):
        raise HTTPException(404)
    path = LOG_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/api/ask-text")
async def api_ask_text(request: Request):
    text = str((await request.json()).get("text", "")).strip()[:1000]
    if not text:
        raise HTTPException(400, "empty question")
    entry, _ = await run_in_threadpool(handle, "dashboard", None, None, text)
    return entry


@app.post("/api/report")
async def api_report(request: Request):
    day = str((await request.json()).get("day", "")) or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(400, "day must be YYYY-MM-DD")
    entries = [e for e in read_log() if e["time"].startswith(day)]
    if not entries:
        raise HTTPException(404, f"nothing logged on {day}")
    lines = [f"{e['time'][11:16]} [{e['kind']}{', photo' if e['photo'] else ''}] "
             f"user: {e['heard']} | assistant: {e['say']}" for e in entries]
    prompt = (
        f"This is the log of a wearable voice AI assistant on {day}. "
        "Write a short daily report in plain text with these sections: Summary, "
        "Notes logged (every 'note' entry, with its time), Questions asked, Translations, "
        "Follow-ups (anything that sounds unresolved or safety-related). "
        "Only use what is in the log; do not invent details. Leave out empty sections.\n\n"
        + "\n".join(lines)
    )
    r = await run_in_threadpool(lambda: client.models.generate_content(model=CHAT_MODEL,
                                                                       contents=prompt))
    report = (r.text or "").strip()
    (LOG_DIR / f"report_{day}.txt").write_text(report)
    return {"day": day, "report": report, "entries": len(entries)}


@app.get("/api/export.csv")
def api_export():
    buf = io.StringIO()
    cols = ["time", "source", "kind", "heard", "say", "show", "photo", "seconds", "id"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(read_log())
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="assistant_log.csv"'})
