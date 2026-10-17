#!/usr/bin/env python3
"""
sources.json এ থাকা প্রতিটি JSON লিংক থেকে আলাদা আলাদা .m3u প্লেলিস্ট বানায়।
শুধু Python-এর বিল্ট-ইন লাইব্রেরি ব্যবহার করা হয়েছে, তাই কিছু install করতে হয় না।

বিশেষ ক্ষমতা:
  * ভাঙা JSON (যেমন  "id": ,  বা শেষে বাড়তি কমা) নিজে ঠিক করে পড়ে
  * ক্যাটেগরির ভেতরে ক্যাটেগরি (nested) থাকলেও সব চ্যানেল তোলে
  * 𝗜𝘀𝗹𝗮𝗺𝗶𝗰 এর মতো বিশেষ ফন্টের অক্ষর সাধারণ অক্ষরে বদলায়
  * ক্যাটেগরি না থাকলে নাম দেখে নিজে গ্রুপ ঠিক করে (auto_group)
  * referer / user_agent থাকলে প্লেলিস্টে যোগ করে
  * "encrypted": true হলে Television-style AES-CBC ডিক্রিপ্ট করে (pycryptodome)
  * GitHub Gist লিংক হলে CDN ক্যাশ এড়িয়ে সরাসরি Gist API থেকে সবসময় সর্বশেষ কনটেন্ট আনে
  * "type": "smiptv" হলে SM IPTV 3-step API flow হ্যান্ডেল করে
    (GET exp/ → POST token → GET channels with Bearer token)
    এবং DRM (Widevine/ClearKey) kid+key সহ KODIPROP লেখে
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = Path(os.environ.get("SOURCES_FILE", ROOT / "sources.json"))
OUT_DIR = Path(os.environ.get("OUT_DIR", ROOT / "playlists"))

# JSON-এ ফিল্ডের নাম যা-ই হোক, এই তালিকা থেকে খুঁজে নেবে (ছোট/বড় হাতের অক্ষর কোনো ব্যাপার না)
CANDIDATES = {
    "name":  ["name", "title", "channel_name", "channelname", "channel", "label", "chname", "n"],
    "url":   ["url", "stream_url", "streamurl", "stream", "link", "source", "src", "m3u8",
              "hls", "hls_url", "play_url", "playurl", "video_url", "videourl", "live_url", "streams", "u"],
    "logo":  ["logo", "logo_url", "logourl", "image", "icon", "thumbnail", "thumb", "poster", "img", "l"],
    "group": ["group", "group_title", "category", "category_name", "genre", "g"],
    "id":    ["id", "tvg_id", "channel_id", "channelid", "epg_id", "i"],
    "user_agent": ["user_agent", "useragent", "user-agent", "ua"],
    "referer":    ["referer", "referrer", "http_referrer", "origin"],
    "license_type": ["license_type", "licensetype", "drm_scheme", "drm"],
    "license_key":  ["license_key", "licensekey", "clearkey", "drm_license"],
}

# auto_group চালু থাকলে চ্যানেলের নামে এই শব্দ থাকলে সেই গ্রুপে যাবে (ওপর থেকে নিচে মিলিয়ে দেখে)
DEFAULT_GROUP_RULES = {
    "Islamic":    ["islamic", "quran", "quren", "peace tv", "madani", "azan", "iqra", "zainabia"],
    "Kids":       ["kids", "cartoon", "cn hd", "nick", "pogo", "doraemon", "tom and", "jungle book",
                   "wow kids", "moto patlo", "duronto", "দুরন্ত", "batul", "কার্টুন"],
    "Sports":     ["sport", "cricket", "dazn", "bein", "willow"],
    "Music":      ["music", "9xm", "8xm", "sangeet", "v2beat", "balle balle", "zoom", "yrf",
                   "dhoom", "beats", "hindi hits", "mastii"],
    "Documentary":["discovery", "animal planet", "national geo", "travel xp", "natur"],
    "Movies":     ["movi", "cinema", "b4u", "gold", "sony max", "sony pix", "star movies",
                   "cineplex", "bolly", "ultra", "pictures", "shemaroo", "sheemaroo",
                   "bhojpuri", "jhojpuri", "rkd studio", "manoranjan"],
    "News":       ["news", "somoy", "jamuna", "ekattor", "dbc", "independent", "akhone", "ekhon",
                   "channel 24", "sa tv", "saotv", "tbn 24", "jago"],
}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- AES ডিক্রিপ্ট (Television-style)
# sources.json-এ  "encrypted": true  থাকলে এই ফাংশন কাজ করবে।
# pycryptodome না থাকলে workflow-এ  pip install pycryptodome  চালাতে হবে।
_AES_KEY = b"T3l3v1s10n_S3cr3t_K3y_2026_@ppX".ljust(32, b"\x00")

def decrypt_television(raw_bytes: bytes) -> str:
    """
    Base64 decode → প্রথম 16 বাইট IV → বাকিটা AES-256-CBC ciphertext → UTF-8 text।
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError:
        raise RuntimeError(
            "pycryptodome ইনস্টল নেই। workflow-এ 'pip install pycryptodome' যোগ করুন।"
        )
    enc = base64.b64decode(raw_bytes)
    iv, ct = enc[:16], enc[16:]
    try:
        plain = unpad(AES.new(_AES_KEY, AES.MODE_CBC, iv).decrypt(ct), 16)
    except Exception:
        # Fallback: static IV
        static_iv = b"1234567890123456"
        plain = unpad(AES.new(_AES_KEY, AES.MODE_CBC, static_iv).decrypt(enc), 16)
    return plain.decode("utf-8")


