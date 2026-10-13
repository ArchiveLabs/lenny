"""
Tests for loan settings, due-date expiry, per-patron limit enforcement,
and the borrow/unborrow model logic.
"""

import os
import datetime
import pytest
from unittest.mock import patch, MagicMock, call


os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")
os.environ.setdefault("ADMIN_INTERNAL_SECRET", "test-internal-secret")


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def app_client():
    from lenny.app import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=True)


def _admin_headers():
    """Bypass real token verification — patch verify_admin_token to True."""
    return {
        "X-Admin-Internal-Secret": "test-internal-secret",
        "Authorization": "Bearer fake-token",
    }


# ─── configs loan getters (cross-worker authoritative read) ──────────────────

class TestLoanConfigGetters:
    def test_falls_back_to_global_when_no_file(self, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "LOAN_ENV_PATH", "/nonexistent/loan.env")
        monkeypatch.setattr(lenny_configs, "LOAN_LIMIT", 9)
        monkeypatch.setattr(lenny_configs, "LOAN_DURATION_DAYS", 13)
        assert lenny_configs.get_loan_limit() == 9
        assert lenny_configs.get_loan_duration_days() == 13

    def test_file_overrides_global(self, monkeypatch, tmp_path):
        from lenny import configs as lenny_configs
        loan_env = tmp_path / "loan.env"
        loan_env.write_text("LENNY_LOAN_LIMIT=3\nLENNY_LOAN_DURATION_DAYS=7\n")
        monkeypatch.setattr(lenny_configs, "LOAN_ENV_PATH", str(loan_env))
        # Globals are stale (simulating a worker that hasn't restarted) — file wins.
        monkeypatch.setattr(lenny_configs, "LOAN_LIMIT", 99)
        monkeypatch.setattr(lenny_configs, "LOAN_DURATION_DAYS", 99)
        assert lenny_configs.get_loan_limit() == 3
        assert lenny_configs.get_loan_duration_days() == 7

    def test_malformed_file_value_falls_back(self, monkeypatch, tmp_path):
        from lenny import configs as lenny_configs
        loan_env = tmp_path / "loan.env"
        loan_env.write_text("LENNY_LOAN_LIMIT=notanint\n")
        monkeypatch.setattr(lenny_configs, "LOAN_ENV_PATH", str(loan_env))
        monkeypatch.setattr(lenny_configs, "LOAN_LIMIT", 8)
        assert lenny_configs.get_loan_limit() == 8


# ─── Loan._active_filters ────────────────────────────────────────────────────

class TestLoanActiveFilters:
    def test_returns_two_clauses(self):
        from lenny.core.models import Loan
        filters = Loan._active_filters()
        assert len(filters) == 2

    def test_first_clause_covers_returned_at(self):
        from lenny.core.models import Loan
        filters = Loan._active_filters()
        assert "returned_at" in str(filters[0])

    def test_second_clause_covers_due_date(self):
        from lenny.core.models import Loan
        filters = Loan._active_filters()
        assert "due_date" in str(filters[1])


# ─── Loan.create sets due_date ────────────────────────────────────────────────

class TestLoanCreateDueDate:
    def test_due_date_set_when_duration_configured(self):
        from lenny import configs
        before = datetime.datetime.now(datetime.timezone.utc)
        with patch.object(configs, "LOAN_DURATION_DAYS", 14):
            due = None
            if configs.LOAN_DURATION_DAYS > 0:
                due = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=configs.LOAN_DURATION_DAYS)
            assert due is not None
            assert (due - before).days >= 13

    def test_due_date_none_when_duration_zero(self):
        from lenny import configs
        with patch.object(configs, "LOAN_DURATION_DAYS", 0):
            due = None
            if configs.LOAN_DURATION_DAYS > 0:
                due = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=configs.LOAN_DURATION_DAYS)
            assert due is None

    def test_loan_create_calls_db_with_due_date(self):
        from lenny.core.models import Loan
        from lenny import configs

        captured_loan = {}

        def fake_add(obj):
            captured_loan["obj"] = obj

        with patch.object(configs, "LOAN_DURATION_DAYS", 7), \
             patch("lenny.core.models.db") as mock_db:
            mock_db.add.side_effect = fake_add
            mock_db.commit.return_value = None

            before = datetime.datetime.now(datetime.timezone.utc)
            loan = Loan.create(item_id=1, email="abc123", hashed=True)
            assert loan.due_date is not None
            assert loan.due_date > before

    def test_loan_create_no_due_date_when_zero(self):
        from lenny.core.models import Loan
        from lenny import configs

        with patch.object(configs, "LOAN_DURATION_DAYS", 0), \
             patch("lenny.core.models.db") as mock_db:
            mock_db.add.return_value = None
            mock_db.commit.return_value = None

            loan = Loan.create(item_id=1, email="abc123", hashed=True)
            assert loan.due_date is None


