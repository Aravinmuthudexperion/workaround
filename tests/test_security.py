from arc_tower.core.security import (INGEST_KEY_PREFIX, decrypt, encrypt, hash_key, hash_password, key_hint,
                                     new_opaque_key, verify_password)


def test_password_hash_roundtrip():
    stored = hash_password("s3cret!")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret!", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret!", None)


def test_keys_are_hashed_not_stored():
    key = new_opaque_key(INGEST_KEY_PREFIX)
    assert key.startswith(INGEST_KEY_PREFIX)
    assert hash_key(key) == hash_key(key) and len(hash_key(key)) == 64
    assert key not in key_hint(key) and key_hint(key).startswith("arc_")


def test_encryption_roundtrip():
    assert decrypt(encrypt("pk:sk")) == "pk:sk"
