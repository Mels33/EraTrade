"""Константы БД-тестов (отдельно от conftest, чтобы не было циклических импортов)."""

import os

TEST_DB_URL = os.environ.get(
    "ERATRADE_TEST_DATABASE_URL",
    "postgresql+asyncpg://eratrade:eratrade_local@127.0.0.1:5435/eratrade_pytest_testnet",
)
