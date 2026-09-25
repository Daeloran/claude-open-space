import pytest

from backend.events import is_pr_command


@pytest.mark.parametrize("cmd", ["gh pr create --fill", "cd x && glab mr create --fill", "gh  pr   create -t 'a'"])
def test_detecte_pr_mr(cmd):
    assert is_pr_command("Bash", {"command": cmd})


@pytest.mark.parametrize("name,data", [("Bash", {"command": "gh pr view 3"}), ("Bash", {"command": "glab mr list"}),
                                       ("Bash", {"command": "echo ghpr create"}), ("Bash", None), ("Read", {"command": "gh pr create"})])
def test_ignore_le_reste(name, data):
    assert not is_pr_command(name, data)
