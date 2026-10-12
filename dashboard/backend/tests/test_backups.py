from app.services.backup import valid_backup_name


def test_valid_names():
    assert valid_backup_name("20260619-042015-manual.tar.gz")
    assert valid_backup_name("20260101-000000-pre-restart.tar.gz")
    assert valid_backup_name("20260101-000000-weekly.tar.gz")


def test_invalid_names():
    for bad in [
        "../etc/passwd",
        "evil.tar.gz",
        "20260619-042015-manual.zip",
        "20260619-042015-manual.tar.gz/../x",
        "; rm -rf /",
        "manual.tar.gz",
        "",
    ]:
        assert not valid_backup_name(bad), bad
