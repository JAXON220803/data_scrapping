"""Public-source adapters. No authentication, stealth mode, or block bypass."""

import logging
import urllib.parse

from .common import Blocked, Missing, normalized
from .parsers import environment, flash_fixtures, flash_statistics, season_year, sofa_fixture, sofa_statistics

LOG = logging.getLogger('collector')

LEAGUE_NAMES = {
    'E1': ['championship'], 'EC': ['national league'], 'F1': ['ligue 1'], 'F2': ['ligue 2'],
    'D2': ['2 bundesliga'], 'G1': ['super league'], 'I1': ['serie a'], 'I2': ['serie b'],
    'N1': ['eredivisie'], 'P1': ['liga portugal', 'primeira liga'], 'B1': ['pro league'],
    'SP2': ['laliga 2', 'laliga2'], 'SC1': ['championship'], 'SC2': ['league one'],
    'SC3': ['league two'], 'T1': ['super lig'],
}


def validate_tournament(target, metadata):
    league = metadata.get('uniqueTournament', metadata)
    country = league.get('category', {}).get('name', '')
    expected = target['country']
    equivalents = {'Turkey': ['turkey', 'turkiye'], 'Netherlands': ['netherlands']}
    if normalized(country, {}) not in [normalized(value, {}) for value in equivalents.get(expected, [expected])]:
        raise ValueError(f'Tournament country mismatch: expected {expected}, received {country}')
    name = normalized(league.get('name', ''), {})
    if not any(normalized(label, {}) in name for label in LEAGUE_NAMES[target['division']]):
        raise ValueError(f'Tournament league mismatch: received {league.get("name")}')


class Sofascore:
    def __init__(self, http, store):
        self.http, self.store = http, store

    def inventory_step(self, target, task, fixtures):
        tournament = target['sources']['sofascore']['tournament_id']
        catalogue_path = f'checkpoints/catalogues/sofascore-{tournament}.json'
        catalogue = self.store.read(catalogue_path, {})
        if 'metadata' not in catalogue:
            catalogue['metadata'] = self.http.json(f'https://www.sofascore.com/api/v1/unique-tournament/{tournament}')
            self.store.write(catalogue_path, catalogue)
        validate_tournament(target, catalogue['metadata'])
        if 'seasons' not in catalogue:
            catalogue['seasons'] = self.http.json(f'https://www.sofascore.com/api/v1/unique-tournament/{tournament}/seasons')['seasons']
            self.store.write(catalogue_path, catalogue)
        if 'season_id' not in task:
            matches = [season for season in catalogue['seasons'] if season_year(season) == target['start_year']]
            if not matches:
                task.update(phase='done', reason='season_not_listed')
                return
            if len(matches) != 1:
                raise ValueError('Ambiguous season identifier')
            task['season_id'], task['next_page'] = matches[0]['id'], 0
        page = task.get('next_page', 0)
        raw = self.http.json(f'https://www.sofascore.com/api/v1/unique-tournament/{tournament}/season/{task["season_id"]}/events/last/{page}')
        if not isinstance(raw.get('events'), list) or type(raw.get('hasNextPage')) is not bool:
            raise ValueError('Event pagination schema changed; inventory remains unfinished')
        for event in raw['events']:
            key = str(event['id'])
            fixtures[key] = {**fixtures.get(key, {}), **sofa_fixture(event)}
        task['next_page'] = page + 1
        if not raw['hasNextPage']:
            task.update(phase='statistics', inventory_complete=True)

    def statistics(self, fixture):
        if fixture['status'] not in ('finished', 'special_result'):
            return {'shots': {}, 'statistics': [], 'statistics_status': 'not_completed'}
        url = f'https://www.sofascore.com/api/v1/event/{fixture["id"]}/statistics'
        try:
            raw = self.http.json(url)
        except Missing:
            return {'shots': {}, 'statistics': [], 'statistics_status': 'unavailable', 'statistics_url': url}
        result = sofa_statistics(raw)
        result.update(statistics_status='available' if result['statistics'] else 'unavailable', statistics_url=url)
        return result

    def close(self):
        pass


