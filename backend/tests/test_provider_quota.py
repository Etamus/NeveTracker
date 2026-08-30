from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from services import provider_quota


class ProviderQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database_patch = patch.object(
            provider_quota,
            "QUOTA_DATABASE_PATH",
            Path(self.temporary_directory.name) / "quota.db",
        )
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.temporary_directory.cleanup()

    def test_blocks_before_crossing_free_limit(self):
        with patch.dict(
            provider_quota.PROVIDER_QUOTAS,
            {"searchapi": {"limit": 3, "period": "lifetime", "unit": "consultas"}},
        ):
            provider_quota.reserve_provider_quota("searchapi", 2)
            with self.assertRaisesRegex(
                provider_quota.ProviderQuotaExceeded,
                "chamada foi bloqueada",
            ):
                provider_quota.reserve_provider_quota("searchapi", 2)

            status = provider_quota.get_provider_quota("searchapi")
            self.assertEqual(status["used"], 2)
            self.assertEqual(status["remaining"], 1)

    def test_monthly_allowance_uses_independent_periods(self):
        january = datetime(2026, 1, 31, tzinfo=timezone.utc)
        february = datetime(2026, 2, 1, tzinfo=timezone.utc)
        provider_quota.reserve_provider_quota("bright_data", 4, january)

        self.assertEqual(
            provider_quota.get_provider_quota("bright_data", january)["used"],
            4,
        )
        self.assertEqual(
            provider_quota.get_provider_quota("bright_data", february)["used"],
            0,
        )

    def test_concurrent_reservations_cannot_cross_limit(self):
        with patch.dict(
            provider_quota.PROVIDER_QUOTAS,
            {"serper": {"limit": 7, "period": "lifetime", "unit": "consultas"}},
        ):
            def attempt_reservation(_index):
                try:
                    provider_quota.reserve_provider_quota("serper", 1)
                    return True
                except provider_quota.ProviderQuotaExceeded:
                    return False

            with ThreadPoolExecutor(max_workers=12) as executor:
                accepted = list(executor.map(attempt_reservation, range(30)))

            self.assertEqual(sum(accepted), 7)
            self.assertEqual(
                provider_quota.get_provider_quota("serper")["used"],
                7,
            )

    def test_unmetered_provider_never_creates_financial_usage(self):
        status = provider_quota.reserve_provider_quota("mercado_livre", 500)
        self.assertIsNone(status["limit"])
        self.assertIsNone(status["remaining"])
        self.assertFalse(status["hard_locked"])


if __name__ == "__main__":
    unittest.main()
