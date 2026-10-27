"""Durable checkpoints, conservative request budgets, and observation-only merges."""

import csv
import datetime as dt
import email.utils
import json
import os
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

FIELDS = ('HS', 'AS', 'HST', 'AST')
AGENT = 'FootballSeasonCollector/1.0'


class Deferred(Exception):
    pass


class Blocked(Exception):
    pass


class Missing(Exception):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Path must remain inside the collection repository')
        return path

    def read(self, relative, default=None):
        path = self.path(relative)
        return json.loads(path.read_text()) if path.exists() else default

    def text(self, relative, text):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        with tmp.open('w', encoding='utf-8', newline='') as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(path)

    def write(self, relative, value):
        self.text(relative, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')

    def csv(self, relative, rows, fields):
        import io
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        self.text(relative, stream.getvalue())


class Budget:
    def __init__(self, store, source, interval, daily, per_run, now=time.time,
                 sleep=time.sleep, deadline=None):
        self.store, self.source = store, source
        self.interval, self.daily, self.per_run = interval, daily, per_run
        self.now, self.sleep, self.deadline = now, sleep, deadline
        self.sent = 0

    def reserve(self):
        if self.deadline and self.now() >= self.deadline:
            raise Deferred('Run time budget reached; next scheduled run resumes')
        limits = self.store.read('checkpoints/limits.json', {})
        day = dt.datetime.fromtimestamp(self.now(), dt.timezone.utc).date().isoformat()
        row = limits.get(self.source, {})
        used = row.get('used', 0) if row.get('day') == day else 0
        if used >= self.daily:
            raise Deferred(f'Daily request budget reached ({used}/{self.daily}); resumes next UTC day')
        if self.sent >= self.per_run:
            raise Deferred(f'Run request budget reached ({self.sent}/{self.per_run})')
        wait = max(0, row.get('last_request', 0) + self.interval - self.now())
        if self.deadline and self.now() + wait >= self.deadline:
            raise Deferred('Not enough run time for the next request')
        if wait:
            self.sleep(wait)
        # Persist before I/O. A crash or failed connection must not reset quotas.
        limits[self.source] = {'day': day, 'used': used + 1, 'last_request': self.now()}
        self.store.write('checkpoints/limits.json', limits)
        self.sent += 1


def robots_allowed(text, url):
    """Evaluate wildcard Allow/Disallow rules using the longest matching rule."""
    groups, agents, rules = [], [], []
    for line in text.splitlines() + ['User-agent: __end__']:
        key, sep, value = line.split('#', 1)[0].partition(':')
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == 'user-agent':
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ('allow', 'disallow') and agents and value:
            rules.append((key == 'allow', value))
    matching = [(max((len(agent) if agent != '*' else 0) for agent in names
                     if agent == '*' or agent in AGENT.lower()), rules)
                for names, rules in groups
                if any(agent == '*' or agent in AGENT.lower() for agent in names)]
    if not matching:
        return True
    specificity = max(item[0] for item in matching)
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parsed.path + ('?' + parsed.query if parsed.query else ''))
    hits = []
    for score, rules in matching:
        if score != specificity:
            continue
        for allow, pattern in rules:
            end = pattern.endswith('$')
            expression = '^' + re.escape(pattern[:-1] if end else pattern).replace(r'\*', '.*') + ('$' if end else '')
            if re.search(expression, path):
                hits.append((len(pattern.replace('*', '')), allow))
    return max(hits, default=(0, True))[1]


