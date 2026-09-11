#!/usr/bin/env python3
"""
Scrape Instagram posts that MENTION (tag) a given account, using the Apify
Instagram Scraper actor, and save them to data/mentions.json.

Usage:
    export APIFY_TOKEN="your_apify_api_token"
    python scripts/scrape_mentions.py <instagram_username> [--limit 50] [--since "3 months"]

Examples:
    python scripts/scrape_mentions.py nike
    python scripts/scrape_mentions.py nike --limit 100 --since "2 months"

The Apify token is read from the APIFY_TOKEN environment variable, so it never
lives in the code or gets committed to the repo.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from apify_client import ApifyClient
from dotenv import load_dotenv

# apify/instagram-scraper (actor id: shu8hvrXbJbY3Eb9W)
ACTOR_ID = "apify/instagram-scraper"

# Instagram account whose mentions we scrape (override on the CLI if needed).
# The live account is cirqle.co.uk — see backend/app/instagram.py's BRAND_HANDLE.
ACCOUNT = "cirqle.co.uk"

# Repo root is the parent of this script's folder; write results into data/.
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "mentions.json"


def scrape_mentions(username, limit, since):
    """Run the actor in 'mentions' mode and return the scraped posts as a list."""
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("Error: set the APIFY_TOKEN environment variable first "
                 "(e.g. export APIFY_TOKEN=...).")

    client = ApifyClient(token)

    run_input = {
        "directUrls": [f"https://www.instagram.com/{username}/"],
        "resultsType": "mentions",          # posts where this profile is tagged
        "resultsLimit": limit,
    }
    if since:
        run_input["onlyPostsNewerThan"] = since

    print(f"Scraping mentions of @{username} … (this can take a minute)")

    # .call() starts the run and blocks until it finishes.
    run = client.actor(ACTOR_ID).call(run_input=run_input)

    # Results land in a "dataset"; pull every item into a plain list.
    dataset = client.dataset(run["defaultDatasetId"])
    return list(dataset.iterate_items())


def print_posts(posts):
    """Print a readable summary of every scraped post to the terminal."""
    print(f"\nFetched {len(posts)} mention post(s):")
    for i, post in enumerate(posts):
        caption = (post.get("caption") or "").replace("\n", " ").strip()
        print("-" * 60)
        print(f"[{i + 1}] @{post.get('ownerUsername')}  ({post.get('ownerFullName')})")
        print(f"    type:     {post.get('type')}")
        print(f"    posted:   {post.get('timestamp')}")
        print(f"    likes:    {post.get('likesCount')}    comments: {post.get('commentsCount')}")
        print(f"    url:      {post.get('url')}")
        print(f"    image:    {post.get('displayUrl')}")
        print(f"    caption:  {caption}")
    print("-" * 60)


def main():
    load_dotenv()  # loads APIFY_TOKEN from a local .env file if one exists
    parser = argparse.ArgumentParser(
        description="Scrape Instagram mentions of an account via Apify.")
    parser.add_argument("username", nargs="?", default=ACCOUNT,
                        help=f"Instagram account whose mentions to fetch (default: {ACCOUNT}).")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max posts to fetch (default: 50).")
    parser.add_argument("--since", default=None,
                        help='Only posts newer than, e.g. "3 months" or "2026-01-01".')
    args = parser.parse_args()

    posts = scrape_mentions(args.username, args.limit, args.since)

    print_posts(posts)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(posts, indent=2, ensure_ascii=False))
    print(f"Saved {len(posts)} posts → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
