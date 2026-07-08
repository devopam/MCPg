"""Credential-redaction tests for the first-party SQL kernel.

Ported from the vendored ``tests/vendor/sql/test_obfuscate_password.py``,
now exercising :func:`mcpg.sql.obfuscate_password`. Behaviour must match
the vendored implementation exactly.
"""

from mcpg.sql import obfuscate_password


def test_obfuscate_none_or_empty() -> None:
    assert obfuscate_password("") == ""
    assert obfuscate_password(None) is None


def test_obfuscate_postgresql_url() -> None:
    url = "postgresql://user:secret@localhost:5432/mydatabase"
    result = obfuscate_password(url)
    assert result is not None
    assert "secret" not in result
    assert "****" in result
    assert result == "postgresql://user:****@localhost:5432/mydatabase"

    url = "postgresql://user:p@$$w0rd@localhost:5432/mydatabase"
    result = obfuscate_password(url)
    assert result is not None
    assert "p@$$w0rd" not in result
    assert "****" in result

    url = "postgresql://user:secret@localhost:5432/mydatabase?sslmode=require"
    result = obfuscate_password(url)
    assert result is not None
    assert "secret" not in result
    assert "?sslmode=require" in result


def test_obfuscate_in_error_message() -> None:
    error_msg = (
        "Failed to connect: could not connect to server: Connection refused. Is the server "
        "running on host 'localhost' (127.0.0.1) and accepting TCP/IP connections on port 5432? "
        "connection string: postgresql://admin:topsecret@localhost:5432/mydb"
    )
    obfuscated = obfuscate_password(error_msg)
    assert obfuscated is not None
    assert "topsecret" not in obfuscated
    assert "****" in obfuscated
    assert "postgresql://admin:****@localhost:5432/mydb" in obfuscated


def test_obfuscate_connection_params() -> None:
    conn_string = "host=localhost port=5432 dbname=mydb user=admin password=secret123"
    obfuscated = obfuscate_password(conn_string)
    assert obfuscated is not None
    assert "secret123" not in obfuscated
    assert "password=****" in obfuscated

    code_snippet = """conn = psycopg.connect("host=localhost dbname=mydb user=postgres password='my$3cret!'")"""
    obfuscated = obfuscate_password(code_snippet)
    assert obfuscated is not None
    assert "my$3cret!" not in obfuscated
    assert "password='****'" in obfuscated


def test_obfuscate_multiple_passwords() -> None:
    text = """
    Primary DB: postgresql://user1:password1@host1:5432/db1
    Secondary DB: postgresql://user2:password2@host2:5432/db2
    """
    obfuscated = obfuscate_password(text)
    assert obfuscated is not None
    assert "password1" not in obfuscated
    assert "password2" not in obfuscated
    assert "user1:****@" in obfuscated
    assert "user2:****@" in obfuscated


def test_obfuscate_no_sensitive_data() -> None:
    text = "This is a normal string with no passwords."
    assert obfuscate_password(text) == text

    url = "http://example.com/path"
    assert obfuscate_password(url) == url


def test_obfuscate_dsn_format() -> None:
    dsn = "host='localhost' user='postgres' password='supersecret' dbname='testdb'"
    obfuscated = obfuscate_password(dsn)
    assert obfuscated is not None
    assert "supersecret" not in obfuscated
    assert "password='****'" in obfuscated

    dsn = 'host="localhost" user="postgres" password="supersecret" dbname="testdb"'
    obfuscated = obfuscate_password(dsn)
    assert obfuscated is not None
    assert "supersecret" not in obfuscated
    assert 'password="****"' in obfuscated
