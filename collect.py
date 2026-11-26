#!/usr/bin/env python3
"""Collect public Bluesky posts/reposts from everyone an account follows."""
import argparse
import concurrent.futures
import datetime as dt
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

API = 'https://public.api.bsky.app/xrpc/'
USER_AGENT = 'OSINT-Morning-Edit-Public-Collector/0.1'
UTC = dt.timezone.utc


def api_get(endpoint, params):
    url = API + endpoint + '?' + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
            retry = exc.headers.get('Retry-After', '')
            try:
                wait = min(60, float(retry))
            except ValueError:
                wait = min(30, 2 ** attempt + random.random())
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            wait = min(30, 2 ** attempt + random.random())
        time.sleep(wait)


def iso_date(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def follows_of(actor):
    items = []
    cursor = None
    while True:
        args = {'actor': actor, 'limit': 100}
        if cursor:
            args['cursor'] = cursor
        result = api_get('app.bsky.graph.getFollows', args)
        items.extend(result.get('follows', []))
        next_cursor = result.get('cursor')
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return list({a['did']: a for a in items if a.get('did')}.values())


def get_recent_posts(account, cutoff, max_pages):
    handle = account['handle']
    cursor = None
    output = []
    partial = False
    for page in range(max_pages):
        args = {'actor': account['did'], 'limit': 100}
        if cursor:
            args['cursor'] = cursor
        result = api_get('app.bsky.feed.getAuthorFeed', args)
        feed = result.get('feed', [])
        if not feed:
            break
        oldest = None
        for event in feed:
            post = event.get('post') or {}
            record = post.get('record') or {}
            reason = event.get('reason') or {}
            repost = reason.get('$type', '').endswith('#reasonRepost')
            # For reposts, the activity time is when it was reposted.
            event_time = iso_date(reason.get('indexedAt') if repost else record.get('createdAt'))
            if event_time is None:
                continue
            oldest = event_time if oldest is None else min(oldest, event_time)
            if event_time < cutoff:
                continue
            uri = post.get('uri', '')
            author = post.get('author', {})
            if not uri.startswith('at://') or not author.get('handle'):
                continue
            rkey = uri.rsplit('/', 1)[-1]
            links = []
            for facet in record.get('facets', []):
                for feature in facet.get('features', []):
                    if feature.get('uri', '').startswith('https://'):
                        links.append(feature['uri'])
            embed = record.get('embed') or {}
            external = embed.get('external') or {}
            if external.get('uri'):
                links.append(external['uri'])
            output.append({
                'uri': uri,
                'url': f"https://bsky.app/profile/{author['handle']}/post/{rkey}",
                'author': author['handle'],
                'text': record.get('text', ''),
                'timestamp': event_time.isoformat(),
                'reposted': repost,
                'found_via': handle,
                'external_links': sorted(set(links)),
                'has_images': bool(embed.get('images') or (post.get('embed') or {}).get('images')),
            })
        next_cursor = result.get('cursor')
        if oldest is not None and oldest < cutoff:
            break
        if not next_cursor or next_cursor == cursor:
            break
        if page == max_pages - 1:
            partial = True
        cursor = next_cursor
    return output, partial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--actor', default='adamparkr.bsky.social')
    ap.add_argument('--hours', type=int, default=24)
    ap.add_argument('--max-pages', type=int, default=3,
                    help='maximum 100-post pages per account (busy accounts may be incomplete)')
    ap.add_argument('--workers', type=int, default=5)
    args = ap.parse_args()
    started = dt.datetime.now(UTC)
    cutoff = started - dt.timedelta(hours=args.hours)
    outdir = Path('output')
    outdir.mkdir(exist_ok=True)
    accounts = follows_of(args.actor)
    print(f'Found {len(accounts)} followed accounts', flush=True)
    if not accounts:
        raise RuntimeError('No followed accounts returned; collection not reliable')
    (outdir / 'accounts.json').write_text(json.dumps(accounts, indent=2, ensure_ascii=False))
    found = {}
    errors = []
    partial_accounts = []

    def worker(account):
        return get_recent_posts(account, cutoff, args.max_pages)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(worker, a): a for a in accounts}
        for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
            a = futures[future]
            try:
                posts, partial = future.result()
                if partial:
                    partial_accounts.append(a['handle'])
                for post in posts:
                    uri = post['uri']
                    if uri in found:
                        watchers = found[uri]['found_via_accounts']
                        if post['found_via'] not in watchers:
                            watchers.append(post['found_via'])
                    else:
                        post['found_via_accounts'] = [post.pop('found_via')]
                        found[uri] = post
            except Exception as exc:
                errors.append({'account': a['handle'], 'error': str(exc)[:180]})
            if idx % 100 == 0:
                print(f'Checked {idx}/{len(accounts)} accounts...', flush=True)
    posts = sorted(found.values(), key=lambda p: p['timestamp'], reverse=True)
    stats = {
        'actor': args.actor, 'run_at_utc': started.isoformat(), 'cutoff_utc': cutoff.isoformat(),
        'followed_accounts': len(accounts), 'accounts_checked': len(accounts) - len(errors),
        'accounts_failed': len(errors), 'posts_collected_unique': len(posts),
        'accounts_truncated_by_page_limit': len(partial_accounts),
        'note': 'Public Bluesky posts only; posts may be missing because of rate limits, deletions, or page limits. No editorial verification performed.'
    }
    (outdir / 'posts.json').write_text(json.dumps(posts, indent=2, ensure_ascii=False))
    (outdir / 'status.json').write_text(json.dumps({'stats': stats, 'errors': errors, 'partial_accounts': partial_accounts}, indent=2))
    lines = ['# Bluesky Social Radar: source material', '',
             f"Collected: {started.strftime('%Y-%m-%d %H:%M UTC')}",
             f"Accounts checked: {stats['accounts_checked']} / {len(accounts)}",
             f"Unique recent posts: {len(posts)}",
             f"Accounts with errors: {len(errors)}; high-volume accounts potentially incomplete: {len(partial_accounts)}", '',
             '**Unverified source posts, not editorial leads.**', '']
    for post in posts[:500]:
        short = ' '.join(post['text'].split())[:450]
        lines += [f"- **{post['author']}** ({post['timestamp']}) [Original post]({post['url']})" +
                  (' (repost)' if post['reposted'] else ''),
                  f"  {short}",
                  *([f"  Links: {', '.join(post['external_links'][:3])}"] if post['external_links'] else [])]
    if len(posts) > 500:
        lines += ['', f'Only the latest 500 shown; all {len(posts)} are in posts.json.']
    (outdir / 'posts.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(stats, indent=2), flush=True)
    if len(errors) == len(accounts):
        raise RuntimeError('All account feed requests failed; check status.json')

if __name__ == '__main__':
    main()
