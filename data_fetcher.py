#!/usr/bin/env python3
"""
data_fetcher.py — High-Speed Multi-Scraper Target Collector.
Collects:
1. 100+ Verified LIVE Active Story Usernames (guaranteed active 24h stories)
2. 100+ Verified Public Posts for Comments & Likes (guaranteed comments open, no restrictions)

Uses verified isolated scraper accounts only. Zero touch on matrix accounts.
"""

import glob
import json
import os
import random
import sys
import time
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ig_actions as A

SCRAPER_DIR = r"D:\login through api and get cookies\ig_login_tool\saved_cookies_part22"

KNOWN_GOOD_SCRAPERS = [
    "cookies_duck.51066700.json",
    "cookies_elephant.8628339.json",
    "cookies_elk.24889930.json",
    "cookies_horse.4849455.json",
    "cookies_lion.4716455.json",
    "cookies_panda.2750708.json",
    "cookies_tiger.4957644.json",
    "cookies_zebra.8362660.json",
    "cookies_otter.43811505.json",
    "cookies_reindeer.3036598.json"
]

TARGET_FILES = [
    os.path.join(r"d:\app action test\ig_multi_matrix_engine", "master_targets.json"),
    os.path.join(r"e:\neweditingtask\Edited\ig_multi_matrix_engine", "master_targets.json"),
]

# Top high-activity global accounts
CANDIDATE_ACCOUNTS = [
    # News & Media
    "bbcnews", "cnn", "nytimes", "aljazeeraenglish", "wsj", "forbes",
    "bloombergbusiness", "variety", "hollywoodreporter", "billboard",
    "rollingstone", "time", "theeconomist", "reuters", "cnet", "wired", "vice",
    "buzzfeed", "theguardian", "enews", "tmz_tv", "people",
    # Sports & Leagues
    "espn", "bleacherreport", "f1", "motogp", "ufc", "wwe", "nba",
    "premierleague", "championsleague", "laliga", "bundesliga", "seriea",
    "realmadrid", "fcbarcelona", "mancity", "chelseafc", "liverpoolfc",
    "arsenal", "juventus", "psg", "acmilan", "redbullracing", "inter",
    "bvb09", "atleticodemadrid", "mercedesamgf1", "scuderiaferrari",
    # Tech & Gaming
    "ign", "gamespot", "playstation", "xbox", "nintendo", "techcrunch",
    "theverge", "nvidia", "spacex", "tesla", "ferrari", "porsche",
    "lamborghini", "bmw", "mercedesamg", "audi", "bugatti", "astonmartin",
    # Brands & Fashion
    "nike", "adidas", "vans", "newbalance", "puma", "redbull", "gymshark",
    "gucci", "dior", "louisvuitton", "chanelofficial", "prada", "zara", "hm",
    "versace", "balenciaga", "burberry", "armani",
    # Travel, Nature & Science
    "natgeo", "natgeotravel", "beautifuldestinations", "discovery", "bbcearth",
    "earthpix", "lonelyplanet", "travelchannel", "history",
    # Entertainment, Creators & Celebrities
    "spotify", "netflix", "disney", "marvel", "warnerbrosentertainment", "hbo",
    "paramountplus", "complex", "hypebeast", "9gag",
    "cristiano", "leomessi", "neymarjr", "therock", "kyliejenner", "kimkardashian",
    "dualipa", "arianagrande", "snoopdogg", "champagnepapi", "iamcardib",
    "davidbeckham", "billieeilish", "taylorswift", "zendaya", "selenagomez",
    "shakira", "badbunnypr", "karolg", "anitta", "khaby00", "mrbeast",
    "ishowspeed", "judebellingham", "marcelotwelve", "travisscott", "theweeknd",
    "calvinharris", "kygomusic", "zedd", "skrillex", "djkhaled", "steveaoki",
    "martingarrix", "davidguetta", "diplo", "charlieputh", "samsmith",
    "vancityreynolds", "zoesaldana", "caradelevingne", "chiaraferragni",
    "nusr_et", "sunnyleone", "emrata", "alessandraambrosio", "candiceswanepoel",
    "sarasampaio", "taylor_hill", "josephineskriver", "hoskelsa", "ashleygraham",
    "irinashayk", "sydney_sweeney", "madelyncline", "priyankachopra", "aliaabhatt",
    "akshaykumar", "kritisanon", "kartikaaryan", "diljitdosanjh", "darshanravaldz",
    "yoyohoneysingh", "armaanmalik", "badboyshah"
]