# ─── Loan.exists_any (for unborrow) ──────────────────────────────────────────

class TestLoanExistsAny:
    def test_exists_any_returns_expired_loan(self):
        """exists_any finds loans even when due_date is past."""
        from lenny.core.models import Loan

        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        expired_loan = MagicMock()
        expired_loan.returned_at = None
        expired_loan.due_date = past

        with patch("lenny.core.models.db") as mock_db:
            mock_db.query.return_value.filter.return_value.first.return_value = expired_loan
            result = Loan.exists_any(item_id=1, email="hashed", hashed=True)
            assert result is expired_loan

    def test_exists_active_filter_excludes_expired(self):
        """exists() with active filter returns None when only expired loans exist."""
        from lenny.core.models import Loan

        with patch("lenny.core.models.db") as mock_db:
            mock_db.query.return_value.filter.return_value.first.return_value = None
            result = Loan.exists(item_id=1, email="hashed", hashed=True)
            assert result is None

    def test_exists_any_does_not_apply_due_date_filter(self):
        """exists_any query must NOT include due_date clause."""
        from lenny.core.models import Loan

        with patch("lenny.core.models.db") as mock_db:
            filter_mock = MagicMock()
            filter_mock.first.return_value = None
            mock_db.query.return_value.filter.return_value = filter_mock
            Loan.exists_any(item_id=1, email="hashed", hashed=True)

            call_args = mock_db.query.return_value.filter.call_args
            clause_strings = [str(c) for c in call_args[0]]
            assert not any("due_date" in s for s in clause_strings)


# ─── Item.unborrow hashed path (for the OAuth2 /return bearer endpoint) ───────

class TestUnborrowHashedPath:
    """A bearer-token return (POST /oauth2/return) presents an already-hashed
    email. unborrow must forward `hashed` to `exists_any`, or the hash is hashed
    a second time and the lookup misses a loan that exists."""

    def _item(self, olid):
        from lenny.core.models import FormatEnum, Item
        return Item(openlibrary_edition=olid, encrypted=True, formats=FormatEnum.EPUB)

    def test_hashed_true_is_forwarded_verbatim(self):
        from lenny.core.models import Loan
        item = self._item(1)
        loan = MagicMock()
        with patch.object(Loan, "exists_any", return_value=loan) as exists_any:
            result = item.unborrow("deadbeefhash", hashed=True)
        exists_any.assert_called_once_with(item.id, "deadbeefhash", hashed=True)
        loan.finalize.assert_called_once()
        assert result is loan.finalize.return_value

    def test_default_is_the_plaintext_path(self):
        """The cookie/OPDS path still passes a plaintext address (hashed=False)."""
        from lenny.core.exceptions import LoanNotFoundError
        from lenny.core.models import Loan
        item = self._item(2)
        with patch.object(Loan, "exists_any", return_value=None) as exists_any:
            with pytest.raises(LoanNotFoundError):
                item.unborrow("patron@example.com")
        exists_any.assert_called_once_with(item.id, "patron@example.com", hashed=False)


# ─── borrow_item route: PatronLoanLimitError → 403 ───────────────────────────

