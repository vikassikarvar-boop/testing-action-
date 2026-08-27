#!/usr/bin/env python3
"""
ig_actions.py — Story VIEW + LIKE + COMMENT, real-app capture formats.
Session: cookies.json or cookies_duck.xxxx.json or cookies_badger.xxxx.json

v3.2: auto-binds account proxy from fingerprint/config, robust session parsing,
safe encoding, URL-to-MediaID resolver.

Formats 100% apne real captures se liye gaye hain:
  - /api/v2/media/seen/  : 3-part reel key + [taken_at_seen_at] value, gzip body
  - /api/v1/media/<id>/like/    : capture wala signed_body + d=0
  - /api/v1/media/<id>/comment/ : idempotence_token wala format
"""
import argparse
import base64
import gzip
import io
import json
import os
import random
import re
import sys
import time
import urllib.parse
import uuid
import zlib

import requests

# FIX-TLS: curl_cffi sends the REAL Instagram Android app's TLS/JA3/HTTP2
# fingerprint (OkHttp4/Conscrypt). Plain python-requests sends a famous bot
# JA3 + HTTP/1.1 -> detected at the handshake, before any header is read.
try:
    from curl_cffi import requests as _cc_requests
    _HAS_CURL_CFFI = True
except Exception:
    _HAS_CURL_CFFI = False

# ---- IG Android app TLS identity (OkHttp 4 / Android 10, Conscrypt) ----
# Verified live against tls.peet.ws echo:
#   python-requests  -> JA4 t13d1713h1_* (bot, HTTP/1.1)
#   this profile     -> JA4 t13d1512h2_* (OkHttp, HTTP/2)
# FIX-JA3-CFG: exact fingerprint config-able hai. JA3 account se NAHI,
# APP BINARY se aata hai — APK ek baar kholo (login NAHI chahiye), Wireshark
# me ClientHello copy karo, aur env vars me do:
#   IG_JA3="771,4865-...,0-23-...,29-23-24,0"
#   IG_AKAMAI="4:16777216|16711681|0|m,p,a,s"
IG_ANDROID_JA3 = os.environ.get("IG_JA3") or ",".join([
    "771",
    "4865-4866-4867-49195-49196-52393-49199-49200-52392-49171-49172-156-157-47-53",
    "0-23-65281-10-11-35-16-5-13-51-45-43-21",
    "29-23-24",
    "0",
])
IG_ANDROID_AKAMAI = os.environ.get("IG_AKAMAI",
                                   "4:16777216|16711681|0|m,p,a,s")
IG_ANDROID_EXTRA_FP = {
    "tls_signature_algorithms": [
        "ecdsa_secp256r1_sha256", "rsa_pss_rsae_sha256", "rsa_pkcs1_sha256",
        "ecdsa_secp384r1_sha384", "rsa_pss_rsae_sha384", "rsa_pkcs1_sha384",
        "rsa_pss_rsae_sha512", "rsa_pkcs1_sha512", "rsa_pkcs1_sha1",
    ],
    "tls_grease": False,
    "tls_permute_extensions": False,
}


class IGAndroidTlsSession:
    """Drop-in replacement for requests.Session that speaks with the
    Instagram Android app's OkHttp TLS/JA3 + HTTP/2 fingerprint.
    API-compatible subset used by this engine: get/post/cookies/proxies."""

    def __init__(self):
        self._s = _cc_requests.Session()
        self._fp = {"ja3": IG_ANDROID_JA3, "akamai": IG_ANDROID_AKAMAI,
                    "extra_fp": dict(IG_ANDROID_EXTRA_FP)}

    def _fp_kwargs(self, kw):
        for k, v in self._fp.items():
            kw.setdefault(k, v)
        return kw

    def get(self, url, **kw):
        return self._s.get(url, **self._fp_kwargs(kw))

    def post(self, url, **kw):
        return self._s.post(url, **self._fp_kwargs(kw))

    @property
    def cookies(self):
        return self._s.cookies

    @property
    def proxies(self):
        return getattr(self._s, "proxies", {})

    @proxies.setter
    def proxies(self, value):
        self._s.proxies = value

    def close(self):
        try:
            self._s.close()
        except Exception:
            pass

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://i.instagram.com"
TEMPLATES = json.load(open(os.path.join(HERE, "templates.json"), encoding="utf-8"))

SUPPORTED_CAPS = json.dumps([
    {"name": "SUPPORTED_SDK_VERSIONS",
     "value": ",".join(f"{v}.0" for v in range(149, 201))},
])

# Per-session connection UUID (NEVER shared across accounts - was a pool-wide leak)
# RUN_CONN_UUID removed; each Session now generates its own conn_uuid.

# Mapping of proxy country -> timezone offset (seconds). Cookie fingerprint has
# 'timezone_offset' already - we use that, falling back to this table by country.
COUNTRY_TZ_OFFSET = {
    "UK": 0, "GB": 0, "US": -18000, "CA": -18000, "AU": 36000, "IN": 19800,
    "SG": 28800, "DE": 3600, "FR": 3600, "NL": 3600, "JP": 32400, "BR": -10800,
    "AE": 14400, "ZA": 7200, "ES": 3600, "IT": 3600, "TR": 10800, "ID": 25200,
    "MX": -21600, "AR": -10800, "PL": 3600, "SE": 3600, "NO": 3600, "FI": 3600,
    "DK": 3600, "CH": 3600, "AT": 3600, "BE": 3600, "IE": 0, "PT": 0,
    "NZ": 46800, "HK": 28800, "KR": 32400, "TH": 25200, "VN": 25200,
    "MY": 28800, "PH": 28800, "RU": 10800, "UA": 10800, "RO": 7200,
    "GR": 7200, "IL": 7200, "SA": 10800, "EG": 7200, "NG": 3600,
}
# Default fallback if country not in table
DEFAULT_TZ_OFFSET = 0


