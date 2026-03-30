"""
email_sender/sender.py
Gửi email HTML qua SMTP (Gmail, Outlook, v.v.)
"""

import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


class EmailSender:
    """Gửi email HTML có template Jinja2 qua SMTP."""

    def __init__(
        self,
        host: str,
        port: int,
        sender_email: str,
        password: str,
        sender_name: str = "",
        use_tls: bool = True,
        delay_between_emails: float = 5.0,
        daily_limit: int = 100,
    ):
        self.host = host
        self.port = port
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.password = password
        self.use_tls = use_tls
        self.delay_between_emails = delay_between_emails
        self.daily_limit = daily_limit

        self._sent_today = 0
        self._jinja = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )

    def _render_template(self, template_file: str, variables: dict) -> str:
        """Render file template Jinja2."""
        try:
            tpl = self._jinja.get_template(template_file)
            return tpl.render(**variables)
        except Exception as e:
            log.error("Không render được template '%s': %s", template_file, e)
            # Fallback plaintext
            return (
                f"Hello,\n\n"
                f"We would love to connect with {variables.get('company_name', 'your company')}.\n\n"
                f"Best regards,\n{variables.get('sender_name', '')}"
            )

    def send(
        self,
        to_email: str,
        subject: str,
        template_file: str,
        template_vars: dict,
    ) -> bool:
        """
        Gửi 1 email. Trả về True nếu thành công.
        Tự động chờ delay_between_emails giây sau khi gửi.
        """
        if self._sent_today >= self.daily_limit:
            log.warning(
                "Đã đạt giới hạn %d email/ngày. Dừng gửi.", self.daily_limit
            )
            return False

        html_body = self._render_template(template_file, template_vars)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject.format(**template_vars)
        msg["From"] = (
            f"{self.sender_name} <{self.sender_email}>"
            if self.sender_name
            else self.sender_email
        )
        msg["To"] = to_email
        msg["X-Mailer"] = "Katana-Outreach-Bot"

        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            if self.use_tls:
                server = smtplib.SMTP(self.host, self.port, timeout=30)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=30)

            server.login(self.sender_email, self.password)
            server.sendmail(self.sender_email, to_email, msg.as_string())
            server.quit()

            self._sent_today += 1
            log.info(
                "Đã gửi (%d/%d): %s → %s",
                self._sent_today, self.daily_limit,
                self.sender_email, to_email,
            )
            time.sleep(self.delay_between_emails)
            return True

        except smtplib.SMTPAuthenticationError:
            log.error(
                "SMTP xác thực thất bại. Kiểm tra email/password (Gmail: dùng App Password)."
            )
            return False
        except smtplib.SMTPRecipientsRefused:
            log.warning("Email từ chối: %s", to_email)
            return False
        except Exception as e:
            log.error("Lỗi gửi email tới %s: %s", to_email, e)
            return False

    def reset_daily_counter(self) -> None:
        """Reset counter hàng ngày."""
        self._sent_today = 0