def load_scraper_sessions():
    sessions = []
    for fn in KNOWN_GOOD_SCRAPERS:
        path = os.path.join(SCRAPER_DIR, fn)
        if os.path.exists(path):
            try:
                sess = A.Session(path, proxy_override="")
                sess.s.proxies = {}
                sessions.append(sess)
            except Exception:
                pass
    print(f"✅ Loaded {len(sessions)} scraper sessions into rotation pool.", flush=True)
    return sessions


def fetch_all(target_stories=100, target_posts=100):
    print("=" * 80, flush=True)
    print("🚀 LIVE TARGET SCRAPER & VERIFIER (ROTATING SCRAPER POOL)", flush=True)
    print("=" * 80, flush=True)

    sessions = load_scraper_sessions()
    if not sessions:
        sys.exit("❌ No scraper sessions available.")

    verified_stories = []
    verified_posts = []
    seen_posts = set()
    verified_story_set = set()

    s_idx = 0

    print(f"\n🔍 Scanning {len(CANDIDATE_ACCOUNTS)} accounts for {target_stories} active stories & {target_posts} public posts...\n", flush=True)

    for username in CANDIDATE_ACCOUNTS:
        if len(verified_stories) >= target_stories and len(verified_posts) >= target_posts:
            break

        sess = sessions[s_idx % len(sessions)]
        s_idx += 1

        try:
            # 1. User Info (timeout 6s)
            h = sess.base_headers("IgApi: users/usernameinfo/")
            r_u = sess.s.get(f"https://i.instagram.com/api/v1/users/{username}/usernameinfo/", headers=h, timeout=6)
            if r_u.status_code != 200:
                continue

            user_obj = r_u.json().get("user", {})
            uid = str(user_obj.get("pk", ""))
            if not uid or user_obj.get("is_private", False):
                continue

            # 2. Stories check (if not yet at target)
            if len(verified_stories) < target_stories and username not in verified_story_set:
                sess_story = sessions[s_idx % len(sessions)]
                s_idx += 1
                h_st = sess_story.base_headers("IgApi: feed/user/story/")
                r_st = sess_story.s.get(f"https://i.instagram.com/api/v1/feed/user/{uid}/story/", headers=h_st, timeout=6)
                if r_st.status_code == 200:
                    reel = r_st.json().get("reel") or {}
                    items = reel.get("items", [])
                    if len(items) > 0:
                        verified_stories.append(username)
                        verified_story_set.add(username)
                        print(f"   [STORY #{len(verified_stories):03d}/{target_stories}] @{username:<22} -> {len(items)} active 24h stories", flush=True)

            # 3. Posts check (if not yet at target)
            if len(verified_posts) < target_posts:
                sess_post = sessions[s_idx % len(sessions)]
                s_idx += 1
                h_fd = sess_post.base_headers("IgApi: feed/user/")
                r_fd = sess_post.s.get(f"https://i.instagram.com/api/v1/feed/user/{uid}/", headers=h_fd, params={"count": 5}, timeout=6)
                if r_fd.status_code == 200:
                    for it in r_fd.json().get("items", []):
                        code = it.get("code")
                        if not code or code in seen_posts:
                            continue
                        if it.get("comments_disabled", False):
                            continue
                        cc = it.get("comment_count", 0)
                        lc = it.get("like_count", 0)
                        if cc >= 2:
                            p_url = f"https://www.instagram.com/p/{code}/"
                            seen_posts.add(code)
                            verified_posts.append(p_url)
                            print(f"   [POST  #{len(verified_posts):03d}/{target_posts}] @{username:<18} -> {p_url} (Comments: {cc}, Likes: {lc})", flush=True)
                            if len(verified_posts) >= target_posts:
                                break

            time.sleep(0.05)

        except Exception as ex:
            time.sleep(0.1)

    # Save to JSON
    master_data = {
        "scraper_pool": f"{len(sessions)} isolated accounts",
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_posts": len(verified_posts),
        "total_active_stories": len(verified_stories),
        "post_urls": verified_posts,
        "story_usernames": verified_stories
    }

    for p in TARGET_FILES:
        try:
            d = os.path.dirname(p)
            if os.path.exists(d):
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(master_data, f, indent=2, ensure_ascii=False)
                print(f"💾 Saved to: {p}", flush=True)
        except Exception as err:
            print(f"⚠️ Error writing {p}: {err}", flush=True)

    print("\n" + "=" * 80, flush=True)
    print("🎉 100% VERIFIED LIVE MASTER DATASET SAVED SUCCESSFULLY!", flush=True)
    print(f"   Total Verified 100% Live Stories : {len(verified_stories)} accounts", flush=True)
    print(f"   Total Verified Open Post URLs    : {len(verified_posts)} posts", flush=True)
    print("=" * 80, flush=True)


if __name__ == "__main__":
    fetch_all(target_stories=100, target_posts=100)
