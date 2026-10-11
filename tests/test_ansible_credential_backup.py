from tests.test_ansible_config_template import load_config_from, render


def test_credential_config_defaults_and_overrides():
    config = load_config_from(render())
    assert config.scheduler.credential_backup_interval == 86400
    assert config.security.credential_backup_retention == 30
    config = load_config_from(render(istota_credential_backup_interval=123,
        istota_credential_backup_retention=7, istota_credential_history_days=9,
        istota_credential_audit_days=11, istota_credential_exports_per_day=5,
        istota_web_auth_step_up_ttl_minutes=12))
    assert config.scheduler.credential_backup_interval == 123
    assert config.security.credential_backup_retention == 7
    assert config.security.credential_history_days == 9
    assert config.security.credential_audit_days == 11
    assert config.security.credential_exports_per_day == 5
    assert config.web.auth_step_up_ttl_minutes == 12


def test_installer_mapping_reaches_config():
    from tests.test_ansible_vault_config import TestTheInstallerPathReachesBothSettings
    settings = {"scheduler": {"credential_backup_interval": 42},
                "security": {"credential_backup_retention": 4, "credential_history_days": 8,
                             "credential_audit_days": 10, "credential_exports_per_day": 6},
                "web": {"auth_step_up_ttl_minutes": 7}}
    converted = TestTheInstallerPathReachesBothSettings()._convert(settings)
    config = load_config_from(render(**converted))
    assert config.scheduler.credential_backup_interval == 42
    assert config.security.credential_backup_retention == 4
    assert config.security.credential_history_days == 8
    assert config.security.credential_audit_days == 10
    assert config.security.credential_exports_per_day == 6
    assert config.web.auth_step_up_ttl_minutes == 7

