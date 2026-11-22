import json
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import git
from looma import codecommit
from looma.api.app import create_app


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


# The AWS CLI stood in for by a script that answers from a table and notes each call.
FAKE_AWS = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_AWS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
profile = args[args.index("--profile") + 1]
if profile == "expired":
    sys.stderr.write("Error when retrieving token from sso: Token has expired and refresh failed\n")
    sys.exit(255)
if profile == "nocc" and args[0] == "codecommit":
    sys.stderr.write("An error occurred (AccessDeniedException) when calling the ListRepositories operation: "
                     "User: arn:aws:iam::123:user/nocc is not authorized to perform: codecommit:ListRepositories\n")
    sys.exit(254)
answers = json.load(open(os.environ["FAKE_AWS_ANSWERS"]))
key = " ".join(args[:2])
if key == "codecommit get-pull-request":
    key += " " + args[args.index("--pull-request-id") + 1]
print(json.dumps(answers.get(key, {})))
'''

ANSWERS = {
    "sts get-caller-identity": {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/vardaan"},
    "codecommit list-repositories": {"repositories": [{"repositoryName": "looma"}, {"repositoryName": "old"}]},
    "codecommit batch-get-repositories": {"repositories": [
        {"repositoryName": "old", "lastModifiedDate": 1700000000.0},
        {"repositoryName": "looma", "repositoryDescription": "It", "defaultBranch": "main",
         "cloneUrlHttp": "https://git-codecommit.ap-south-1.amazonaws.com/v1/repos/looma",
         "lastModifiedDate": "2026-10-27T10:00:00+00:00"}]},
    "codecommit list-pull-requests": {"pullRequestIds": ["7"]},
    "codecommit get-pull-request 7": {"pullRequest": {
        "pullRequestId": "7", "title": "Add Ask", "authorArn": "arn:aws:iam::123456789012:user/vardaan",
        "lastActivityDate": "2026-10-28T09:00:00+00:00",
        "pullRequestTargets": [{"repositoryName": "looma", "sourceReference": "refs/heads/ask",
                                "destinationReference": "refs/heads/main"}]}},
}


@pytest.fixture
def aws(tmp_path, monkeypatch):
    folder = tmp_path / "awsbin"
    folder.mkdir()
    tool = folder / "aws"
    tool.write_text(FAKE_AWS.replace("{python}", sys.executable))
    tool.chmod(0o755)
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(ANSWERS))
    log = tmp_path / "aws.log"
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_AWS_ANSWERS", str(answers))
    monkeypatch.setenv("FAKE_AWS_LOG", str(log))
    aws_home = tmp_path / "aws-home"
    aws_home.mkdir()
    (aws_home / "config").write_text("[default]\nregion = us-east-1\n\n[profile work]\nregion = ap-south-1\n\n"
                                     "[sso-session corp]\nsso_region = us-east-1\n")
    (aws_home / "credentials").write_text("[default]\naws_access_key_id = AKIAEXAMPLEKEY\naws_secret_access_key = "
                                          "shh-secret\n\n[keysonly]\naws_access_key_id = AKIA2\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_home / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_home / "credentials"))
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_connecting_a_profile_then_listing_repos_and_pull_requests(aws, repo):
    api = client()
    before = api.get("/api/codecommit").json()
    assert (before["cli"], before["connected"]) == (True, False)
    # Names and regions only: an sso-session isn't a profile, and no key reaches the page.
    assert before["profiles"] == [{"name": "default", "region": "us-east-1"}, {"name": "keysonly", "region": None},
                                  {"name": "work", "region": "ap-south-1"}]
    assert "shh-secret" not in json.dumps(before) and "AKIA" not in json.dumps(before)

    done = api.post("/api/codecommit/connect", json={"profile": "work", "region": "ap-south-1"}).json()
    assert (done["connected"], done["profile"], done["region"], done["account"]) == (True, "work", "ap-south-1", "123456789012")
    assert all(call[call.index("--profile") + 1] == "work" for call in aws())

    # A project cloned from one of them is marked as such; newest change first, whichever CLI's time format.
    git(repo, "remote", "add", "origin", "https://git-codecommit.ap-south-1.amazonaws.com/v1/repos/looma")
    api.post("/api/repos", json={"path": str(repo)})
    listed = api.get("/api/codecommit/repos").json()
    assert [r["name"] for r in listed] == ["looma", "old"]
    assert listed[0]["tracked"] == str(repo) and listed[1]["tracked"] is None
    assert listed[1]["updated_at"].startswith("2023-11-14")
    assert "ap-south-1.console.aws.amazon.com" in listed[0]["url"]

    [project] = api.get("/api/repos").json()
    pulls = api.get(f"/api/repos/{project['id']}/pulls").json()
    assert (pulls["repo"], pulls["host"], pulls["connected"]) == ("looma", "codecommit", True)
    [pull] = pulls["pulls"]
    assert (pull["number"], pull["title"], pull["author"], pull["branch"], pull["base"]) == (7, "Add Ask", "vardaan", "ask", "main")
    assert "/pull-requests/7/" in pull["url"]

    assert api.request("DELETE", "/api/codecommit", json={}).json()["connected"] is False
    assert api.get("/api/codecommit/repos").status_code == 409
    after = api.get(f"/api/repos/{project['id']}/pulls").json()
    assert (after["host"], after["connected"], after["pulls"]) == ("codecommit", False, [])


def test_a_repository_in_another_region_says_so(aws, repo):
    api = client()
    api.post("/api/codecommit/connect", json={"profile": "work", "region": "ap-south-1"})
    git(repo, "remote", "add", "origin", "codecommit::eu-west-1://work@looma")
    api.post("/api/repos", json={"path": str(repo)})
    [project] = api.get("/api/repos").json()
    pulls = api.get(f"/api/repos/{project['id']}/pulls").json()
    assert pulls["pulls"] == [] and "eu-west-1" in pulls["error"]


def test_a_profile_aws_refuses_is_not_kept(aws):
    api = client()
    expired = api.post("/api/codecommit/connect", json={"profile": "expired", "region": "us-east-1"})
    assert expired.status_code == 400 and "aws sso login --profile expired" in expired.json()["detail"]
    denied = api.post("/api/codecommit/connect", json={"profile": "nocc", "region": "us-east-1"})
    assert denied.status_code == 400 and "isn't allowed" in denied.json()["detail"]
    assert api.get("/api/codecommit").json()["connected"] is False
    assert api.post("/api/codecommit/connect", json={"profile": "work", "region": "Mumbai"}).status_code == 400
    assert api.post("/api/codecommit/connect", json={"profile": "work; rm -rf ~", "region": "us-east-1"}).status_code == 400


def test_no_aws_cli_says_how_to_get_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(codecommit, "_find_aws", lambda: None)
    api = client()
    assert api.get("/api/codecommit").json()["cli"] is False
    failed = api.post("/api/codecommit/connect", json={"profile": "default", "region": "us-east-1"})
    assert failed.status_code == 400 and "aws.amazon.com/cli" in failed.json()["detail"]


def test_cloning_sets_the_clone_to_sign_with_the_profile_and_keeps_no_key(aws, repo, tmp_path, monkeypatch):
    # CodeCommit stood in for by a folder of bare repositories.
    hosted = tmp_path / "hosted"
    hosted.mkdir()
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(hosted / "tool"))
    monkeypatch.setattr(codecommit, "_https", lambda region, name: (hosted / name).as_uri())
    api = client()
    api.post("/api/codecommit/connect", json={"profile": "work", "region": "ap-south-1"})

    made = api.post("/api/codecommit/clone", json={"name": "tool", "folder": str(tmp_path / "code")})
    assert made.status_code == 201 and made.json()["name"] == "tool"
    cloned = tmp_path / "code" / "tool"
    assert (cloned / "app.py").is_file()
    config = (cloned / ".git" / "config").read_text()
    assert "--profile work codecommit credential-helper" in config and "usehttppath = true" in config.lower()
    assert "shh-secret" not in config and "AKIA" not in config

    again = api.post("/api/codecommit/clone", json={"name": "tool", "folder": str(tmp_path / "code")})
    assert again.status_code == 400 and "already exists" in again.json()["detail"]
    assert api.post("/api/codecommit/clone", json={"name": "../etc", "folder": str(tmp_path)}).status_code == 400


def test_codecommit_remotes_are_read_in_every_spelling():
    for url, expected in (
        ("https://git-codecommit.ap-south-1.amazonaws.com/v1/repos/dev-dash", ("ap-south-1", "dev-dash")),
        ("https://vardaan-at-123@git-codecommit.us-east-1.amazonaws.com/v1/repos/dev-dash", ("us-east-1", "dev-dash")),
        ("ssh://APKAEXAMPLE@git-codecommit.eu-west-1.amazonaws.com/v1/repos/dev-dash", ("eu-west-1", "dev-dash")),
        ("https://git-codecommit-fips.us-east-2.amazonaws.com/v1/repos/dev-dash/", ("us-east-2", "dev-dash")),
        ("codecommit::us-west-2://work@dev-dash", ("us-west-2", "dev-dash")),
        ("codecommit://dev-dash", (None, "dev-dash")),
        ("codecommit://work@dev-dash", (None, "dev-dash")),
    ):
        assert codecommit.parse_remote(url) == expected, url
    assert codecommit.parse_remote("https://github.com/vardaan/dev-dash.git") is None


def test_a_github_project_still_shows_github(aws, repo):
    git(repo, "remote", "add", "origin", "git@github.com:vardaan/looma.git")
    api = client()
    api.post("/api/repos", json={"path": str(repo)})
    [project] = api.get("/api/repos").json()
    pulls = api.get(f"/api/repos/{project['id']}/pulls").json()
    assert (pulls["repo"], pulls["host"], pulls["connected"]) == ("vardaan/looma", "github", False)
