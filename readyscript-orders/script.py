"""Клиент ReadyScript API.

Модуль импортируется сервисом michelangelo-readyscript-sync, который до начала
работы переопределяет настройки ниже через readyscript_sync.configure_script.
Значения по умолчанию нужны только чтобы модуль импортировался сам по себе.
"""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from typing import Any

import requests

PAGE_SIZE = 100

# Дополнительно вызывать order.get для каждого заказа.
# Это даёт более подробный объект заказа, но создаёт больше запросов.
FETCH_ORDER_DETAILS = True

# Вызывать user.get для каждого уникального user_id.
FETCH_USERS = True

# Небольшая пауза между дополнительными запросами.
REQUEST_DELAY_SECONDS = 0.05

REQUEST_TIMEOUT = 30

PLATFORM_KEYWORDS = (
    "telegram",
    "max",
    "profile",
    "platform",
    "creator",
    "messenger",
    "bot",
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
