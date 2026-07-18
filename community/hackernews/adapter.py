"""Hacker News scanner using Algolia pagination and exact mention checks."""

from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        try:
            source_port = source.port or (443 if source.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            source_port, target_port = -2, -1
        if (
            source.scheme.casefold() != target.scheme.casefold()
            or (source.hostname or "").casefold() != (target.hostname or "").casefold()
            or source_port != target_port
        ):
            raise urllib.error.HTTPError(
                newurl, code, "cross-origin redirect refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_SameOriginRedirectHandler()).open(
        req, timeout=timeout
    )


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_json(raw: bytes | str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_key,
        parse_constant=_reject_constant,
    )


def _object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and 1 <= len(value) <= 20
    )


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return ""
    if isinstance(value, str) and len(value) <= maximum:
        return value
    return None


def _nonnegative_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 10**12
    )


def _normalize_hit(value: object, *, story: bool) -> dict | None:
    if not isinstance(value, dict) or not _object_id(value.get("objectID")):
        return None
    author = value.get("author", "")
    if author is None:
        author = ""
    if not isinstance(author, str) or len(author) > 1_000:
        return None
    normalized: dict = {"object_id": value["objectID"], "author": author}
    for field in ("title", "comment_text", "story_text"):
        text = _optional_text(value.get(field), 1_000_000)
        if text is None:
            return None
        normalized[field] = text
    for field in ("points", "num_comments"):
        raw = value.get(field)
        if raw is None:
            raw = 0
        if not _nonnegative_int(raw):
            return None
        normalized[field] = raw
    if story and not normalized["title"]:
        return None
    return normalized


