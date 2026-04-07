"""SMTP delivery helpers for transactional product emails."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from ruperto.models import StoreVertical

SMTP_SSL_PORT = 465


class SignupEmailDeliveryError(RuntimeError):
    """Raised when the signup welcome email cannot be delivered."""

    def __init__(self) -> None:
        super().__init__("Could not deliver signup email.")


def _vertical_label(vertical: StoreVertical) -> str:
    """Return the human label used in signup emails."""
    if vertical == StoreVertical.MUNICIPAL:
        return "Municipio"
    return "Local de comida"


def build_signup_welcome_email(  # noqa: PLR0913
    *,
    sender_email: str,
    recipient_email: str,
    recipient_name: str,
    store_name: str,
    store_slug: str,
    vertical: StoreVertical,
    dashboard_url: str,
    demo_chat_url: str,
) -> EmailMessage:
    """Build the owner-facing welcome email sent after a successful signup."""
    message = EmailMessage()
    message["Subject"] = f"Tu espacio {store_name} ya esta listo"
    message["From"] = sender_email
    message["To"] = recipient_email
    message.set_content(
        "\n".join(
            [
                f"Hola {recipient_name},",
                "",
                f"Tu espacio {store_name} ya quedo creado en Ruperto.",
                f"Tipo de organizacion: {_vertical_label(vertical)}",
                f"Slug publico: {store_slug}",
                "",
                f"Dashboard: {dashboard_url}",
                f"Demo chat: {demo_chat_url}",
                "",
                "Ya podes entrar con el correo y la contrasena que usaste en el signup.",
            ]
        )
    )
    return message


def send_signup_welcome_email(  # noqa: PLR0913
    *,
    smtp_server: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    recipient_email: str,
    recipient_name: str,
    store_name: str,
    store_slug: str,
    vertical: StoreVertical,
    dashboard_url: str,
    demo_chat_url: str,
) -> None:
    """Send the owner-facing welcome email through the configured SMTP account."""
    message = build_signup_welcome_email(
        sender_email=smtp_user,
        recipient_email=recipient_email,
        recipient_name=recipient_name,
        store_name=store_name,
        store_slug=store_slug,
        vertical=vertical,
        dashboard_url=dashboard_url,
        demo_chat_url=demo_chat_url,
    )
    try:
        if smtp_port == SMTP_SSL_PORT:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as client:
                client.login(smtp_user, smtp_password)
                client.send_message(message)
            return

        with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as client:
            client.ehlo()
            client.starttls()
            client.ehlo()
            client.login(smtp_user, smtp_password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise SignupEmailDeliveryError() from error