class Http:
    # Exact data hosts observed in the normal public Flashscore browser interface.
    HOSTS = {'www.sofascore.com', 'www.flashscore.com', 'global.flashscore.ninja', '2.ds.lsapp.eu'}

    def __init__(self, budget):
        self.budget = budget

    def _request(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in self.HOSTS:
            raise ValueError('Unexpected collection host')
        self.budget.reserve()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        request = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': '*/*'})
        try:
            response = urllib.request.build_opener(NoRedirect()).open(request, timeout=35)
        except urllib.error.HTTPError as error:
            response = error
        except urllib.error.URLError as error:
            reason = str(error.reason)[:200]
            raise OSError(f'Network failure: {type(error.reason).__name__}: {reason}') from None
        with response:
            status = response.code
            raw = response.read(15 * 1024 * 1024 + 1)
            if len(raw) > 15 * 1024 * 1024:
                raise ValueError('Response exceeds the 15 MB size limit')
            headers = {key.lower(): value for key, value in response.headers.items()}
        if status in (401, 403):
            raise Blocked(f'HTTP {status}: source denied access')
        if status == 429:
            value = headers.get('retry-after', '3600')
            try:
                seconds = float(value)
            except ValueError:
                try:
                    seconds = email.utils.parsedate_to_datetime(value).timestamp() - self.budget.now()
                except (ValueError, TypeError):
                    seconds = 3600
            raise Blocked(f'HTTP 429: retry after {max(60, int(seconds))} seconds')
        if status == 404:
            raise Missing('HTTP 404: resource is not available')
        if status >= 500:
            raise OSError(f'Source server returned HTTP {status}')
        if status != 200:
            raise Blocked(f'Unexpected HTTP {status}; no redirect or alternate-host bypass attempted')
        return raw.decode('utf-8', errors='strict')

    def check_robots(self, url):
        host = urllib.parse.urlsplit(url).hostname
        if host not in self.HOSTS:
            raise ValueError('Unexpected host')
        policies = self.budget.store.read('checkpoints/robots.json', {})
        policy = policies.get(host)
        if not policy or policy['expires'] <= self.budget.now():
            try:
                text = self._request(f'https://{host}/robots.txt')
            except Missing:
                text = 'User-agent: *\nAllow: /\n'
            policy = {'text': text, 'expires': self.budget.now() + 86400}
            policies[host] = policy
            self.budget.store.write('checkpoints/robots.json', policies)
        if not robots_allowed(policy['text'], url):
            raise Blocked('robots.txt disallows this path')

    def text(self, url):
        self.check_robots(url)
        return self._request(url)

    def json(self, url):
        return json.loads(self.text(url))


def normalized(value, aliases):
    value = aliases.get(value, value)
    value = unicodedata.normalize('NFKD', value.casefold()).replace('ı', 'i')
    words = re.findall(r'[a-z0-9]+', ''.join(c for c in value if not unicodedata.combining(c)))
    for token in ('fc', 'cf', 'afc', 'sc', 'ac', 'sv', 'vfl', 'vfb', 'uc', 'us', 'ssc'):
        words = [word for word in words if word != token]
    return ''.join(words)


def date_iso(value):
    for pattern in ('%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y', '%d.%m.%Y'):
        try:
            return dt.datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Unrecognized match date')


def merge_rows(originals, records, aliases):
    rows, changes = [], []
    for original in originals:
        row = dict(original)
        # Postponed/unfinished reference matches have no verified score to match.
        if not all(str(row.get(score, '')).strip().isdigit() for score in ('FTHG', 'FTAG')):
            rows.append(row)
            continue
        matches = [r for r in records if r.get('status') == 'finished'
                   and r['date'] == date_iso(row['Date'])
                   and normalized(r['home'], aliases) == normalized(row['HomeTeam'], aliases)
                   and normalized(r['away'], aliases) == normalized(row['AwayTeam'], aliases)
                   and r.get('home_goals') == int(row['FTHG']) and r.get('away_goals') == int(row['FTAG'])]
        for source in ('sofascore', 'flashscore'):
            candidates = [r for r in matches if r['source'] == source]
            if len(candidates) != 1:
                continue
            match = candidates[0]
            proposed = {field: value for field, value in match.get('shots', {}).items()
                        if not str(row.get(field, '')).strip() and type(value) is int and value >= 0}
            for total, on_target in (('HS', 'HST'), ('AS', 'AST')):
                total_value = proposed.get(total, row.get(total, ''))
                target_value = proposed.get(on_target, row.get(on_target, ''))
                if str(total_value).strip() and str(target_value).strip() and int(target_value) > int(total_value):
                    proposed.pop(total, None)
                    proposed.pop(on_target, None)
            for field, value in proposed.items():
                row[field] = str(value)
                changes.append({'date': row['Date'], 'home': row['HomeTeam'], 'away': row['AwayTeam'],
                                'field': field, 'value': value, 'source': source, 'fixture_id': match['id']})
        rows.append(row)
    return rows, changes
