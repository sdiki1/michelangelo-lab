from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


load_dotenv()

API_BASE = os.getenv(
    "RS_API_BASE",
    "https://michelangelo-lab.rscms.ru/api-6cdywf0i/methods",
).rstrip("/")

CLIENT_ID = os.getenv("RS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("RS_CLIENT_SECRET", "")
USERNAME = os.getenv("RS_USERNAME", "")
PASSWORD = os.getenv("RS_PASSWORD", "")

PAGE_SIZE = int(os.getenv("RS_PAGE_SIZE", "100"))
INTERVAL_SECONDS = int(os.getenv("RS_INTERVAL_SECONDS", "60"))
OUTPUT_FILE = Path(os.getenv("RS_OUTPUT_FILE", "orders_latest.json"))

# 1 — дополнительно вызывать order.get для каждого заказа.
# Это даёт более подробный объект заказа, но создаёт больше запросов.
FETCH_ORDER_DETAILS = os.getenv(
    "RS_FETCH_ORDER_DETAILS",
    "1",
).strip().lower() in {"1", "true", "yes", "on"}

# 1 — вызывать user.get для каждого уникального user_id.
FETCH_USERS = os.getenv(
    "RS_FETCH_USERS",
    "1",
).strip().lower() in {"1", "true", "yes", "on"}

# Небольшая пауза между дополнительными запросами.
REQUEST_DELAY_SECONDS = float(
    os.getenv("RS_REQUEST_DELAY_SECONDS", "0.05")
)

REQUEST_TIMEOUT = int(os.getenv("RS_REQUEST_TIMEOUT", "30"))

PLATFORM_KEYWORDS = (
    "telegram",
    "max",
    "profile",
    "platform",
    "creator",
    "messenger",
    "bot",
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("readyscript-orders")


class ReadyScriptAPIError(Exception):
    """Ошибка, возвращённая API ReadyScript."""


class ReadyScriptClient:
    def __init__(
        self,
        api_base: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
    ) -> None:
        self.api_base = api_base
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password

        self.token: str | None = None
        self.token_expire: int | None = None

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "ReadyScriptOrderSync/2.0",
            }
        )

    def close(self) -> None:
        self.session.close()

    def _parse_response(
        self,
        response: requests.Response,
    ) -> dict[str, Any]:
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError as error:
            raise ReadyScriptAPIError(
                "ReadyScript вернул не JSON. "
                f"HTTP {response.status_code}: {response.text[:500]}"
            ) from error

        if not isinstance(data, dict):
            raise ReadyScriptAPIError(
                f"Неожиданный формат ответа: {data!r}"
            )

        if "error" in data:
            api_error = data["error"]

            if isinstance(api_error, dict):
                code = api_error.get("code", "unknown")
                title = (
                    api_error.get("title")
                    or api_error.get("message")
                    or "Неизвестная ошибка"
                )
                raise ReadyScriptAPIError(
                    f"Ошибка ReadyScript API {code}: {title}"
                )

            raise ReadyScriptAPIError(
                f"Ошибка ReadyScript API: {api_error}"
            )

        response_data = data.get("response")

        if not isinstance(response_data, dict):
            raise ReadyScriptAPIError(
                "Поле response отсутствует или имеет неправильный "
                f"формат: {response_data!r}"
            )

        return response_data

    def authorize(self) -> str:
        """Получает служебный OAuth-токен ReadyScript."""

        logger.info("Авторизация в ReadyScript")

        response = self.session.post(
            f"{self.api_base}/oauth.token",
            params={
                "v": "1",
                "lang": "ru",
            },
            data={
                "grant_type": "password",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "username": self.username,
                "password": self.password,
            },
            timeout=REQUEST_TIMEOUT,
        )

        result = self._parse_response(response)
        auth = result.get("auth")

        if not isinstance(auth, dict):
            raise ReadyScriptAPIError(
                f"ReadyScript не вернул секцию auth: {result}"
            )

        token = auth.get("token")

        if not token:
            raise ReadyScriptAPIError(
                f"ReadyScript не вернул токен: {result}"
            )

        self.token = str(token)

        expire = auth.get("expire")

        try:
            self.token_expire = int(expire) if expire else None
        except (TypeError, ValueError):
            self.token_expire = None

        user = result.get("user", {})

        if isinstance(user, dict):
            logger.info(
                "Авторизация успешна. user_id=%s, имя=%s",
                user.get("id"),
                (
                    user.get("full_name")
                    or user.get("name")
                    or user.get("login")
                ),
            )
        else:
            logger.info("Авторизация успешна")

        return self.token

    def token_is_expired(self) -> bool:
        if not self.token:
            return True

        if not self.token_expire:
            return False

        return int(time.time()) >= self.token_expire - 60

    def ensure_token(self) -> str:
        if self.token_is_expired():
            return self.authorize()

        assert self.token is not None
        return self.token

    def _authorized_get(
        self,
        method: str,
        params: dict[str, Any] | list[tuple[str, Any]],
        retry_authorization: bool = True,
    ) -> dict[str, Any]:
        token = self.ensure_token()

        if isinstance(params, dict):
            request_params: dict[str, Any] | list[tuple[str, Any]] = {
                **params,
                "v": "1",
                "lang": "ru",
                "token": token,
            }
        else:
            request_params = [
                ("v", "1"),
                ("lang", "ru"),
                ("token", token),
                *params,
            ]

        try:
            response = self.session.get(
                f"{self.api_base}/{method}",
                params=request_params,
                timeout=REQUEST_TIMEOUT,
            )
            return self._parse_response(response)

        except ReadyScriptAPIError:
            if not retry_authorization:
                raise

            logger.warning(
                "Метод %s вернул ошибку. Обновляем токен и повторяем запрос",
                method,
            )

            self.token = None
            self.token_expire = None
            self.authorize()

            return self._authorized_get(
                method=method,
                params=params,
                retry_authorization=False,
            )

    def get_orders_page(self, page: int) -> dict[str, Any]:
        params = [
            ("sort", "id"),
            ("page", str(page)),
            ("pageSize", str(PAGE_SIZE)),
            ("ignore_user_group", "1"),
            ("sections[]", "users"),
            ("sections[]", "statuses"),
            ("sections[]", "address"),
        ]

        return self._authorized_get(
            method="order.getList",
            params=params,
        )

    def get_order(self, order_id: int | str) -> dict[str, Any]:
        """Получает полный ответ order.get для одного заказа."""

        return self._authorized_get(
            method="order.get",
            params={
                "order_id": str(order_id),
                "ignore_user_group": "1",
            },
        )

    def get_user(self, user_id: int | str) -> dict[str, Any]:
        """Получает полный ответ user.get для пользователя ReadyScript."""

        return self._authorized_get(
            method="user.get",
            params={
                "user_id": str(user_id),
            },
        )

    def get_all_orders(self) -> list[dict[str, Any]]:
        orders_by_id: dict[str, dict[str, Any]] = {}

        page = 1
        total = 0

        while True:
            logger.info("Получение страницы заказов №%s", page)

            result = self.get_orders_page(page)
            orders = result.get("list", [])
            summary = result.get("summary", {})

            if not isinstance(orders, list):
                raise ReadyScriptAPIError(
                    f"response.list не является списком: {orders!r}"
                )

            for order in orders:
                if not isinstance(order, dict):
                    continue

                order_id = str(order.get("id", ""))

                if order_id:
                    orders_by_id[order_id] = order

            try:
                total = int(summary.get("total", len(orders_by_id)))
            except (TypeError, ValueError, AttributeError):
                total = len(orders_by_id)

            logger.info(
                "Страница %s: получено %s заказов. Всего по API: %s",
                page,
                len(orders),
                total,
            )

            if not orders or page * PAGE_SIZE >= total:
                break

            page += 1

        def order_sort_key(order: dict[str, Any]) -> int:
            try:
                return int(order.get("id", 0))
            except (TypeError, ValueError):
                return 0

        all_orders = sorted(
            orders_by_id.values(),
            key=order_sort_key,
        )

        logger.info("Итого получено заказов: %s", len(all_orders))
        return all_orders


