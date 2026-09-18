from storage import init_db
def test_db(tmp_path):
    p = tmp_path / "x.sqlite3"
    init_db(str(p))
    assert p.exists()
