"""Exercise the developer capture command, including its isolation and repeatability."""
import json
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtGui import QImage


def test_gallery_renders_real_scenarios_without_touching_existing_app_home(tmp_path):
    home = tmp_path / 'existing-home'
    home.mkdir()
    sentinel = home / 'settings.json'
    sentinel.write_text('{"keep": true}')
    output = tmp_path / 'gallery'
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / 'tools/ui_gallery.py'), '--output', str(output),
               '--size', '1100x760']
    scenarios = ['home-empty', 'playlist', 'review', 'transfers', 'check-files']
    for scenario in scenarios:
        command.extend(['--scenario', scenario])
    for _ in range(2):
        result = subprocess.run(command, cwd=root, env=dict(os.environ, S2S_HOME=str(home)),
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        manifest = json.loads((output / 'manifest.json').read_text())
        assert [row['scenario'] for row in manifest] == scenarios
        assert len(list(output.glob('*.png'))) == len(scenarios)
        for row in manifest:
            image = QImage(str(output / row['file']))
            assert not image.isNull()
            assert (image.width(), image.height()) == (row['width'], row['height'])
            assert image.width() >= 1100 and image.height() >= 760
            assert row['file'] in (output / 'index.html').read_text()
        assert sentinel.read_text() == '{"keep": true}'
        assert list(home.iterdir()) == [sentinel]