class HackerNewsScanner:
    name = "hackernews"
    _POLL_BUDGET_SECONDS = 45

    def configure(self) -> dict:
        return {
            "enabled": False,
            "watch_keywords": [],
            "username": "",
            "min_points": 100,
            "max_items": 100,
            "max_pages": 3,
            "keywords_per_poll": 2,
        }

    @staticmethod
    def _utc_now_z() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _api_get(self, endpoint: str, params: dict) -> dict | None:
        query = urllib.parse.urlencode(params)
        url = f"https://hn.algolia.com/api/v1/{endpoint}?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": "HiveScanner/1.0"})
        deadline = getattr(self, "_poll_deadline", None)
        timeout = 15.0
        if isinstance(deadline, (int, float)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, max(0.1, remaining))
        try:
            with _urlopen(req, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("response exceeded 2 MB")
                result = _strict_json(raw)
                return result if isinstance(result, dict) else None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[hackernews] API error: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _load_state(watermark: str) -> dict | None:
        try:
            state = _strict_json(watermark)
        except (json.JSONDecodeError, TypeError, ValueError):
            state = None
        if isinstance(state, dict) and state.get("version") == 4:
            state.setdefault("keyword_next_index", 0)
            keyword_seen = state.get("keyword_seen")
            keyword_next_index = state.get("keyword_next_index")
            mention_seen = state.get("mention_seen")
            mention_pending = state.get("mention_pending")
            numeric_fields = (
                state.get("created_epoch"),
                state.get("mention_page"),
                state.get("mention_since_epoch"),
                state.get("mention_until_epoch"),
            )
            if (
                not isinstance(keyword_seen, dict)
                or len(keyword_seen) > 20
                or not all(
                    isinstance(scope, str)
                    and len(scope) <= 128
                    and isinstance(ids, list)
                    and len(ids) <= 500
                    and all(_object_id(value) for value in ids)
                    for scope, ids in keyword_seen.items()
                )
                or isinstance(keyword_next_index, bool)
                or not isinstance(keyword_next_index, int)
                or not 0 <= keyword_next_index <= 1_000_000
                or not isinstance(state.get("mention_username"), str)
                or len(state["mention_username"]) > 100
                or not isinstance(state.get("mention_initialized"), bool)
                or not isinstance(mention_seen, list)
                or len(mention_seen) > 5_000
                or not all(_object_id(value) for value in mention_seen)
                or not isinstance(mention_pending, list)
                or len(mention_pending) > 5_000
                or not all(_object_id(value) for value in mention_pending)
                or not all(_nonnegative_int(value) for value in numeric_fields)
                or state["mention_page"] > 1_000
                or state["mention_since_epoch"] > state["mention_until_epoch"]
            ):
                return None
            return state
        if isinstance(state, dict) and state.get("version") == 3:
            legacy_seen = state.get("seen", [])
            if not isinstance(legacy_seen, list):
                return None
            mention_seen = [
                str(value).split(":", 1)[1]
                for value in legacy_seen
                if isinstance(value, str) and value.startswith("mention:")
            ] if isinstance(legacy_seen, list) else []
            return {
                "version": 4,
                "keyword_seen": {},
                "keyword_next_index": 0,
                "created_epoch": state.get("created_epoch", 0),
                "mention_username": str(state.get("mention_username") or ""),
                "mention_initialized": bool(state.get("mention_initialized")),
                "mention_seen": mention_seen[-5000:],
                "mention_page": 0,
                "mention_since_epoch": 0,
                "mention_until_epoch": 0,
                "mention_pending": [],
            }
        if isinstance(watermark, str) and watermark.lstrip().startswith(("{", "[")):
            return None
        return {
            "version": 4,
            "keyword_seen": {},
            "keyword_next_index": 0,
            "created_epoch": 0,
            "mention_username": "",
            "mention_initialized": False,
            "mention_seen": [],
            "mention_page": 0,
            "mention_since_epoch": 0,
            "mention_until_epoch": 0,
            "mention_pending": [],
        }

    @staticmethod
    def _dump_state(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def _search(
        self,
        endpoint: str,
        params: dict,
        page_size: int,
        max_pages: int,
        *,
        require_complete: bool = True,
    ) -> list[dict] | None:
        hits: list[dict] = []
        for page in range(max_pages):
            result = self._api_get(
                endpoint,
                {**params, "hitsPerPage": page_size, "page": page},
            )
            if result is None or not isinstance(result.get("hits"), list):
                return None
            page_hits = result["hits"]
            if not all(isinstance(value, dict) for value in page_hits):
                return None
            hits.extend(page_hits)
            total_pages = result.get("nbPages")
            if (
                isinstance(total_pages, bool)
                or not isinstance(total_pages, int)
                or not 0 <= total_pages <= 1_000
            ):
                return None
            if total_pages == 0 or page + 1 >= total_pages:
                return hits
            if page + 1 >= max_pages:
                if require_complete:
                    print("[hackernews] search exceeded max_pages", file=sys.stderr)
                    return None
                return hits
        return hits

    @staticmethod
    def _plain_text(value: object) -> str:
        raw = value if isinstance(value, str) else ""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(raw))).strip()

    def poll(self, config: dict, watermark: str) -> tuple[list[dict], str]:
        self._poll_deadline = time.monotonic() + self._POLL_BUDGET_SECONDS
        keywords_raw = config.get("watch_keywords", [])
        if (
            not isinstance(keywords_raw, list)
            or len(keywords_raw) > 20
            or not all(isinstance(value, str) for value in keywords_raw)
            or any(
                not value
                or value != value.strip()
                or len(value) > 100
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in keywords_raw
            )
        ):
            return [], watermark
        keywords = list(keywords_raw)
        raw_username = config.get("username", "")
        fallback_username = config.get("_username", "")
        if not isinstance(raw_username, str) or not isinstance(fallback_username, str):
            return [], watermark
        username = raw_username or fallback_username
        if (
            len(username) > 100
            or username != username.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in username)
        ):
            return [], watermark
        min_points = config.get("min_points", 100)
        page_size = config.get("max_items", 100)
        max_pages = config.get("max_pages", 3)
        keywords_per_poll = config.get("keywords_per_poll", 2)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (min_points, page_size, max_pages, keywords_per_poll)
            )
            or not 0 <= min_points <= 1_000_000_000
            or not 1 <= page_size <= 100
            or not 1 <= max_pages <= 5
            or not 1 <= keywords_per_poll <= 5
        ):
            print("[hackernews] pagination limits are invalid", file=sys.stderr)
            return [], watermark

        state = self._load_state(watermark)
        if state is None:
            print("[hackernews] invalid persisted state; preserving watermark", file=sys.stderr)
            return [], watermark
        prior_keyword_seen = state.get("keyword_seen", {})
        if not isinstance(prior_keyword_seen, dict):
            prior_keyword_seen = {}
        next_keyword_seen: dict[str, list[str]] = {}
        pollen: list[dict] = []
        emitted_story_ids: set[str] = set()
        discovered_at = self._utc_now_z()
        scan_started_epoch = int(datetime.now(timezone.utc).timestamp())

        unique_keywords: dict[str, str] = {}
        for keyword in keywords:
            unique_keywords.setdefault(keyword.casefold(), keyword)
        keyword_values = list(unique_keywords.values())
        active_keyword_scopes = {
            f"{keyword.casefold()}\0{min_points}" for keyword in keyword_values
        }
        next_keyword_seen = {
            key: value
            for key, value in prior_keyword_seen.items()
            if key in active_keyword_scopes and isinstance(value, list)
        }
        try:
            keyword_next_index = (
                int(state.get("keyword_next_index", 0) or 0) % len(keyword_values)
                if keyword_values
                else 0
            )
        except (TypeError, ValueError):
            keyword_next_index = 0
        selected_keywords = [
            keyword_values[(keyword_next_index + offset) % len(keyword_values)]
            for offset in range(min(keywords_per_poll, len(keyword_values)))
        ]
        for keyword in selected_keywords:
            keyword_scope = f"{keyword.casefold()}\0{min_points}"
            raw_prior = prior_keyword_seen.get(keyword_scope, [])
            prior_order = (
                list(dict.fromkeys(str(value) for value in raw_prior if value))[-500:]
                if isinstance(raw_prior, list)
                else []
            )
            prior_ids = set(prior_order)
            next_order = list(prior_order)
            keyword_initialized = keyword_scope in prior_keyword_seen
            hits = self._search(
                "search",
                {
                    "tags": "story",
                    "query": keyword,
                    "numericFilters": f"points>={min_points}",
                },
                page_size,
                max_pages,
                require_complete=False,
            )
            if hits is None:
                if keyword_initialized:
                    next_keyword_seen[keyword_scope] = prior_order
                continue
            parsed_hits = [_normalize_hit(hit, story=True) for hit in hits]
            if any(hit is None for hit in parsed_hits):
                print("[hackernews] malformed keyword search page", file=sys.stderr)
                if keyword_initialized:
                    next_keyword_seen[keyword_scope] = prior_order
                continue
            for hit in parsed_hits:
                assert hit is not None
                object_id = hit["object_id"]
                if object_id in prior_ids:
                    continue
                prior_ids.add(object_id)
                next_order.append(object_id)
                if not keyword_initialized:
                    continue
                if object_id in emitted_story_ids:
                    continue
                emitted_story_ids.add(object_id)
                title = hit["title"]
                pollen.append({
                    "id": f"hn-story-{object_id}",
                    "source": "hackernews",
                    "type": "hn_top_story",
                    "title": title[:100],
                    "preview": title[:200],
                    "discovered_at": discovered_at,
                    "author": hit["author"],
                    "author_name": hit["author"],
                    "group": "Hacker News",
                    "url": f"https://news.ycombinator.com/item?id={object_id}",
                    "metadata": {
                        "points": hit["points"],
                        "num_comments": hit["num_comments"],
                        "keyword": keyword,
                    },
                })

            next_keyword_seen[keyword_scope] = next_order[-500:]
        if keyword_values:
            keyword_next_index = (
                keyword_next_index + len(selected_keywords)
            ) % len(keyword_values)

        mention_initialized = (
            state["mention_initialized"]
            and state["mention_username"].casefold()
            == username.casefold()
        )
        raw_mention_seen = state.get("mention_seen", [])
        mention_seen_order = (
            list(dict.fromkeys(raw_mention_seen))[-5000:]
            if mention_initialized
            else []
        )
        mention_seen = set(mention_seen_order)
        next_created_epoch = state["created_epoch"]
        next_mention_initialized = mention_initialized
        next_mention_page = 0
        next_mention_since = 0
        next_mention_until = 0
        next_mention_pending: list[str] = []
        if username:
            saved_mention_since = state["mention_since_epoch"]
            saved_mention_until = state["mention_until_epoch"]
            saved_mention_page = state["mention_page"]
            if mention_initialized and saved_mention_until:
                mention_since = saved_mention_since
                mention_until = saved_mention_until
                mention_page = saved_mention_page
                raw_pending = state.get("mention_pending", [])
                pending_order = (
                    list(dict.fromkeys(raw_pending))[-5000:]
                )
            else:
                mention_since = (
                    max(0, next_created_epoch - 60)
                    if mention_initialized and next_created_epoch
                    else scan_started_epoch - 86400
                )
                mention_until = scan_started_epoch
                mention_page = 0
                pending_order = []
            pending_ids = set(pending_order)
            pattern = re.compile(
                rf"(?<![\w-])@?{re.escape(username)}(?![\w-])", re.I
            )
            mention_ok = True
            mention_complete = False
            pages_completed = 0
            pages_to_fetch = max_pages if mention_initialized else 1
            for page_offset in range(pages_to_fetch):
                page_number = mention_page + page_offset
                result = self._api_get(
                    "search_by_date",
                    {
                        "query": username,
                        "numericFilters": (
                            f"created_at_i>{mention_since},created_at_i<={mention_until}"
                        ),
                        "hitsPerPage": page_size,
                        "page": page_number,
                    },
                )
                if result is None or not isinstance(result.get("hits"), list):
                    mention_ok = False
                    break
                total_pages = result.get("nbPages")
                if (
                    isinstance(total_pages, bool)
                    or not isinstance(total_pages, int)
                    or not 0 <= total_pages <= 1_000
                ):
                    mention_ok = False
                    break
                parsed_hits = [
                    _normalize_hit(hit, story=False) for hit in result["hits"]
                ]
                if any(hit is None for hit in parsed_hits):
                    mention_ok = False
                    break
                for hit in parsed_hits:
                    assert hit is not None
                    object_id = hit["object_id"]
                    author = hit["author"]
                    title = self._plain_text(hit["title"])
                    comment = self._plain_text(hit["comment_text"])
                    story_text = self._plain_text(hit["story_text"])
                    combined = f"{title} {comment} {story_text}"
                    if (
                        author.casefold() == username.casefold()
                        or not pattern.search(combined)
                    ):
                        continue
                    if object_id in mention_seen or object_id in pending_ids:
                        continue
                    pending_ids.add(object_id)
                    pending_order.append(object_id)
                    if not mention_initialized:
                        continue
                    display = title or comment[:100] or story_text[:100]
                    pollen.append({
                        "id": f"hn-mention-{object_id}",
                        "source": "hackernews",
                        "type": "hn_mention",
                        "title": display[:100],
                        "preview": (comment or story_text or display)[:200],
                        "discovered_at": discovered_at,
                        "author": author,
                        "author_name": author,
                        "group": "Hacker News",
                        "url": f"https://news.ycombinator.com/item?id={object_id}",
                        "metadata": {
                            "points": hit["points"],
                            "num_comments": hit["num_comments"],
                        },
                    })
                pages_completed += 1
                if total_pages == 0 or page_number + 1 >= total_pages:
                    mention_complete = True
                    break
            if not mention_initialized and mention_ok:
                # First enable is deliberately quiet regardless of older pages.
                mention_complete = True
            if mention_ok and mention_complete:
                mention_seen_order = [*mention_seen_order, *pending_order][-5000:]
                next_created_epoch = mention_until
                next_mention_initialized = True
            elif mention_ok or pages_completed:
                next_mention_page = mention_page + pages_completed
                next_mention_since = mention_since
                next_mention_until = mention_until
                next_mention_pending = pending_order[-5000:]
            else:
                next_mention_page = state["mention_page"]
                next_mention_since = state["mention_since_epoch"]
                next_mention_until = state["mention_until_epoch"]
                next_mention_pending = state["mention_pending"][-5000:]

        next_state = {
            "version": 4,
            "keyword_seen": next_keyword_seen,
            "keyword_next_index": keyword_next_index,
            "created_epoch": next_created_epoch if username else 0,
            "mention_username": username,
            "mention_initialized": next_mention_initialized if username else False,
            "mention_seen": mention_seen_order if username else [],
            "mention_page": next_mention_page if username else 0,
            "mention_since_epoch": next_mention_since if username else 0,
            "mention_until_epoch": next_mention_until if username else 0,
            "mention_pending": next_mention_pending if username else [],
        }
        return pollen, self._dump_state(next_state)


if __name__ == "__main__" and "--sandboxed" in sys.argv:
    data = _strict_json(sys.stdin.read())
    if not isinstance(data, dict):
        raise ValueError("sandbox payload must be an object")
    scanner = HackerNewsScanner()
    if data.get("command") == "poll":
        poll_pollen, wm = scanner.poll(data.get("config"), data.get("watermark"))
        print(json.dumps({"pollen": poll_pollen, "watermark": wm}))
    elif data.get("command") == "configure":
        print(json.dumps({"config": scanner.configure()}))
    else:
        raise ValueError("unsupported sandbox command")
