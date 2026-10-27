"""Check the collection manifest and reference identities without network access."""
import csv
from pathlib import Path
import urllib.parse

from .common import FIELDS, Store, date_iso


def validate(root):
    store = Store(root)
    targets = store.read('targets.json')['targets']
    seen = set()
    for target in targets:
        identity = f'{target["country"]}/{target["division"]}/{target["season"]}'
        if target['id'] != identity or identity in seen:
            raise ValueError(f'Invalid or duplicated target identity: {identity}')
        seen.add(identity)
        if target['start_year'] != int(target['season'].split('-')[0]):
            raise ValueError(f'Season year mismatch: {identity}')
        sources = target['sources']
        if type(sources['sofascore']['tournament_id']) is not int:
            raise ValueError(f'Invalid tournament ID: {identity}')
        url = urllib.parse.urlsplit(sources['flashscore']['url'])
        if url.scheme != 'https' or url.hostname != 'www.flashscore.com' or not url.path.endswith('/results/'):
            raise ValueError(f'Invalid archive URL: {identity}')
        with store.path(target['reference']).open(newline='') as file:
            rows = list(csv.DictReader(file))
        if len(rows) != target['reference_matches'] or not rows:
            raise ValueError(f'Reference count mismatch: {identity}')
        for row in rows:
            date_iso(row['Date'])
            for score in ('FTHG', 'FTAG'):
                if row[score].strip():
                    int(row[score])
            if not row['HomeTeam'] or not row['AwayTeam'] or any(field not in row for field in FIELDS):
                raise ValueError(f'Incomplete reference identity: {identity}')
    return len(targets)


if __name__ == '__main__':
    count = validate(Path(__file__).resolve().parents[1])
    print(f'Validated {count} target seasons and their reference matches')
