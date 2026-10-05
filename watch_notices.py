# -*- coding: utf-8 -*-
"""
watch_notices.py -- vigila los hechos de importancia de Latinex y avisa cuando
sale uno nuevo que mencione los emisores/palabras clave configurados.

Una pasada por ejecucion (pensado para un cron: GitHub Actions o el
Programador de tareas de Windows). Guarda las notas ya vistas en un archivo de
estado; la primera ejecucion solo registra lo existente, sin avisar.

    python watch_notices.py            # una pasada
    python watch_notices.py --dry      # muestra lo que avisaria, sin enviar
    python watch_notices.py --test     # envia una notificacion de prueba

Variables de entorno:
    WATCH_KEYWORDS  palabras a buscar en emisor o titulo (coma-separado; default ARROCHA)
    NTFY_TOPIC      si existe, push a https://ntfy.sh/<topic> (app ntfy en el celular)
    WATCH_TOAST=1   ademas, notificacion de escritorio de Windows (uso local)
    WATCH_STATE     archivo de estado (default data/watch_state.json)
"""

import json
import os
import subprocess
import sys
import unicodedata
from datetime import datetime
from urllib.parse import quote

import requests

FEED_URL = "https://www.latinexbolsa.com/emisor/hechos/relevantes"
PDF_BASE = "https://files.latinexbolsa.com/pabvprblob01/"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.getenv("WATCH_STATE", os.path.join(HERE, "data", "watch_state.json"))
KEYWORDS = [k.strip() for k in os.getenv("WATCH_KEYWORDS", "ARROCHA").split(",") if k.strip()]
HOT_WORDS = ["REDENCION", "EMISION", "OFERTA", "SERIE", "SUPLEMENTO", "PROSPECTO", "BONO"]


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).upper()


def fetch_matches():
    resp = requests.get(FEED_URL, timeout=60, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json", "Referer": "https://www.latinexbolsa.com/en/"})
    resp.raise_for_status()
    out = []
    for r in resp.json().get("data", []):
        text = _norm(f"{r.get('emisor')} {r.get('nombre')}")
        if any(_norm(k) in text for k in KEYWORDS):
            path = (r.get("path") or "").lstrip("/")
            out.append({"id": path or f"{r.get('registro')}|{r.get('nombre')}",
                        "date": r.get("registro", ""), "issuer": r.get("emisor", ""),
                        "title": r.get("nombre", ""),
                        "url": PDF_BASE + quote(path, safe="/") if path else ""})
    return out


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def viewer_url(pdf_url):
    """Latinex serves PDFs as application/octet-stream (forces a download, and
    Chrome on Android has no inline PDF viewer); Google's viewer renders them
    as a web page instead."""
    return "https://docs.google.com/viewer?embedded=true&url=" + quote(pdf_url, safe="")


def ntfy_topic():
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        try:
            with open(os.path.join(HERE, ".ntfy_topic"), encoding="utf-8") as f:
                topic = f.read().strip()
        except OSError:
            pass
    return topic


def already_sent(topic, message):
    """The GitHub Actions runner and the local backup keep separate state; ntfy's
    12h message cache is the shared record so the same filing is pushed once."""
    try:
        resp = requests.get(f"https://ntfy.sh/{topic}/json",
                            params={"poll": "1", "since": "12h"}, timeout=30)
        resp.raise_for_status()
        return any(json.loads(line).get("message") == message
                   for line in resp.text.splitlines() if line.strip())
    except (requests.RequestException, ValueError):
        return False


def notify_ntfy(topic, title, message, url="", hot=False):
    body = {"topic": topic, "title": title, "message": message,
            "priority": 5 if hot else 4, "tags": ["rotating_light" if hot else "bell"]}
    if url:
        body["click"] = viewer_url(url)
        body["actions"] = [{"action": "view", "label": "Ver PDF", "url": viewer_url(url)},
                           {"action": "view", "label": "Descargar", "url": url}]
    requests.post("https://ntfy.sh/", json=body, timeout=30).raise_for_status()


def notify_toast(title, message):
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x=$t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode('{title.replace(chr(39), '')}')) > $null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode('{message.replace(chr(39), '')}')) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'Latinex Watch').Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def send(item, dry=False):
    hot = any(w in _norm(item["title"]) for w in HOT_WORDS)
    title = f"Latinex: hecho de importancia - {item['issuer'][:40]}"
    message = f"{item['date']} | {item['title']}"
    print(f"[NUEVO{' - PRIORIDAD' if hot else ''}] {message}\n  {item['url']}", flush=True)
    if dry:
        return
    topic = ntfy_topic()
    if topic:
        if already_sent(topic, message):
            print("  (ya notificado por el otro vigilante; no se reenvia)", flush=True)
        else:
            notify_ntfy(topic, title, message, item["url"], hot)
    if os.getenv("WATCH_TOAST") == "1" and sys.platform.startswith("win"):
        notify_toast(title, message)


def main():
    dry = "--dry" in sys.argv
    if "--test" in sys.argv:
        # Re-send the most recent real matching filing, labeled as a test, so the
        # notification looks (and links) exactly like a live alert would.
        matches = fetch_matches()
        if matches:
            sample = dict(matches[0])
            sample["title"] = f"[PRUEBA] {sample['title']}"
        else:
            sample = {"date": datetime.now().strftime("%d/%m/%Y"), "issuer": "PRUEBA",
                      "title": "Notificacion de prueba del vigilante de Latinex", "url": ""}
        send(sample)
        return

    matches = fetch_matches()
    state = load_state()
    if state is None:
        save_state({"seen": [m["id"] for m in matches],
                    "initialized": datetime.now().isoformat(timespec="seconds")})
        print(f"Primera ejecucion: {len(matches)} hechos existentes registrados "
              f"({', '.join(KEYWORDS)}); se avisara solo de los nuevos.", flush=True)
        return

    seen = set(state.get("seen", []))
    new = [m for m in matches if m["id"] not in seen]
    for item in new:
        send(item, dry=dry)
    if not dry:
        state["seen"] = sorted(seen | {m["id"] for m in matches})
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
    print(f"Chequeo OK: {len(matches)} hechos coinciden, {len(new)} nuevos.", flush=True)


if __name__ == "__main__":
    if sys.stdout is None:  # pythonw (Task Scheduler): no console
        os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(HERE, "data", "watch.log"), "a",
                                       encoding="utf-8")
        print(f"--- {datetime.now().isoformat(timespec='seconds')}", flush=True)
    main()
