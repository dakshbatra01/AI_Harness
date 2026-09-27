from slugkit import slugify, unique_slug


def test_runs_of_symbols_become_one_separator():
    assert slugify("x -- y") == "x-y"
    assert slugify("C++ & Python!!") == "c-python"


def test_no_edge_separators():
    assert slugify("__init__ file") == "init-file"
    assert slugify("!!!") == ""


def test_truncation_does_not_end_with_separator():
    assert slugify("Hello World", max_length=6) == "hello"


def test_custom_separator_and_unique():
    assert slugify("A  B", sep="_") == "a_b"
    assert unique_slug("My Post!", {"my-post"}) == "my-post-2"
