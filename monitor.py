#!/usr/bin/env python3
"""Monitor simples de vagas no GetYourGuide via label da data na página."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gyg-monitor")

STATE_FILE = Path(os.getenv("STATE_FILE", "data/state.json"))
DEBUG_DIR = STATE_FILE.parent / "debug"
PAGE_LOAD_RETRIES = int(os.getenv("PAGE_LOAD_RETRIES", "3"))
SELECTOR_TIMEOUT_MS = int(os.getenv("SELECTOR_TIMEOUT_MS", "90000"))

MONTHS_PT = {
    1: "jan.",
    2: "fev.",
    3: "mar.",
    4: "abr.",
    5: "mai.",
    6: "jun.",
    7: "jul.",
    8: "ago.",
    9: "set.",
    10: "out.",
    11: "nov.",
    12: "dez.",
}


CheckMode = Literal["day", "time"]


@dataclass
class Config:
    url: str
    target_date: str
    expected_label: str
    participants: int
    lang: str
    check_mode: CheckMode
    preferred_time: str | None
    check_interval: int
    telegram_bot_token: str
    telegram_chat_ids: list[str]
    telegram_bot_username: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        url = os.getenv("GYG_URL", "").strip()
        if not url:
            raise ValueError("GYG_URL é obrigatório")

        target_date = normalize_date(os.getenv("GYG_TARGET_DATE", "").strip())
        if not target_date:
            raise ValueError("GYG_TARGET_DATE é obrigatório (ex: 18/07/2026)")

        expected_label = os.getenv("GYG_EXPECTED_DATE_LABEL", "").strip()
        if not expected_label:
            expected_label = build_expected_label(target_date)

        participants = int(os.getenv("GYG_PARTICIPANTS", "1"))
        lang = os.getenv("GYG_LANG", "pt").strip() or "pt"
        check_mode = parse_check_mode(os.getenv("GYG_CHECK_MODE", "day"))
        preferred_time = normalize_time(os.getenv("GYG_PREFERRED_TIME", "").strip()) or None
        if check_mode == "time" and not preferred_time:
            raise ValueError(
                "GYG_CHECK_MODE=time exige GYG_PREFERRED_TIME (ex: 14:30)"
            )
        check_interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_ids = collect_chat_ids()
        if not bot_token or not chat_ids:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN e pelo menos um TELEGRAM_CHANNEL_ID são obrigatórios"
            )

        return cls(
            url=url,
            target_date=target_date,
            expected_label=expected_label,
            participants=participants,
            lang=lang,
            check_mode=check_mode,
            preferred_time=preferred_time,
            check_interval=check_interval,
            telegram_bot_token=bot_token,
            telegram_chat_ids=chat_ids,
        )


def normalize_date(value: str) -> str:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Data inválida: {value}. Use DD/MM/YYYY ou YYYY-MM-DD")


def parse_check_mode(value: str) -> CheckMode:
    mode = value.strip().lower()
    aliases = {
        "day": "day",
        "dia": "day",
        "date": "day",
        "data": "day",
        "time": "time",
        "horario": "time",
        "horário": "time",
        "hour": "time",
    }
    parsed = aliases.get(mode)
    if not parsed:
        raise ValueError(
            "GYG_CHECK_MODE inválido. Use 'day' (somente dia) ou 'time' (dia + horário)."
        )
    return parsed  # type: ignore[return-value]


def normalize_time(value: str) -> str | None:
    if not value:
        return None
    value = value.strip().replace(".", ":")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        raise ValueError(f"Horário inválido: {value}. Use HH:MM (ex: 14:30)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"Horário inválido: {value}")
    return f"{hour:02d}:{minute:02d}"


def build_expected_label(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    month = MONTHS_PT[dt.month]
    return f"{dt.day} de {month} de {dt.year}"


def normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def build_check_url(
    base_url: str,
    target_date: str,
    participants: int,
    lang: str = "pt",
) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["date_from"] = [target_date]
    query["lang"] = [lang]
    query["_pc"] = [str(participants)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"last_available": False, "last_preferred_available": False}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_available": False, "last_preferred_available": False}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_chat_ids() -> list[str]:
    chat_ids: list[str] = []
    for key in (
        "TELEGRAM_CHANNEL_ID",
        "TELEGRAM_CHANNEL_NAME",
        "DRAMAFLEX_CHANNEL",
        "TELEGRAM_CHAT_ID",
    ):
        value = os.getenv(key, "").strip()
        if not value:
            continue
        for part in value.split(","):
            part = part.strip()
            if part and part not in chat_ids:
                chat_ids.append(part)
    return chat_ids


def telegram_api(bot_token: str, method: str, **kwargs: Any) -> dict[str, Any]:
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/{method}",
        json=kwargs,
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram API resposta inválida: {response.text}") from exc

    if not response.ok or not body.get("ok"):
        description = body.get("description", response.text)
        raise RuntimeError(f"Telegram API error: {description}")
    return body


def verify_telegram(config: Config) -> None:
    me = telegram_api(config.telegram_bot_token, "getMe")
    username = me["result"]["username"]
    config.telegram_bot_username = username
    log.info("Bot conectado: @%s", username)

    accessible: list[str] = []
    for chat_id in config.telegram_chat_ids:
        try:
            chat = telegram_api(
                config.telegram_bot_token,
                "getChat",
                chat_id=chat_id,
            )["result"]
            title = chat.get("title") or chat.get("username") or chat_id
            log.info("Canal acessível: %s (%s)", title, chat_id)
            accessible.append(chat_id)
        except RuntimeError as exc:
            log.warning("Canal inacessível %s: %s", chat_id, exc)

    if accessible:
        config.telegram_chat_ids = accessible
        return

    log.error(
        "Nenhum canal acessível pelo bot @%s. Para corrigir:\n"
        "  1. Abra o canal no Telegram\n"
        "  2. Gerenciar canal → Administradores → Adicionar administrador\n"
        "  3. Busque @%s e conceda permissão 'Postar mensagens'\n"
        "  4. Confirme o TELEGRAM_CHANNEL_ID (ex: -1002402375685)\n"
        "  5. Reinicie: docker compose up -d --build",
        username,
        username,
    )
    raise RuntimeError(
        f"Bot @{username} não tem acesso a nenhum canal configurado. "
        "Adicione o bot como administrador do canal."
    )


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(config: Config, message: str) -> None:
    errors: list[str] = []
    for chat_id in config.telegram_chat_ids:
        try:
            telegram_api(
                config.telegram_bot_token,
                "sendMessage",
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
            log.info("Notificação enviada ao Telegram (chat_id=%s)", chat_id)
            return
        except RuntimeError as exc:
            errors.append(f"{chat_id}: {exc}")
            log.warning("Falha ao enviar para %s: %s", chat_id, exc)

    bot = config.telegram_bot_username or "seu_bot"
    raise RuntimeError(
        "Não foi possível enviar para nenhum canal. "
        f"Erros: {' | '.join(errors)}. "
        f"Adicione @{bot} como administrador do canal com permissão 'Postar mensagens'."
    )


def accept_cookies(page) -> None:
    selectors = [
        "button:has-text('Accept all')",
        "button:has-text('Aceitar todos')",
        "button:has-text('Accept')",
        "button:has-text('Aceitar')",
        "[data-testid='uc-accept-all-button']",
        "#uc-btn-accept-banner",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=2500):
                button.click(timeout=3000)
                page.wait_for_timeout(1000)
                log.info("Banner de cookies aceito")
                return
        except Exception:
            continue


def is_blocked_page(page) -> bool:
    title = page.title().lower()
    snippet = page.content()[:8000].lower()
    markers = (
        "getyourguide – error",
        "getyourguide - error",
        "access denied",
        "captcha",
        "challenge-platform",
        "cf-browser-verification",
        "unsupported",
        "enable javascript",
        "noscript",
    )
    return any(marker in title or marker in snippet for marker in markers)


def log_page_diagnostics(page, label: str) -> None:
    try:
        title = page.title()
        url = page.url
        log.warning("Diagnóstico [%s] título=%r url=%s", label, title, url)
        if is_blocked_page(page):
            log.warning(
                "Diagnóstico [%s]: página de bloqueio/erro detectada "
                "(comum em VPS com IP de datacenter)",
                label,
            )
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        screenshot = DEBUG_DIR / f"{label}-{stamp}.png"
        html_path = DEBUG_DIR / f"{label}-{stamp}.html"
        page.screenshot(path=str(screenshot), full_page=True)
        html_path.write_text(page.content(), encoding="utf-8")
        log.warning("Diagnóstico salvo em %s e %s", screenshot, html_path)
    except Exception as exc:
        log.warning("Não foi possível salvar diagnóstico: %s", exc)


def create_browser_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    context = browser.new_context(
        locale="pt-BR",
        timezone_id="Europe/Zurich",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        extra_http_headers={
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return browser, context


def load_activity_page(page, url: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, PAGE_LOAD_RETRIES + 1):
        try:
            log.info("Carregando página (tentativa %s/%s)", attempt, PAGE_LOAD_RETRIES)
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            accept_cookies(page)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                page.wait_for_timeout(5000)
            if is_blocked_page(page):
                raise RuntimeError("GetYourGuide retornou página de erro ou bloqueio")
            page.wait_for_selector("span.input-title", timeout=SELECTOR_TIMEOUT_MS)
            return
        except Exception as exc:
            last_error = exc
            log.warning("Falha ao carregar página: %s", exc)
            log_page_diagnostics(page, f"load-{attempt}")
            if attempt < PAGE_LOAD_RETRIES:
                page.wait_for_timeout(5000)
    raise RuntimeError(
        f"Não foi possível carregar a página após {PAGE_LOAD_RETRIES} tentativas"
    ) from last_error


def read_input_titles(page) -> list[str]:
    selectors = [
        "span.input-title",
        ".booking-assistant span.input-title",
        "[class*='booking-assistant'] span.input-title",
    ]
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=SELECTOR_TIMEOUT_MS)
            titles = [
                text.strip()
                for text in page.locator(selector).all_inner_texts()
                if text.strip()
            ]
            if titles:
                return titles
        except Exception:
            continue
    log_page_diagnostics(page, "no-input-title")
    raise RuntimeError(
        "Elemento span.input-title não encontrado. "
        "Verifique data/debug/ na VPS (screenshot e HTML)."
    )


def is_slot_available(titles: list[str], expected_label: str) -> bool:
    expected = normalize_label(expected_label)
    return any(normalize_label(title) == expected for title in titles)


def parse_time_from_text(text: str) -> str | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), match.group(2)
    if hour > 23:
        return None
    return f"{hour:02d}:{minute}"


def click_check_availability(page) -> bool:
    selectors = [
        ".js-check-availability",
        "button:has-text('Verificar disponibilidade')",
        "button:has-text('Ver disponibilidade')",
        "button:has-text('Check availability')",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=3000):
                button.click(timeout=5000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def wait_for_time_slots(page) -> None:
    selectors = [
        "#activity-starting-time option",
        ".starting-time-chip-wrapper",
        '[class*="starting-times"] button',
    ]
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=15000)
            return
        except Exception:
            continue
    log.warning("Horários não carregaram após verificar disponibilidade")


def read_available_times(page) -> list[str]:
    if not click_check_availability(page):
        log.warning("Botão 'Verificar disponibilidade' não encontrado")

    wait_for_time_slots(page)
    page.wait_for_timeout(2000)

    times: set[str] = set()

    option_selectors = [
        "#activity-starting-time option",
        "select[name='activity-starting-time'] option",
    ]
    for selector in option_selectors:
        try:
            for text in page.locator(selector).all_inner_texts():
                parsed = parse_time_from_text(text)
                if parsed:
                    times.add(parsed)
        except Exception:
            continue

    chip_selectors = [
        ".starting-time-chip-wrapper button",
        ".starting-time-chip-wrapper [role='button']",
        '[class*="starting-times"] button',
        '[class*="starting-time"] button',
    ]
    for selector in chip_selectors:
        try:
            for text in page.locator(selector).all_inner_texts():
                parsed = parse_time_from_text(text)
                if parsed and is_reasonable_slot_time(parsed):
                    times.add(parsed)
        except Exception:
            continue

    if times:
        log.info("Horários extraídos: %s", ", ".join(sorted(times)))
    else:
        log.warning("Nenhum horário encontrado na página")

    return sorted(times)


def is_reasonable_slot_time(value: str) -> bool:
    hour = int(value.split(":")[0])
    return 6 <= hour <= 23


def is_preferred_time_available(
    preferred_time: str | None,
    available_times: list[str],
) -> bool | None:
    if not preferred_time:
        return None
    if not available_times:
        return None
    return preferred_time in available_times


def build_message(
    config: Config,
    check_url: str,
    available_times: list[str],
    preferred_time_available: bool | None,
) -> str:
    safe_url = escape_html(check_url)
    safe_label = escape_html(config.expected_label)
    lines = [
        "🎟️ <b>Vaga encontrada no GetYourGuide!</b>",
        "",
        f"📅 <b>Data:</b> {safe_label}",
        f"👥 <b>Participantes:</b> {config.participants}",
        "",
    ]

    if config.check_mode == "time" and config.preferred_time:
        safe_preferred = escape_html(config.preferred_time)
        if preferred_time_available is True:
            lines.append(
                f"✅ Encontrou o dia <b>no horário planejado</b> ({safe_preferred})."
            )
        elif preferred_time_available is False:
            lines.append(
                f"⚠️ Encontrou o dia, mas <b>não no horário planejado</b> ({safe_preferred})."
            )
            if available_times:
                lines.append("")
                lines.append("<b>Horários disponíveis:</b>")
                for slot in available_times:
                    lines.append(f"• {escape_html(slot)}")
        else:
            lines.append(
                f"⚠️ Encontrou o dia, mas <b>não foi possível confirmar</b> "
                f"o horário planejado ({safe_preferred})."
            )
    elif config.check_mode == "day":
        lines.append("A data apareceu no seletor da página — há indício de vaga.")
        if available_times:
            lines.append("")
            lines.append("<b>Horários disponíveis:</b>")
            for slot in available_times:
                lines.append(f"• {escape_html(slot)}")
    else:
        lines.append("A data apareceu no seletor da página — há indício de vaga.")

    lines.extend(["", f'🔗 <a href="{safe_url}">Reservar agora</a>'])
    return "\n".join(lines)


def check_once(config: Config) -> None:
    check_url = build_check_url(
        config.url,
        config.target_date,
        config.participants,
        config.lang,
    )
    log.info("Verificando: %s", check_url)
    log.info("Label esperado: %s", config.expected_label)

    with sync_playwright() as playwright:
        browser, context = create_browser_context(playwright)
        page = context.new_page()
        titles: list[str] = []
        available_times: list[str] = []
        preferred_time_available: bool | None = None
        try:
            load_activity_page(page, check_url)
            titles = read_input_titles(page)
            if is_slot_available(titles, config.expected_label):
                available_times = read_available_times(page)
                if config.check_mode == "time":
                    preferred_time_available = is_preferred_time_available(
                        config.preferred_time,
                        available_times,
                    )
        finally:
            browser.close()

    log.info("Labels encontrados: %s", titles)
    available = is_slot_available(titles, config.expected_label)

    if available:
        log.info("Vaga detectada para %s", config.expected_label)
        if available_times:
            log.info("Horários disponíveis: %s", ", ".join(available_times))
        if config.check_mode == "time" and config.preferred_time:
            if preferred_time_available is True:
                log.info("Horário planejado %s disponível", config.preferred_time)
            elif preferred_time_available is False:
                log.info(
                    "Horário planejado %s indisponível",
                    config.preferred_time,
                )
            else:
                log.info(
                    "Horários não listados — horário planejado %s não confirmado",
                    config.preferred_time,
                )
    else:
        log.info("Sem vaga para %s", config.expected_label)

    state = load_state()
    if available:
        send_telegram(
            config,
            build_message(config, check_url, available_times, preferred_time_available),
        )
        log.info("Alerta enviado ao Telegram")

    state["last_check"] = datetime.now().isoformat()
    state["last_available"] = available
    state["last_preferred_available"] = preferred_time_available is True
    state["last_titles"] = titles
    state["last_times"] = available_times
    save_state(state)


def main() -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)

    log.info("Monitor GetYourGuide iniciado")
    log.info(
        "Modo: %s (%s)",
        config.check_mode,
        "somente dia" if config.check_mode == "day" else f"horário {config.preferred_time}",
    )
    log.info("Alertas: repetidos a cada verificação enquanto houver vaga")
    log.info("Intervalo: %s segundos", config.check_interval)

    try:
        verify_telegram(config)
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)

    while True:
        try:
            check_once(config)
        except Exception as exc:
            log.exception("Erro durante verificação: %s", exc)

        log.info("Próxima verificação em %s segundos", config.check_interval)
        time.sleep(config.check_interval)


if __name__ == "__main__":
    main()
