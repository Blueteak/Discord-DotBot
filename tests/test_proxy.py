import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientError
from discord.http import Route

from dotbot.cli import main, parser
from dotbot.proxy import from_environment
from dotbot.relay import Relay, delivery_error


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_opt_in_and_validation(self):
        for command in ("run", "serve"):
            self.assertFalse(parser().parse_args([command]).proxy_from_env)
            self.assertTrue(parser().parse_args([command, "--proxy-from-env"]).proxy_from_env)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:8080"}, clear=True):
            direct = Relay({}, None)
            self.assertIsNone(direct.http.proxy)
            self.assertIsNone(direct.http.proxy_auth)
        for value in ("", "https://private:secret@proxy.invalid", "socks5://proxy.invalid",
                      "http://private:secret@proxy.invalid:bad", "http://proxy.invalid/path"):
            with self.subTest(value=value), patch.dict(os.environ, {"HTTPS_PROXY": value}, clear=True):
                with self.assertRaises(ValueError) as caught:
                    from_environment()
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_precedence_and_credentials(self):
        with patch.dict(os.environ, {"https_proxy": "http://lower.invalid",
                                    "HTTPS_PROXY": "http://test-user:p%40ss@proxy.invalid:8080"}, clear=True):
            options = from_environment()
            self.assertEqual(options["proxy"], "http://proxy.invalid:8080")
            self.assertEqual(options["proxy_auth"].login, "test-user")
            self.assertEqual(options["proxy_auth"].password, "p@ss")
            relay = Relay({}, None, proxy_from_env=True)
            error = delivery_error(RuntimeError("test-user p@ss " + options["proxy_auth"].encode()),
                                   {}, secrets=relay.proxy_secrets)
            for secret in relay.proxy_secrets:
                self.assertNotIn(secret, error)
        with patch.dict(os.environ, {"https_proxy": "http://[::1]:8080"}, clear=True):
            self.assertEqual(from_environment()["proxy"], "http://[::1]:8080")

    def test_empty_uppercase_falls_back(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "", "https_proxy": "http://lower.invalid"}, clear=True):
            self.assertEqual(from_environment()["proxy"], "http://lower.invalid")

    def test_startup_errors_never_echo_credentials(self):
        secret = "synthetic-proxy-credential"
        exceptions = [OSError(secret), ValueError(secret), RuntimeError(secret),
                      ClientError(secret), FileNotFoundError(2, secret, "/fake/" + secret)]
        for command in ("run", "serve"):
            for exception in exceptions:
                with self.subTest(command=command, error=type(exception).__name__):
                    output, errors = io.StringIO(), io.StringIO()
                    with patch("sys.argv", ["dotbot", command, "--proxy-from-env"]), \
                            patch("dotbot.cli.config.load", return_value={}), \
                            patch("dotbot.cli.config.token", return_value="synthetic-bot-token"), \
                            patch("dotbot.cli.Store"), \
                            patch("dotbot.relay.run", side_effect=exception), \
                            redirect_stdout(output), redirect_stderr(errors):
                        with self.assertRaises(SystemExit) as caught:
                            main()
                    self.assertEqual(caught.exception.code, 1)
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("Relay failed", errors.getvalue())
                    self.assertNotIn(secret, errors.getvalue())
                    self.assertNotIn("synthetic-bot-token", errors.getvalue())
                    self.assertNotIn("Traceback", errors.getvalue())

    async def test_rest_and_gateway_use_proxy_without_network(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://test-user:test-pass@proxy.invalid:8080"}, clear=True):
            relay = Relay({}, None, proxy_from_env=True)
        http = relay.http
        http.loop = asyncio.get_running_loop()
        http._global_over = asyncio.Event()
        http._global_over.set()
        response = SimpleNamespace(status=200, headers={"content-type": "application/json"},
                                   text=AsyncMock(return_value="{}"))
        session = Mock()
        session.request.return_value = AsyncMock()
        session.request.return_value.__aenter__.return_value = response
        session.ws_connect = AsyncMock()
        # Replace only the transport: exercise the pinned client's REST and WS paths.
        http._HTTPClient__session = session
        await http.request(Route("GET", "/users/@me"))
        await http.ws_connect("wss://gateway.discord.gg")
        for call in (session.request.call_args, session.ws_connect.call_args):
            self.assertEqual(call.kwargs["proxy"], "http://proxy.invalid:8080")
            self.assertEqual(call.kwargs["proxy_auth"].password, "test-pass")


if __name__ == "__main__":
    unittest.main()
