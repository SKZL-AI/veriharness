"""A release gate must reject changed bytes and unexpected index files."""
import hashlib
import io
import json
import urllib.error

import pytest

from tools import distribution_release as release


@pytest.fixture
def bundle(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    hashes = {}
    for name in release.FILES:
        raw = name.encode()
        (dist / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    (tmp_path / "SHA256SUMS.txt").write_text("".join(f"{v}  {k}\n" for k, v in hashes.items()))
    return tmp_path


def test_changed_local_artifact_is_refused(bundle):
    (bundle / "dist" / "hoh-0.1.0.tar.gz").write_bytes(b"changed")
    with pytest.raises(AssertionError):
        release.expected_hashes(bundle)


@pytest.mark.parametrize("fault", ["digest", "extra", "missing", "duplicate"])
def test_existing_index_must_match_complete_file_set(bundle, monkeypatch, fault):
    hashes = release.expected_hashes(bundle)
    files = [{"filename": k, "digests": {"sha256": v}} for k, v in hashes.items()]
    if fault == "digest":
        files[0]["digests"]["sha256"] = "0" * 64
    elif fault == "extra":
        files.append({"filename": "unexpected.whl", "digests": {"sha256": "0" * 64}})
    elif fault == "missing":
        files.pop()
    else:
        files.append(files[0])
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps({"urls": files}).encode()))
    with pytest.raises(AssertionError, match="Published files differ"):
        release.index(bundle, "testpypi", False)


def test_only_404_can_mean_absent(bundle, monkeypatch):
    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org", 403, "Forbidden", {}, None)
    monkeypatch.setattr(release.urllib.request, "urlopen", unavailable)
    with pytest.raises(urllib.error.HTTPError):
        release.index(bundle, "pypi", True)


def test_other_release_ref_is_refused(tmp_path):
    with pytest.raises(AssertionError, match="Only the audited"):
        release.identity(tmp_path, "main")
