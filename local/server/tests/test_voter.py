from app.recognizer import PlateVoter


def test_confirmation_after_n_reads():
    v = PlateVoter("cam", min_confirmations=2, session_gap=2.0)
    s1, new1 = v.add("A123BC77", 0.9, 0.0)
    assert not new1
    s2, new2 = v.add("A123BC77", 0.9, 0.3)
    assert new2 and s1 is s2
    _, new3 = v.add("A123BC77", 0.9, 0.6)
    assert not new3  # повторно не сообщаем


def test_similar_reads_share_session_and_expire():
    v = PlateVoter("cam", min_confirmations=2, session_gap=2.0)
    s1, _ = v.add("A123BC77", 0.9, 0.0)
    s2, _ = v.add("A123BC71", 0.7, 0.2)
    s3, _ = v.add("K555MM99", 0.9, 0.3)
    assert s1 is s2 and s3 is not s1
    assert v.expire(1.0) == []
    done = v.expire(2.5)
    assert len(done) == 2
    assert s1.best()[0] == "A123BC77"
