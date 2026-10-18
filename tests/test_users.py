"""VF extRelationId must be a uint64; NodeRings CRs store org UUIDs."""

from virtfusion_operator.users import ext_relation_id_for_api


def test_numeric_passthrough():
    assert ext_relation_id_for_api("42") == 42


def test_uuid_is_stable_positive_int():
    org = "e25f53d7-291f-4409-b1c9-0f3c6dc880c0"
    n = ext_relation_id_for_api(org)
    assert 1 <= n <= (1 << 63) - 1
    assert ext_relation_id_for_api(org) == n
    assert ext_relation_id_for_api(org.upper()) == n


def test_non_uuid_label_hashes():
    n = ext_relation_id_for_api("org-uuid")
    assert 1 <= n <= (1 << 63) - 1
    assert ext_relation_id_for_api("org-uuid") == n
