import unittest

import ip2region.searcher as xdb
from shared_lib.utils.ip import IPResolver


class _FakeSearcher(xdb.Searcher):
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, ip: str | bytes) -> str:
        self.queries.append(ip.decode() if isinstance(ip, bytes) else ip)
        return "亚洲|中国|广东省|深圳市|宝安区|电信|"


class IPResolverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = IPResolver.__new__(IPResolver)
        self.searcher = _FakeSearcher()
        self.resolver._searcher = self.searcher

    def test_ipv4_mapped_ipv6_is_queried_as_ipv4(self) -> None:
        self.assertEqual(self.resolver.lookup("::ffff:df68:55e9"), ("中国", "广东省"))
        self.assertEqual(self.searcher.queries, ["223.104.85.233"])

    def test_bracketed_ipv4_mapped_endpoint_is_queried_as_ipv4(self) -> None:
        self.assertEqual(self.resolver.lookup("[::ffff:df68:c4f6]:0"), ("中国", "广东省"))
        self.assertEqual(self.searcher.queries, ["223.104.196.246"])

    def test_ipv4_endpoint_is_queried_without_port(self) -> None:
        self.assertEqual(self.resolver.lookup("106.40.125.2:37015"), ("中国", "广东省"))
        self.assertEqual(self.searcher.queries, ["106.40.125.2"])

    def test_native_ipv6_is_skipped_for_ipv4_database(self) -> None:
        self.assertIsNone(self.resolver.lookup("[fe80::dcdc:c76a:f3a4:6eee]:0"))
        self.assertEqual(self.searcher.queries, [])


if __name__ == "__main__":
    unittest.main()
