"""Optional FTPS: creates a private root CA and a server certificate.

The camera only trusts servers whose certificate chains to a root certificate
imported from the memory card. We therefore create our own small CA: its
certificate (``cacert.pem``) goes onto the card, the server uses a certificate
signed by it that lists all of this computer's IP addresses.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path
from typing import Dict, Iterable, List

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

try:
    import OpenSSL  # noqa: F401  (needed by pyftpdlib's TLS_FTPHandler)

    HAVE_PYOPENSSL = True
except ImportError:  # pragma: no cover
    HAVE_PYOPENSSL = False

TLS_AVAILABLE = HAVE_CRYPTO and HAVE_PYOPENSSL


class TlsFiles:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "tls"
        self.ca_cert = self.dir / "cacert.pem"
        self.ca_key = self.dir / "ca.key"
        self.cert = self.dir / "server.crt"
        self.key = self.dir / "server.key"

    def exists(self) -> bool:
        return all(p.is_file() for p in (self.ca_cert, self.cert, self.key))

    def info(self) -> Dict:
        out = {"available": TLS_AVAILABLE, "exists": self.exists(), "ips": [], "valid_until": None}
        if self.exists() and HAVE_CRYPTO:
            try:
                cert = x509.load_pem_x509_certificate(self.cert.read_bytes())
                san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                out["ips"] = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
                out["valid_until"] = cert.not_valid_after_utc.isoformat()
            except Exception:
                pass
        return out


def _name(cn: str) -> "x509.Name":
    return x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "a7v-tether"),
    ])


def generate(files: TlsFiles, ips: Iterable[str], years: int = 10) -> List[str]:
    """(Re)create server certificate; keep the CA if it exists (camera keeps trusting it)."""
    if not HAVE_CRYPTO:
        raise RuntimeError("Paket 'cryptography' fehlt (pip install cryptography pyopenssl)")
    files.dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    if files.ca_cert.is_file() and files.ca_key.is_file():
        ca_key = serialization.load_pem_private_key(files.ca_key.read_bytes(), None)
        ca_cert = x509.load_pem_x509_certificate(files.ca_cert.read_bytes())
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(_name("a7v-tether Root CA"))
            .issuer_name(_name("a7v-tether Root CA"))
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365 * 20))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        files.ca_key.write_bytes(ca_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        files.ca_cert.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    ip_list = sorted({ip for ip in ips if ip} | {"127.0.0.1"})
    san = [x509.DNSName(socket.gethostname() or "localhost"), x509.DNSName("localhost")]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ip_list]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(ip_list[0]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365 * years))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    files.key.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    files.cert.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
    return ip_list
