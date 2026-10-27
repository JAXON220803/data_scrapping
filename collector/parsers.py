"""Pure parsers. Never derive missing observations from scores or averages."""

import datetime as dt
import json
import re

from bs4 import BeautifulSoup

from .common import date_iso


def season_year(season):
    for text in (str(season.get('year', '')), str(season.get('name', ''))):
        full = re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)', text)
        if full:
            return int(full[1])
        short = re.fullmatch(r'(\d{2})[/-]\d{2}', text)
        if short:
            return (2000 if int(short[1]) < 70 else 1900) + int(short[1])
    return None


def integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'\d+', value.strip()):
        return int(value)
    return None


def validate(shots):
    for total, on_target in (('HS', 'HST'), ('AS', 'AST')):
        if total in shots and on_target in shots and shots[on_target] > shots[total]:
            raise ValueError('Shots on target exceed total shots')
    return shots


def shot_fields(items):
    shots = {}
    for item in items:
        label, key = item['name'].casefold(), item.get('key')
        if label == 'total shots' or key in ('totalShots', 'totalShotsOnGoal'):
            suffix = 'S'
        elif label == 'shots on target' or key in ('shotsOnGoal', 'shotsOnTarget'):
            suffix = 'ST'
        else:
            continue
        for side, prefix in (('home', 'H'), ('away', 'A')):
            value = integer(item.get(side + 'Value', item.get(side)))
            if value is not None:
                field = prefix + suffix
                if field in shots and shots[field] != value:
                    raise ValueError('Conflicting shot totals in the same source')
                shots[field] = value
    return validate(shots)


def sofa_fixture(raw):
    status = raw.get('status', {})
    match_status = 'finished' if status.get('code') == 100 else status.get('type', 'unknown')
    if match_status == 'finished' and status.get('code') != 100:
        match_status = 'special_result'
    return {'id': str(raw['id']), 'source': 'sofascore',
            'date': dt.datetime.fromtimestamp(raw['startTimestamp'], dt.timezone.utc).date().isoformat(),
            'home': raw['homeTeam']['name'], 'away': raw['awayTeam']['name'],
            'home_id': raw['homeTeam'].get('id'), 'away_id': raw['awayTeam'].get('id'),
            'home_goals': raw.get('homeScore', {}).get('current'),
            'away_goals': raw.get('awayScore', {}).get('current'),
            'status': match_status,
            'round': raw.get('roundInfo', {}), 'raw_fixture': raw,
            'url': f'https://www.sofascore.com/football/match/{raw.get("customId", "") }#id:{raw["id"]}'}


def sofa_statistics(raw):
    items = [dict(item, group=group.get('groupName'))
             for period in raw.get('statistics', []) if period.get('period') == 'ALL'
             for group in period.get('groups', []) for item in group.get('statisticsItems', [])]
    return {'shots': shot_fields(items), 'statistics': items, 'raw_statistics': raw}


def environment(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script'):
        text = script.string or script.get_text()
        match = re.search(r'window\.environment\s*=\s*', text)
        if match:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.end():])
                return value
            except json.JSONDecodeError:
                continue
    return {}


def flash_fixtures(html):
    soup, fixtures = BeautifulSoup(html, 'html.parser'), {}
    for row in soup.select('[id^="g_1_"]'):
        link = row.select_one('a[href*="/match/"]')
        home, away = row.select_one('.event__homeParticipant'), row.select_one('.event__awayParticipant')
        date_node = row.select_one('[data-testid="wcl-stageTime"]')
        if not link or not home or not away or not date_node:
            continue
        match = re.search(r'\d{2}\.\d{2}\.\d{4}', date_node.get_text(' ', strip=True))
        if not match:
            continue
        def score(side):
            node = row.select_one('.event__score--' + side)
            found = re.match(r'\d+', node.get_text(strip=True)) if node else None
            return int(found[0]) if found else None
        event_id = row['id'].removeprefix('g_1_')
        status_text = date_node.get_text(' ', strip=True)
        def team_name(node):
            name = node.select_one('span[class*="wcl-name_"]')
            if name:
                return name.get_text(' ', strip=True)
            for icon in node.select('svg'):
                icon.decompose()
            return node.get_text(' ', strip=True)
        fixtures[event_id] = {'id': event_id, 'source': 'flashscore', 'date': date_iso(match[0]),
            'home': team_name(home), 'away': team_name(away),
            'home_goals': score('home'), 'away_goals': score('away'),
            'status': 'finished' if not any(word in status_text.lower() for word in ('awd', 'awarded', 'canc', 'abn', 'pen', 'aet')) else 'special_result',
            'source_status': status_text, 'url': link['href']}
    return list(fixtures.values())


def flash_statistics(html):
    soup, items = BeautifulSoup(html, 'html.parser'), []
    for row in soup.select('[data-testid="wcl-statistics"]'):
        label_row = row.find('div', class_=lambda value: value and 'labelRow' in value)
        children = label_row.find_all('div', recursive=False) if label_row else []
        if len(children) != 3:
            continue
        items.append({'name': children[1].get_text(' ', strip=True),
                      'home': children[0].get_text(' ', strip=True),
                      'away': children[2].get_text(' ', strip=True)})
    return {'shots': shot_fields(items), 'statistics': items}
