"""Read new posts from public Telegram channels and send them to the TopTel24 site.

Telegram is filtered in Iran, so this small script runs somewhere outside the filter (a GitHub
Actions schedule, or any PC with a VPN) and the site, on its Iranian server, does the rest.
It uses Telegram's public channel preview (https://t.me/s/<channel>): no account, no API key.
Only the Python standard library is needed.

    APP_URL=https://toptel24.example FETCHER_TOKEN=... python telegram_fetch.py [--force]
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
FIRST_RUN_HOURS = 24           # a channel seen for the first time: only its posts from the last day

_WRAP = re.compile(r'<div class="tgme_widget_message_wrap')
_POST = re.compile(r'data-post="([^"/]+)/(\d+)"')
_PHOTO = re.compile(r'tgme_widget_message_photo_wrap[^>]*?background-image:url\(\'([^\']+)\'\)')
_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')
_DOC = re.compile(r'tgme_widget_message_document_title[^>]*>(.*?)</div>', re.S)


def _get(url, proxy=None, timeout=30):
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
        return r.read()


def _text(fragment):
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse_channel_page(page):
    """Posts on a t.me/s page, oldest first: {id, date, text, photos, documents}."""
    posts = []
    for block in _WRAP.split(page)[1:]:
        m = _POST.search(block)
        if not m:
            continue
        t = _TIME.search(block)
        text = _TEXT.search(block)
        posts.append({
            "id": int(m.group(2)),
            "date": t.group(1) if t else None,
            "text": _text(text.group(1)) if text else "",
            "photos": [html.unescape(u) for u in _PHOTO.findall(block)],
            "documents": [_text(d) for d in _DOC.findall(block)],
        })
    return posts


def new_posts(channel, last_post_id=0, proxy=None, now=None):
    """Posts newer than last_post_id (on a first visit: only the last FIRST_RUN_HOURS)."""
    channel = channel.strip().lstrip("@").removeprefix("https://t.me/").removeprefix("s/").strip("/")
    page = _get(f"https://t.me/s/{urllib.parse.quote(channel)}", proxy).decode("utf-8", "replace")
    posts = [p for p in parse_channel_page(page) if p["id"] > (last_post_id or 0)]
    if not last_post_id:
        since = (now or datetime.now(timezone.utc)) - timedelta(hours=FIRST_RUN_HOURS)
        posts = [p for p in posts if p["date"] and datetime.fromisoformat(p["date"]) >= since]
    return posts


def download(url, proxy=None):
    return _get(url, proxy, timeout=60)


# ---------------------------------------------------------------- sending to the site
def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for i, data in enumerate(files):
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="images"; filename="{i}.jpg"\r\n'
                   f'Content-Type: image/jpeg\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _api(app_url, token, path, body=None, content_type=None, timeout=300):
    req = urllib.request.Request(app_url.rstrip("/") + path, data=body,
                                 headers={"Authorization": f"Bearer {token}", "User-Agent": UA,
                                          **({"Content-Type": content_type} if content_type else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main(argv):
    app_url, token = os.environ["APP_URL"], os.environ["FETCHER_TOKEN"]
    proxy = os.environ.get("TELEGRAM_PROXY") or None
    force = "--force" in argv
    todo = _api(app_url, token, "/api/fetcher/channels" + ("?force=1" if force else ""))
    if not todo.get("due"):
        print("not time for a check yet")
        return 0
    failed = 0
    for ch in todo["channels"]:
        try:
            posts = new_posts(ch["channel"], ch.get("last_post_id"), proxy)
        except Exception as e:                     # one broken channel must not stop the others
            print(f"{ch['channel']}: could not read ({e})")
            failed += 1
            continue
        for p in posts:
            # text-only posts ("no sales today") are only reported so the site remembers it saw them
            images = [download(u, proxy) for u in p["photos"]]
            body, ctype = _multipart({"wholesaler": ch["id"], "post_id": p["id"], "date": p["date"] or "",
                                      "text": p["text"], "documents": "\n".join(p["documents"])}, images)
            r = _api(app_url, token, "/api/fetcher/posts", body, ctype)
            print(f"{ch['channel']}/{p['id']}: {r.get('result')}")
            time.sleep(1)
    return 1 if failed and failed == len(todo["channels"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
