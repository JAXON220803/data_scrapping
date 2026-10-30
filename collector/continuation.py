"""Queue another batch only when a source can do useful work now."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import time

from .run import Engine
from .status import needs_work


def continuation_plan(root, sources=('sofascore', 'flashscore'), now=None):
    now = time.time() if now is None else now
    engine = Engine(root)
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    states = engine.store.read('checkpoints/sources.json', {})
    limits = engine.store.read('checkpoints/limits.json', {})
    eligible, reasons = [], {}
    if not needs_work(root):
        return {'continue': False, 'source': 'both', 'reasons': {'all': 'collection_complete'}}
    for source in sources:
        if states.get(source, {}).get('retry_at', 0) > now:
            reasons[source] = 'source_cooldown'
            continue
        quota = limits.get(source, {})
        if quota.get('day') == day and quota.get('used', 0) >= engine.settings['daily_requests_per_source']:
            reasons[source] = 'daily_budget_exhausted'
            continue
        runnable = False
        for target in engine.targets:
            task = engine.task(target, source)
            if task['phase'] == 'done' or task.get('retry_at', 0) > now:
                continue
            if task['phase'] == 'inventory':
                runnable = True
                break
            fixtures = engine.store.read(engine.fixture_path(target, source), {})
            if all(f.get('statistics_status') for f in fixtures.values()) or any(
                not f.get('statistics_status') and f.get('retry_at', 0) <= now for f in fixtures.values()
            ):
                runnable = True
                break
        if runnable:
            eligible.append(source)
        else:
            reasons[source] = 'no_runnable_tasks'
    return {'continue': bool(eligible), 'source': eligible[0] if len(eligible) == 1 else 'both', 'reasons': reasons}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=('both', 'sofascore', 'flashscore'), default='both')
    args = parser.parse_args()
    sources = ('sofascore', 'flashscore') if args.source == 'both' else (args.source,)
    plan = continuation_plan(Path(__file__).resolve().parents[1], sources)
    print(json.dumps(plan, indent=2))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as file:
            file.write(f'continue={str(plan["continue"]).lower()}\nsource={plan["source"]}\n')
