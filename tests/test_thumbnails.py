"""Exercise real decoding, cache identity and the project's asset boundary."""
import asyncio
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from substar_core.media import thumbnails
from substar_core.editor import http_api


def run_tool(name, *args):
    return subprocess.run([thumbnails._find_tool(name), *map(str, args)], check=True,
                          capture_output=True, timeout=30)


def wait_result(project, source):
    for _ in range(100):
        result = thumbnails.status(project, source)
        if result['state'] in {'ready', 'error', 'unavailable'}:
            return result
        time.sleep(.1)
    raise AssertionError('thumbnail generation did not finish')


def test_real_video_partial_sprite_cache_and_source_change(tmp_path):
    source = tmp_path / 'video.mp4'
    run_tool('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=10',
             '-t', '2.2', '-c:v', 'mpeg4', source)
    thumbnails.request_generation(tmp_path, source)
    result = wait_result(tmp_path, source)
    assert result['state'] == 'ready', result
    assert result['interval'] == .5 and result['count'] == 5
    sprite = thumbnails.cache_directory(tmp_path, source) / 'sprite-00001.jpg'
    info = json.loads(run_tool('ffprobe', '-v', 'error', '-show_streams', '-of', 'json', sprite).stdout)
    assert (info['streams'][0]['width'], info['streams'][0]['height']) == (1280, 720)
    with patch.object(thumbnails, '_generate', side_effect=AssertionError('cache was decoded again')):
        assert thumbnails.request_generation(tmp_path, source)['state'] == 'ready'
    with source.open('ab') as output:
        output.write(b'changed')
    assert thumbnails.status(tmp_path, source)['state'] == 'idle'


def test_audio_is_unavailable_without_breaking_project(tmp_path):
    source = tmp_path / 'audio.wav'
    run_tool('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '0.2', source)
    thumbnails.request_generation(tmp_path, source)
    assert wait_result(tmp_path, source)['state'] == 'unavailable'
    assert not list(tmp_path.glob('thumbnail_cache/*/sprite-*.jpg'))


def test_assets_are_bound_to_current_media_and_complete_manifest(tmp_path):
    source = tmp_path / 'source.mp4'; source.write_bytes(b'media')
    directory = thumbnails.cache_directory(tmp_path, source); directory.mkdir(parents=True)
    (directory / 'sprite-00001.jpg').write_bytes(b'jpeg')
    thumbnails.atomic_write_json(directory / 'manifest.json', {'state': 'ready'})
    app = FastAPI(); app.include_router(http_api.router)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            base = f'/api/projects/p/thumbnails/{directory.name}'
            assert (await client.get(base + '/sprite-00001.jpg')).content == b'jpeg'
            assert (await client.get(base + '/manifest.json')).status_code == 404
            assert (await client.get('/api/projects/p/thumbnails/old/sprite-00001.jpg')).status_code == 404
            thumbnails.atomic_write_json(directory / 'manifest.json', {'state': 'generating'})
            assert (await client.get(base + '/sprite-00001.jpg')).status_code == 404

    with patch.object(http_api, '_project_media_source', return_value=(source, {})), \
         patch.object(http_api, 'project_job_path', return_value=tmp_path):
        asyncio.run(check())


def test_creation_setting_is_opt_in_and_survives_task_snapshot():
    import app
    for value, expected in [(True, True), (False, False), ("false", False)]:
        settings, _ = app._automatic_settings_from_payload(app.AutomaticTaskPayload(
            job_name='thumbnail-test', settings_overrides={'generate_thumbnails': value}))
        assert settings['generate_thumbnails'] is expected
