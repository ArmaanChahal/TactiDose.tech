"""TTS disk cache (tactidose/audio/cache.py)."""

from __future__ import annotations

import pytest

from tactidose.audio.cache import TTSCache
from tactidose.audio.playback import pcm_to_wav, read_wav

PCM = b"\x10\x00\x20\x00" * 400


@pytest.fixture
def cache(tmp_path) -> TTSCache:
    return TTSCache(tmp_path / "tts_cache")


def test_key_is_sha256_of_all_parts():
    base = TTSCache.key("elevenlabs", "voice", "model", "pcm_22050", "Cancelled.")
    assert len(base) == 64 and all(c in "0123456789abcdef" for c in base)
    assert base == TTSCache.key("elevenlabs", "voice", "model", "pcm_22050", "  Cancelled. ")
    others = {
        TTSCache.key("offline", "voice", "model", "pcm_22050", "Cancelled."),
        TTSCache.key("elevenlabs", "voice2", "model", "pcm_22050", "Cancelled."),
        TTSCache.key("elevenlabs", "voice", "model2", "pcm_22050", "Cancelled."),
        TTSCache.key("elevenlabs", "voice", "model", "pcm_16000", "Cancelled."),
        TTSCache.key("elevenlabs", "voice", "model", "pcm_22050", "Cancelled"),
    }
    assert base not in others and len(others) == 5


def test_put_pcm_get_wav_round_trip(cache: TTSCache):
    key = TTSCache.key("elevenlabs", "v", "m", "pcm_22050", "Hello.")
    assert not cache.contains(key) and cache.get(key) is None and cache.count() == 0
    path = cache.put(key, PCM, sample_rate=22050)
    assert path is not None and path.name == f"{key}.wav" and path.parent == cache.directory
    wav = cache.get(key)
    assert wav is not None and wav[:4] == b"RIFF"
    info = read_wav(wav)
    assert (info.sample_rate, info.channels, info.sample_width, info.frames) == (22050, 1, 2, PCM)
    assert cache.contains(key) and cache.count() == 1
    assert not list(cache.directory.glob("*.tmp"))  # atomic write leaves no temp files


def test_put_wav_bytes_are_stored_verbatim(cache: TTSCache):
    wav = pcm_to_wav(PCM, 16000)
    key = TTSCache.key("offline", "windows-sapi", "default", "wav", "Hi.")
    assert cache.put(key, wav) is not None
    assert cache.get(key) == wav


def test_put_pcm_without_rate_is_refused(cache: TTSCache):
    key = TTSCache.key("elevenlabs", "v", "m", "pcm_22050", "x")
    assert cache.put(key, PCM) is None
    assert cache.count() == 0


def test_corrupt_entry_is_deleted_and_reported_as_miss(cache: TTSCache):
    key = TTSCache.key("elevenlabs", "v", "m", "pcm_22050", "broken")
    cache.directory.mkdir(parents=True)
    cache.path_for(key).write_bytes(b"RIFF\x00\x00\x00\x00WAVEgarbage")
    assert cache.get(key) is None
    assert not cache.path_for(key).exists()


def test_invalid_keys_never_touch_the_filesystem(cache: TTSCache, tmp_path):
    for bad in ("", "../escape", "ABC", "a/b"):
        assert cache.contains(bad) is False
        assert cache.get(bad) is None
        assert cache.put(bad, PCM, sample_rate=16000) is None
    assert not (tmp_path / "escape.wav").exists()


def test_count_ignores_other_files(cache: TTSCache):
    cache.directory.mkdir(parents=True)
    (cache.directory / "notes.txt").write_text("x")
    for i in range(3):
        cache.put(TTSCache.key("p", "v", "m", "f", f"text {i}"), PCM, sample_rate=16000)
    assert cache.count() == 3


def test_count_of_missing_directory_is_zero(tmp_path):
    assert TTSCache(tmp_path / "does-not-exist").count() == 0
