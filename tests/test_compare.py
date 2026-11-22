import pytest

from conftest import git
from looma.compare import CompareError, branch_details, compare, default_base, parse_patch, retire


def by_path(result):
    return {item["path"]: item for item in result["files"]}


def test_branch_against_main_groups_changes_by_function(repo):
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n\n\ndef bye():\n    return 'bye'\n")
    (repo / "Home.tsx").write_text("export const Home = () => {\n  return null;\n};\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "add bye")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("def hello():\n    return 'hello'\n")
    (repo / "Home.tsx").write_text("export const Home = () => {\n  return <View />;\n};\n\n"
                                   "export function Profile() {\n  return null;\n}\n")
    git(repo, "commit", "-q", "-am", "edit\n\nCo-Authored-By: Claude <noreply@anthropic.com>")

    result = compare(repo)

    assert (result["base"], result["head"], result["ahead"], result["behind"]) == ("main", "feature", 1, 0)
    assert result["commits"][0]["ai"] is True
    files = by_path(result)
    assert files["app.py"]["functions"] == [
        {"name": "hello", "change": "modified"}, {"name": "bye", "change": "deleted"}]
    assert files["Home.tsx"]["functions"] == [
        {"name": "Home", "change": "modified"}, {"name": "Profile", "change": "added"}]
    assert result["totals"]["files"] == 2


def test_only_changes_since_the_branch_left_main_are_shown(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "feature")
    git(repo, "checkout", "-q", "main")
    (repo / "later.py").write_text("y = 2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "later on main")

    result = compare(repo, head="feature")

    assert list(by_path(result)) == ["feature.py"]
    assert (result["ahead"], result["behind"]) == (1, 1)


def test_uncommitted_includes_edits_and_new_files(repo):
    (repo / "app.py").write_text("def hello():\n    return 'hey'\n")
    (repo / "new.py").write_text("def fresh():\n    pass\n")
    (repo / "package-lock.json").write_text("{}")

    assert compare(repo)["files"] == []
    files = by_path(compare(repo, uncommitted=True))

    assert files["app.py"]["status"] == "modified"
    assert files["new.py"]["status"] == "added"
    assert files["new.py"]["functions"] == [{"name": "fresh", "change": "added"}]
    assert "package-lock.json" not in files


def test_lockfiles_are_counted_but_not_shown(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "package-lock.json").write_text('{\n  "a": 1\n}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "lock")

    item = by_path(compare(repo))["package-lock.json"]

    assert item["generated"] is True and item["hunks"] == [] and item["additions"] == 3


def test_renames_and_deletes(repo):
    (repo / "old.txt").write_text("one\ntwo\nthree\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "files")
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "mv", "old.txt", "new.txt")
    git(repo, "rm", "-q", "app.py")
    git(repo, "commit", "-q", "-m", "move")

    files = by_path(compare(repo))

    assert files["new.txt"]["status"] == "renamed" and files["new.txt"]["old_path"] == "old.txt"
    assert files["app.py"]["status"] == "deleted"


def test_default_base_falls_back_to_master(tmp_path):
    path = tmp_path / "m"
    path.mkdir()
    git(path, "init", "-q", "-b", "master")
    git(path, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "x")
    assert default_base(path) == "master"


def test_default_base_finds_dev_in_any_case(tmp_path):
    path = tmp_path / "d"
    path.mkdir()
    git(path, "init", "-q", "-b", "Dev")
    git(path, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "x")
    git(path, "checkout", "-q", "-b", "feature/admin")
    assert default_base(path) == "Dev"
    assert compare(path)["base"] == "Dev"


def test_default_base_uses_a_branch_only_on_the_remote(tmp_path):
    upstream = tmp_path / "up"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "dev")
    git(upstream, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "x")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    git(clone, "checkout", "-q", "-b", "feature/admin")
    git(clone, "branch", "-q", "-D", "dev")
    git(clone, "remote", "set-head", "origin", "-d")
    assert default_base(clone) == "origin/dev"
    assert compare(clone)["base"] == "origin/dev"


def test_unknown_branch_is_an_error(repo):
    with pytest.raises(CompareError):
        compare(repo, head="nope")


def test_parse_patch_line_numbers():
    patch = ("diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n"
             "@@ -2,2 +2,2 @@ intro\n same\n-old\n+new\n")
    [item] = parse_patch(patch)
    lines = item["hunks"][0]["lines"]
    assert [(l["kind"], l["old"], l["new"]) for l in lines] == [
        ("context", 2, 2), ("delete", 3, None), ("add", None, 3)]
    assert item["hunks"][0]["context"] == "intro"


def test_state_changes_with_branches_commits_checkouts_and_edits(repo):
    from looma.compare import state

    seen = {state(repo)}
    git(repo, "branch", "feature")
    assert state(repo) not in seen
    seen.add(state(repo))
    git(repo, "checkout", "-q", "feature")
    assert state(repo) not in seen
    seen.add(state(repo))
    (repo / "app.py").write_text("def hello():\n    return 'yo'\n")
    assert state(repo) in seen  # edits only count when asked for
    first_edit = state(repo, uncommitted=True)
    (repo / "app.py").write_text("def hello():\n    return 'yo yo'\n")
    assert state(repo, uncommitted=True) != first_edit
    git(repo, "commit", "-q", "-am", "edit")
    assert state(repo) not in seen


def commit_file(repo, name, text="x = 1\n"):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)


def test_branch_made_from_an_unmerged_branch_counts_only_its_own_changes(repo):
    git(repo, "checkout", "-q", "-b", "first")
    commit_file(repo, "first.py")
    commit_file(repo, "first_more.py")
    git(repo, "checkout", "-q", "-b", "second")  # made while on first, before first is merged

    fresh = compare(repo)
    assert (fresh["head"], fresh["ahead"], fresh["files"], fresh["commits"]) == ("second", 0, [], [])

    commit_file(repo, "second.py")
    result = compare(repo)
    assert list(by_path(result)) == ["second.py"]
    assert result["ahead"] == 1 and [c["subject"] for c in result["commits"]] == ["second.py"]
    # The first branch still shows all of its own.
    assert sorted(by_path(compare(repo, head="first"))) == ["first.py", "first_more.py"]


def test_branch_made_from_an_unmerged_branch_with_uncommitted_work(repo):
    git(repo, "checkout", "-q", "-b", "first")
    commit_file(repo, "first.py")
    git(repo, "checkout", "-q", "-b", "second")
    (repo / "wip.py").write_text("y = 2\n")

    assert list(by_path(compare(repo, uncommitted=True))) == ["wip.py"]


def test_once_the_first_branch_is_merged_the_second_counts_from_main(repo):
    git(repo, "checkout", "-q", "-b", "first")
    commit_file(repo, "first.py")
    git(repo, "checkout", "-q", "-b", "second")
    commit_file(repo, "second.py")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge first", "first")
    commit_file(repo, "main.py")

    result = compare(repo, head="second")
    assert list(by_path(result)) == ["second.py"] and (result["ahead"], result["behind"]) == (1, 2)


def test_branch_checked_out_from_the_remote_keeps_its_commits(tmp_path, repo):
    git(repo, "checkout", "-q", "-b", "shared")
    commit_file(repo, "shared.py")
    git(repo, "checkout", "-q", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    git(clone, "checkout", "-q", "shared")  # a local copy of origin/shared, not a new branch

    assert list(by_path(compare(clone))) == ["shared.py"]


def test_rebased_branch_falls_back_to_where_it_left_main(repo):
    git(repo, "checkout", "-q", "-b", "first")
    commit_file(repo, "first.py")
    git(repo, "checkout", "-q", "-b", "second")
    commit_file(repo, "second.py")
    git(repo, "rebase", "-q", "--onto", "main", "first", "second")

    assert list(by_path(compare(repo))) == ["second.py"]


def test_new_branch_with_nothing_committed_is_not_merged(repo):
    git(repo, "branch", "fresh")
    git(repo, "checkout", "-q", "-b", "done")
    commit_file(repo, "done.py")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "done")

    details = {b["name"]: b["merged"] for b in branch_details(repo, "main")}
    assert details == {"main": False, "fresh": False, "done": True}


def test_merged_and_deleted_branches_go_after_15_minutes():
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    details = [{"name": "open", "merged": False, "gone": False},
               {"name": "merged", "merged": True, "gone": False},
               {"name": "gone", "merged": False, "gone": True},
               {"name": "here", "merged": True, "gone": True}]

    first = {b["name"]: b["removes_at"] for b in retire("1", details, "here", now=start)}
    assert first == {"open": None, "merged": "2026-10-26T12:15:00+00:00",
                     "gone": "2026-10-26T12:15:00+00:00", "here": "2026-10-26T12:15:00+00:00"}
    # The clock runs from when each was first seen, not from each look.
    later = retire("1", details, "here", now=start + timedelta(minutes=14))
    assert [b["removes_at"] for b in later if b["name"] == "merged"] == ["2026-10-26T12:15:00+00:00"]
    # After 15 minutes they're gone, except the checked-out branch; other projects keep their own clock.
    after = retire("1", details, "here", now=start + timedelta(minutes=15))
    assert [b["name"] for b in after] == ["open", "here"]
    assert [b["name"] for b in retire("2", details, None, now=start + timedelta(minutes=15))] == [
        "open", "merged", "gone", "here"]


def test_a_branch_that_gets_new_work_starts_its_clock_again():
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    merged = [{"name": "b", "merged": True, "gone": False}]
    retire("1", merged, None, now=start)
    retire("1", [{"name": "b", "merged": False, "gone": False}], None, now=start + timedelta(minutes=5))
    [again] = retire("1", merged, None, now=start + timedelta(minutes=20))
    assert again["removes_at"] == "2026-10-26T12:35:00+00:00"