class TestBorrowRouteErrors:
    """Test that the borrow route correctly maps model exceptions to HTTP codes."""

    def _mock_item(self, borrow_side_effect):
        mock_item = MagicMock()
        mock_item.borrow.side_effect = borrow_side_effect
        return mock_item

    def test_patron_loan_limit_returns_403(self, app_client, monkeypatch):
        from lenny.core.exceptions import PatronLoanLimitError
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "ADMIN_INTERNAL_SECRET", "test-internal-secret")

        mock_item = self._mock_item(PatronLoanLimitError("Loan limit of 2 reached."))

        with patch("lenny.routes.api.Item.exists", return_value=mock_item), \
             patch("lenny.routes.api.get_authenticated_email", return_value="patron@example.com"):
            resp = app_client.get("/v1/api/items/123/borrow", follow_redirects=False)

        assert resp.status_code == 403
        assert "limit" in resp.json()["detail"].lower()

    def test_book_unavailable_returns_409(self, app_client, monkeypatch):
        from lenny.core.exceptions import BookUnavailableError
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "ADMIN_INTERNAL_SECRET", "test-internal-secret")

        mock_item = self._mock_item(BookUnavailableError("No copies available."))

        with patch("lenny.routes.api.Item.exists", return_value=mock_item), \
             patch("lenny.routes.api.get_authenticated_email", return_value="patron@example.com"):
            resp = app_client.get("/v1/api/items/123/borrow", follow_redirects=False)

        assert resp.status_code == 409


# ─── Admin loan settings endpoint ────────────────────────────────────────────

class TestLoanSettingsEndpoint:
    def _bypass_admin(self):
        """Context manager that bypasses both admin checks."""
        return patch.multiple(
            "lenny.routes.api.auth",
            verify_admin_internal_secret=MagicMock(return_value=True),
            verify_admin_token=MagicMock(return_value=True),
        )

    def test_get_returns_current_values(self, app_client, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "LOAN_LIMIT", 5)
        monkeypatch.setattr(lenny_configs, "LOAN_DURATION_DAYS", 14)

        with self._bypass_admin():
            resp = app_client.get("/v1/api/admin/loan/settings", headers=_admin_headers())

        assert resp.status_code == 200
        data = resp.json()
        assert data["loan_limit"] == 5
        assert data["loan_duration_days"] == 14

    def test_put_updates_values_and_writes_env(self, app_client, monkeypatch):
        from lenny import configs as lenny_configs
        monkeypatch.setattr(lenny_configs, "LOAN_LIMIT", 10)
        monkeypatch.setattr(lenny_configs, "LOAN_DURATION_DAYS", 0)

        with self._bypass_admin(), \
             patch("lenny.core.ol_bootstrap.update_env_file") as mock_write:
            resp = app_client.put(
                "/v1/api/admin/loan/settings",
                json={"loan_limit": 3, "loan_duration_days": 7},
                headers=_admin_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["loan_limit"] == 3
        assert data["loan_duration_days"] == 7
        mock_write.assert_called_once()
        # Loan settings live in their own loan.env — never the main .env.
        target_path = mock_write.call_args[0][0]
        assert target_path.endswith("loan.env")
        assert not target_path.endswith("/.env")
        written = mock_write.call_args[0][1]
        assert written["LENNY_LOAN_LIMIT"] == "3"
        assert written["LENNY_LOAN_DURATION_DAYS"] == "7"

    def test_put_rejects_zero_limit(self, app_client):
        with self._bypass_admin():
            resp = app_client.put(
                "/v1/api/admin/loan/settings",
                json={"loan_limit": 0},
                headers=_admin_headers(),
            )
        assert resp.status_code == 400

    def test_put_rejects_negative_duration(self, app_client):
        with self._bypass_admin():
            resp = app_client.put(
                "/v1/api/admin/loan/settings",
                json={"loan_duration_days": -1},
                headers=_admin_headers(),
            )
        assert resp.status_code == 400

    def test_put_rejects_empty_body(self, app_client):
        with self._bypass_admin():
            resp = app_client.put(
                "/v1/api/admin/loan/settings",
                json={},
                headers=_admin_headers(),
            )
        assert resp.status_code == 400

    def test_get_requires_admin(self, app_client):
        resp = app_client.get("/v1/api/admin/loan/settings")
        assert resp.status_code in (401, 403)
