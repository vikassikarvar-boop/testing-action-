# 🔧 ig_actions.py — 2 Correlation Bug Fixes

Sirf 2 bugs fix kiye hain (kuch aur touch nahi hua). Bas ye file apne
`ig_multi_matrix_engine` folder me **ig_actions.py** ki jagah replace karo.

## Fix 1 — hardcoded x-fb-session-id 🔴
**Pehle:** sab accounts same value bhejte the:
`x-fb-session-id: nid=eb61LiYjtu+6;nc=1;fc=1;bc=0;`
→ ek hi header se saare 6 accounts correlate ho sakte the.

**Ab:** har session apna unique `nid=<12-char random>` generate karta hai
(`Session.__init__` me `self.fb_session_id`), aur `like()` wahi bhejta hai.
Format same hai, value har account+session me alag.

## Fix 2 — configure() device mismatch
**Pehle:** hardcoded `"Xiaomi", "2203121C", android 9` — jabki account ka
User-Agent alag device bolta tha (e.g. 2201117SY / Android 10). Post upload
pe device-mismatch = detection flag.

**Ab:** `configure()` session ka APNA `_device_info_snapshot` use karta hai
(manufacturer/model/api_level/android_version), aur story `camera_model` +
`camera_make` bhi usi device se aate hain.

## Verify (sab tests pass)
- TEST1: per-session unique ids, real format ✅
- TEST2: configure() device = session identity ✅
- TEST3: camera_model/camera_make session device follow ✅
- TEST4: hardcoded `eb61LiYjtu` code se hat gaya ✅

> Baaki sab kuch (cold_start, story_seen, comment flow, pacing) bilkul
> untouched hai.

---
## Fix 3 — TLS/HTTP2 fingerprint (SABSE BADA) 🔴🔴
**Pehle:** engine `requests` library se chalta tha — famous **python-bot TLS
signature** + HTTP/1.1. IG handshake dekhte hi detect kar leta tha, headers
se pehle.

**Ab:** engine `curl_cffi` se chalta hai jo **real Instagram Android app ka
OkHttp4 TLS/JA3 + HTTP/2 fingerprint** bhejta hai.

**Live proof (aaj tls.peet.ws pe test):**
- Purana engine:  JA4 `t13d1713h1_ab0a1bf427ad` (HTTP/1.1, python bot)
- Patched engine: JA4 `t13d1512h2_8daaf6152771` (HTTP/2, OkHttp/Android) ✅

**Zaroori:** apne machine pe chalao:
```
pip install curl_cffi
```
(Agar install nahi hua to engine purane behavior pe warning ke saath chalega,
crash nahi karega.)

Detail: TLS_RESEARCH.md

---
## Fix 4 — Media download + telemetry MISSING (capture-verified) 🔴
**Problem:** Real app har like/story ke saath media bhi download karta hai +
QPL telemetry bhejta hai. Engine sirf API call bhejta tha — "post like ki
par dekhi nahi" = bot signal.

**Capture evidence (fullaction.mitm):**
- 165 CDN media requests (`instagram.fbho3-*.fna.fbcdn.net`) — friendly-name
  `TigonDownloadService`, **bina authorization** ke
- 15x `/logging_client_events` (graph.instagram.com) — full schema decode kiya
- bandwidth headers har API call pe: `x-ig-bandwidth-speed-kbps/totalbytes-b/totaltime-ms`

**Kya add hua:**
- `download_media()` — story/post media ka real CDN download (action se pehle)
- `telemetry_beat()` — real-app-exact QPL payload (zlib+base64, same fields)
- bandwidth headers har request pe (cumulative counters)
- matrix_runner hooks: story-seen se pehle story media, like/comment se pehle
  post media, session start + har 4th gap pe telemetry beat

**Tests:** payload schema match ✅ | download via OkHttp TLS ✅ | photo+video URL extraction ✅

---
## Files is zip me
- `ig_actions.py`  — Fix 1+2+3+4 (sessionid, device, TLS, media+telemetry)
- `matrix_runner.py` — media/telemetry hooks (Fix 4)
- `TLS_RESEARCH.md` — research doc
- `PATCH_NOTES.md` — ye file

**Install:** `pip install curl_cffi` (bas yahi naya dependency hai)

---
## Fix 5 — hpke_pubkey hardcoded (correlation) 🔴
**Pehle:** sab accounts same push-encryption key bhejte the (BCkcS8F6...).
**Ab:** har session apna **fresh P-256 keypair** generate karta hai (real
P-256 point via cryptography lib, fallback format-valid random point).
Verified: 65 bytes, 0x04 prefix, valid curve point, unique per session ✅

## Fix 6 — Soft-limit pe "human stop" (crab-killer fix) 🔴🔴
**Problem:** IG ka "Comments limited / commenting too fast" BLOCK_MARKERS me
nahi tha — engine usse normal FAIL maan ke retry karta raha. Crab isi se mara:
6 warnings ignore → permanent ban. Real app/user warning dekh ke RUK jata hai.
**Ab:** SOFT_MARKERS detect karte hain ("comments limited", "too fast",
"wait a few minutes", "slow down"...) → **pehli soft warning = session turant
band + 60-180 min rest** — bilkul wahi jo ek insaan karta hai.
Speed ab problem nahi — jab tak engine ceiling pe rukna jaanta hai.

Install: `pip install cryptography` (optional — na ho to format-valid fallback)
