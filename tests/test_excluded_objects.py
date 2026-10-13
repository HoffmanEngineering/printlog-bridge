from printlog_bridge.adapters.moonraker.excluded import note


def test_counts_against_the_files_objects():
    assert note(["PART_3", "PART_5"], 6) == "2 of 6 objects were cancelled: PART_3, PART_5"
    assert note(["A"], 1) == "1 of 1 object was cancelled: A"


def test_without_the_files_objects_just_counts_the_cancelled():
    assert note(["A", "B"], 0) == "2 objects were cancelled: A, B"
    assert note(["A"], 0) == "1 object was cancelled: A"


def test_nothing_cancelled_is_no_note():
    assert note([], 6) is None
