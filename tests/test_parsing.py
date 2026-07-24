"""Reading the JSON array back out of whatever the model actually wrote."""
from src.parsing import TRUNCATED, UNPARSEABLE, extract_json_array, extract_json_array_status


def test_plain_array():
    assert extract_json_array('[{"i": 1}]') == [{"i": 1}]


def test_markdown_fence():
    assert extract_json_array('```json\n[{"i": 1}]\n```') == [{"i": 1}]


def test_bracket_in_the_preamble_does_not_swallow_the_array():
    # A greedy [.*] spans from "[for your entries]" to the final "]" and fails.
    raw = 'Here are the results [for your entries]:\n[{"i": 1, "v": "relevant"}]'
    assert extract_json_array(raw) == [{"i": 1, "v": "relevant"}]


def test_trailing_prose_after_the_array():
    raw = '[{"i": 1}]\nLet me know if you want [more detail].'
    assert extract_json_array(raw) == [{"i": 1}]


def test_brackets_inside_strings_are_not_structure():
    raw = '[{"summary": "read about arrays [] and objects {}"}]'
    assert extract_json_array(raw) == [{"summary": "read about arrays [] and objects {}"}]


def test_longest_array_wins_over_an_inline_example():
    raw = 'For example ["a"], here is the answer:\n[{"i": 1}, {"i": 2}, {"i": 3}]'
    assert extract_json_array(raw) == [{"i": 1}, {"i": 2}, {"i": 3}]


def test_truncated_response_keeps_the_complete_objects():
    raw = '[{"i": 1, "category": "A"}, {"i": 2, "category": "B"}, {"i": 3, "categ'
    array, problem = extract_json_array_status(raw)
    assert problem == TRUNCATED
    assert array == [{"i": 1, "category": "A"}, {"i": 2, "category": "B"}]


def test_nothing_at_all():
    array, problem = extract_json_array_status("I cannot help with that.")
    assert array is None and problem == UNPARSEABLE


def test_clean_parse_reports_no_problem():
    assert extract_json_array_status('[{"i": 1}]') == ([{"i": 1}], None)
