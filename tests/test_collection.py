import json
from pathlib import Path
import tempfile
import unittest

from collector.common import Blocked, Budget, Deferred, Store, merge_rows
from collector.parsers import flash_fixtures, flash_statistics, sofa_fixture, sofa_statistics, season_year
from collector.run import Engine
from collector.status import needs_work
from collector.common import robots_allowed


class CollectionTests(unittest.TestCase):
    def test_extra_time_match_is_retained_but_not_eligible_for_regular_time_merge(self):
        raw = {'id': 1, 'startTimestamp': 1500000000,
               'homeTeam': {'name': 'A'}, 'awayTeam': {'name': 'B'},
               'status': {'code': 110, 'type': 'finished'}}
        self.assertEqual(sofa_fixture(raw)['status'], 'special_result')

    def test_unfinished_reference_match_is_preserved(self):
        original = [{'Date': '01/01/2011', 'HomeTeam': 'A', 'AwayTeam': 'B',
                     'FTHG': '', 'FTAG': '', 'HS': '', 'AS': '', 'HST': '', 'AST': ''}]
        records = [{'id': 1, 'date': '2011-01-01', 'home': 'A', 'away': 'B',
                    'home_goals': 2, 'away_goals': 1, 'status': 'finished', 'source': 'sofascore',
                    'shots': {'HS': 10, 'AS': 5, 'HST': 4, 'AST': 1}}]
        self.assertEqual(merge_rows(original, records, {}), (original, []))

    def test_completion_gate_invalidates_after_configuration_change(self):
        import hashlib
        store = Store(self.root)
        store.write('targets.json', {'targets': []})
        store.write('settings.json', {'interval_seconds': 15})
        self.assertTrue(needs_work(self.root))
        signature = hashlib.sha256(json.dumps([{'targets': []}, {'interval_seconds': 15}], sort_keys=True).encode()).hexdigest()
        store.write('COMPLETE.json', {'manifest_signature': signature})
        self.assertFalse(needs_work(self.root))
        store.write('settings.json', {'interval_seconds': 20})
        self.assertTrue(needs_work(self.root))

    def test_robots_specific_allow_and_agent_rules(self):
        policy = 'User-agent: *\nDisallow: /api/\nAllow: /api/public/\n'
        self.assertFalse(robots_allowed(policy, 'https://www.sofascore.com/api/private'))
        self.assertTrue(robots_allowed(policy, 'https://www.sofascore.com/api/public/event'))
        self.assertTrue(robots_allowed(policy, 'https://www.sofascore.com/football'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_budget_survives_new_runner_and_midnight(self):
        clock = [1800000000.0]
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds
        for _ in range(2):
            budget = Budget(Store(self.root), 'sofascore', 15, 2, 100,
                            now=lambda: clock[0], sleep=sleep)
            budget.reserve()
        self.assertGreaterEqual(sum(waits), 15)
        with self.assertRaises(Deferred):
            Budget(Store(self.root), 'sofascore', 15, 2, 100,
                   now=lambda: clock[0], sleep=sleep).reserve()
        clock[0] += 86400
        Budget(Store(self.root), 'sofascore', 15, 2, 100,
               now=lambda: clock[0], sleep=sleep).reserve()

    def test_independent_run_limit_does_not_reset_daily_limit(self):
        store = Store(self.root)
        budget = Budget(store, 'flashscore', 0, 50, 1)
        budget.reserve()
        with self.assertRaises(Deferred):
            budget.reserve()
        self.assertEqual(store.read('checkpoints/limits.json')['flashscore']['used'], 1)

    def test_store_rejects_paths_outside_repository(self):
        store = Store(self.root)
        with self.assertRaises(ValueError):
            store.write('../secret.json', {})

    def test_full_match_only_and_missing_never_becomes_zero(self):
        raw = {'statistics': [
            {'period': '1ST', 'groups': [{'statisticsItems': [{'name': 'Total shots', 'home': '3', 'away': '2'}]}]},
            {'period': 'ALL', 'groups': [{'statisticsItems': [
                {'name': 'Total shots', 'homeValue': 12, 'awayValue': 0},
                {'name': 'Shots on target', 'homeValue': 4, 'awayValue': None},
                {'name': 'Corner kicks', 'homeValue': 5, 'awayValue': 1}]}]}]}
        result = sofa_statistics(raw)
        self.assertEqual(result['shots'], {'HS': 12, 'AS': 0, 'HST': 4})
        self.assertEqual(len(result['statistics']), 3)
        self.assertEqual(sofa_statistics({'statistics': []})['shots'], {})

    def test_historical_year_formats(self):
        self.assertEqual(season_year({'year': '10/11'}), 2010)
        self.assertEqual(season_year({'name': 'Eredivisie 2010/2011'}), 2010)

    def test_flashscore_rendered_statistics_and_other_metrics(self):
        html = '''<div data-testid="wcl-statistics"><div class="wcl-labelRow_abc">
            <div><div>22</div></div><div><span>Total shots</span></div><div><div>13</div></div>
            </div></div><div data-testid="wcl-statistics"><div class="wcl-labelRow_abc">
            <div><div>4</div></div><div><span>Shots on target</span></div><div><div>3</div></div>
            </div></div><div data-testid="wcl-statistics"><div class="wcl-labelRow_abc">
            <div>69%</div><div>Ball possession</div><div>31%</div></div></div>'''
        result = flash_statistics(html)
        self.assertEqual(result['shots'], {'HS': 22, 'AS': 13, 'HST': 4, 'AST': 3})
        self.assertEqual(len(result['statistics']), 3)

    def test_flashscore_inventory_includes_extra_playoff_fixtures(self):
        html = '''<div id="g_1_abc12345"><a class="eventRowLink" href="https://www.flashscore.com/match/football/a/b/?mid=abc12345"></a>
          <span data-testid="wcl-stageTime">15.05.2011</span>
          <div class="event__homeParticipant"><span class="wcl-name_example">Groningen</span><svg><title>Winner: Groningen</title></svg></div><div class="event__awayParticipant">Den Haag</div>
          <span class="event__score--home">5<sup>(3)</sup></span><span class="event__score--away">1<sup>(4)</sup></span></div>'''
        result = flash_fixtures(html)
        self.assertEqual(result[0]['home_goals'], 5)
        self.assertEqual(result[0]['away_goals'], 1)
        self.assertEqual(result[0]['date'], '2011-05-15')
        self.assertEqual(result[0]['home'], 'Groningen')

    def test_merge_preserves_original_values_and_does_not_infer_unmatched(self):
        original = [{'Date': '28/08/10', 'HomeTeam': 'Duisburg', 'AwayTeam': 'Ingolstadt',
                     'FTHG': '4', 'FTAG': '1', 'HS': '', 'AS': '0', 'HST': '', 'AST': ''}]
        records = [{'id': 123, 'date': '2010-08-28', 'home': 'MSV Duisburg', 'away': 'FC Ingolstadt 04',
                    'home_goals': 4, 'away_goals': 1, 'status': 'finished',
                    'shots': {'HS': 11, 'AS': 14, 'HST': 4, 'AST': 5}, 'source': 'sofascore'}]
        rows, changes = merge_rows(original, records, {'Duisburg': 'MSV Duisburg', 'Ingolstadt': 'FC Ingolstadt 04'})
        # Existing zero must remain; incompatible mixed totals are held for review.
        self.assertEqual(rows[0]['AS'], '0')
        self.assertNotIn('AST', [change['field'] for change in changes])
        self.assertEqual(original[0]['HS'], '')

    def test_ambiguous_or_awarded_result_never_silently_merges(self):
        original = [{'Date': '01/01/2011', 'HomeTeam': 'A', 'AwayTeam': 'B',
                     'FTHG': '3', 'FTAG': '0', 'HS': '', 'AS': '', 'HST': '', 'AST': ''}]
        records = [{'id': 1, 'date': '2011-01-01', 'home': 'A', 'away': 'B',
                    'home_goals': 1, 'away_goals': 1, 'status': 'awarded',
                    'shots': {'HS': 5, 'AS': 7, 'HST': 1, 'AST': 2}, 'source': 'flashscore'}]
        self.assertEqual(merge_rows(original, records, {})[1], [])

    def make_repository(self):
        store = Store(self.root)
        store.write('settings.json', {'interval_seconds': 15, 'daily_requests_per_source': 400,
                    'requests_per_source_per_run': 20, 'max_run_seconds': 1000,
                    'blocked_retry_hours': 24, 'max_pages_per_season': 60, 'aliases': {}})
        store.write('targets.json', {'version': 1, 'targets': [
            {'id': 'Test/N1/2010-2011', 'country': 'Test', 'division': 'N1', 'season': '2010-2011',
             'start_year': 2010, 'whole_season_missing': True, 'reference_matches': 2,
             'reference': 'reference/N1.csv', 'sources': {'sofascore': {'tournament_id': 37},
                                                        'flashscore': {'url': 'https://www.flashscore.com/example/'}}}]})
        store.text('reference/N1.csv', 'Date,HomeTeam,AwayTeam,FTHG,FTAG,HS,AS,HST,AST\n01/01/2011,A,B,2,1,,,,\n02/01/2011,C,D,0,0,,,,\n')
        return store

    def test_interrupted_batch_resumes_without_redownloading_finished_statistics(self):
        store = self.make_repository()
        class Fake:
            calls = []
            stop = True
            def inventory_step(self, target, task, fixtures):
                for index, home, away, hg, ag in [(1, 'A', 'B', 2, 1), (2, 'C', 'D', 0, 0)]:
                    fixtures[str(index)] = {'id': str(index), 'source': 'sofascore', 'date': f'2011-01-0{index}',
                        'home': home, 'away': away, 'home_goals': hg, 'away_goals': ag, 'status': 'finished'}
                task.update(phase='statistics', inventory_complete=True)
            def statistics(self, fixture):
                self.calls.append(fixture['id'])
                if fixture['id'] == '2' and self.stop:
                    raise Deferred('Test run request limit')
                return {'shots': {'HS': 10, 'AS': 4, 'HST': 3, 'AST': 1},
                        'statistics': [{'name': 'Total shots', 'home': 10, 'away': 4}], 'statistics_status': 'available'}
            def close(self):
                pass
        adapter = Fake()
        first = Engine(self.root, adapters={'sofascore': adapter}).run(('sofascore',))
        self.assertEqual(first['recovered_cells'], 4)
        self.assertEqual(first['last_run']['sofascore']['status'], 'paused_by_budget')
        adapter.stop = False
        second = Engine(self.root, adapters={'sofascore': adapter}).run(('sofascore',))
        self.assertEqual(second['recovered_cells'], 8)
        self.assertEqual(adapter.calls, ['1', '2', '2'])

    def test_source_block_does_not_become_a_finished_task(self):
        self.make_repository()
        class Fake:
            def inventory_step(self, *args):
                raise Blocked('HTTP 403')
            def close(self):
                pass
        report = Engine(self.root, adapters={'sofascore': Fake()}).run(('sofascore',))
        self.assertEqual(report['status'], 'in_progress')
        self.assertEqual(report['source_tasks_done'], 0)
        self.assertEqual(report['last_run']['sofascore']['status'], 'Blocked')

    def test_one_unrendered_match_does_not_prevent_checking_other_matches(self):
        self.make_repository()
        class Fake:
            def inventory_step(self, target, task, fixtures):
                for number in (1, 2):
                    fixtures[str(number)] = {'id': str(number), 'date': f'2011-01-0{number}',
                        'home': 'A' if number == 1 else 'C', 'away': 'B' if number == 1 else 'D',
                        'home_goals': 2 if number == 1 else 0, 'away_goals': 1 if number == 1 else 0,
                        'source': 'sofascore', 'status': 'finished'}
                task.update(phase='statistics', inventory_complete=True)
            def statistics(self, fixture):
                if fixture['id'] == '1':
                    raise ValueError('Statistics not rendered')
                return {'statistics_status': 'available', 'shots': {'HS': 10, 'AS': 5, 'HST': 4, 'AST': 1}}
            def close(self):
                pass
        report = Engine(self.root, adapters={'sofascore': Fake()}).run(('sofascore',))
        self.assertEqual(report['recovered_cells'], 4)
        self.assertEqual(report['source_tasks_done'], 0)
        self.assertEqual(report['seasons'][0]['sources']['sofascore']['statistics_checked'], 1)

    def test_partial_archive_is_not_marked_complete_and_does_not_busy_retry(self):
        store = self.make_repository()
        class Fake:
            calls = 0
            def inventory_step(self, target, task, fixtures):
                self.calls += 1
                fixtures['1'] = {'id': '1', 'date': '2011-01-01', 'home': 'A', 'away': 'B',
                    'home_goals': 2, 'away_goals': 1, 'source': 'flashscore', 'status': 'finished',
                    'statistics_status': 'unavailable'}
                task.update(phase='statistics', inventory_complete=False, inventory_retry_at=1800086400)
            def close(self):
                pass
        adapter = Fake()
        for _ in range(2):
            report = Engine(self.root, now=lambda: 1800000000, adapters={'flashscore': adapter}).run(('flashscore',))
            self.assertEqual(report['source_tasks_done'], 0)
            self.assertEqual(report['seasons'][0]['sources']['flashscore']['phase'], 'inventory')
        self.assertEqual(adapter.calls, 1)
        self.assertFalse(store.path('COMPLETE.json').exists())


if __name__ == '__main__':
    unittest.main()
