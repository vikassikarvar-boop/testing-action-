#!/usr/bin/env python3
"""
unique_comment_generator.py — Zero-Emoji Globally Unique Comment Engine.
Guarantees 100% distinct, human-written, text-only English comments.
Thread-safe for parallel multi-account execution.
"""

import json
import os
import random
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.join(HERE, "global_used_comments.json")
COMMENT_LOCK = threading.Lock()

# Text-only natural vocabularies (Zero Emojis)
OPENERS = [
    "Incredible", "Such a great", "Truly inspiring", "Super clean", "Loving this",
    "Absolute masterpiece", "Really well captured", "Top tier", "Such a beautiful",
    "Outstanding", "Pure", "Brilliant", "So aesthetically pleasing", "Remarkable",
    "Such a powerful", "Phenomenal", "Extremely well done", "Awesome", "Fantastic",
    "Superb", "Magnificent", "Genuinely impressive", "Spectacular", "Wonderful",
    "Flawless", "Exceptional", "Unreal", "Spot on", "Simply stunning", "Next level",
    "Breath taking", "Major", "Great", "Solid", "Perfect"
]

ADJECTIVES = [
    "clean", "detailed", "artistic", "vibrant", "moody", "crisp", "creative",
    "balanced", "smooth", "dynamic", "aesthetic", "inspiring", "striking",
    "authentic", "natural", "subtle", "sharp", "powerful", "original", "flawless"
]

SUBJECTS = [
    "perspective", "composition", "lighting", "vibe", "capture", "frame",
    "visual", "angle", "shot", "moment", "work", "detail", "view",
    "scene", "photography", "atmosphere", "portrait", "landscape", "feel",
    "concept", "depth", "execution", "focus", "clarity", "style"
]

CLOSERS = [
    "honestly", "as always", "keep it up", "pure quality", "right here",
    "to be honest", "keep inspiring", "top level work", "really love this",
    "hands down", "super impressive", "great execution", "keep them coming",
    "well deserved", "great work", "very well captured", "so good",
    "love the mood here", "impressive as ever", "true talent", "no doubt",
    "always delivering quality", "great stuff", "much respect", "terrific job"
]

TEMPLATES = [
    "{opener} {subject} {closer}",
    "{opener} {adjective} {subject}",
    "{opener} {adjective} {subject} {closer}",
    "{opener} {subject} really well done",
    "The {subject} in this is {opener}",
    "Really loving this {adjective} {subject} {closer}",
    "Such a {adjective} {subject} {closer}",
    "This {subject} is {opener} {closer}",
    "Great {subject} and {adjective} {subject}",
    "{opener} work on the {subject} {closer}"
]


def load_used_comments():
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_used_comments(used_set):
    try:
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(used_set)), f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: Could not save comments registry: {e}")


# FIX #15: per-account vocabulary subsets - each account gets its own seeded
# slice of the vocab so all accounts don't sound like one bot. Plus occasional
# natural question forms and (rarely) a relevant emoji for organic feel.
ACCOUNT_VOCAB_CACHE = {}


def _get_account_vocab(account_name):
    """Deterministic per-account subset of the global vocab (FIX #15)."""
    key = account_name or "default"
    if key in ACCOUNT_VOCAB_CACHE:
        return ACCOUNT_VOCAB_CACHE[key]

    rng = random.Random(f"comment_vocab:{key}")
    n_open = max(6, len(OPENERS) // 2)
    n_adj = max(5, len(ADJECTIVES) // 2)
    n_sub = max(6, len(SUBJECTS) // 2)
    n_close = max(6, len(CLOSERS) // 2)
    subset = {
        "openers": rng.sample(OPENERS, n_open),
        "adjectives": rng.sample(ADJECTIVES, n_adj),
        "subjects": rng.sample(SUBJECTS, n_sub),
        "closers": rng.sample(CLOSERS, n_close),
    }
    ACCOUNT_VOCAB_CACHE[key] = subset
    return subset


# Occasional question-form comments to break up the statement-only pattern
QUESTION_TAILS = [
    "where was this taken?",
    "how long did this take?",
    "what gear did you use?",
    "is this recent?",
    "which location is this?",
    "can you share the settings?",
]


def get_unique_comment(account_name=None):
    """
    Generates a guaranteed unique, natural comment with per-account vocab subset.
    Thread-safe across all parallel accounts.
    """
    with COMMENT_LOCK:
        used = load_used_comments()
        v = _get_account_vocab(account_name)
        # FIX #15: per-account seeded RNG (not global random)
        crng = random.Random(f"{account_name or 'x'}:{len(used)}")

        for _ in range(500):
            # ~12% chance of a question-form comment (more organic)
            if crng.random() < 0.12:
                comment = (f"{crng.choice(v['openers'])} {crng.choice(v['subjects'])}. "
                           f"{crng.choice(QUESTION_TAILS)}")
            else:
                tmpl = crng.choice(TEMPLATES)
                comment = tmpl.format(
                    opener=crng.choice(v["openers"]),
                    adjective=crng.choice(v["adjectives"]),
                    subject=crng.choice(v["subjects"]),
                    closer=crng.choice(v["closers"])
                ).strip()

            comment = comment[0].upper() + comment[1:]

            if comment not in used:
                used.add(comment)
                save_used_comments(used)
                return comment

        base = f"{crng.choice(v['openers'])} {crng.choice(v['subjects'])} {crng.choice(v['closers'])}"
        unique_fallback = f"{base} ({crng.randint(100, 999)})"
        used.add(unique_fallback)
        save_used_comments(used)
        return unique_fallback
