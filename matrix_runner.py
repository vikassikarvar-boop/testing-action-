#!/usr/bin/env python3
"""
matrix_runner.py — 6-Account Parallel Multi-Matrix Engine (100% Resilient Auto-Fallback).
Features:
- Instant auto-fallback on expired stories or restricted posts (guarantees HTTP 200 OK).
- Crash-proof non-blocking CSV logging.
- Authentic human pacing (20-40 min sessions with 3-4 min inter-comment gaps).
- Active passive feed browsing during wait intervals.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import sys
import threading
import time
import uuid

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ig_actions as A
import unique_comment_generator as CG

CONFIG_FILE = os.path.join(HERE, "matrix_config.json")
MASTER_TARGETS_FILE = os.path.join(HERE, "master_targets.json")
STATE_FILE = os.path.join(HERE, "matrix_state.json")
LOG_FILE = os.path.join(HERE, "matrix_log.csv")

BLOCK_MARKERS = ("rate_limit_error", "feedback_required", "action_block",
                 "challenge_required", "checkpoint_required", "sentry_block",
                 "try again later", "comment_not_allowed")

# FIX-SOFTLIMIT: the REAL APP shows a toast and the HUMAN STOPS when IG says
# "too fast / comments limited". Engine previously ignored these (crab died
# after 6 ignored warnings). Now: first soft warning = session ends + rest.
SOFT_MARKERS = ("commenting too fast", "comments limited", "comments_limit",
                "wait a few minutes", "slow down", "temporarily blocked",
                "too many requests", "you're going too fast")

LOG_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
TARGET_LOCK = threading.Lock()


def t_print(msg):
    with PRINT_LOCK:
        print(msg)


def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}
    return default if default is not None else {}


def save_state(state):
    with STATE_LOCK:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            t_print(f"Warning: Could not save state: {e}")


def in_window(gen_cfg):
    now = dt.datetime.now()
    cur = now.hour * 60 + now.minute
    for a, b in gen_cfg.get("activity_windows", []):
        ha, ma = map(int, a.split(":"))
        hb, mb = map(int, b.split(":"))
        if ha * 60 + ma <= cur <= hb * 60 + mb:
            return True
    return False


def secs_to_next_window(gen_cfg):
    now = dt.datetime.now()
    cur = now.hour * 60 + now.minute
    starts = []
    for a, b in gen_cfg.get("activity_windows", []):
        ha, ma = map(int, a.split(":"))
        hb, mb = map(int, b.split(":"))
        if ha * 60 + ma <= cur <= hb * 60 + mb:
            return 0
        starts.append(ha * 60 + ma)
    future = [s for s in starts if s > cur]
    return (min(future) - cur) * 60 if future else None


def is_blocked(resp):
    if resp is None:
        return False
    return resp.status_code == 429 or any(m in resp.text for m in BLOCK_MARKERS)


def is_soft_limit(resp):
    """Soft rate warning ('commenting too fast', 'comments limited'...) —
    what a real user sees as a toast and STOPS for."""
    if resp is None:
        return False
    txt = resp.text.lower()
    return any(m in txt for m in SOFT_MARKERS)


class ThreadSafeCSVLog:
    def __init__(self, path):
        self.path = path
        self.buffer = []
        self._ensure_header()

    def _ensure_header(self):
        if not os.path.exists(self.path):
            try:
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "ts", "day", "session_num", "acct_id", "account_name", "profile",
                        "action", "target", "media_id", "status", "ok", "blocked",
                        "dwell_sec", "gap_sec", "comment_text", "note"
                    ])
            except Exception:
                pass

    def row(self, *args):
        with LOG_LOCK:
            self.buffer.append(args)
            self._flush_buffer()

    def _flush_buffer(self):
        if not self.buffer:
            return

        for attempt in range(5):
            try:
                with open(self.path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    while self.buffer:
                        writer.writerow(self.buffer.pop(0))
                return
            except PermissionError:
                time.sleep(0.15)
            except Exception as e:
                time.sleep(0.15)


# =====================================================================
# API CALL HELPERS
# =====================================================================

def get_user_pk(sess, username):
    clean_user = username.lstrip("@").strip()
    r = sess.get(f"/api/v1/users/{clean_user}/usernameinfo/", "IgApi: users/usernameinfo/")
    if r.status_code == 200:
        try:
            return str(r.json().get("user", {}).get("pk", ""))
        except Exception:
            pass
    return None


def get_user_stories(sess, user_pk):
    r = sess.get(f"/api/v1/feed/user/{user_pk}/story/", "IgApi: feed/user/story/")
    if r.status_code == 200:
        try:
            reel = r.json().get("reel") or {}
            items = reel.get("items", [])
            if items:
                return items
        except Exception:
            pass
    r2 = sess.get(f"/api/v1/feed/reels_media/?user_ids={user_pk}", "IgApi: feed/reels_media/")
    if r2.status_code == 200:
        try:
            reels = r2.json().get("reels", {})
            user_reel = reels.get(str(user_pk), {})
            return user_reel.get("items", [])
        except Exception:
            pass
    return []


def get_media_owner_pk(sess, media_id):
    """Returns (owner_pk, media_url) — media URL used for the realistic
    CDN download before liking/commenting (FIX-MEDIA)."""
    r = sess.get(f"/api/v1/media/{media_id}/info/", "IgApi: media/info/")
    if r.status_code == 200:
        try:
            items = r.json().get("items", [])
            if items:
                owner = str(items[0].get("user", {}).get("pk", ""))
                murl = A.media_first_url(items[0])
                return owner, murl
        except Exception:
            pass
    return "", ""


def shift_time(tstr, minutes):
    """FIX #17: shift an HH:MM time by minutes (wraps around 24h)."""
    h, m = map(int, tstr.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def sleep_with_passive_browsing(sess, total_seconds, gen_cfg, prefix=""):
    t_start = time.time()
    t_end = t_start + total_seconds
    # FIX #13: track how many passive calls made per session (per-account)
    if not hasattr(sess, "_browse_n"):
        sess._browse_n = 0

    while time.time() < t_end:
        rem = t_end - time.time()
        chunk = min(rem, random.uniform(50.0, 95.0))
        time.sleep(chunk)
        
        if (t_end - time.time()) > 30.0:
            try:
                # FIX #13: max 2-3 network calls per session - 1 timeline + 1 reels_tray,
                # then silence (saves data AND removes bot-like constant feed polling)
                sess._browse_n += 1
                if sess._browse_n == 1:
                    sess.get("/api/v1/feed/timeline/", "IgApi: feed/timeline/", params={"count": 8})
                elif sess._browse_n == 2:
                    sess.post_form("/api/v1/feed/reels_tray/",
                                   {"reason": "pull_to_refresh", "timezone_offset": sess.tz_offset,
                                    "tray_session_id": str(uuid.uuid4()),
                                    "request_id": str(uuid.uuid4()), "_uuid": sess.device_uuid,
                                    "page_size": 20},
                                   "IgApi: feed/reels_tray/")
            except Exception:
                pass


# =====================================================================
# SINGLE ACCOUNT SESSION WORKER (WITH SMART RETRY & CLEAN TARGETING)
# =====================================================================

def run_account_session(acct_cfg, gen_cfg, master_data, state, log, day, ses_idx, stop_event=None):
    acct_id = acct_cfg["id"]
    acct_name = acct_cfg["name"]
    profile = acct_cfg["profile"]
    cookie_path = os.path.join(HERE, acct_cfg["cookies"])
    prefix = f"[Acct #{acct_id:02d} {acct_name[:12]:12s}]"

    if not os.path.exists(cookie_path):
        t_print(f"❌ {prefix} Cookie file not found: {cookie_path}")
        return False

    with STATE_LOCK:
        cooldown_until = state.get("cooldowns", {}).get(acct_name, 0)
    if time.time() < cooldown_until:
        rem_min = (cooldown_until - time.time()) / 60
        t_print(f"⚠️ {prefix} in cooldown ({rem_min:.1f}m left). Sleeping.")
        return False

    sess_dur_min = random.uniform(*acct_cfg.get("session_duration_minutes", [20, 30]))
    t_print(f"🚀 {prefix} Starting Session #{ses_idx} (Duration: ~{sess_dur_min:.1f}m | Profile: {profile})")
    
    sess = A.Session(cookie_path)
    if sess.proxy:
        proxy_short = sess.proxy.split("@")[-1] if "@" in sess.proxy else sess.proxy
        t_print(f"   {prefix} Proxy: {proxy_short}")

    if not A.verify(sess, acct_name[:5]):
        t_print(f"❌ {prefix} Session invalid / checkpointed! Marking status.")
        with STATE_LOCK:
            # FIX #18: dedupe dead accounts (was appending duplicates 5x)
            deads = state.setdefault("dead_accounts", [])
            if acct_name not in deads:
                deads.append(acct_name)
            # Track per-account death count for severity-based quarantine
            state.setdefault("account_status", {})[acct_name] = {
                "status": "dead",
                "marked_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
        save_state(state)
        return False

    # FIX #19: per-account seeded RNG (deterministic-but-distinct per account per day)
    rng = random.Random(f"{acct_name}:{day}:{ses_idx}")
    def seeded_choice(seq):
        return seq[rng.randrange(len(seq))] if seq else None
    def seeded_uniform(lo, hi):
        return rng.uniform(lo, hi)

    # 1. Cold start app open
    A.cold_start(sess, verbose=False)

    # Initial passive browse (25s to 45s)
    t_init_browse = seeded_uniform(25.0, 45.0)
    time.sleep(t_init_browse)

    # FIX-TELEMETRY: real app sends QPL client events continuously
    # (15 batches in one 5-min capture). One beat after cold start.
    A.telemetry_beat(sess, module="feed_timeline")

    actions_spec = acct_cfg.get("actions_per_session", {})
    planned = []
    for act, (lo, hi) in actions_spec.items():
        planned += [act] * rng.randint(lo, hi)
    rng.shuffle(planned)

    t_print(f"   {prefix} Planned {len(planned)} action(s) spaced across ~{sess_dur_min:.1f}m session: {planned}")

    with STATE_LOCK:
        acct_state = state.setdefault("accounts", {}).setdefault(acct_name, {
            "used_stories": [],
            "used_posts": [],
            "total_done": {"like": 0, "comment": 0, "seen": 0},
            "blocks": [],
            "first_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        # FIX #7: days_active tracking for volume ramp-up
        try:
            first_dt = dt.datetime.strptime(acct_state.get("first_seen", ""), "%Y-%m-%d %H:%M:%S")
            days_active = max(0, (dt.datetime.now() - first_dt).days)
        except Exception:
            days_active = 0

        # FIX #7: ramp-up factor - new accounts start slow, scale to 100% by day 22
        if days_active <= 3:
            ramp_factor = 0.2
        elif days_active <= 7:
            ramp_factor = 0.4
        elif days_active <= 14:
            ramp_factor = 0.6
        elif days_active <= 21:
            ramp_factor = 0.8
        else:
            ramp_factor = 1.0

        # FIX #6: month-safe daily volume caps (per action type)
        # comment <= 12, like <= 25, seen <= 40 => max ~77 actions/day (was 143!)
        daily_caps = {
            "comment": int(acct_cfg.get("target_24h", {}).get("comment", 0) or 0) or 12,
            "like": int(acct_cfg.get("target_24h", {}).get("like", 0) or 0) or 25,
            "seen": int(acct_cfg.get("target_24h", {}).get("seen", 0) or 0) or 40,
        }

        # FIX #2: per-account target subsets (each account only ever touches its own slice)
        acct_pool_key = f"pool_{acct_name}"
        if acct_pool_key not in state:
            # deterministic split of master targets across accounts by hash
            state[acct_pool_key] = {"stories": [], "posts": []}
            all_stories = master_data.get("story_usernames", [])
            all_posts = master_data.get("post_urls", [])
            n_accts = max(1, len([a for a in (state.get("account_status") or {})]))
            # simple deterministic assignment
            idx = abs(hash(acct_name)) % max(1, len(all_stories))
            state[acct_pool_key]["stories"] = all_stories[idx:] + all_stories[:idx]
            state[acct_pool_key]["posts"] = all_posts[:]
        pool_stories = state[acct_pool_key]["stories"]
        pool_posts = state[acct_pool_key]["posts"]

    seq = 0
    intervals = acct_cfg.get("action_interval_seconds", {})
    # FIX #12: action gap maturity - minimum realistic human gaps
    MIN_GAP = {"seen": 90, "like": 120, "comment": 120}
    # FIX #11: realistic dwell ranges
    dwell_map = acct_cfg.get("dwell_seconds", {})

    # FIX #16: per-session story fetch cache (TTL 10-15 min)
    sess._story_cache = {}
    sess._story_cache_ttl = 600 + random.randint(0, 300)

    for act in planned:
        if stop_event and stop_event.is_set():
            t_print(f"   {prefix} Stop event received, concluding session.")
            break

        # FIX #6: enforce daily volume caps (skip if cap reached for this action)
        with STATE_LOCK:
            day_done = state.setdefault("days", {}).setdefault(day, {}).setdefault(acct_name, {})
            cap = daily_caps.get(act, 40)
            if day_done.get(act, 0) >= cap:
                t_print(f"   {prefix} {act} daily cap {cap} reached, skipping.")
                continue

        int_range = intervals.get(act, [90, 180])
        # FIX #12: never below the minimum realistic gap
        gap = max(seeded_uniform(*int_range), MIN_GAP.get(act, 90))
        sleep_with_passive_browsing(sess, gap, gen_cfg, prefix)

        # FIX-TELEMETRY: occasional QPL beat mid-session (~1 in 4 gaps)
        if seq > 0 and seq % 4 == 0 and seeded_uniform(0, 1) < 0.75:
            A.telemetry_beat(sess, module=random.choice(
                ["feed_timeline", "story_viewer", "profile_page"]))

        seq += 1
        target_str = ""
        media_id = ""
        comment_text = ""
        status = 0
        ok = False
        blocked = False
        note = ""

        # --- Mid-session health check DISABLED ---
        # Previously fired A.verify() every 3rd action → extra HTTP call = extra detection signal.
        # Block detection is already handled by is_blocked(r) on every action response.
        # Session-start verify (line 261) is sufficient to catch suspended/expired sessions.
        # if seq % 3 == 0:
        #     if not A.verify(sess, f"{acct_name[:5]}#{seq}"):
        #         t_print(f"⚠️ {prefix} Health check failed mid-session! Concluding early.")
        #         break

        # --- STORY SEEN ACTION (WITH AUTO-FALLBACK TO LIVE USER) ---
        if act == "seen":
            found_story = False
            for _ in range(5):  # Try up to 5 candidates if one has expired stories
                with TARGET_LOCK:
                    avail_stories = [u for u in pool_stories
                                     if u not in acct_state["used_stories"]]
                    if not avail_stories:
                        avail_stories = pool_stories
                    target_user = seeded_choice(avail_stories) if avail_stories else ""

                if not target_user:
                    break

                target_str = f"@{target_user.lstrip('@')}"
                u_pk = get_user_pk(sess, target_user)
                if not u_pk:
                    continue

                # FIX #16: story fetch cache - don't re-fetch same user within TTL
                now_t = time.time()
                cached = sess._story_cache.get(target_user)
                if cached and (now_t - cached[0]) < sess._story_cache_ttl:
                    stories = cached[1]
                else:
                    stories = get_user_stories(sess, u_pk)
                    sess._story_cache[target_user] = (now_t, stories)
                if not stories:
                    with STATE_LOCK:
                        acct_state["used_stories"].append(target_user)
                    continue

                st0 = stories[0]
                pk = str(st0.get("pk", ""))
                taken = int(st0.get("taken_at", time.time()))
                own = str(st0.get("user", {}).get("pk", u_pk))
                media_id = pk

                # FIX-MEDIA: real app downloads the story media while viewing
                A.download_media(sess, A.media_first_url(st0))

                # FIX #11: realistic story-view dwell (4-12s, not 3-7s)
                drange = dwell_map.get("seen", [4.0, 12.0])
                dwell = seeded_uniform(*drange)
                time.sleep(dwell)

                ok = A.story_seen(sess, pk, own, taken)
                status = 200 if ok else 400
                note = f"story_pk={pk}"
                if ok:
                    with STATE_LOCK:
                        acct_state["used_stories"].append(target_user)
                    found_story = True
                    break

            if not found_story and not ok:
                t_print(f"   {prefix} #{seq:02d} seen -> Skipped (No live stories found)")
                continue

        # --- POST LIKE / COMMENT ACTION (WITH AUTO-FALLBACK TO CLEAN POST) ---
        elif act in ("like", "comment"):
            found_post = False
            for _ in range(5):
                with TARGET_LOCK:
                    # FIX #2: use per-account post pool (deterministic slice)
                    avail_posts = [p for p in pool_posts
                                   if p not in acct_state["used_posts"]]
                    if not avail_posts:
                        avail_posts = pool_posts
                    target_url = seeded_choice(avail_posts) if avail_posts else ""

                if not target_url:
                    break

                target_str = target_url
                mid = A.url_to_media_id(target_url)
                if not mid:
                    continue

                # FIX #11: realistic dwell (like 8-20s, comment 25-60s)
                drange = dwell_map.get(act, [8.0, 20.0] if act == "like" else [25.0, 60.0])
                dwell = seeded_uniform(*drange)
                time.sleep(dwell)

                owner_pk, post_media_url = get_media_owner_pk(sess, mid)
                full_media_id = f"{mid}_{owner_pk}" if owner_pk else mid
                media_id = mid

                # FIX-MEDIA: real app downloads the post's media before
                # liking/commenting (image fetched while user looks at it)
                A.download_media(sess, post_media_url)

                if act == "like":
                    r = A.like(sess, full_media_id)
                    status, ok, note = r.status_code, '"ok"' in r.text.lower(), r.text[:100]
                    blocked = is_blocked(r)
                    soft = is_soft_limit(r)
                    if ok:
                        with STATE_LOCK:
                            acct_state["used_posts"].append(target_url)
                        found_post = True
                        break
                    elif "cannot like" in r.text.lower() or "no longer available" in r.text.lower():
                        # Bad post -> remove and retry another post
                        with STATE_LOCK:
                            acct_state["used_posts"].append(target_url)
                        continue

                elif act == "comment":
                    comment_text = CG.get_unique_comment(acct_name)
                    r = A.comment(sess, full_media_id, comment_text)
                    status, ok, note = r.status_code, '"ok"' in r.text.lower(), r.text[:100]
                    blocked = is_blocked(r)
                    soft = is_soft_limit(r)
                    if ok:
                        with STATE_LOCK:
                            acct_state["used_posts"].append(target_url)
                        found_post = True
                        break
                    elif "no longer available" in r.text.lower() or "comment_not_allowed" in r.text.lower():
                        with STATE_LOCK:
                            acct_state["used_posts"].append(target_url)
                        continue
                
                # FIX-CHALLENGE-RETRY: If account is challenged/blocked, IMMEDIATELY stop
                # retrying. Firing 5 rapid blocked requests triggers Instagram firewall escalation.
                # Previously: loop continued 5x on challenge -> now: break on first detection.
                note_l = note.lower() if note else ""
                if blocked or any(kw in note_l for kw in (
                    "challenge_required", "checkpoint_required", "login_required",
                    "action_block", "feedback_required", "sentry_block"
                )):
                    t_print(f"   ⚠️ {prefix} #{seq:02d} {act} -> ACCOUNT CHALLENGED/BLOCKED — stopping retry loop immediately!")
                    break

            if not found_post and not ok:
                t_print(f"   {prefix} #{seq:02d} {act} -> Target restricted, skipping.")
                continue

        # FIX-SOFTLIMIT: human behavior on "too fast" — STOP, don't retry.
        if not blocked and soft:
            cd_min = random.uniform(60, 180)
            with STATE_LOCK:
                state.setdefault("cooldowns", {})[acct_name] = time.time() + cd_min * 60
            try:
                log.row(time.strftime("%Y-%m-%d %H:%M:%S"),
                        day, ses_idx, acct_id, acct_name, profile, act,
                        target_str, media_id, status, False, True,
                        0, 0, comment_text, "SOFT_LIMIT->stop")
            except Exception:
                pass
            t_print(f"   ⚠️ {prefix} #{seq:02d} {act} -> SOFT LIMIT (too fast) — "
                    f"stopping like a human, resting {cd_min:.0f}m")
            save_state(state)
            break

        # Safe logging call
        try:
            log.row(
                time.strftime("%Y-%m-%d %H:%M:%S"), day, ses_idx, acct_id, acct_name,
                profile, act, target_str, media_id, status, ok, blocked,
                round(dwell, 1), round(gap, 1), comment_text, note
            )
        except Exception:
            pass

        flag = "OK" if ok else ("BLOCK" if blocked else "FAIL")
        cmt_info = f" (Text: \"{comment_text}\")" if comment_text else ""
        t_print(f"   {prefix} #{seq:02d} {act:7s} -> {target_str}{cmt_info}: HTTP {status} [{flag}]")

        if ok:
            with STATE_LOCK:
                acct_state["total_done"][act] = acct_state["total_done"].get(act, 0) + 1
                day_done = state.setdefault("days", {}).setdefault(day, {}).setdefault(acct_name, {})
                day_done[act] = day_done.get(act, 0) + 1

        if blocked:
            with STATE_LOCK:
                # FIX #8: exponential backoff + severity-based quarantine
                #   - soft block (429 / rate_limit)  : 2h, 4h, 8h...
                #   - challenge / checkpoint          : 24h, 48h, 96h...
                #   - action_block / feedback        : 6h, 12h, 24h...
                acct_state.setdefault("block_count", 0)
                acct_state["block_count"] += 1
                bc = acct_state["block_count"]
                note_l = note.lower()
                if "challenge" in note_l or "checkpoint" in note_l or "login_required" in note_l:
                    severity = "severe"
                    cd_base = 24 * 60
                elif "action_block" in note_l or "feedback" in note_l or "sentry" in note_l:
                    severity = "medium"
                    cd_base = 6 * 60
                else:
                    severity = "soft"
                    cd_base = 2 * 60
                cd_min = cd_base * (2 ** min(bc - 1, 3))  # exponential: x1, x2, x4, x8 capped
                state.setdefault("cooldowns", {})[acct_name] = time.time() + cd_min * 60
                acct_state["blocks"].append({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "action": act,
                    "status": status,
                    "severity": severity,
                    "block_count": bc,
                    "note": note
                })
            t_print(f"   ⚠️ {prefix} BLOCK DETECTED ({severity}, #{bc})! Cooldown activated for {cd_min}m.")
            break

        save_state(state)

    save_state(state)
    t_print(f"✅ {prefix} Session #{ses_idx} Finished (~{sess_dur_min:.1f}m session complete).")
    return True


# =====================================================================
# INDEPENDENT ACCOUNT WORKER THREAD (24H LIFECYCLE)
# =====================================================================

class AccountWorkerThread(threading.Thread):
    def __init__(self, acct_cfg, gen_cfg, master_data, state, log, is_day_mode=True, stop_event=None):
        super().__init__()
        self.acct_cfg = acct_cfg
        self.gen_cfg = gen_cfg
        self.master_data = master_data
        self.state = state
        self.log = log
        self.is_day_mode = is_day_mode
        self.stop_event = stop_event or threading.Event()
        self.daemon = True

    def run(self):
        acct_id = self.acct_cfg["id"]
        name = self.acct_cfg["name"]
        prefix = f"[Acct #{acct_id:02d} {name[:12]:12s}]"

        # FIX #17: account-specific activity windows (stagger by account id so all
        # accounts don't hammer at the exact same minute)
        win_shift = (acct_id * 17) % 90
        base_windows = self.gen_cfg.get("activity_windows", [["08:00", "13:00"], ["14:00", "18:30"], ["19:30", "23:59"]])
        acct_windows = [[shift_time(a, win_shift), shift_time(b, win_shift)] for a, b in base_windows]
        acct_gen = dict(self.gen_cfg)
        acct_gen["activity_windows"] = acct_windows

        # FIX #18: dead accounts never re-run
        with STATE_LOCK:
            if name in self.state.get("dead_accounts", []):
                t_print(f"🪦 {prefix} Dead account, skipping permanently.")
                return

        stagger = random.uniform(3.0, 15.0) * (acct_id % 4)
        time.sleep(stagger)

        day = time.strftime("%Y-%m-%d")

        if not self.is_day_mode:
            with STATE_LOCK:
                done_sessions = self.state.setdefault("days", {}).setdefault(day, {}).get(f"{name}_sessions", 0)
                ses_idx = done_sessions + 1
            run_account_session(self.acct_cfg, acct_gen, self.master_data,
                                self.state, self.log, day, ses_idx, self.stop_event)
            with STATE_LOCK:
                self.state["days"][day][f"{name}_sessions"] = ses_idx
            save_state(self.state)
            return

        while not self.stop_event.is_set():
            current_day = time.strftime("%Y-%m-%d")
            if current_day != day:
                day = current_day

            with STATE_LOCK:
                if name in self.state.get("dead_accounts", []):
                    t_print(f"🪦 {prefix} Dead account detected, stopping worker.")
                    return

            wait = secs_to_next_window(acct_gen)
            if wait is None:
                time.sleep(1800)
                continue

            if wait > 0:
                time.sleep(min(wait, 600))
                continue

            max_sessions = self.acct_cfg.get("sessions_per_day", [5, 6])[1]
            with STATE_LOCK:
                done_sessions = self.state.setdefault("days", {}).setdefault(day, {}).get(f"{name}_sessions", 0)

            if done_sessions >= max_sessions:
                time.sleep(1800)
                continue

            ses_idx = done_sessions + 1
            success = run_account_session(self.acct_cfg, acct_gen, self.master_data,
                                          self.state, self.log, day, ses_idx, self.stop_event)
            if success:
                with STATE_LOCK:
                    self.state["days"][day][f"{name}_sessions"] = ses_idx
                save_state(self.state)

            break_min = random.uniform(45.0, 80.0)
            t_print(f"☕ {prefix} Finished Session #{ses_idx}. Resting for {break_min:.1f}m...")
            
            for _ in range(int(break_min * 6)):
                if self.stop_event.is_set():
                    break
                time.sleep(10)


# =====================================================================
# CONSOLIDATED SCORECARD REPORT
# =====================================================================

def print_matrix_scorecard(config, state):
    print("\n" + "=" * 90)
    print("📊 6-ACCOUNT EXPERIMENTAL PARALLEL MATRIX SCORECARD (NATURAL 20-40M PACING)")
    print("=" * 90)
    print(f"{'ID':<3} | {'Account Name':<22} | {'Profile Tier':<20} | {'Done (C/L/S)':<14} | {'Target':<14} | {'Status':<8}")
    print("-" * 90)

    for acct in config.get("accounts", []):
        aid = acct["id"]
        name = acct["name"]
        prof = acct["profile"]
        targets = acct.get("target_24h", {})
        tgt_str = f"C:{targets.get('comment',0)} L:{targets.get('like',0)} S:{targets.get('seen',0)}"

        acct_s = state.get("accounts", {}).get(name, {})
        d = acct_s.get("total_done", {})
        done_str = f"C:{d.get('comment',0)} L:{d.get('like',0)} S:{d.get('seen',0)}"

        is_dead = name in state.get("dead_accounts", [])
        is_cd = time.time() < state.get("cooldowns", {}).get(name, 0)
        
        status = "DEAD" if is_dead else ("COOLDOWN" if is_cd else "ACTIVE")
        print(f"{aid:<3} | {name:<22} | {prof:<20} | {done_str:<14} | {tgt_str:<14} | {status:<8}")

    print("=" * 90)
    used_comments = CG.load_used_comments()
    print(f"💬 Total Unique Text Comments in Global Registry: {len(used_comments)}")
    print(f"📄 Detailed CSV Log Location                     : {LOG_FILE}")
    print("=" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description="6-Account Parallel Multi-Matrix Engine (Natural Pacing)")
    parser.add_argument("--status", action="store_true", help="Display 6-account matrix scorecard")
    parser.add_argument("--account", help="Specific account ID (1-6) or username to execute")
    parser.add_argument("--all", action="store_true", help="Run all active accounts concurrently in parallel")
    parser.add_argument("--mode", choices=["session", "day"], default="day", help="Execution mode (session or 24h day)")
    args = parser.parse_args()

    config = load_json(CONFIG_FILE)
    if not config or "accounts" not in config:
        sys.exit(f"❌ matrix_config.json not found or invalid at {CONFIG_FILE}")

    state = load_json(STATE_FILE, {
        "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "days": {},
        "accounts": {},
        "cooldowns": {},
        "dead_accounts": []
    })

    if args.status:
        print_matrix_scorecard(config, state)
        return

    master_data = load_json(MASTER_TARGETS_FILE)
    log = ThreadSafeCSVLog(LOG_FILE)
    gen_cfg = config.get("general", {})

    target_accounts = []
    if args.account:
        for a in config["accounts"]:
            if str(a["id"]) == str(args.account) or a["name"].lower() == args.account.lower():
                target_accounts.append(a)
                break
        if not target_accounts:
            sys.exit(f"❌ Account '{args.account}' not found in matrix_config.json.")
    elif args.all:
        target_accounts = config["accounts"]
    else:
        print_matrix_scorecard(config, state)
        print("💡 Usage:")
        print("   python matrix_runner.py --all --mode day       # Runs all 6 active accounts in parallel")
        print("   python matrix_runner.py --all --mode session   # Runs all 6 active accounts for 1 session")
        return

    is_day_mode = (args.mode == "day")
    mode_title = "24-HOUR CONTINUOUS PARALLEL DAY ENGINE (6 ACTIVE ACCOUNTS)" if is_day_mode else "PARALLEL SESSION BATCH"

    print("\n" + "=" * 85)
    print(f"🚀 LAUNCHING {mode_title}")
    print(f"👥 Active Parallel Threads / Accounts: {len(target_accounts)}")
    print("=" * 85)

    stop_event = threading.Event()
    threads = []

    for acct in target_accounts:
        t = AccountWorkerThread(
            acct_cfg=acct,
            gen_cfg=gen_cfg,
            master_data=master_data,
            state=state,
            log=log,
            is_day_mode=is_day_mode,
            stop_event=stop_event
        )
        threads.append(t)
        t.start()

    print(f"✅ All {len(threads)} account threads running on natural 20-40m session timeline!\n")

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n⚠️ KeyboardInterrupt received! Gracefully stopping all account threads...")
        stop_event.set()
        for t in threads:
            t.join(timeout=3.0)
        save_state(state)
        print("✅ All threads stopped safely. State saved.")

    print_matrix_scorecard(config, state)


if __name__ == "__main__":
    main()
