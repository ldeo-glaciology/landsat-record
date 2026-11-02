import json
from pathlib import Path

import pytest
from fsspec.implementations.local import LocalFileSystem

import landsat_record.cli as cli
import landsat_record.publish as publish
from landsat_record.keys import store_key

from landsat_record.publish import ALWAYS_COPY

from .conftest import BOUNDS, FakeGCS
from .test_keys import SECRET_FRAGMENT

BOX = [str(v) for v in BOUNDS]


@pytest.fixture
def bucket(tmp_path):
    return FakeGCS(tmp_path / "gcs")


@pytest.fixture
def cli_env(scratch, sources, monkeypatch, config_home, bucket):
    """The CLI with local files for scratch and for the bucket."""
    monkeypatch.setattr(cli, "scratch_filesystem", lambda scratch: LocalFileSystem(auto_mkdir=True))
    monkeypatch.setattr(cli, "missing_permissions", lambda key, bucket: [])
    monkeypatch.setattr(publish, "gcs_filesystem", lambda key: bucket)
    return scratch


def run(*args):
    return cli.main(["run", *args])


def test_stages():
    parser = cli.build_parser()
    stages = lambda line: cli.choose_stages(parser.parse_args(["run", "x", *line.split()]))
    assert stages("") == ["search", "crop", "cogs", "browse"]
    assert stages("--publish") == ["search", "crop", "cogs", "browse", "publish"]
    assert stages("--from browse --publish") == ["browse", "publish"]
    assert stages("--from cogs --to browse") == ["cogs", "browse"]
    with pytest.raises(cli.SiteError):
        stages("--from publish")
    with pytest.raises(cli.SiteError):
        stages("--from browse --to crop")


def test_outline_flags_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "x", "--bbox", "-68", "-71", "-67", "-70", "--geojson", "f.geojson"])


def test_dry_run_writes_nothing(cli_env, capsys):
    assert run("flask_tiny", "--bbox-3031", *BOX, "--dry-run", "--scratch", cli_env) == 0
    out = capsys.readouterr().out
    assert "Catalogue: 13 scenes" in out and "nothing was written" in out
    assert not Path(cli_env).exists()


def test_first_run_needs_an_outline(cli_env, capsys):
    assert run("flask_tiny", "--scratch", cli_env) == 2
    assert "needs an outline" in capsys.readouterr().err


def test_run_then_rerun_reuses_and_guards_the_outline(cli_env, capsys, tmp_path):
    assert run("flask_tiny", "--bbox-3031", *BOX, "--title", "Flask test", "--test", "--scratch", cli_env) == 0
    out = capsys.readouterr().out
    assert "[4/4] Browse images and viewer" in out and "Full run, projected" in out and "Not published" in out
    stored = json.loads((Path(cli_env) / "flask_tiny_test" / "site.json").read_text())
    assert stored["title"] == "Flask test"
    index = json.loads((Path(cli_env) / "flask_tiny_test_cog" / "index.json").read_text())
    assert index["title"] == "Flask test Landsat record"

    # No outline: the stored one is used, along with its title
    assert run("flask_tiny", "--test", "--from", "browse", "--scratch", cli_env) == 0
    assert '"Flask test"' in capsys.readouterr().out

    # A different outline is refused
    bigger = [str(BOUNDS[0] - 5000), *BOX[1:]]
    assert run("flask_tiny", "--bbox-3031", *bigger, "--test", "--scratch", cli_env) == 2
    assert "different outline" in capsys.readouterr().err


def test_publish_needs_a_key(cli_env, capsys):
    assert run("flask_tiny", "--bbox-3031", *BOX, "--test", "--publish", "--scratch", cli_env) == 2
    assert "landsat-record login" in capsys.readouterr().err


def test_publish_end_to_end_keeps_the_key_secret(cli_env, fake_key, bucket, capsys, tmp_path):
    store_key(fake_key)
    code = run("flask_tiny", "--bbox-3031", *BOX, "--test", "--publish", "--publish-to", "gs://test-bucket/landsat", "--scratch", cli_env)
    output = capsys.readouterr()
    assert code == 0, output.err
    assert "as publisher@example.iam.gserviceaccount.com" in output.out
    assert "Viewer: https://storage.googleapis.com/test-bucket/landsat/flask_tiny_test/index.html" in output.out
    published = bucket.root / "test-bucket/landsat/flask_tiny_test"
    local = {str(p.relative_to(f"{cli_env}/flask_tiny_test_cog")) for p in Path(f"{cli_env}/flask_tiny_test_cog").rglob("*") if p.is_file()}
    assert {str(p.relative_to(published)) for p in published.rglob("*") if p.is_file()} == local

    # Publishing again copies only the index and tables
    bucket.content_types.clear()
    assert run("flask_tiny", "--test", "--from", "publish", "--publish", "--publish-to", "gs://test-bucket/landsat", "--scratch", cli_env) == 0
    assert {Path(path).name for path in bucket.content_types} == ALWAYS_COPY

    output = capsys.readouterr()
    assert SECRET_FRAGMENT not in output.out + output.err
    for path in tmp_path.rglob("*"):
        if path.is_file() and path not in (fake_key, cli.stored_key_path()):
            assert SECRET_FRAGMENT.encode() not in path.read_bytes(), path


def test_bad_publish_destination(cli_env, capsys):
    assert run("flask_tiny", "--bbox-3031", *BOX, "--publish-to", "s3://elsewhere", "--scratch", cli_env) == 2
    assert "gs://" in capsys.readouterr().err