# ---------------------------------------------------------------- ডাউনলোড
# ---------------------------------------------------------------- GitHub Gist (ক্যাশ-মুক্ত)
# সমস্যা: gist.githubusercontent.com/.../raw/<ফাইল> লিংক GitHub-এর CDN-এ ক্যাশ হয়ে থাকে,
# তাই Actions রান সফল হলেও অনেক সময় পুরনো কনটেন্ট আসে এবং প্লেলিস্ট আপডেট হয় না।
# সমাধান: Gist API (ক্যাশ হয় না) থেকে সর্বশেষ কনটেন্ট আনা; ব্যর্থ হলে ক্যাশ-বাস্টিং সহ raw লিংক।
_GIST_RAW_RE = re.compile(
    r"^https?://gist\.githubusercontent\.com/([^/]+)/([0-9a-fA-F]+)/raw"
    r"(?:/([0-9a-fA-F]{40}))?(?:/([^?#/]+))?/?(?:[?#].*)?$"
)


def _parse_gist_raw(url):
    """Gist raw লিংক হলে (gist_id, filename) দেয়। নির্দিষ্ট revision পিন করা থাকলে None।"""
    m = _GIST_RAW_RE.match(url.strip())
    if not m:
        return None
    _user, gist_id, rev, fname = m.groups()
    if rev and fname:  # /raw/<40-hex>/<file> -> ব্যবহারকারী ইচ্ছা করেই পুরনো revision পিন করেছেন
        return None
    if rev and not fname:  # /raw/<name> যেখানে নামটা দৈবক্রমে 40 hex
        fname = rev
    return gist_id, fname


