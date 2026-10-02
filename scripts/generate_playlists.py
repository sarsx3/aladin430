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
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = Path(os.environ.get("SOURCES_FILE", ROOT / "sources.json"))
OUT_DIR = Path(os.environ.get("OUT_DIR", ROOT / "playlists"))

# JSON-এ ফিল্ডের নাম যা-ই হোক, এই তালিকা থেকে খুঁজে নেবে (ছোট/বড় হাতের অক্ষর কোনো ব্যাপার না)
CANDIDATES = {
    "name": ["name", "title", "channel_name", "channelname", "channel", "label", "chname"],
    "url": ["url", "stream_url", "streamurl", "stream", "link", "source", "src", "m3u8",
            "hls", "hls_url", "play_url", "playurl", "video_url", "videourl", "live_url", "streams"],
    "logo": ["logo", "logo_url", "logourl", "image", "icon", "thumbnail", "thumb", "poster", "img"],
    "group": ["group", "group_title", "category", "category_name", "genre"],
    "id": ["id", "tvg_id", "channel_id", "channelid", "epg_id"],
    "user_agent": ["user_agent", "useragent", "user-agent", "ua"],
    "referer": ["referer", "referrer", "http_referrer", "origin"],
    "license_type": ["license_type", "licensetype", "drm_scheme", "drm"],
    "license_key": ["license_key", "licensekey", "clearkey", "drm_license"],
}

# auto_group চালু থাকলে চ্যানেলের নামে এই শব্দ থাকলে সেই গ্রুপে যাবে (ওপর থেকে নিচে মিলিয়ে দেখে)
DEFAULT_GROUP_RULES = {
    "Islamic": ["islamic", "quran", "quren", "peace tv", "madani", "azan", "iqra", "zainabia"],
    "Kids": ["kids", "cartoon", "cn hd", "nick", "pogo", "doraemon", "tom and", "jungle book",
             "wow kids", "moto patlo", "duronto", "দুরন্ত", "batul", "কার্টুন"],
    "Sports": ["sport", "cricket", "dazn", "bein", "willow"],
    "Music": ["music", "9xm", "8xm", "sangeet", "v2beat", "balle balle", "zoom", "yrf",
              "dhoom", "beats", "hindi hits", "mastii"],
    "Documentary": ["discovery", "animal planet", "national geo", "travel xp", "natur"],
    "Movies": ["movi", "cinema", "b4u", "gold", "sony max", "sony pix", "star movies",
               "cineplex", "bolly", "ultra", "pictures", "shemaroo", "sheemaroo",
               "bhojpuri", "jhojpuri", "rkd studio", "manoranjan"],
    "News": ["news", "somoy", "jamuna", "ekattor", "dbc", "independent", "akhone", "ekhon",
             "channel 24", "sa tv", "saotv", "tbn 24", "jago"],
}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- ডাউনলোড
def fetch_text(url, user_agent=None, retries=3, timeout=30):
    """ইন্টারনেট থেকে ডাটা আনে। ফেল করলে ৩ বার চেষ্টা করে।"""
    headers = {"User-Agent": user_agent or "Mozilla/5.0 (compatible; M3U-Updater/1.0)"}
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode("utf-8-sig", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  চেষ্টা {attempt}/{retries} ব্যর্থ: {e}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"ডাউনলোড ব্যর্থ: {last_err}")


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


def lenient_json_loads(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log("  ⚠ JSON-এ ভুল আছে, নিজে ঠিক করে পড়া হচ্ছে")
        return json.loads(repair_json(text))


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
        if cid and not cid.isdigit():  # শুধু সংখ্যার আইডি EPG-র কাজে লাগে না
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


# ---------------------------------------------------------------- প্রধান কাজ
def process_source(src):
    name, slug, url = src["name"], src["slug"], src["url"]
    mapping = src.get("mapping", {}) or {}
    out_file = OUT_DIR / f"{slug}.m3u"
    log(f"▶ {name} -> {out_file.name}")

    text = fetch_text(url, user_agent=src.get("user_agent"))

    if text.lstrip().startswith("#EXTM3U"):  # আগে থেকেই M3U হলে সরাসরি রাখবে
        content = text if text.endswith("\n") else text + "\n"
    else:
        try:
            data = lenient_json_loads(text)
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
            process_source(src)
        except Exception as e:  # noqa: BLE001
            # একটা সোর্স ফেল করলে বাকিগুলো চলবে, আর পুরনো প্লেলিস্ট মুছবে না
            log(f"  ❌ ব্যর্থ: {e}")
            print(f"::error title={src.get('name')}::{e}")
            failed.append(src.get("name"))
    if failed:
        log(f"\nব্যর্থ সোর্স: {', '.join(map(str, failed))}")
        sys.exit(1)


if __name__ == "__main__":
    main()
