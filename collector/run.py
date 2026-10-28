"""Run a bounded collection batch; checkpoints and exports are committed by CI."""

import argparse
import csv
import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
import time

from .common import Blocked, Budget, Deferred, FIELDS, Http, Missing, Store, merge_rows
from .sources import Flashscore, Sofascore

LOG = logging.getLogger('collector')


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Engine:
    def __init__(self, root, options=None, now=time.time, sleep=time.sleep, adapters=None):
        self.store = Store(root)
        self.settings = self.store.read('settings.json')
        self.targets = self.store.read('targets.json')['targets']
        self.options = options or {}
        self.now, self.sleep = now, sleep
        self.adapters = adapters or {}
        self.run_summary = {}

    def task_path(self, target, source):
        return f'checkpoints/tasks/{target["id"]}/{source}.json'

    def fixture_path(self, target, source):
        return f'data/{target["id"]}/{source}/fixtures.json'

    def task(self, target, source):
        task = self.store.read(self.task_path(target, source), {})
        version = signature([target['id'], target['sources'].get(source)])
        if task.get('signature') != version:
            task = {'signature': version, 'phase': 'inventory', 'next_page': 0}
        return task

    def run_source(self, source, deadline):
        source_states = self.store.read('checkpoints/sources.json', {})
        state = source_states.get(source, {})
        if self.options.get('retry_blocked'):
            source_states.pop(source, None)
        if state.get('retry_at', 0) > self.now() and not self.options.get('retry_blocked'):
            self.run_summary[source] = {**state, 'status': 'waiting_after_error',
                                       'previous_status': state.get('status'), 'requests': 0}
            return
        budget = Budget(self.store, source, self.settings['interval_seconds'],
                        self.settings['daily_requests_per_source'],
                        self.options.get('max_requests') or self.settings['requests_per_source_per_run'],
                        now=self.now, sleep=self.sleep, deadline=deadline)
        http = Http(budget)
        adapter = self.adapters.get(source)
        if adapter is None:
            browser_options = {**self.settings, 'browser_executable':
                               self.options.get('browser_executable') or self.settings.get('browser_executable')}
            adapter = Sofascore(http, self.store) if source == 'sofascore' else Flashscore(http, self.store, browser_options)
        current = None
        priority = {division: index for index, division in enumerate(self.settings.get('league_priority', []))}
        tasks = sorted(self.targets, key=lambda t: (not t['whole_season_missing'],
                        priority.get(t['division'], 100), -t['start_year'], t['id']))
        try:
            for target in tasks:
                if self.options.get('target') and target['id'] != self.options['target']:
                    continue
                task = self.task(target, source)
                if self.options.get('retry_blocked'):
                    task.pop('retry_at', None)
                current = target['id']
                if task['phase'] == 'done' or task.get('retry_at', 0) > self.now():
                    continue
                if not target['sources'].get(source):
                    task.update(phase='done', reason='source_not_configured')
                    self.store.write(self.task_path(target, source), task)
                    continue
                fixtures = self.store.read(self.fixture_path(target, source), {})
                LOG.info('%s %s: phase=%s fixtures=%s', source, current, task['phase'], len(fixtures))
                try:
                    while task['phase'] == 'inventory':
                        adapter.inventory_step(target, task, fixtures)
                        self.store.write(self.fixture_path(target, source), fixtures)
                        self.store.write(self.task_path(target, source), task)
                        LOG.info('%s %s: inventory=%s next_page=%s complete=%s', source, current,
                                 len(fixtures), task.get('next_page'), task.get('inventory_complete', False))
                        if task.get('next_page', 0) > self.settings['max_pages_per_season']:
                            raise ValueError('Configured maximum page count exceeded')
                    if task['phase'] == 'statistics':
                        for fixture in sorted(fixtures.values(), key=lambda f: (f['date'], f['id'])):
                            if fixture.get('statistics_status'):
                                continue
                            if fixture.get('retry_at', 0) > self.now() and not self.options.get('retry_blocked'):
                                continue
                            try:
                                result = adapter.statistics(fixture)
                            except (ValueError, Missing) as error:
                                fixture.update(last_error=str(error)[:250], retry_at=self.now() + 86400)
                                task['last_error'] = f'Match {fixture["id"]}: {str(error)[:180]}'
                                self.store.write(self.fixture_path(target, source), fixtures)
                                LOG.warning('%s %s match %s deferred: %s', source, current, fixture['id'], error)
                                continue
                            fixture.update(result)
                            fixture.pop('last_error', None)
                            fixture.pop('retry_at', None)
                            self.store.write(self.fixture_path(target, source), fixtures)
                            LOG.info('%s %s %s %s–%s: %s shots=%s', source, current, fixture['date'],
                                     fixture['home'], fixture['away'], fixture['statistics_status'], fixture.get('shots', {}))
                        if all(f.get('statistics_status') for f in fixtures.values()) and task.get('inventory_complete'):
                            task.update(phase='done', reason='all_source_fixtures_checked')
                            task.pop('last_error', None)
                        elif all(f.get('statistics_status') for f in fixtures.values()):
                            task.update(phase='inventory', retry_at=task.get('inventory_retry_at', self.now() + 86400))
                except (ValueError, Missing) as error:
                    task.update(last_error=str(error)[:250], retry_at=self.now() + 86400,
                                errors=task.get('errors', 0) + 1)
                    LOG.warning('%s %s needs review: %s', source, current, error)
                finally:
                    # This includes a quota pause midway through inventory or match downloads.
                    self.store.write(self.fixture_path(target, source), fixtures)
                    self.store.write(self.task_path(target, source), task)
            source_states[source] = {'status': 'batch_processed'}
            self.run_summary[source] = {'status': 'batch_processed', 'requests': budget.sent}
        except Deferred as error:
            self.run_summary[source] = {'status': 'paused_by_budget', 'reason': str(error),
                                        'requests': budget.sent, 'target': current}
        except (Blocked, OSError, Missing) as error:
            # Missing catalogue/page is a reviewable source error, never fabricated data.
            delay = self.settings['blocked_retry_hours'] * 3600 if isinstance(error, Blocked) else 3600
            state = {'status': type(error).__name__, 'reason': str(error)[:250], 'retry_at': self.now() + delay,
                     'target': current}
            source_states[source] = state
            self.run_summary[source] = {**state, 'requests': budget.sent}
            LOG.warning('%s paused: %s', source, error)
        except Exception as error:
            state = {'status': 'adapter_error', 'reason': f'{type(error).__name__}: {str(error)[:180]}',
                     'retry_at': self.now() + 3600, 'target': current}
            source_states[source] = state
            self.run_summary[source] = {**state, 'requests': budget.sent}
            LOG.exception('%s adapter failed; checkpoints retained', source)
        finally:
            adapter.close()
            self.store.write('checkpoints/sources.json', source_states)

    def export(self):
        season_reports, merged_changes = [], []
        done = fixtures_total = stats_total = full_shots = reference_total = remaining_total = 0
        fields = ['id', 'source', 'date', 'home', 'away', 'home_goals', 'away_goals', 'status',
                  'statistics_status', 'HS', 'AS', 'HST', 'AST', 'statistics_json', 'url', 'statistics_url']
        for target in self.targets:
            all_records, providers = [], {}
            for source in ('sofascore', 'flashscore'):
                task = self.task(target, source)
                records = list(self.store.read(self.fixture_path(target, source), {}).values())
                done += task['phase'] == 'done'
                available = sum(bool(r.get('statistics')) for r in records)
                shots = sum(all(key in r.get('shots', {}) for key in FIELDS) for r in records)
                checked = sum(bool(r.get('statistics_status')) for r in records)
                fixtures_total += len(records)
                stats_total += available
                full_shots += shots
                providers[source] = {'phase': task['phase'], 'fixtures': len(records), 'statistics_checked': checked,
                    'statistics_available': available, 'all_shot_values': shots,
                    'inventory_complete': task.get('inventory_complete', False),
                    'inventory_shortfall': max(0, target['reference_matches'] - len(records)) if task.get('inventory_complete') else None,
                    'last_error': task.get('last_error'), 'reason': task.get('reason')}
                if records:
                    export_rows = [{**{key: r.get(key, '') for key in fields}, **r.get('shots', {}),
                                    'statistics_json': json.dumps(r.get('statistics', []), ensure_ascii=False)} for r in records]
                    self.store.csv(f'data/{target["id"]}/{source}/matches.csv', export_rows, fields)
                all_records.extend(records)
            with self.store.path(target['reference']).open(newline='', encoding='utf-8') as file:
                originals = list(csv.DictReader(file))
            merged, changes = merge_rows(originals, all_records, self.settings['aliases'])
            for change in changes:
                change['target'] = target['id']
            merged_changes.extend(changes)
            output = f'filled/{target["country"]}/{target["season"]}/{target["division"]}.csv'
            self.store.csv(output, merged, list(originals[0]) if originals else [])
            remaining = sum(any(not str(row.get(key, '')).strip() for key in FIELDS) for row in merged)
            reference_total += len(merged)
            remaining_total += remaining
            season_reports.append({'id': target['id'], 'reference_matches': len(merged),
                                   'remaining_missing_shots': remaining, 'cells_recovered': len(changes), 'sources': providers})
        complete = done == len(self.targets) * 2
        report = {'updated_utc': dt.datetime.fromtimestamp(self.now(), dt.timezone.utc).isoformat(),
                  'status': 'complete_with_gaps' if complete and remaining_total else 'complete' if complete else 'in_progress',
                  'manifest_signature': signature([self.store.read('targets.json'), self.settings]),
                  'target_seasons': len(self.targets), 'source_tasks_done': done, 'source_tasks_total': len(self.targets) * 2,
                  'provider_fixture_records': fixtures_total, 'provider_records_with_statistics': stats_total,
                  'provider_records_with_all_shots': full_shots, 'reference_matches': reference_total,
                  'reference_matches_still_missing_shots': remaining_total, 'recovered_cells': len(merged_changes),
                  'last_run': self.run_summary, 'seasons': season_reports}
        self.store.write('progress.json', report)
        self.store.write('data/provenance.json', merged_changes)
        if complete:
            self.store.write('COMPLETE.json', {key: report[key] for key in ('status', 'updated_utc', 'manifest_signature')})
        else:
            self.store.path('COMPLETE.json').unlink(missing_ok=True)
        public_lines = [f'# Collection progress: {report["status"]}', '',
            f'Updated: {report["updated_utc"]}', '',
            f'- Whole league-seasons selected: {len(self.targets)}',
            f'- Provider season tasks completed: {done}/{len(self.targets) * 2}',
            f'- Provider match records downloaded: {fixtures_total}',
            f'- Provider records with all four shot values: {full_shots}',
            f'- Previously empty reference cells recovered: {len(merged_changes)}',
            f'- Reference matches still missing shot values: {remaining_total}', '',
            '| Season | Sofa fixtures / checked / with shots | Flash fixtures / checked / with shots | Remaining gaps |',
            '|---|---:|---:|---:|']
        for season in season_reports:
            values = [season['sources'][source] for source in ('sofascore', 'flashscore')]
            public_lines.append(f'| {season["id"]} | ' + ' | '.join(f'{v["fixtures"]} / {v["statistics_checked"]} / {v["all_shot_values"]}' for v in values)
                                + f' | {season["remaining_missing_shots"]} |')
        self.store.text('PROGRESS.md', '\n'.join(public_lines) + '\n')
        return report

    def run(self, sources=('sofascore', 'flashscore')):
        finished = self.store.read('COMPLETE.json', {})
        current = signature([self.store.read('targets.json'), self.settings])
        if finished.get('manifest_signature') == current and not self.options.get('retry_unavailable'):
            LOG.info('Collection pass already complete; no requests made')
            return self.store.read('progress.json')
        if self.options.get('retry_unavailable'):
            for target in self.targets:
                if self.options.get('target') and target['id'] != self.options['target']:
                    continue
                for source in sources:
                    fixtures = self.store.read(self.fixture_path(target, source), {})
                    for fixture in fixtures.values():
                        if fixture.get('statistics_status') in ('unavailable', 'not_completed'):
                            fixture.pop('statistics_status', None)
                    self.store.write(self.fixture_path(target, source), fixtures)
                    task = self.task(target, source)
                    task.update(phase='statistics' if task.get('inventory_complete') else 'inventory', retry_at=0)
                    self.store.write(self.task_path(target, source), task)
        deadline = self.now() + self.settings['max_run_seconds']
        for source in sources:
            self.run_source(source, deadline)
        return self.export()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source', choices=('both', 'sofascore', 'flashscore'), default='both')
    parser.add_argument('--target', help='Optional exact target id, for example Netherlands/N1/2016-2017')
    parser.add_argument('--max-requests', type=int)
    parser.add_argument('--retry-unavailable', action='store_true')
    parser.add_argument('--retry-blocked', action='store_true')
    parser.add_argument('--browser-executable', help='Optional local Chromium path; GitHub uses the installed Playwright browser')
    args = parser.parse_args()
    if args.max_requests is not None and args.max_requests < 1:
        parser.error('Request budget must be positive')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    options = vars(args)
    sources = ('sofascore', 'flashscore') if args.source == 'both' else (args.source,)
    report = Engine(args.root, options).run(sources)
    print(json.dumps({key: value for key, value in report.items() if key != 'seasons'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
