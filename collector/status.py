"""Dependency-free completion gate for scheduled jobs."""
import hashlib
import json
from pathlib import Path
import os


def needs_work(root):
    root = Path(root)
    marker = root / 'COMPLETE.json'
    if not marker.exists():
        return True
    value = [json.loads((root / name).read_text()) for name in ('targets.json', 'settings.json')]
    signature = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return json.loads(marker.read_text()).get('manifest_signature') != signature


if __name__ == '__main__':
    active = needs_work(Path(__file__).resolve().parents[1])
    active = active or os.environ.get('RETRY_UNAVAILABLE') == 'true'
    output = f'needs_work={str(active).lower()}\n'
    print(output, end='')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as file:
            file.write(output)
