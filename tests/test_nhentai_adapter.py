import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core.http import HttpResponse
from mediagent.platforms.nhentai import auth, client, links, parser


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nhentai"


class FakeHttpClient:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def get_json(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("GET", url, headers or {}))
        return self.responses.pop(0)

    def post_form(self, url: str, data: dict[str, str], *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("POST", url, headers or {}))
        return self.responses.pop(0)


class NhentaiLinkTests(unittest.TestCase):
    def test_gallery_link_is_canonical_and_exact(self) -> None:
        link = links.parse_gallery_link("http://www.nhentai.net/g/00123456/?ignored=yes#fragment")
        self.assertIsNotNone(link)
        assert link
        self.assertEqual(link.gallery_id, "123456")
        self.assertEqual(link.canonical_url, "https://nhentai.net/g/123456/")

    def test_rejects_non_gallery_and_foreign_links(self) -> None:
        self.assertIsNone(links.parse_gallery_link("https://nhentai.net/search/?q=x"))
        self.assertIsNone(links.parse_gallery_link("https://example.test/g/123/"))


class NhentaiParserTests(unittest.TestCase):
    def test_gallery_becomes_one_comic_with_ordered_page_manifest(self) -> None:
        payload = json.loads((FIXTURES / "gallery.json").read_text(encoding="utf-8"))
        item = parser.parse_gallery(payload)

        self.assertEqual(item["platform"], "nhentai")
        self.assertEqual(item["remote_id"], "gallery:123456")
        self.assertEqual(item["media_type"], "photo")
        self.assertEqual(item["metadata"]["work_type"], "comic")
        self.assertEqual(item["metadata"]["storage_category"], "comic-pages")
        self.assertEqual(item["metadata"]["comic"]["provider_work_id"], "gallery:123456")
        self.assertTrue(item["metadata"]["comic"]["is_one_shot"])
        files = item["metadata"]["files"]
        self.assertEqual([entry["page_number"] for entry in files], [1, 2, 3])
        self.assertEqual([entry["extension"] for entry in files], [".jpg", ".webp", ".png"])
        self.assertTrue(all(entry["storage_category"] == "comic-pages" for entry in files))

    def test_gallery_without_pages_stays_a_comic_with_empty_manifest(self) -> None:
        item = parser.parse_gallery({"id": 42, "title": {"pretty": "Empty"}, "pages": []})
        self.assertEqual(item["metadata"]["work_type"], "comic")
        self.assertEqual(item["metadata"]["files"], [])
        self.assertEqual(item["metadata"]["page_count"], 0)

    def test_declared_page_count_exposes_an_incomplete_manifest(self) -> None:
        item = parser.parse_gallery(
            {
                "id": 42,
                "title": {"pretty": "Incomplete"},
                "num_pages": 2,
                "pages": [{"number": 1, "path": "https://evil.test/1.jpg"}],
            }
        )
        self.assertEqual(item["metadata"]["page_count"], 2)
        self.assertEqual(item["metadata"]["manifest_page_count"], 0)
        self.assertEqual(item["metadata"]["files"], [])

    def test_rejects_untrusted_and_traversing_image_paths(self) -> None:
        self.assertIsNone(parser.image_url("https://evil.test/page.jpg"))
        self.assertIsNone(parser.image_url("galleries/1/../../secret"))
        self.assertEqual(
            parser.image_url("galleries/1/1.jpg"),
            "https://i.nhentai.net/galleries/1/1.jpg",
        )


