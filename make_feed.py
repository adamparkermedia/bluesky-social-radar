#!/usr/bin/env python3
"""Publish a transparent, unverified candidate shortlist from public Bluesky posts."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path('output/posts.json')
DEST = Path('feed.json')

# Broad editorial discovery indicators, not a claim of verification.
SIGNALS = {
    'satellite': 5, 'imagery': 4, 'geolocat': 5, 'verified': 3,
    'investigat': 4, 'evidence': 4, 'document': 3, 'filing': 4,
    'osint': 5, 'ship': 2, 'vessel': 3, 'naval': 3, 'convoy': 4,
    'maritime': 3, 'harbour': 3, 'harbor': 3, 'port': 1,
    'russian': 2, 'russia': 2, 'ukraine': 1, 'drone': 3,
    'military': 2, 'aircraft': 3, 'radar': 3, 'jamming': 5,
    'phishing': 5, 'cyber': 3, 'disinformation': 4,
    'smuggl': 4, 'sanction': 4, 'ownership': 4, 'landfill': 4,
    'pollution': 4, 'environment': 2, 'toxic': 4,
    'exclusive': 2, 'new report': 2, 'new research': 3,
    'new data': 3, 'new investigation': 5,
}


def score(post):
    text = ' '.join(str(post.get('text') or '').split())
    if len(text) < 65:
        return 0
    lower = text.lower()
    value = sum(weight for term, weight in SIGNALS.items() if term in lower)
    if post.get('external_links'):
        value += 2
    if post.get('has_images'):
        value += 1
    if post.get('reposted'):
        value -= 1
    # A high threshold avoids filling the feed with general commentary.
    return value if value >= 5 else 0


def main():
    posts = json.loads(SOURCE.read_text(encoding='utf-8'))
    seen = set()
    ranked = []
    for post in posts:
        url = post.get('url', '')
        if not url.startswith('https://bsky.app/profile/') or url in seen:
            continue
        seen.add(url)
        points = score(post)
        if points:
            ranked.append((points, post))
    ranked.sort(key=lambda pair: (pair[0], pair[1].get('timestamp', '')), reverse=True)
    stories = []
    for points, post in ranked[:25]:
        content = ' '.join(str(post.get('text') or '').split())
        title = re.sub(r'\s+', ' ', content[:115]).strip()
        if len(content) > 115:
            title = title.rsplit(' ', 1)[0] + '...'
        stories.append({
            'title': title,
            'summary': f"Unverified post by @{post.get('author', 'unknown')}: {content[:400]}",
            'url': post['url'],
            'posted_at': post.get('timestamp'),
            'score': points,
        })
    feed = {
        'updated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'description': 'Automatically filtered public posts; not verified or editorially endorsed.',
        'posts_scanned': len(posts),
        'stories': stories,
    }
    DEST.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {len(stories)} candidates from {len(posts)} posts to {DEST}')


if __name__ == '__main__':
    main()