class Flashscore:
    def __init__(self, http, store, options):
        self.http, self.store, self.options = http, store, options
        self.browser = self.context = self.runtime = None
        self.browser_error = None
        self.empty_archive_response = False

    def _ensure_browser(self):
        if self.browser:
            return
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        launch = {'headless': True}
        if self.options.get('browser_executable'):
            launch['executable_path'] = self.options['browser_executable']
        self.browser = self.runtime.chromium.launch(**launch)
        self.context = self.browser.new_context(locale='en-GB', timezone_id='UTC')

        def route_handler(route):
            request = route.request
            parsed = urllib.parse.urlsplit(request.url)
            host, kind = parsed.hostname or '', request.resource_type
            if self.browser_error:
                route.abort()
                return
            # Images and advertising are irrelevant to factual results/statistics.
            if kind in ('image', 'media', 'font'):
                route.abort()
                return
            if kind in ('document', 'xhr', 'fetch'):
                if host not in self.http.HOSTS:
                    route.abort()
                    return
                # Observed live-score/odds feeds are unnecessary for archive statistics.
                feed = parsed.path.rsplit('/', 1)[-1]
                if host == 'global.flashscore.ninja' and (feed.startswith(('cmc_', 'mc_', 'dc_', 'df_dos_')) or feed == 'f_'):
                    route.abort()
                    return
                try:
                    self.http.check_robots(request.url)
                    self.http.budget.reserve()
                except Exception as error:
                    self.browser_error = error
                    route.abort()
                    return
            route.continue_()

        def response_handler(response):
            if urllib.parse.urlsplit(response.url).hostname in self.http.HOSTS and response.status in (401, 403, 429):
                self.browser_error = Blocked(f'Browser received HTTP {response.status}; source stopped')
            if '/feed/tr_' in response.url and response.status == 200:
                try:
                    self.empty_archive_response = response.text().strip() in ('', '0')
                except Exception:
                    pass  # The normal DOM timeout remains the fallback.

        self.context.route('**/*', route_handler)
        self.context.on('response', response_handler)

    def _check(self):
        if self.browser_error:
            raise self.browser_error

    def _page(self, url):
        self._ensure_browser()
        page = self.context.new_page()
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=120000)
            self._check()
            if response is not None and response.status == 404:
                raise Missing('Flashscore page returned HTTP 404')
            if response is None or response.status != 200:
                raise Blocked('Archive did not return HTTP 200')
            page.wait_for_timeout(2000)
            self._check()
            title = page.title().casefold()
            if any(text in title for text in ('just a moment', 'access denied', 'verify you are human')):
                raise Blocked('Browser challenge encountered; no bypass attempted')
            reject = page.locator('#onetrust-reject-all-handler')
            if reject.count() and reject.is_visible():
                reject.click(timeout=3000)
            return page
        except Exception:
            page.close()
            self._check()
            raise

    def inventory_step(self, target, task, fixtures):
        url = target['sources']['flashscore']['url']
        self.empty_archive_response = False
        page = self._page(url)
        try:
            page.wait_for_selector('[id^="g_1_"]', timeout=25000)
            if target['season'].replace('-', '/') not in page.inner_text('body'):
                raise ValueError('Archive does not show the requested season')
            for _ in range(self.options.get('max_pages_per_season', 60)):
                for fixture in flash_fixtures(page.content()):
                    fixtures[fixture['id']] = {**fixtures.get(fixture['id'], {}), **fixture}
                # Save partial inventory before another potentially interrupted request.
                self.store.write(f'data/{target["id"]}/flashscore/fixtures.json', fixtures)
                LOG.info('flashscore %s: archive inventory saved (%s fixtures)', target['id'], len(fixtures))
                more = page.get_by_role('button', name='Show more matches', exact=True)
                if not more.count() or not more.is_visible():
                    task.update(phase='statistics', inventory_complete=True)
                    return
                previous = page.locator('[id^="g_1_"]').count()
                more.click(timeout=30000)
                for attempt in range(120):
                    self._check()
                    if self.empty_archive_response:
                        # Fetch statistics for the discovered matches now, then retry
                        # the incomplete archive on another day. Keep it unfinished.
                        task.update(phase='statistics', inventory_complete=False,
                                    inventory_retry_at=self.http.budget.now() + 86400,
                                    last_error='Flashscore Show more returned HTTP 200 with empty archive payload (0)')
                        return
                    if page.locator('[id^="g_1_"]').count() > previous:
                        break
                    page.wait_for_timeout(1000)
                else:
                    raise ValueError('Archive pagination did not append matches')
                self._check()
            raise ValueError('Archive still has more matches; maximum page limit reached')
        finally:
            page.close()

    def statistics(self, fixture):
        landing = self.http.text(fixture['url'])
        meta = environment(landing)
        if meta.get('event_info', {}).get('hasStats') is False:
            return {'shots': {}, 'statistics': [], 'statistics_status': 'unavailable'}
        parsed = urllib.parse.urlsplit(fixture['url'])
        path = parsed.path.rstrip('/') + '/summary/stats/'
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ''))
        page = self._page(url)
        try:
            try:
                page.wait_for_selector('[data-testid="wcl-statistics"]', timeout=25000)
            except Exception:
                self._check()
                if environment(page.content()).get('event_info', {}).get('hasStats') is False:
                    return {'shots': {}, 'statistics': [], 'statistics_status': 'unavailable'}
                # Rendering failures are retryable; they must not become "no statistics".
                raise ValueError('Statistics were not rendered; source response needs another attempt')
            self._check()
            result = flash_statistics(page.content())
            if not result['statistics']:
                raise ValueError('Statistics selector schema changed')
            result.update(statistics_status='available', statistics_url=url)
            return result
        finally:
            page.close()

    def close(self):
        if self.browser:
            self.browser.close()
        if self.runtime:
            self.runtime.stop()
