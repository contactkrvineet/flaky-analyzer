import flaky_analyzer
from app import seed_demo


def test_demo_scoring_detects_expected_flaky_tests(tmp_path):
    db_path = tmp_path / "test_flaky.db"
    flaky_analyzer.DB_PATH = str(db_path)

    seed_demo()
    rows = flaky_analyzer.score_tests()
    ids = [r["test_id"] for r in rows]

    assert len(rows) == 3
    assert "com.api.LoginTest#redirectsAfterLogin" in ids
    assert "com.api.PaymentTest#chargesCard" in ids
    assert "com.api.CartTest#concurrentAdd" in ids
    assert "com.api.HealthTest#pingReturns200" not in ids
    assert all(r["score"] > 0 for r in rows)