def _gen_hpke_pubkey():
    """Fresh P-256 public key (65-byte uncompressed point, base64) per session.
    Uses the cryptography lib when available; otherwise emits a format-valid
    random point (0x04 prefix). Real app: new keypair every fresh install."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        pub = ec.generate_private_key(ec.SECP256R1()).public_key()
        raw = pub.public_bytes(serialization.Encoding.X962,
                               serialization.PublicFormat.UncompressedPoint)
        return base64.b64encode(raw).decode()
    except Exception:
        return base64.b64encode(b"\x04" + os.urandom(64)).decode()


class Session:
    def __init__(self, path, proxy_override=None):
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        if not os.path.exists(path):
            sys.exit(f"Cookies file not found: {path}")

        d = json.load(open(path, encoding="utf-8"))
        self.username = d.get("username", "")
        mob = d.get("mobile_api_session", {})
        sess_data = mob.get("session_data", {}) if isinstance(mob, dict) else {}
        
        # 1. Authorization Bearer token
        self.auth = d.get("authorization") or sess_data.get("Authorization") or sess_data.get("authorization")
        
        # 2. Cookies
        self.cookies = d.get("cookies", {})
        self.uid = str(self.cookies.get("ds_user_id", ""))
        self.mid = str(self.cookies.get("mid", ""))
        # FIX-RUR: Extract ONLY the 3-letter routing cluster prefix from rur cookie.
        # The full rur cookie value is e.g. "NCG,40899756504,1819025527:01fffb..."
        # Instagram's mobile app sends ONLY "NCG" in ig-u-rur header.
        _rur_full = str(self.cookies.get("rur", "") or "")
        self.rur_cluster = _rur_full.split(",")[0].strip() if _rur_full else "ODN"

        self.country = str(d.get("country", "") or "").upper()
        fp = d.get("fingerprint", {}) or {}
        self.proxy_country = str(fp.get("proxy_country", "") or self.country or "").upper()

        # FIX #4: timezone offset derived from proxy country / cookie fingerprint
        tz_from_fp = fp.get("timezone_offset")
        try:
            self.tz_offset = int(tz_from_fp) if tz_from_fp is not None else COUNTRY_TZ_OFFSET.get(self.proxy_country, DEFAULT_TZ_OFFSET)
        except Exception:
            self.tz_offset = COUNTRY_TZ_OFFSET.get(self.proxy_country, DEFAULT_TZ_OFFSET)
        
        # 3. Device identifiers + FIX-UA + FIX-LOCALE:
        # Build User-Agent and locale from the account's OWN device_info — NOT from templates.json.
        dev = d.get("device") or {}
        tmpl = TEMPLATES["mobileconfig"]["headers"]
        idents = mob.get("identifiers", {}) if isinstance(mob, dict) else {}
        uuids = idents.get("uuids", [])
        dev_uuid = uuids[0] if isinstance(uuids, list) and len(uuids) > 0 else (uuids.get("device_id") if isinstance(uuids, dict) else "")
        fam_uuid = uuids[1] if isinstance(uuids, list) and len(uuids) > 1 else (uuids.get("family_device_id") if isinstance(uuids, dict) else "")

        self.android_id = dev.get("android_id") or idents.get("android_id") or tmpl.get("x-ig-android-id", "")
        self.device_uuid = dev.get("device_uuid") or dev_uuid or tmpl.get("x-ig-device-id", str(uuid.uuid4()))
        self.family_uuid = dev.get("family_uuid") or fam_uuid or tmpl.get("x-ig-family-device-id", str(uuid.uuid4()))

        # FIX-UA: Build dynamic User-Agent from each account's real device_info.
        # Format: Instagram {app_version} Android ({api_level}/{android_version}; {dpi}; {resolution}; {manufacturer}; {model}; {device_codename}; {cpu}; {locale}; {build_number})
        di = mob.get("device_info", {}) if isinstance(mob, dict) else {}
        app_version   = di.get("app_version")   or "400.0.0.49.68"
        api_level     = di.get("api_level")     or "29"
        android_ver   = di.get("android_version") or "10"
        dpi           = di.get("dpi")           or "420dpi"
        resolution    = di.get("resolution")    or "1080x2400"
        manufacturer  = di.get("manufacturer")  or "Xiaomi"
        model         = di.get("model")         or "2201117SY"
        device_code   = di.get("device_codename") or model  # fallback to model if no codename
        cpu           = di.get("cpu")           or "qcom"
        build_number  = di.get("build_number")  or "799297105"
        # FIX-LOCALE: use account's real locale (e.g. en_GB for UK accounts)
        self.locale   = di.get("locale")        or "en_GB"
        # HTTP accept-language format: en-GB (hyphen, not underscore)
        self.accept_lang = self.locale.replace("_", "-")
        self.user_agent = (
            f"Instagram {app_version} Android "
            f"({api_level}/{android_ver}; {dpi}; {resolution}; "
            f"{manufacturer}; {model}; {device_code}; {cpu}; {self.locale}; {build_number})"
        )
        # Store all device fields for the identity export
        self._device_info_snapshot = {
            "app_version": app_version, "api_level": api_level,
            "android_version": android_ver, "dpi": dpi, "resolution": resolution,
            "manufacturer": manufacturer, "model": model, "device_codename": device_code,
            "cpu": cpu, "build_number": build_number, "locale": self.locale,
        }

        # 4. Proxy auto-binding
        self.proxy = proxy_override or d.get("proxy") or fp.get("proxy_url")

        if not self.auth or not self.uid:
            sys.exit(f"{path} me authorization/ds_user_id nahi mila")

        # FIX-TLS: use OkHttp/Android TLS fingerprint when available.
        if _HAS_CURL_CFFI:
            self.s = IGAndroidTlsSession()
            self.transport = "curl_cffi (OkHttp/JA3 impersonation)"
        else:
            self.s = requests.Session()
            self.transport = "PLAIN requests (BOT TLS fingerprint!) - pip install curl_cffi"
            print("  !! WARNING: curl_cffi not installed -> sending python-requests TLS fingerprint")
        self.s.cookies.update(self.cookies)
        if self.proxy:
            self.s.proxies = {"http": self.proxy, "https": self.proxy}

        self.nav = []
        self.pigeon = f"UFS-{uuid.uuid4()}-0"
        # FIX #1: per-session connection UUID (was module-level RUN_CONN_UUID shared by all accounts)
        self.conn_uuid = uuid.uuid4().hex
        # FIX #3: random per-session connection type (WIFI or mobile data)
        self.conn_type = random.choice(["WIFI", "4G", "LTE", "3G"])
        # FCM push token (per-session stable, realistic format)
        self.fcm_token = (base64.urlsafe_b64encode(os.urandom(8)).rstrip(b"=").decode()
                          + ":APA91b"
                          + base64.urlsafe_b64encode(os.urandom(112)).rstrip(b"=").decode())
        # FIX-CORR-1: per-session x-fb-session-id (was a hardcoded constant shared
        # by ALL accounts -> instant cross-account correlation fingerprint).
        # Real format: nid=<random token>;nc=1;fc=1;bc=0;
        self.fb_session_id = (
            "nid=" + base64.b64encode(os.urandom(9)).rstrip(b"=").decode()
            + ";nc=1;fc=1;bc=0;"
        )
        
        # FIX-MEDIA: cumulative bandwidth counters (real app sends these on
        # every API call — capture values: ~6153 kbps, growing totals)
        self.bw_bytes = random.randint(1_500_000, 6_000_000)
        self.bw_time_ms = random.randint(400, 1500)
        # FIX-TELEMETRY: per-session QPL sequence counter + carrier
        self.qpl_seq = random.randint(40, 150)
        uk_carriers = ["EE", "Vodafone", "Three", "O2"]
        self.carrier = random.choice(uk_carriers) if self.conn_type != "WIFI" else ""
        # FIX-HPKE: real app generates a FRESH P-256 HPKE keypair per install
        # for push registration. Engine had ONE hardcoded constant for ALL
        # accounts -> correlation fingerprint. Now per-session.
        self.hpke_pubkey = _gen_hpke_pubkey()

        # Write per-account identity file immediately on session creation
        self._export_identity_file()

    # ------------------------------------------------------------ identity export
    def _export_identity_file(self):
        """Write a per-account device identity snapshot to device_identity/ folder.
        This file lets you instantly verify what device/locale/headers each account uses."""
        out_dir = os.path.join(HERE, "device_identity")
        os.makedirs(out_dir, exist_ok=True)
        safe_name = re.sub(r'[^\w.-]', '_', self.username)
        out_path = os.path.join(out_dir, f"identity_{safe_name}.json")
        data = {
            "username": self.username,
            "uid": self.uid,
            "mid": self.mid,
            "rur_cluster_sent": self.rur_cluster,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "accept_language": self.accept_lang,
            "timezone_offset": self.tz_offset,
            "country": self.country,
            "proxy_country": self.proxy_country,
            "android_id": self.android_id,
            "device_uuid": self.device_uuid,
            "family_uuid": self.family_uuid,
            "device_info": self._device_info_snapshot,
            "headers_snapshot": {
                "ig-u-rur": self.rur_cluster,
                "user-agent": self.user_agent,
                "accept-language": self.accept_lang,
                "x-ig-app-locale": self.locale,
                "x-ig-device-locale": self.locale,
                "x-ig-mapped-locale": self.locale,
                "x-ig-timezone-offset": str(self.tz_offset),
                "x-ig-android-id": self.android_id,
                "x-ig-device-id": self.device_uuid,
                "x-ig-family-device-id": self.family_uuid,
            },
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------ headers
    def base_headers(self, friendly, endpoint_hint=None):
        h = dict(TEMPLATES["mobileconfig"]["headers"])
        h.pop("content-type", None)
        h["authorization"] = self.auth
        h["ig-intended-user-id"] = self.uid
        h["ig-u-ds-user-id"] = self.uid
        # FIX-RUR: send ONLY the 3-letter cluster prefix, NOT the full cookie string
        h["ig-u-rur"] = self.rur_cluster
        # FIX-MEDIA: bandwidth headers — real app reports cumulative usage
        self.bw_bytes += random.randint(40_000, 180_000)
        self.bw_time_ms += random.randint(150, 900)
        h["x-ig-bandwidth-speed-kbps"] = f"{random.uniform(2500, 8500):.3f}"
        h["x-ig-bandwidth-totalbytes-b"] = str(self.bw_bytes)
        h["x-ig-bandwidth-totaltime-ms"] = str(self.bw_time_ms)
        h["accept-encoding"] = "gzip"
        h["accept"] = "*/*"
        h["priority"] = "u=3"
        h["x-mid"] = self.mid
        h["x-ig-android-id"] = self.android_id
        h["x-ig-device-id"] = self.device_uuid
        h["x-ig-family-device-id"] = self.family_uuid
        h["x-fb-http-engine"] = "Tigon/MNS/TCP"
        h["x-fb-conn-uuid-client"] = self.conn_uuid
        h["x-fb-client-ip"] = "True"
        h["x-fb-server-cluster"] = "True"
        h["x-fb-connection-type"] = self.conn_type
        h["x-ig-connection-type"] = self.conn_type
        h["x-ig-timezone-offset"] = str(self.tz_offset)
        h["x-fb-friendly-name"] = friendly
        h["x-fb-request-analytics-tags"] = json.dumps(
            {"network_tags": {"product": "567067343352427", "surface": "undefined",
                              "request_category": "api", "purpose": "fetch",
                              "retry_attempt": "0"}}, separators=(",", ":"))
        h["x-pigeon-session-id"] = self.pigeon
        h["x-pigeon-rawclienttime"] = f"{time.time():.3f}"
        h["x-tigon-is-retry"] = "False"
        nav = ",".join(self.nav[-8:]) if self.nav else ""
        if nav:
            h["x-ig-nav-chain"] = nav
        if endpoint_hint:
            h["x-ig-client-endpoint"] = endpoint_hint
        # FIX-UA: Override template's hardcoded User-Agent with account's real device UA
        h["user-agent"] = self.user_agent
        # FIX-LOCALE: Override template's hardcoded en_US with account's real locale
        h["accept-language"] = self.accept_lang
        h["x-ig-app-locale"] = self.locale
        h["x-ig-device-locale"] = self.locale
        h["x-ig-mapped-locale"] = self.locale
        return h

    def push_nav(self, frag, surface):
        self.nav.append(f"{frag}:{surface}:{len(self.nav)+1}:user_action:{time.time():.3f}::")

    # ------------------------------------------------------------ helpers
    def signed_body(self, obj):
        return "SIGNATURE." + json.dumps(obj, separators=(",", ":"))

    def post_form(self, path, obj, friendly, extra_form=None, gzip_body=False,
                  extra_headers=None):
        h = self.base_headers(friendly)
        h["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        form = {"signed_body": self.signed_body(obj)}
        if extra_form:
            form.update(extra_form)
        body = urllib.parse.urlencode(form, quote_via=urllib.parse.quote).encode()
        if gzip_body:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                gz.write(body)
            body = buf.getvalue()
            h["content-encoding"] = "gzip"
        if extra_headers:
            h.update(extra_headers)
        r = self.s.post(BASE + path, headers=h, data=body, timeout=30)
        return r

    def get(self, path, friendly, params=None):
        h = self.base_headers(friendly)
        return self.s.get(BASE + path, headers=h, params=params, timeout=30)


# ================================================================= HELPERS
def extract_shortcode(url_or_code: str) -> str:
    """Extract Instagram shortcode from URL or string."""
    if not url_or_code:
        return None
    url_or_code = url_or_code.strip()
    match = re.search(r'/(?:p|reel|tv|share/p)/([A-Za-z0-9_-]+)', url_or_code)
    if match:
        return match.group(1)
    if re.match(r'^[A-Za-z0-9_-]{5,20}$', url_or_code):
        return url_or_code
    return None


def shortcode_to_media_id(shortcode: str) -> str:
    """Convert Instagram shortcode to numerical Media ID / PK."""
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    media_id = 0
    for char in shortcode:
        idx = alphabet.find(char)
        if idx == -1:
            return None
        media_id = (media_id * 64) + idx
    return str(media_id)


def url_to_media_id(url_or_code: str) -> str:
    """Converts any Instagram post/reel URL directly to media integer ID."""
    shortcode = extract_shortcode(url_or_code)
    if not shortcode:
        return None
    return shortcode_to_media_id(shortcode)


# ================================================================= ACTIONS
def verify(sess, label=""):
    r = sess.get(f"/api/v1/users/{sess.uid}/info/", "IgApi: users/info/")
    ok = r.status_code == 200 and sess.username in r.text
    flag = ""
    for pat, msg in (("challenge_required", "CHALLENGE!"),
                     ("accounts/suspended", "SUSPENDED!"),
                     ("login_required", "SESSION EXPIRED")):
        if pat in r.text:
            flag = " *** " + msg
    status_text = "[OK]" if ok else "[FAIL]"
    print(f"[verify {label:6s}] HTTP {r.status_code} -> {status_text}{flag}")
    return ok and not flag


def cold_start(sess, verbose=False):
    """Real app-open sequence — fullaction.mitm cold start (0-15s window) se."""
    calls = oks = 0
    t_start = time.time()

    def step(method, path, friendly, obj=None, form=None, params=None):
        nonlocal calls, oks
        time.sleep(random.uniform(0.2, 0.8))
        try:
            if method == "GET":
                r = sess.get(path, friendly, params=params)
            elif obj is not None:
                r = sess.post_form(path, obj, friendly)
            else:
                h = sess.base_headers(friendly)
                h["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                body = urllib.parse.urlencode(form, quote_via=urllib.parse.quote).encode()
                r = sess.s.post(BASE + path, headers=h, data=body, timeout=30)
            calls += 1
            ok = r.status_code == 200
            oks += ok
            if verbose:
                print(f"   cold {r.status_code} {path}")
        except Exception as e:
            if verbose:
                print(f"   cold ERR {path}: {e}")

    uid, duuid, fduuid = sess.uid, sess.device_uuid, sess.family_uuid

    step("POST", "/api/v1/android_modules/download/", "IgApi: android_modules/download/",
         obj={"_uid": uid, "_uuid": duuid,
              "hashes": ["0c63422693eec3941bf5b6ae23aabe38c62928c656d7da081dca668910df00be"]})
    step("GET", "/api/v1/live/pre_live_tools/", "IgApi: live/pre_live_tools/")
    step("GET", "/api/v1/creatives/get_unlockable_sticker_nux/", "IgApi: creatives/get_unlockable_sticker_nux/")
    step("GET", "/api/v1/clips/user/share_to_fb_config/", "IgApi: clips/user/share_to_fb_config/")
    step("GET", "/api/v1/users/reel_settings/", "IgApi: users/reel_settings/")
    step("GET", "/api/v1/upcoming_events/add_event_list/", "IgApi: upcoming_events/add_event_list/",
         params={"event_category": "broadcast"})
    step("GET", "/api/v1/notifications/get_notification_settings/", "IgApi: notifications/get_notification_settings/",
         params={"content_type": "post_and_comments"})
    step("GET", "/api/v1/direct_v2/async_get_pending_requests_preview/",
         "IgApi: direct_v2/async_get_pending_requests_preview/",
         params={"pending_inbox_filters": "[]"})
    step("POST", "/api/v1/launcher/mobileconfig/", "IgApi: launcher/mobileconfig/",
         obj={"bool_opt_policy": "0", "mobileconfigsessionless": "", "api_version": "10",
              "client_context": "[\"opt,value_hash\"]", "unit_type": "1",
              "use_case": "STANDARD",
              "query_hash": "597390b5cb4ce1dabe98df56106be3b4f174cc774a4711a1455b5e9adc44f968",
              "ts": str(int(time.time())), "device_id": duuid,
              "fetch_mode": "CONFIG_SYNC_ONLY", "fetch_type": "SYNC_FULL",
              "family_device_id": fduuid.upper()})
    step("POST", "/api/v1/launcher/mobileconfig/", "IgApi: launcher/mobileconfig/",
         obj={"bool_opt_policy": "0", "mobileconfig": "", "api_version": "10",
              "client_context": "[\"opt,p64v\"]", "unit_type": "2",
              "use_case": "STANDARD",
              "query_hash": "686e6697d0c77b742beb4dbd302376f72082a1b734e99017c5f1408e510dbb69",
              "ts": str(int(time.time())), "_uid": uid, "device_id": duuid,
              "_uuid": duuid, "fetch_mode": "CONFIG_SYNC_ONLY", "fetch_type": "SYNC_FULL",
              "request_data_query_hash": "686e6697d0c77b742beb4dbd302376f72082a1b734e99017c5f1408e510dbb69"})
    step("GET", "/api/v1/multiple_accounts/get_account_family/",
         "IgApi: multiple_accounts/get_account_family/",
         params={"request_source": "startup_manager"})
    step("GET", "/api/v1/scores/bootstrap/users/", "IgApi: scores/bootstrap/users/",
         params={"surfaces": json.dumps(["autocomplete_user_list",
                                         "coefficient_besties_list_ranking",
                                         "coefficient_rank_recipient_user_suggestion"])})
    step("GET", "/api/v1/loom/fetch_config/", "IgApi: loom/fetch_config/")
    step("POST", "/api/v1/notifications/badge/", "IgApi: notifications/badge/",
         form={"phone_id": fduuid, "trigger": "UNTRACKED", "user_ids": uid,
               "device_id": duuid, "_uuid": duuid})
    step("POST", "/api/v1/push/register/", "IgApi: push/register/",
         form={"device_type": "android_fcm",
               "os_settings": "{\"notificationEnabled\":true}",
               "hpke_pubkey": sess.hpke_pubkey,
               "device_sub_type": "0", "hpke_ciphersuite": "1001000010000",
               "device_token": sess.fcm_token,
               "guid": duuid, "request_id": str(uuid.uuid4()), "_uuid": duuid,
               "users": uid, "family_device_id": fduuid,
               "hpke_keystore_id": str(int(time.time() * 1000))})
    step("POST", "/api/v1/feed/reels_tray/", "IgApi: feed/reels_tray/",
         form={"reason": "cold_start", "timezone_offset": sess.tz_offset,
               "tray_session_id": str(uuid.uuid4()),
               "request_id": str(uuid.uuid4()), "_uuid": duuid, "page_size": 50})
    step("POST", "/api/v1/feed/timeline/", "IgApi: feed/timeline/",
         form={"reason": "cold_start", "count": 8, "timezone_offset": sess.tz_offset,
               "_uuid": duuid, "device_id": sess.android_id,
               "phone_id": fduuid, "is_pull_to_refresh": 0,
               "feed_view_info": "[]"})
    sess.push_nav("MainFeedFragment", "feed_timeline")
    dur = time.time() - t_start
    print(f"[cold start       ] {oks}/{calls} ok in {dur:.1f}s")
    return oks == calls and calls > 0


def upload_image(sess, img_bytes, w, h, is_story):
    upload_id = str(int(time.time() * 1000))
    entity = f"{upload_id}_0_{random.randint(10**9, 10**10 - 1)}"
    rparams = {"upload_id": upload_id, "session_id": upload_id, "media_type": "1"}
    if is_story:
        rparams.update({"upload_engine_config_enum": "0",
                        "share_type": "stories", "is_optimistic_upload": "1"})
    else:
        rparams.update({"upload_media_width": str(w), "upload_media_height": str(h)})
    hdr = sess.base_headers("undefined:media-upload")
    hdr.update({
        "content-type": "application/octet-stream",
        "offset": "0",
        "x-entity-length": str(len(img_bytes)),
        "x-entity-name": entity,
        "x-entity-type": "image/jpeg",
        "x-instagram-rupload-params": json.dumps(rparams, separators=(",", ":")),
        "x_fb_photo_waterfall_id": str(uuid.uuid4()),
    })
    hdr.pop("priority", None)
    r = sess.s.post(f"{BASE}/rupload_igphoto/{entity}", headers=hdr,
                    data=img_bytes, timeout=60)
    print(f"[rupload          ] {r.status_code} {r.text[:80]}")
    if r.status_code != 200 or '"ok"' not in r.text:
        return None
    return upload_id


def configure(sess, upload_id, w, h, is_story, caption=""):
    # FIX-CORR-2: use the session's OWN device identity (was hardcoded
    # Xiaomi 2203121C / Android 9, conflicting with the per-account UA).
    # In IG's configure payload: "android_version" = API level (int),
    # "android_release" = Android version string.
    di = sess._device_info_snapshot
    try:
        _api = int(di.get("api_level", 28))
    except Exception:
        _api = 28
    dev = {"manufacturer": di.get("manufacturer", "Xiaomi"),
           "model": di.get("model", "2203121C"),
           "android_version": _api,
           "android_release": str(di.get("android_version", "9"))}
    now = int(time.time())
    obj = {
        "supported_capabilities_new": SUPPORTED_CAPS,
        "upload_id": upload_id,
        "_uid": sess.uid,
        "_uuid": sess.device_uuid,
        "device_id": sess.android_id,
        "timezone_offset": sess.tz_offset,
        "client_timestamp": str(now),
        "client_shared_at": str(now - 6),
        "edits": {"filter_type": 0, "filter_strength": 0.5,
                  "crop_original_size": [float(w), float(h)]},
        "extra": {"source_width": w, "source_height": h},
        "device": dev,
        "nav_chain": ",".join(sess.nav[-6:]) if sess.nav else "",
    }
    if is_story:
        obj.update({
            "has_camera_metadata": 1, "camera_entry_point": 11,
            "original_media_type": "1", "camera_session_id": str(uuid.uuid4()),
            "original_height": h, "original_width": w,
            "camera_model": dev["model"], "camera_make": dev["manufacturer"],
            "camera_position": "front", "capture_type": "normal",
            "creation_surface": "camera", "creation_tool_info": [],
            "configure_mode": 1, "source_type": 3, "audience": "default",
            "hide_from_profile_grid": False, "scene_capture_type": "",
            "include_e2ee_mentioned_user_list": 1,
            "composition_id": str(uuid.uuid4()),
            "bottom_camera_dial_selected": 2, "publish_id": 1,
            "media_transformation_info": json.dumps(
                {"width": "360", "height": "640", "x_transform": "0",
                 "y_transform": "0", "zoom": "1.0", "rotation": "0.0",
                 "background_coverage": "0.0"}, separators=(",", ":")),
        })
        path, friendly = "/api/v1/media/configure_to_story/", "IgApi: media/configure_to_story/"
        extra_hdr = {"retry_context": json.dumps(
            {"num_reupload": 0, "num_step_manual_retry": 0,
             "num_step_auto_retry": 0}, separators=(",", ":")),
            "x-ig-client-endpoint": "reel_composer_camera"}
    else:
        obj.update({"source_type": 4, "media_folder": "Camera",
                    "caption": caption,
                    "usertags": json.dumps({"in": []}, separators=(",", ":"))})
        path, friendly = "/api/v1/media/configure/", "IgApi: media/configure/"
        extra_hdr = {"x-ig-client-endpoint": "reel_composer_camera"}
    r = sess.post_form(path, obj, friendly, gzip_body=True, extra_headers=extra_hdr)
    if r.status_code != 200:
        print(f"   !! configure error body: {r.text[:400]}")
    try:
        j = r.json()
    except Exception:
        j = {}
    media = j.get("media") or {}
    pk, taken = str(media.get("pk", "")), int(media.get("taken_at", now))
    print(f"[configure {'story' if is_story else 'photo':5s}] {r.status_code} "
          f"pk={pk} taken_at={taken}")
    return pk, taken, r.status_code


# FIX #10: container_module rotation - real users open stories from different surfaces
STORY_SEEN_MODULES = [
    "feed_timeline_item_header",
    "story_viewer_profile_ring",
    "story_viewer_reel_tray",
    "feed_contextual_story",
    "story_viewer_other_user_story",
    "profile_stories_ring",
    "story_viewer_search",
]

def story_seen(sess, media_pk, owner, taken_at, module=None):
    """CAPTURE-exact format: 3-part key, [taken_seen] value, gzip body."""
    if module is None:
        module = random.choice(STORY_SEEN_MODULES)
    obj = {
        "_uid": sess.uid,
        "_uuid": sess.device_uuid,
        "container_module": module,
        "reels": {f"{media_pk}_{owner}_{owner}": [f"{taken_at}_{int(time.time())}"]},
        "reel_media_skipped": {},
        "nuxes": {},
        "nuxes_skipped": {},
        "force_seen_story_ids": [],
    }
    r = sess.post_form("/api/v2/media/seen/?reel=1&live_vod=0", obj,
                       "IgApi: media/seen/?reel=1&live_vod=0", gzip_body=True)
    print(f"[story seen       ] {r.status_code} {r.text[:80]}")
    return r.status_code == 200


# =====================================================================
# FIX-MEDIA: real app downloads post/story media from CDN (NO authorization
# header — verified in capture; friendly-name TigonDownloadService).
# Without this: "liked a post but never fetched its image" = bot signal.
# =====================================================================
def download_media(sess, url, friendly="TigonDownloadService"):
    """Fetch media from CDN exactly like the app's Tigon download service.
    Non-fatal: any failure returns 0 bytes, session continues."""
    if not url:
        return 0
    try:
        h = {
            "user-agent": sess.user_agent,
            "accept-encoding": "gzip, deflate",
            "accept": "*/*",
            "priority": "u=3, i",
            "x-fb-client-ip": "True",
            "x-fb-server-cluster": "True",
            "x-fb-friendly-name": friendly,
            "x-fb-http-engine": "Tigon/MNS/TCP",
            "x-fb-conn-uuid-client": sess.conn_uuid,
            "x-fb-rmd": "state=URL_ELIGIBLE",
            "x-fb-request-analytics-tags": json.dumps(
                {"network_tags": {"product": "567067343352427",
                                  "purpose": "none", "retry_attempt": "0"}},
                separators=(",", ":")),
        }
        r = sess.s.get(url, headers=h, timeout=30)
        n = len(r.content or b"") if r.status_code == 200 else 0
        sess.bw_bytes += n  # real app counts downloaded bytes too
        if n:
            print(f"[media download   ] {r.status_code} {n//1024}KB")
        return n
    except Exception as e:
        print(f"[media download   ] skipped ({type(e).__name__})")
        return 0


def media_first_url(item):
    """Pull the best media URL out of a post/story item dict."""
    try:
        cands = (item.get("image_versions2") or {}).get("candidates") or []
        if cands:
            return cands[0].get("url", "")
        vids = item.get("video_versions") or []
        if vids:
            return vids[0].get("url", "")
    except Exception:
        pass
    return ""


# =====================================================================
# FIX-TELEMETRY: real app POSTs QPL client events to graph.instagram.com
# (15 calls in one 5-min capture). Schema decoded from real capture:
# message = base64(zlib(JSON with device/session identity + event data)).
# Best-effort: 'claims' + 'config_checksum' omitted (session-bound values).
# =====================================================================
APP_ACCESS_TOKEN = "567067343352427|f249176f09e26ce54212b472dbab8fa8"

def telemetry_beat(sess, module="feed_timeline", dry_run=False):
    """Send one QPL client-event batch like the real app does. Non-fatal."""
    try:
        now_ms = int(time.time() * 1000)
        sess.qpl_seq += 1
        di = sess._device_info_snapshot
        payload = {
            "time": now_ms,
            "app_id": "567067343352427",
            "app_ver": di.get("app_version", "400.0.0.49.68"),
            "build_num": int(di.get("build_number", 799297105) or 799297105),
            "consent_state": 0,
            "device": di.get("model", ""),
            "os_ver": str(di.get("android_version", "")),
            "device_id": sess.device_uuid,
            "family_device_id": sess.family_uuid,
            "session_id": sess.pigeon,
            "seq": sess.qpl_seq,
            "app_uid": sess.uid,
            "data": [{
                "extra": {
                    "pigeon_reserved_keyword_module": module,
                    "activity_time": now_ms - random.randint(500, 4000),
                    "last_activity_time": now_ms - random.randint(60000, 900000),
                    "last_foreground_time": now_ms - random.randint(900000, 3600000),
                    "pk": sess.uid,
                    "release_channel": "prod",
                    "radio_type": f"{sess.conn_type}-UNKNOWN",
                    "pigeon_reserved_keyword_requested_latency": -2.0,
                    "pigeon_reserved_keyword_log_type": "client_event",
                    "pigeon_reserved_keyword_bg": "false",
                },
                "log_type": "client_event", "bg": "false",
                "time": now_ms / 1000.0, "module": module,
                "name": "immediate_active_seconds",
                "sampling_rate": 1, "tags": 8388608,
            }],
            "tier": "micro_batch",
            "sent_time": now_ms / 1000.0,
            "carrier": sess.carrier or "Unknown",
            "conn": sess.conn_type,
            "config_version": "v2",
            "qpl_config_version": "v7",
        }
        message = base64.b64encode(zlib.compress(
            json.dumps(payload, separators=(",", ":")).encode())).decode()
        form = {
            "access_token": APP_ACCESS_TOKEN,
            "format": "json", "ffdb_token": "", "compressed": "1",
            "sent_time": f"{time.time():.3f}", "message": message,
        }
        if dry_run:
            return form
        url = "https://graph.instagram.com/logging_client_events"
        h = {
            "user-agent": sess.user_agent,
            "accept-language": sess.accept_lang,
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "priority": "u=5, i",
            "x-fb-client-ip": "True",
            "x-fb-server-cluster": "True",
            "x-fb-connection-type": sess.conn_type,
            "x-ig-connection-type": sess.conn_type,
            "x-fb-friendly-name": "undefined:analytics",
            "x-ig-app-id": "567067343352427",
            "x-ig-capabilities": "3brTv10=",
            "x-tigon-is-retry": "False",
            "accept-encoding": "gzip",
        }
        body = urllib.parse.urlencode(form).encode()
        r = sess.s.post(url, headers=h, data=body, timeout=30)
        print(f"[telemetry        ] {r.status_code}")
        return r.status_code
    except Exception as e:
        print(f"[telemetry        ] skipped ({type(e).__name__})")
        return 0


LIKE_MODULES = ["feed_timeline", "profile", "feed_contextual_post", "explore_popular",
                "video_viewer", "feed_short_video", "reel_feed_timeline"]

def like(sess, media_id, pct_watched=None):
    if pct_watched is None:
        # Realistic watch percentage variance (not a fixed 0.639 constant)
        pct_watched = f"{random.uniform(0.25, 1.0):.13f}"
    obj = {
        "is_2m_enabled": "false",
        "inventory_source": random.choice(["explore_story", "media_viewer", "feed"]),
        "delivery_class": "organic",
        "tap_source": "button",
        "media_id": media_id,
        "like_bubble_user_ids": "",
        "media_pct_watched": pct_watched,
        "radio_type": "wifi-none",
        "_uid": sess.uid,
        "_uuid": sess.device_uuid,
        "nav_chain": ",".join(sess.nav[-6:]) if sess.nav else "",
        "is_from_swipe": False,
        "recs_ix": 0,
        "is_carousel_bumped_post": False,
        "floating_context_items": [],
        "container_module": random.choice(LIKE_MODULES),
        "feed_position": random.randint(0, 5),
    }
    r = sess.post_form(f"/api/v1/media/{media_id}/like/", obj,
                       f"IgApi: media/{media_id}/like/", extra_form={"d": "0"},
                       extra_headers={"x-fb-session-id": sess.fb_session_id})
    print(f"[like             ] {r.status_code} {r.text[:100]}")
    return r


def fetch_comments(sess, media_id):
    """Fetch media comment list (simulates user tapping the comment icon and reading existing comments)."""
    mid = media_id.split("_")[0] if "_" in str(media_id) else media_id
    r = sess.get(f"/api/v1/media/{mid}/comments/", "IgApi: media/comments/",
                 params={"can_support_threading": "true", "permalink_enabled": "false"})
    sess.push_nav("CommentsListFragment", "comments_v2")
    return r


def comment(sess, media_id, text, prefetch_comments=True):
    """
    Simulates authentic user commenting flow:
    1. Opens comment sheet -> GET /api/v1/media/{id}/comments/
    2. Reads comments & types text (natural 2.0s - 4.5s delay)
    3. Posts comment from comments_v2 container
    """
    if prefetch_comments:
        try:
            r_comments = fetch_comments(sess, media_id)
            if r_comments.status_code == 200:
                # Check if comments are disabled
                if "comments_disabled" in r_comments.text and '"comments_disabled":true' in r_comments.text:
                    print(f"[comment prefetch ] 200 comments_disabled=True")
            # Natural human pause: reading comments + typing text
            time.sleep(random.uniform(2.0, 4.5))
        except Exception as e:
            print(f"[comment prefetch ] error: {e}")

    obj = {
        "delivery_class": "organic",
        "feed_position": random.randint(0, 5),
        "container_module": "comments_v2",
        "idempotence_token": str(uuid.uuid4()),
        "media_id": media_id,
        "comment_text": text,
        "_uid": sess.uid,
        "_uuid": sess.device_uuid,
        "device_id": sess.android_id,
        "radio_type": "wifi-none",
    }
    r = sess.post_form(f"/api/v1/media/{media_id}/comment/", obj,
                       f"IgApi: media/{media_id}/comment/", extra_form={"d": "0"})
    print(f"[comment          ] {r.status_code} {r.text[:160]}")
    return r


def make_test_image(story=False):
    from PIL import Image, ImageDraw
    w, h = (640, 480) if not story else (360, 640)
    img = Image.new("RGB", (w, h),
                    (random.randint(30, 200), random.randint(30, 200), random.randint(30, 200)))
    d = ImageDraw.Draw(img)
    d.text((20, 20), f"test {time.strftime('%H:%M:%S')}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), w, h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default=os.path.join(HERE, "cookies_badger.4951992.json"))
    ap.add_argument("--proxy", help="Proxy URL override")
    ap.add_argument("--action", default="verify",
                    choices=["verify", "seen", "like", "comment", "all",
                             "upload-story", "upload-photo"])
    ap.add_argument("--comment-text", default="testing 🙌")
    ap.add_argument("--media-pk", help="seen/like/comment target media pk or post URL")
    ap.add_argument("--owner-id", help="story/media owner uid")
    ap.add_argument("--taken-at", help="story taken_at epoch")
    args = ap.parse_args()

    sess = Session(args.cookies, proxy_override=args.proxy)
    sess.push_nav("MainFeedFragment", "feed_timeline")
    print(f">> account: {sess.username} (uid {sess.uid})")
    if sess.proxy:
        print(f">> proxy: {sess.proxy.split('@')[-1] if '@' in sess.proxy else sess.proxy}")
    if not verify(sess, "START"):
        sys.exit("Session invalid — pehle login karo")

    target_pk = args.media_pk
    if target_pk and ("http://" in target_pk or "https://" in target_pk or "/p/" in target_pk or "/reel/" in target_pk):
        target_pk = url_to_media_id(target_pk)
        print(f">> converted URL to media pk: {target_pk}")

    results = {}
    if args.action in ("upload-story", "all"):
        sess.push_nav("QuickCaptureFragment", "stories_gallery")
        img, w, h = make_test_image(story=True)
        up = upload_image(sess, img, w, h, is_story=True)
        if up:
            pk, taken, _ = configure(sess, up, w, h, is_story=True)
            results["story_pk"], results["story_taken"] = pk, taken
            time.sleep(random.uniform(2, 4))

    if args.action == "seen" and target_pk:
        results["seen"] = story_seen(sess, target_pk,
                                     args.owner_id or sess.uid,
                                     int(args.taken_at or time.time()))

    if args.action in ("like", "all"):
        media_id = target_pk or results.get("photo_pk")
        if media_id:
            mid = media_id if "_" in media_id else f"{media_id}_{args.owner_id or sess.uid}"
            results["like"] = like(sess, mid).status_code == 200
        else:
            print("[like] --media-pk chahiye ya all chalao")

    if args.action in ("comment", "all"):
        media_id = target_pk or results.get("photo_pk")
        if media_id:
            mid = media_id if "_" in media_id else f"{media_id}_{args.owner_id or sess.uid}"
            time.sleep(random.uniform(2, 4))
            results["comment"] = comment(sess, mid, args.comment_text).status_code == 200
        else:
            print("[comment] --media-pk chahiye ya all chalao")

    time.sleep(1.5)
    if verify(sess, "END"):
        print("\nSESSION VALID — actions ke baad bhi theek, koi challenge nahi")
    else:
        print("\nSESSION KO ISSUE — upar ke messages dekho")


if __name__ == "__main__":
    main()