def find_platform_fields(
    value: Any,
    path: str = "response",
) -> list[dict[str, Any]]:
    """
    Рекурсивно находит поля, названия которых связаны с Telegram, MAX,
    профилями или платформами.

    Результат нужен в том числе для диагностики нестандартных полей
    модуля ReadyScript.
    """

    found: list[dict[str, Any]] = []

    if isinstance(value, dict):
        for key, child in value.items():
            current_path = f"{path}.{key}"
            normalized_key = str(key).lower()

            if any(
                keyword in normalized_key
                for keyword in PLATFORM_KEYWORDS
            ):
                found.append(
                    {
                        "path": current_path,
                        "value": child,
                    }
                )

            found.extend(
                find_platform_fields(
                    child,
                    current_path,
                )
            )

    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                find_platform_fields(
                    child,
                    f"{path}[{index}]",
                )
            )

    return found


def extract_user_object(
    user_response: dict[str, Any] | None,
    user_id: str,
) -> dict[str, Any] | None:
    """
    Нормализует разные возможные форматы ответа user.get.

    Скрипт всё равно сохраняет полный ответ в user_api_response,
    поэтому нестандартные поля не потеряются.
    """

    if not isinstance(user_response, dict):
        return None

    direct_user = user_response.get("user")

    if isinstance(direct_user, dict):
        # Иногда user может быть непосредственно объектом пользователя.
        if str(direct_user.get("id", "")) == user_id:
            return direct_user

        # Иногда user может быть словарём вида {"7": {...}}.
        nested = direct_user.get(user_id)

        if isinstance(nested, dict):
            return nested

    users = user_response.get("users")

    if isinstance(users, dict):
        nested = users.get(user_id)

        if isinstance(nested, dict):
            return nested

    if str(user_response.get("id", "")) == user_id:
        return user_response

    return None


