"""Port preflight must work for users unable to bind privileged ports."""
import errno
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tls


class PortChecks(unittest.TestCase):
    def check(self, bind_error, listeners="", ss_error=None):
        cluster = Mock()
        cluster.call.return_value.stdout = ""
        with patch.object(tls.socket, "socket") as socket_factory, \
                patch.object(tls.subprocess, "run") as run:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = bind_error
            run.return_value.stdout = listeners
            run.side_effect = ss_error
            tls.check_port(cluster, {"tls": {"host_https_port": 443}})
            return run

    def test_privileged_port_available(self):
        run = self.check(PermissionError(errno.EACCES, "Permission denied"))
        self.assertEqual(run.call_args.args[0], ["ss", "-H", "-ltn", "sport = :443"])

    def test_privileged_port_in_use(self):
        with self.assertRaisesRegex(ValueError, "使用中"):
            self.check(PermissionError(errno.EACCES, "Permission denied"), "LISTEN 0 128 *:443 *:*")

    def test_address_in_use(self):
        with self.assertRaisesRegex(ValueError, "使用中"):
            self.check(OSError(errno.EADDRINUSE, "Address in use"))

    def test_missing_ss(self):
        with self.assertRaisesRegex(ValueError, "iproute2"):
            self.check(PermissionError(errno.EACCES, "Permission denied"), ss_error=FileNotFoundError())

    def test_ss_failure(self):
        with self.assertRaisesRegex(ValueError, "確認に失敗"):
            self.check(PermissionError(errno.EACCES, "Permission denied"),
                       ss_error=subprocess.CalledProcessError(1, "ss"))

    def test_other_socket_error(self):
        with self.assertRaisesRegex(ValueError, "確認できません"):
            self.check(OSError(errno.EADDRNOTAVAIL, "Address unavailable"))
