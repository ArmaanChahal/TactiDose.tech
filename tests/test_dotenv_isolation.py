"""The test suite never reads a developer's .env (it may hold real keys and the live data dir)."""

from __future__ import annotations

from tactidose.config import Settings


def test_settings_ignore_a_dotenv_in_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "TACTIDOSE_NUM_SLOTS=5\nGEMINI_API_KEY=not-a-real-key\nTACTIDOSE_DATA_DIR=C:/should/not/load\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.num_slots == 3 and not s.gemini_configured and "should" not in str(s.data_dir)


def test_an_explicit_env_file_still_loads(tmp_path):
    env = tmp_path / "custom.env"
    env.write_text("TACTIDOSE_NUM_SLOTS=5\n", encoding="utf-8")
    assert Settings(_env_file=env).num_slots == 5
