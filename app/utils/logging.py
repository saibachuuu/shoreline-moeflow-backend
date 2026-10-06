# -*- coding: utf-8 -*-
"""
from app.utils.logging import logger
使用 logger 记录即可
"""

import os
import logging
import threading
import time
from collections import deque
from logging.handlers import SMTPHandler
from flask import Flask
from typing import Optional

logger = logging.getLogger(__name__)
root_logger = logging.getLogger("root")


class SMTPSSLHandler(SMTPHandler):
    """使用SMTP_SSL以支持SSL"""

    def emit(self, record):
        """
        Emit a record.

        Format the record and send it to the specified addressees.
        """
        if hasattr(self, "notification_config"):
            # Independent of MongoDB/Celery. No recursive logging on mail failure.
            from app.services.notification_mail import send_protected_mail
            from email.utils import make_msgid

            with self.notification_lock:
                now = time.monotonic()
                while self.notification_times and self.notification_times[0] < now - 60:
                    self.notification_times.popleft()
                if len(self.notification_times) >= 5:
                    return
                self.notification_times.append(now)
            try:
                for address in self.toaddrs:
                    send_protected_mail(
                        self.notification_config,
                        address,
                        self.getSubject(record),
                        self.format(record),
                        message_id=make_msgid(),
                        user_mail=False,
                    )
            except Exception:
                # Never re-emit the triggering record (which may contain private diagnostics).
                pass
            return
        try:
            import smtplib
            from email.message import EmailMessage
            import email.utils

            port = self.mailport
            if not port:
                port = smtplib.SMTP_PORT
            smtp = smtplib.SMTP_SSL(self.mailhost, port, timeout=self.timeout)
            msg = EmailMessage()
            msg["From"] = self.fromaddr
            msg["To"] = ",".join(self.toaddrs)
            msg["Subject"] = self.getSubject(record)
            msg["Date"] = email.utils.localtime()
            msg.set_content(self.format(record))
            if self.username:
                if self.secure is not None:
                    smtp.ehlo()
                    smtp.starttls(*self.secure)
                    smtp.ehlo()
                smtp.login(self.username, self.password)
            smtp.send_message(msg)
            smtp.quit()
        except Exception:
            self.handleError(record)


_logger_configured = False


def configure_root_logger(override: Optional[str] = None):
    global _logger_configured
    if _logger_configured:
        raise AssertionError("configure_root_logger already executed")
    _logger_configured = True
    logging.debug(
        "configuring root logger %s %s",
        root_logger.level,
        root_logger.getEffectiveLevel(),
    )
    level = override or os.environ.get("LOG_LEVEL")
    if not level:
        return
    logging.basicConfig(
        format="[%(asctime)s] %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,  # why the f is this required?
        level=getattr(logging, level.upper()),
    )
    logging.debug("reset log level %s", level)


def configure_extra_logs(app: Flask):
    if app.config.get("ENABLE_LOG_EMAIL"):
        _enable_email_error_log(app)
    if app.config.get("LOG_PATH"):
        _enable_file_log(app)


def _enable_file_log(app: Flask):
    file_formatter = logging.Formatter(
        "[%(asctime)s %(pathname)s:%(lineno)d] (%(levelname)s) %(message)s"
    )
    log_path = app.config.get("LOG_PATH")
    log_folder = os.path.dirname(log_path)
    if not os.path.isdir(log_folder):
        os.makedirs(log_folder)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)


def _enable_email_error_log(app: Flask):
    # === 邮件输出 ===

    mail_formatter = logging.Formatter(
        """
        Message type:       %(levelname)s
        Location:           %(pathname)s:%(lineno)d
        Module:             %(module)s
        Function:           %(funcName)s
        Time:               %(asctime)s

        Message:

        %(message)s
        """
    )
    mail_handler = SMTPSSLHandler(
        (app.config["EMAIL_SMTP_HOST"], app.config["EMAIL_SMTP_PORT"]),
        app.config["EMAIL_ADDRESS"],
        app.config["EMAIL_ERROR_ADDRESS"],
        "萌翻站点发生错误",
        credentials=(
            app.config["EMAIL_ADDRESS"],
            app.config["EMAIL_PASSWORD"],
        ),
    )
    if app.config.get("ENABLE_NOTIFICATIONS"):
        mail_handler.notification_config = app.config
        mail_handler.notification_lock = threading.Lock()
        mail_handler.notification_times = deque()
    mail_handler.setLevel(logging.ERROR)
    mail_handler.setFormatter(mail_formatter)
    logger.addHandler(mail_handler)
    app.logger.addHandler(mail_handler)
