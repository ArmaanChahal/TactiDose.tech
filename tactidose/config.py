"""Application configuration.

All settings come from environment variables (or a ``.env`` file in the working
directory). App settings use the ``TACTIDOSE_`` prefix; third-party credentials
use their conventional names (``GEMINI_API_KEY``, ``ELEVENLABS_API_KEY``,
``TIDB_*``, ``SNOWFLAKE_*``). See ``.env.example`` for a documented template.

Every cloud integration is optional: with no keys at all the system still runs
end-to-end offline (SQLite + simulator + offline TTS + manual onboarding).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tactidose.hardware import buzzer_config
from tactidose.hardware.protocol import DEFAULT_TIMEOUTS_S, MAX_SLOTS, MIN_SLOTS, CommandName


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TACTIDOSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ------------------------------------------------------------------ server
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    #: Enables demo-only endpoints: clock travel, simulator faults, raw commands, data reset.
    demo_mode: bool = True
    #: If set, caregiver/demo mutating endpoints require header ``X-Caregiver-Pin``.
    caregiver_pin: SecretStr | None = None

    # ------------------------------------------------------------------ device
    device_id: str = "tactidose-001"
    device_name: str = "TactiDose demo unit"
    #: Pill containers on the device (v2 hardware has 3).
    num_slots: int = Field(3, ge=MIN_SLOTS, le=MAX_SLOTS)
    #: IANA zone, e.g. "America/Vancouver". Empty = system local zone.
    timezone: str | None = None

    # ------------------------------------------------------------------ dose policy (deterministic)
    #: A dose may be dispensed from ``scheduled_at - early`` …
    dose_early_minutes: int = Field(30, ge=0, le=720)
    #: … until ``scheduled_at + late``; after that an undispensed dose becomes MISSED.
    dose_late_minutes: int = Field(120, ge=1, le=1440)
    #: "Taken" is accepted for a DISPENSED dose up to this long after dispensing.
    confirm_window_minutes: int = Field(180, ge=1, le=1440)
    #: Host closes the gate if the user has not confirmed within this time.
    gate_open_timeout_s: float = Field(60.0, ge=5, le=600)
    #: After this many hardware failures on one dose it is locked for caregiver review.
    max_dispense_attempts: int = Field(3, ge=1, le=10)
    #: Never open the same medication's compartment twice within this many minutes, even if
    #: two scheduled windows overlap (prevents an accidental double dose). 0 disables.
    min_dose_interval_minutes: int = Field(60, ge=0, le=1440)
    #: If True, "What do I take now?" dispenses immediately (handoff §4.1 single-step flow).
    #: Default False: the user must say "Dispense" / press the button (explicit consent).
    check_due_auto_dispense: bool = False
    #: How far ahead dose events are materialised from schedules.
    schedule_horizon_hours: int = Field(36, ge=1, le=168)
    scheduler_tick_s: float = Field(20.0, ge=1, le=600)

    # ------------------------------------------------------------------ v2 drops & inventory
    #: Default global cooldown for new devices: after ANY drop, manual/agent drops of ANY pill
    #: are refused for this many minutes. Doctor/family change it per device in the care portal.
    manual_cooldown_minutes: int = Field(60, ge=0, le=1440)
    #: Scheduled doses drop automatically at their time even if the patient forgets.
    auto_drop_enabled: bool = True
    #: After a failed scheduled drop, retry this often until the dose window closes.
    auto_drop_retry_minutes: int = Field(5, ge=1, le=120)
    default_container_capacity: int = Field(30, ge=1, le=500)
    default_low_stock_threshold: int = Field(3, ge=0, le=100)
    #: v1 firmware (no DROP_SLOT): keep the gate open this long, then CLOSE_GATE.
    drop_close_delay_ms: int = Field(1500, ge=200, le=10000)
    timeout_drop_s: float = Field(30.0, gt=0, le=120)
    #: Also notify linked doctor/family accounts about every drop (not just problems).
    notify_caregivers_on_drop: bool = True

    # ------------------------------------------------------------------ v2 accounts & portals
    session_ttl_hours: int = Field(12, ge=1, le=720)
    session_cookie_name: str = "td_session"
    #: Set True when serving over HTTPS (adds the Secure cookie flag).
    cookie_secure: bool = False
    allow_registration: bool = True
    #: Demo mode only: create demo patient / family / doctor logins on first start.
    seed_demo_accounts: bool = True
    demo_password: SecretStr = SecretStr("demo1234")

    # ------------------------------------------------------------------ v2 conversational agent
    #: "auto" = Gemini when a key is configured, else the offline rule-based agent.
    agent_provider: Literal["auto", "gemini", "rules"] = "auto"
    #: Empty = use gemini_model.
    agent_model: str | None = Field(None, validation_alias=_alias("TACTIDOSE_AGENT_MODEL", "AGENT_MODEL"))
    agent_max_steps: int = Field(4, ge=1, le=10)
    agent_timeout_s: float = Field(30.0, gt=0, le=120)
    agent_history_messages: int = Field(20, ge=2, le=100)
    #: Gemini 3.x thinking level for the agent ("" = the model's default; "low" answers faster).
    agent_thinking_level: Literal["", "low", "medium", "high"] = ""
    #: After a Gemini failure (network block, quota, bad key, timeout) the rules agent answers
    #: straight away for this many seconds before Gemini is tried again (0 = retry every turn).
    agent_retry_after_s: float = Field(60.0, ge=0, le=3600)

    # ------------------------------------------------------------------ v2 reports & email
    report_max_days: int = Field(90, ge=1, le=366)
    #: Ask Gemini for a factual summary of the period's conversations (falls back to rules).
    report_ai_summary: bool = True
    smtp_host: str | None = Field(None, validation_alias=_alias("SMTP_HOST", "TACTIDOSE_SMTP_HOST"))
    smtp_port: int = Field(587, validation_alias=_alias("SMTP_PORT", "TACTIDOSE_SMTP_PORT"))
    smtp_user: str | None = Field(None, validation_alias=_alias("SMTP_USER", "SMTP_USERNAME", "TACTIDOSE_SMTP_USER"))
    smtp_password: SecretStr | None = Field(None, validation_alias=_alias("SMTP_PASSWORD", "TACTIDOSE_SMTP_PASSWORD"))
    smtp_from: str | None = Field(None, validation_alias=_alias("SMTP_FROM", "TACTIDOSE_SMTP_FROM"))
    #: STARTTLS on smtp_port (587). Set smtp_ssl for implicit TLS (465) instead.
    smtp_starttls: bool = Field(True, validation_alias=_alias("SMTP_STARTTLS", "TACTIDOSE_SMTP_STARTTLS"))
    smtp_ssl: bool = Field(False, validation_alias=_alias("SMTP_SSL", "TACTIDOSE_SMTP_SSL"))
    smtp_timeout_s: float = Field(20.0, gt=0, le=120)

    # ------------------------------------------------------------------ well-being check-in (optional)
    #: The non-clinical check-in from ``tactidose-wellbeing/`` (needs that package installed).
    #: It never affects drops, schedules or the agent's decisions (tactidose/wellbeing.py).
    #: Saved check-ins live in the main database next to pill_drops (patient + care team see them).
    wellbeing_enabled: bool = True
    #: After every pill that drops, offer the patient a check-in (portal / kiosk / agent reply).
    wellbeing_after_drop: bool = True
    #: At most one after-drop offer per patient in this many minutes (0 = after every drop).
    wellbeing_after_drop_gap_minutes: int = Field(120, ge=0, le=1440)
    #: Urgent-support wording and verified crisis resources (tactidose-wellbeing/config/*.json).
    wellbeing_config_file: Path | None = None
    #: Inactivity timeout of an open check-in; unsaved answers are discarded after it.
    wellbeing_session_ttl_s: int = Field(1800, ge=60, le=86400)
    #: Render check-in replies with the server TTS (ElevenLabs when configured). Off by default:
    #: replies can read the patient's own note back, so the browser speaks them locally.
    wellbeing_server_tts: bool = False

    # ------------------------------------------------------------------ guided demo (demo mode only)
    #: Pause between the MORNING / NOON / NIGHT slots of the guided judge demo (tactidose/guided/).
    demo_pause_seconds: float = Field(7.0, ge=0, le=120)
    #: How long the guided demo waits for a spoken/typed answer before treating it as unclear.
    demo_answer_timeout_s: float = Field(45.0, ge=1, le=600)
    #: How long the (simulated) buzzer sounds after a pill drops, to guide the patient to the table.
    demo_buzzer_seconds: float = Field(5.0, ge=0, le=60)
    #: Extract mood / symptoms / concerns from the free-text check-in with Gemini (rules otherwise).
    demo_checkin_ai: bool = True
    #: Which buzzer guides the patient to the pill: "laptop" (beeping tone from the kiosk / demo
    #: screen - today's behaviour), "serial" (the device's buzzer via BUZZER ON/OFF, falls back to
    #: the laptop tone), "both", or "none". Values per side: tactidose/hardware/buzzer_config.py and
    #: the BUZZER block in firmware config.h. Checklist: docs/BUZZER.md.
    buzzer_backend: Literal["laptop", "serial", "both", "none"] = "laptop"

    # ------------------------------------------------------------------ hardware
    #: "wifi" = the ESP32 over HTTP (lid / dispense endpoints in tactidose/hardware/wifi_config.py).
    hardware_mode: Literal["sim", "serial", "wifi", "none"] = "sim"
    #: "auto" (USB VID/PID scan), "COM5", "/dev/ttyUSB0", "socket://127.0.0.1:7777", "loop://"
    serial_port: str = "auto"
    serial_baud: int = 115200
    #: hardware_mode "wifi": the ESP32's address. Empty = wifi_config.ESP32_BASE_URL (http://172.20.10.9).
    esp32_url: str | None = None
    #: Home automatically after connecting if the device reports it is not homed.
    hw_auto_home: bool = True
    hw_heartbeat_s: float = Field(5.0, ge=0.5, le=120)
    hw_reconnect_max_s: float = Field(10.0, ge=0.5, le=120)
    #: Seconds to wait after opening the port for a possible auto-reset boot banner.
    hw_boot_wait_s: float = Field(2.5, ge=0, le=30)
    timeout_ping_s: float = DEFAULT_TIMEOUTS_S[CommandName.PING]
    timeout_status_s: float = DEFAULT_TIMEOUTS_S[CommandName.STATUS]
    timeout_home_s: float = DEFAULT_TIMEOUTS_S[CommandName.HOME]
    timeout_move_s: float = DEFAULT_TIMEOUTS_S[CommandName.MOVE_SLOT]
    timeout_dispense_s: float = DEFAULT_TIMEOUTS_S[CommandName.DISPENSE_SLOT]
    timeout_gate_s: float = DEFAULT_TIMEOUTS_S[CommandName.OPEN_GATE]
    timeout_stop_s: float = DEFAULT_TIMEOUTS_S[CommandName.STOP]
    #: Simulator speed multiplier (1.0 = realistic timing, 10 = ten times faster).
    sim_speed: float = Field(1.0, gt=0, le=1000)

    # ------------------------------------------------------------------ database
    #: Full SQLAlchemy URL. Overrides the TIDB_* settings. Default: SQLite in data_dir.
    database_url: str | None = Field(None, validation_alias=_alias("TACTIDOSE_DATABASE_URL", "DATABASE_URL"))
    tidb_host: str | None = Field(None, validation_alias=_alias("TIDB_HOST", "TACTIDOSE_TIDB_HOST"))
    tidb_port: int = Field(4000, validation_alias=_alias("TIDB_PORT", "TACTIDOSE_TIDB_PORT"))
    tidb_user: str | None = Field(None, validation_alias=_alias("TIDB_USER", "TACTIDOSE_TIDB_USER"))
    tidb_password: SecretStr | None = Field(None, validation_alias=_alias("TIDB_PASSWORD", "TACTIDOSE_TIDB_PASSWORD"))
    tidb_database: str = Field("tactidose", validation_alias=_alias("TIDB_DATABASE", "TIDB_DB_NAME", "TACTIDOSE_TIDB_DATABASE"))
    #: CA bundle path for TLS. Empty = the file named by SSL_CERT_FILE / REQUESTS_CA_BUNDLE if it
    #: exists, else the certifi bundle (db/session.tidb_ssl_ca).
    tidb_ssl_ca: str | None = Field(None, validation_alias=_alias("TIDB_SSL_CA", "TIDB_CA_PATH", "CA_PATH"))
    tidb_ssl: bool = Field(True, validation_alias=_alias("TIDB_SSL", "TACTIDOSE_TIDB_SSL"))

    # ------------------------------------------------------------------ voice input (offline)
    voice_enabled: bool = True
    vosk_model_path: Path = Path("models/vosk-model-small-en-us-0.15")
    #: Restrict Vosk to the command vocabulary (much more robust for a small command set).
    voice_use_grammar: bool = True
    voice_min_confidence: float = Field(0.55, ge=0, le=1)
    #: sounddevice input device index or name substring. Empty = system default.
    mic_device: str | None = None
    voice_sample_rate: int = 16000

    # ------------------------------------------------------------------ speech output
    tts_provider: Literal["elevenlabs", "offline", "none"] = "elevenlabs"
    elevenlabs_api_key: SecretStr | None = Field(None, validation_alias=_alias("ELEVENLABS_API_KEY", "TACTIDOSE_ELEVENLABS_API_KEY"))
    elevenlabs_voice_id: str = Field("JBFqnCBsd6RMkjVDRZzb", validation_alias=_alias("ELEVENLABS_VOICE_ID", "TACTIDOSE_ELEVENLABS_VOICE_ID"))
    elevenlabs_model_id: str = Field("eleven_flash_v2_5", validation_alias=_alias("ELEVENLABS_MODEL_ID", "TACTIDOSE_ELEVENLABS_MODEL_ID"))
    #: If the configured voice is not available to the account (the default "George" is a legacy
    #: voice new accounts do not have), use the account's first premade voice instead.
    elevenlabs_auto_voice: bool = True
    #: Raw PCM so playback needs no MP3 decoder. pcm_16000/22050/24000 work on all tiers.
    elevenlabs_output_format: str = "pcm_22050"
    tts_timeout_s: float = Field(6.0, gt=0, le=60)
    #: Speak medication names (they are sent to ElevenLabs when online).
    tts_include_med_names: bool = True
    #: sounddevice output device index or name substring. Empty = system default.
    audio_output_device: str | None = None

    # ------------------------------------------------------------------ label onboarding (Gemini)
    gemini_api_key: SecretStr | None = Field(None, validation_alias=_alias("GEMINI_API_KEY", "GOOGLE_API_KEY", "TACTIDOSE_GEMINI_API_KEY"))
    gemini_model: str = Field("gemini-3.8-flash", validation_alias=_alias("GEMINI_MODEL", "TACTIDOSE_GEMINI_MODEL"))
    #: Tried once if the primary model id is rejected (404 / not found).
    gemini_fallback_model: str = Field("gemini-flash-latest", validation_alias=_alias("GEMINI_FALLBACK_MODEL", "TACTIDOSE_GEMINI_FALLBACK_MODEL"))
    gemini_timeout_s: float = Field(45.0, gt=0, le=300)
    #: "auto" = gemini if a key is set, else disabled. "fake" = deterministic demo extraction.
    label_extractor: Literal["auto", "gemini", "fake", "disabled"] = "auto"
    max_label_image_bytes: int = 8 * 1024 * 1024

    # ------------------------------------------------------------------ analytics (Snowflake)
    snowflake_account: str | None = Field(None, validation_alias=_alias("SNOWFLAKE_ACCOUNT"))
    snowflake_user: str | None = Field(None, validation_alias=_alias("SNOWFLAKE_USER"))
    snowflake_password: SecretStr | None = Field(None, validation_alias=_alias("SNOWFLAKE_PASSWORD"))
    #: Programmatic access token (preferred over passwords; MFA-safe).
    snowflake_token: SecretStr | None = Field(None, validation_alias=_alias("SNOWFLAKE_TOKEN", "SNOWFLAKE_PAT"))
    snowflake_private_key_file: str | None = Field(None, validation_alias=_alias("SNOWFLAKE_PRIVATE_KEY_FILE"))
    snowflake_private_key_file_pwd: SecretStr | None = Field(None, validation_alias=_alias("SNOWFLAKE_PRIVATE_KEY_FILE_PWD"))
    #: Empty = inferred (token -> PROGRAMMATIC_ACCESS_TOKEN, key file -> SNOWFLAKE_JWT, else password).
    snowflake_authenticator: str | None = Field(None, validation_alias=_alias("SNOWFLAKE_AUTHENTICATOR"))
    snowflake_warehouse: str | None = Field(None, validation_alias=_alias("SNOWFLAKE_WAREHOUSE"))
    snowflake_database: str = Field("TACTIDOSE", validation_alias=_alias("SNOWFLAKE_DATABASE"))
    snowflake_schema: str = Field("ANALYTICS", validation_alias=_alias("SNOWFLAKE_SCHEMA"))
    snowflake_role: str | None = Field(None, validation_alias=_alias("SNOWFLAKE_ROLE"))
    analytics_sync_interval_s: float = Field(30.0, ge=2, le=3600)
    #: Secret used to pseudonymise user ids before they leave the device.
    analytics_salt: SecretStr = SecretStr("tactidose-demo-salt-change-me")

    # ------------------------------------------------------------------ validators
    @field_validator("timezone", "mic_device", "audio_output_device", "wellbeing_config_file", "esp32_url",
                     mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("serial_port", mode="before")
    @classmethod
    def _default_port(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "auto"
        return v.strip() if isinstance(v, str) else v

    # ------------------------------------------------------------------ derived
    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "tactidose.db"

    @property
    def tts_cache_dir(self) -> Path:
        return self.data_dir / "tts_cache"

    @property
    def scans_dir(self) -> Path:
        return self.data_dir / "label_scans"

    @property
    def outbox_dir(self) -> Path:
        """Where report emails are saved as .eml when SMTP is not configured."""
        return self.data_dir / "outbox"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and (self.smtp_from or self.smtp_user))

    @property
    def effective_agent_provider(self) -> str:
        if self.agent_provider == "auto":
            return "gemini" if self.gemini_configured else "rules"
        return self.agent_provider

    @property
    def effective_agent_model(self) -> str:
        return self.agent_model or self.gemini_model

    @property
    def snowflake_configured(self) -> bool:
        return bool(
            self.snowflake_account
            and self.snowflake_user
            and (self.snowflake_password or self.snowflake_token or self.snowflake_private_key_file)
        )

    @property
    def gemini_configured(self) -> bool:
        return self.gemini_api_key is not None and bool(self.gemini_api_key.get_secret_value())

    @property
    def elevenlabs_configured(self) -> bool:
        return self.elevenlabs_api_key is not None and bool(self.elevenlabs_api_key.get_secret_value())

    @property
    def effective_label_extractor(self) -> str:
        if self.label_extractor == "auto":
            return "gemini" if self.gemini_configured else "disabled"
        return self.label_extractor

    def command_timeout_s(self, name: CommandName) -> float:
        return {
            CommandName.PING: self.timeout_ping_s,
            CommandName.STATUS: self.timeout_status_s,
            CommandName.HOME: self.timeout_home_s,
            CommandName.MOVE_SLOT: self.timeout_move_s,
            CommandName.DISPENSE_SLOT: self.timeout_dispense_s,
            CommandName.OPEN_GATE: self.timeout_gate_s,
            CommandName.CLOSE_GATE: self.timeout_gate_s,
            CommandName.STOP: self.timeout_stop_s,
            CommandName.DROP_SLOT: self.timeout_drop_s,
            CommandName.BUZZER: buzzer_config.COMMAND_TIMEOUT_S,
        }[name]

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.tts_cache_dir, self.scans_dir, self.outbox_dir):
            p.mkdir(parents=True, exist_ok=True)

    def public_summary(self) -> dict[str, object]:
        """Non-secret view for /api/health and the doctor command."""
        return {
            "device_id": self.device_id,
            "num_slots": self.num_slots,
            "hardware_mode": self.hardware_mode,
            "serial_port": self.serial_port,
            "esp32_url": self.esp32_url if self.hardware_mode == "wifi" else None,
            "demo_mode": self.demo_mode,
            "timezone": self.timezone or "system",
            "database": "tidb" if (self.tidb_host and not self.database_url) else (
                "custom" if self.database_url else "sqlite"),
            "voice_enabled": self.voice_enabled,
            "tts_provider": self.tts_provider,
            "elevenlabs_configured": self.elevenlabs_configured,
            "label_extractor": self.effective_label_extractor,
            "gemini_model": self.gemini_model,
            "snowflake_configured": self.snowflake_configured,
            "check_due_auto_dispense": self.check_due_auto_dispense,
            "caregiver_pin_required": self.caregiver_pin is not None,
            "agent_provider": self.effective_agent_provider,
            "agent_model": self.effective_agent_model if self.effective_agent_provider == "gemini" else None,
            "smtp_configured": self.smtp_configured,
            "manual_cooldown_minutes_default": self.manual_cooldown_minutes,
            "auto_drop_enabled": self.auto_drop_enabled,
            "allow_registration": self.allow_registration,
            "wellbeing_enabled": self.wellbeing_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (tests construct ``Settings(...)`` directly)."""
    return Settings()
