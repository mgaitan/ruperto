"""Tests for SMTP mail helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ruperto.mail import SignupEmailDeliveryError, build_signup_welcome_email, send_signup_welcome_email
from ruperto.models import StoreVertical

EXPECTED_EHLO_CALLS = 2


def test_build_signup_welcome_email_contains_core_fields():
    """Signup emails include the key tenant details."""
    message = build_signup_welcome_email(
        sender_email="mailer@example.com",
        recipient_email="owner@example.com",
        recipient_name="Nora",
        store_name="Mi Muni",
        store_slug="mi-muni",
        vertical=StoreVertical.MUNICIPAL,
        dashboard_url="https://example.com/dashboard",
        demo_chat_url="https://example.com/demo/chat/mi-muni",
    )

    assert message["Subject"] == "Tu espacio Mi Muni ya esta listo"
    assert "Mi Muni" in message.get_content()
    assert "Municipio" in message.get_content()


def test_build_signup_welcome_email_uses_food_label_for_ordering_spaces():
    """Ordering tenants should render the food-business label in the welcome email."""
    message = build_signup_welcome_email(
        sender_email="mailer@example.com",
        recipient_email="owner@example.com",
        recipient_name="Nora",
        store_name="Lo de Nora",
        store_slug="lo-de-nora",
        vertical=StoreVertical.ORDERING,
        dashboard_url="https://example.com/dashboard",
        demo_chat_url="https://example.com/demo/chat/lo-de-nora",
    )

    assert "Local de comida" in message.get_content()


def test_send_signup_welcome_email_uses_ssl_for_port_465():
    """Port 465 uses SMTP over SSL."""
    with patch("ruperto.mail.smtplib.SMTP_SSL") as smtp_ssl:
        send_signup_welcome_email(
            smtp_server="smtp.example.com",
            smtp_port=465,
            smtp_user="mailer@example.com",
            smtp_password="smtp-secret",
            recipient_email="owner@example.com",
            recipient_name="Nora",
            store_name="Mi Muni",
            store_slug="mi-muni",
            vertical=StoreVertical.MUNICIPAL,
            dashboard_url="https://example.com/dashboard",
            demo_chat_url="https://example.com/demo/chat/mi-muni",
        )

    smtp_ssl.assert_called_once()
    smtp_ssl.return_value.__enter__.return_value.login.assert_called_once_with("mailer@example.com", "smtp-secret")


def test_send_signup_welcome_email_uses_starttls_for_non_ssl_ports():
    """Non-SSL SMTP ports should negotiate STARTTLS before sending the welcome email."""
    with patch("ruperto.mail.smtplib.SMTP") as smtp:
        send_signup_welcome_email(
            smtp_server="smtp.example.com",
            smtp_port=587,
            smtp_user="mailer@example.com",
            smtp_password="smtp-secret",
            recipient_email="owner@example.com",
            recipient_name="Nora",
            store_name="Mi Muni",
            store_slug="mi-muni",
            vertical=StoreVertical.MUNICIPAL,
            dashboard_url="https://example.com/dashboard",
            demo_chat_url="https://example.com/demo/chat/mi-muni",
        )

    smtp_client = smtp.return_value.__enter__.return_value
    assert smtp_client.ehlo.call_count == EXPECTED_EHLO_CALLS
    smtp_client.starttls.assert_called_once()
    smtp_client.login.assert_called_once_with("mailer@example.com", "smtp-secret")
    smtp_client.send_message.assert_called_once()


def test_send_signup_welcome_email_wraps_smtp_errors():
    """SMTP transport errors are exposed as domain-specific signup failures."""
    with (
        patch("ruperto.mail.smtplib.SMTP", side_effect=OSError("boom")),
        pytest.raises(SignupEmailDeliveryError),
    ):
        send_signup_welcome_email(
            smtp_server="smtp.example.com",
            smtp_port=587,
            smtp_user="mailer@example.com",
            smtp_password="smtp-secret",
            recipient_email="owner@example.com",
            recipient_name="Nora",
            store_name="Mi Muni",
            store_slug="mi-muni",
            vertical=StoreVertical.MUNICIPAL,
            dashboard_url="https://example.com/dashboard",
            demo_chat_url="https://example.com/demo/chat/mi-muni",
        )
