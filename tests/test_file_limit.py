from pterodactyl_operator.limits import MAX_FILE_BYTES, within_file_limit


def test_file_limit_matches_wings_upload_limit():
    assert MAX_FILE_BYTES == 100 * 1024 * 1024
    assert within_file_limit(0)
    assert within_file_limit(MAX_FILE_BYTES)
    assert not within_file_limit(MAX_FILE_BYTES + 1)
    assert not within_file_limit(-1)
