from datetime import UTC, datetime
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from michelangelo_bots.admin import app
from michelangelo_bots.config import Settings, get_settings
from michelangelo_bots.db import BotUser
from michelangelo_bots.user_import import parse_user_import, update_existing_user


def test_parse_cp1251_csv_with_russian_headers_and_duplicate_users() -> None:
    content = (
        "Мессенджер;User ID;Ник;ФИО;Телефон;Дата и время регистрации\n"
        "Telegram;123;@ivan;Иван Иванов;+79990000000;01.02.2025 12:30\n"
        "telegram;123;;Иван Петров;;31.01.2025 10:00\n"
    ).encode("cp1251")

    result = parse_user_import("users.csv", content)

    assert result.total_rows == 2
    assert result.duplicate_rows == 1
    assert not result.errors
    assert len(result.records) == 1
    user = result.records[0]
    assert user.platform == "telegram"
    assert user.platform_user_id == "123"
    assert user.chat_id == "123"
    assert user.username == "ivan"
    assert user.full_name == "Иван Петров"
    assert user.first_seen_at == datetime(2025, 1, 31, 7, tzinfo=UTC)


def test_parse_excel_csv_separator_directive_and_preserve_line_numbers() -> None:
    result = parse_user_import(
        "users.csv",
        "sep=;\nПлатформа;User ID;ФИО\ntelegram;123;Иван\n".encode(),
    )

    assert not result.errors
    assert result.records[0].row == 3
    assert result.records[0].full_name == "Иван"


def test_explicit_telegram_and_max_columns_create_two_users() -> None:
    content = (
        "Telegram ID,MAX ID,ФИО,Email\n"
        "111,222,Один клиент,TEST@EXAMPLE.COM\n"
    ).encode()

    result = parse_user_import("users.csv", content)

    assert [(record.platform, record.platform_user_id) for record in result.records] == [
        ("telegram", "111"),
        ("max", "222"),
    ]
    assert all(record.email == "test@example.com" for record in result.records)


def test_bare_id_requires_explicit_confirmation() -> None:
    content = "ID,ФИО\n123,Иван\n".encode()

    ambiguous = parse_user_import("users.csv", content, default_platform="telegram")
    assert not ambiguous.records
    assert "нет Platform User ID" in ambiguous.errors[0].message

    result = parse_user_import(
        "users.csv",
        content,
        default_platform="telegram",
        id_is_platform_user_id=True,
    )
    assert result.records[0].platform_user_id == "123"


def test_parse_xlsx_and_warn_about_unrecognized_optional_date() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Платформа", "Platform User ID", "ФИО", "Дата регистрации"])
    sheet.append(["MAX", 987654321, "Максим", "когда-то"])
    data = BytesIO()
    workbook.save(data)

    result = parse_user_import("users.xlsx", data.getvalue())

    assert not result.errors
    assert result.records[0].platform == "max"
    assert result.records[0].platform_user_id == "987654321"
    assert result.records[0].first_seen_at is None
    assert "не распознана" in result.warnings[0].message


def test_merge_mode_only_fills_empty_existing_fields() -> None:
    existing = BotUser(
        platform="telegram",
        platform_user_id="123",
        chat_id="123",
        username="current",
        phone=None,
        email=None,
        status="active",
        first_seen_at=datetime(2025, 2, 1, tzinfo=UTC),
        last_seen_at=datetime(2025, 2, 2, tzinfo=UTC),
        total_actions=5,
    )
    imported = parse_user_import(
        "users.csv",
        b"platform,user_id,username,phone\ntelegram,123,old,+79990000000\n",
    ).records[0]

    changed = update_existing_user(
        existing,
        imported,
        overwrite=False,
        apply=True,
        source_filename="users.csv",
    )

    assert changed
    assert existing.username == "current"
    assert existing.phone == "+79990000000"
    assert existing.raw_profile == {
        "legacy_import": {"filename": "users.csv", "row": 2}
    }


def test_admin_exposes_import_page_and_csv_template() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="secret",
        _env_file=None,
    )
    try:
        client = TestClient(app)
        page = client.get("/users/import", auth=("admin", "secret"))
        template = client.get("/users/import/template", auth=("admin", "secret"))
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Перенести пользователей" in page.text or "Загрузить базу" in page.text
    assert template.status_code == 200
    assert template.content.startswith(b"\xef\xbb\xbfplatform,platform_user_id")
