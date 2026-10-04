from openreactor.auth import hash_password, looks_like_hash, verify_password
from openreactor.web import Gate


def test_a_hash_verifies_its_password_only():
    stored = hash_password("correct horse")
    assert looks_like_hash(stored)
    assert verify_password("correct horse", stored)
    assert not verify_password("correct horsE", stored)
    assert not verify_password("", stored)


def test_each_hash_has_its_own_salt():
    assert hash_password("same") != hash_password("same")


def test_a_malformed_hash_never_matches():
    stored = hash_password("pw")
    scheme, n, r, p, salt, key = stored.split("$")
    for bad in [
        "",
        "pw",
        f"bcrypt${n}${r}${p}${salt}${key}",
        f"scrypt${n}${r}${p}${salt}",
        f"scrypt$x${r}${p}${salt}${key}",
        f"scrypt${n}${r}${p}$!!!${key}",
    ]:
        assert not verify_password("pw", bad), bad


def test_a_cost_out_of_bounds_is_refused_without_running_scrypt(monkeypatch):
    from openreactor import auth

    stored = hash_password("pw")
    _, n, r, p, salt, key = stored.split("$")

    def no_scrypt(*args):
        raise AssertionError("scrypt ran")

    monkeypatch.setattr(auth, "_scrypt", no_scrypt)
    for bad in [
        f"scrypt${2**21}${r}${p}${salt}${key}",
        f"scrypt${n}$33${p}${salt}${key}",
        f"scrypt${n}${r}$17${salt}${key}",
        f"scrypt$1${r}${p}${salt}${key}",
    ]:
        assert not verify_password("pw", bad), bad


def test_the_gate_remembers_a_verified_password_only():
    gate = Gate(hash_password("pw"))
    assert not gate.check("nope")
    assert not gate.check("nope")
    assert gate.check("pw")
    assert gate.check("pw")
    assert not gate.check("nope")
    assert Gate(None).check("anything")