def enrich_orders(
    client: ReadyScriptClient,
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Добавляет к каждому заказу:
      - полный ответ order.get;
      - данные user.get;
      - автоматически найденные platform/telegram/max/profile-поля.

    user.get вызывается только один раз для каждого уникального user_id.
    """

    enriched_orders: list[dict[str, Any]] = []
    users_cache: dict[str, dict[str, Any] | None] = {}

    for index, order_from_list in enumerate(orders, start=1):
        order_id = str(order_from_list.get("id", ""))
        user_id = str(order_from_list.get("user_id", "") or "")

        logger.info(
            "Обогащение заказа %s/%s: id=%s, user_id=%s",
            index,
            len(orders),
            order_id,
            user_id or "нет",
        )

        record: dict[str, Any] = deepcopy(order_from_list)
        order_api_response: dict[str, Any] | None = None
        user_api_response: dict[str, Any] | None = None

        if FETCH_ORDER_DETAILS and order_id:
            try:
                order_api_response = client.get_order(order_id)

                detailed_order = order_api_response.get("order")

                if isinstance(detailed_order, dict):
                    # Полный объект заказа становится основой результата.
                    record = {
                        **record,
                        **detailed_order,
                    }

                    # user_id мог присутствовать только в подробном объекте.
                    user_id = str(
                        record.get("user_id", "")
                        or user_id
                    )

                record["order_api_response"] = order_api_response

            except (
                requests.RequestException,
                ReadyScriptAPIError,
            ) as error:
                logger.error(
                    "Не удалось получить order.get для заказа %s: %s",
                    order_id,
                    error,
                )
                record["order_api_error"] = str(error)

            if REQUEST_DELAY_SECONDS > 0:
                time.sleep(REQUEST_DELAY_SECONDS)

        if FETCH_USERS and user_id and user_id != "0":
            if user_id not in users_cache:
                try:
                    users_cache[user_id] = client.get_user(user_id)
                except (
                    requests.RequestException,
                    ReadyScriptAPIError,
                ) as error:
                    logger.error(
                        "Не удалось получить user.get для user_id=%s: %s",
                        user_id,
                        error,
                    )
                    users_cache[user_id] = {
                        "_error": str(error),
                    }

                if REQUEST_DELAY_SECONDS > 0:
                    time.sleep(REQUEST_DELAY_SECONDS)

            user_api_response = users_cache[user_id]
            record["user_api_response"] = user_api_response

            user_object = extract_user_object(
                user_response=user_api_response,
                user_id=user_id,
            )

            if user_object is not None:
                record["readyscript_user"] = user_object

        platform_sources: dict[str, Any] = {
            "order": record,
        }

        if order_api_response is not None:
            platform_sources["order_api_response"] = order_api_response

        if user_api_response is not None:
            platform_sources["user_api_response"] = user_api_response

        platform_data = find_platform_fields(
            platform_sources,
            path="data",
        )

        # Убираем точные дубликаты найденных пар path/value.
        unique_platform_data: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in platform_data:
            serialized = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )

            if serialized in seen:
                continue

            seen.add(serialized)
            unique_platform_data.append(item)

        record["platform_data"] = unique_platform_data
        enriched_orders.append(record)

    return enriched_orders


def validate_settings() -> None:
    missing_variables = []

    required_variables = {
        "RS_CLIENT_ID": CLIENT_ID,
        "RS_CLIENT_SECRET": CLIENT_SECRET,
        "RS_USERNAME": USERNAME,
        "RS_PASSWORD": PASSWORD,
    }

    for variable_name, value in required_variables.items():
        if not value:
            missing_variables.append(variable_name)

    if missing_variables:
        raise RuntimeError(
            "Не заполнены переменные в .env: "
            + ", ".join(missing_variables)
        )

    if PAGE_SIZE < 1:
        raise RuntimeError("RS_PAGE_SIZE должен быть больше нуля")

    if INTERVAL_SECONDS < 1:
        raise RuntimeError(
            "RS_INTERVAL_SECONDS должен быть больше нуля"
        )

    if REQUEST_TIMEOUT < 1:
        raise RuntimeError(
            "RS_REQUEST_TIMEOUT должен быть больше нуля"
        )

    if REQUEST_DELAY_SECONDS < 0:
        raise RuntimeError(
            "RS_REQUEST_DELAY_SECONDS не может быть отрицательным"
        )


def save_orders(orders: list[dict[str, Any]]) -> None:
    result = {
        "updated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S%z",
            time.localtime(),
        ),
        "count": len(orders),
        "orders": orders,
    }

    temporary_file = OUTPUT_FILE.with_suffix(
        OUTPUT_FILE.suffix + ".tmp"
    )

    temporary_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_file.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(OUTPUT_FILE)

    logger.info(
        "Заказы сохранены в %s",
        OUTPUT_FILE.resolve(),
    )


def print_orders_summary(
    orders: list[dict[str, Any]],
) -> None:
    paid_count = sum(
        1
        for order in orders
        if str(order.get("is_payed", "0")) == "1"
    )

    telegram_count = sum(
        1
        for order in orders
        if str(
            order.get("creator_platform_id", "")
        ).lower() == "telegram-web-app"
    )

    max_count = sum(
        1
        for order in orders
        if "max" in str(
            order.get("creator_platform_id", "")
        ).lower()
    )

    with_found_platform_data = sum(
        1
        for order in orders
        if order.get("platform_data")
    )

    logger.info(
        (
            "Всего заказов: %s; оплаченных: %s; "
            "Telegram: %s; MAX: %s; "
            "с найденными platform-полями: %s"
        ),
        len(orders),
        paid_count,
        telegram_count,
        max_count,
        with_found_platform_data,
    )

    for order in orders[-10:]:
        user = order.get("readyscript_user")

        if not isinstance(user, dict):
            user = {}

        logger.info(
            (
                "Заказ id=%s, номер=%s, user_id=%s, "
                "платформа=%s, пользователь=%s, сумма=%s %s, "
                "оплачен=%s"
            ),
            order.get("id"),
            order.get("order_num"),
            order.get("user_id"),
            order.get("creator_platform_id"),
            (
                user.get("full_name")
                or " ".join(
                    filter(
                        None,
                        [
                            user.get("surname"),
                            user.get("name"),
                            user.get("midname"),
                        ],
                    )
                )
                or user.get("login")
                or user.get("e_mail")
                or "не найден"
            ),
            order.get("totalcost"),
            order.get("currency"),
            order.get("is_payed"),
        )


def run_sync_loop() -> None:
    validate_settings()

    client = ReadyScriptClient(
        api_base=API_BASE,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        username=USERNAME,
        password=PASSWORD,
    )

    logger.info(
        (
            "Синхронизация запущена. Интервал: %s сек.; "
            "order.get=%s; user.get=%s"
        ),
        INTERVAL_SECONDS,
        FETCH_ORDER_DETAILS,
        FETCH_USERS,
    )

    try:
        while True:
            iteration_started = time.monotonic()

            try:
                orders = client.get_all_orders()
                enriched_orders = enrich_orders(
                    client=client,
                    orders=orders,
                )

                save_orders(enriched_orders)
                print_orders_summary(enriched_orders)

            except requests.RequestException:
                logger.exception(
                    "Сетевая ошибка при обращении к ReadyScript"
                )

            except ReadyScriptAPIError:
                logger.exception(
                    "ReadyScript вернул ошибку"
                )

            except Exception:
                logger.exception(
                    "Непредвиденная ошибка синхронизации"
                )

            iteration_duration = (
                time.monotonic() - iteration_started
            )

            sleep_seconds = max(
                1,
                INTERVAL_SECONDS - iteration_duration,
            )

            logger.info(
                "Следующая проверка через %.1f сек.",
                sleep_seconds,
            )

            time.sleep(sleep_seconds)

    except KeyboardInterrupt:
        logger.info("Синхронизация остановлена пользователем")

    finally:
        client.close()


if __name__ == "__main__":
    run_sync_loop()
