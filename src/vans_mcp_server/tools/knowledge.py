from __future__ import annotations

import json
from typing import Any

# Course mock Knowledge Portal pages (Milestone 1 — no real Notion OAuth).
MOCK_PAGES: list[dict[str, Any]] = [
    {
        "id": "page_hualien_guide",
        "title": "花蓮智慧導覽筆記",
        "summary": "花蓮景點、交通與在地資料來源整理。",
        "tags": ["hualien", "quest", "guide"],
    },
    {
        "id": "page_portal_awakening",
        "title": "Portal Awakening 概念",
        "summary": "Tool / Skill / Portal / MCP 的課程比喻與 Level 10 目標。",
        "tags": ["portal", "mcp", "level10"],
    },
    {
        "id": "page_calendar_notes",
        "title": "行程規劃草稿",
        "summary": "示範用行事曆筆記：週末花蓮行程草案。",
        "tags": ["calendar", "draft"],
    },
]


def search_pages(query: str, limit: int = 5) -> dict[str, Any]:
    q = (query or "").strip().lower()
    limit = max(1, min(limit, 20))
    if not q:
        hits = MOCK_PAGES[:limit]
    else:
        hits = []
        for page in MOCK_PAGES:
            hay = " ".join(
                [
                    page["id"],
                    page["title"],
                    page["summary"],
                    " ".join(page.get("tags", [])),
                ]
            ).lower()
            if q in hay:
                hits.append(page)
            if len(hits) >= limit:
                break
    return {
        "query": query,
        "count": len(hits),
        "pages": hits,
        "source": "course_mock_knowledge_portal",
    }


def read_page(page_id: str) -> dict[str, Any]:
    for page in MOCK_PAGES:
        if page["id"] == page_id:
            return {
                "found": True,
                "page": {
                    **page,
                    "body": (
                        f"# {page['title']}\n\n{page['summary']}\n\n"
                        "（此為課程假資料，尚未連接真實 Notion。）"
                    ),
                },
                "source": "course_mock_knowledge_portal",
            }
    return {
        "found": False,
        "page_id": page_id,
        "error": "page_not_found",
        "hint": "Try notion_search_pages first to list available mock pages.",
        "source": "course_mock_knowledge_portal",
    }


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
