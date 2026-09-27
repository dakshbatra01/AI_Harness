from textstats import average_word_length, top_words
from textstats.cli import main


def test_average_of_empty_text_is_zero():
    assert average_word_length("") == 0.0
    assert average_word_length("  \n\t ") == 0.0
    assert average_word_length("... !!!") == 0.0


def test_top_words_empty():
    assert top_words("") == []


def test_cli_on_empty_file(tmp_path, capsys):
    p = tmp_path / "empty.txt"
    p.write_text("")
    assert main([str(p)]) == 0
    out = capsys.readouterr().out
    assert "words: 0" in out
    assert "average length: 0.0" in out
