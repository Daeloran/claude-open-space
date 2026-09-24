"""Tests de logique de backend/projects.py : lecture partielle des transcripts."""
import json

import backend.projects as projects


def test_cwd_au_dela_de_max_lines_ignore(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join([json.dumps({"type": "x"})] * 60 + [json.dumps({"cwd": str(tmp_path)})]))
    assert projects.first_cwd(f) is None
    assert projects.first_cwd(f, max_lines=100) == str(tmp_path)


def test_transcript_illisible_ignore(tmp_path):
    assert projects.first_cwd(tmp_path / "absent.jsonl") is None
    (tmp_path / "bin.jsonl").write_bytes(b"\xff\xfe[1,2]\n")
    assert projects.first_cwd(tmp_path / "bin.jsonl") is None


def test_lecture_arretee_des_la_limite_atteinte(tmp_path, monkeypatch):
    for i in range(10):
        d = tmp_path / f"p{i}"
        d.mkdir()
        s = tmp_path / "cfg" / "projects" / f"s{i}"
        s.mkdir(parents=True)
        (s / "t.jsonl").write_text(json.dumps({"cwd": str(d)}) + "\n")
    read = []
    real = projects.first_cwd
    monkeypatch.setattr(projects, "first_cwd", lambda p: read.append(p) or real(p))
    assert len(projects.recent_projects(tmp_path / "cfg", limit=3)) == 3
    assert len(read) == 3


def test_extra_relatif_rendu_absolu(tmp_path, monkeypatch):
    (tmp_path / "rel").mkdir()
    monkeypatch.chdir(tmp_path)
    assert projects.recent_projects(tmp_path / "vide", extra="rel") == [{"cwd": str(tmp_path / "rel"), "name": "rel"}]


def test_symlinked_paths_are_one_project(tmp_path):
    real = tmp_path / "real" / "eter"
    real.mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    proj = tmp_path / "cfg" / "projects" / "p"
    proj.mkdir(parents=True)
    for i, cwd in enumerate([real, tmp_path / "link" / "eter"]):
        (proj / f"{i}.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")
    assert [p["name"] for p in projects.recent_projects(tmp_path / "cfg")] == ["eter"]