class NhentaiClientTests(unittest.TestCase):
    def test_resolve_exact_returns_one_normalized_chapter(self) -> None:
        body = (FIXTURES / "gallery.json").read_bytes()
        fake = FakeHttpClient([HttpResponse(200, {}, body)])
        items = client.resolve_exact("https://nhentai.net/g/123456/", http_client=fake)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["remote_id"], "gallery:123456")
        self.assertEqual(fake.requests[0][1], "https://nhentai.net/api/v2/galleries/123456")

    def test_gallery_unavailable_does_not_expose_response_body(self) -> None:
        secret = "should-never-appear"
        fake = FakeHttpClient([HttpResponse(404, {}, secret.encode())])
        with self.assertRaises(client.NhentaiApiError) as caught:
            client.get_gallery("123", http_client=fake)
        self.assertEqual(caught.exception.code, "nhentai_gallery_unavailable")
        self.assertNotIn(secret, str(caught.exception))

    def test_favorites_collects_all_pages_and_deduplicates_targets(self) -> None:
        responses = [
            HttpResponse(
                200,
                {},
                json.dumps(
                    {"result": [{"id": 1}, {"id": 2}], "total": 3, "has_next": True}
                ).encode(),
            ),
            HttpResponse(
                200,
                {},
                json.dumps(
                    {"result": [{"id": 2}, {"id": 3}], "total": 3, "has_next": False}
                ).encode(),
            ),
        ]
        fake = FakeHttpClient(responses)
        result = client.collect_favorites(http_client=fake, session=_session())
        self.assertTrue(result["complete"])
        self.assertEqual(result["pages_fetched"], 2)
        self.assertEqual(result["target_policy"], "exact")
        self.assertEqual(
            [target["provider_work_id"] for target in result["targets"]],
            ["gallery:1", "gallery:2", "gallery:3"],
        )

    def test_bounded_favorites_result_is_explicitly_incomplete(self) -> None:
        fake = FakeHttpClient(
            [HttpResponse(200, {}, json.dumps({"items": [{"id": 1}], "next_page": 2}).encode())]
        )
        result = client.collect_favorites(
            http_client=fake,
            session=_session(),
            max_pages=1,
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["next_page"], 2)

    def test_favorites_supports_num_pages_envelope(self) -> None:
        fake = FakeHttpClient(
            [
                HttpResponse(200, {}, json.dumps({"result": [{"id": 1}], "num_pages": 2}).encode()),
                HttpResponse(200, {}, json.dumps({"result": [], "num_pages": 2}).encode()),
            ]
        )
        result = client.collect_favorites(http_client=fake, session=_session())
        self.assertTrue(result["complete"])
        self.assertEqual(result["pages_fetched"], 2)


class NhentaiSessionTests(unittest.TestCase):
    def test_netscape_cookie_txt_is_reusable_and_remains_cookie_txt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".nhentai.net\tTRUE\t/\tTRUE\t0\taccess_token\ttest-access-token\n"
                "#HttpOnly_.nhentai.net\tTRUE\t/\tTRUE\t0\tcf_clearance\tclearance\n",
                encoding="utf-8",
            )
            env = {auth.NHENTAI_COOKIE_FILE_ENV: str(path)}
            loaded = auth.load_session(env=env, cwd=Path(temp_dir))
            auth.save_session(loaded, env=env, cwd=Path(temp_dir))
            persisted = path.read_text(encoding="utf-8")
            mode = path.stat().st_mode & 0o777
        self.assertEqual(loaded["access_token"], "test-access-token")
        self.assertEqual(mode, 0o600)
        self.assertTrue(persisted.startswith("# Netscape HTTP Cookie File"))
        self.assertNotIn("{", persisted)

    def test_session_file_is_private_and_reusable(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials" / "nhentai.json"
            env = {auth.NHENTAI_SESSION_FILE_ENV: str(path)}
            auth.save_session(_session(), env=env, cwd=Path(temp_dir))
            loaded = auth.load_session(env=env, cwd=Path(temp_dir))
            mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(loaded["access_token"], "test-access-token")

    def test_refresh_rotates_cookie_and_persists_without_returning_secret(self) -> None:
        fake = FakeHttpClient(
            [
                HttpResponse(
                    200,
                    {"Set-Cookie": "access_token=rotated-token; Domain=nhentai.net; Path=/; Secure"},
                    b"{}",
                )
            ]
        )
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nhentai.json"
            env = {auth.NHENTAI_SESSION_FILE_ENV: str(path)}
            auth.save_session(_session(), env=env, cwd=Path(temp_dir))
            result = auth.refresh_saved_session(http_client=fake, env=env, cwd=Path(temp_dir))
            loaded = auth.load_session(env=env, cwd=Path(temp_dir))
        self.assertEqual(result["status"], "refreshed")
        self.assertNotIn("rotated-token", json.dumps(result))
        header = auth.session_headers(loaded)["Cookie"]
        self.assertIn("access_token=rotated-token", header)
        self.assertEqual(auth.session_headers(loaded)["Authorization"], "User rotated-token")
        self.assertNotIn("test-access-token", header)

    def test_auth_failure_does_not_include_cookie_value(self) -> None:
        secret = "test-access-token"
        fake = FakeHttpClient([HttpResponse(403, {}, b"denied")])
        with self.assertRaises(auth.NhentaiAuthError) as caught:
            auth.refresh_session(http_client=fake, session=_session(token=secret))
        self.assertEqual(caught.exception.code, "nhentai_auth_required")
        self.assertNotIn(secret, str(caught.exception))


def _session(token: str = "test-access-token") -> dict:
    return {
        "schema_version": 1,
        "cookies": [
            {
                "name": "access_token",
                "value": token,
                "domain": "nhentai.net",
                "path": "/",
                "secure": True,
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
