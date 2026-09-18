from storage import get_state, init_db, record_risk_event, set_state

def test_storage_roundtrip(tmp_path):
    db=tmp_path/"grid.sqlite3"
    init_db(str(db))
    set_state(str(db),"foo",{"bar":1})
    assert get_state(str(db),"foo") == '{"bar":1}'
    record_risk_event(str(db),False,"TEST_BLOCK",{"x":1})