def _http_get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_gist_via_api(gist_id, filename=None, timeout=30):
    """Gist API থেকে ফাইলের সর্বশেষ কনটেন্ট (bytes) আনে। ফেরত: (bytes, updated_at)।"""
    api = f"https://api.github.com/gists/{gist_id}"
    base = {
        "User-Agent": "M3U-Updater/1.0",
        "Accept": "application/vnd.github+json",
        "Cache-Control": "no-cache",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        hdr = dict(base)
        if token:
            hdr["Authorization"] = f"Bearer {token}"
        raw = _http_get(api, hdr, timeout)
    except urllib.error.HTTPError as e:
        if token and e.code in (401, 403):  # টোকেনে সমস্যা হলে টোকেন ছাড়া চেষ্টা
            raw = _http_get(api, base, timeout)
        else:
            raise
    meta = json.loads(raw.decode("utf-8"))
    files = meta.get("files") or {}
    if not files:
        raise RuntimeError("Gist-এ কোনো ফাইল নেই")
    f = files.get(filename) if filename else None
    if f is None:
        # ফাইলের নাম বদলে গেলেও যেন কাজ চলে: একটাই ফাইল থাকলে সেটা, নইলে প্রথমটা
        name0 = sorted(files)[0]
        if filename:
            log(f"  ⚠ Gist-এ '{filename}' নেই, '{name0}' ব্যবহার করা হচ্ছে")
        f = files[name0]
    content = f.get("content")
    if f.get("truncated") or content is None:
        content = _http_get(f["raw_url"], base, timeout).decode("utf-8", errors="replace")
    return content.encode("utf-8"), meta.get("updated_at", "?")


def fetch_text(url, user_agent=None, retries=3, timeout=30):
    """ইন্টারনেট থেকে ডাটা আনে। ফেল করলে ৩ বার চেষ্টা করে।"""
    headers = {"User-Agent": user_agent or "Mozilla/5.0 (compatible; M3U-Updater/1.0)"}
    gist = _parse_gist_raw(url)
    if gist:
        gist_id, fname = gist
        try:
            data, updated = fetch_gist_via_api(gist_id, fname, timeout)
            log(f"  📥 Gist API থেকে আনা হয়েছে (gist সর্বশেষ আপডেট: {updated}, {len(data)} বাইট, "
                f"sha1: {hashlib.sha1(data).hexdigest()[:10]})")
            return data
        except Exception as e:  # noqa: BLE001
            log(f"  ⚠ Gist API ব্যর্থ ({e}); ক্যাশ-বাস্টিং সহ raw লিংকে যাচ্ছি")
        headers.update({"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            target = url
            if gist:  # প্রতিবার আলাদা কোয়েরি -> CDN ক্যাশ হিট হবে না
                sep = "&" if "?" in url else "?"
                target = f"{url}{sep}_cb={int(time.time())}{attempt}"
            raw = _http_get(target, headers, timeout)
            if gist:
                log(f"  📥 raw লিংক থেকে আনা হয়েছে ({len(raw)} বাইট, "
                    f"sha1: {hashlib.sha1(raw).hexdigest()[:10]})")
            return raw  # bytes হিসেবে ফেরত দেওয়া হচ্ছে (encrypted সোর্সের জন্য)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  চেষ্টা {attempt}/{retries} ব্যর্থ: {e}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"ডাউনলোড ব্যর্থ: {last_err}")


def fetch_json_api(url, method="GET", headers=None, timeout=30):
    """JSON API endpoint থেকে ডাটা আনে।"""
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; M3U-Updater/1.0)")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ================================================================
# SM IPTV — বিশেষ 3-step API flow
# ================================================================
def process_smiptv(src):
    """
    SM IPTV এর ৩ ধাপের API flow:
      1) GET {config_url}  →  { "url": "https://worker-url/" }
      2) POST {worker}/token  →  { "token": "..." }
      3) GET  {worker}/channels  (Authorization: Bearer <token>)
         →  { "categories": [ { "name": "...", "channels": [...] } ] }

    প্রতিটি চ্যানেলে থাকতে পারে:
      stream_url, logo, referer, user_agent, origin, cookie, kid, key
    DRM চ্যানেলে kid+key থাকলে KODIPROP:clearkey লেখা হয়।
    """
    name       = src["name"]
    slug       = src["slug"]
    config_url = src["config_url"]
    out_file   = OUT_DIR / f"{slug}.m3u"
    log(f"▶ {name} (SM IPTV) -> {out_file.name}")

    # Step 1 — Worker URL
    log("  ➤ Step 1: config URL থেকে worker URL নেওয়া হচ্ছে...")
    cfg = fetch_json_api(config_url)
    worker_url = cfg.get("url", "").rstrip("/") + "/"
    if not worker_url.startswith("https://"):
        raise RuntimeError(f"Worker URL ভুল বা পাওয়া যায়নি: {cfg}")
    log(f"  ✓ Worker: {worker_url}")

    # Step 2 — Auth Token
    log("  ➤ Step 2: auth token নেওয়া হচ্ছে...")
    token_data = fetch_json_api(worker_url + "token", method="POST")
    token = token_data.get("token")
    if not token:
        raise RuntimeError(f"Token পাওয়া যায়নি: {token_data}")
    log("  ✓ Token পাওয়া গেছে")

    # Step 3 — Channel list
    log("  ➤ Step 3: চ্যানেল ডেটা লোড হচ্ছে...")
    data = fetch_json_api(
        worker_url + "channels",
        headers={"Authorization": f"Bearer {token}"}
    )
    categories = data.get("categories", [])
    if not categories:
        raise RuntimeError("কোনো ক্যাটাগরি/চ্যানেল পাওয়া যায়নি")

    # Build M3U
    app_name = data.get("app_name", name)
    tg       = data.get("telegram_channel", "")
    lines = [
        "#EXTM3U",
        f"#PLAYLIST:{app_name}",
        f"# Source: SM IPTV  |  {tg}",
        f"# Auto-generated — do not edit manually",
        "",
    ]
    seen  = set()
    total = 0
    drm_count = 0

    for cat in categories:
        cat_name = clean(cat.get("name") or "General")
        for ch in cat.get("channels") or []:
            stream_url = (ch.get("stream_url") or ch.get("streamUrl") or "").strip()
            ch_name    = clean(ch.get("name") or "Unknown")
            if not stream_url or not is_http(stream_url):
                continue
            key_pair = (ch_name.lower(), stream_url)
            if key_pair in seen:
                continue
            seen.add(key_pair)

            ch_id  = ch.get("id") or ""
            logo   = (ch.get("logo") or "").replace('"', "%22").replace(" ", "%20")
            ua     = ch.get("user_agent") or ch.get("userAgent") or ""
            ref    = ch.get("referer") or ""
            origin = ch.get("origin") or ""
            cookie = ch.get("cookie") or ""
            kid    = ch.get("kid") or ""
            ck_key = ch.get("key") or ""

            # #EXTINF
            attrs = []
            if ch_id and not str(ch_id).isdigit():
                attrs.append(f'tvg-id="{clean(str(ch_id))}"')
            attrs.append(f'tvg-name="{ch_name}"')
            if logo:
                attrs.append(f'tvg-logo="{logo}"')
            attrs.append(f'group-title="{cat_name}"')

            lines.append(f'#EXTINF:-1 {" ".join(attrs)},{ch_name}')

            # HTTP headers (VLC/Kodi style)
            if ua:
                lines.append(f"#EXTVLCOPT:http-user-agent={ua}")
            if ref:
                lines.append(f"#EXTVLCOPT:http-referrer={ref}")
            if origin:
                lines.append(f"#EXTVLCOPT:http-origin={origin}")
            if cookie:
                lines.append(f"#EXTVLCOPT:http-cookie={cookie}")

            # DRM ClearKey
            if kid and ck_key:
                lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
                lines.append(f"#KODIPROP:inputstream.adaptive.license_key={kid}:{ck_key}")
                drm_count += 1

            lines.append(stream_url)
            lines.append("")
            total += 1

    if total == 0:
        raise RuntimeError("০টা চ্যানেল পাওয়া গেছে, পুরনো ফাইল অক্ষত রাখা হলো")

    content = "\n".join(lines) + "\n"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if out_file.exists() and out_file.read_text(encoding="utf-8") == content:
        log(f"  কোনো পরিবর্তন নেই ({total} চ্যানেল, {drm_count} DRM)")
    else:
        out_file.write_text(content, encoding="utf-8")
        log(f"  ✅ আপডেট হয়েছে ({total} চ্যানেল, {drm_count} DRM)")


# ---------------------------------------------------------------- ভাঙা JSON মেরামত
def repair_json(text):
    """
    স্ট্রিংয়ের বাইরে থাকা সাধারণ ভুল ঠিক করে:
      "id": ,        ->  "id": null,      (মান নেই)
      [1, 2, ]       ->  [1, 2]           (শেষে বাড়তি কমা)
      a,, b          ->  a, b             (দুইটা কমা)
    """
    out, in_str, esc, n, i = [], False, False, len(text), 0
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
        elif c in ",:":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            nxt = text[j] if j < n else ""
            if c == "," and nxt and nxt in "}],":
                pass  # বাড়তি কমা বাদ
            elif c == ":" and (not nxt or nxt in ",}]"):
                out.append(": null")
            else:
                out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


# "key": "value" জোড়া খোঁজার প্যাটার্ন। value-র শেষের " বাদ পড়ে গেলেও লাইনের শেষ পর্যন্ত ধরে নেয়।
_KV_RE = re.compile(
    r'"([^"\\\r\n]{1,40})"\s*:\s*'
    r'(?:"((?:[^"\\\r\n]|\\.)*)("?)'
    r'|(-?\d+(?:\.\d+)?)(?![\w.])'
    r'|(true|false|null)(?!\w))'
)


def _extract_fields(chunk):
    """ভাঙা একটা অংশ থেকে যতগুলো "key": "value" পাওয়া যায় তুলে আনে।"""
    rec = {}
    for m in _KV_RE.finditer(chunk):
        key = m.group(1)
        if m.group(2) is not None:
            val = m.group(2)
            if m.group(3) == "":  # শেষের " নেই -> শেষে লেগে থাকা , } ] ফেলে দাও
                val = val.rstrip(" \t,}]")
            try:
                val = json.loads('"' + val + '"', strict=False)  # \/ \uXXXX ইত্যাদি ঠিক করা
            except ValueError:
                pass
        elif m.group(4) is not None:
            val = m.group(4)
        else:
            val = None
        rec.setdefault(key, val)
    return rec


def salvage_channels(text, mapping):
    """
    পুরো JSON না পড়া গেলে শেষ ভরসা: প্রতিটা { থেকে আলাদা আলাদা অবজেক্ট পড়ার চেষ্টা করে।
    ভালো অবজেক্ট যেমন আছে তেমন নেয়; ভাঙা অবজেক্ট থেকে field গুলো তুলে আনে।
    লিংক (http...) নেই এমন কিছু নেয় না। ফেরত দেয়: (চ্যানেলের তালিকা, ভাঙা থেকে উদ্ধার হওয়া সংখ্যা)
    """
    dec = json.JSONDecoder(strict=False)
    starts = [m.start() for m in re.finditer(r"\{", text)]
    items, repaired, k, n = [], 0, 0, len(text)
    while k < len(starts):
        s = starts[k]
        try:
            obj, end = dec.raw_decode(text, s)
        except json.JSONDecodeError:
            obj, end = None, s
        if obj is not None:
            items.append(obj)
            while k < len(starts) and starts[k] < end:  # এই অবজেক্টের ভেতরের { গুলো বাদ
                k += 1
            continue
        nxt = starts[k + 1] if k + 1 < len(starts) else n
        rec = _extract_fields(text[s:nxt])
        if is_http(pick(rec, "url", mapping)):
            items.append(rec)
            repaired += 1
        k += 1
    return items, repaired


def lenient_json_loads(text, mapping=None):
    # strict=False: string-এর ভেতরে raw tab / newline / অন্য control character থাকলেও পড়বে
    # (ডিফল্টে Python এগুলোকে "Invalid control character" বলে reject করে)
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        err = e
    log("  ⚠ JSON-এ ভুল আছে, নিজে ঠিক করে পড়া হচ্ছে")
    try:
        return json.loads(repair_json(text), strict=False)
    except json.JSONDecodeError:
        pass
    # ভুলের জায়গাটা লগে দেখাও, যাতে সোর্সের কোথায় সমস্যা বোঝা যায়
    a, b = max(0, err.pos - 80), min(len(text), err.pos + 80)
    log(f"  🔎 ভুল: {err.msg} (char {err.pos}) — আশেপাশের লেখা: {text[a:b]!r}")
    items, repaired = salvage_channels(text, mapping or {})
    if not items:
        raise err
    log(f"  🛟 পুরো JSON পড়া যায়নি; তবুও {len(items)}টা এন্ট্রি উদ্ধার হয়েছে "
        f"({repaired}টা ভাঙা অবস্থা থেকে মেরামত করে)")
    return items


# ---------------------------------------------------------------- ফিল্ড বের করা
def to_text(value):
    """যেকোনো ভ্যালুকে সাধারণ টেক্সটে বদলায়।"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        for v in value:
            t = to_text(v)
            if t:
                return t
        return ""
    if isinstance(value, dict):
        for k in ("url", "link", "src", "name", "title"):
            for key, v in value.items():
                if str(key).lower() == k:
                    t = to_text(v)
                    if t:
                        return t
    return ""


def pick(item, field, mapping):
    lowered = {str(k).lower(): v for k, v in item.items()}
    keys = []
    if mapping.get(field):
        keys.append(mapping[field].lower())
    keys += CANDIDATES.get(field, [])
    for k in keys:
        if k in lowered:
            text = to_text(lowered[k])
            if text:
                return text
    return ""


def is_http(text):
    return text.lower().startswith(("http://", "https://", "rtmp://", "rtsp://"))


def clean(text):
    """বিশেষ ফন্ট -> সাধারণ অক্ষর, আর M3U ভাঙতে পারে এমন চিহ্ন সরায়।"""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text.replace('"', "'").replace(",", " ")).strip()


# ---------------------------------------------------------------- চ্যানেল সংগ্রহ
def collect(node, group, mapping, out, stats, is_root=True):
    """
    JSON ঘুরে ঘুরে চ্যানেল খোঁজে। ক্যাটেগরির (গ্রুপ) নাম মনে রেখে নিচের চ্যানেলকে দেয়।
    out-এ (চ্যানেল, গ্রুপ) জোড়া জমা হয়।
    """
    if isinstance(node, list):
        for it in node:
            collect(it, group, mapping, out, stats, is_root=False)
    elif isinstance(node, dict):
        url = pick(node, "url", mapping)
        if is_http(url):
            out.append((node, group))
            return
        keys = {str(k).lower() for k in node}
        url_keys = set(CANDIDATES["url"]) | ({mapping["url"].lower()} if mapping.get("url") else set())
        if keys & url_keys:
            stats["no_url"] += 1  # চ্যানেল আছে কিন্তু লিংক ফাঁকা
            return
        # এটা চ্যানেল নয়, ক্যাটেগরি/কন্টেইনার। সবচেয়ে উপরের স্তরের নাম গ্রুপ ধরা হয় না
        g = group
        if not is_root:
            own = ""
            for k, v in node.items():
                if str(k).lower() in ("name", "title") and isinstance(v, str) and v.strip():
                    own = v.strip()
                    break
            g = clean(own) or group
        for v in node.values():
            if isinstance(v, (list, dict)):
                collect(v, g, mapping, out, stats, is_root=False)


def auto_group_for(name, rules, default):
    lname = unicodedata.normalize("NFKC", name).lower()
    for group, words in rules.items():
        if any(w.lower() in lname for w in words):
            return group
    return default


# ---------------------------------------------------------------- M3U তৈরি
def build_m3u(entries, mapping, src):
    style = src.get("header_style", "extvlcopt")  # "extvlcopt" অথবা "pipe"
    use_auto = bool(src.get("auto_group"))
    rules = src.get("group_rules") or DEFAULT_GROUP_RULES
    default_group = src.get("default_group", "Entertainment")

    lines = ["#EXTM3U"]
    if src.get("name"):
        lines.append(f"#PLAYLIST:{clean(src['name'])}")
    seen = set()
    for i, (ch, inherited) in enumerate(entries, start=1):
        url = re.sub(r"\s+", "", pick(ch, "url", mapping))
        name = clean(pick(ch, "name", mapping)) or f"Channel {i}"
        key = (name.lower(), url)
        if key in seen:
            continue
        seen.add(key)

        attrs = []
        cid = pick(ch, "id", mapping)
        if cid and not cid.isdigit():
            attrs.append(f'tvg-id="{clean(cid)}"')
        attrs.append(f'tvg-name="{name}"')
        logo = pick(ch, "logo", mapping).replace('"', "%22").replace(" ", "%20")
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        group = clean(pick(ch, "group", mapping)) or inherited
        if not group and use_auto:
            group = auto_group_for(name, rules, default_group)
        if group:
            attrs.append(f'group-title="{group}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{name}')

        ua = pick(ch, "user_agent", mapping)
        ref = pick(ch, "referer", mapping)
        if style == "pipe":
            hdr = []
            if ref:
                hdr.append(f"Referer={ref}")
            if ua:
                hdr.append(f"User-Agent={ua}")
            if hdr:
                url = url + "|" + "&".join(hdr)
        else:
            if ua:
                lines.append(f"#EXTVLCOPT:http-user-agent={ua}")
            if ref:
                lines.append(f"#EXTVLCOPT:http-referrer={ref}")
        lt, lk = pick(ch, "license_type", mapping), pick(ch, "license_key", mapping)
        if lt and lk:
            lines.append(f"#KODIPROP:inputstream.adaptive.license_type={lt}")
            lines.append(f"#KODIPROP:inputstream.adaptive.license_key={lk}")
        lines.append(url)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- প্রধান কাজ (সাধারণ সোর্স)
def process_source(src):
    name, slug, url = src["name"], src["slug"], src["url"]
    mapping = src.get("mapping", {}) or {}
    out_file = OUT_DIR / f"{slug}.m3u"
    log(f"▶ {name} -> {out_file.name}")

    raw = fetch_text(url, user_agent=src.get("user_agent"))

    # ── Encrypted সোর্স (Television-style AES) ──────────────────────────────
    if src.get("encrypted"):
        log("  🔐 এনক্রিপ্টেড সোর্স, ডিক্রিপ্ট হচ্ছে…")
        text = decrypt_television(raw)
    else:
        text = raw.decode("utf-8-sig", errors="replace")

    if text.lstrip().startswith("#EXTM3U"):  # আগে থেকেই M3U হলে সরাসরি রাখবে
        content = text if text.endswith("\n") else text + "\n"
    else:
        try:
            data = lenient_json_loads(text, mapping)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSON পড়া যায়নি: {e}")
        entries, stats = [], {"no_url": 0}
        collect(data, None, mapping, entries, stats)
        if not entries:
            raise RuntimeError("কোনো চ্যানেল/স্ট্রিম লিংক পাওয়া যায়নি (mapping দিয়ে ফিল্ডের নাম বলে দিন)")
        if stats["no_url"]:
            log(f"  ℹ {stats['no_url']}টা চ্যানেলের লিংক ফাঁকা ছিল, বাদ দেওয়া হয়েছে")
        content = build_m3u(entries, mapping, src)

    count = content.count("#EXTINF")
    if count == 0:
        raise RuntimeError("০টা চ্যানেল পাওয়া গেছে, পুরনো ফাইল অক্ষত রাখা হলো")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if out_file.exists() and out_file.read_text(encoding="utf-8") == content:
        log(f"  কোনো পরিবর্তন নেই ({count} চ্যানেল)")
    else:
        out_file.write_text(content, encoding="utf-8")
        log(f"  ✅ আপডেট হয়েছে ({count} চ্যানেল)")


def main():
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    failed = []
    for src in sources:
        if src.get("enabled", True) is False:
            log(f"⏭ {src.get('name')} বন্ধ করা আছে, বাদ দেওয়া হলো")
            continue
        try:
            # SM IPTV এর জন্য আলাদা handler
            if src.get("type") == "smiptv":
                process_smiptv(src)
            else:
                process_source(src)
        except Exception as e:  # noqa: BLE001
            log(f"  ❌ ব্যর্থ: {e}")
            print(f"::error title={src.get('name')}::{e}")
            failed.append(src.get("name"))
    if failed:
        log(f"\nব্যর্থ সোর্স: {', '.join(map(str, failed))}")
        sys.exit(1)


if __name__ == "__main__":
    main()
