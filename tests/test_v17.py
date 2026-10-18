"""0.5.1: DNS filter in the agent image (image/hands-dns-conf.sh) and the firewall probes of `hands <p> check`.

The generator is a bash script; these tests run it for real (no docker, no network) and check the
dnsmasq config it prints, that it fails closed on anything that is not an exact lowercase hostname, and
that nothing an `allowed-domains` line contains can become a dnsmasq directive.
"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"

from hands.__main__ import FIREWALL_PROBE, firewall_checks  # noqa: E402

GEN = ROOT / "image" / "hands-dns-conf.sh"
FIREWALL = ROOT / "image" / "init-firewall.sh"
UP = ["153.92.2.6", "1.1.1.1"]


def gen(names: str, *args: str) -> subprocess.CompletedProcess:
    argv = ["bash", str(GEN), *(args or ["allowed-domains", *UP])]
    return subprocess.run(argv, input=names, capture_output=True, text=True, timeout=30)


def directives(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln and not ln.startswith("#")]


class TestDnsConfig(unittest.TestCase):
    def test_allowlist_becomes_server_and_ipset_lines(self):
        r = gen("api.anthropic.com\n# a comment\n\n  sentry.io  \nsentry.io\r\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = directives(r.stdout)
        for h in ("api.anthropic.com", "sentry.io"):
            for up in UP:
                self.assertIn(f"server=/{h}/{up}", d)
            self.assertIn(f"ipset=/{h}/allowed-domains", d)
        self.assertEqual(d.count("ipset=/sentry.io/allowed-domains"), 1, "duplicates are collapsed")
        # the fixed part: local-only resolver, nothing else resolves, pid where the firewall looks
        for fixed in ("no-resolv", "no-hosts", "listen-address=127.0.0.1", "bind-interfaces", "local=/#/",
                      "user=root", "pid-file=/run/hands-dns/dnsmasq.pid", "filter-AAAA", "stop-dns-rebind"):
            self.assertIn(fixed, d)
        self.assertFalse([ln for ln in d if ln.startswith("server=") and "/#/" in ln], "no catch-all upstream")
        # every directive is one of the known forms with a safe value
        ok = re.compile(r"^(?:[a-zA-Z-]+(?:=[A-Za-z0-9_./#-]+)?|server=/[a-z0-9.-]+/[0-9.]+|ipset=/[a-z0-9.-]+/[A-Za-z0-9_-]+)$")
        for ln in d:
            self.assertRegex(ln, ok)

    def test_empty_allowlist_resolves_nothing(self):
        r = gen("# nothing\n\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([ln for ln in directives(r.stdout) if ln.startswith(("server=", "ipset="))])
        self.assertIn("local=/#/", r.stdout)

    def test_invalid_names_fail_closed_with_no_output(self):
        bad = ["*.evil.com", "evil.com.", ".evil.com", "a..b", "Evil.com", "nodot", "-a.com", "a-.com",
               "1.2.3.4", "x.com/1.1.1.1", "a.com#c", "a.com b.com", "a_b.com", "a.com,b.com",
               "x" * 64 + ".com", "é.com", "a.com\tserver=/x/1.1.1.1", "a.com;echo", "$(id).com", "a.com\\"]
        for name in bad:
            with self.subTest(name=name):
                r = gen(f"good.example.com\n{name}\n")
                self.assertEqual(r.returncode, 2, f"{name!r} accepted: {r.stdout}")
                self.assertEqual(r.stdout, "", "fails closed: no partial config")
                self.assertIn("not an exact lowercase hostname", r.stderr)

    def test_newline_in_a_name_cannot_inject_a_directive(self):
        # a file is read line by line, so the only way to smuggle a directive is as its own line
        r = gen("a.com\nserver=/evil.com/9.9.9.9\naddress=/#/1.2.3.4\n")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        r = gen("a.com\nconf-dir=/etc\n")
        self.assertEqual(r.returncode, 2)

    def test_upstream_and_set_name_are_validated(self):
        for ups in (["1.1.1.1\nserver=/x/2.2.2.2"], ["one.one.one.one"], ["1.1.1"], ["1.1.1.256"], ["01.1.1.1"],
                    ["127.0.0.1"], ["0.0.0.0"], ["::1"], ["1.1.1.1/24"], [""]):
            with self.subTest(upstream=ups):
                r = gen("a.com\n", "allowed-domains", *ups)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertEqual(r.stdout, "")
        for s in ("", "a b", "a/b", "a;b", "x" * 32, "allowed-domains\nbad"):
            with self.subTest(set=s):
                r = gen("a.com\n", s, "1.1.1.1")
                self.assertEqual(r.returncode, 2, r.stdout)
        r = gen("a.com\n")  # no upstream at all
        self.assertEqual(subprocess.run(["bash", str(GEN), "allowed-domains"], input="a.com\n",
                                        capture_output=True, text=True).returncode, 2)
        r = gen("a.com\n", "allowed-domains", "10.0.0.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("server=/a.com/10.0.0.2", r.stdout)

    def test_firewall_script_wires_dnsmasq_and_closes_the_old_holes(self):
        s = FIREWALL.read_text()
        self.assertIn("hands-dns-conf.sh allowed-domains", s)
        self.assertIn("dnsmasq --conf-file=", s)
        self.assertIn("nameserver 127.0.0.1", s)
        self.assertIn("--uid-owner 0", s)
        self.assertIn("--dport 53 -j DROP", s)
        for gone in ("-p udp --dport 53 -j ACCEPT\n", "--dport 22", "api.github.com", "HOST_NETWORK", "ipset add allowed-domains \"$cidr\""):
            self.assertNotIn(gone, s, f"old rule still present: {gone!r}")
        self.assertIn("example.com resolved", s, "startup verifies the filter")
        # built-in allowlist: Anthropic API and the npm registry only; no telemetry hosts (suffix match
        # would open *.sentry.io / *.statsig.com ingest endpoints as an exfiltration route)
        builtin = s.split("ALLOWED_NAMES=$(printf", 1)[1].split("cat /etc/hands/allowed-domains", 1)[0]
        self.assertEqual(sorted(re.findall(r'"([a-z0-9.-]+)"', builtin)), ["api.anthropic.com", "registry.npmjs.org"])
        for tele in ("sentry", "statsig", "github"):
            self.assertNotIn(tele, builtin)
        df = (ROOT / "image" / "Dockerfile").read_text()
        self.assertIn("dnsmasq-base", df)
        self.assertIn("hands-dns-conf.sh /usr/local/bin/", df)
        self.assertEqual((ROOT / "image" / "versions.env").read_text().count("IMAGE_VARIANT=dns2"), 1)
        self.assertEqual(subprocess.run(["bash", "-n", str(FIREWALL)]).returncode, 0)


# a probe transcript of a correctly locked-down container
GOOD = """blocked
ip-blocked
anthropic=404
cannot-flush
no-sudo
v6-blocked
dns-allowed-ok
dns-random-blocked
dns-direct-blocked-1.1.1.1+udp
dns-direct-blocked-1.1.1.1+tcp
dns-direct-blocked-8.8.8.8+udp
dns-direct-blocked-8.8.8.8+tcp
dnsmasq-running
dnsmasq-survived
dnsconf-readonly
ssh-blocked
ghip-blocked
gh-dns-blocked
github=000
host-blocked
1000
"""


class TestFirewallProbe(unittest.TestCase):
    def test_probe_covers_every_marker_and_is_valid_bash(self):
        self.assertEqual(subprocess.run(["bash", "-n", "-c", FIREWALL_PROBE]).returncode, 0)
        for marker in GOOD.split()[:-1]:
            base = marker.split("=")[0].replace("-1.1.1.1+udp", "").replace("-8.8.8.8+tcp", "")
            self.assertIn(base.rsplit("-", 1)[0] if base.startswith("dns-direct") else base, FIREWALL_PROBE, marker)
        for ns in ("1.1.1.1", "8.8.8.8"):
            self.assertIn(ns, FIREWALL_PROBE)
        self.assertIn("+tcp", FIREWALL_PROBE)
        # a timed-out dig prints ';; communications error ...' on stdout: that must not count as an answer
        snippet = FIREWALL_PROBE.split("for ns in", 1)[1].split("done; done;", 1)[0]
        self.assertIn("grep -v '^;'", snippet)
        self.assertIn("140.82.112.3/22", FIREWALL_PROBE)
        self.assertIn("pkill -9 -x dnsmasq", FIREWALL_PROBE)

    def test_clean_transcript_passes_everything(self):
        checks = firewall_checks(GOOD)
        self.assertEqual(len(checks), 13)
        self.assertTrue(all(ok for ok, _ in checks), [l for ok, l in checks if not ok])

    def test_each_leak_fails_exactly_its_check(self):
        leaks = {
            "blocked": ("LEAK", "egress blocked for non-allowlisted sites"),
            "ip-blocked": ("LEAK-IP", "raw outside IP"),
            "anthropic=404": ("anthropic=000", "api.anthropic.com reachable"),
            "cannot-flush": ("FLUSHED", "change firewall"),
            "v6-blocked": ("LEAK6", "IPv6"),
            "dns-allowed-ok": ("DNS-ALLOWED-FAILED", "allowlisted name resolves"),
            "dns-random-blocked": ("DNS-RANDOM-RESOLVED", "non-allowlisted name"),
            "dns-direct-blocked-8.8.8.8+tcp": ("DNS-OPEN-8.8.8.8+tcp", "direct queries"),
            "dnsmasq-survived": ("DNSMASQ-KILLED", "kill or reconfigure"),
            "dnsconf-readonly": ("DNSCONF-WRITABLE", "kill or reconfigure"),
            "dnsmasq-running": ("DNSMASQ-MISSING", "kill or reconfigure"),
            "ssh-blocked": ("LEAK-SSH", "tcp/22"),
            "ghip-blocked": ("LEAK-GHIP", "GitHub IP"),
            "gh-dns-blocked": ("GH-DNS-RESOLVED", "api.github.com"),
            "github=000": ("github=200", "api.github.com"),
            "host-blocked": ("LEAK-HOST", "host"),
        }
        for good, (leak, label_part) in leaks.items():
            with self.subTest(leak=leak):
                out = "\n".join(leak if ln == good else ln for ln in GOOD.splitlines())
                failed = [l for ok, l in firewall_checks(out) if not ok]
                self.assertEqual(len(failed), 1, failed)
                self.assertIn(label_part, failed[0])

    def test_missing_markers_fail_not_pass(self):
        failed = [l for ok, l in firewall_checks("") if not ok]
        self.assertEqual(len(failed), 13)
        # a probe that died halfway must not pass the checks whose markers never printed
        half = "\n".join(GOOD.splitlines()[:6])
        self.assertTrue([l for ok, l in firewall_checks(half) if not ok and "DNS" in l])

    def test_github_probes_skipped_when_allowlisted(self):
        swap = {"ghip-blocked": "LEAK-GHIP", "gh-dns-blocked": "GH-DNS-RESOLVED", "github=000": "github=200"}
        out = "\n".join(swap.get(ln, ln) for ln in GOOD.splitlines())
        self.assertEqual(len([l for ok, l in firewall_checks(out) if not ok]), 2)
        checks = firewall_checks(out, {"api.github.com", "x.com"})
        self.assertTrue(all(ok for ok, _ in checks))
        self.assertTrue(any("skipped" in l and "api.github.com" in l for _, l in checks))


if __name__ == "__main__":
    unittest.main()
