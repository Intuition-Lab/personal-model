from persome.evomem import identity


def test_ascii_identity_does_not_match_inside_an_ordinary_word() -> None:
    roster = identity.Roster.build([("D", []), ("Ann", [])])

    assert identity.scan_mentions("closure evidence timeout", roster) == []
    assert identity.scan_mentions("planning the release", roster) == []


def test_ascii_identity_still_matches_as_a_complete_token() -> None:
    roster = identity.Roster.build([("D", []), ("Ann", [])])

    assert identity.scan_mentions("Ask D, then Ann.", roster) == ["D", "Ann"]


def test_chinese_identity_keeps_substring_matching() -> None:
    roster = identity.Roster.build([("\u5f20\u4f1f", [])])

    assert identity.scan_mentions("\u5f20\u4f1f\u4eca\u5929\u5728\u5fd9\u4ec0\u4e48", roster) == [
        "\u5f20\u4f1f"
    ]
