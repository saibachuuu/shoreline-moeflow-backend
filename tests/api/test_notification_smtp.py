"""Real loopback TLS SMTP tests. No external SMTP or real recipient is ever contacted."""

import ipaddress
import shutil
import socketserver
import ssl
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from email.parser import BytesParser
from email import policy
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tests import MoeTestCase
from app.services.notification_mail import send_notification_mail


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 localhost test SMTP\r\n")
        while True:
            line = self.rfile.readline(65536)
            if not line:
                return
            command = line.split(b" ", 1)[0].strip().upper()
            if command in (b"EHLO", b"HELO"):
                self.wfile.write(b"250 localhost\r\n")
            elif command == b"MAIL":
                self.wfile.write(b"250 OK\r\n")
            elif command == b"RCPT":
                self.wfile.write(
                    b"550 rejected\r\n"
                    if self.server.mode == "reject"
                    else b"250 OK\r\n"
                )
            elif command == b"DATA":
                self.wfile.write(b"354 End with dot\r\n")
                content = bytearray()
                while True:
                    part = self.rfile.readline(65536)
                    if part in (b".\r\n", b""):
                        break
                    content.extend(part)
                    if len(content) > 1024 * 1024:
                        return
                self.server.received.append(bytes(content))
                if self.server.mode == "disconnect":
                    return
                self.wfile.write(
                    b"451 try later\r\n"
                    if self.server.mode == "temporary"
                    else b"250 queued\r\n"
                )
            elif command == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def smtp_server(mode):
    root = Path(__file__).resolve().parents[2] / "artifacts" / "notification-smtp-tests"
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="smtp-", dir=root))
    assert folder.resolve().is_relative_to(root.resolve())
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        certfile, keyfile = folder / "cert.pem", folder / "key.pem"
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certfile, keyfile)
        client_tls = ssl.create_default_context(cafile=str(certfile))
        with Server(("127.0.0.1", 0), Handler) as server:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            server.mode, server.received = mode, []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server, client_tls
            finally:
                server.shutdown()
                thread.join(timeout=5)
    finally:
        # Only the verified, uniquely-created test directory is recursively removed.
        assert folder.resolve().is_relative_to(root.resolve())
        shutil.rmtree(folder)


class NotificationSMTPTestCase(MoeTestCase):
    def send(self, mode):
        with smtp_server(mode) as (server, context):
            self.app.config.update(
                ENABLE_USER_EMAIL=True,
                EMAIL_SMTP_HOST="127.0.0.1",
                EMAIL_SMTP_PORT=server.server_address[1],
                EMAIL_USE_SSL=True,
                EMAIL_ADDRESS="sender@example.test",
                EMAIL_USERNAME="",
                NOTIFICATION_EMAIL_ALLOWLIST=["recipient@example.test"],
            )
            with patch(
                "app.services.notification_mail.ssl.create_default_context",
                return_value=context,
            ):
                result = send_notification_mail(
                    "recipient@example.test",
                    "通知测试",
                    "plain fallback",
                    "<b>safe HTML</b>",
                    message_id="<stable-test@example.test>",
                )
            return result, server.received

    def test_tls_accepts_one_recipient_with_multipart_fallback(self):
        result, messages = self.send("accept")
        self.assertEqual(result, ("accepted", ""))
        self.assertEqual(len(messages), 1)
        msg = BytesParser(policy=policy.default).parsebytes(messages[0])
        self.assertEqual(msg["To"], "recipient@example.test")
        self.assertNotIn("Cc", msg)
        self.assertEqual(msg["Message-ID"], "<stable-test@example.test>")
        self.assertEqual(
            msg.get_body(preferencelist=("plain",)).get_content().strip(),
            "plain fallback",
        )
        self.assertIn("safe HTML", msg.get_body(preferencelist=("html",)).get_content())

    def test_permanent_recipient_rejection_is_not_transient(self):
        result, messages = self.send("reject")
        self.assertEqual(result, ("failed", "recipient_refused"))
        self.assertEqual(messages, [])

    def test_explicit_temporary_data_rejection_is_retryable(self):
        result, _ = self.send("temporary")
        self.assertEqual(result, ("retry_wait", "smtp_response"))

    def test_disconnect_after_data_is_unknown_not_definite_failure(self):
        result, messages = self.send("disconnect")
        self.assertEqual(result, ("unknown", "transport_interrupted"))
        self.assertEqual(len(messages), 1)
