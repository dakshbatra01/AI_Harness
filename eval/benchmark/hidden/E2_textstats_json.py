import json

from textstats.cli import main


def _run(tmp_path, capsys, text, *flags):
    p = tmp_path / "t.txt"
    p.write_text(text)
    code = main([str(p), *flags])
    return code, capsys.readouterr().out


def test_json_flag_outputs_report(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, "one two two three three three", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["words"] == 6
    assert data["unique"] == 3
    assert data["avg_length"] == round((3 + 3 + 3 + 5 + 5 + 5) / 6, 2)
    assert data["top"][0] == ["three", 3]


def test_json_respects_top(tmp_path, capsys):
    _, out = _run(tmp_path, capsys, "a a b c", "--json", "--top", "1")
    assert json.loads(out)["top"] == [["a", 2]]


def test_text_is_still_default(tmp_path, capsys):
    _, out = _run(tmp_path, capsys, "hello world")
    assert out.startswith("words: 2")
